# RingKeeper (Android)

Kotlin app for your own ring (Oura Ring Gen 3/4/5 via the open_oura protocol library):
BLE sync via the Rust core (`../core`, open_oura) → local SQLite buffer → upload raw rows to
the ingest service on your PC (`server/ingest.py` on 127.0.0.1:8092, exposed via
`tailscale serve` as `POST https://<your-host>:8444/v1/events`, bearer token).

**Status: early. Builds (`./gradlew assembleDebug`); phone + ring testing is limited.**
Toolchain + build steps: `../core/TOOLCHAIN.md`.

| file | what |
| --- | --- |
| `ble/RingGatt.kt` | BluetoothGatt transport = core `BleWriter`: bonded-address connect, createBond, MTU 247, subscribe all notify/indicate chars of `98ed0001`, serialized write-requests to `98ed0002`, frames → `RingSession.pushFrame` |
| `ble/CompanionAssociation.kt` | CompanionDeviceManager association (stub; presence observing is on the roadmap) |
| `sync/SyncWorker.kt` | WorkManager periodic (90 min ±30, battery-not-low) + "Sync now" expedited; runs as foreground (`connectedDevice`); sync → upload |
| `net/Uploader.kt` | pages `exportSince` (≤ 2 000 rows, core `maxUploadBatch`) → `uploadPayloadJson` (ring cursor only on the last page) → POST; cursor (local `events.id`) advances only after 2xx; 401/403 stop, 400/413 halve the page |
| `data/SecretStore.kt` | ring key + ingest token, AES-256-GCM with a non-exportable AndroidKeyStore key; only ciphertext in prefs; no cloud backup |
| `data/AppState.kt` | ring address, serial, upload cursor, last results; DB in `noBackupFilesDir/ring.db` |
| `MainActivity.kt` | Compose: last sync, battery, events/upload state, Sync now, Associate, Pair fresh ring (key minted + persisted **before** install), token entry, battery-opt exemption |

HTTPS only (no cleartext exceptions). The built-in default URL is a placeholder; set it at
build time with `./gradlew assembleDebug -PingestUrl=...` (or env `RINGKEEPER_INGEST_URL`) or at runtime in the
app's Setup section ("Ingest URL", must be `https://`).

End-to-end upload test (host, loopback only, temp copy of `data/oura.db` if you have one, temp token/DB):
`python3 -I server/e2e_upload.py` — runs ingest.py on a free port and core's
`e2e_upload_to_ingest_if_configured` Rust test against it; asserts every row lands
byte-exact, the server cursor equals the ring cursor, and a re-upload is all duplicates.

Install: `adb install -r app/build/outputs/apk/debug/app-debug.apk` — full walkthrough in
`../docs/PHONE-SETUP.md`.
