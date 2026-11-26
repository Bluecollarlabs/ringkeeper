#!/usr/bin/env python3
"""Weekly sleep summary (meant for Sunday mornings): <=8 lines of chat/SMS-friendly text + JSON.

Reads data/nightly.json (built from the DB that data/source.json selects), never the DB itself.

    python3 -I tools/analysis/weekly.py [--end 2026-11-29] [--nightly data/nightly.json]
        [--json-out data/weekly.json] [--json]

Week = the 7 wake dates ending on --end (default: today, local time zone); "prior week" = the 7
before that. Only kind == night counts (naps ignored). Reported:
  - average estimated sleep (fallback: time in bed) and nights recorded
  - resting HR (hr_low) and HRV (rmssd_avg) week average vs prior week (delta; "steady" within
    +-1 bpm / +-3 ms, otherwise up/down), or "no prior week yet"
  - best / worst night by estimated sleep
  - bedtime consistency: SD of bedtime (clock wrapped around noon so 23:30 and 00:30 are 1 h
    apart) -> very consistent <= 30 min, fairly consistent <= 60 min, else variable; plus range
  - activity (when data/days.json from day.py is available): average estimated steps/day and
    active minutes/day (MET >= 3) over days with >= 60 recorded awake minutes, vs prior week
Wording is non-medical; stage-based numbers and steps are estimates.
"""
import argparse
import datetime as dt
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

WEEKLY_JSON = os.path.join(C.DATA_DIR, 'weekly.json')
MAX_LINES = 8
STEADY = {'rhr': 1.0, 'hrv': 3.0}


def hm(minutes):
    m = int(round(minutes))
    return f'{m // 60}h {m % 60:02d}m'


def _sleep_min(n):
    v = n.get('sleep_min_est')
    return v if v is not None else n.get('time_in_bed_min')


def bed_minutes(iso_s):
    """Bedtime as minutes after noon (12:00 -> 0, 23:30 -> 690, 00:30 -> 750)."""
    t = dt.datetime.fromisoformat(iso_s)
    return (t.hour * 60 + t.minute + t.second / 60 - 720) % 1440


def clock_of(mins_after_noon):
    m = int(round(mins_after_noon + 720)) % 1440
    return f'{m // 60:02d}:{m % 60:02d}'


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _window(nights, end, days):
    lo = end - dt.timedelta(days=days - 1)
    out = []
    for n in nights:
        if n.get('kind', 'night') != 'night' or not n.get('date'):
            continue
        d = dt.date.fromisoformat(n['date'])
        if lo <= d <= end:
            out.append(n)
    return sorted(out, key=lambda n: n['date'])


def _trend(cur, prev, key):
    if cur is None:
        return {'week': None, 'prior': prev, 'delta': None, 'dir': None}
    if prev is None:
        return {'week': round(cur, 1), 'prior': None, 'delta': None, 'dir': None}
    d = cur - prev
    direction = 'steady' if abs(d) <= STEADY[key] else ('up' if d > 0 else 'down')
    return {'week': round(cur, 1), 'prior': round(prev, 1), 'delta': round(d, 1), 'dir': direction}


def _day(n):
    return dt.date.fromisoformat(n['date']).strftime('%a %-d %b')


MIN_DAY_MIN = 60


def _activity(days, lo, hi):
    ds = [d for d in days or [] if lo.isoformat() <= d.get('date', '') <= hi.isoformat()
          and (d.get('recorded_min') or 0) >= MIN_DAY_MIN and (d.get('steps') or {}).get('value') is not None]
    if not ds:
        return None
    kc = [d['active_kcal'] for d in ds if d.get('active_kcal') is not None]
    return {'days': len(ds), 'steps_avg': round(_avg([d['steps']['value'] for d in ds]), -1),
            'active_min_avg': round(_avg([d.get('active_min') or 0 for d in ds])),
            'active_kcal_avg': round(_avg(kc)) if kc else None,
            'most_steps': max(ds, key=lambda d: d['steps']['value'])['date'], 'steps_estimated': True}


def summarize(nights, end=None, days=None):
    end = end or dt.datetime.now(C.TZ).date()
    week = _window(nights, end, 7)
    prior = _window(nights, end - dt.timedelta(days=7), 7)
    start = end - dt.timedelta(days=6)
    act = _activity(days, start, end)
    act_prior = _activity(days, start - dt.timedelta(days=7), end - dt.timedelta(days=7))
    res = {'activity': act, 'prior_activity': act_prior,
           'week_start': start.isoformat(), 'week_end': end.isoformat(), 'nights': len(week),
           'prior_nights': len(prior), 'generated': C.iso(dt.datetime.now(C.TZ).timestamp())}
    if not week:
        res['text'] = (f"Week {start.strftime('%-d %b')}–{end.strftime('%-d %b')}: no nights recorded. "
                       'Wear the ring to bed and sync in the morning.')
        res['lines'] = [res['text']]
        return res
    sleeps = [(n, _sleep_min(n)) for n in week if _sleep_min(n) is not None]
    avg_sleep = _avg([s for _, s in sleeps])
    rhr = _trend(_avg([n.get('hr_low') for n in week]), _avg([n.get('hr_low') for n in prior]), 'rhr')
    hrv = _trend(_avg([n.get('rmssd_avg') for n in week]), _avg([n.get('rmssd_avg') for n in prior]), 'hrv')
    beds = [bed_minutes(n['bed']) for n in week if n.get('bed')]
    bed = None
    if beds:
        sd = st.pstdev(beds) if len(beds) > 1 else 0.0
        label = ('n/a (1 night)' if len(beds) < 2 else 'very consistent' if sd <= 30
                 else 'fairly consistent' if sd <= 60 else 'variable')
        bed = {'avg': clock_of(st.mean(beds)), 'earliest': clock_of(min(beds)), 'latest': clock_of(max(beds)),
               'sd_min': round(sd), 'label': label}
    best = max(sleeps, key=lambda x: x[1]) if sleeps else None
    worst = min(sleeps, key=lambda x: x[1]) if len(sleeps) > 1 else None
    res.update({
        'avg_sleep_min': round(avg_sleep) if avg_sleep is not None else None,
        'avg_sleep_estimated': any(n.get('sleep_min_est') is not None for n in week),
        'prior_avg_sleep_min': (round(_avg([_sleep_min(n) for n in prior])) if prior and _avg([_sleep_min(n) for n in prior]) is not None else None),
        'rhr': rhr, 'hrv': hrv, 'bedtime': bed,
        'best': {'date': best[0]['date'], 'sleep_min': round(best[1])} if best else None,
        'worst': {'date': worst[0]['date'], 'sleep_min': round(worst[1])} if worst else None,
    })

    def trend_line(name, t, unit, fmt='{:.0f}'):
        if t['week'] is None:
            return None
        s = f'{name}: {fmt.format(t["week"])} {unit} avg'
        if t['prior'] is None:
            return s + ' (no prior week to compare yet)'
        word = {'steady': 'steady', 'up': 'up', 'down': 'down'}[t['dir']]
        return s + f' — {word} vs {fmt.format(t["prior"])} {unit} the week before ({t["delta"]:+.0f})'

    lines = [f"Your week, {start.strftime('%a %-d %b')}–{end.strftime('%a %-d %b')} ({len(week)} night{'s' if len(week) != 1 else ''} recorded)"]
    if avg_sleep is not None:
        s = f"Sleep: {hm(avg_sleep)} a night on average{' (estimated)' if res['avg_sleep_estimated'] else ''}"
        if res['prior_avg_sleep_min'] is not None:
            s += f", vs {hm(res['prior_avg_sleep_min'])} the week before"
        lines.append(s)
    if act:
        s = (f"Activity: ~{act['steps_avg']:,.0f} steps/day (estimated), {act['active_min_avg']} active min/day "
             f"over {act['days']} day{'s' if act['days'] != 1 else ''} with data")
        if act_prior:
            s += f", vs ~{act_prior['steps_avg']:,.0f} the week before"
        lines.append(s)
    lines += [x for x in (trend_line('Resting HR', rhr, 'bpm'), trend_line('HRV', hrv, 'ms')) if x]
    if best and worst and best[0] is not worst[0]:
        lines.append(f'Best night: {_day(best[0])} ({hm(best[1])}); shortest: {_day(worst[0])} ({hm(worst[1])})')
    elif best:
        lines.append(f'Only night: {_day(best[0])} ({hm(best[1])})')
    if bed and len(beds) > 1:
        lines.append(f"Bedtime: {bed['label']} (±{bed['sd_min']} min), {bed['earliest']}–{bed['latest']}, avg {bed['avg']}")
    elif bed:
        lines.append(f"Bedtime: {bed['avg']}")
    if len(week) < 4:
        lines.append('A few more nights each week will make these trends more meaningful.')
    lines = lines[:MAX_LINES - 1] + ['Ring estimates for awareness only, not medical advice.']
    res['lines'] = lines
    res['text'] = '\n'.join(lines)
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description='weekly sleep summary (<=8 lines) from data/nightly.json')
    ap.add_argument('--end', type=dt.date.fromisoformat, default=None, help='last wake date of the week (default today)')
    ap.add_argument('--nightly', default=C.NIGHTLY_JSON)
    ap.add_argument('--days', default=os.path.join(C.DATA_DIR, 'days.json'), help='day.py output (optional)')
    ap.add_argument('--json-out', default=WEEKLY_JSON, help="'-' = don't write")
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args(argv)
    nights = (C.read_json(a.nightly, {}) or {}).get('nights', [])
    days = (C.read_json(a.days, {}) or {}).get('days', []) if a.days else []
    res = summarize(nights, a.end, days)
    res['data_source'] = C.source_config()['mode']
    if a.json_out and a.json_out != '-':
        C.write_json_atomic(a.json_out, res)
    import json
    print(json.dumps(res, indent=1, ensure_ascii=False) if a.json else res['text'])
    return 0


if __name__ == '__main__':
    sys.exit(main())
