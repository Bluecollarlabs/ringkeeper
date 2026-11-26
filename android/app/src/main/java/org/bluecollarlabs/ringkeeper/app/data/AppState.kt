package org.bluecollarlabs.ringkeeper.app.data

import android.content.Context
import org.bluecollarlabs.ringkeeper.app.BuildConfig
import java.io.File

/** Non-secret app state (ring address, upload cursor, last results) + DB path. */
class AppState(context: Context) {
    private val prefs = context.getSharedPreferences("state", Context.MODE_PRIVATE)

    /** open_oura oura-store schema SQLite — the local raw-event buffer. */
    val dbPath: String = File(context.noBackupFilesDir, "ring.db").absolutePath

    var ringAddress: String?
        get() = prefs.getString("ring_address", null)
        set(v) = prefs.edit().putString("ring_address", v).apply()

    var serial: String?
        get() = prefs.getString("serial", null)
        set(v) = prefs.edit().putString("serial", v).apply()

    /** Highest local events.id acknowledged (2xx) by the ingest server. */
    var uploadedId: Long
        get() = prefs.getLong("uploaded_id", 0)
        set(v) { prefs.edit().putLong("uploaded_id", v).commit() }

    /** Runtime override of the ingest URL (Setup screen); null → BuildConfig.INGEST_URL. */
    var ingestUrlOverride: String?
        get() = prefs.getString("ingest_url", null)
        set(v) = prefs.edit().putString("ingest_url", v?.trim()?.takeIf { it.isNotEmpty() }).apply()

    val ingestUrl: String
        get() = ingestUrlOverride ?: BuildConfig.INGEST_URL

    var lastSyncResult: String?
        get() = prefs.getString("last_sync_result", null)
        set(v) = prefs.edit().putString("last_sync_result", v).apply()

    var lastUploadResult: String?
        get() = prefs.getString("last_upload_result", null)
        set(v) = prefs.edit().putString("last_upload_result", v).apply()
}
