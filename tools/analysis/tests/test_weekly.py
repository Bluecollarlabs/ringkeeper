"""Tests for weekly.py (synthetic nights only)."""
import datetime as dt
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import weekly as W  # noqa: E402


def night(date, bed='23:00', sleep=420, rhr=50, hrv=80, kind='night'):
    d = dt.date.fromisoformat(date)
    h, m = map(int, bed.split(':'))
    bd = d - dt.timedelta(days=1) if h >= 12 else d
    return {'date': date, 'kind': kind, 'bed': f'{bd.isoformat()}T{h:02d}:{m:02d}:00-05:00',
            'time_in_bed_min': sleep + 40, 'sleep_min_est': sleep, 'hr_low': rhr, 'rmssd_avg': hrv}


END = dt.date(2026, 11, 29)  # a Sunday


def days(start, k):
    return [(start + dt.timedelta(days=i)).isoformat() for i in range(k)]


class TestWeekly(unittest.TestCase):
    def test_no_nights(self):
        r = W.summarize([], END)
        self.assertEqual(r['nights'], 0)
        self.assertIn('no nights recorded', r['text'])
        self.assertEqual(len(r['lines']), 1)

    def test_full_week_vs_prior(self):
        prior = [night(d, rhr=52, hrv=70, sleep=400) for d in days(dt.date(2026, 11, 16), 7)]
        cur_dates = days(dt.date(2026, 11, 23), 7)
        sleeps = [420, 450, 380, 500, 410, 430, 440]
        cur = [night(d, rhr=49, hrv=85, sleep=s) for d, s in zip(cur_dates, sleeps)]
        cur.append(night('2026-11-28', kind='nap', sleep=30))  # nap ignored
        r = W.summarize(prior + cur, END)
        self.assertEqual(r['nights'], 7)
        self.assertEqual(r['prior_nights'], 7)
        self.assertEqual(r['avg_sleep_min'], round(sum(sleeps) / 7))
        self.assertEqual(r['rhr']['dir'], 'down')
        self.assertAlmostEqual(r['rhr']['delta'], -3.0)
        self.assertEqual(r['hrv']['dir'], 'up')
        self.assertEqual(r['best']['date'], '2026-11-26')
        self.assertEqual(r['worst']['date'], '2026-11-25')
        self.assertEqual(r['bedtime']['label'], 'very consistent')
        self.assertLessEqual(len(r['lines']), W.MAX_LINES)
        self.assertTrue(all(len(x) <= 160 for x in r['lines']))
        self.assertIn('not medical advice', r['lines'][-1])

    def test_steady_and_no_prior(self):
        cur = [night(d, rhr=50 + (i % 2) * 0.5, hrv=80) for i, d in enumerate(days(dt.date(2026, 11, 23), 5))]
        r = W.summarize(cur, END)
        self.assertIsNone(r['rhr']['dir'])
        self.assertIn('no prior week', r['text'])
        prior = [night(d, rhr=50.5, hrv=81) for d in days(dt.date(2026, 11, 16), 3)]
        r = W.summarize(prior + cur, END)
        self.assertEqual(r['rhr']['dir'], 'steady')
        self.assertEqual(r['hrv']['dir'], 'steady')

    def test_bedtime_wraps_midnight(self):
        beds = ['23:30', '00:30', '23:45', '00:15']
        cur = [night(d, bed=b) for d, b in zip(days(dt.date(2026, 11, 26), 4), beds)]
        r = W.summarize(cur, END)
        self.assertLess(r['bedtime']['sd_min'], 40)       # not ~11 h because of the wrap
        self.assertEqual(r['bedtime']['earliest'], '23:30')
        self.assertEqual(r['bedtime']['latest'], '00:30')
        self.assertEqual(r['bedtime']['avg'], '00:00')

    def test_variable_bedtime_and_sparse(self):
        cur = [night('2026-11-24', bed='21:00'), night('2026-11-27', bed='02:00')]
        r = W.summarize(cur, END)
        self.assertEqual(r['bedtime']['label'], 'variable')
        self.assertTrue(any('few more nights' in x for x in r['lines']))

    def test_single_night(self):
        r = W.summarize([night('2026-11-29')], END)
        self.assertIsNone(r['worst'])
        self.assertTrue(any(x.startswith('Only night') for x in r['lines']))
        self.assertLessEqual(len(r['lines']), W.MAX_LINES)

    def test_window_bounds(self):
        ns = [night('2026-11-22'), night('2026-11-23'), night('2026-11-30')]
        r = W.summarize(ns, END)
        self.assertEqual(r['nights'], 1)
        self.assertEqual(r['prior_nights'], 1)


if __name__ == '__main__':
    unittest.main()
