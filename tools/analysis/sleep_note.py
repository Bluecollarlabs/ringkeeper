#!/usr/bin/env python3
"""Short plain-English morning sleep note (<=5 lines, chat/SMS-friendly) + JSON.

Usage (from repo root):
    python3 -I tools/analysis/sleep_note.py [--nightly data/nightly.json]
        [--json-out data/sleep_note.json] [--refresh] [--db data/oura.db]

--refresh re-runs nights.py first (reads the DB read-only, rewrites data/nightly.json).
Prints the note text to stdout; writes JSON (text + structured fields) to --json-out.
Wording is deliberately non-medical and second person, so the same note works for
whoever wears the ring. Stage numbers are labelled as estimates.
"""
import argparse
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import baselines as B  # noqa: E402
import common as C  # noqa: E402

NOTE_JSON = os.path.join(C.DATA_DIR, 'sleep_note.json')
MAX_LINES = 5


def hm(minutes):
    if minutes is None:
        return '?'
    m = int(round(minutes))
    return f'{m // 60}h {m % 60:02d}m'


def _clock(iso_s):
    return dt.datetime.fromisoformat(iso_s).strftime('%H:%M') if iso_s else '?'


def _night_label(n):
    w = dt.datetime.fromisoformat(n['wake']).date()
    p = w - dt.timedelta(days=1)
    return f"night of {p.strftime('%a')} {p.day}–{w.strftime('%a')} {w.day} {w.strftime('%b')}"


PHRASES = {
    ('rhr', 'high'): 'resting HR was higher than your usual',
    ('rhr', 'low'): 'resting HR was lower than your usual',
    ('rmssd', 'high'): 'HRV was higher than your usual',
    ('rmssd', 'low'): 'HRV was lower than your usual',
    ('temp_dev', 'high'): 'skin temp ran warmer than usual',
    ('temp_dev', 'low'): 'skin temp ran cooler than usual',
}


def build_note(nights):
    ns = [x for x in nights if x.get('kind', 'night') == 'night']
    if not ns:
        return {'text': 'No night recorded yet — wear the ring to bed and sync in the morning.', 'lines': 1}
    n = ns[-1]
    ev = B.evaluate(nights)
    s = n.get('stages') or {}
    lines = [f"Sleep, {_night_label(n)}: {hm(n['time_in_bed_min'])} in bed ({_clock(n['bed'])}–{_clock(n['wake'])})"
             + (f", about {hm(n.get('sleep_min_est'))} asleep" if n.get('sleep_min_est') else '') + '.']
    if s.get('ok'):
        mn = s['minutes']
        lines.append(f"Stages (estimated): deep {hm(mn['deep'])}, REM {hm(mn['rem'])}, "
                     f"light {hm(mn['light'])}, awake {hm(mn['awake'])}.")
    vit = []
    if n.get('hr_low') is not None:
        vit.append(f"resting HR {n['hr_low']:.0f} bpm")
    if n.get('rmssd_avg') is not None:
        vit.append(f"HRV {n['rmssd_avg']:.0f} ms")
    td = ev['metrics'].get('temp_dev', {}).get('value')
    if td is not None:
        vit.append(f'skin temp {td:+.1f} °C vs usual')
    elif n.get('temp_avg_c') is not None:
        vit.append(f"skin temp {n['temp_avg_c']:.1f} °C")
    if vit:
        lines.append(vit[0][0].upper() + ', '.join(vit)[1:] + '.')
    flags = [PHRASES[(k, m['status'])] for k, m in ev['metrics'].items() if (k, m['status']) in PHRASES]
    insuff = [m.get('need_more') for m in ev['metrics'].values() if m['status'] == 'insufficient']
    if flags:
        lines.append('Compared with your recent nights: ' + '; '.join(flags) + '.')
    elif insuff:
        lines.append(f'Still learning your normal: {max(insuff)} more night(s) needed before comparisons.')
    elif any(m['status'] == 'ok' for m in ev['metrics'].values()):
        lines.append('Resting HR, HRV and skin temp were all within your usual range.')
    lines.append('Ring-based estimates for awareness only, not medical advice.')
    lines = lines[:MAX_LINES]
    return {
        'text': '\n'.join(lines), 'lines': len(lines),
        'night': {k: n.get(k) for k in ('date', 'bed', 'wake', 'time_in_bed_min', 'sleep_min_est',
                                        'efficiency_pct_est', 'hr_median', 'hr_low', 'rmssd_avg',
                                        'temp_avg_c', 'movement_index_pct')},
        'stages': {'label': s.get('label'), 'minutes': s.get('minutes'), 'pct_of_sleep': s.get('pct_of_sleep'),
                   'checks_passed': s.get('checks_passed'), 'checks_total': s.get('checks_total')},
        'baselines': {k: {kk: m.get(kk) for kk in ('value', 'raw', 'status', 'need_more', 'z_mad', 'unit')}
                      for k, m in ev['metrics'].items()},
        'generated': C.iso(dt.datetime.now(C.TZ).timestamp()),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description='morning sleep note')
    ap.add_argument('--nightly', default=C.NIGHTLY_JSON)
    ap.add_argument('--json-out', default=NOTE_JSON)
    ap.add_argument('--refresh', action='store_true')
    ap.add_argument('--db', default=C.DEFAULT_DB)
    a = ap.parse_args(argv)
    if a.refresh:
        import nights as N
        N.main(['--db', a.db, '--out', a.nightly, '--quiet'])
    doc = C.read_json(a.nightly, {}) or {}
    note = build_note(doc.get('nights', []))
    if a.json_out and a.json_out != '-':
        C.write_json_atomic(a.json_out, note)
    print(note['text'])
    return 0


if __name__ == '__main__':
    sys.exit(main())
