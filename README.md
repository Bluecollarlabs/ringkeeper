# RingKeeper

**Your smart ring's sleep and recovery data — on your own PC and phone. No subscription. No cloud.**

RingKeeper works with **Oura Ring Gen 3/4/5 via the [open_oura](https://github.com/Th0rgal/open_oura)
protocol library**. It pulls the raw data straight off *your own* ring over Bluetooth, stores
it on *your own* computer, works out sleep, heart rate, HRV, temperature and recovery
trends with open formulas you can read, and (optionally) writes a plain-English morning note
with an AI model that runs **on your PC** — not in somebody's data center.

It's free, open-source research software from [Blue Collar Labs](https://github.com/Bluecollarlabs).

> ### ⚠️ Read this first
> - **Not affiliated with, endorsed by, or supported by Oura Health Oy.** "Oura" is their
>   trademark; we use the name only to say which hardware this works with.
> - **Not a medical device.** Nothing here diagnoses, treats or prevents anything. Talk to a
>   doctor about your health.
> - **Fatigue / readiness numbers are for awareness only — they are NOT a fitness-for-duty
>   determination.** Follow your employer's rules, your safety program and your own judgment.
>   A "good" score never means it's safe to climb, drive or work hot.
> - **Cycle tracking is awareness only — NOT contraception.** Do not use it to prevent pregnancy.
> - The Bluetooth protocol was **reverse-engineered** by the open_oura project for
>   interoperability with a device you own. A firmware update can break it at any time.
> - RingKeeper **never decrypts, uses or ships Oura's proprietary algorithms or models.** All
>   scores are our own open formulas, and they are *not* Oura's scores.
> - Using it means **factory-resetting your ring and leaving the Oura app**. You lose Oura's
>   app, cloud history and membership features for that ring. Export anything you want to
>   keep from the Oura app first.

---

## Who it's for

People who work long, early, late or rotating hours and want to *see* how they're actually
recovering:

- **Electricians, linemen, storm crews** — 16-hour restoration days, call-outs at 2 a.m.
- **HVAC and refrigeration techs** — heat days in attics and on rooftops.
- **Shift and night workers** — plants, hospitals, warehouses, public safety.
- **Contractors and the self-employed** — who don't want another monthly subscription.

## Why

- **Sleep, recovery and fatigue awareness** across long shifts, night work, back-to-back
  call-outs and heat. Spot the trend *before* the bad week.
- **No subscription.** Your ring, your data, your PC.
- **Your data stays with you.** It goes ring → your phone → your PC over a private
  [Tailscale](https://tailscale.com) link. Nothing is uploaded to Oura, us, or anyone else.
- **Local AI.** The optional morning note and "ask a question" chat use a small model
  running on your own PC through [Ollama](https://ollama.com). Only derived numbers are given
  to the model, and the code refuses to talk to anything that isn't on your own machine.
- **Open formulas.** Every score is a few lines of Python you can read and argue with.
- **Export or delete everything** from the dashboard at any time.

## What you need

| | |
| --- | --- |
| **Ring** | Oura Ring Gen 3, 4 or 5 that you own (most testing so far is on Gen 5; Gen 3/4 rely on open_oura's support — hardware reports welcome). It will be **factory-reset** and won't work with the Oura app afterwards (you can go back by resetting again and setting it up in the Oura app). |
| **PC** | Any Linux PC that's on most of the time (an old desktop or mini-PC is fine). Python 3 (tested on 3.14), stdlib only. macOS/Windows: the analysis and dashboard should work; the BLE CLI path is only tested on Linux. 6 GB GPU optional for fast local AI. |
| **Phone** | Android 10 or newer (for automatic syncing during the day). iPhone isn't supported yet. |
| **Free accounts** | [Tailscale](https://tailscale.com) (free personal plan) to connect phone ↔ PC privately. |
| **Time** | An evening for setup. Comfortable with a terminal, or have a friend who is. |

## Quick start

Full walkthrough: **[docs/GETTING-STARTED.md](docs/GETTING-STARTED.md)**. The short version:

```sh
git clone --recurse-submodules https://github.com/Bluecollarlabs/ringkeeper.git ~/ringkeeper
cd ~/ringkeeper
scripts/fetch-upstream.sh --build      # open_oura at the tested commit (+ the `oura` CLI)
cp config.example.env ringkeeper.env   # then edit: your Tailscale host name, ports, backups

# PC side: ingest service + dashboard (loopback only; Tailscale publishes them privately)
mkdir -m 700 -p server/data secrets data
python3 -I server/ingest.py serve &
python3 -I tools/dash_api.py &          # http://127.0.0.1:8091

# Phone side: build + install the Android app, pair the ring → docs/PHONE-SETUP.md
```

Every morning (cron or a systemd timer):

```sh
tools/morning_sync.sh --ai     # decode → analyze → dashboard → backup → morning note
```

## Screenshots

_Coming soon — dashboard (Today / Sleep / Readiness / Trends / AI) and the Android app._
<!-- Put PNGs in docs/img/ and link them here. Use the demo data only — never a real person's night. -->

## What's inside

| folder | what |
| --- | --- |
| `android/` | Kotlin app: BLE sync with the ring, local buffer, upload to your PC |
| `core/` | Rust library (UniFFI) the app uses — wraps open_oura's protocol, link and storage crates |
| `server/` | Tiny stdlib-only Python ingest service (`POST /v1/events`), loopback-only |
| `tools/analysis/` | Nightly metrics, sleep staging heuristic, baselines, readiness, day/evening, cycle awareness, AI note |
| `tools/` | Morning sync, dashboard server, export/delete, backup, health check |
| `ui-mockup/` | The dashboard (static HTML/JS, installable as a PWA) with built-in demo data |
| `patches/` | Optional workaround patch for a BlueZ 5.87 crash (Linux) — see `patches/README.md` |
| `upstream/open_oura` | git submodule: the open_oura protocol library at a pinned, tested commit |
| `docs/` | [Getting started](docs/GETTING-STARTED.md) · [Phone setup](docs/PHONE-SETUP.md) · [Local AI](docs/LOCAL-AI.md) · [Architecture](docs/ARCHITECTURE.md) · [Roadmap](docs/ROADMAP.md) |

## Safety and privacy notes

- **Health data is sensitive** — reproductive/cycle data most of all. Keep the PC's disk
  encrypted, keep backups on an encrypted drive, and never expose the services with
  `tailscale funnel` or port-forwarding.
- If you set this up **for someone else**, it's their data: get their consent, show them
  how to export and delete it, and don't look at it without their OK.
- Fatigue scores are rough estimates from a ring on one finger. They miss a lot (caffeine,
  dehydration, stress, illness, medication). Use them to start a conversation with yourself,
  not to make a safety call.

## Contributing

Bug reports, hardware reports ("works on my Gen 4 with firmware X") and pull requests are
welcome. See [CONTRIBUTING.md](CONTRIBUTING.md). **Never attach real health data, ring
serials, keys or tokens to an issue.** Security problems: see [SECURITY.md](SECURITY.md).

## Credits and license

MIT — see [LICENSE](LICENSE) and [NOTICE](NOTICE). Built on
[open_oura](https://github.com/Th0rgal/open_oura) and ideas from
[open_health](https://github.com/Th0rgal/open_health) by Th0rgal (both MIT). Thank you.
