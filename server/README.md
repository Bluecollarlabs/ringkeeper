# server/ — ingest service

See `docs/ARCHITECTURE.md` (component 4). Single-file Python (stdlib only, no venv
needed) HTTP service. It receives raw ring event rows from the Android app and stores
them idempotently in `server/data/ingest.db`, which uses the open_oura `oura-store`
schema (copied verbatim from `crates/oura-store/src/storage.rs` @ e57c43c). It is a
**separate** db from `data/oura.db`.

- Bind: `127.0.0.1:8092` only. The service refuses non-loopback binds unless you pass
  `--allow-non-loopback`.
- Rows are inserted with `INSERT OR IGNORE`, so `UNIQUE(serial, tag, ring_timestamp, body)`
  makes re-uploads harmless. `name` comes from `event_names.py`, a mirror of
  `oura-protocol::events::event_name`. `decoded_json` is left NULL; open_oura's
  `redecode` (or the later analysis step) can fill it in.
- `cursor` is upserted into `sync_state.next_cursor` per serial. It only moves forward.
- Logs are JSON lines on stderr (journald), one per request: method, path, status, ms,
  device, serial, rows, inserted, cursor. Logs never include bodies or tokens.

## API

### `GET /v1/health`
No auth. Returns `{"ok":true,"service":"ringkeeper-ingest","version":"0.1.0","time_unix":…}`.

### `POST /v1/events`
Headers: `Authorization: Bearer <device token>` and `Content-Type: application/json`.
A `Content-Length` is required; chunked encoding is rejected.

```json
{"serial": "0000000000000001",
 "cursor": 100000,
 "rows": [{"tag": 96, "ring_timestamp": 12345, "body_hex": "aa01"}]}
```

| field | rule |
|---|---|
| `serial` | `[0-9A-Za-z_-]{1,64}`; must be in the token's `serials` list if that list is non-empty |
| `cursor` | int ≥ 0 or null/absent (the ring's next-read cursor after this batch) |
| `rows` | ≤ 10 000 per request; an empty list is OK (cursor-only update) |
| `tag` | int 0–255 (the event tag byte, e.g. 0x60 = 96) |
| `ring_timestamp` | int 0–2³²−1 (ring-relative timestamp exactly as oura-store stores it) |
| `body_hex` | even-length hex for the event payload, ≤ 255 bytes (the same bytes as `events.body`) |

Unknown fields cause a 400. The whole request is limited to 4 MiB.

Responses:
- `200 {"ok":true,"received":N,"inserted":K,"duplicates":N-K,"cursor":C}`. The batch is
  committed in one transaction.
- `400` invalid JSON or invalid field. `401` missing or bad token. `403` serial not allowed for
  the token. `411`/`413`/`415` framing, size, or content-type problems. `503` (+`Retry-After`) for a db error.

## Tokens

`server/tokens.json` is gitignored and must be `chmod 600`. If the permissions are looser,
the service ignores the file and rejects every request. The file stores only SHA-256
hashes. The service reloads it automatically when its mtime changes, so no restart is needed.

```sh
python3 -I server/ingest.py mint-token --name my-phone --serial <RING_SERIAL>
# prints the token once → put it in the phone app; only the hash is stored
```
To rotate a token, delete that device's entry from `tokens.json` and mint again.

## Run / install (not enabled)

```sh
mkdir -m 700 -p server/data           # must exist: unit uses ProtectSystem=strict
python3 -I server/ingest.py serve      # foreground, 127.0.0.1:8092
# as a user service:
ln -s ~/ringkeeper/server/ringkeeper-ingest.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now ringkeeper-ingest
journalctl --user -u ringkeeper-ingest -f
```

## Exposure over Tailscale

The dashboard uses `tailscale serve` on `:8443` (→127.0.0.1:8091). Give the ingest service
its own HTTPS port on the tailnet so the two don't collide:

```sh
tailscale serve --bg --https=8444 http://127.0.0.1:8092
# → https://<your-host>.<tailnet>.ts.net:8444/v1/health   (tailnet only, not Funnel)
# undo: tailscale serve --https=8444 off
```
Add a tailnet ACL/grant so that only the phone's node can reach `<your-host>:8444`.
Do **not** use `tailscale funnel`.

## Tests

```sh
python3 -I -m unittest discover -s server -v
python3 -I server/e2e_upload.py   # Android core payloads -> live ingest.py (temp port/token/DB)
```
The tests use a temporary read-only snapshot of `data/oura.db` (via `sqlite3 .backup` into a
tmpdir that is deleted afterwards). Without that db they fall back to synthetic rows.
Health data never ends up in the repo. The tests check: idempotent and overlapping
re-uploads, byte-exact bodies and tag names matching open_oura's own rows, an identical
`sqlite_master` schema, a monotonic cursor, auth/403, validation, the 413 size limit, and
token-file permission enforcement.

## Android uploader contract

1. After each ring sync, select local raw rows where `uploaded = 0`, ordered by id.
   Send them in batches of ≤ 2 000 rows (well under 4 MiB) to
   `POST https://<your-host>:8444/v1/events` with `Authorization: Bearer <token>`.
   Keep the token in a Keystore-wrapped EncryptedFile and never log it.
2. `rows[i] = {tag, ring_timestamp, body_hex}`. These must be the exact `tag`,
   `ring_timestamp`, and `body` values that oura-store writes on the phone, with the body
   lower-case hex. Only the last batch of a sync should carry `cursor` (the ring's
   `next_cursor`); earlier batches send `null`.
3. On a **2xx** response, mark those row ids uploaded. Retrying is always safe because the
   insert is idempotent and `duplicates` just goes up.
4. On `5xx`/network errors, retry with exponential backoff (WorkManager `BackoffPolicy.EXPONENTIAL`)
   and honour `Retry-After`. On `401`/`403`, stop and show "re-pair with server" in the UI
   without retrying. On `400`/`413`, log the error and halve the batch size. If a single row
   still fails, report it as a bug and don't loop forever.
5. Optionally call `GET /v1/health` before uploading to show the "server reachable" state.

```kotlin
val req = Request.Builder()
    .url("https://your-host.your-tailnet.ts.net:8444/v1/events")
    .header("Authorization", "Bearer $token")
    .post(json.toRequestBody("application/json".toMediaType()))
    .build()
```
