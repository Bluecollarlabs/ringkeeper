//! `ringkeeper_core` — the UniFFI surface the ringkeeper Android app links against.
//!
//! Modelled on open_health `crates/oura-core` (MIT), slimmed to what the phone needs:
//! protocol + link (no btleplug) + store. No summary/model code, no Oura cloud.
//!
//! Flow on the phone:
//! 1. Kotlin connects/bonds via `BluetoothGatt`, enables notifications on the ring's
//!    `98ed000x` notify characteristics and creates a [`RingSession`] with a
//!    [`BleWriter`] that writes to `98ed0002`.
//! 2. Every notification is handed to [`RingSession::push_frame`].
//! 3. [`RingSession::pair`] (once, on a factory-reset ring) / [`RingSession::sync`]
//!    (every sync window) drive the shared `oura-link` client and write the local
//!    SQLite buffer (open_oura `oura-store` schema).
//! 4. [`export_since`] / [`upload_payload_json`] page new raw rows out for the
//!    uploader (`POST /v1/events` on your PC); Kotlin stores the returned `last_id`
//!    only after a 2xx.

use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use oura_link::transport::Transport;
use oura_link::OuraClient;
use oura_protocol::events::RingEvent;
use oura_store::storage::Store;
use serde_json::json;
use tokio::sync::{broadcast, watch};

uniffi::setup_scaffolding!();

/// Build/version string — trivial FFI round-trip check.
#[uniffi::export]
pub fn core_version() -> String {
    format!("ringkeeper_core {}", env!("CARGO_PKG_VERSION"))
}

// ── callback interfaces implemented in Kotlin ────────────────────────────────

/// Kotlin implements this to send one request frame to the ring (write to
/// characteristic `98ed0002`). Fire-and-forget: responses arrive via
/// [`RingSession::push_frame`].
#[uniffi::export(callback_interface)]
pub trait BleWriter: Send + Sync {
    fn write(&self, data: Vec<u8>);
}

/// Sync progress. `stage` is a short tag ("auth" / "setup" / "time" / "sync" /
/// "rebase"); during "sync", `bytes_left` is the ring's count of event bytes still to
/// transfer and `events_synced` the events pulled so far.
#[uniffi::export(callback_interface)]
pub trait SyncProgressListener: Send + Sync {
    fn on_progress(&self, stage: String, bytes_left: u64, events_synced: u32);
}

// ── records / errors ─────────────────────────────────────────────────────────

#[derive(Debug, Clone, uniffi::Record)]
pub struct SyncReport {
    pub serial: String,
    pub events_synced: u32,
    pub inserted: u32,
    pub next_cursor: u32,
    /// Battery read after the drain (None if the ring didn't answer).
    pub battery_percent: Option<u8>,
}

/// What [`RingSession::pair`] installed. `key_hex` is the ONLY copy of the ring's
/// auth key (the ring never reveals it) — persist it before anything else.
#[derive(Debug, Clone, uniffi::Record)]
pub struct PairReport {
    pub serial: String,
    pub key_hex: String,
    pub minted: bool,
}

#[derive(Debug, Clone, uniffi::Record)]
pub struct BatteryReport {
    pub percent: u8,
    pub charging_progress: u8,
    pub charging_recommended: u8,
}

/// One raw stored event row, as uploaded to the ingest server.
#[derive(Debug, Clone, PartialEq, uniffi::Record)]
pub struct RawEventRow {
    /// Local SQLite rowid — the upload cursor (monotonic).
    pub id: i64,
    pub serial: String,
    pub tag: u8,
    pub name: String,
    pub ring_timestamp: u32,
    pub body: Vec<u8>,
    pub captured_unix: i64,
}

#[derive(Debug, Clone, uniffi::Record)]
pub struct ExportBatch {
    pub rows: Vec<RawEventRow>,
    /// Highest `id` in `rows`, or the input `after_id` when empty. Persist this as the
    /// upload cursor only after the server acknowledged the batch.
    pub last_id: i64,
    /// More rows exist after `last_id` (call again).
    pub has_more: bool,
}

/// Local DB status for the UI.
#[derive(Debug, Clone, uniffi::Record)]
pub struct LocalStatus {
    pub serial: Option<String>,
    pub firmware: Option<String>,
    pub last_sync_unix: i64,
    pub ring_cursor: i64,
    pub total_events: u64,
    pub last_battery_percent: Option<f64>,
}

#[derive(Debug, thiserror::Error, uniffi::Error)]
pub enum CoreError {
    #[error("{0}")]
    Failed(String),
    #[error("storage {operation}: {detail} (sqlite={code}, retryable={retryable}, checkpoint={checkpoint})")]
    Storage {
        operation: String,
        code: i32,
        /// (not `message`: that collides with Throwable.message in Kotlin)
        detail: String,
        retryable: bool,
        checkpoint: u32,
    },
    #[error("interrupted: {reason}")]
    Interrupted { reason: String },
}

fn storage_failure(operation: &str, error: oura_store::error::Error, checkpoint: u32) -> CoreError {
    let code = match &error {
        oura_store::error::Error::Sqlite { code, .. } => *code,
        _ => 0,
    };
    CoreError::Storage {
        operation: operation.into(),
        code,
        detail: error.to_string(),
        retryable: code == 5 || code == 6, // SQLITE_BUSY / SQLITE_LOCKED
        checkpoint,
    }
}

fn sql_err(e: rusqlite::Error) -> CoreError {
    CoreError::Failed(format!("sqlite: {e}"))
}

// ── transport bridge ─────────────────────────────────────────────────────────

struct FfiTransport {
    tx: broadcast::Sender<Vec<u8>>,
    writer: Arc<dyn BleWriter>,
}

#[async_trait::async_trait]
impl Transport for FfiTransport {
    async fn write(&self, data: &[u8]) -> oura_link::Result<()> {
        self.writer.write(data.to_vec());
        Ok(())
    }
    fn subscribe(&self) -> broadcast::Receiver<Vec<u8>> {
        self.tx.subscribe()
    }
}

// ── session ──────────────────────────────────────────────────────────────────

/// A live session bound to one connected ring. Create per connection.
#[derive(uniffi::Object)]
pub struct RingSession {
    stop: watch::Sender<Option<String>>,
    tx: broadcast::Sender<Vec<u8>>,
    writer: Arc<dyn BleWriter>,
    /// Response quiet window; None = oura-link default (1.5 s). Tests shorten it.
    quiet: Option<Duration>,
}

#[uniffi::export(async_runtime = "tokio")]
impl RingSession {
    #[uniffi::constructor]
    pub fn new(writer: Box<dyn BleWriter>) -> Arc<Self> {
        Self::with_quiet(writer, None)
    }

    /// Abort any in-flight `pair`/`sync`/`battery` (e.g. GATT disconnected, service
    /// stopped). Sticky: create a new session for the next connection.
    pub fn cancel(&self, reason: String) {
        self.stop.send_replace(Some(reason));
    }

    /// Kotlin pushes every inbound notification (any `98ed000x` notify char) here.
    pub fn push_frame(&self, data: Vec<u8>) {
        let _ = self.tx.send(data);
    }

    /// Pair a **factory-reset** ring: install a 16-byte auth key and verify it.
    /// `existing_key_hex` re-installs a key we already hold; None mints a fresh one
    /// from the OS CSPRNG. Fails without changes on a ring that already has a key.
    pub async fn pair(&self, existing_key_hex: Option<String>) -> Result<PairReport, CoreError> {
        self.cancellable(self.pair_inner(existing_key_hex)).await
    }

    /// Authenticate and read the battery.
    pub async fn battery(&self, key_hex: String) -> Result<BatteryReport, CoreError> {
        self.cancellable(async {
            let key = parse_key(&key_hex).ok_or_else(bad_key)?;
            let client = self.client();
            client.authenticate(&key).await.map_err(fail)?;
            let b = client.battery().await.map_err(fail)?;
            Ok(BatteryReport {
                percent: b.percent,
                charging_progress: b.charging_progress,
                charging_recommended: b.charging_recommended,
            })
        })
        .await
    }

    /// Authenticate, set up the app stream, sync time, and drain history events into
    /// the SQLite DB at `db_path` from its persisted cursor. Checkpoints per batch, so
    /// a failed call can be retried (reconnect + call again) and resumes.
    pub async fn sync(
        &self,
        db_path: String,
        key_hex: String,
        progress: Box<dyn SyncProgressListener>,
    ) -> Result<SyncReport, CoreError> {
        self.cancellable(self.sync_inner(db_path, key_hex, progress)).await
    }
}

impl RingSession {
    fn with_quiet(writer: Box<dyn BleWriter>, quiet: Option<Duration>) -> Arc<Self> {
        let (tx, _) = broadcast::channel(8192);
        let (stop, _) = watch::channel(None);
        Arc::new(Self { stop, tx, writer: Arc::from(writer), quiet })
    }

    fn client(&self) -> OuraClient<FfiTransport> {
        let c = OuraClient::new(FfiTransport { tx: self.tx.clone(), writer: self.writer.clone() });
        match self.quiet {
            Some(q) => c.with_quiet(q),
            None => c,
        }
    }

    async fn cancellable<T>(
        &self,
        fut: impl std::future::Future<Output = Result<T, CoreError>>,
    ) -> Result<T, CoreError> {
        let mut stop = self.stop.subscribe();
        if let Some(reason) = stop.borrow().clone() {
            return Err(CoreError::Interrupted { reason });
        }
        tokio::select! {
            biased;
            _ = stop.changed() => Err(CoreError::Interrupted {
                reason: stop.borrow().clone().unwrap_or_else(|| "cancelled".into()) }),
            r = fut => r,
        }
    }

    async fn pair_inner(&self, existing_key_hex: Option<String>) -> Result<PairReport, CoreError> {
        let (key, minted) = match existing_key_hex.as_deref().map(str::trim).filter(|s| !s.is_empty()) {
            Some(hex) => (parse_key(hex).ok_or_else(bad_key)?, false),
            None => (random_key().map_err(CoreError::Failed)?, true),
        };
        let client = self.client();
        // Serial needs no auth: a dead link fails here, before any key is installed.
        let serial = client.serial().await.map_err(|e| fail_ctx("reading the ring serial", e))?;
        client
            .set_auth_key(&key)
            .await
            .map_err(|e| CoreError::Failed(format!("{e} — is the ring factory-reset?")))?;
        client.authenticate(&key).await.map_err(|e| {
            CoreError::Failed(format!("key installed on {serial} but verification failed: {e}"))
        })?;
        Ok(PairReport { serial, key_hex: to_hex(&key), minted })
    }

    async fn sync_inner(
        &self,
        db_path: String,
        key_hex: String,
        progress: Box<dyn SyncProgressListener>,
    ) -> Result<SyncReport, CoreError> {
        let key = parse_key(&key_hex).ok_or_else(bad_key)?;
        let client = self.client();

        progress.on_progress("auth".into(), 0, 0);
        client.authenticate(&key).await.map_err(fail)?;
        progress.on_progress("setup".into(), 0, 0);
        client.setup_app_stream().await.map_err(fail)?;
        // Give this boot a wall-clock anchor (time_sync event).
        progress.on_progress("time".into(), 0, 0);
        if client.sync_time_app().await.is_err() {
            let _ = client.sync_time().await;
        }
        // Let the ring post-process sleep while we drain (fire-and-forget).
        let _ = client.check_sleep_analysis(false).await;
        let serial = client.serial().await.unwrap_or_else(|_| "unknown".into());
        let info = client.firmware().await.ok();

        let store = Mutex::new(Store::open(&db_path).map_err(|e| storage_failure("open", e, 0))?);
        store
            .lock()
            .unwrap()
            .upsert_device(&serial, None, info.as_ref())
            .map_err(|e| storage_failure("upsert_device", e, 0))?;
        let cursor = store
            .lock()
            .unwrap()
            .cursor(&serial)
            .map_err(|e| storage_failure("read_cursor", e, 0))?;

        let inserted = AtomicU32::new(0);
        progress.on_progress("sync".into(), 0, 0);
        let mut outcome = match drain_into_store(&client, cursor, &serial, &store, &inserted, progress.as_ref()).await {
            Ok(o) => o,
            // Ring 5 rejects a stale cursor with ExtGetEvent result 0xff → rebase to 0
            // (inserts are de-duplicated).
            Err(e) if cursor > 0 && is_rejected_history_cursor(&e.to_string()) => {
                rebase(&store, &serial, cursor, progress.as_ref())?;
                drain_into_store(&client, 0, &serial, &store, &inserted, progress.as_ref()).await?
            }
            Err(e) => return Err(e),
        };
        if outcome.events_synced == 0 && cursor > 0 {
            // Empty incremental drain: verify the cursor is still valid (ring reboot).
            let marker = cursor_marker_present(&client, cursor).await.map_err(CoreError::Failed)?;
            if !marker {
                rebase(&store, &serial, cursor, progress.as_ref())?;
                outcome = drain_into_store(&client, 0, &serial, &store, &inserted, progress.as_ref()).await?;
            }
        }
        if let Some(anchor) = phone_anchor_event(outcome.events_synced, outcome.next_cursor, now_unix()) {
            let _ = store.lock().unwrap().insert_event(&serial, &anchor);
        }
        let battery_percent = match client.battery().await {
            Ok(b) => {
                let _ = store.lock().unwrap().insert_battery(&serial, &b);
                Some(b.percent)
            }
            Err(_) => None,
        };
        Ok(SyncReport {
            serial,
            events_synced: outcome.events_synced,
            inserted: inserted.into_inner(),
            next_cursor: outcome.next_cursor,
            battery_percent,
        })
    }
}

fn rebase(
    store: &Mutex<Store>,
    serial: &str,
    cursor: u32,
    progress: &dyn SyncProgressListener,
) -> Result<(), CoreError> {
    store
        .lock()
        .unwrap()
        .set_cursor(serial, 0)
        .map_err(|e| storage_failure("rebase_cursor", e, cursor))?;
    progress.on_progress("rebase".into(), 0, 0);
    Ok(())
}

/// Drain one range, committing each batch (+cursor) before the ring is acked. A DB
/// failure makes oura-link withhold the ack, so the cursor never passes unsaved data.
async fn drain_into_store<T: Transport>(
    client: &OuraClient<T>,
    cursor: u32,
    serial: &str,
    store: &Mutex<Store>,
    inserted: &AtomicU32,
    progress: &dyn SyncProgressListener,
) -> Result<oura_link::client::SyncOutcome, CoreError> {
    let batch = Mutex::new(Vec::new());
    let checkpoint = AtomicU32::new(cursor);
    let db_err: Mutex<Option<CoreError>> = Mutex::new(None);
    let outcome = client
        .drain_events(
            cursor,
            |ev| {
                batch.lock().unwrap().push(ev.clone());
                true
            },
            |p| {
                let mut events = batch.lock().unwrap();
                match store.lock().unwrap().commit_batch(serial, &events, p.next_cursor) {
                    Ok(n) => {
                        inserted.fetch_add(n, Ordering::Relaxed);
                        checkpoint.store(p.next_cursor, Ordering::Relaxed);
                        events.clear();
                        progress.on_progress("sync".into(), p.bytes_left as u64, p.events_synced);
                        true
                    }
                    Err(e) => {
                        *db_err.lock().unwrap() =
                            Some(storage_failure("commit_batch", e, checkpoint.load(Ordering::Relaxed)));
                        false
                    }
                }
            },
        )
        .await;
    if let Some(e) = db_err.lock().unwrap().take() {
        return Err(e);
    }
    outcome.map_err(fail)
}

async fn cursor_marker_present<T: Transport>(client: &OuraClient<T>, cursor: u32) -> Result<bool, String> {
    if cursor == 0 {
        return Ok(true);
    }
    let o = client
        .drain_events(cursor - 1, |_| true, |_| true)
        .await
        .map_err(|e| e.to_string())?;
    Ok(o.events_synced > 0 && o.next_cursor >= cursor)
}

fn is_rejected_history_cursor(error: &str) -> bool {
    error.contains("extended history request failed with result code 0xff")
}

/// Synthetic `time_sync` (0x42) row pairing the newest ring timestamp with the
/// phone clock (same convention as open_health oura-core: body = unix LE + "phone").
fn phone_anchor_event(events_synced: u32, next_cursor: u32, now: u64) -> Option<RingEvent> {
    if events_synced == 0 || next_cursor == 0 || now == 0 {
        return None;
    }
    let unix = u32::try_from(now).ok()?;
    let mut body = unix.to_le_bytes().to_vec();
    body.extend_from_slice(b"phone");
    Some(RingEvent {
        tag: 0x42,
        name: oura_protocol::events::event_name(0x42),
        timestamp: next_cursor - 1,
        body,
        decoded: Some(json!({ "unix_time": unix, "source": "phone" })),
    })
}

// ── local DB: status + export for upload ─────────────────────────────────────

/// Status of the local buffer DB for the UI. Missing DB → empty status.
#[uniffi::export]
pub fn local_status(db_path: String) -> Result<LocalStatus, CoreError> {
    if !std::path::Path::new(&db_path).exists() {
        return Ok(LocalStatus {
            serial: None,
            firmware: None,
            last_sync_unix: 0,
            ring_cursor: 0,
            total_events: 0,
            last_battery_percent: None,
        });
    }
    let store = Store::open_read_only(&db_path).map_err(|e| storage_failure("open", e, 0))?;
    let dev = store.device_info().map_err(|e| storage_failure("device_info", e, 0))?;
    let (total, _) = store.event_totals().map_err(|e| storage_failure("totals", e, 0))?;
    drop(store);
    let conn = open_ro(&db_path)?;
    let battery: Option<f64> = conn
        .query_row(
            "SELECT value FROM readings WHERE kind = 'battery_percent' ORDER BY id DESC LIMIT 1",
            [],
            |r| r.get(0),
        )
        .ok();
    Ok(match dev {
        Some((serial, _hw, fw, _api, _mac, _upd, last_sync, cursor)) => LocalStatus {
            serial: Some(serial),
            firmware: Some(fw).filter(|s| !s.is_empty()),
            last_sync_unix: last_sync,
            ring_cursor: cursor,
            total_events: total as u64,
            last_battery_percent: battery,
        },
        None => LocalStatus {
            serial: None,
            firmware: None,
            last_sync_unix: 0,
            ring_cursor: 0,
            total_events: total as u64,
            last_battery_percent: battery,
        },
    })
}

fn open_ro(db_path: &str) -> Result<rusqlite::Connection, CoreError> {
    let conn = rusqlite::Connection::open_with_flags(db_path, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)
        .map_err(sql_err)?;
    conn.busy_timeout(Duration::from_millis(5000)).map_err(sql_err)?;
    Ok(conn)
}

/// Upload page size cap (server/README.md "Android uploader contract": batches of
/// ≤ 2 000 rows, well under ingest.py's 10 000-row / 4 MiB request limits).
pub const MAX_UPLOAD_BATCH: u32 = 2_000;

/// [`MAX_UPLOAD_BATCH`] for Kotlin (UniFFI does not export constants).
#[uniffi::export]
pub fn max_upload_batch() -> u32 {
    MAX_UPLOAD_BATCH
}

/// Page raw event rows with local `id > after_id`, oldest first, for one ring
/// `serial` (rows of other serials are never mixed into one upload). `limit` is
/// clamped to 1..=[`MAX_UPLOAD_BATCH`].
#[uniffi::export]
pub fn export_since(db_path: String, serial: String, after_id: i64, limit: u32) -> Result<ExportBatch, CoreError> {
    let limit = limit.clamp(1, MAX_UPLOAD_BATCH) as i64;
    let conn = open_ro(&db_path)?;
    let mut stmt = conn
        .prepare(
            "SELECT id, serial, tag, name, ring_timestamp, body, captured_unix FROM events \
             WHERE serial = ?1 AND id > ?2 ORDER BY id LIMIT ?3",
        )
        .map_err(sql_err)?;
    let rows = stmt
        .query_map(rusqlite::params![serial, after_id, limit + 1], |r| {
            Ok(RawEventRow {
                id: r.get(0)?,
                serial: r.get(1)?,
                tag: r.get::<_, i64>(2)? as u8,
                name: r.get(3)?,
                ring_timestamp: r.get::<_, i64>(4)? as u32,
                body: r.get(5)?,
                captured_unix: r.get(6)?,
            })
        })
        .map_err(sql_err)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(sql_err)?;
    let mut rows = rows;
    let has_more = rows.len() as i64 > limit;
    rows.truncate(limit as usize);
    let last_id = rows.last().map(|r| r.id).unwrap_or(after_id);
    Ok(ExportBatch { rows, last_id, has_more })
}

/// The exact JSON body for `POST /v1/events` (server/ingest.py contract):
/// `{"serial", "cursor", "rows":[{"tag","ring_timestamp","body_hex"}]}`.
/// `ring_cursor` is sent only on the final page (`None` → `null`); a negative cursor
/// (never valid for the server, which requires ≥ 0) is sent as `null`.
#[uniffi::export]
pub fn upload_payload_json(serial: String, rows: Vec<RawEventRow>, ring_cursor: Option<i64>) -> String {
    let rows: Vec<_> = rows
        .iter()
        .map(|r| json!({ "tag": r.tag, "ring_timestamp": r.ring_timestamp, "body_hex": hex::encode(&r.body) }))
        .collect();
    let cursor = ring_cursor.filter(|c| *c >= 0);
    json!({ "serial": serial, "cursor": cursor, "rows": rows }).to_string()
}

/// Validate a stored key without touching the ring (UI / settings import).
#[uniffi::export]
pub fn is_valid_key_hex(key_hex: String) -> bool {
    parse_key(&key_hex).is_some()
}

// ── helpers ──────────────────────────────────────────────────────────────────

fn fail(e: oura_link::Error) -> CoreError {
    CoreError::Failed(e.to_string())
}
fn fail_ctx(ctx: &str, e: oura_link::Error) -> CoreError {
    CoreError::Failed(format!("{ctx}: {e}"))
}
fn bad_key() -> CoreError {
    CoreError::Failed("auth key must be 32 hex chars".into())
}

fn now_unix() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

fn random_key() -> Result<[u8; 16], String> {
    let mut key = [0u8; 16];
    getrandom::getrandom(&mut key).map_err(|e| format!("system CSPRNG unavailable: {e}"))?;
    Ok(key)
}

fn to_hex(key: &[u8; 16]) -> String {
    hex::encode(key)
}

fn parse_key(s: &str) -> Option<[u8; 16]> {
    let s = s.trim();
    if s.len() != 32 {
        return None;
    }
    hex::decode(s).ok()?.try_into().ok()
}

#[cfg(test)]
mod tests;
