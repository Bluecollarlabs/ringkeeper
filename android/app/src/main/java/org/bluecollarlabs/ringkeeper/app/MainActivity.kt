package org.bluecollarlabs.ringkeeper.app

import android.Manifest
import android.bluetooth.BluetoothDevice
import android.bluetooth.le.ScanResult
import android.companion.CompanionDeviceManager
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.IntentSenderRequest
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.lifecycle.lifecycleScope
import org.bluecollarlabs.ringkeeper.app.ble.CompanionAssociation
import org.bluecollarlabs.ringkeeper.app.ble.RingGatt
import org.bluecollarlabs.ringkeeper.app.data.AppState
import org.bluecollarlabs.ringkeeper.app.data.SecretStore
import org.bluecollarlabs.ringkeeper.app.sync.SyncWorker
import org.bluecollarlabs.ringkeeper.core.LocalStatus
import org.bluecollarlabs.ringkeeper.core.RingSession
import org.bluecollarlabs.ringkeeper.core.localStatus
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.security.SecureRandom
import java.text.DateFormat
import java.util.Date

class MainActivity : ComponentActivity() {
    private lateinit var state: AppState
    private lateinit var secrets: SecretStore
    private val message = mutableStateOf("")

    private val permissions = registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) {}

    private val chooser = registerForActivityResult(ActivityResultContracts.StartIntentSenderForResult()) { res ->
        // pre-33: the chosen device comes back in the result intent
        val data = res.data ?: return@registerForActivityResult
        @Suppress("DEPRECATION")
        val addr = when (val d = data.getParcelableExtra<android.os.Parcelable>(CompanionDeviceManager.EXTRA_DEVICE)) {
            is ScanResult -> d.device.address
            is BluetoothDevice -> d.address
            else -> null
        }
        if (addr != null) { state.ringAddress = addr; message.value = "Associated $addr" }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        state = AppState(this)
        secrets = SecretStore(this)
        requestRuntimePermissions()
        setContent { MaterialTheme { Surface(Modifier.fillMaxSize()) { Screen() } } }
    }

    private fun requestRuntimePermissions() {
        val p = mutableListOf<String>()
        if (Build.VERSION.SDK_INT >= 31) p += listOf(Manifest.permission.BLUETOOTH_CONNECT, Manifest.permission.BLUETOOTH_SCAN)
        else p += Manifest.permission.ACCESS_FINE_LOCATION
        if (Build.VERSION.SDK_INT >= 33) p += Manifest.permission.POST_NOTIFICATIONS
        permissions.launch(p.toTypedArray())
    }

    private fun associate() {
        CompanionAssociation(this).request(
            onChooser = { chooser.launch(IntentSenderRequest.Builder(it).build()) },
            onAssociated = { addr -> addr?.let { state.ringAddress = it; message.value = "Associated $it" } },
            onError = { message.value = "Association failed: $it" },
        )
    }

    private fun requestBatteryExemption() {
        val pm = getSystemService(PowerManager::class.java)
        if (!pm.isIgnoringBatteryOptimizations(packageName)) {
            @Suppress("BatteryLife")
            startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName")))
        }
    }

    /**
     * Pair a FACTORY-RESET ring: mint the key here and persist it (Keystore-wrapped)
     * BEFORE installing it, so a failure mid-pair can never lose the only copy.
     * UNTESTED ON HARDWARE.
     */
    private fun pairFreshRing() {
        val addr = state.ringAddress ?: run { message.value = "Associate the ring first"; return }
        if (secrets.ringKeyHex != null) { message.value = "A key is already stored — refusing to overwrite"; return }
        val key = ByteArray(16).also { SecureRandom().nextBytes(it) }.joinToString("") { "%02x".format(it) }
        secrets.ringKeyHex = key
        lifecycleScope.launch {
            message.value = "Pairing…"
            var session: RingSession? = null
            val gatt = RingGatt(this@MainActivity, addr, onFrame = { session?.pushFrame(it) }, onDisconnected = { session?.cancel(it) })
            try {
                withContext(Dispatchers.IO) { gatt.connect() }
                val s = RingSession(gatt)
                session = s
                val r = s.pair(key)
                state.serial = r.serial
                message.value = "Paired ${r.serial}. Key stored on this phone."
            } catch (e: Exception) {
                message.value = "Pair failed (key kept for retry): ${e.message}"
            } finally {
                session?.close(); gatt.close()
            }
        }
    }

    @Composable
    private fun Screen() {
        var status by remember { mutableStateOf<LocalStatus?>(null) }
        var tokenInput by remember { mutableStateOf("") }
        var confirmPair by remember { mutableStateOf(false) }
        var tick by remember { mutableStateOf(0) }
        LaunchedEffect(tick) {
            while (true) {
                status = withContext(Dispatchers.IO) { runCatching { localStatus(state.dbPath) }.getOrNull() }
                delay(3000)
            }
        }
        val fmt = DateFormat.getDateTimeInstance(DateFormat.SHORT, DateFormat.SHORT)
        Column(
            Modifier.padding(20.dp).verticalScroll(rememberScrollState()),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            Text("RingKeeper", style = MaterialTheme.typography.headlineMedium)
            val s = status
            Text("Last sync: " + (s?.lastSyncUnix?.takeIf { it > 0 }?.let { fmt.format(Date(it * 1000)) } ?: "never"))
            Text("Battery: " + (s?.lastBatteryPercent?.let { "${it.toInt()}%" } ?: "?"))
            Text("Ring: ${s?.serial ?: "—"}  fw ${s?.firmware ?: "—"}")
            Text("Events stored: ${s?.totalEvents ?: 0}, uploaded up to id ${state.uploadedId}")
            Text("Sync: ${state.lastSyncResult ?: "—"}", style = MaterialTheme.typography.bodySmall)
            Text("Upload: ${state.lastUploadResult ?: "—"}", style = MaterialTheme.typography.bodySmall)
            Button(onClick = { SyncWorker.syncNow(this@MainActivity); message.value = "Sync queued"; tick++ }) {
                Text("Sync now")
            }
            if (message.value.isNotEmpty()) Text(message.value, color = MaterialTheme.colorScheme.primary)

            Text("Setup", style = MaterialTheme.typography.titleMedium)
            Text("Ring address: ${state.ringAddress ?: "not associated"}", style = MaterialTheme.typography.bodySmall)
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedButton(onClick = { associate() }) { Text("Associate ring") }
                OutlinedButton(onClick = { confirmPair = true }) { Text("Pair fresh ring") }
            }
            OutlinedButton(onClick = { requestBatteryExemption() }) { Text("Allow background (battery)") }
            OutlinedTextField(
                value = tokenInput, onValueChange = { tokenInput = it },
                label = { Text(if (secrets.ingestToken != null) "Ingest token (set)" else "Ingest token") },
                visualTransformation = PasswordVisualTransformation(), singleLine = true,
            )
            OutlinedButton(onClick = {
                if (tokenInput.isNotBlank()) { secrets.ingestToken = tokenInput.trim(); tokenInput = ""; message.value = "Token saved" }
            }) { Text("Save token") }
            var urlInput by remember { mutableStateOf(state.ingestUrl) }
            OutlinedTextField(
                value = urlInput, onValueChange = { urlInput = it },
                label = { Text("Ingest URL") }, singleLine = true,
            )
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedButton(onClick = {
                    val u = urlInput.trim()
                    if (u.startsWith("https://") && u.length > 8) {
                        state.ingestUrlOverride = if (u == BuildConfig.INGEST_URL) null else u
                        message.value = "Ingest URL saved"
                    } else message.value = "Ingest URL must start with https://"
                }) { Text("Save URL") }
                OutlinedButton(onClick = {
                    state.ingestUrlOverride = null; urlInput = BuildConfig.INGEST_URL; message.value = "Ingest URL reset"
                }) { Text("Default") }
            }
            Text("Uploads to ${state.ingestUrl}", style = MaterialTheme.typography.bodySmall)
        }
        if (confirmPair) {
            AlertDialog(
                onDismissRequest = { confirmPair = false },
                title = { Text("Pair a factory-reset ring?") },
                text = { Text("Only for a ring that was just factory-reset and never set up in the Oura app. A new key is created and stored only on this phone.") },
                confirmButton = { TextButton(onClick = { confirmPair = false; pairFreshRing() }) { Text("Pair") } },
                dismissButton = { TextButton(onClick = { confirmPair = false }) { Text("Cancel") } },
            )
        }
    }
}
