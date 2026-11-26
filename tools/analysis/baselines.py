#!/usr/bin/env python3
"""Rolling personal baselines (robust median / MAD) from data/nightly.json.

Usage (from repo root):
    python3 -I tools/analysis/baselines.py [--nightly data/nightly.json] [--json]

For each metric and window (14 and 30 nights) the baseline of night i uses only the
nights *before* i (kind == "night", metric present): median and MAD (x1.4826, SD-scale).
A value is flagged "high"/"low" when it is more than 2 MAD from the median.
MAD is floored per metric so a very steady run of nights does not flag tiny changes.
Flags start once MIN_NIGHTS prior nights exist; before that status = "insufficient" with
`need_more` = nights still needed.

Metrics:
  rhr       night's lowest 5-min HR (`hr_low`, bpm)
  rmssd     average of the ring's 5-min RMSSD samples (`rmssd_avg`, ms)
  temp_dev  skin temp average minus the baseline median (`temp_avg_c`, deg C); the
            reported value is the deviation, the baseline is on the absolute temp
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

WINDOWS = (14, 30)
MIN_NIGHTS = 5
K = 2.0
METRICS = {
    # name: (nightly field, MAD floor, unit, decimals)
    'rhr': ('hr_low', 1.0, 'bpm', 1),
    'rmssd': ('rmssd_avg', 4.0, 'ms', 1),
    'temp_dev': ('temp_avg_c', 0.10, 'C', 2),
}


def _baseline(prior, floor):
    vals = [v for v in prior if v is not None]
    if not vals:
        return None
    med, md = C.median(vals), C.mad(vals)
    md_eff = max(md or 0.0, floor)
    return {'n': len(vals), 'median': med, 'mad': md, 'mad_used': md_eff,
            'low': med - K * md_eff, 'high': med + K * md_eff}


def evaluate(nights, idx=None):
    """Baseline status for night `idx` (default: latest 'night'-kind entry)."""
    ns = [x for x in nights if x.get('kind', 'night') == 'night']
    if not ns:
        return {'night': None, 'metrics': {}, 'nights_available': 0}
    if idx is None:
        idx = len(ns) - 1
    cur = ns[idx]
    out = {}
    for name, (field, floor, unit, dec) in METRICS.items():
        val = cur.get(field)
        prior_all = [x.get(field) for x in ns[:idx]]
        avail = sum(v is not None for v in prior_all)
        m = {'field': field, 'unit': unit, 'raw': val, 'windows': {}}
        for w in WINDOWS:
            b = _baseline(prior_all[-w:], floor)
            m['windows'][str(w)] = None if b is None else {k: (round(v, dec + 1) if isinstance(v, float) else v)
                                                            for k, v in b.items()}
        b14 = _baseline(prior_all[-WINDOWS[0]:], floor)
        if name == 'temp_dev':
            m['value'] = None if (val is None or b14 is None) else round(val - b14['median'], dec)
        else:
            m['value'] = None if val is None else round(val, dec)
        if val is None:
            m['status'] = 'missing'
        elif avail < MIN_NIGHTS:
            m['status'] = 'insufficient'
            m['need_more'] = MIN_NIGHTS - avail
        else:
            m['status'] = 'high' if val > b14['high'] else 'low' if val < b14['low'] else 'ok'
            m['z_mad'] = round((val - b14['median']) / b14['mad_used'], 2)
        out[name] = m
    return {'night': cur.get('date'), 'bed': cur.get('bed'), 'metrics': out,
            'nights_available': len(ns), 'prior_nights': idx, 'min_nights': MIN_NIGHTS,
            'k_mad': K, 'windows': list(WINDOWS)}


def history(nights):
    """Per-night value + 14-night baseline band (for trend charts)."""
    ns = [x for x in nights if x.get('kind', 'night') == 'night']
    rows = []
    for i in range(len(ns)):
        e = evaluate(ns, i)
        rows.append({'date': ns[i].get('date'), **{
            k: {'value': v.get('value'), 'status': v['status'],
                'median14': (v['windows']['14'] or {}).get('median'),
                'low14': (v['windows']['14'] or {}).get('low'),
                'high14': (v['windows']['14'] or {}).get('high')}
            for k, v in e['metrics'].items()}})
    return rows


def describe(ev):
    """Short human text per metric (non-medical)."""
    words = {'rhr': 'resting HR', 'rmssd': 'HRV', 'temp_dev': 'skin temp'}
    out = []
    for k, m in ev['metrics'].items():
        if m['status'] == 'insufficient':
            out.append(f"{words[k]}: need {m['need_more']} more nights for a baseline")
        elif m['status'] in ('high', 'low'):
            out.append(f"{words[k]}: {m['status']}er than usual")
        elif m['status'] == 'ok':
            out.append(f'{words[k]}: within usual range')
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description='rolling baselines')
    ap.add_argument('--nightly', default=C.NIGHTLY_JSON)
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args(argv)
    doc = C.read_json(a.nightly, {}) or {}
    nights = doc.get('nights', [])
    ev = evaluate(nights)
    ev['history'] = history(nights)
    if a.json:
        print(json.dumps(ev, indent=1))
    else:
        print(f"night {ev['night']}: {ev['nights_available']} nights on file (flags need {MIN_NIGHTS} prior)")
        for line in describe(ev):
            print('  ' + line)
    return 0


if __name__ == '__main__':
    sys.exit(main())
