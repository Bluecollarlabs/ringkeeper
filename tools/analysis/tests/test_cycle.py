"""Synthetic tests for tools/analysis/cycle.py (no real health data).

Run from repo root:
    python3 -I -m unittest discover -s tools/analysis/tests -v
"""
import datetime as dt
import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import common as C  # noqa: E402
import cycle as CY  # noqa: E402

START = dt.date(2026, 1, 5)
BASE_T = 34.6


def make(cycles, seed=1, noise=0.07, rise=0.35, missing=(), spikes=None, ramp=2):
    """cycles: list of (length, ovulation_cd or None). Returns (nights, periods, true_ov_dates).

    Night value for wake date d: follicular BASE_T, luteal BASE_T + rise (ramping over `ramp`
    nights starting the day after ovulation), Gaussian noise. `missing`: dates (index from
    START) without a night; `spikes`: {index: +delta}.
    """
    rng = random.Random(seed)
    nights, periods, truth = [], [], []
    day = START
    i = 0
    for length, ov in cycles:
        periods.append({'start': day.isoformat(), 'end': (day + dt.timedelta(days=4)).isoformat()})
        if ov:
            truth.append(day + dt.timedelta(days=ov - 1))
        for cd in range(1, length + 1):
            d = day + dt.timedelta(days=cd - 1)
            lvl = 0.0
            if ov and cd > ov:
                lvl = rise * min(1.0, (cd - ov) / ramp)
            v = BASE_T + lvl + rng.gauss(0, noise)
            if spikes and i in spikes:
                v += spikes[i]
            if i not in missing:
                nights.append({'date': d.isoformat(), 'kind': 'night', 'temp_avg_c': round(v, 3),
                               'coverage_pct': {'temp': 97.0}, 'time_in_bed_min': 450})
            i += 1
        day += dt.timedelta(days=length)
    return nights, periods, truth, day


class ShiftRules(unittest.TestCase):
    def test_3over6_clean(self):
        pts = [(cd, 34.5 + (0.4 if cd > 14 else 0.0)) for cd in range(1, 29)]
        s = CY.shift_3over6(pts)
        self.assertIsNotNone(s)
        self.assertEqual(s['first_high_cd'], 15)
        self.assertEqual(s['est_ovulation_cd'], 14)
        self.assertEqual(s['confirm_cd'], 17)

    def test_3over6_needs_min_rise(self):
        pts = [(cd, 34.5 + (0.1 if cd > 14 else 0.0)) for cd in range(1, 29)]
        self.assertIsNone(CY.shift_3over6(pts))
        self.assertIsNone(CY.shift_smoothed(pts))

    def test_smoothed_tolerates_one_dip(self):
        pts = [(cd, 34.5 + (0.4 if cd > 14 else 0.0)) for cd in range(1, 29)]
        pts[15] = (16, 34.45)    # one low night right after the rise breaks 3-over-6 at day 15
        s = CY.shift_smoothed(pts)
        self.assertIsNotNone(s)
        self.assertLessEqual(abs(s['est_ovulation_cd'] - 14), 2)


class Analyze(unittest.TestCase):
    def test_biphasic_cycles_detected(self):
        spec = [(29, 15), (27, 13), (30, 16), (28, 14)]
        for seed in range(1, 6):
            nights, periods, truth, end = make(spec + [(10, None)], seed=seed)
            res = CY.analyze(nights, periods, today=end - dt.timedelta(days=1))
            self.assertEqual(res['status'], 'ok')
            for c, t in zip(res['cycles'][:4], truth):
                self.assertIsNotNone(c['est_ovulation'], (seed, c['start']))
                err = abs((dt.date.fromisoformat(c['est_ovulation']['date']) - t).days)
                self.assertLessEqual(err, 2, (seed, c['start'], c['est_ovulation']))
            p = res['prediction']
            self.assertAlmostEqual(p['mean_cycle_len'], 28.5)
            self.assertEqual(p['n_cycles'], 4)
            self.assertEqual(p['confidence'], 'high')
            self.assertTrue(res['disclaimer'].startswith('Awareness only'))

    def test_noisy_missed_nights_and_spike(self):
        # cycle 1: ov day 14; nights 13,16 missing (around the shift), fever spike on night 6
        nights, periods, truth, end = make([(28, 14), (12, None)], seed=7, noise=0.08,
                                           missing={12, 15, 20}, spikes={5: 0.9})
        res = CY.analyze(nights, periods, today=end - dt.timedelta(days=1))
        c0 = res['cycles'][0]
        self.assertIsNotNone(c0['est_ovulation'])
        err = abs((dt.date.fromisoformat(c0['est_ovulation']['date']) - truth[0]).days)
        self.assertLessEqual(err, 2)
        spike = [t for t in res['temps'] if t['date'] == (START + dt.timedelta(days=5)).isoformat()][0]
        self.assertTrue(spike['excluded'] and spike['excluded'].startswith('outlier'))

    def test_anovulatory_cycle_no_shift(self):
        false_pos = 0
        for seed in range(1, 21):
            nights, periods, truth, end = make([(30, None), (5, None)], seed=seed, noise=0.08)
            res = CY.analyze(nights, periods, today=end - dt.timedelta(days=1))
            if res['cycles'][0]['shift'] is not None:
                false_pos += 1
        self.assertLessEqual(false_pos, 1)   # occasional noise-run tolerated, not systematic

    def test_no_period_logged(self):
        nights, _, _, end = make([(5, None)])
        res = CY.analyze(nights, [], today=end)
        self.assertEqual(res['status'], 'no_period_logged')
        self.assertTrue(res['need']['period_log'])
        self.assertEqual(res['need']['nights'], 4)
        self.assertTrue(any('Log the first day' in m for m in res['messages']))
        self.assertEqual(len(res['temps']), 5)
        self.assertIn('disclaimer', res)

    def test_single_period_default_prediction(self):
        nights, periods, _, _ = make([(6, None)])
        res = CY.analyze(nights, periods[:1], today=START + dt.timedelta(days=5))
        p = res['prediction']
        self.assertEqual(p['confidence'], 'low')
        self.assertEqual(p['mean_cycle_len'], 28)
        self.assertEqual(p['next_period_start'], (START + dt.timedelta(days=28)).isoformat())
        self.assertEqual(res['current']['cycle_day'], 6)
        self.assertEqual(res['current']['phase'], 'follicular')
        self.assertEqual(res['need']['nights'], 3)
        self.assertEqual(res['current']['est_ovulation_basis'][:8], 'calendar')

    def test_phases(self):
        nights, periods, _, _ = make([(28, 14), (28, 14), (28, None)], seed=3)
        cstart = START + dt.timedelta(days=56)
        phase = lambda k: CY.analyze(nights, periods, today=cstart + dt.timedelta(days=k - 1))['current']['phase']
        self.assertEqual(phase(2), 'menstrual')
        self.assertEqual(phase(7), 'follicular')
        self.assertEqual(phase(12), 'fertile')
        self.assertEqual(phase(20), 'luteal')
        self.assertEqual(phase(40), 'late')

    def test_hormonal_bc_flag(self):
        nights, periods, _, end = make([(28, None), (3, None)])
        res = CY.analyze(nights, periods, today=end, settings={'hormonal_bc': True})
        self.assertTrue(res['hormonal_bc'])
        self.assertTrue(any('Hormonal birth control' in m for m in res['messages']))

    def test_log_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'cycle_log.json')
            CY.log_start(path, '2026-11-01', 'medium', ['cramps'])
            CY.log_end(path, '2026-11-05')
            CY.log_start(path, '2026-11-29')
            periods, _ = CY.load_log(path)
            self.assertEqual([p['start'].isoformat() for p in periods], ['2026-11-01', '2026-11-29'])
            self.assertEqual(periods[0]['end'], dt.date(2026, 11, 5))
            self.assertEqual(periods[0]['symptoms'], ['cramps'])
            self.assertEqual(oct(os.stat(path).st_mode & 0o777), '0o600')


if __name__ == '__main__':
    unittest.main()
