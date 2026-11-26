#!/usr/bin/env python3
"""Readiness score 0-100 -- OUR transparent formula, not Oura's.

Usage (from repo root):
    python3 -I tools/analysis/readiness.py [--nightly data/nightly.json] [--db PATH] [--need-h 8] [--json]
        -> prints the score + per-contributor breakdown, writes data/readiness.json

Inputs: data/nightly.json (tools/analysis/nights.py) and, optionally, the source DB
(data/source.json: pc -> data/oura.db, phone -> data/phone.db; opened read-only) for the
previous day's activity (`activity_information` MET bins).

Contributors (each scored 0-100, then a weighted mean over the contributors that have data;
weights are renormalised when one is missing):

  rhr        25  resting HR (`hr_low`). Baseline: 85 - 15*z  (z = MAD-z vs 14-night median;
                 lower than usual scores higher). Provisional: absolute bands (<=50 bpm 90 ...
                 >=80 bpm 30).
  hrv        25  RMSSD (`rmssd_avg`). Baseline: 85 + 15*z. Provisional: absolute bands
                 (>=60 ms 90 ... <=15 ms 30). Absolute HRV varies a lot between people, so this
                 is only a placeholder until the baseline exists.
  sleep      20  estimated sleep (`sleep_min_est`, else 0.9 x time in bed) vs need (default 8 h):
                 >= need -> 100, else 100 - 150 * (missing share).
  efficiency 10  estimated sleep efficiency: 100 - 3 * (90 - eff%), capped 0..100.
  temp       10  skin-temp deviation vs 14-night median: 100 - 150 * max(0, |dev| - 0.2 C).
                 Needs the baseline (no absolute fallback: absolute finger temp means little).
  activity   10  previous day's activity (awake MET minutes in the 16 h before bed, via day.py):
                 mean MET 1.3-2.2 -> 100; a very heavy day lowers it (-40 per MET above 2.2, or
                 > 3 h at >= 3 MET / > 1 h at >= 6 MET, floor 40); a very still day (mean < 1.3,
                 or a >= 6 h record with < 15 active min and < 3000 est. steps) -> 85. Detail shows
                 estimated steps + active minutes. Skipped when < 60 minutes were recorded.

Calibrating: until rhr and rmssd both have >= MIN_NIGHTS (5) prior nights, `status` is
"calibrating" and the score is `provisional` (absolute heuristics; temp has no input yet).
The UI must say so. Not medical advice.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import baselines as B  # noqa: E402
import common as C  # noqa: E402

READINESS_JSON = os.path.join(C.DATA_DIR, 'readiness.json')
NEED_H = 8.0
WEIGHTS = {'rhr': 25, 'hrv': 25, 'sleep': 20, 'efficiency': 10, 'temp': 10, 'activity': 10}
NAMES = {'rhr': 'Resting heart rate', 'hrv': 'HRV (RMSSD)', 'sleep': 'Sleep vs need',
         'efficiency': 'Sleep efficiency (est.)', 'temp': 'Skin temperature', 'activity': 'Previous day activity'}
ACT_WINDOW_S = 16 * 3600
ACT_MIN_BINS = 60
ACT_HEAVY_ACTIVE_MIN = 180   # > 3 h at >= 3 MET
ACT_HEAVY_HIGH_MIN = 60      # > 1 h at >= 6 MET
LABEL = 'our score, not Oura\'s'


def clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


def interp(x, pts):
    """Piecewise-linear through sorted (x, y) points, flat outside."""
    if x <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return pts[-1][1]


RHR_ABS = [(50, 90), (60, 75), (70, 55), (80, 30)]
HRV_ABS = [(15, 30), (20, 50), (40, 75), (60, 90)]


def score_rhr(m):
    if m is None or m.get('raw') is None:
        return None
    if m['status'] in ('ok', 'high', 'low') and m.get('z_mad') is not None:
        z = m['z_mad']
        return {'score': clamp(85 - 15 * z), 'basis': 'baseline',
                'detail': f"{m['raw']:.0f} bpm vs usual {m['median14']:.0f} (z {z:+.1f})"}
    return {'score': interp(m['raw'], RHR_ABS), 'basis': 'absolute',
            'detail': f"{m['raw']:.0f} bpm (absolute heuristic, no baseline yet)"}


def score_hrv(m):
    if m is None or m.get('raw') is None:
        return None
    if m['status'] in ('ok', 'high', 'low') and m.get('z_mad') is not None:
        z = m['z_mad']
        return {'score': clamp(85 + 15 * z), 'basis': 'baseline',
                'detail': f"{m['raw']:.0f} ms vs usual {m['median14']:.0f} (z {z:+.1f})"}
    return {'score': interp(m['raw'], HRV_ABS), 'basis': 'absolute',
            'detail': f"{m['raw']:.0f} ms (absolute heuristic, no baseline yet)"}


def score_sleep(night, need_h=NEED_H):
    mins, est = night.get('sleep_min_est'), 'estimated sleep'
    if mins is None and night.get('time_in_bed_min') is not None:
        mins, est = 0.9 * night['time_in_bed_min'], '0.9 x time in bed'
    if mins is None:
        return None
    need = need_h * 60
    miss = max(0.0, 1 - mins / need)
    h, m = divmod(int(round(mins)), 60)
    return {'score': clamp(100 - 150 * miss), 'basis': 'absolute',
            'detail': f'{h}h {m:02d}m {est} vs {need_h:g}h need'}


def score_eff(night):
    e = night.get('efficiency_pct_est')
    if e is None:
        return None
    return {'score': clamp(100 - 3 * (90 - e)), 'basis': 'absolute', 'detail': f'{e:.0f}% (estimated)'}


def score_temp(m):
    if m is None or m.get('value') is None or m['status'] not in ('ok', 'high', 'low'):
        return None
    d = m['value']
    return {'score': clamp(100 - 150 * max(0.0, abs(d) - 0.2)), 'basis': 'baseline',
            'detail': f'{d:+.2f} C vs your usual'}


def score_activity(act):
    """Previous-day activity sub-score. Uses mean MET; when day.py fields are present also active
    minutes (MET >= 3), vigorous minutes (>= 6) and estimated steps."""
    if not act or act.get('bins', 0) < ACT_MIN_BINS:
        return None
    mm = act['mean_met']
    s = 85 if mm < 1.3 else 100 if mm <= 2.2 else max(40.0, 100 - 40 * (mm - 2.2))
    am, hm, steps = act.get('active_min'), act.get('high_min'), act.get('steps_est')
    if am is not None:
        if am > ACT_HEAVY_ACTIVE_MIN or (hm or 0) > ACT_HEAVY_HIGH_MIN:   # a very heavy day
            s = min(s, max(40.0, 100 - 0.25 * max(am - ACT_HEAVY_ACTIVE_MIN, 0) - 0.5 * max((hm or 0) - ACT_HEAVY_HIGH_MIN, 0)))
        elif am < 15 and steps is not None and steps < 3000 and act.get('recorded_min', 0) >= 6 * 60:
            s = min(s, 85.0)                                                # a very still (full) day
    detail = f"mean {mm:.1f} MET over {act['bins']} recorded min, {act['high_pct']:.0f}% vigorous (>= 6 MET)"
    if am is not None:
        detail = (f"~{steps:,} steps (est.), {am} active min (>= 3 MET), " if steps is not None else f"{am} active min, ") + detail
    return {'score': float(s), 'basis': 'absolute', 'detail': detail}


def prev_day_activity(db_path, bed_unix, window_s=ACT_WINDOW_S, nights=None):
    """Awake MET minutes in [bed - window, bed) via day.py (steps est., active minutes). None if unavailable."""
    if not db_path or bed_unix is None or not os.path.exists(db_path):
        return None
    try:
        import day as DY
        ev, clock = DY.load(db_path)
        cx = DY.Ctx(ev, clock, nights or [])
    except Exception:
        return None
    return cx.activity_window(bed_unix - window_s, bed_unix)


def compute(nights, idx=None, need_h=NEED_H, activity=None):
    """Readiness for night `idx` (default latest kind=night). `activity` = prev_day_activity() dict."""
    ns = [x for x in nights if x.get('kind', 'night') == 'night']
    if not ns:
        return {'status': 'no_data', 'score': None, 'label': LABEL, 'contributors': []}
    if idx is None:
        idx = len(ns) - 1
    night = ns[idx]
    ev = B.evaluate(ns, idx)
    mt = ev['metrics']
    for m in mt.values():
        m['median14'] = ((m.get('windows') or {}).get('14') or {}).get('median')
    parts = {'rhr': score_rhr(mt.get('rhr')), 'hrv': score_hrv(mt.get('rmssd')),
             'sleep': score_sleep(night, need_h), 'efficiency': score_eff(night),
             'temp': score_temp(mt.get('temp_dev')), 'activity': score_activity(activity)}
    used = {k: v for k, v in parts.items() if v is not None}
    wsum = sum(WEIGHTS[k] for k in used)
    contributors = []
    for k, w in WEIGHTS.items():
        p = parts[k]
        c = {'key': k, 'name': NAMES[k], 'weight': w, 'available': p is not None}
        if p:
            c |= {'score': round(p['score']), 'basis': p['basis'], 'detail': p['detail'],
                  'share_pct': round(100 * w / wsum, 1),
                  'points': round(p['score'] * w / wsum, 1)}
        else:
            c['detail'] = {'temp': 'needs a 5-night baseline', 'activity': 'no activity data for the previous day'}.get(
                k, 'no data for this night')
        contributors.append(c)
    score = round(sum(p['score'] * WEIGHTS[k] for k, p in used.items()) / wsum) if wsum else None
    need = [m.get('need_more', 0) for k, m in mt.items() if k in ('rhr', 'rmssd') and m['status'] == 'insufficient']
    calibrating = bool(need) or any(parts[k] and parts[k]['basis'] == 'absolute' for k in ('rhr', 'hrv'))
    out = {
        'date': night.get('date'), 'score': score, 'label': LABEL,
        'status': 'calibrating' if calibrating else 'ok', 'provisional': calibrating,
        'need_more': max(need) if need else 0, 'min_nights': B.MIN_NIGHTS, 'need_h': need_h,
        'band': None if score is None else 'high' if score >= 85 else 'good' if score >= 70 else 'fair' if score >= 55 else 'low',
        'contributors': contributors,
        'note': ('Calibrating: provisional value from absolute rules of thumb until your own baseline '
                 f'exists ({max(need) if need else 0} more nights). ' if calibrating else '')
                + 'Our own open formula, not Oura\'s Readiness. Not medical advice.',
    }
    if activity is not None:
        out['activity_input'] = activity
    return out


def history(nights, need_h=NEED_H):
    ns = [x for x in nights if x.get('kind', 'night') == 'night']
    return [{'date': ns[i].get('date'), **{k: v for k, v in compute(ns, i, need_h).items()
                                            if k in ('score', 'status', 'provisional')}} for i in range(len(ns))]


def build(nights, db_path=None, need_h=NEED_H):
    ns = [x for x in nights if x.get('kind', 'night') == 'night']
    act = prev_day_activity(db_path, ns[-1].get('bed_unix'), nights=ns) if ns and db_path else None
    res = compute(ns, None, need_h, act)
    res['history'] = history(ns, need_h)
    res['generated'] = C.iso(time.time())
    res['data_source'] = C.source_config()['mode']
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description='readiness score (ours, not Oura\'s)')
    ap.add_argument('--nightly', default=C.NIGHTLY_JSON)
    ap.add_argument('--db', default=C.DEFAULT_DB)
    ap.add_argument('--out', default=READINESS_JSON)
    ap.add_argument('--need-h', type=float, default=NEED_H)
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args(argv)
    nights = (C.read_json(a.nightly, {}) or {}).get('nights', [])
    res = build(nights, a.db, a.need_h)
    if a.out:
        C.write_json_atomic(a.out, res)
    if a.json:
        print(json.dumps(res, indent=1))
    else:
        tag = ' (provisional, calibrating)' if res.get('provisional') else ''
        print(f"readiness {res.get('date')}: {res.get('score')}{tag} -- {LABEL}")
        for c in res['contributors']:
            print(f"  {c['name']:<26} " + (f"{c['score']:>3}  x{c['share_pct']:>4}%  {c['detail']}"
                                           if c['available'] else f"  -  {c['detail']}"))
    return 0


if __name__ == '__main__':
    sys.exit(main())
