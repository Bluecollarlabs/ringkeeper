package org.bluecollarlabs.ringkeeper.app.ble

import android.bluetooth.le.ScanFilter
import android.companion.AssociationInfo
import android.companion.AssociationRequest
import android.companion.BluetoothLeDeviceFilter
import android.companion.CompanionDeviceManager
import android.content.Context
import android.content.IntentSender
import android.os.Build
import android.os.ParcelUuid
import java.util.concurrent.Executor

/**
 * CompanionDeviceManager association with the ring (STUB — UNTESTED ON HARDWARE).
 *
 * Association gives: a system chooser instead of a raw scan, the ring's address, and
 * (with REQUEST_COMPANION_* permissions) background-start / FGS-from-background
 * exemptions that the periodic WorkManager sync relies on.
 *
 * TODO(roadmap): startObservingDevicePresence + CompanionDeviceService to sync when
 * the ring comes in range; handle re-association after a factory reset.
 */
class CompanionAssociation(private val context: Context) {
    private val cdm = context.getSystemService(CompanionDeviceManager::class.java)

    fun request(
        onChooser: (IntentSender) -> Unit,
        onAssociated: (address: String?) -> Unit,
        onError: (String) -> Unit,
    ) {
        val filter = BluetoothLeDeviceFilter.Builder()
            .setScanFilter(ScanFilter.Builder().setServiceUuid(ParcelUuid(RingGatt.SERVICE)).build())
            .build()
        val req = AssociationRequest.Builder()
            .addDeviceFilter(filter)
            .setSingleDevice(true)
            .build()
        val cb = object : CompanionDeviceManager.Callback() {
            override fun onAssociationPending(intentSender: IntentSender) = onChooser(intentSender)
            override fun onAssociationCreated(info: AssociationInfo) {
                onAssociated(if (Build.VERSION.SDK_INT >= 33) info.deviceMacAddress?.toString()?.uppercase() else null)
            }
            @Deprecated("pre-33")
            override fun onDeviceFound(intentSender: IntentSender) = onChooser(intentSender)
            override fun onFailure(error: CharSequence?) = onError(error?.toString() ?: "association failed")
        }
        if (Build.VERSION.SDK_INT >= 33) {
            cdm.associate(req, Executor { it.run() }, cb)
        } else {
            @Suppress("DEPRECATION")
            cdm.associate(req, cb, null)
        }
    }

    /** Existing associations' MAC addresses (all API levels). */
    @Suppress("DEPRECATION")
    fun associatedAddresses(): List<String> =
        if (Build.VERSION.SDK_INT >= 33) cdm.myAssociations.mapNotNull { it.deviceMacAddress?.toString()?.uppercase() }
        else cdm.associations.map { it.uppercase() }
}
