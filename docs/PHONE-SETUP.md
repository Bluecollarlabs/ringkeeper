# Phone setup (Android app + Tailscale)

Goal: your Android phone pairs with the ring, syncs over Bluetooth (BLE), and uploads the raw
events over Tailscale to the ingest service on your PC. In this setup the phone is the
**only** device paired with the ring.

Allow about 45 minutes. If you're setting this up for someone else (a partner, a parent, a
crew member), do it **together, with their consent** — it's their phone and their health data.

> Status: early. The app builds and the full path has been tested end-to-end on the host
> (`server/e2e_upload.py`), but phone + ring testing is limited. Do a dry run on a spare
> phone if you can. Known gap: the app doesn't yet switch on automatic HR/SpO2 measurement on
> the ring (§7).

Throughout this guide:

- `PC` = the computer that runs RingKeeper (ingest service, analysis, dashboard).
- `<host>` = your PC's Tailscale MagicDNS name, e.g. `mypc.tail1234.ts.net`
  (`RINGKEEPER_PUBLIC_HOST` in `ringkeeper.env`, see `config.example.env`).
- `<serial>` = your ring's serial number (the app shows it after pairing).

---

## P. Optional: you tested the ring on the PC first

If you paired the ring with the PC using the open_oura CLI (see `docs/GETTING-STARTED.md`),
move it over to the phone like this. After step P2 the PC can no longer reach the ring over
BLE; from then on data only arrives via the phone → ingest server.

**P1. Last PC sync.** A reset erases the ring's event buffer, so drain it first:
```sh
tools/morning_sync.sh                          # pc mode: `sync: ok`
```

**P2. Factory-reset the ring from the PC** (it still holds the PC key). Ring on its charger, close by:
```sh
upstream/open_oura/target/release/oura --name "" --key-file secrets/ring.key factory-reset --yes
```
This wipes the key, all BLE bonds and the on-ring buffer. Then `bluetoothctl devices` →
`bluetoothctl remove <ring MAC>` so BlueZ stops trying to reconnect.

**P3. Switch the PC side to phone mode** after the phone is uploading (§6):
```sh
printf '{"mode": "phone", "serial": "<serial>"}\n' > data/source.json
python3 -I tools/phone_db.py          # builds data/phone.db (decoded); first run can say "no uploads yet"
```
`tools/morning_sync.sh [--ai]` then skips BLE: it rebuilds `data/phone.db`, prints
`phone: last upload … (N h ago)` (with a WARNING if that's more than 24 h ago), and runs
analysis, export, backup and the AI note as before. PC history in `data/oura.db` isn't merged (§9).
To go back: `printf '{"mode": "pc"}\n' > data/source.json` (needs the ring re-paired to the PC).

## 0. Before you start (on the PC)

- [ ] **Ring factory-reset and NOT set up in the Oura app.** If the Oura app sees the reset ring
      and offers "set up new ring", **don't** let it — it installs its own key and locks
      RingKeeper out.
- [ ] Ingest service running: `systemctl --user is-active ringkeeper-ingest` → `active`;
      `curl -s http://127.0.0.1:8092/v1/health` → `{"ok":true,…}`.
- [ ] Tailscale serve for ingest: `tailscale serve status` shows
      `https://<host>:8444 → http://127.0.0.1:8092`.
      (If not: `tailscale serve --bg --https=8444 http://127.0.0.1:8092`. **Never** use `funnel` —
      that would put your health data on the public internet.)
- [ ] Debug APK built: `cd android && ./gradlew assembleDebug -PingestUrl=https://<host>:8444/v1/events`
      → `app/build/outputs/apk/debug/app-debug.apk` (toolchain notes: `core/TOOLCHAIN.md`).
- [ ] Ring charged (>50 %) and on its charger, close to the phone.
- [ ] Phone: Android 10+ (minSdk 29), charged, on Wi-Fi.
- [ ] If the **Oura app** is installed: remove the ring from it, or uninstall it.

## 1. Tailscale on the phone

1. Install **Tailscale** from the Play Store and sign in.
   - Your own phone: sign in to the same tailnet as your PC.
   - Someone else's phone: they can use their own Tailscale account and you **share only the
     PC** with them (admin console → Machines → your PC → **Share…**), so they don't see the
     rest of your devices. Or invite them to your tailnet and restrict them with an ACL/grant
     to only `<PC>:8444` (and `:8443` for the dashboard).
2. Turn on the Tailscale VPN toggle, then Android Settings → Network → VPN → Tailscale ⚙ →
   **Always-on VPN**. Leave "Block connections without VPN" **off**.
3. Test in the phone browser: `https://<host>:8444/v1/health` should show `{"ok":true,…}`.
   If a shared node's name doesn't resolve, try the PC's Tailscale IP on `http://…:8092` only
   as a **reachability test** — the app itself requires HTTPS.
4. Optional (dashboard): `https://<host>:8443/`.

## 2. Install the APK (sideload)

**Option A, adb (recommended):**
1. On the phone: Settings → About phone → tap **Build number** 7× → Developer options →
   turn on **USB debugging**.
2. Connect USB and accept the "Allow USB debugging?" prompt.
3. `adb devices` → device listed as `device`, not `unauthorized`.
4. `adb install -r android/app/build/outputs/apk/debug/app-debug.apk` → `Success`.
5. Afterwards, turn **USB debugging off** again.

**Option B, file transfer:** copy `app-debug.apk` to the phone (USB file transfer, or
`tailscale file cp app-debug.apk <phone>:`). Open it in Files → allow "Install unknown apps"
**for that Files app only** → Install. Play Protect may warn about an unknown developer →
"Install anyway". Then turn the "unknown apps" permission off again.

The app is called **RingKeeper** (`org.bluecollarlabs.ringkeeper.app`).

## 3. Permissions (first launch)

Open **RingKeeper**. It asks for:
- **Nearby devices** (Bluetooth connect/scan): **Allow**. On Android ≤ 11 it asks for
  **Location** instead (needed for BLE there): allow "while using the app".
- **Notifications**: **Allow**. The sync runs as a foreground service with a notification.

Then in the app's Setup section:
- Tap **Allow background (battery)** → "Let app always run in background?" → **Allow**.
- **OEM extras** (these vendors kill background apps aggressively):
  - Samsung: Settings → Apps → RingKeeper → Battery → **Unrestricted**; Settings → Battery →
    Background usage limits → add RingKeeper to "Never sleeping apps".
  - Xiaomi/Redmi/POCO: App info → Autostart **on**, Battery saver → **No restrictions**.
  - OnePlus/Oppo/Realme: App info → Battery → **Allow background activity**, lock the app in recents.
  - Pixel: "Unrestricted" from the dialog above is enough.
  - See <https://dontkillmyapp.com> for your exact model.

## 4. Associate + pair the fresh ring

The ring must be **factory-reset** (§0), on or next to its charger, near the phone. Lift it
off and put it back on the charger to wake it.

1. Tap **Associate ring**. Android's companion-device chooser appears. Pick the ring
   (it may show as an unnamed device; a reset ring advertises service `98ed0001`). Confirm.
   This enables background BLE for the app (CompanionDeviceManager).
2. Tap **Pair fresh ring** → "Pair a factory-reset ring?" → **Pair**.
   - The app mints a new random 16-byte key and saves it (Android Keystore-encrypted, this
     phone only) **before** installing it on the ring.
   - Android shows a Bluetooth **pairing request** → accept (Just Works, no PIN).
3. The screen then shows `Ring: <serial> fw <version>`, the ring address and the battery
   level. Note the serial.
4. The ring now appears as a paired device in Android Bluetooth settings. **Don't "forget"
   it there** — losing the bond means another factory reset.

If pairing fails, see Troubleshooting before retrying. Repeated attempts can leave the ring half-keyed.

## 5. Server token

On the PC (stores only the token's hash in `server/tokens.json`; the token itself goes to
`secrets/<device>.token`, chmod 600, and is never printed):

```sh
DEVICE=my-phone SERIAL=<serial> tools/phone_setup.sh
```

Get the token onto the phone **without a cloud messenger**: show it as a QR code on the PC
(`qrencode -t ansiutf8 < secrets/my-phone.token`), scan it, copy and paste into RingKeeper →
Setup → token field → **Save token**. Set **Ingest URL** to `https://<host>:8444/v1/events`
if you didn't bake it into the APK. Clear the terminal afterwards (`clear && printf '\e[3J'`).

To rotate: delete the device entry from `server/tokens.json`, trash `secrets/my-phone.token`, re-run.

## 6. Verify the first sync + upload

1. Wear the ring a few minutes.
2. In RingKeeper, tap **Sync now**. A foreground notification appears during the sync.
3. The app shows `Last sync: <time>`, `Events stored: N`, `Upload: … 200 …`.
4. On the PC:
   ```sh
   journalctl --user -u ringkeeper-ingest -n 20 --no-pager   # POST /v1/events 200, device my-phone
   python3 -I tools/phone_db.py status                        # last phone upload + event count
   ```
5. Tap **Sync now** again → the upload shows mostly duplicates / 0 new rows (idempotency check).
6. Leave it overnight. Next morning the last sync should be recent with no tap needed
   (WorkManager runs about every 90 min ±30, when the battery isn't low).

## 7. Measurement features (known gap)

After key-only pairing the ring's automatic HR/SpO2 measurements are **off**. The open_oura
CLI enables them with `oura … features --enable-hr --enable-spo2`; **the app doesn't do this
yet** (see ROADMAP.md). Without it, nights still record sleep/temperature/motion, but daytime
HR and SpO2 are missing.

## 8. Things the wearer should know

- Keep **Tailscale on** (Always-on VPN) and **RingKeeper** installed. Don't force-stop it.
- Don't open the Oura app or set the ring up in it — that replaces the key.
- The ring buffers several days offline (exact depth not measured). Sync at least daily.
- **Cycle features are awareness only, NOT birth control.**
- **Fatigue/readiness numbers are awareness only** — not a fitness-for-duty determination.
- The wearer can export or delete all their data at any time (§10).

## 9. After setup (PC side)

- **Data-source switch:** `data/source.json` = `{"mode": "pc"|"phone", "serial": "…"}`. Missing
  file = `pc`. Env `RINGKEEPER_SOURCE=pc|phone` overrides it for one run.
- **How phone data gets decoded:** ingest stores rows with `decoded_json = NULL`.
  `tools/phone_db.py` (run by `morning_sync.sh` in phone mode) snapshots
  `server/data/ingest.db` read-only, optionally filters by `serial`, runs
  `oura --db <snapshot> redecode` (offline, no BLE), checks the result and atomically
  replaces `data/phone.db` (600). It's rebuilt from scratch every time, so decoder upgrades
  apply to all history.
- **Why not merge into `data/oura.db`:** after a factory reset the ring's `ring_timestamp`
  counter may restart; mixing two clock epochs in one DB would break `RingClock`.
- `tools/backup.sh` snapshots `server/data/ingest.db` too — in phone mode it's the only copy.
- `tools/phone_db.py status` exit codes: 0 ok, 3 no uploads, 4 stale (> 24 h), 1 error.

## 10. Export / delete

- Dashboard → Privacy → **Export** (ZIP) or **Delete** (moves everything to a recoverable trash
  folder; needs the PIN in `secrets/dashboard_pin`). CLI: `python3 -I tools/privacy.py --help`.
- Manual: stop `ringkeeper-ingest`, trash `server/data/ingest.db`, `data/` and the backups in
  `$RINGKEEPER_BACKUP_DIR`, remove the device's token entry.
- On the phone: RingKeeper → app info → Storage → Clear storage, then uninstall. The key is
  gone, so the ring needs a factory reset before any other use.

---

## Troubleshooting

| symptom | check / fix |
| --- | --- |
| Health URL doesn't load on the phone | Tailscale toggle on? PC visible in the phone's Tailscale app? `tailscale serve status` on the PC. Captive-portal Wi-Fi? |
| Upload `401`/`403` | Token typo, or token minted for a different `--serial`. `403` = serial not allowed → mint again with the serial shown in the app. The app stops retrying on 401/403 until a new token is saved. |
| Upload `5xx` / timeout | `systemctl --user status ringkeeper-ingest`; `journalctl --user -u ringkeeper-ingest`; `server/data/` must exist (`mkdir -m 700 -p server/data`). The app retries with backoff. |
| Ring not in the companion chooser | Ring asleep → lift it off the charger and put it back. Bluetooth on? A second phone or the PC still connected (`bluetoothctl disconnect`)? Was it actually reset? |
| No pairing prompt / "pairing rejected" | Accept the system pairing notification quickly (check the notification shade). Remove any stale ring entry in Android Bluetooth settings, toggle Bluetooth, retry. |
| Pair fails at auth / "already keyed" | The ring holds another key (PC test, or the Oura app). Factory-reset it (from the PC while it still has the key, or via the charger per the manufacturer's instructions), then pair again. |
| Connected but drops right away (HCI 0x15 / bond errors) | Bond missing on the phone side. Forget the ring in Bluetooth settings, factory-reset, pair again (a bond can only be created in the post-reset window). |
| Sync only works with the app open | Battery exemption (§3), OEM killer settings, Always-on VPN. `adb shell dumpsys jobscheduler \| grep -A3 org.bluecollarlabs.ringkeeper`. Below ~15 % battery it waits. |
| `INSTALL_FAILED_UPDATE_INCOMPATIBLE` | A different debug keystore built the old APK → `adb uninstall org.bluecollarlabs.ringkeeper.app` first. **That deletes the ring key on the phone → factory reset + re-pair needed.** Always build from the same machine/keystore. |
| Uploads OK but dashboard unchanged | `data/source.json` still `pc`? Switch to phone (§P3 / §9), then `tools/morning_sync.sh`. |
