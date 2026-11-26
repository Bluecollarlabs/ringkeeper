#!/usr/bin/env python3
"""Menstrual cycle awareness from nightly skin temperature + a period log (stdlib only).

=====================================================================================
  AWARENESS ONLY -- NOT CONTRACEPTION.
  This is a DIY, unvalidated estimate. It must never be used to decide whether
  unprotected sex is "safe". Temperature-based birth control needs a validated,
  regulated algorithm (e.g. Natural Cycles, FDA-cleared). Hormonal birth control
  (pill, patch, vaginal ring, implant, injection, hormonal IUD) usually suppresses
  ovulation and therefore the temperature shift -- the phases shown here then mean
  little. Every output carries a `disclaimer` field; UIs and AI answers must show it.
=====================================================================================

Usage (from repo root):
    python3 -I tools/analysis/cycle.py                 # text summary
    python3 -I tools/analysis/cycle.py --json          # full JSON on stdout (+ data/cycle.json)
    python3 -I tools/analysis/cycle.py --log-start 2026-11-20 [--flow medium] [--symptoms cramps,fatigue]
    python3 -I tools/analysis/cycle.py --log-end 2026-11-24
    python3 -I tools/analysis/cycle.py --today 2026-12-01   # pretend "today" (testing)

Inputs
  data/nightly.json   (tools/analysis/nights.py) -- uses `date` (local wake date), `kind`,
                      `temp_avg_c`, `coverage_pct.temp`, `time_in_bed_min`.
  data/cycle_log.json (gitignored, chmod 600) -- either a list
                          [{"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"?, "flow": str?, "symptoms": [str]?}, ...]
                      or {"periods": [...same...], "hormonal_bc": bool?}.
                      `start` = first day of bleeding (cycle day 1). `end` optional.

Method
  1. Nightly value = `temp_avg_c` of the main sleep (kind == night) for each wake date.
     Excluded: temp coverage < 50 %, time in bed < 3 h, or an isolated spike more than
     0.45 °C from the median of the 3+3 neighbouring nights (fever, late night, ...).
  2. Deviation = value - rolling baseline (median of up to 28 previous valid nights; with
     fewer than 3 prior nights the median of the first <= 7 nights). Display only: the
     shift detectors below work on the raw values inside each cycle.
  3. Cycles: from each logged period start to the day before the next one (last = open).
  4. Shift detection per cycle (cycle days >= 4, valid nights only):
     a. "3-over-6": the first night k whose value and the next 2 valid nights are all above
        the coverline = max of the 6 previous valid nights (those 6 must span <= 10 days;
        the 3 highs <= 4 days, i.e. one missed night tolerated), AND
        mean(3 highs) - mean(6 lows) >= MIN_RISE (0.2 °C).
        Confirmed on the 3rd high night. Estimated ovulation = the day before night k.
        Sustained check: if >= 3 later valid nights exist, their median (next <= 5) must
        stay >= mean(lows) + 0.75*MIN_RISE, otherwise the candidate is rejected as a noise
        run; with < 3 later nights the shift is flagged `provisional`.
        Synthetic sweep (anovulatory 30-d cycle, 200 seeds): false shifts 0 % at 0.08 °C
        night-to-night noise; see README for higher-noise numbers. Real-world noise of
        finger skin temp is not yet measured.
     b. "smoothed" (level shift): 3-night centred median, then the split point k that
        maximises mean(after, <= 10 nights) - mean(before, <= 10 nights) with >= 3 nights
        each side; accepted if the step >= MIN_RISE and >= 80 % of the after-nights sit
        above the midpoint. Retrospective, more robust to one bad night, less precise.
     Primary estimate = (a) if found, else (b); if neither, calendar estimate
     (predicted cycle length - mean luteal length, default 14 d) labelled as such.
  5. Prediction: next period = last start + mean length of the last <= 6 complete cycles
     (lengths outside 15-60 d ignored). No complete cycle -> 28 d default, confidence low.
     Window = +/- max(2, SD) days.
  6. Fertile window (ESTIMATE) = est. ovulation - 5 .. + 1 day.

Sparse data: `status` + `messages` + `need` explain what is missing
("log your period", "need N more nights").
"""
import argparse
import datetime as dt
import json
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

CYCLE_LOG = os.path.join(C.DATA_DIR, 'cycle_log.json')
CYCLE_JSON = os.path.join(C.DATA_DIR, 'cycle.json')

DISCLAIMER = ('Awareness only - NOT birth control. These are unvalidated estimates from skin '
              'temperature and your period log; never use them to decide whether unprotected sex '
              'is safe. Hormonal birth control usually suppresses ovulation and the temperature '
              'shift, so phases shown here may not apply.')

MIN_RISE = 0.20          # °C, minimum mean rise for a shift
LOW_N, HIGH_N = 6, 3     # 3-over-6
LOW_SPAN, HIGH_SPAN = 10, 4
MIN_SEARCH_CD = 4
SUSTAIN_N, SUSTAIN_FRAC = 5, 0.75   # median of next <= 5 nights must stay >= lows + 0.75*MIN_RISE        # don't call a shift before cycle day 4
SPIKE_C = 0.45
BASE_WIN = 28
DEFAULT_LEN = 28
DEFAULT_PERIOD = 5
DEFAULT_LUTEAL = 14
MAX_CYCLES = 6
VALID_LEN = (15, 60)
FERTILE_BEFORE, FERTILE_AFTER = 5, 1


def D(s):
    return s if isinstance(s, dt.date) else dt.date.fromisoformat(str(s)[:10])


def iso(d):
    return None if d is None else d.isoformat()


def r2(x):
    return None if x is None else round(x, 2)


# ---------------------------------------------------------------- inputs

def load_log(path=CYCLE_LOG):
    """Return (periods, settings). Accepts a list or {"periods": [...], ...}."""
    raw = C.read_json(path, None)
    if raw is None:
        return [], {}
    if isinstance(raw, dict):
        periods, settings = raw.get('periods') or [], {k: v for k, v in raw.items() if k != 'periods'}
    else:
        periods, settings = raw, {}
    return normalize_periods(periods), settings


def normalize_periods(periods):
    out = {}
    for p in periods or []:
        try:
            s = D(p['start'])
        except (KeyError, TypeError, ValueError):
            continue
        e = None
        if p.get('end'):
            try:
                e = D(p['end'])
            except ValueError:
                e = None
            if e is not None and e < s:
                e = None
        out[s] = {'start': s, 'end': e, 'flow': p.get('flow'), 'symptoms': list(p.get('symptoms') or [])}
    return [out[k] for k in sorted(out)]


def nightly_temps(nights):
    """{date: (value_c or None, excluded_reason or None)} for main-sleep nights."""
    raw = {}
    for n in nights or []:
        if n.get('kind', 'night') != 'night' or not n.get('date'):
            continue
        v = n.get('temp_avg_c')
        cov = (n.get('coverage_pct') or {}).get('temp')
        tib = n.get('time_in_bed_min')
        reason = None
        if v is None:
            reason = 'no temperature'
        elif cov is not None and cov < 50:
            reason = 'low sensor coverage'
        elif tib is not None and tib < 180:
            reason = 'short night'
        # later entries for the same date (re-analysis) win
        raw[D(n['date'])] = (v, reason)
    # isolated-spike filter
    days = sorted(raw)
    good = [d for d in days if raw[d][1] is None]
    vals = {d: raw[d][0] for d in good}
    for i, d in enumerate(good):
        nb = [vals[x] for x in good[max(0, i - 3):i] + good[i + 1:i + 4]]
        if len(nb) >= 4 and abs(vals[d] - st.median(nb)) > SPIKE_C:
            raw[d] = (raw[d][0], 'outlier (fever/late night?)')
    return raw


def with_deviation(temps):
    """List of dicts sorted by date with rolling-median baseline + deviation."""
    days = sorted(temps)
    valid = [(d, temps[d][0]) for d in days if temps[d][1] is None]
    first = [v for _, v in valid[:7]]
    out = []
    for d in days:
        v, why = temps[d]
        prior = [x for dd, x in valid if dd < d][-BASE_WIN:]
        if len(prior) >= 3:
            base, bq = st.median(prior), 'rolling'
        elif first:
            base, bq = st.median(first), 'provisional'
        else:
            base, bq = None, None
        out.append({'date': d, 'temp_c': v, 'excluded': why,
                    'baseline_c': base, 'baseline': bq,
                    'dev_c': None if (v is None or base is None) else v - base})
    return out


# ---------------------------------------------------------------- shift detection

def shift_3over6(series, min_rise=MIN_RISE):
    """series: list of (cycle_day, value) with valid values, sorted by cd. Returns dict or None."""
    pts = [(cd, v) for cd, v in series if v is not None]
    for i in range(LOW_N, len(pts) - HIGH_N + 1):
        cd_k = pts[i][0]
        if cd_k < MIN_SEARCH_CD:
            continue
        lows = pts[i - LOW_N:i]
        highs = pts[i:i + HIGH_N]
        if lows[-1][0] - lows[0][0] > LOW_SPAN - 1 or highs[-1][0] - highs[0][0] > HIGH_SPAN - 1:
            continue
        if cd_k - lows[-1][0] > 2:   # gap between lows and first high: too uncertain
            continue
        cover = max(v for _, v in lows)
        if not all(v > cover for _, v in highs):
            continue
        low_mean = st.mean(v for _, v in lows)
        rise = st.mean(v for _, v in highs) - low_mean
        if rise < min_rise:
            continue
        # sustained: a real luteal rise stays up; a noise run falls back. Check the next
        # <= SUSTAIN_N valid nights (if fewer than 3 exist yet the shift is provisional).
        later = [v for _, v in pts[i + HIGH_N:i + HIGH_N + SUSTAIN_N]]
        if len(later) >= 3 and st.median(later) < low_mean + SUSTAIN_FRAC * min_rise:
            continue
        return {'method': '3-over-6', 'first_high_cd': cd_k, 'confirm_cd': highs[-1][0],
                'last_low_cd': lows[-1][0], 'coverline_c': r2(cover), 'rise_c': r2(rise),
                'est_ovulation_cd': cd_k - 1, 'provisional': len(later) < 3}
    return None


def shift_smoothed(series, min_rise=MIN_RISE):
    pts = [(cd, v) for cd, v in series if v is not None]
    if len(pts) < 6:
        return None
    sm = []
    for i in range(len(pts)):
        w = [pts[j][1] for j in range(max(0, i - 1), min(len(pts), i + 2))]
        sm.append(st.median(w))
    best = None
    for i in range(3, len(pts) - 2):
        if pts[i][0] < MIN_SEARCH_CD:
            continue
        before = sm[max(0, i - 10):i]
        after = sm[i:i + 10]
        if len(before) < 3 or len(after) < 3:
            continue
        step = st.mean(after) - st.mean(before)
        if best is None or step > best[0]:
            best = (step, i, before, after)
    if best is None:
        return None
    step, i, before, after = best
    mid = (st.mean(before) + st.mean(after)) / 2
    frac = sum(v > mid for v in after) / len(after)
    if step < min_rise or frac < 0.8:
        return None
    cd_k = pts[i][0]
    return {'method': 'smoothed', 'first_high_cd': cd_k, 'confirm_cd': pts[min(len(pts) - 1, i + 2)][0],
            'rise_c': r2(step), 'midline_c': r2(mid), 'est_ovulation_cd': cd_k - 1}


# ---------------------------------------------------------------- main analysis

def _conf(n, sd):
    if n >= 3 and (sd or 0) <= 2.5:
        return 'high'
    if n >= 2:
        return 'medium'
    return 'low'


def analyze(nights, periods, today=None, settings=None):
    settings = settings or {}
    today = D(today) if today else dt.datetime.now(C.TZ).date()
    periods = normalize_periods([{'start': iso(p['start']) if isinstance(p.get('start'), dt.date) else p.get('start'),
                                  'end': iso(p['end']) if isinstance(p.get('end'), dt.date) else p.get('end'),
                                  'flow': p.get('flow'), 'symptoms': p.get('symptoms')} for p in (periods or [])])
    periods = [p for p in periods if p['start'] <= today]
    temps = nightly_temps(nights)
    series = with_deviation(temps)
    valid_n = sum(1 for x in series if x['excluded'] is None)
    out = {
        'disclaimer': DISCLAIMER,
        'generated': dt.datetime.now(C.TZ).isoformat(timespec='seconds'),
        'today': iso(today),
        'hormonal_bc': bool(settings.get('hormonal_bc')),
        'params': {'min_rise_c': MIN_RISE, 'rule': '3-over-6', 'baseline_window': BASE_WIN,
                   'default_cycle_len': DEFAULT_LEN, 'fertile_window': f'-{FERTILE_BEFORE}..+{FERTILE_AFTER} d around est. ovulation'},
        'nights_valid': valid_n,
        'messages': [], 'need': {},
        'cycles': [], 'current': None, 'prediction': None, 'stats': None,
    }
    msgs, need = out['messages'], out['need']
    if out['hormonal_bc']:
        msgs.append('Hormonal birth control is on: the temperature shift is usually suppressed, so ovulation '
                    'estimates are not shown as meaningful.')

    # cycles
    cycles = []
    for i, p in enumerate(periods):
        nxt = periods[i + 1]['start'] if i + 1 < len(periods) else None
        length = (nxt - p['start']).days if nxt else None
        per_len = (p['end'] - p['start']).days + 1 if p['end'] else None
        cycles.append({'start': p['start'], 'end_date': (nxt - dt.timedelta(days=1)) if nxt else None,
                       'length': length, 'complete': nxt is not None, 'period_end': p['end'],
                       'period_len': per_len, 'flow': p['flow'], 'symptoms': p['symptoms']})

    complete = [c for c in cycles if c['complete'] and VALID_LEN[0] <= c['length'] <= VALID_LEN[1]]
    ignored = [c for c in cycles if c['complete'] and not (VALID_LEN[0] <= c['length'] <= VALID_LEN[1])]
    recent = complete[-MAX_CYCLES:]
    lens = [c['length'] for c in recent]
    pers = [c['period_len'] for c in cycles if c['period_len']]
    avg_per = st.mean(pers) if pers else DEFAULT_PERIOD

    # tag nights with cycle + run detectors per cycle
    starts = [c['start'] for c in cycles]
    for x in series:
        x['cycle'] = None
        x['cd'] = None
        for ci in range(len(cycles) - 1, -1, -1):
            if x['date'] >= starts[ci]:
                x['cycle'] = ci
                x['cd'] = (x['date'] - starts[ci]).days + 1
                break
    for ci, c in enumerate(cycles):
        pts = [(x['cd'], x['temp_c'] if x['excluded'] is None else None) for x in series if x['cycle'] == ci]
        a = shift_3over6(pts)
        b = shift_smoothed(pts)
        c['nights_valid'] = sum(1 for _, v in pts if v is not None)
        c['shift'] = a
        c['shift_smoothed'] = b
        prim = a or b
        if prim:
            c['est_ovulation'] = {'cd': prim['est_ovulation_cd'], 'method': prim['method'],
                                  'confidence': 'medium' if a and b and abs(a['est_ovulation_cd'] - b['est_ovulation_cd']) <= 2
                                  else 'low'}
            if c['complete']:
                c['luteal_len'] = c['length'] - prim['est_ovulation_cd']
        else:
            c['est_ovulation'] = None
        if a and b:
            c['methods_agree'] = abs(a['est_ovulation_cd'] - b['est_ovulation_cd']) <= 2

    luts = [c['luteal_len'] for c in recent if c.get('luteal_len') and 8 <= c['luteal_len'] <= 18]
    luteal = st.mean(luts) if luts else DEFAULT_LUTEAL

    # prediction
    if lens:
        mean_len = st.mean(lens)
        sd = st.pstdev(lens) if len(lens) > 1 else None
        conf = _conf(len(lens), sd)
        basis = f'mean of last {len(lens)} cycle{"s" if len(lens) > 1 else ""}'
    else:
        mean_len, sd, conf, basis = DEFAULT_LEN, None, 'low', 'default 28-day cycle (no complete cycle logged yet)'
    window = max(2, round(sd)) if sd else (2 if len(lens) >= 2 else 4)

    if not cycles:
        out['status'] = 'no_period_logged'
        msgs.append('Log the first day of your period to start cycle tracking '
                    '(cycle.py --log-start YYYY-MM-DD, or the app once logging is wired up).')
        need['period_log'] = True
        if valid_n < LOW_N + HIGH_N:
            need['nights'] = LOW_N + HIGH_N - valid_n
            msgs.append(f'Temperature: {valid_n} usable night(s) so far; a temperature shift can be checked '
                        f'after at least {LOW_N + HIGH_N} nights in one cycle.')
    else:
        cur = cycles[-1]
        cd = (today - cur['start']).days + 1
        pred_len = max(mean_len, 1)
        next_start = cur['start'] + dt.timedelta(days=round(pred_len))
        late = today > next_start + dt.timedelta(days=window)
        cur_per = cur['period_len'] or round(avg_per)
        in_period = (cur['period_end'] is not None and today <= cur['period_end']) or \
                    (cur['period_end'] is None and cd <= round(avg_per))
        ov = cur['est_ovulation']
        if ov:
            ov_cd, ov_basis = ov['cd'], 'temperature (' + ov['method'] + ')'
        else:
            ov_cd, ov_basis = max(round(pred_len - luteal), 8), 'calendar (predicted length - luteal phase)'
        fw = (ov_cd - FERTILE_BEFORE, ov_cd + FERTILE_AFTER)
        if in_period:
            phase = 'menstrual'
        elif fw[0] <= cd <= fw[1]:
            phase = 'fertile'
        elif cd < fw[0]:
            phase = 'follicular'
        else:
            phase = 'luteal'
        if late:
            phase = 'late'
        labels = {'menstrual': 'Menstrual', 'follicular': 'Follicular', 'fertile': 'Fertile window (estimate)',
                  'luteal': 'Luteal' + ('' if ov else ' (estimated)'), 'late': 'Period later than predicted'}
        out['current'] = {
            'cycle_start': iso(cur['start']), 'cycle_day': cd, 'phase': phase, 'phase_label': labels[phase],
            'in_period': in_period, 'period_len_assumed': cur_per,
            'shift_detected': bool(ov), 'est_ovulation_cd': ov_cd, 'est_ovulation_date': iso(cur['start'] + dt.timedelta(days=ov_cd - 1)),
            'est_ovulation_basis': ov_basis,
            'fertile_window': {'start_cd': fw[0], 'end_cd': fw[1],
                               'start': iso(cur['start'] + dt.timedelta(days=fw[0] - 1)),
                               'end': iso(cur['start'] + dt.timedelta(days=fw[1] - 1)),
                               'label': 'estimate - not a safe/unsafe signal', 'basis': ov_basis},
            'nights_valid': cur['nights_valid'],
        }
        out['prediction'] = {
            'next_period_start': iso(next_start), 'window_days': window,
            'earliest': iso(next_start - dt.timedelta(days=window)), 'latest': iso(next_start + dt.timedelta(days=window)),
            'days_until': (next_start - today).days, 'mean_cycle_len': round(mean_len, 1),
            'sd_days': None if sd is None else round(sd, 1), 'n_cycles': len(lens), 'confidence': conf, 'basis': basis,
            'late': late,
        }
        out['stats'] = {
            'cycles_logged': len(cycles), 'complete_cycles': len(complete), 'ignored_cycles': len(ignored),
            'avg_cycle_len': None if not lens else round(mean_len, 1),
            'range': [min(lens), max(lens)] if lens else None,
            'avg_period_len': round(avg_per, 1) if pers else None,
            'avg_luteal_len': round(st.mean(luts), 1) if luts else None,
            'shifts_detected': sum(1 for c in cycles if c['est_ovulation']),
        }
        out['status'] = 'ok'
        if not lens:
            msgs.append('Prediction uses a default 28-day cycle until a full cycle is logged '
                        '(log the start of your next period). Low confidence.')
        if ignored:
            msgs.append(f'{len(ignored)} cycle(s) outside {VALID_LEN[0]}-{VALID_LEN[1]} days ignored for averages '
                        '(missed log?).')
        nv = cur['nights_valid']
        if not ov and nv < LOW_N + HIGH_N:
            need['nights'] = LOW_N + HIGH_N - nv
            msgs.append(f'Need {LOW_N + HIGH_N - nv} more usable night(s) this cycle before a temperature shift '
                        'can be detected. Wear the ring to sleep.')
        elif not ov and phase in ('luteal', 'late'):
            msgs.append('No temperature shift detected this cycle yet. That can happen (missed nights, illness, '
                        'anovulatory cycle, hormonal birth control).')
        if late:
            msgs.append('Period is later than the predicted window. Cycles vary; if this keeps happening or '
                        'you are concerned, talk to a doctor.')

    # serialise
    out['cycles'] = [{
        'start': iso(c['start']), 'end_date': iso(c['end_date']), 'length': c['length'], 'complete': c['complete'],
        'period_end': iso(c['period_end']), 'period_len': c['period_len'], 'flow': c['flow'], 'symptoms': c['symptoms'],
        'nights_valid': c['nights_valid'],
        'shift': _shift_doc(c['shift'], c['start']), 'shift_smoothed': _shift_doc(c['shift_smoothed'], c['start']),
        'methods_agree': c.get('methods_agree'),
        'est_ovulation': None if not c['est_ovulation'] else dict(c['est_ovulation'], date=iso(c['start'] + dt.timedelta(days=c['est_ovulation']['cd'] - 1))),
        'fertile_window': None if not c['est_ovulation'] else {
            'start': iso(c['start'] + dt.timedelta(days=c['est_ovulation']['cd'] - 1 - FERTILE_BEFORE)),
            'end': iso(c['start'] + dt.timedelta(days=c['est_ovulation']['cd'] - 1 + FERTILE_AFTER)),
            'label': 'estimate (retrospective)'},
        'luteal_len': c.get('luteal_len'),
    } for c in cycles]
    out['temps'] = [{'date': iso(x['date']), 'temp_c': r2(x['temp_c']), 'baseline_c': r2(x['baseline_c']),
                     'dev_c': r2(x['dev_c']), 'excluded': x['excluded'], 'baseline': x['baseline'],
                     'cycle': x['cycle'], 'cd': x['cd'],
                     'period': _in_period(x['date'], cycles, avg_per)} for x in series]
    if valid_n == 0:
        msgs.append('No nightly skin temperature yet: wear the ring to sleep and sync in the morning.')
    return out


def _in_period(d, cycles, avg_per):
    for c in cycles:
        end = c['period_end'] or (c['start'] + dt.timedelta(days=round(avg_per) - 1))
        if c['start'] <= d <= end:
            return True
    return False


def _shift_doc(s, start):
    if not s:
        return None
    o = dict(s)
    for k in ('first_high_cd', 'confirm_cd', 'est_ovulation_cd'):
        if k in o:
            o[k.replace('_cd', '_date')] = iso(start + dt.timedelta(days=o[k] - 1))
    return o


# ---------------------------------------------------------------- log editing + CLI

def _write_log(path, periods, settings):
    doc = [{k: (iso(v) if isinstance(v, dt.date) else v) for k, v in p.items() if v not in (None, [])}
           for p in periods]
    if settings:
        doc = dict(settings, periods=doc)
    C.write_json_atomic(path, doc)


def log_start(path, day, flow=None, symptoms=None):
    periods, settings = load_log(path)
    day = D(day)
    if any(p['start'] == day for p in periods):
        p = next(p for p in periods if p['start'] == day)
    else:
        p = {'start': day, 'end': None, 'flow': None, 'symptoms': []}
        periods.append(p)
    if flow:
        p['flow'] = flow
    if symptoms:
        p['symptoms'] = sorted(set(p['symptoms']) | set(symptoms))
    periods.sort(key=lambda x: x['start'])
    _write_log(path, periods, settings)
    return p


def log_end(path, day):
    periods, settings = load_log(path)
    day = D(day)
    cand = [p for p in periods if p['start'] <= day]
    if not cand:
        raise SystemExit('no period start on or before ' + iso(day))
    cand[-1]['end'] = day
    _write_log(path, periods, settings)
    return cand[-1]


def describe(res):
    lines = []
    if res['status'] == 'no_period_logged':
        lines.append('Cycle: no period logged yet.')
    else:
        c, p = res['current'], res['prediction']
        lines.append(f"Cycle day {c['cycle_day']} ({c['phase_label']}), started {c['cycle_start']}.")
        lines.append(f"Next period ~{p['next_period_start']} (±{p['window_days']} d, {p['confidence']} confidence, {p['basis']}).")
        lines.append(f"Est. ovulation day {c['est_ovulation_cd']} via {c['est_ovulation_basis']}; "
                     f"fertile window (estimate) {c['fertile_window']['start']}..{c['fertile_window']['end']}.")
    lines += res['messages']
    lines.append('NOTE: ' + res['disclaimer'])
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--nightly', default=C.NIGHTLY_JSON)
    ap.add_argument('--log', default=CYCLE_LOG)
    ap.add_argument('--out', default=CYCLE_JSON)
    ap.add_argument('--today')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--log-start', metavar='DATE')
    ap.add_argument('--log-end', metavar='DATE')
    ap.add_argument('--flow', choices=['spotting', 'light', 'medium', 'heavy'])
    ap.add_argument('--symptoms', help='comma-separated')
    a = ap.parse_args(argv)
    if a.log_start:
        p = log_start(a.log, a.log_start, a.flow, [s.strip() for s in (a.symptoms or '').split(',') if s.strip()])
        print(f"logged period start {iso(p['start'])} -> {a.log}")
    if a.log_end:
        p = log_end(a.log, a.log_end)
        print(f"logged period end {iso(p['end'])} (start {iso(p['start'])}) -> {a.log}")
    periods, settings = load_log(a.log)
    nights = (C.read_json(a.nightly, {}) or {}).get('nights', [])
    res = analyze(nights, [{'start': iso(p['start']), 'end': iso(p['end']), 'flow': p['flow'], 'symptoms': p['symptoms']}
                           for p in periods], a.today, settings)
    C.write_json_atomic(a.out, res)
    if a.json:
        print(json.dumps(res, indent=1))
    else:
        print('\n'.join(describe(res)))


if __name__ == '__main__':
    main()
