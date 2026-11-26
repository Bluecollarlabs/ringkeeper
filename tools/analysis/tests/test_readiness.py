"""Tests for readiness.py (synthetic nights only)."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import readiness as R  # noqa: E402


def night(i, rhr=50.0, hrv=70.0, sleep=480, eff=92.0, temp=35.0, kind='night'):
    return {'date': f'2026-11-{i + 1:02d}', 'kind': kind, 'bed_unix': 1795000000 + i * 86400,
            'hr_low': rhr, 'rmssd_avg': hrv, 'sleep_min_est': sleep, 'time_in_bed_min': sleep + 30,
            'efficiency_pct_est': eff, 'temp_avg_c': temp}


def series(n, **kw):
    # small night-to-night wobble so MADs are not degenerate
    return [night(i, rhr=50 + (i % 3 - 1) * 0.5, hrv=70 + (i % 3 - 1) * 2, temp=35 + (i % 3 - 1) * 0.03)
            for i in range(n)]


def contrib(res, key):
    return next(c for c in res['contributors'] if c['key'] == key)


class TestReadiness(unittest.TestCase):
    def test_no_data(self):
        self.assertEqual(R.compute([])['status'], 'no_data')

    def test_first_night_is_calibrating_provisional(self):
        r = R.compute([night(0)])
        self.assertEqual(r['status'], 'calibrating')
        self.assertTrue(r['provisional'])
        self.assertEqual(r['need_more'], 5)
        self.assertIn('not Oura', r['label'])
        self.assertEqual(contrib(r, 'rhr')['basis'], 'absolute')
        self.assertFalse(contrib(r, 'temp')['available'])  # no absolute fallback for temp
        self.assertFalse(contrib(r, 'activity')['available'])
        self.assertTrue(0 <= r['score'] <= 100)

    def test_baseline_ready_typical_night_scores_high(self):
        ns = series(10) + [night(10)]
        r = R.compute(ns)
        self.assertEqual(r['status'], 'ok')
        self.assertFalse(r['provisional'])
        self.assertEqual(contrib(r, 'hrv')['basis'], 'baseline')
        self.assertTrue(contrib(r, 'temp')['available'])
        self.assertGreaterEqual(r['score'], 85)

    def test_bad_night_scores_lower_and_breakdown_sums(self):
        base = series(10)
        good = R.compute(base + [night(10)])
        bad = R.compute(base + [night(10, rhr=58, hrv=45, sleep=300, eff=75, temp=35.6)])
        self.assertLess(bad['score'], good['score'] - 30)
        pts = sum(c['points'] for c in bad['contributors'] if c['available'])
        self.assertAlmostEqual(pts, bad['score'], delta=1.0)
        self.assertAlmostEqual(sum(c['share_pct'] for c in bad['contributors'] if c['available']), 100, delta=0.5)
        self.assertLess(contrib(bad, 'temp')['score'], 50)

    def test_sleep_need(self):
        self.assertEqual(R.score_sleep({'sleep_min_est': 480})['score'], 100)
        self.assertAlmostEqual(R.score_sleep({'sleep_min_est': 240})['score'], 25)
        self.assertAlmostEqual(R.score_sleep({'sleep_min_est': 420}, need_h=7)['score'], 100)
        self.assertIsNone(R.score_sleep({}))
        self.assertIn('time in bed', R.score_sleep({'time_in_bed_min': 400})['detail'])

    def test_activity(self):
        self.assertIsNone(R.score_activity({'bins': 10, 'mean_met': 2, 'high_pct': 0}))
        self.assertEqual(R.score_activity({'bins': 100, 'mean_met': 1.8, 'high_pct': 0})['score'], 100)
        self.assertLess(R.score_activity({'bins': 100, 'mean_met': 3.5, 'high_pct': 30})['score'], 60)
        r = R.compute([night(0)], activity={'bins': 100, 'mean_met': 1.8, 'high_pct': 2})
        self.assertTrue(contrib(r, 'activity')['available'])

    def test_naps_ignored_and_history(self):
        ns = series(6) + [night(6, kind='nap', rhr=80)]
        r = R.compute(ns)
        self.assertEqual(r['date'], '2026-11-06')
        h = R.history(ns)
        self.assertEqual(len(h), 6)
        self.assertTrue(h[0]['provisional'])
        self.assertFalse(h[-1]['provisional'])

    def test_prev_day_activity_missing_db(self):
        self.assertIsNone(R.prev_day_activity('/nonexistent.db', 1795000000))


if __name__ == '__main__':
    unittest.main()
