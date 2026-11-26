package org.bluecollarlabs.ringkeeper.app.sync

import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.content.pm.ServiceInfo
import android.os.Build
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.ForegroundInfo
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.OutOfQuotaPolicy
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import org.bluecollarlabs.ringkeeper.app.ble.RingGatt
import org.bluecollarlabs.ringkeeper.app.data.AppState
import org.bluecollarlabs.ringkeeper.app.data.SecretStore
import org.bluecollarlabs.ringkeeper.app.net.Uploader
import org.bluecollarlabs.ringkeeper.core.CoreException
import org.bluecollarlabs.ringkeeper.core.RingSession
import org.bluecollarlabs.ringkeeper.core.SyncProgressListener
import java.util.concurrent.TimeUnit

/**
 * One sync window: (foreground, type connectedDevice) connect by bonded address →
 * RingSession.sync into the local DB → disconnect → upload new rows.
 * Used by both the periodic job and "Sync now". UNTESTED ON HARDWARE.
 */
class SyncWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {

    override suspend fun getForegroundInfo(): ForegroundInfo = foregroundInfo("Syncing ring…")

    override suspend fun doWork(): Result {
        val state = AppState(applicationContext)
        val secrets = SecretStore(applicationContext)
        val address = state.ringAddress ?: return fail(state, "no ring associated")
        val key = secrets.ringKeyHex ?: return fail(state, "no ring key (pair first)")

        try { setForeground(foregroundInfo("Syncing ring…")) } catch (e: Exception) {
            // Background FGS start can be refused (Android 12+) without a CDM association.
            Log.w(TAG, "foreground refused: ${e.message}")
        }

        // ── BLE sync ──
        var session: RingSession? = null
        val gatt = RingGatt(applicationContext, address,
            onFrame = { session?.pushFrame(it) },
            onDisconnected = { session?.cancel(it) })
        try {
            gatt.connect()
            val s = RingSession(gatt)
            session = s
            val report = s.sync(state.dbPath, key, object : SyncProgressListener {
                override fun onProgress(stage: String, bytesLeft: ULong, eventsSynced: UInt) {
                    Log.d(TAG, "progress $stage left=$bytesLeft events=$eventsSynced")
                }
            })
            state.serial = report.serial
            state.lastSyncResult = "ok: ${report.eventsSynced} events, battery ${report.batteryPercent ?: "?"}%"
        } catch (e: CoreException) {
            state.lastSyncResult = "sync failed: ${e.message}"
            return if (runAttemptCount < 3) Result.retry() else Result.failure()
        } catch (e: Exception) {
            state.lastSyncResult = "ble failed: ${e.javaClass.simpleName}: ${e.message}"
            return if (runAttemptCount < 3) Result.retry() else Result.failure()
        } finally {
            session?.close()
            gatt.close()
        }

        // ── upload (independent of BLE; failure only retried next window) ──
        val token = secrets.ingestToken
        state.lastUploadResult = if (token == null) "skipped: no ingest token" else {
            val r = Uploader(state, token).uploadPending()
            r.error?.let { "upload error after ${r.rowsSent} rows: $it" } ?: "ok: ${r.rowsSent} rows"
        }
        return Result.success()
    }

    private fun fail(state: AppState, msg: String): Result {
        state.lastSyncResult = msg
        return Result.failure()
    }

    private fun foregroundInfo(text: String): ForegroundInfo {
        val nm = applicationContext.getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(CHANNEL, "Ring sync", NotificationManager.IMPORTANCE_LOW))
        val n = NotificationCompat.Builder(applicationContext, CHANNEL)
            .setSmallIcon(android.R.drawable.stat_sys_data_bluetooth)
            .setContentTitle("RingKeeper")
            .setContentText(text)
            .setOngoing(true)
            .build()
        return if (Build.VERSION.SDK_INT >= 29)
            ForegroundInfo(NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE)
        else ForegroundInfo(NOTIF_ID, n)
    }

    companion object {
        private const val TAG = "SyncWorker"
        private const val CHANNEL = "sync"
        private const val NOTIF_ID = 1001
        private const val PERIODIC = "ring-periodic-sync"
        private const val NOW = "ring-sync-now"

        /** Periodic sync every ~90 min (PLAN: 1–2 h). Keeps an existing schedule. */
        fun schedulePeriodic(ctx: Context) {
            val req = PeriodicWorkRequestBuilder<SyncWorker>(90, TimeUnit.MINUTES, 30, TimeUnit.MINUTES)
                .setConstraints(Constraints.Builder().setRequiresBatteryNotLow(true).build())
                .build()
            WorkManager.getInstance(ctx).enqueueUniquePeriodicWork(PERIODIC, ExistingPeriodicWorkPolicy.KEEP, req)
        }

        fun syncNow(ctx: Context) {
            val req = OneTimeWorkRequestBuilder<SyncWorker>()
                .setExpedited(OutOfQuotaPolicy.RUN_AS_NON_EXPEDITED_WORK_REQUEST)
                .build()
            WorkManager.getInstance(ctx).enqueueUniqueWork(NOW, ExistingWorkPolicy.KEEP, req)
        }
    }
}
