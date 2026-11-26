#!/usr/bin/env python3
"""Export real ring data from data/oura.db (pc mode) or data/phone.db (phone mode, see
tools/phone_db.py and data/source.json) into ui-mockup/real-data.json for the UI mockup.

Opens the DB strictly read-only. Output contains personal health data: it is .gitignored.

Night segmentation, sleep staging, baselines and the morning note come from tools/analysis
(nights.py / stages.py / baselines.py / sleep_note.py); this script adds the 5-min chart
series (HR, RMSSD, temp, motion, SpO2 est.) and packages everything for ui-mockup/app.js.

Usage: python3 -I tools/export_dashboard.py [--db PATH] [--out PATH] [--nightly PATH] [--cycle-log PATH]
                                           [--ai-note PATH] [--no-refresh]
  --no-refresh  use the existing data/nightly.json (e.g. morning_sync.sh already refreshed it)
  --ai-note     local-LLM note (tools/analysis/ai_note.py, default data/ai_note.json); exported as
                `ai_note` only if it was written by the model for this same night
"""
import argparse
import collections
import datetime as dt
import json
import os
import sqlite3
import statistics as st
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools', 'analysis'))
import baselines as B  # noqa: E402
import common as C  # noqa: E402
import cycle as CY  # noqa: E402
import day as DY  # noqa: E402
import nights as N  # noqa: E402
import readiness as RD  # noqa: E402
import sleep_note as SN  # noqa: E402
TZ = C.TZ
EPOCH_S = 300  # 5-min bins
FIRMWARE = None  # fallback when the DB has no firmware info

# SpO2 "Simple" calibration: SpO2 = a*r^2 + b*r + c, clamped to [85, 100].
# The coefficients are NOT read from the ring: they are a static per-hardware table in the
# official app (com/ouraring/oura/workitem/data/items/d.java), see
# upstream/open_oura/docs/spo2-calibration.md and tools/run_spo2.py. Ring 5 is not in that
# table (app v7.18 predates it); gen4/oreo is upstream's default and differs from 'cooper'
# by <1%. The firmware-computed % (spo2_event 0x6f / 0x70) is not emitted by our sync.
SPO2_COEFFS = {'gen4': (-13.4, -5.1, 105.2), 'cooper': (-12.1, -6.9, 106.3)}
SPO2_HW = 'gen4'


def spo2_pct(rv, hw=SPO2_HW):
    a, b, c = SPO2_COEFFS[hw]
    return min(100.0, max(85.0, a * rv * rv + b * rv + c))


def q(vals, p):
    v = sorted(vals)
    if not v:
        return None
    return v[min(len(v) - 1, int(p * len(v)))]


def r(x, d=1):
    return None if x is None else round(x, d)


def baselines_doc(nights):
    ev = B.evaluate(nights)
    words = {'rhr': 'Resting HR', 'rmssd': 'HRV (RMSSD)', 'temp_dev': 'Skin temp deviation'}
    metrics = {k: {kk: m.get(kk) for kk in ('value', 'raw', 'status', 'need_more', 'z_mad', 'unit')}
               | {'name': words.get(k, k), 'median14': ((m.get('windows') or {}).get('14') or {}).get('median')}
               for k, m in ev.get('metrics', {}).items()}
    need = [m['need_more'] for m in metrics.values() if m['status'] == 'insufficient' and m.get('need_more')]
    return {
        'nights_available': ev.get('nights_available', 0), 'min_nights': B.MIN_NIGHTS, 'k_mad': B.K,
        'status': 'insufficient' if need else ('ok' if metrics else 'none'),
        'need_more': max(need) if need else 0,
        'text': B.describe(ev) if metrics else [],
        'metrics': metrics,
        'history': B.history(nights),
    }


def sleep_note_doc(nights):
    note = SN.build_note(nights)
    return {'text': note['text'], 'lines': note['text'].split('\n'), 'generated': note.get('generated')}


def ai_note_doc(path, night_date):
    """data/ai_note.json -> `ai_note` block, or None (missing, template fallback, or another night)."""
    j = C.read_json(path, None) if path and os.path.exists(path) else None
    if not isinstance(j, dict) or j.get('source') != 'ai' or not j.get('text') or j.get('night_date') != night_date:
        return None
    return {'text': j['text'], 'lines': j.get('lines') or j['text'].split('\n'), 'generated': j.get('generated'),
            'model': j.get('model'), 'night_date': j.get('night_date'), 'local_only': True}


def cycle_doc(nights, log_path):
    """tools/analysis/cycle.py result (awareness only; carries `disclaimer`). Never fatal."""
    try:
        periods, settings = CY.load_log(log_path)
        res = CY.analyze(nights, [{'start': p['start'].isoformat(), 'end': p['end'] and p['end'].isoformat(),
                                   'flow': p['flow'], 'symptoms': p['symptoms']} for p in periods],
                         settings=settings)
        res['log_path'] = os.path.relpath(log_path, ROOT)
        return res
    except Exception as e:  # keep the dashboard export working even if cycle analysis breaks
        return {'status': 'error', 'error': f'{type(e).__name__}: {e}', 'disclaimer': CY.DISCLAIMER}


def readiness_doc(nights, db):
    """tools/analysis/readiness.py (our score, not Oura's); also refreshes data/readiness.json. Never fatal."""
    try:
        res = RD.build(nights, db)
        C.write_json_atomic(RD.READINESS_JSON, res)
        return res
    except Exception as e:
        return {'status': 'error', 'score': None, 'error': f'{type(e).__name__}: {e}', 'label': RD.LABEL, 'contributors': []}


def day_doc(db, nightly_path):
    """tools/analysis/day.py: per-day steps (estimated) / activity / daytime HR + evening wind-down.
    Also refreshes data/days.json. Exported: last 14 days, today's day, latest evening. Never fatal."""
    try:
        doc = DY.build(db, nightly_path)
        C.write_json_atomic(DY.DAYS_JSON, doc)
        today = dt.datetime.now(TZ).date().isoformat()
        days = doc.get('days') or []
        evs = doc.get('evenings') or []
        return {'label': doc.get('label'), 'sources': doc.get('sources'), 'weight_kg': doc.get('weight_kg'),
                'weight_source': doc.get('weight_source'), 'today_date': today,
                'today': next((d for d in days if d['date'] == today), None),
                'latest': days[-1] if days else None,
                'days': days[-14:], 'evening': evs[-1] if evs else None, 'evenings': evs[-14:],
                'generated': doc.get('generated')}
    except Exception as e:
        return {'status': 'error', 'error': f'{type(e).__name__}: {e}', 'days': [], 'today': None, 'evening': None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=C.DEFAULT_DB,
                    help='default: data/oura.db (pc mode) or data/phone.db (phone mode, data/source.json)')
    ap.add_argument('--out', default=os.path.join(ROOT, 'ui-mockup', 'real-data.json'))
    ap.add_argument('--nightly', default=C.NIGHTLY_JSON)
    ap.add_argument('--cycle-log', default=CY.CYCLE_LOG)
    ap.add_argument('--ai-note', default=os.path.join(C.DATA_DIR, 'ai_note.json'))
    ap.add_argument('--no-refresh', action='store_true', help='do not re-run nights.py first')
    a = ap.parse_args()

    # ---- analysis: per-night metrics + staging (tools/analysis) ----
    if not a.no_refresh or not os.path.exists(a.nightly):
        N.main(['--db', a.db, '--out', a.nightly, '--quiet'])
    all_nights = (C.read_json(a.nightly, {}) or {}).get('nights', [])
    sleeps = [x for x in all_nights if x.get('kind', 'night') == 'night']
    if not sleeps:
        raise SystemExit(f'no nights in {a.nightly}')
    night = sleeps[-1]
    stg = night.get('stages') or {}

    con = sqlite3.connect(f'file:{a.db}?mode=ro', uri=True)
    rows = con.execute('SELECT name, ring_timestamp, decoded_json FROM events WHERE decoded_json IS NOT NULL ORDER BY ring_timestamp').fetchall()
    ev = collections.defaultdict(list)
    for name, ts, js in rows:
        try:
            ev[name].append((ts, json.loads(js)))
        except ValueError:
            pass
    counts = dict(sorted(((n, c) for n, c in con.execute('SELECT name, COUNT(*) FROM events GROUP BY name')), key=lambda x: -x[1]))

    # ---- ring decisecond clock -> unix, anchored on time_sync events ----
    syncs = sorted((ts, d['unix_time']) for ts, d in ev['time_sync'] if 'unix_time' in d)
    if not syncs:
        raise SystemExit('no time_sync events: cannot convert ring timestamps')
    (t0, u0), (t1, u1) = syncs[0], syncs[-1]
    rate = (u1 - u0) / ((t1 - t0) / 10) if t1 != t0 else 1.0  # ~1.0; corrects ring clock drift

    def unix(ts):
        return u0 + (ts - t0) / 10 * rate

    def iso(u):
        return dt.datetime.fromtimestamp(u, TZ).isoformat(timespec='seconds')

    # ---- night window ----
    if not ev['bedtime_period']:
        raise SystemExit('no bedtime_period event')
    # same night as tools/analysis picked (later bedtime_period with the same start wins)
    bp = next((d for _, d in reversed(ev['bedtime_period'])
               if d.get('bedtime_start_ds') == night.get('bedtime_start_ds')), ev['bedtime_period'][-1][1])
    bed_u, wake_u = float(night['bed_unix']), float(night['wake_unix'])
    n = int((wake_u - bed_u) // EPOCH_S) + (1 if (wake_u - bed_u) % EPOCH_S else 0)

    def epoch(ts):
        k = int((unix(ts) - bed_u) // EPOCH_S)
        return k if 0 <= k < n else None

    def epoch_u(u):
        k = int((u - bed_u) // EPOCH_S)
        return k if 0 <= k < n else None

    # ---- HR from IBI beats (amplitude>0, 35<hr<120) ----
    hr_bins = [[] for _ in range(n)]
    for ts, d in ev['ibi_and_amplitude_event']:
        k = epoch(ts)
        if k is None:
            continue
        for hr, amp in zip(d.get('hr_bpm', []), d.get('amplitude', [])):
            if amp > 0 and 35 < hr < 120:
                hr_bins[k].append(hr)
    hr = [r(st.median(b), 1) if len(b) >= 5 else None for b in hr_bins]

    # ---- HRV (hrv_event: 5-min HR + RMSSD; samples end at the event timestamp) ----
    rmssd = [None] * n
    hr5 = [None] * n
    for ts, d in ev['hrv_event']:
        step = d.get('interval_min', 5) * 60
        hs, rs = d.get('hr_bpm', []), d.get('rmssd_ms', [])
        m = len(rs)
        for i, (h, v) in enumerate(zip(hs, rs)):
            u = unix(ts) - (m - 1 - i) * step - step / 2  # centre of the 5-min window
            k = epoch_u(u)
            if k is None:
                continue
            if v > 0:
                rmssd[k] = v
            if h > 0:
                hr5[k] = h
    # fill HR gaps from hrv_event HR
    hr = [h if h is not None else hr5[k] for k, h in enumerate(hr)]

    # ---- skin temperature ----
    t_bins = [[] for _ in range(n)]
    for ts, d in ev['sleep_temp_event']:
        vs = d.get('temps_c', [])
        for i, v in enumerate(vs):  # ~30 s per sample, last one at event timestamp
            k = epoch_u(unix(ts) - (len(vs) - 1 - i) * 30)
            if k is not None and 30 < v < 40:
                t_bins[k].append(v)
    for ts, d in ev['temp_event']:  # fallback: first channel tracks sleep_temp closely
        k = epoch(ts)
        vs = d.get('temps_c', [])
        if k is not None and not t_bins[k] and vs and 30 < vs[0] < 40:
            t_bins[k].append(vs[0])
    temp = [r(st.mean(b), 2) if b else None for b in t_bins]

    # ---- motion ----
    acm_bins = [[] for _ in range(n)]
    for ts, d in ev['sleep_acm_period']:
        k = epoch(ts)
        if k is not None:
            acm_bins[k].extend(d.get('acm_mad', []))
    msec = [0] * n
    for ts, d in ev['motion_event']:
        k = epoch(ts)
        if k is not None:
            msec[k] += d.get('motion_seconds', 0)
    acm = [st.mean(b) if b else None for b in acm_bins]
    acm_ok = [x for x in acm if x is not None]
    acm_p75 = q(acm_ok, 0.75) or 0
    acm_p90 = q(acm_ok, 0.90) or 0
    # restlessness 0..100: motion seconds dominate, acm adds the fine-grained part
    motion = []
    for k in range(n):
        a_ = acm[k] or 0
        s = min(100, msec[k] / 30 * 100 * 0.7 + min(1, a_ / (acm_p90 * 2 or 1)) * 30)
        motion.append(r(s, 0))

    # ---- staging: tools/analysis/stages.py (30-s epochs + Oura-app-style 5-min view) ----
    in_bed_min = (wake_u - bed_u) / 60
    if stg.get('ok'):
        mins = {k: stg['minutes'][k] for k in ('awake', 'rem', 'light', 'deep')}
        asleep = round(mins['rem'] + mins['light'] + mins['deep'], 1)
    else:
        mins, asleep = {'awake': None, 'rem': None, 'light': None, 'deep': None}, None
    t0s = stg.get('t0_unix', bed_u)
    rv = [v for v in rmssd if v is not None]
    # ---- extras ----
    sp = [x for ts, d in ev['spo2_r_pi_event'] if epoch(ts) is not None for x in d.get('r', []) if x and x > 0]
    spv = [spo2_pct(x) for x in sp]
    sp_bins = [[] for _ in range(n)]
    for ts, d in ev['spo2_r_pi_event']:
        k = epoch(ts)
        if k is not None:
            sp_bins[k].extend(spo2_pct(x) for x in d.get('r', []) if x and x > 0)
    spo2_series = [r(st.median(b), 1) if b else None for b in sp_bins]
    if spv:
        spo2 = {
            'status': 'estimated', 'label': 'estimated (Oura app "SpO2 Simple" calibration, gen4 coefficients; Ring 5 mapping unconfirmed)',
            'hw_coeffs': SPO2_HW, 'samples': len(spv),
            'median': r(st.median(spv)), 'mean': r(st.mean(spv)), 'low': r(min(spv)), 'p5': r(q(spv, 0.05)),
            'pct_below_90': r(100 * sum(v < 90 for v in spv) / len(spv)),
            'pct_below_88': r(100 * sum(v < 88 for v in spv) / len(spv)),
            'r_samples': len(sp), 'r_median': r(st.median(sp), 3),
        }
    else:
        spo2 = {'status': 'no data', 'r_samples': 0, 'r_median': None}
    met = []
    for ts, d in ev['activity_information']:
        met.append({'t': iso(unix(ts)), 'met': d.get('met', []), 'state': d.get('state')})
    dev = con.execute('SELECT firmware, api_version FROM device LIMIT 1').fetchone()
    ss = con.execute('SELECT MAX(last_sync_unix) FROM sync_state').fetchone()
    last_sync = ss[0] if ss and ss[0] else u1

    hrs_bed = [h for h in hr if h is not None]
    nadir_k = min((k for k in range(n) if hr[k] is not None), key=lambda k: hr[k])
    tv = [t for t in temp if t is not None]
    out = {
        'generated': iso(dt.datetime.now(TZ).timestamp()),
        'source': 'ringkeeper data/oura.db (read-only export)',
        'nights': len(sleeps),
        'tz': C.TZ_NAME,
        'epoch_min': 5,
        'night': {
            'bed': iso(bed_u), 'wake': iso(wake_u),
            'bed_unix': round(bed_u), 'wake_unix': round(wake_u),
            'time_in_bed_min': round(in_bed_min, 1),
            'duration_hours_ring': bp.get('duration_hours'),
        },
        'summary': {
            # resting HR = analysis hr_low (lowest 5.5-min rolling median while asleep), same as the note
            'hr_median': night.get('hr_median') if night.get('hr_median') is not None else r(st.median(hrs_bed)),
            'hr_low': night.get('hr_low') if night.get('hr_low') is not None else r(min(hrs_bed)),
            'hr_low_at': night.get('hr_low_at') or iso(bed_u + nadir_k * EPOCH_S + EPOCH_S / 2),
            'rmssd_avg': r(st.mean(rv)), 'rmssd_median': r(st.median(rv)), 'rmssd_max': max(rv), 'rmssd_samples': len(rv),
            'temp_avg_c': r(st.mean(tv), 2), 'temp_min_c': min(tv), 'temp_max_c': max(tv),
        },
        'estimated': {
            'label': stg.get('label'),
            'stage_codes': {'0': 'awake', '1': 'rem', '2': 'light', '3': 'deep'},
            'minutes': mins, 'asleep_min': asleep,
            'pct_of_sleep': stg.get('pct_of_sleep'),
            'efficiency_pct': night.get('efficiency_pct_est'),
            'latency_min': night.get('latency_min_est'),
            'waso_min': night.get('waso_min_est'),
            'method': 'tools/analysis/stages.py: actigraphy wake/sleep, then deep/REM/light from detrended HR, '
                      'slow HR SD, RMSSD and skin-temp slope with time-of-night priors (see tools/analysis/README.md).',
        },
        # stages.epochs5[k] covers t0 + 300*k (Oura-app 5-min view); epochs30 = 30-s digit string
        'stages': {
            'ok': bool(stg.get('ok')), 'label': stg.get('label'), 'reason': stg.get('reason'),
            't0_unix': stg.get('t0_unix'), 't0': iso(t0s),
            'epochs5': stg.get('epochs5') or [], 'epochs30': stg.get('epochs30') or '',
            'minutes': stg.get('minutes'), 'pct_of_sleep': stg.get('pct_of_sleep'),
            'checks_passed': stg.get('checks_passed'), 'checks_total': stg.get('checks_total'),
            'threshold_adjusted': stg.get('threshold_adjusted'),
        },
        'series': {
            'hr': hr, 'rmssd': rmssd, 'temp_c': temp, 'motion': motion,
            'motion_seconds': msec, 'spo2': spo2_series,
        },
        'nightly': [{k: x.get(k) for k in ('date', 'kind', 'bed', 'wake', 'bed_unix', 'wake_unix', 'time_in_bed_min',
                                            'sleep_min_est', 'efficiency_pct_est', 'latency_min_est', 'hr_median',
                                            'hr_low', 'rmssd_avg', 'temp_avg_c', 'movement_index_pct')}
                    | {'stage_minutes': (x.get('stages') or {}).get('minutes'),
                       'stage_pct': (x.get('stages') or {}).get('pct_of_sleep')}
                    for x in all_nights],
        'baselines': baselines_doc(all_nights),
        'sleep_note': sleep_note_doc(all_nights),
        'readiness': readiness_doc(all_nights, a.db),
        'ai_note': ai_note_doc(a.ai_note, night.get('date')),
        'cycle': cycle_doc(all_nights, a.cycle_log),
        'day': day_doc(a.db, a.nightly),
        'spo2': spo2,
        'activity_information': met,
        'device': {'firmware': (dev[0] if dev and dev[0] else FIRMWARE), 'api': dev[1] if dev else None,
                   'battery': C.latest_battery(a.db)},
        'last_sync': iso(last_sync),
        'event_counts': counts,
        'total_events': sum(counts.values()),
    }
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    tmp = a.out + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(out, f, separators=(',', ':'))
    os.replace(tmp, a.out)
    s = out['summary']
    print(f"wrote {a.out}: {out['night']['bed']} -> {out['night']['wake']}, {n} epochs; "
          f"HR med {s['hr_median']} low {s['hr_low']}; RMSSD avg {s['rmssd_avg']}; temp {s['temp_avg_c']} C; "
          f"stages {mins} (latency {out['estimated']['latency_min']} min, eff {out['estimated']['efficiency_pct']}%); "
          f"SpO2 med {spo2.get('median')} low {spo2.get('low')} <90% {spo2.get('pct_below_90')}% of samples")


if __name__ == '__main__':
    main()
