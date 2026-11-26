#!/usr/bin/env python3
"""Heuristic sleep staging from ring signals — ESTIMATED, NOT VALIDATED.

Usage (from repo root):
    python3 -I tools/analysis/stages.py [--db data/oura.db] [--night -1]   # self-evaluation text

This is our own transparent rule-based stager (no Oura models). It has never been compared
against polysomnography or the Oura app; treat every number as a rough estimate.

Codes: 0 awake, 1 REM, 2 light, 3 deep (same as tools/export_dashboard.py).

Method (30-s epochs, see README.md for the rationale):
 1. Wake vs sleep (actigraphy, Cole-Kripke style): an epoch "moves" if accelerometer
    MAD (trimmed mean) > max(0.02 g, 4x the night's median), motion_event reports >=5 s,
    or finger angle jumps >10 deg. Wake = strong movement, or movement with a neighbour
    that also moves / with HR >=5 bpm over the 30-min local HR baseline. Sleep runs
    <1.5 min between wake runs become wake. Sleep onset = first 10 min with <=1 wake epoch;
    final wake = after the last 5 quiet min.
 2. NREM/REM/deep among sleep epochs, from 5-min physiology, robust-z-scored within the night:
      hr_d   = HR minus its 90-min rolling median (detrended; deep lower, REM higher)
      hr_sd  = SD of the 30-s HR values in +-2.5 min (REM: irregular surges; deep: very steady)
      lrmssd = log beat-derived RMSSD in +-2.5 min (deep: high vagal tone)
      tslope = 10-min skin-temp slope (distal warming while settling into deep sleep; small weight)
    deep_score = -1.0 hr_d - 0.8 hr_sd + 0.6 lrmssd + 0.2 tslope + prior_deep(t)
    rem_score  = +0.8 hr_d + 1.0 hr_sd - 0.4 lrmssd + prior_rem(t)
    prior_deep = 1.2*exp(-t/2h) - 0.4    (homeostatic sleep pressure: deep early)
    prior_rem  = -1.5 for t<60 min, else 0.6*min(1, t/6h) - 0.1   (REM grows later)
    Scores are smoothed over 3.5 min; epochs with movement in +-2.5 min cannot be deep.
    deep if deep_score>=1.0 (and > rem_score); REM if rem_score>=1.0; else light.
 3. Guard: if deep or REM lands outside a wide physiological band (deep 5-30%, REM 8-32% of
    sleep) the threshold is nudged until it is inside, and `threshold_adjusted` is set — so
    the sanity checks below are *not* fully independent when that happens.
 4. Bouts of deep/REM shorter than 3 min -> light. 5-min view uses the Oura app display rule
    (>=4 awake epochs of 10 -> awake, else mode, ties awake>deep>REM>light).

Sanity checks: deep 10-25% and REM 15-25% of sleep, >=60% of deep in the first half,
>=55% of REM in the second half, first REM >=45 min after onset, efficiency 70-98%,
HR/ACM coverage >=80%.
"""
import argparse
import collections
import math
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

WAKE, REM, LIGHT, DEEP = 0, 1, 2, 3
CODES = {'0': 'awake', '1': 'rem', '2': 'light', '3': 'deep'}
LETTER = {WAKE: 'W', REM: 'R', LIGHT: 'L', DEEP: 'D'}
LABEL = 'estimated, not validated (ringkeeper heuristic, not Oura)'
EPOCH = 30
THETA = 1.0
DEEP_BAND, REM_BAND = (5.0, 30.0), (8.0, 32.0)
MIN_EPOCHS = 120  # 1 h


def _moving(sig):
    n = sig['n']
    mads = [m for m in sig['mad'] if m is not None]
    med = st.median(mads) if mads else 0.0
    thr = max(0.02, 4 * med)
    mov, strong = [False] * n, [False] * n
    for k in range(n):
        m, ms = sig['mad'][k], sig['motion_s'][k]
        a0, a1 = sig['angle'][k], sig['angle'][k - 1] if k else None
        jump = a0 is not None and a1 is not None and abs(a0 - a1) > 10
        mov[k] = (m is not None and m > thr) or ms >= 5 or jump
        strong[k] = (m is not None and m > 5 * thr) or ms >= 15
    return mov, strong, thr


def _runs(seq):
    out, s = [], 0
    for k in range(1, len(seq) + 1):
        if k == len(seq) or seq[k] != seq[s]:
            out.append((s, k, seq[s]))
            s = k
    return out


def wake_sleep(sig):
    n = sig['n']
    mov, strong, thr = _moving(sig)
    hr = sig['hr']
    hr_base = C.rolling(hr, 30, st.median)  # 30-min centred median
    wake = [False] * n
    for k in range(n):
        if strong[k]:
            wake[k] = True
        elif mov[k]:
            nb = sum(mov[j] for j in range(max(0, k - 2), min(n, k + 3)) if j != k)
            hr_up = hr[k] is not None and hr_base[k] is not None and hr[k] - hr_base[k] >= 5
            wake[k] = nb >= 1 or hr_up
        elif hr[k] is None and sig['mad'][k] is None:
            wake[k] = True  # no data at all: ring likely off / not worn
    # short sleep runs (<3 epochs) between wake runs -> wake
    for s, e, v in _runs(wake):
        if not v and e - s < 3 and s > 0 and e < n:
            for j in range(s, e):
                wake[j] = True
    onset = next((k for k in range(n) if sum(wake[k:k + 20]) <= 1 and not wake[k]), n)
    final = next((k + 1 for k in range(n - 1, onset - 1, -1)
                  if not wake[k] and sum(wake[max(onset, k - 9):k + 1]) <= 1), onset)
    for k in range(onset):
        wake[k] = True
    for k in range(final, n):
        wake[k] = True
    return wake, onset, final, mov, thr


def _scores(sig, wake, onset, mov):
    n = sig['n']
    hr = C.rolling(sig['hr'], 5, st.median)
    trend = C.rolling(hr, 90, st.median)
    hr_d = [None if h is None or t is None else h - t for h, t in zip(hr, trend)]
    lr = [math.log(r) if r else None for r in sig['rmssd5']]
    tmp = C.rolling(sig['temp'], 4, C.mean)
    tsl = [None if k < 10 or k + 10 >= n or tmp[k + 10] is None or tmp[k - 10] is None
           else tmp[k + 10] - tmp[k - 10] for k in range(n)]
    sl = [not w for w in wake]

    def zs(xs):
        z = C.robust_z([x if s else None for x, s in zip(xs, sl)])
        return [0.0 if v is None else max(-3.0, min(3.0, v)) for v in z]
    z_hr, z_sd, z_lr, z_ts = zs(hr_d), zs(sig['hr_sd5']), zs(lr), zs(tsl)
    deep, rem = [], []
    for k in range(n):
        t_h = max(0.0, (k - onset) * EPOCH / 3600.0)
        p_deep = 1.2 * math.exp(-t_h / 2.0) - 0.4
        p_rem = -1.5 if t_h < 1.0 else 0.6 * min(1.0, t_h / 6.0) - 0.1
        deep.append(-1.0 * z_hr[k] - 0.8 * z_sd[k] + 0.6 * z_lr[k] + 0.2 * z_ts[k] + p_deep)
        rem.append(0.8 * z_hr[k] + 1.0 * z_sd[k] - 0.4 * z_lr[k] + p_rem)
    deep = C.rolling(deep, 3, C.mean)
    rem = C.rolling(rem, 3, C.mean)
    still5 = [not any(mov[max(0, k - 5):k + 6]) for k in range(n)]
    return deep, rem, still5


def _assign(wake, deep, rem, still5, th_d, th_r):
    out = []
    for k, w in enumerate(wake):
        if w:
            out.append(WAKE)
        elif still5[k] and deep[k] >= th_d and deep[k] > rem[k]:
            out.append(DEEP)
        elif rem[k] >= th_r:
            out.append(REM)
        else:
            out.append(LIGHT)
    for s, e, v in _runs(out):  # min bout 3 min for deep/REM
        if v in (DEEP, REM) and e - s < 6:
            for j in range(s, e):
                out[j] = LIGHT
    return out


def _pct(stg, code):
    sl = sum(1 for s in stg if s != WAKE)
    return 100.0 * sum(1 for s in stg if s == code) / sl if sl else 0.0


def aggregate_5min(ep30):
    """Oura app display rule: 10 epochs; >=4 awake -> awake; else mode, ties W>D>R>L;
    a trailing window shorter than 5 epochs is dropped."""
    pri = {WAKE: 4, DEEP: 3, REM: 2, LIGHT: 1}
    out = []
    for i in range(0, len(ep30), 10):
        w = ep30[i:i + 10]
        if len(w) < 5:
            break
        c = collections.Counter(w)
        if c[WAKE] >= 4:
            out.append(WAKE)
        else:
            out.append(max(c, key=lambda s: (c[s], pri[s])))
    return out


def checks(res, sig):
    ep = res['epochs30']
    on, fin = res['onset'], res['final']
    mid = on + (fin - on) // 2
    d_tot = sum(1 for s in ep if s == DEEP)
    r_tot = sum(1 for s in ep if s == REM)
    d_first = sum(1 for s in ep[:mid] if s == DEEP)
    r_second = sum(1 for s in ep[mid:] if s == REM)
    first_rem = next((k for k in range(on, len(ep)) if ep[k] == REM), None)
    n = sig['n']
    out = []

    def add(name, val, lo, hi, unit=''):
        ok = val is not None and lo <= val <= hi
        out.append({'check': name, 'value': C.rnd(val), 'expected': f'{lo}-{hi}{unit}', 'pass': ok})
    add('deep % of sleep', res['pct_of_sleep']['deep'], 10, 25, '%')
    add('REM % of sleep', res['pct_of_sleep']['rem'], 15, 25, '%')
    add('deep in first half', 100.0 * d_first / d_tot if d_tot else None, 60, 100, '%')
    add('REM in second half', 100.0 * r_second / r_tot if r_tot else None, 55, 100, '%')
    add('first REM after onset', (first_rem - on) * EPOCH / 60.0 if first_rem is not None else None, 45, 240, ' min')
    add('sleep efficiency', res['efficiency_pct'], 70, 98, '%')
    add('HR coverage', 100.0 * sum(h is not None for h in sig['hr']) / n, 80, 100, '%')
    add('ACM coverage', 100.0 * sum(m is not None for m in sig['mad']) / n, 80, 100, '%')
    return out


def stage_night(sig):
    n = sig['n']
    if n < MIN_EPOCHS or sum(h is not None for h in sig['hr']) < 0.3 * n:
        return {'ok': False, 'reason': 'too short or too little HR data', 'label': LABEL}
    wake, onset, final, mov, thr = wake_sleep(sig)
    if final - onset < MIN_EPOCHS // 2:
        return {'ok': False, 'reason': 'no sustained sleep detected', 'label': LABEL}
    deep, rem, still5 = _scores(sig, wake, onset, mov)
    th_d = th_r = THETA
    adjusted = False
    ep = _assign(wake, deep, rem, still5, th_d, th_r)
    for _ in range(40):  # guard: keep within wide physiological bands
        pd, pr = _pct(ep, DEEP), _pct(ep, REM)
        nd = th_d - 0.1 if pd < DEEP_BAND[0] else th_d + 0.1 if pd > DEEP_BAND[1] else th_d
        nr = th_r - 0.1 if pr < REM_BAND[0] else th_r + 0.1 if pr > REM_BAND[1] else th_r
        if (nd, nr) == (th_d, th_r):
            break
        th_d, th_r, adjusted = nd, nr, True
        ep = _assign(wake, deep, rem, still5, th_d, th_r)
    cnt = collections.Counter(ep)
    mins = {CODES[str(c)]: cnt[c] * EPOCH / 60.0 for c in (WAKE, REM, LIGHT, DEEP)}
    asleep = n - cnt[WAKE]
    tib = (sig['wake_u'] - sig['bed_u']) / 60.0
    waso = sum(1 for k in range(onset, final) if ep[k] == WAKE) * EPOCH / 60.0
    res = {
        'ok': True, 'label': LABEL,
        'epochs30': ep, 'epochs30_str': ''.join(str(s) for s in ep),
        'epochs5': aggregate_5min(ep),
        'onset': onset, 'final': final,
        'minutes': mins,
        'asleep_min': asleep * EPOCH / 60.0,
        'latency_min': onset * EPOCH / 60.0,
        'waso_min': waso,
        'efficiency_pct': C.rnd(100.0 * asleep * EPOCH / 60.0 / tib) if tib else None,
        'pct_of_sleep': {k: C.rnd(_pct(ep, c)) for k, c in (('rem', REM), ('light', LIGHT), ('deep', DEEP))},
        'thresholds': {'deep': round(th_d, 2), 'rem': round(th_r, 2), 'move_g': round(thr, 4)},
        'threshold_adjusted': adjusted,
    }
    res['checks'] = checks(res, sig)
    res['checks_passed'] = sum(c['pass'] for c in res['checks'])
    res['checks_total'] = len(res['checks'])
    return res


def hm(minutes):
    m = int(round(minutes or 0))
    return f'{m // 60}h{m % 60:02d}m'


def self_eval_text(m, res):
    """Plot-free text summary of one night's staging."""
    lines = [f"Night {m['date']}: {m['bed']} -> {m['wake']} ({hm(m['time_in_bed_min'])} in bed)",
             f'Staging: {LABEL}']
    if not res.get('ok'):
        return '\n'.join(lines + [f"no staging: {res.get('reason')}"])
    mn, p = res['minutes'], res['pct_of_sleep']
    lines.append(f"asleep {hm(res['asleep_min'])} (eff {res['efficiency_pct']}%, latency {res['latency_min']:.0f} min, "
                 f"WASO {res['waso_min']:.0f} min)")
    lines.append(f"deep {hm(mn['deep'])} ({p['deep']}%) | REM {hm(mn['rem'])} ({p['rem']}%) | "
                 f"light {hm(mn['light'])} ({p['light']}%) | awake {hm(mn['awake'])}")
    lines.append(f"thresholds {res['thresholds']} adjusted={res['threshold_adjusted']}")
    lines.append('hypnogram (5-min, W/R/L/D, one char = 5 min, | = each hour):')
    s5 = ''.join(LETTER[s] for s in res['epochs5'])
    lines.append('  ' + '|'.join(s5[i:i + 12] for i in range(0, len(s5), 12)))
    lines.append(f"sanity checks {res['checks_passed']}/{res['checks_total']}:")
    for c in res['checks']:
        lines.append(f"  [{'ok' if c['pass'] else '!!'}] {c['check']}: {c['value']} (expected {c['expected']})")
    return '\n'.join(lines)


def main(argv=None):
    import nights as N
    ap = argparse.ArgumentParser(description='self-evaluation of heuristic staging')
    ap.add_argument('--db', default=C.DEFAULT_DB)
    ap.add_argument('--night', type=int, default=-1, help='index into nights (default: last)')
    a = ap.parse_args(argv)
    res, _ = N.analyse(a.db)
    if not res:
        print('no bedtime_period events in DB')
        return 1
    m, sig, stg = res[a.night]
    print(self_eval_text(m, stg))
    return 0


if __name__ == '__main__':
    sys.exit(main())
