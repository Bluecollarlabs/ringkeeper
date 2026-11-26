# Roadmap

Rough order, no dates. Contributions welcome on any of these — open an issue first for big ones.

## Done

- [x] PC-mode pairing + sync on Linux (BlueZ) with the open_oura CLI; BlueZ 5.87 workaround
- [x] Rust core for Android (UniFFI Kotlin bindings), host tests with a scripted fake ring
- [x] Android app: BLE transport, pair/bond, manual + periodic sync, upload over Tailscale
- [x] Ingest service (idempotent, token per device, loopback only) + phone-mode decode path
- [x] Nightly metrics, heuristic staging, baselines, readiness, day/evening + steps (estimated)
- [x] Cycle awareness (temperature shift, period log) — awareness only, not contraception
- [x] Dashboard PWA with export / recoverable delete; local-AI morning note + Q&A
- [x] Backups, health check, weekly summary

## Next

- [ ] App: one-time "enable measurements" step after pairing (HR/SpO2 are off after key-only pairing)
- [ ] App: user-initiated encrypted export of the ring key for offline backup
- [ ] App: CompanionDeviceService presence-based sync (sync when the ring comes in range)
- [ ] More phone + ring field testing; battery impact over a week; measure ring buffer depth
- [ ] Hardware matrix: confirm Gen 3 / Gen 4 / more Gen 5 firmware versions
- [ ] SpO2: per-device calibration coefficients
- [ ] Packaging: one-command PC install (systemd user units + timer), signed release APK

## Ideas for shift and trades workers

- [ ] Shift-aware baselines (day vs night rotation, split sleep, naps)
- [ ] "Coming off nights" recovery view and circadian-shift tracking
- [ ] Heat-day context: overnight skin-temperature + resting-HR elevation after hot shifts
      (awareness only — the ring can't measure hydration or core temperature)
- [ ] Sleep debt over a storm / outage week
- [ ] Simple printable weekly sheet; SMS/chat delivery of the morning note via a bridge you run yourself

## Not planned

- Using, decrypting or redistributing Oura's proprietary models or cloud APIs
- Any hosted/cloud version run by us
- Fitness-for-duty, medical or contraceptive claims
