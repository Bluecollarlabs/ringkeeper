package org.bluecollarlabs.ringkeeper.app.ble

import android.annotation.SuppressLint
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.bluetooth.BluetoothStatusCodes
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.Build
import android.util.Log
import androidx.core.content.ContextCompat
import org.bluecollarlabs.ringkeeper.core.BleWriter
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeout
import java.util.UUID

/**
 * BluetoothGatt transport for the Oura ring, implementing the Rust core's [BleWriter].
 *
 * - Connects by the **bonded address** (no scan; after bonding Android resolves the
 *   ring's rotating private address via its IRK).
 * - createBond() if not bonded yet (Just-Works on a factory-reset ring — must happen
 *   in the post-reset window, see docs/ARCHITECTURE.md).
 * - MTU 247, discovers services, enables notifications/indications on EVERY notify
 *   characteristic of service 98ed0001 (…0003 plus Ring 5's …0004/5/6).
 * - [write] queues frames and writes them one at a time to …0002 as write-requests
 *   (WRITE_TYPE_DEFAULT, matching upstream's WriteType::WithResponse).
 * - Every inbound notification goes to [onFrame] (→ RingSession.pushFrame).
 *
 * UNTESTED ON HARDWARE. Bond/MTU/notify order follows upstream's macOS/Linux flow.
 */
@SuppressLint("MissingPermission") // callers check BLUETOOTH_CONNECT before use
class RingGatt(
    private val context: Context,
    private val address: String,
    private val onFrame: (ByteArray) -> Unit,
    private val onDisconnected: (String) -> Unit = {},
) : BleWriter {

    companion object {
        private const val TAG = "RingGatt"
        val SERVICE: UUID = UUID.fromString("98ed0001-a541-11e4-b6a0-0002a5d5c51b")
        val WRITE: UUID = UUID.fromString("98ed0002-a541-11e4-b6a0-0002a5d5c51b")
        val CCCD: UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")
        const val MTU = 247
    }

    private val adapter: BluetoothAdapter =
        (context.getSystemService(Context.BLUETOOTH_SERVICE) as BluetoothManager).adapter
    private var gatt: BluetoothGatt? = null
    private var writeChar: BluetoothGattCharacteristic? = null

    // one outstanding GATT op at a time (Android drops concurrent ops)
    private var pendingOp: CompletableDeferred<Int>? = null
    private var connected = CompletableDeferred<Unit>()
    private val writeQueue = Channel<ByteArray>(Channel.UNLIMITED)
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    private val callback = object : BluetoothGattCallback() {
        override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) {
            Log.i(TAG, "conn state=$newState status=$status")
            if (newState == BluetoothProfile.STATE_CONNECTED && status == BluetoothGatt.GATT_SUCCESS) {
                connected.complete(Unit)
            } else if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                val reason = "gatt disconnected (status=$status)"
                connected.completeExceptionally(IllegalStateException(reason))
                pendingOp?.completeExceptionally(IllegalStateException(reason))
                onDisconnected(reason)
            }
        }
        override fun onMtuChanged(g: BluetoothGatt, mtu: Int, status: Int) {
            Log.i(TAG, "mtu=$mtu status=$status"); pendingOp?.complete(status)
        }
        override fun onServicesDiscovered(g: BluetoothGatt, status: Int) { pendingOp?.complete(status) }
        override fun onDescriptorWrite(g: BluetoothGatt, d: BluetoothGattDescriptor, status: Int) {
            pendingOp?.complete(status)
        }
        override fun onCharacteristicWrite(g: BluetoothGatt, c: BluetoothGattCharacteristic, status: Int) {
            pendingOp?.complete(status)
        }
        // API 33+
        override fun onCharacteristicChanged(g: BluetoothGatt, c: BluetoothGattCharacteristic, value: ByteArray) {
            onFrame(value)
        }
        // API ≤ 32
        @Deprecated("pre-33 callback")
        override fun onCharacteristicChanged(g: BluetoothGatt, c: BluetoothGattCharacteristic) {
            @Suppress("DEPRECATION")
            if (Build.VERSION.SDK_INT < 33) c.value?.let(onFrame)
        }
    }

    val device: BluetoothDevice get() = adapter.getRemoteDevice(address)

    /** Bond (if needed), connect, MTU, discover, subscribe. Throws on failure/timeout. */
    suspend fun connect(timeoutMs: Long = 30_000) {
        withTimeout(timeoutMs) {
            ensureBonded()
            connected = CompletableDeferred()
            gatt = device.connectGatt(context, false, callback, BluetoothDevice.TRANSPORT_LE)
            connected.await()
            val g = gatt!!
            g.requestConnectionPriority(BluetoothGatt.CONNECTION_PRIORITY_HIGH)
            op { g.requestMtu(MTU) }
            check(op { g.discoverServices() } == BluetoothGatt.GATT_SUCCESS) { "service discovery failed" }
            val svc = g.getService(SERVICE) ?: error("Oura service 98ed0001 not found")
            writeChar = svc.getCharacteristic(WRITE) ?: error("write char 98ed0002 not found")
            for (c in svc.characteristics) {
                val notify = c.properties and BluetoothGattCharacteristic.PROPERTY_NOTIFY != 0
                val indicate = c.properties and BluetoothGattCharacteristic.PROPERTY_INDICATE != 0
                if (!notify && !indicate) continue
                g.setCharacteristicNotification(c, true)
                val d = c.getDescriptor(CCCD) ?: continue
                val v = if (notify) BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
                        else BluetoothGattDescriptor.ENABLE_INDICATION_VALUE
                val st = op { writeDescriptor(g, d, v) }
                Log.i(TAG, "subscribed ${c.uuid} status=$st")
            }
        }
        scope.launch { pumpWrites() }
    }

    /** BleWriter: called from Rust (any thread). Queued; written in order. */
    override fun write(data: ByteArray) {
        writeQueue.trySend(data)
    }

    private suspend fun pumpWrites() {
        for (frame in writeQueue) {
            val g = gatt ?: break
            val c = writeChar ?: break
            try {
                val st = op { writeCharacteristic(g, c, frame) }
                if (st != BluetoothGatt.GATT_SUCCESS) Log.w(TAG, "write status=$st")
            } catch (e: Exception) {
                Log.w(TAG, "write failed: ${e.message}"); break
            }
        }
    }

    fun close() {
        scope.cancel()
        writeQueue.close()
        gatt?.let { it.disconnect(); it.close() }
        gatt = null
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    private suspend fun op(start: () -> Boolean): Int {
        val d = CompletableDeferred<Int>()
        pendingOp = d
        check(start()) { "GATT op rejected" }
        return withTimeout(10_000) { d.await() }
    }

    private fun writeDescriptor(g: BluetoothGatt, d: BluetoothGattDescriptor, v: ByteArray): Boolean =
        if (Build.VERSION.SDK_INT >= 33) g.writeDescriptor(d, v) == BluetoothStatusCodes.SUCCESS
        else @Suppress("DEPRECATION") run { d.value = v; g.writeDescriptor(d) }

    private fun writeCharacteristic(g: BluetoothGatt, c: BluetoothGattCharacteristic, v: ByteArray): Boolean =
        if (Build.VERSION.SDK_INT >= 33)
            g.writeCharacteristic(c, v, BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT) == BluetoothStatusCodes.SUCCESS
        else @Suppress("DEPRECATION") run {
            c.writeType = BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
            c.value = v
            g.writeCharacteristic(c)
        }

    private suspend fun ensureBonded() {
        val dev = device
        if (dev.bondState == BluetoothDevice.BOND_BONDED) return
        val done = CompletableDeferred<Int>()
        val rx = object : BroadcastReceiver() {
            override fun onReceive(ctx: Context, i: Intent) {
                @Suppress("DEPRECATION")
                val d: BluetoothDevice? = i.getParcelableExtra(BluetoothDevice.EXTRA_DEVICE)
                if (d?.address != address) return
                val s = i.getIntExtra(BluetoothDevice.EXTRA_BOND_STATE, BluetoothDevice.ERROR)
                if (s == BluetoothDevice.BOND_BONDED || s == BluetoothDevice.BOND_NONE) done.complete(s)
            }
        }
        ContextCompat.registerReceiver(
            context, rx, IntentFilter(BluetoothDevice.ACTION_BOND_STATE_CHANGED),
            ContextCompat.RECEIVER_EXPORTED,
        )
        try {
            check(dev.createBond()) { "createBond refused" }
            val s = withTimeout(30_000) { done.await() }
            check(s == BluetoothDevice.BOND_BONDED) { "bonding failed — ring not in post-reset window?" }
        } finally {
            context.unregisterReceiver(rx)
        }
    }
}
