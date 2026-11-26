//! Host-side tests: a scripted fake ring behind the real `BleWriter` /
//! `push_frame` bridge, so the full Kotlin-facing path (session → oura-link →
//! oura-store → export) runs without hardware. Frames are synthetic, taken from
//! open_oura's own link tests (auth vector, extended drain) plus hand-built
//! serial/battery/set-key replies.

use super::*;
use std::collections::HashMap;
use std::sync::{OnceLock, Weak};

const TEST_KEY: &str = "4431967d8bacc2659743142b68391d9a";
const SERIAL: &str = "0000000000000001"; // synthetic test serial

/// Fake ring: request hex → canned notification frames, delivered back through
/// `RingSession::push_frame` exactly as the Kotlin GATT callback would.
struct FakeRing {
    script: Arc<Mutex<HashMap<String, Vec<String>>>>,
    writes: Arc<Mutex<Vec<String>>>,
    session: Arc<OnceLock<Weak<RingSession>>>,
}

impl BleWriter for FakeRing {
    fn write(&self, data: Vec<u8>) {
        let h = hex::encode(&data);
        self.writes.lock().unwrap().push(h.clone());
        let replies = self.script.lock().unwrap().get(&h).cloned().unwrap_or_default();
        if let Some(s) = self.session.get().and_then(Weak::upgrade) {
            for r in replies {
                s.push_frame(hex::decode(r).unwrap());
            }
        }
    }
}

struct Harness {
    session: Arc<RingSession>,
    script: Arc<Mutex<HashMap<String, Vec<String>>>>,
    writes: Arc<Mutex<Vec<String>>>,
}

impl Harness {
    fn new() -> Self {
        let script = Arc::new(Mutex::new(HashMap::new()));
        let writes = Arc::new(Mutex::new(Vec::new()));
        let slot = Arc::new(OnceLock::new());
        let ring = FakeRing { script: script.clone(), writes: writes.clone(), session: slot.clone() };
        let session = RingSession::with_quiet(Box::new(ring), Some(Duration::from_millis(20)));
        slot.set(Arc::downgrade(&session)).ok().unwrap();
        Self { session, script, writes }
    }
    fn on(&self, req: &str, replies: &[&str]) {
        self.script
            .lock()
            .unwrap()
            .insert(req.to_string(), replies.iter().map(|s| s.to_string()).collect());
    }
    fn wrote(&self, req: &str) -> bool {
        self.writes.lock().unwrap().iter().any(|w| w == req)
    }
}

fn serial_reply(serial: &str) -> String {
    format!("19{:02x}00{}", serial.len() + 1, hex::encode(serial))
}

fn script_auth_and_info(h: &Harness) {
    // auth vector from open_oura crates/oura-link client.rs tests
    h.on("2f012b", &["2f102c0e2d6a0a08c99b4365f458e6e97382"]);
    h.on("2f112da38a8772d3acb6db5c2b516dd56987c8", &["2f022e00"]);
    h.on("1803080010", &[&serial_reply(SERIAL)]);
    h.on("0803000000", &["091202000003040301000105000cffeeddccbbaa"]);
}

struct Progress(Arc<Mutex<Vec<String>>>);
impl SyncProgressListener for Progress {
    fn on_progress(&self, stage: String, _: u64, _: u32) {
        self.0.lock().unwrap().push(stage);
    }
}

struct Silent;
impl BleWriter for Silent {
    fn write(&self, _: Vec<u8>) {}
}

#[test]
fn keys_round_trip_and_validate() {
    let k = random_key().unwrap();
    let h = to_hex(&k);
    assert_eq!(h.len(), 32);
    assert_eq!(parse_key(&h), Some(k));
    assert!(is_valid_key_hex(TEST_KEY.into()));
    assert!(!is_valid_key_hex("abc".into()));
    assert!(!is_valid_key_hex("zz31967d8bacc2659743142b68391d9a".into()));
    assert_ne!(random_key().unwrap(), random_key().unwrap());
}

#[tokio::test]
async fn pair_installs_existing_key_and_verifies() {
    let h = Harness::new();
    script_auth_and_info(&h);
    h.on(&format!("2410{TEST_KEY}"), &["250100"]);
    let rep = h.session.pair(Some(TEST_KEY.into())).await.unwrap();
    assert_eq!(rep.serial, SERIAL);
    assert_eq!(rep.key_hex, TEST_KEY);
    assert!(!rep.minted);
    assert!(h.wrote(&format!("2410{TEST_KEY}")));
}

#[tokio::test]
async fn pair_refuses_on_a_ring_that_already_has_a_key() {
    let h = Harness::new();
    script_auth_and_info(&h);
    h.on(&format!("2410{TEST_KEY}"), &["250101"]);
    let err = h.session.pair(Some(TEST_KEY.into())).await.unwrap_err();
    assert!(err.to_string().contains("factory-reset"), "{err}");
}

#[tokio::test]
async fn pair_fails_before_installing_when_link_is_dead() {
    let h = Harness::new(); // nothing scripted: the ring never answers
    let err = h.session.pair(None).await.unwrap_err();
    assert!(err.to_string().contains("serial"), "{err}");
    assert!(!h.writes.lock().unwrap().iter().any(|w| w.starts_with("2410")));
}

#[tokio::test]
async fn battery_after_auth() {
    let h = Harness::new();
    script_auth_and_info(&h);
    h.on("0c00", &["0d035a0001"]);
    let b = h.session.battery(TEST_KEY.into()).await.unwrap();
    assert_eq!(b.percent, 90);
    assert_eq!(b.charging_recommended, 1);
}

#[tokio::test]
async fn full_sync_drains_into_store_and_exports_for_upload() {
    let dir = tempfile::tempdir().unwrap();
    let db = dir.path().join("ring.db").to_string_lossy().into_owned();
    let h = Harness::new();
    script_auth_and_info(&h);
    h.on("0c00", &["0d03500000"]);
    // extended drain from open_oura's extended_drain_does_not_send_legacy_ack
    h.on("280100", &["290100"]);
    h.on("2f0c410000000000000000000010", &["2f09430600aa430364bbcc2f0a42010000000000000000"]);
    h.on("2f0c4100c8000000000000000010", &["2f0a42000000000000000000"]);

    let stages = Arc::new(Mutex::new(Vec::new()));
    let rep = h
        .session
        .sync(db.clone(), TEST_KEY.into(), Box::new(Progress(stages.clone())))
        .await
        .unwrap();
    assert_eq!(rep.serial, SERIAL);
    assert_eq!(rep.events_synced, 1);
    assert_eq!(rep.next_cursor, 2);
    assert_eq!(rep.battery_percent, Some(80));
    // 1 ring event + 1 synthetic phone time anchor
    assert_eq!(rep.inserted, 1);
    let s = stages.lock().unwrap().clone();
    for st in ["auth", "setup", "time", "sync"] {
        assert!(s.iter().any(|x| x == st), "missing stage {st}: {s:?}");
    }

    let st = local_status(db.clone()).unwrap();
    assert_eq!(st.serial.as_deref(), Some(SERIAL));
    assert_eq!(st.firmware.as_deref(), Some("3.4.3"));
    assert_eq!(st.ring_cursor, 2);
    assert_eq!(st.total_events, 2);
    assert_eq!(st.last_battery_percent, Some(80.0));
    assert!(st.last_sync_unix > 0);

    let page = export_since(db.clone(), SERIAL.into(), 0, 1).unwrap();
    assert_eq!(page.rows.len(), 1);
    assert!(page.has_more);
    let page2 = export_since(db.clone(), SERIAL.into(), page.last_id, 100).unwrap();
    assert_eq!(page2.rows.len(), 1);
    assert!(!page2.has_more);
    assert_eq!(page2.rows[0].tag, 0x42); // phone anchor
    let empty = export_since(db.clone(), SERIAL.into(), page2.last_id, 100).unwrap();
    assert!(empty.rows.is_empty());
    assert_eq!(empty.last_id, page2.last_id);
    // other serials are never mixed in
    assert!(export_since(db, "other".into(), 0, 100).unwrap().rows.is_empty());
}

#[tokio::test]
async fn sync_with_wrong_key_fails_at_auth_and_writes_nothing() {
    let dir = tempfile::tempdir().unwrap();
    let db = dir.path().join("ring.db").to_string_lossy().into_owned();
    let h = Harness::new();
    h.on("2f012b", &["2f102c0e2d6a0a08c99b4365f458e6e97382"]);
    // any authenticate attempt is rejected (state 0x01 = AuthenticationError)
    let other = "00112233445566778899aabbccddeeff";
    let err = h
        .session
        .sync(db.clone(), other.into(), Box::new(Progress(Default::default())))
        .await
        .unwrap_err();
    assert!(matches!(err, CoreError::Failed(_)), "{err}");
    assert!(!std::path::Path::new(&db).exists());
    assert_eq!(local_status(db).unwrap().total_events, 0);
}

#[tokio::test]
async fn cancel_interrupts_a_waiting_sync() {
    let session = RingSession::new(Box::new(Silent));
    let running = session.clone();
    let task = tokio::spawn(async move {
        running.sync("unused.db".into(), TEST_KEY.into(), Box::new(Progress(Default::default()))).await
    });
    tokio::task::yield_now().await;
    session.cancel("gatt disconnected".into());
    let r = tokio::time::timeout(Duration::from_secs(1), task).await.unwrap().unwrap();
    assert!(matches!(r, Err(CoreError::Interrupted { reason }) if reason == "gatt disconnected"));
    // sticky: a later call on the same session fails fast
    assert!(matches!(session.pair(None).await, Err(CoreError::Interrupted { .. })));
}

#[test]
fn upload_payload_matches_ingest_contract() {
    let rows = vec![RawEventRow {
        id: 7,
        serial: SERIAL.into(),
        tag: 0x43,
        name: "x".into(),
        ring_timestamp: 100000,
        body: vec![0xaa, 0x01],
        captured_unix: 1,
    }];
    let v: serde_json::Value =
        serde_json::from_str(&upload_payload_json(SERIAL.into(), rows.clone(), Some(100001))).unwrap();
    assert_eq!(v["serial"], SERIAL);
    assert_eq!(v["cursor"], 100001);
    assert_eq!(v["rows"][0], json!({"tag": 67, "ring_timestamp": 100000, "body_hex": "aa01"}));
    // only the fields server/ingest.py accepts (it rejects unknown keys)
    let keys: Vec<_> = v.as_object().unwrap().keys().cloned().collect();
    assert_eq!(keys.len(), 3);
    assert_eq!(v["rows"][0].as_object().unwrap().len(), 3);
    let v2: serde_json::Value = serde_json::from_str(&upload_payload_json(SERIAL.into(), rows, None)).unwrap();
    assert!(v2["cursor"].is_null());
}

#[test]
fn phone_anchor_requires_new_ring_time() {
    assert!(phone_anchor_event(0, 500, 1_789_056_420).is_none());
    assert!(phone_anchor_event(3, 0, 1_789_056_420).is_none());
    let a = phone_anchor_event(3, 705_001, 1_789_056_420).unwrap();
    assert_eq!((a.tag, a.timestamp), (0x42, 705_000));
}

/// Optional: run export against a real synced DB (e.g. one you synced with the open_oura CLI):
/// `RINGKEEPER_TEST_DB=/path/to/your/ring.db cargo test real_db -- --nocapture`. Read-only.
#[test]
fn real_db_export_if_available() {
    let Ok(db) = std::env::var("RINGKEEPER_TEST_DB") else { return };
    let st = local_status(db.clone()).unwrap();
    let serial = st.serial.clone().expect("serial in real db");
    let mut after = 0;
    let mut n = 0usize;
    loop {
        let b = export_since(db.clone(), serial.clone(), after, 5000).unwrap();
        for r in &b.rows {
            assert!(r.body.len() <= 255);
        }
        n += b.rows.len();
        after = b.last_id;
        if !b.has_more {
            break;
        }
    }
    eprintln!("real db: serial={serial} events={} exported={n} cursor={}", st.total_events, st.ring_cursor);
    assert_eq!(n as u64, st.total_events);
}

#[test]
fn export_pages_are_capped_at_max_upload_batch() {
    assert_eq!(max_upload_batch(), 2_000);
    let rows = vec![];
    let v: serde_json::Value = serde_json::from_str(&upload_payload_json(SERIAL.into(), rows, Some(-1))).unwrap();
    assert!(v["cursor"].is_null());
}

/// End-to-end against a live server/ingest.py (driven by server/e2e_upload_test.py,
/// which starts the server on a temp port with a temp token + temp DB and a temp copy
/// of data/oura.db). Pages exactly like the Kotlin `Uploader`: `export_since` (≤ 2 000)
/// → `upload_payload_json` (cursor only on the last page) → POST. Skipped unless
/// RINGKEEPER_E2E_URL, RINGKEEPER_E2E_TOKEN and RINGKEEPER_TEST_DB are set.
#[test]
fn e2e_upload_to_ingest_if_configured() {
    let (Ok(url), Ok(token), Ok(db)) = (
        std::env::var("RINGKEEPER_E2E_URL"),
        std::env::var("RINGKEEPER_E2E_TOKEN"),
        std::env::var("RINGKEEPER_TEST_DB"),
    ) else {
        return;
    };
    let st = local_status(db.clone()).unwrap();
    let serial = st.serial.clone().expect("serial in test db");
    let (mut after, mut pages, mut sent, mut inserted, mut dups) = (0i64, 0usize, 0u64, 0u64, 0u64);
    loop {
        let b = export_since(db.clone(), serial.clone(), after, 10_000).unwrap();
        if b.rows.is_empty() {
            break;
        }
        assert!(b.rows.len() <= MAX_UPLOAD_BATCH as usize, "page over cap");
        let cursor = if b.has_more { None } else { Some(st.ring_cursor) };
        let body = upload_payload_json(serial.clone(), b.rows.clone(), cursor);
        let v: serde_json::Value = serde_json::from_str(&body).unwrap();
        assert_eq!(v["cursor"].is_null(), b.has_more, "cursor only on the last page");
        let (code, resp) = http_post(&url, &token, &body);
        assert_eq!(code, 200, "POST page {pages}: {resp}");
        let r: serde_json::Value = serde_json::from_str(&resp).unwrap();
        assert_eq!(r["received"].as_u64().unwrap(), b.rows.len() as u64);
        sent += b.rows.len() as u64;
        inserted += r["inserted"].as_u64().unwrap();
        dups += r["duplicates"].as_u64().unwrap();
        pages += 1;
        after = b.last_id; // advance only after the 2xx, like Uploader.kt
        if !b.has_more {
            assert_eq!(r["cursor"].as_i64(), Some(st.ring_cursor));
            break;
        }
    }
    // machine-readable line for the Python harness
    println!(
        "E2E_RESULT {}",
        json!({"serial": serial, "pages": pages, "sent": sent, "inserted": inserted,
               "duplicates": dups, "ring_cursor": st.ring_cursor, "total_events": st.total_events})
    );
    assert_eq!(sent, st.total_events);
}

/// Minimal HTTP/1.1 POST for the loopback test server (no extra deps).
fn http_post(url: &str, token: &str, body: &str) -> (u16, String) {
    use std::io::{Read, Write};
    let rest = url.strip_prefix("http://").expect("e2e uses http://127.0.0.1");
    let (hostport, path) = rest.split_once('/').map(|(h, p)| (h, format!("/{p}"))).unwrap();
    let mut s = std::net::TcpStream::connect(hostport).unwrap();
    s.set_read_timeout(Some(Duration::from_secs(60))).unwrap();
    write!(
        s,
        "POST {path} HTTP/1.1\r\nHost: {hostport}\r\nAuthorization: Bearer {token}\r\n\
         Content-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    )
    .unwrap();
    let mut out = String::new();
    s.read_to_string(&mut out).unwrap();
    let code = out.split(' ').nth(1).and_then(|c| c.parse().ok()).unwrap_or(0);
    let resp = out.split_once("\r\n\r\n").map(|(_, b)| b.to_string()).unwrap_or_default();
    (code, resp)
}
