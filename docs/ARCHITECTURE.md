# Architecture

```
Ring ──BLE──▶ Android app (Kotlin + Rust core) ──HTTPS over Tailscale──▶ Your PC
              local SQLite buffer, sync cursor                           ingest service → SQLite
                                                                         analysis (our own code)
                                                                         dashboard + local AI note
```

PC mode (no phone) replaces the left half with the open_oura CLI syncing straight into
`data/oura.db` on the PC.

**Hard rules**

- No Oura cloud, no Oura account, nothing leaves your own devices.
- **Never** decrypt, run or redistribute Oura's proprietary models (the encrypted `.pt.enc`
  files in the official app). All metrics are computed by our own code. open_oura's MIT ports
  of the on-device heuristics are fine; Oura's neural models are not.
- Services bind loopback only; Tailscale `serve` (never `funnel`) publishes them on your
  private tailnet.
- Only derived numbers go to the (local) LLM, and only to a loopback URL.

## Upstream

| repo | pinned commit | license | how it's used |
| --- | --- | --- | --- |
| [Th0rgal/open_oura](https://github.com/Th0rgal/open_oura) | `e57c43ce5fe0cf9a646afa790bda3731be767507` | MIT | git submodule `upstream/open_oura`; `core/` depends on its crates by path; the `oura` CLI is used for PC-mode sync and offline `redecode` |
| [Th0rgal/open_health](https://github.com/Th0rgal/open_health) | — (reference only) | MIT | `core/` is modelled on its UniFFI `oura-core` crate; no code vendored |

Treat upstream as third-party code: read diffs before bumping the pin.

## Protocol background (summarised from open_oura docs)

- **Auth key.** 16 random bytes, installed with `SetAuthKey`; each connection authenticates
  with a nonce → AES-128-ECB challenge. The ring stores but never reveals the key. Whoever
  holds the key can authenticate — RingKeeper mints its own key and keeps it on your device
  (Android Keystore-encrypted on the phone, `secrets/ring.key` 600 on the PC).
- **BLE bond.** After provisioning, a central that isn't bonded is dropped (HCI 0x15). A new
  host must bond while the ring is still in its post-reset window. In practice: pair each ring
  with **one** host (phone *or* PC). Moving hosts = factory reset + fresh pair.
- **Reset / re-pair needs nothing from Oura's app or cloud.** Reset over BLE from an
  authenticated client (`oura factory-reset --yes`) or via the charger's hardware procedure;
  pairing afterwards is pure BLE. Setting the ring up in the Oura app again replaces the key.
- **Firmware updates are cloud-gated**, so once off the Oura app the ring firmware is frozen.
  That keeps the protocol stable — and means no bug-fix updates either.
- **Minimal sync recipe:** connect + bond → subscribe notify characteristics → authenticate →
  capability query (extended vs legacy event fetch) → time sync → flush → drain events from a
  persisted cursor in batches until nothing is left. After a key-only pairing, automatic HR
  and SpO2 measurements are **off** until enabled (`features --enable-hr --enable-spo2`).
- **Ring buffer depth while the phone is away** is not documented; sync at least daily, and
  `tools/health_check.py` warns after 24 h without data.

## Components

1. **Rust core (`core/`)** — thin UniFFI crate over open_oura `oura-protocol`, `oura-link`
   (`default-features = false`, so no btleplug: Kotlin supplies the BLE transport) and
   `oura-store`. Exports `RingSession { new(writer), push_frame, pair, sync }` and
   `export_since(cursor)` / `upload_payload_json` for uploading. Built for `arm64-v8a` with
   `cargo-ndk`; host tests drive a scripted fake ring.
2. **Android app (`android/`)** — Kotlin + Compose, minSdk 29:
   - `BluetoothGatt` transport: connect by bonded address (no scan), `createBond`, MTU 247,
     notifications on all `98ed000x` notify characteristics, serialized writes to `…0002`.
   - `CompanionDeviceManager` association (background BLE start, fewer background limits).
   - `WorkManager` periodic job (~90 min ± 30, battery-not-low) running as a
     `connectedDevice` foreground service during the sync window.
   - Ring key + ingest token in an AES-256-GCM store with a non-exportable AndroidKeyStore
     key; never logged, excluded from cloud backup.
   - Local SQLite is the buffer; rows are marked uploaded only after a 2xx.
3. **Transport** — Tailscale on the phone; the PC publishes the ingest port with
   `tailscale serve --https`. Per-device bearer tokens (only SHA-256 hashes stored on the
   server), optionally restricted to a ring serial; tailnet ACL so only the phone can reach it.
4. **Ingest (`server/ingest.py`)** — stdlib-only Python, loopback-only. `POST /v1/events`
   (raw `tag | ring_timestamp | body` rows + serial + ring cursor), idempotent
   `INSERT OR IGNORE` into an open_oura-schema SQLite. `tools/phone_db.py` snapshots it
   read-only and runs open_oura's offline `redecode` to build `data/phone.db`.
5. **Analysis (`tools/analysis/`)** — our own, stdlib-only: night segmentation, HR / HRV
   (RMSSD) / resting HR / skin-temperature deviation vs baseline, heuristic 4-stage sleep
   staging from HR/HRV/motion/temperature, 14/30-night robust baselines, readiness, daytime
   activity and estimated steps, cycle awareness. Transparent formulas, **not** Oura's scores.
6. **Dashboard (`ui-mockup/` + `tools/dash_api.py`)** — static PWA + a small loopback API
   (`/api/health`, `/api/ask`, `/api/export`, `/api/delete` with PIN and rate limits).
7. **Local AI (`tools/analysis/ai_note.py`)** — Ollama on loopback; compact JSON of last night
   + baselines → short note / Q&A, with output checks and a template fallback.
8. **Ops** — `tools/morning_sync.sh` (sync/decode → analysis → export → backup → note),
   `tools/backup.sh` (SQLite online backup, 14-day rotation), `tools/health_check.py`.

## Data locations (all gitignored)

| path | what |
| --- | --- |
| `secrets/` (700) | `ring.key` (PC mode), `<device>.token`, `dashboard_pin` |
| `data/oura.db` | PC-mode raw + decoded events (open_oura schema) |
| `server/data/ingest.db` | phone uploads (raw) — the only copy in phone mode; back it up |
| `data/phone.db` | derived from ingest.db by `tools/phone_db.py`; rebuilt each run |
| `data/*.json` | derived nightly/readiness/cycle/AI outputs |
| `ui-mockup/real-data.json` | dashboard export |
| `$RINGKEEPER_BACKUP_DIR` | daily snapshots (put on an encrypted volume) |

## Known risks

- **Android background BLE:** Doze/App Standby and OEM app-killers. Mitigated with
  CompanionDeviceManager, a foreground service during sync, battery-optimisation exemption
  and connect-by-bonded-address.
- **Battery** (ring and phone): every sync costs ring battery; live streaming stays off.
- **Hardware/firmware variance:** most testing so far on one Gen 5 (firmware 2.1.x).
  Undocumented characteristics exist on Gen 5. The ring doesn't emit Oura's hypnogram on
  Gen 4/5, so staging is our own (less accurate than Oura's).
- **Key/bond lockout:** losing the key or the bond = factory reset (erases unsynced data).
- **Health-data privacy:** encrypted disks and backups, tailnet ACLs, no third-party LLMs,
  export/delete for the wearer.
- **Upstream is a hobby reverse-engineering project:** pin, review, test before bumping.

## Linux note: BlueZ 5.87

BlueZ 5.87's `bluetoothd` crashes (SIGSEGV in `is_filter_match()`, upstream
[bluez#2282](https://github.com/bluez/bluez/issues/2282), fixed in 5.88 by PR #2283) when a
client sets a service-UUID discovery filter — which btleplug does for the open_oura CLI.
Update BlueZ, or apply `patches/0001-no-uuid-discovery-filter-bluez-2282.patch` (see
`patches/README.md`).
