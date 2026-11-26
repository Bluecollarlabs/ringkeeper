# Getting started

This walks you from "I have a ring and a PC" to "my dashboard shows last night". Plan on an
evening. If you get stuck, open a GitHub issue (no health data, serials or tokens in it!).

> Not a medical device. Fatigue/readiness = awareness only, not fitness-for-duty.
> Cycle tracking = awareness only, NOT contraception. Not affiliated with Oura Health Oy.

## 0. Decide and prepare

- [ ] **Export what you want to keep from the Oura app** (Oura app/web → data export). The
      factory reset in step 3 wipes the ring and the Oura app can't be used with it afterwards.
- [ ] Ring charged and on its charger.
- [ ] A Linux PC that stays on (Python 3, git, `sqlite3`). For the BLE CLI and the Android build:
      Rust (see `mise.toml` / `core/TOOLCHAIN.md`). Packages on Arch: `dbus pkgconf gcc bluez bluez-utils`;
      Debian/Ubuntu: `libdbus-1-dev pkg-config build-essential bluez`.
- [ ] Optional: an Android 10+ phone for all-day automatic sync (recommended).
- [ ] Optional: [Ollama](https://ollama.com) for the local-AI morning note (`docs/LOCAL-AI.md`).

## 1. Get the code

```sh
git clone --recurse-submodules https://github.com/Bluecollarlabs/ringkeeper.git ~/ringkeeper
cd ~/ringkeeper
scripts/fetch-upstream.sh --build          # open_oura @ e57c43c + `oura` CLI
cp config.example.env ringkeeper.env       # edit it (host name, ports, backup dir)
mkdir -m 700 -p secrets data server/data
```

**Linux with BlueZ 5.87 only:** that BlueZ version crashes (`bluetoothd` SIGSEGV) when a
client sets a service-UUID discovery filter, which the CLI does. Update BlueZ to 5.88+ if
your distro has it; otherwise apply our workaround with `scripts/fetch-upstream.sh --patch --build`
(details in `patches/README.md`).

## 2. Run the tests (optional, no ring needed)

```sh
python3 -I -m unittest discover -s tools/analysis/tests     # synthetic data only
python3 -I -m unittest discover -s server
(cd core && cargo test)                                    # needs Rust + the submodule
```

## 3. Choose where the ring pairs

The ring can only be bonded to **one** host comfortably. Pick one:

- **Phone mode (recommended):** the Android app pairs and syncs all day, then uploads to your
  PC over Tailscale. Follow **[PHONE-SETUP.md](PHONE-SETUP.md)** and skip to step 5.
- **PC mode (quick test, or no Android phone):** the PC pairs over Bluetooth and you sync each
  morning with the ring near the PC. Continue with step 4.

## 4. PC mode: pair and sync with the CLI

Factory-reset the ring first (from the Oura app while it's still set up there, or with the
charger's reset procedure from the manufacturer's instructions). **Do not set it up in the
Oura app again** — that installs the app's own key and locks RingKeeper out.

```sh
OURA=upstream/open_oura/target/release/oura
$OURA --name "" scan                                   # look for service 98ed0001-…
$OURA --name "" info                                   # firmware + serial (no key needed)
$OURA --name "" --key-file secrets/ring.key pair       # mints a key, saves it FIRST, installs it
$OURA --key-file secrets/ring.key info                 # battery shown → paired
$OURA --key-file secrets/ring.key features --enable-hr --enable-spo2
```

If BlueZ asks to confirm pairing, accept it (`bluetoothctl` → `agent on` → `default-agent`
in another terminal if needed). **Back up `secrets/ring.key`** (encrypted) — lose it and you
need another factory reset.

Wear the ring overnight. In the morning:

```sh
tools/morning_sync.sh          # sync → analysis → dashboard export → backup → morning note
tools/morning_sync.sh --ai     # same, plus the local-AI note (needs Ollama)
```

## 5. Dashboard

```sh
python3 -I tools/dash_api.py                      # http://127.0.0.1:8091 (loopback only)
tailscale serve --bg --https=8443 http://127.0.0.1:8091   # private HTTPS on your tailnet
```

Open `https://<your-pc>.<tailnet>.ts.net:8443/` on your phone and "Add to home screen". The
dashboard shows demo data until `tools/morning_sync.sh` has exported a real night.

To turn on **Delete my data** in the dashboard, create a PIN: `python3 -I tools/privacy.py gen-pin`.

## 6. Make it automatic

- Ingest service: `server/ringkeeper-ingest.service` (systemd user unit; adjust paths).
- Morning run: a systemd user timer or cron, e.g. `30 7 * * * ~/ringkeeper/tools/morning_sync.sh --ai`.
- Health check: `python3 -I tools/health_check.py` exits non-zero when data is stale, the
  battery is low, a service is down, disk is low or backups are old — hook it to whatever
  notifies you.

## 7. Reading the numbers (please read)

- Scores are **our own open formulas** on **estimated** sleep stages — not Oura's, not
  clinically validated. Trends over weeks matter more than any one night.
- The first ~5–14 nights are "calibrating": baselines need history.
- A low readiness after a night shift or a storm call-out is *information*, not an order —
  and a high one doesn't mean you're fit for duty. Follow your safety program.
- Heat days: the ring can't measure hydration or core temperature. Use your crew's heat-illness
  plan (water, rest, shade) regardless of what the dashboard says.
