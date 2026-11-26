package org.bluecollarlabs.ringkeeper.app.net

import android.util.Log
import org.bluecollarlabs.ringkeeper.app.data.AppState
import org.bluecollarlabs.ringkeeper.core.exportSince
import org.bluecollarlabs.ringkeeper.core.localStatus
import org.bluecollarlabs.ringkeeper.core.maxUploadBatch
import org.bluecollarlabs.ringkeeper.core.uploadPayloadJson
import java.net.HttpURLConnection
import java.net.URL

/**
 * Uploads new raw rows from the local DB to your PC's ingest service
 * (`POST /v1/events`, server/ingest.py; contract in server/README.md) over Tailscale HTTPS.
 *
 * - Pages of ≤ [maxUploadBatch] (2 000) rows via core `exportSince`; the body is core
 *   `uploadPayloadJson` (`{serial, cursor, rows:[{tag, ring_timestamp, body_hex}]}`),
 *   with the ring cursor only on the last page (`null` before).
 * - The upload cursor (local events.id) advances ONLY after a 2xx; the server inserts
 *   idempotently (INSERT OR IGNORE), so re-sending a page is harmless.
 * - 401/403 → stop ("re-pair with server"); 400/413 → halve the page and retry; a
 *   single-row page that still fails is reported as a bug, never looped on.
 *   5xx/network errors → caller retries next window (Retry-After is reported).
 *
 * Verified end-to-end on the host against a live ingest.py (server/e2e_upload.py drives
 * the same core functions); not yet run from a phone.
 */
class Uploader(
    private val state: AppState,
    private val token: String,
    private val endpoint: String = state.ingestUrl,
    pageSize: UInt = maxUploadBatch(),
) {
    data class Result(val rowsSent: Int, val pages: Int, val error: String?, val authFailed: Boolean = false)

    private var page: UInt = pageSize.coerceIn(1u, maxUploadBatch())

    fun uploadPending(): Result {
        val local = localStatus(state.dbPath)
        val serial = state.serial ?: local.serial
            ?: return Result(0, 0, "no ring serial yet (sync first)")
        val ringCursor = local.ringCursor.takeIf { it >= 0 }
        var sent = 0
        var pages = 0
        while (true) {
            val batch = exportSince(state.dbPath, serial, state.uploadedId, page)
            if (batch.rows.isEmpty()) break
            val body = uploadPayloadJson(serial, batch.rows, if (batch.hasMore) null else ringCursor)
            val resp = try { post(body) } catch (e: Exception) {
                return Result(sent, pages, "network: ${e.javaClass.simpleName}: ${e.message}")
            }
            when (resp.code) {
                in 200..299 -> Unit
                401, 403 -> return Result(sent, pages, "HTTP ${resp.code}: re-pair with server (check token)", authFailed = true)
                400, 413 -> {
                    if (page <= 1u) return Result(sent, pages, "HTTP ${resp.code} on a single row (id ${batch.rows.first().id}) — bug, not retrying")
                    page = (page / 2u).coerceAtLeast(1u)
                    Log.w(TAG, "HTTP ${resp.code}; retrying with page size $page")
                    continue
                }
                else -> return Result(sent, pages, "HTTP ${resp.code}" + (resp.retryAfter?.let { ", retry after ${it}s" } ?: ""))
            }
            state.uploadedId = batch.lastId
            sent += batch.rows.size
            pages++
            if (!batch.hasMore) break
        }
        return Result(sent, pages, null)
    }

    private data class Resp(val code: Int, val retryAfter: String?)

    private fun post(json: String): Resp {
        val conn = URL(endpoint).openConnection() as HttpURLConnection
        try {
            conn.requestMethod = "POST"
            conn.connectTimeout = 15_000
            conn.readTimeout = 60_000
            conn.doOutput = true
            conn.setRequestProperty("Content-Type", "application/json")
            conn.setRequestProperty("Authorization", "Bearer $token")
            val bytes = json.toByteArray(Charsets.UTF_8)
            conn.setFixedLengthStreamingMode(bytes.size) // ingest.py requires Content-Length
            conn.outputStream.use { it.write(bytes) }
            val code = conn.responseCode
            Log.i(TAG, "POST ${bytes.size}B → $code") // never log the token or body
            return Resp(code, conn.getHeaderField("Retry-After"))
        } finally {
            conn.disconnect()
        }
    }

    private companion object { const val TAG = "Uploader" }
}
