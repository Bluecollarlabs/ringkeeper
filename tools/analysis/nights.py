#!/usr/bin/env python3
"""Segment nights from ring `bedtime_period` events and compute per-night metrics.

Usage (from repo root):
    python3 -I tools/analysis/nights.py [--db data/oura.db] [--out data/nightly.json] [--quiet]

Each `bedtime_period` (0x76) defines one sleep window [bedtime_start_ds, bedtime_end_ds]
in ring deciseconds; a later event with the same start replaces an earlier one (as the
official app does). Ring time -> unix via `time_sync` anchors (common.RingClock).

Inside each window we build a 30-s epoch grid aligned to floor30(bed) and extract:
  acm   sleep_acm_period (one 30-s window per event; fields per open_health docs:
        [angle_tmean, angle_max, angle_iqr, mad_tmean, mad_max, mad_iqr] — MAD in g)
  mot   motion_event motion_seconds (30-s windows)
  hr    ibi_and_amplitude_event beats (amplitude>0, 300..2000 ms, local-median outlier
        filter); beat k stamped at event_time - sum(ibi[k:]) (start of its interval)
  hrv   beat-derived 5-min RMSSD and slow HR SD (of 30-s HR) around every epoch, plus the ring's own
        hrv_event 5-min HR/RMSSD samples (used for nightly RMSSD summary)
  temp  sleep_temp_event samples (30 s apart, last one at event time)

Output data/nightly.json (gitignored, contains personal health data). Existing nights
not present in the DB any more are kept (history survives DB pruning).
"""
import argparse
import bisect
import math
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402
import stages as S  # noqa: E402

EPOCH = 30
MIN_NIGHT_HOURS = 3.0          # shorter bedtime periods are labelled "nap"
MOVE_G = 0.02                  # mad_tmean above this (g) = a "movement" epoch (fixed, comparable across nights)
POS_CHANGE_DEG = 10.0          # |delta angle_tmean| between epochs counted as a position change
SCHEMA = 1


# ---------------------------------------------------------------- segmentation
def bedtimes(ev):
    by_start = {}
    for ts, d in ev.get('bedtime_period', []):
        s, e = d.get('bedtime_start_ds'), d.get('bedtime_end_ds')
        if s is None or e is None or e <= s:
            continue
        by_start[s] = max(e, by_start.get(s, e))  # later/longer event replaces
    return [{'start_ds': s, 'end_ds': e} for s, e in sorted(by_start.items())]


# ---------------------------------------------------------------- signals
def _beats(ev, clock, lo_u, hi_u):
    """Return list of beats (t_unix, ibi_ms, valid) in stream order within [lo_u, hi_u]."""
    raw = []
    last_t = -math.inf
    for ts, d in ev.get('ibi_and_amplitude_event', []):
        T = clock.unix(ts)
        if T < lo_u - 30 or T > hi_u + 30:
            continue
        ibis, amps = d.get('ibi_ms', []), d.get('amplitude', [])
        tail = 0.0
        stamps = []
        for i in range(len(ibis) - 1, -1, -1):
            tail += ibis[i] / 1000.0
            stamps.append(T - tail)
        stamps.reverse()
        for t, ibi, amp in zip(stamps, ibis, amps + [0] * (len(ibis) - len(amps))):
            t = max(t, last_t)  # clamp non-decreasing
            last_t = t
            raw.append([t, ibi, bool(amp > 0 and 300 <= ibi <= 2000)])
    # local-median outlier filter over +-5 valid beats
    vidx = [i for i, b in enumerate(raw) if b[2]]
    vals = [raw[i][1] for i in vidx]
    for j, i in enumerate(vidx):
        w = vals[max(0, j - 5):j + 6]
        m = st.median(w)
        if abs(raw[i][1] - m) > 0.25 * m:
            raw[i][2] = False
    return [tuple(b) for b in raw if lo_u <= b[0] <= hi_u]


def build_signals(ev, clock, start_ds, end_ds):
    bed_u, wake_u = clock.unix(start_ds), clock.unix(end_ds)
    t0 = math.floor(bed_u / EPOCH) * EPOCH
    n = max(1, math.ceil((wake_u - t0) / EPOCH))

    def idx(u):
        k = int((u - t0) // EPOCH)
        return k if 0 <= k < n else None

    sig = {
        'bed_u': bed_u, 'wake_u': wake_u, 't0': t0, 'n': n, 'epoch_s': EPOCH,
        'mad': [None] * n, 'mad_max': [None] * n, 'angle': [None] * n,
        'motion_s': [0] * n, 'hr': [None] * n, 'hr_sd5': [None] * n,
        'rmssd5': [None] * n, 'temp': [None] * n, 'hrv5': [],
    }
    for ts, d in ev.get('sleep_acm_period', []):
        a = d.get('acm_mad') or []
        k = idx(clock.unix(ts) - EPOCH / 2)
        if k is not None and len(a) >= 5:
            sig['angle'][k], sig['mad'][k], sig['mad_max'][k] = a[0], a[3], a[4]
    for ts, d in ev.get('motion_event', []):
        k = idx(clock.unix(ts) - EPOCH / 2)
        if k is not None:
            sig['motion_s'][k] += d.get('motion_seconds', 0) or 0

    beats = _beats(ev, clock, t0 - 180, t0 + n * EPOCH + 180)
    per = [[] for _ in range(n)]
    for t, ibi, ok in beats:
        k = idx(t)
        if ok and k is not None:
            per[k].append(ibi)
    for k, b in enumerate(per):
        if len(b) >= 8:
            sig['hr'][k] = 60000.0 / st.median(b)
    # slow HR fluctuation: SD of the 30-s HR medians in +-2.5 min (not beat-to-beat, so
    # respiratory sinus arrhythmia does not leak in; that part is captured by RMSSD)
    for k in range(n):
        w = [h for h in sig['hr'][max(0, k - 5):k + 6] if h is not None]
        if len(w) >= 6:
            sig['hr_sd5'][k] = st.pstdev(w)
    # 5-min RMSSD (+-150 s around each epoch centre) from successive valid beats
    pairs = []  # (t, diff^2) for consecutive valid beats
    for (t1, i1, ok1), (t2, i2, ok2) in zip(beats, beats[1:]):
        if ok1 and ok2 and 0 < t2 - t1 <= 2.5:
            pairs.append((t2, (i2 - i1) ** 2))
    pt = [p[0] for p in pairs]
    for k in range(n):
        c = t0 + k * EPOCH + EPOCH / 2
        a, b = bisect.bisect_left(pt, c - 150), bisect.bisect_right(pt, c + 150)
        if b - a >= 30:
            sig['rmssd5'][k] = math.sqrt(sum(p[1] for p in pairs[a:b]) / (b - a))
    for ts, d in ev.get('sleep_temp_event', []):
        vs = d.get('temps_c', [])
        T = clock.unix(ts)
        for i, v in enumerate(vs):
            k = idx(T - (len(vs) - 1 - i) * 30)
            if k is not None and 30 < v < 40:
                sig['temp'][k] = v
    for ts, d in ev.get('hrv_event', []):
        step = (d.get('interval_min') or 5) * 60
        hs, rs = d.get('hr_bpm', []), d.get('rmssd_ms', [])
        T = clock.unix(ts)
        for i, rv in enumerate(rs):
            u = T - (len(rs) - 1 - i) * step - step / 2
            if bed_u <= u <= wake_u:
                sig['hrv5'].append({'u': u, 'hr': hs[i] if i < len(hs) and hs[i] > 0 else None,
                                    'rmssd': rv if rv > 0 else None})
    return sig


# ---------------------------------------------------------------- metrics
def night_metrics(sig, stg):
    n, t0 = sig['n'], sig['t0']
    bed_u, wake_u = sig['bed_u'], sig['wake_u']
    tib = (wake_u - bed_u) / 60.0
    asleep = [s != S.WAKE for s in stg['epochs30']] if stg.get('ok') else [True] * n
    hr_sleep = [h for h, a in zip(sig['hr'], asleep) if a and h is not None]
    hr5 = C.rolling(sig['hr'], 5, st.median)  # 5.5-min rolling median
    lows = [(h, k) for k, (h, a) in enumerate(zip(hr5, asleep)) if a and h is not None]
    low = min(lows) if lows else (None, None)
    rv = [x['rmssd'] for x in sig['hrv5'] if x['rmssd']]
    rv_beats = [r for r, a in zip(sig['rmssd5'], asleep) if a and r is not None]
    temps = [t for t in sig['temp'] if t is not None]
    mads = sig['mad']
    have_mad = [m for m in mads if m is not None]
    moving = [m is not None and m > MOVE_G for m in mads]
    bouts = sum(1 for k in range(n) if moving[k] and (k == 0 or not moving[k - 1]))
    angs = sig['angle']
    pos = sum(1 for k in range(1, n) if angs[k] is not None and angs[k - 1] is not None
              and abs(angs[k] - angs[k - 1]) > POS_CHANGE_DEG)
    hours = max(tib / 60.0, 1e-9)
    st_min = stg.get('minutes', {})
    return {
        'date': C.local_date(wake_u),
        'kind': 'night' if tib / 60.0 >= MIN_NIGHT_HOURS else 'nap',
        'bed': C.iso(bed_u), 'wake': C.iso(wake_u),
        'bed_unix': round(bed_u), 'wake_unix': round(wake_u),
        'time_in_bed_min': C.rnd(tib),
        'sleep_min_est': stg.get('asleep_min'),
        'latency_min_est': stg.get('latency_min'),
        'waso_min_est': stg.get('waso_min'),
        'efficiency_pct_est': stg.get('efficiency_pct'),
        'hr_median': C.rnd(C.median(hr_sleep)),
        'hr_low': C.rnd(low[0]),
        'hr_low_at': C.iso(t0 + low[1] * EPOCH + EPOCH / 2) if low[1] is not None else None,
        'rmssd_avg': C.rnd(C.mean(rv)), 'rmssd_median': C.rnd(C.median(rv)), 'rmssd_samples': len(rv),
        'rmssd_beats_median': C.rnd(C.median(rv_beats)),
        'temp_avg_c': C.rnd(C.mean(temps), 2), 'temp_min_c': C.rnd(min(temps), 2) if temps else None,
        'temp_max_c': C.rnd(max(temps), 2) if temps else None,
        'movement_index_pct': C.rnd(100.0 * sum(moving) / max(1, len(have_mad))) if have_mad else None,
        'movement_bouts_per_h': C.rnd(bouts / hours),
        'position_changes': pos,
        'coverage_pct': {
            'hr': C.rnd(100.0 * sum(h is not None for h in sig['hr']) / n),
            'acm': C.rnd(100.0 * len(have_mad) / n),
            'temp': C.rnd(100.0 * sum(t is not None for t in sig['temp']) / n),
        },
        'stages': {
            'label': S.LABEL,
            'ok': stg.get('ok', False),
            'minutes': st_min,
            'pct_of_sleep': stg.get('pct_of_sleep'),
            'epochs30': stg.get('epochs30_str'),
            'epochs5': stg.get('epochs5'),
            'epoch_codes': S.CODES,
            't0_unix': round(t0),
            'checks_passed': stg.get('checks_passed'),
            'checks_total': stg.get('checks_total'),
            'threshold_adjusted': stg.get('threshold_adjusted'),
            'reason': stg.get('reason'),
        },
    }


def analyse(db_path=C.DEFAULT_DB):
    con = C.open_ro(db_path)
    try:
        ev = C.load_events(con)
    finally:
        con.close()
    clock = C.RingClock.from_events(ev)
    out = []
    for bt in bedtimes(ev):
        sig = build_signals(ev, clock, bt['start_ds'], bt['end_ds'])
        stg = S.stage_night(sig)
        m = night_metrics(sig, stg)
        m['bedtime_start_ds'], m['bedtime_end_ds'] = bt['start_ds'], bt['end_ds']
        out.append((m, sig, stg))
    return out, clock


def merge(existing, new_nights):
    """Merge by bed_unix (rounded to the minute); new values win."""
    key = lambda x: round(x['bed_unix'] / 60)  # noqa: E731
    by = {key(x): x for x in (existing or [])}
    for x in new_nights:
        by[key(x)] = x
    return sorted(by.values(), key=lambda x: x['bed_unix'])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--db', default=C.DEFAULT_DB)
    ap.add_argument('--out', default=C.NIGHTLY_JSON)
    ap.add_argument('--quiet', action='store_true')
    a = ap.parse_args(argv)
    res, clock = analyse(a.db)
    old = C.read_json(a.out, {}) or {}
    nights = merge(old.get('nights'), [m for m, _, _ in res])
    doc = {
        'schema': SCHEMA,
        'generated': C.iso(__import__('time').time()),
        'tz': C.TZ_NAME,
        'source': 'ringkeeper tools/analysis/nights.py (read-only DB)',
        'note': 'personal health data - do not commit. Stage fields are ' + S.LABEL,
        'clock_warnings': clock.warnings,
        'nights': nights,
    }
    C.write_json_atomic(a.out, doc)
    if not a.quiet:
        for m in nights:
            s = m['stages']
            print(f"{m['date']} {m['kind']}: {m['bed']} -> {m['wake']} TIB {m['time_in_bed_min']} min, "
                  f"sleep~{m['sleep_min_est']} min, HR med {m['hr_median']} low {m['hr_low']}, "
                  f"RMSSD avg {m['rmssd_avg']}, temp {m['temp_avg_c']} C, move {m['movement_index_pct']}% | "
                  f"stages {s['minutes']} checks {s['checks_passed']}/{s['checks_total']}")
        print(f'wrote {a.out} ({len(nights)} nights)')
    return doc


if __name__ == '__main__':
    main()
