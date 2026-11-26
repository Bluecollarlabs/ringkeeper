# tools/analysis — nightly metrics, heuristic staging, baselines, sleep note

Python 3 stdlib only. Reads the DB picked by `data/source.json` (`pc` → `data/oura.db`, `phone` →
`data/phone.db` built by `tools/phone_db.py` from the ingest server; see docs/PHONE-SETUP.md §9) **read-only** (`sqlite3` URI `mode=ro`) and writes
derived JSON into `data/` (gitignored — it is personal health data; never commit it).

```
python3 -I tools/analysis/nights.py          # DB -> data/nightly.json (all nights, merged)
python3 -I tools/analysis/stages.py          # text self-evaluation of last night's staging
python3 -I tools/analysis/baselines.py [--json]
python3 -I tools/analysis/sleep_note.py --refresh   # note text on stdout + data/sleep_note.json
python3 -I tools/analysis/cycle.py [--json]          # cycle awareness -> data/cycle.json (see below)
python3 -I tools/analysis/cycle.py --log-start 2026-12-03 [--flow medium] [--symptoms cramps]
python3 -I tools/analysis/cycle.py --log-end 2026-12-07
python3 -I tools/analysis/ai_note.py [note]            # local LLM note -> data/ai_note.json (see docs/LOCAL-AI.md)
python3 -I tools/analysis/ai_note.py ask "How was my deep sleep?"
python3 -I tools/analysis/weekly.py [--end 2026-11-29] [--json]   # Sunday weekly summary -> data/weekly.json
python3 -I tools/analysis/readiness.py [--need-h 8] [--json]       # readiness 0-100 (ours) -> data/readiness.json
python3 -I tools/analysis/day.py [--date 2026-11-25] [--json]      # daytime/evening + steps (est.) -> data/days.json
python3 -I tools/health_check.py [--pretty]                        # JSON {ok, issues[]}; exit 0 ok / 1 issues
python3 -I tools/privacy.py export OUT.zip | delete --confirm "DELETE MY DATA" | gen-pin
python3 -I -m unittest discover -s tools/analysis/tests -v   # synthetic data only
```

Scripts add their own directory to `sys.path`, so they work under `python3 -I`.

## Files

| file | what |
| --- | --- |
| `common.py` | read-only DB, `RingClock` (ring deciseconds → unix via `time_sync`, piecewise-linear), robust stats |
| `nights.py` | night segmentation (`bedtime_period`, later event with same start wins), 30-s signal grid, per-night metrics → `data/nightly.json` |
| `stages.py` | heuristic staging (30-s epochs + Oura-app-style 5-min view), sanity checks, self-evaluation text |
| `baselines.py` | 14/30-night rolling median/MAD baselines, ±2 MAD flags, "need N more nights" |
| `sleep_note.py` | ≤5-line plain-English morning note (chat/SMS-friendly) + JSON |
| `ai_note.py` | optional **local-LLM** morning note + `ask` Q&A over derived metrics (Ollama on 127.0.0.1 only; template fallback) |
| `weekly.py` | ≤ 8-line weekly summary (chat/SMS-friendly text) + `data/weekly.json`: avg sleep, RHR/HRV vs prior week, best/shortest night, bedtime consistency |
| `day.py` | per-calendar-day steps (**estimated**), active minutes/kcal, intensity, daytime HR, exercise sessions, wear time + evening wind-down → `data/days.json` |
| `readiness.py` | readiness score 0–100 (**our formula, not Oura's**) with per-contributor breakdown; "calibrating"/provisional until 5 baseline nights |
| `../privacy.py` | data export ZIP + recoverable delete (moves to trash, never hard-deletes); dashboard PIN |
| `../health_check.py` | data age, ring battery, ingest/dashboard/ollama up, disk, backups → JSON |
| `../dash_api.py` | dashboard server: static `ui-mockup/` + `POST /api/ask` (local AI chat), loopback only |
| `cycle.py` | cycle awareness: temp deviation, biphasic shift (3-over-6 + smoothed), ovulation estimate, phase, next-period prediction, fertile window (estimate). **Not contraception.** |

## Signals (per 30-s epoch, aligned to floor30(bed))

- **ACM** `sleep_acm_period` (0x72): one 30-s window per event. Decoded `acm_mad` holds six
  numbers in parser order `[angle_tmean, angle_max, angle_iqr, mad_tmean, mad_max, mad_iqr]`
  (see `upstream/open_health/docs/algorithms/sleep-staging.md`). We use `mad_tmean` (g) as
  movement and `angle_tmean` jumps (>10°) as position changes. NB: only indices 3–5 are
  accelerometer MAD; 0–2 are finger angle.
- **motion_event** `motion_seconds` per 30-s window.
- **HR** from `ibi_and_amplitude_event` beats: amplitude > 0, 300–2000 ms, ±25 % from the
  local median of 11 valid beats. Beat *k* stamped at event time − Σ ibi[k:] (clamped
  non-decreasing). Epoch HR = 60000 / median IBI (≥ 8 beats).
- **RMSSD (5-min, beats)** from successive valid beats in ±150 s; **HR SD (5-min)** = SD
  of the 30-s HR values in ±2.5 min (slow fluctuation, excludes respiratory arrhythmia).
- **hrv_event** 5-min HR/RMSSD samples (the ring's own) → nightly RMSSD avg/median.
- **Skin temp** `sleep_temp_event` samples, 30 s apart, last at the event time.

## Per-night metrics (`data/nightly.json` → `nights[]`)

`date` (local wake date), `kind` (night ≥ 3 h / nap), `bed`, `wake`, `bed_unix`, `wake_unix`,
`time_in_bed_min`, `sleep_min_est`, `latency_min_est`, `waso_min_est`, `efficiency_pct_est`,
`hr_median` (sleep epochs), `hr_low` (lowest 5.5-min rolling median HR while asleep — used as
"resting HR"), `hr_low_at`, `rmssd_avg`/`rmssd_median`/`rmssd_samples` (hrv_event),
`rmssd_beats_median`, `temp_avg_c`/`temp_min_c`/`temp_max_c`, `movement_index_pct` (% of
epochs with `mad_tmean` > 0.02 g — fixed threshold so nights are comparable),
`movement_bouts_per_h`, `position_changes`, `coverage_pct{hr,acm,temp}`, and
`stages{label, ok, minutes{awake,rem,light,deep}, pct_of_sleep, epochs30 (digit string),
epochs5 (list), epoch_codes, t0_unix, checks_passed, checks_total, threshold_adjusted}`.

Stage codes: `0 awake, 1 REM, 2 light, 3 deep` (same as `tools/export_dashboard.py`).
`epochs30[k]` starts at `t0_unix + 30·k`; `epochs5[k]` at `t0_unix + 300·k`.

## Staging method — **estimated, not validated**

No Oura models; transparent rules (full detail in the `stages.py` docstring):

1. **Wake/sleep** — actigraphy in the Cole–Kripke spirit: movement epochs (MAD > max(0.02 g,
   4× night median), ≥ 5 motion-seconds, or angle jump) become wake if strong, if a
   neighbour also moves, or if HR is ≥ 5 bpm over its 30-min rolling median. Short (< 1.5 min)
   sleep runs between wakes → wake. Onset = first 10 min with ≤ 1 wake epoch; final wake
   after the last quiet 5 min. Isolated twitches stay "sleep" (REM/light twitches are normal).
2. **Deep / REM / light** — robust z-scores within the night of: HR detrended by a 90-min
   rolling median (deep ↓, REM ↑), slow HR SD (REM ↑, deep ↓), log RMSSD (deep ↑), 10-min
   skin-temp slope (small weight, distal warming). Plus time-of-night priors: deep favoured
   early (sleep pressure decays, τ = 2 h), REM suppressed for the first hour and favoured
   later. Scores smoothed over 3.5 min; deep requires no movement in ±2.5 min; deep/REM
   bouts < 3 min → light.
3. **Guard** — if deep falls outside 5–30 % or REM outside 8–32 % of sleep, thresholds are
   nudged into the band and `threshold_adjusted = true` (the proportion checks are then not
   independent evidence).
4. **5-min view** — the Oura app's display rule: 10 epochs; ≥ 4 awake → awake; else mode,
   ties awake > deep > REM > light.

**Sanity checks** (reported, not enforced): deep 10–25 %, REM 15–25 % of sleep, ≥ 60 % of
deep in the first half, ≥ 55 % of REM in the second half, first REM ≥ 45 min after onset,
efficiency 70–98 %, HR and ACM coverage ≥ 80 %.

**Caveats** — The time-of-night priors push deep early / REM late, so the half-night checks
are partly a consequence of the priors, not proof of accuracy. Weights were set from
physiology literature and one real night, never fitted against PSG or the Oura app. Light
vs REM and light vs deep are the weakest distinctions; wake detection (motion-driven) is the
most trustworthy part. Synthetic tests check that the code recovers a planted hypnogram
(~89 % epoch agreement); that is a plumbing test, not a validation.

## Baselines

For night *i*, baseline = nights before *i* (kind = night) in the last 14 and 30:
median and MAD × 1.4826. Flag `high`/`low` when outside median ± 2·MAD (14-night window),
with MAD floors (RHR 1 bpm, RMSSD 4 ms, temp 0.10 °C). Needs ≥ 5 prior nights; otherwise
`status = insufficient`, `need_more = N`. `temp_dev` value = tonight − 14-night median.
`history()` gives per-night value + band for trend charts.

## Cycle (`cycle.py`) — awareness only, NOT birth control

Inputs: `data/nightly.json` + `data/cycle_log.json` (gitignored, 600; a list of
`{start, end?, flow?, symptoms?}` or `{"periods": [...], "hormonal_bc": bool}`).
Every output has a `disclaimer` field: UIs/AI answers must show it. Hormonal BC suppresses
the temperature shift (`hormonal_bc: true` adds a message).

- Nightly value = `temp_avg_c`; skipped: temp coverage < 50 %, < 3 h in bed, isolated spikes
  > 0.45 °C from the 3+3-neighbour median. Deviation = value − median of up to 28 prior
  valid nights ("provisional" with < 3).
- **3-over-6**: 3 consecutive valid nights above the max of the previous 6 (6 within 10
  days, 3 within 4), mean rise ≥ 0.2 °C, and **sustained** (median of the next ≤ 5 nights
  ≥ lows + 0.15 °C; `provisional` until 3 later nights exist). Ovulation ≈ day before the
  first high night. **Smoothed** variant: 3-night median + best level-shift split (≥ 0.2 °C,
  ≥ 80 % of after-nights above midpoint). Primary = 3-over-6, else smoothed, else calendar
  (predicted length − mean luteal, default 14).
- Prediction: mean of last ≤ 6 complete cycles (15–60 d), ± max(2, SD) days; none →
  28 d default, `confidence: low`. Fertile window = est. ovulation −5..+1 d, labelled estimate.
- Sparse data: `status` (`no_period_logged`/`ok`), `need{period_log, nights}`, `messages`.
- Synthetic sweep (200 seeds, 28-d cycle ov day 14, +0.35 °C luteal, Gaussian night noise):
  noise 0.08 °C → 100 % within ±2 d, 0 % false shifts in anovulatory cycles;
  0.12 °C → 94 % / 3 %; 0.15 °C → 87 % / 14 %. Real finger-temp noise is not yet measured,
  so treat any single-cycle estimate as rough.
- Exported by `tools/export_dashboard.py` as `cycle`; the UI Cycle tab (Real mode) shows an
  onboarding card until a period is logged.

## Sleep note

`sleep_note.py` → ≤ 5 lines, second person, non-medical (no diagnoses or advice); stage
numbers marked "estimated". JSON (`data/sleep_note.json`): `text`, `lines`, `night{…}`,
`stages{…}`, `baselines{rhr,rmssd,temp_dev: value, raw, status, need_more, z_mad, unit}`.

## Local AI note (`ai_note.py`)

Optional, off by default (`tools/morning_sync.sh --ai`). Setup, model choice and the privacy
model are in [`docs/LOCAL-AI.md`](../../docs/LOCAL-AI.md). In short:

- Context = derived numbers only (`ai_note.py context` prints exactly what the model sees): last
  night's summary, estimated stage minutes, `vs_your_usual` from baselines, 7-night trend, and
  cycle day / phase / days to next period if a period log exists (fertility fields left out).
- The endpoint must be loopback (`assert_loopback`); proxies are bypassed. No cloud API anywhere.
- Strict output check: ≤ 4 model lines (one sentence each) + fixed disclaimer line = 5; ≤ 220
  chars/line, ≤ 700 total; no links/markdown; banned diagnosis/medication/fertility words; no
  "usual/typical/normal" comparisons before baselines exist; every number > 12 must be traceable
  to the context (minutes may be written as `Xh Ym` / hours). One retry with the rejection reason,
  then fall back to the `sleep_note.py` template (`source: "template"`, `ai_error`).
- `ask`: same context, ≤ 8 lines, named illnesses/diagnoses/doses → canned "ask a qualified
  professional" reply. Exit 1 when no model answer is returned.
- Tests: `tests/test_ai_note.py` (mock Ollama HTTP server on 127.0.0.1, synthetic nights).

## Weekly summary (`weekly.py`)

Week = the 7 wake dates ending on `--end` (default today; run it on Sunday), prior week = the 7
before. Nights only (naps ignored). Lines (≤ 8): header with nights recorded; average sleep
(estimated; falls back to time in bed) vs prior week; resting HR (`hr_low`) and HRV
(`rmssd_avg`) averages with delta vs prior week (steady within ±1 bpm / ±3 ms); best and
shortest night; bedtime consistency = SD of bedtime with the clock wrapped at noon (≤ 30 min
very consistent, ≤ 60 fairly, else variable) + range; a "few more nights" hint under 4 nights;
an Activity line when `data/days.json` exists (avg estimated steps/day + active min/day over days with ≥ 60 recorded awake minutes, vs prior week); the not-medical-advice line is always last (lowest-priority lines are dropped to stay ≤ 8). JSON adds the raw numbers and `data_source` (pc/phone).
Tests: `tests/test_weekly.py`.

## Dashboard server + live AI chat (`tools/dash_api.py`)

`ringkeeper-dashboard.service` runs `dash_api.py --port 8091` (127.0.0.1 only; tailscale serve
exposes it at https://<RINGKEEPER_PUBLIC_HOST>:8443). It serves files under `ui-mockup/` only
(no traversal, dotfiles, symlinks out of the root, or listings) and:

- `POST /api/ask {"question"}` → `{answer, source: "local AI", ok, model, data_source, wall_s}`
  via `ai_note.ask` (Ollama on loopback, derived metrics from `data/nightly.json`, which follows
  `data/source.json`). Guards: body ≤ 4 KB, question ≤ 300 chars, one model call at a time
  (429 busy), 6 asks/min and 60/day globally, cross-origin POSTs refused. Questions are never logged.
- `GET /api/health` → `{ok, data_source, nights}`.

The AI tab uses it in Real mode (typing indicator, error bubbles); Demo mode keeps the canned
sample conversation. Tests: `tests/test_dash_api.py` (stub ask, temp web root).

PWA: `ui-mockup/manifest.json`, `icons/` (regenerate with `python3 -I tools/pwa_icons.py ui-mockup/icons`),
`sw.js` (network-first for pages + `real-data.json`, cache-first for the versioned static shell,
never caches `/api/`). When bumping `?v=` in `index.html`, bump `VERSION` in `sw.js` too. The
offline copy of `real-data.json` lives in the phone browser's cache storage (same device).

## Readiness (`readiness.py`) — our score, not Oura's

Weighted mean (weights renormalised over contributors that have data) of 0–100 sub-scores:

| contributor | weight | baseline mode (≥ 5 prior nights) | before baseline (provisional) |
| --- | --- | --- | --- |
| resting HR (`hr_low`) | 25 | 85 − 15·z (MAD-z vs 14-night median) | bands: ≤ 50 bpm 90, 60 → 75, 70 → 55, ≥ 80 → 30 |
| HRV (`rmssd_avg`) | 25 | 85 + 15·z | bands: ≤ 15 ms 30, 20 → 50, 40 → 75, ≥ 60 → 90 |
| sleep vs need (default 8 h, `--need-h`) | 20 | 100 − 150 × missing share (`sleep_min_est`, else 0.9 × time in bed) | same |
| efficiency (est.) | 10 | 100 − 3 × (90 − eff %) | same |
| skin temp deviation | 10 | 100 − 150 × max(0, \|dev\| − 0.2 °C) | skipped (needs baseline) |
| previous-day activity | 10 | awake MET minutes in the 16 h before bed via `day.py` (≥ 60 min): mean 1.3–2.2 MET → 100, < 1.3 → 85, heavier −40/MET; > 3 h at ≥ 3 MET or > 1 h at ≥ 6 MET lowers it (floor 40); full still day (< 15 active min, < 3000 est. steps) → 85. Detail shows est. steps + active minutes | same |

`status: calibrating`, `provisional: true` while RHR/HRV still use the absolute bands; the UI shows
`82*` + "calibrating · provisional". Output: `score`, `band`, `contributors[{key, name, weight,
available, score, basis, detail, share_pct, points}]` (points sum to the score), `need_more`,
`history[]`, `note`. Exported as `readiness` by `export_dashboard.py` (Today ring replaces the old
Recovery placeholder; breakdown under "What went into readiness?"). Weights and bands are choices,
not fitted to anything. Tests: `tests/test_readiness.py`.

## Export / delete (`tools/privacy.py`, dashboard Device sheet → Privacy)

- `GET /api/export` → ZIP: `README.txt`, `nightly.json`, `cycle_log.json`, `readiness.json`,
  `events.csv` (every row of the source DB: id, name, tag, ring_timestamp, unix_time, local_time,
  body_hex, decoded_json). Built in a temp file, streamed, never stored.
- `POST /api/delete {"confirm": "DELETE MY DATA", "pin"}` — PIN from `secrets/dashboard_pin`
  (600, `privacy.py gen-pin`; delete is disabled if it is missing or group/world-readable);
  5 wrong PINs / 15 min (20/day) lock it; same-origin JSON only; refuses (409) while
  `morning_sync.sh` holds its lock. **Never hard-deletes**: moves everything in `data/` except
  `source.json` and dotfiles, plus `ui-mockup/real-data.json`, into
  `~/.local/share/Trash/files/ringkeeper-YYYYmmdd-HHMMSS/` (+ `.trashinfo`; fallback
  `data/.deleted-…`), and moves the ring serial's rows out of `server/data/ingest.db` into
  `ingest-rows.db` there. `manifest.json` + `RESTORE.txt` inside; keep ≥ 30 days (nothing purges
  it automatically). Not touched: `secrets/`, `~/backups/ringkeeper` (14-day rotation).
- Tests: `tests/test_privacy.py` (temp tree, synthetic DBs).

## Health check (`tools/health_check.py`)

JSON `{ok, time, data_source, issues[{check, severity, message}], checks{}}`; exit 0 ok / 1 issues.
Checks: newest data for the current mode ≤ 24 h (pc: `sync_state.last_sync_unix`; phone: last
upload in ingest.db), latest decoded ring battery (`debug_data` `battery_level_changed`) ≥ 20 %,
ingest `:8092/v1/health`, dashboard `:8091/api/health`, Ollama `:11434/api/version` (loopback,
no proxies), disk free ≥ 10 % (repo + backup fs), newest `~/backups/ringkeeper` snapshot ≤ 2 days.
Not scheduled by itself. Tests: `tests/test_health_check.py`.

## Day / evening + steps (`day.py`) — estimates

Local calendar days (system time zone or `RINGKEEPER_TZ`, DST-safe). Day metrics use only minutes **outside** sleep
windows (nightly.json nights/naps + DB `bedtime_period`). Output `data/days.json`:
`days[]{date, recorded_min, sleep_min_excluded, wear_min, wear_awake_min, steps{value, estimated,
source, gait_windows, gait_walking_min, gait_steps_est, ring_counter, real_steps_present},
distance_km_est, intensity_min{sedentary <1.5, low 1.5–3, medium 3–6, high ≥6 MET}, active_min
(≥ 3 MET), met_min, mean_met, active_kcal, weight_kg, hourly{steps, met, recorded_min, hr}[24],
hr{min, avg, max, samples_5min}, hrv_rmssd_median, last_activity, exercise_sessions[],
activity_bouts[]}` and `evenings[]` (one per night).

- **MET** `activity_information` 0x50: one bin per **minute**, **end-stamped** (13-bin events arrive
  ~13 min apart; the native parser stamps batched samples so the last one is at the event time).
  Sleep has irregular gaps (events missing), so we count recorded minutes, never assume coverage.
- **Steps are estimated.** `real_step_event_feature_1/2` (0x7e/0x7f, REAL_STEPS) carry 30-s gait
  *features*, not counts; counting needs Oura's proprietary `steps_motion_decoder` TorchScript model
  (see `upstream/open_oura/tools/run_activity_model.py`), which we don't have / ship. Headline =
  upstream oura-summary MET rule: 105 steps/min at 2.5–7 MET, 150 at ≥ 7 (rounded to 10). Also
  reported, never headline: REAL_STEPS gait windows (× 30 s = walking minutes; × 1.8 steps/s =
  `gait_steps_est`, unvalidated) and `ring_counter` = sum of the 0x50 `state` byte — 0 in sleep, rises
  with MET, saturates at 255; on the first real evening it summed to 841 vs 870 from the MET rule, so
  it may be the ring's own step count, **unverified** (validate with a counted walk).
- **Active kcal** = Σ max(0, MET − 1) × weight / 60. Weight: `data/profile.json {"weight_kg": …}`,
  else `user_information` 0x5c unless it is the firmware default (40 y / 75 kg / sex 2 / 176 cm — what
  our ring reports), else null.
- **Daytime HR**: beats from 0x60 (amplitude > 0) + 0x80 (quality ≤ 1), 300–2000 ms, ±25 % local
  median; 5-min medians (≥ 10 beats) outside sleep → min/avg/max; hourly medians.
- **Exercise sessions**: `ehr_trace_event` 0x73 has **no decoder upstream** (payload unknown) → sessions
  from event timing (gap > 5 min splits) merged with `feature_session` feature_id 3 start(1)/stop(≠1);
  HR from beats; `ehr_acm_intensity_event` 0x74 intensity mean/max. MET **activity bouts** (≥ 3 MET,
  ≥ 5 min, gaps ≤ 2 min) are listed separately as estimates.
- **Wear time**: minutes with temp_event probe 0 ≥ 30 °C or ≥ 3 valid beats.
- **Evening wind-down** (3 h before bed; belongs to the date of bed − 6 h): HR 15-min medians,
  first- vs last-hour mean, slope (settling ≤ −3 bpm, rising ≥ +3), RMSSD first/last hour (hrv_event,
  else 5-min beat RMSSD), mean MET, last minute ≥ 2.5 MET ("last activity") and ≥ 3 MET, `partial`
  when < 2 h of HR or MET is in the window.
- Exported by `export_dashboard.py` as `day` (today, latest, last 14 days, latest evening); used by
  the Activity tab (Real), the Today Steps ring + Evening card, readiness (previous-day activity) and
  `weekly.py`. Tests: `tests/test_day.py` (synthetic events + temp DB).
