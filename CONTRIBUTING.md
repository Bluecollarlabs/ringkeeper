# Contributing to RingKeeper

Thanks for helping. You don't need to be a programmer — hardware reports, docs fixes and
"this step confused me" issues are just as valuable.

## Golden rule: no personal data

**Never** put real health data, database files, exports, screenshots of a real person's
night, ring serial numbers, auth keys, ingest tokens, Tailscale host names or IP addresses
in an issue, PR, test fixture or commit. Use the dashboard's demo data and synthetic values.
If you posted something by mistake, edit it out and tell us — we'll help scrub it.

## Ways to help

- **Hardware reports:** ring generation, firmware version (from `oura info` or the app), phone
  model + Android version, what worked and what didn't. Use the "Hardware report" issue form.
- **Bugs:** steps to reproduce, expected vs actual, logs with anything personal removed.
- **Docs:** especially plain-English setup help for non-developers.
- **Code:** see the roadmap in `docs/ROADMAP.md`. Open an issue before large changes.

## Development

```sh
git clone --recurse-submodules https://github.com/Bluecollarlabs/ringkeeper.git
cd ringkeeper && scripts/fetch-upstream.sh
python3 -I -m unittest discover -s tools/analysis/tests     # analysis + dashboard API
python3 -I -m unittest discover -s server                   # ingest service
(cd core && cargo test)                                     # Rust core (fake ring)
(cd android && ./gradlew assembleDebug)                     # needs core/TOOLCHAIN.md setup
```

Guidelines:

- Python: stdlib only (no pip dependencies), runs under `python3 -I`.
- Tests use **synthetic data only**. Add a test for every behaviour change.
- Keep services loopback-only; never add code that sends health data off the device, or
  that talks to Oura's cloud.
- **Never** add code that decrypts, loads or ships Oura's proprietary models or firmware.
- Scores must stay transparent and labelled as ours (not Oura's), and the medical /
  fitness-for-duty / contraception disclaimers must stay intact.
- Bumping the open_oura pin: read the upstream diff, run all tests, note the new commit in
  `scripts/fetch-upstream.sh`, `core/Cargo.toml` comments, `NOTICE` and `docs/ARCHITECTURE.md`.

By contributing you agree your contribution is licensed under the MIT License (see LICENSE)
and that you follow the [Code of Conduct](CODE_OF_CONDUCT.md).
