"""Tests for day.py (synthetic events only, no real health data)."""
import datetime as dt
import json
import os
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import common as C  # noqa: E402
import day as D  # noqa: E402
import readiness as RD  # noqa: E402
import weekly as W  # noqa: E402

DAY = dt.date(2026, 11, 20)
U0 = dt.datetime(2026, 11, 20, 0, 0, tzinfo=C.TZ).timestamp()   # local midnight
NOPROF = '/nonexistent/profile.json'


def ds(u):
    """unix -> ring deciseconds (anchor: ring 0 == U0)."""
    return int(round((u - U0) * 10))


def at(h, m=0):
    return U0 + h * 3600 + m * 60


class Ev:
    def __init__(self):
        self.ev = {'time_sync': [(0, {'unix_time': U0}), (ds(U0 + 3 * 86400), {'unix_time': U0 + 3 * 86400})]}

    def add(self, name, u, d):
        self.ev.setdefault(name, []).append((ds(u), d))
        self.ev[name].sort(key=lambda x: x[0])
        return self

    def met_run(self, start_u, mets):
        """Emit activity_information events (13 bins, end-stamped) covering start_u.. with given MET list."""
        for i in range(0, len(mets), 13):
            chunk = mets[i:i + 13]
            self.add('activity_information', start_u + (i + len(chunk)) * 60, {'state': 0, 'met': chunk})
        return self

    def beats(self, start_u, end_u, ibi_ms, amp=1000):
        t = start_u
        while t < end_u:
            ibis = [ibi_ms] * 6
            t += sum(ibis) / 1000
            self.add('ibi_and_amplitude_event', t, {'ibi_ms': ibis, 'amplitude': [amp] * 6})
        return self

    def clock(self):
        return C.RingClock.from_events(self.ev)


def night(bed_u, wake_u, date='2026-11-21'):
    return {'date': date, 'kind': 'night', 'bed_unix': bed_u, 'wake_unix': wake_u,
            'bed': C.iso(bed_u), 'wake': C.iso(wake_u)}


def run(e, nights=(), dates=(DAY,), profile=NOPROF):
    doc, cx = D.analyse_ev(e.ev, e.clock(), list(nights), profile, dates=list(dates))
    return doc, cx


class TestMet(unittest.TestCase):
    def test_bins_are_end_stamped_minutes(self):
        e = Ev().add('activity_information', at(10, 13), {'state': 0, 'met': [1.0] * 12 + [5.0]})
        mins, _ = D.met_minutes(e.ev, e.clock())
        self.assertEqual(min(mins), int(at(10, 0)))
        self.assertEqual(max(mins), int(at(10, 12)))
        self.assertEqual(mins[int(at(10, 12))], 5.0)

    def test_steps_active_minutes_and_sleep_exclusion(self):
        e = Ev().met_run(at(9), [4.0] * 26 + [8.0] * 13 + [1.2] * 13)   # 26 min walk, 13 min run
        e.met_run(at(23), [4.0] * 26)                                     # inside sleep window -> excluded
        n = night(at(22, 30), at(30))
        doc, _ = run(e, [n])
        d = doc['days'][0]
        self.assertEqual(d['steps']['value'], 26 * 105 + 13 * 150)
        self.assertTrue(d['steps']['estimated'])
        self.assertEqual(d['active_min'], 39)
        self.assertEqual(d['intensity_min'], {'sedentary': 13, 'low': 0, 'medium': 26, 'high': 13})
        self.assertEqual(d['recorded_min'], 52)
        self.assertEqual(d['hourly']['steps'][9], 26 * 105 + 13 * 150 - 0)  # 09:00-09:51 all in hour 9
        self.assertEqual(sum(d['hourly']['steps']), d['steps']['value'])
        self.assertEqual(d['sleep_min_excluded'], 90)
        self.assertIsNone(d['active_kcal'])                    # no weight
        self.assertEqual(d['last_activity'][11:16], '09:39')
        bouts = d['activity_bouts']
        self.assertEqual(len(bouts), 1)
        self.assertEqual(bouts[0]['duration_min'], 39)

    def test_weight_profile_and_firmware_default(self):
        e = Ev().met_run(at(12), [4.0] * 13)
        e.add('user_information', at(1), {'age_years': 40, 'weight_kg': 75, 'sex_code': 2, 'height_cm': 176})
        doc, _ = run(e)
        self.assertIsNone(doc['days'][0]['active_kcal'])
        self.assertIn('firmware default', doc['weight_source'])
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, 'profile.json')
            with open(p, 'w') as f:
                json.dump({'weight_kg': 80}, f)
            doc, _ = run(e, profile=p)
        self.assertEqual(doc['days'][0]['active_kcal'], round(13 * 3.0 * 80 / 60))
        e2 = Ev().met_run(at(12), [4.0] * 13).add('user_information', at(1), {'age_years': 33, 'weight_kg': 60, 'sex_code': 1, 'height_cm': 165})
        doc, _ = run(e2)
        self.assertEqual(doc['days'][0]['weight_kg'], 60.0)

    def test_dst_day_has_25_hours(self):
        from unittest import mock
        import zoneinfo
        # pin a zone with a known DST change so the test doesn't depend on the machine's zone
        with mock.patch.object(C, 'TZ', zoneinfo.ZoneInfo('Europe/Berlin')):
            a, b = D.day_bounds(dt.date(2026, 10, 25))   # DST ends in central Europe
        self.assertEqual(b - a, 25 * 3600)


class TestStepsSources(unittest.TestCase):
    def test_gait_windows_and_ring_counter_reported_not_headline(self):
        e = Ev().met_run(at(14), [3.0] * 13)
        e.ev['activity_information'][0][1]['state'] = 120
        for k in range(20):
            e.add('real_step_event_feature_1', at(14, 1) + k * 30, {'fields': [0] * 14})
            e.add('real_step_event_feature_2', at(14, 1) + k * 30 + 0.1, {})
        doc, _ = run(e)
        s = doc['days'][0]['steps']
        self.assertEqual(s['value'], round(13 * 105, -1))
        self.assertEqual(s['source'], 'met_rule')
        self.assertEqual(s['gait_windows'], 20)
        self.assertEqual(s['gait_walking_min'], 10.0)
        self.assertTrue(s['real_steps_present'])
        self.assertEqual(s['ring_counter'], 120)
        self.assertTrue(doc['sources']['real_steps_0x7e_0x7f']['present'])


class TestHeart(unittest.TestCase):
    def test_daytime_hr_outside_sleep_only(self):
        e = Ev().beats(at(10), at(11), 1000).beats(at(23), at(23, 30), 750)
        doc, _ = run(e, [night(at(22, 30), at(30))])
        h = doc['days'][0]['hr']
        self.assertAlmostEqual(h['avg'], 60.0, delta=0.5)
        self.assertAlmostEqual(h['max'], 60.0, delta=0.5)     # the 80-bpm sleep beats are excluded
        self.assertGreaterEqual(h['samples_5min'], 11)
        self.assertGreaterEqual(doc['days'][0]['wear_min'], 59)

    def test_exercise_sessions_from_ehr_trace_and_feature_session(self):
        e = Ev().beats(at(17), at(17, 40), 500)
        for k in range(30):
            e.add('ehr_trace_event', at(17, 5) + k * 60, {})
        e.add('feature_session', at(17, 2), {'feature_id': 3, 'session_status': 1})
        e.add('feature_session', at(17, 36), {'feature_id': 3, 'session_status': 3})
        e.add('ehr_acm_intensity_event', at(17, 10), {'intensity': [10, 30]})
        e.add('ehr_trace_event', at(19), {})                     # separate short cluster
        doc, _ = run(e)
        ss = doc['days'][0]['exercise_sessions']
        self.assertEqual(len(ss), 2)
        s0 = ss[0]
        self.assertEqual(s0['start'][11:16], '17:02')
        self.assertEqual(s0['end'][11:16], '17:36')
        self.assertAlmostEqual(s0['hr_avg'], 120, delta=1)
        self.assertEqual(s0['intensity_max'], 30)
        self.assertIn('feature_session', s0['source'])


class TestEvening(unittest.TestCase):
    def test_wind_down_trend_and_last_activity(self):
        e = Ev()
        bed = at(23)
        # HR falls 70 -> ~58 over the 3 h before bed (ibi 857 -> 1034 ms in 15-min steps)
        for k in range(12):
            hr = 70 - k
            e.beats(bed - 3 * 3600 + k * 900, bed - 3 * 3600 + (k + 1) * 900, 60000 / hr)
        e.met_run(at(20, 30), [4.0] * 13 + [1.1] * 52)      # walk 20:30-20:43, then still
        for k in range(36):                                     # ring RMSSD rising 30 -> 65
            e.add('hrv_event', bed - 3 * 3600 + (k + 1) * 300, {'hr_bpm': [60], 'rmssd_ms': [30 + k]})
        doc, _ = run(e, [night(bed, at(31))])
        ev = doc['evenings'][0]
        self.assertEqual(ev['date'], '2026-11-20')
        self.assertEqual(ev['hr_trend'], 'settling')
        self.assertLess(ev['hr_slope_bpm_per_h'], -3)
        self.assertGreater(ev['hr_first_hour'], ev['hr_last_hour'])
        self.assertGreater(ev['rmssd_last_hour'], ev['rmssd_first_hour'])
        self.assertEqual(ev['last_activity'][11:16], '20:43')
        self.assertEqual(ev['last_activity_min_before_bed'], 137)
        self.assertEqual(ev['rmssd_source'], 'hrv_event')

    def test_after_midnight_bed_belongs_to_previous_day(self):
        e = Ev()
        doc, _ = run(e, [night(at(24, 40), at(31))])
        self.assertEqual(doc['evenings'][0]['date'], '2026-11-20')
        self.assertTrue(doc['evenings'][0]['partial'])
        self.assertIsNone(doc['evenings'][0]['hr_trend'])


class TestGraceful(unittest.TestCase):
    def test_no_activity_no_hr(self):
        e = Ev()
        doc, _ = run(e)
        d = doc['days'][0]
        self.assertIsNone(d['steps']['value'])
        self.assertIsNone(d['hr']['avg'])
        self.assertEqual(d['exercise_sessions'], [])
        self.assertIn('No daytime', D.describe(None))
        self.assertTrue(D.describe(d))
        doc, _ = D.analyse_ev(e.ev, e.clock(), [], NOPROF)    # auto dates: none
        self.assertEqual(doc['days'], [])


class TestDbAndConsumers(unittest.TestCase):
    def _db(self, td, e):
        p = os.path.join(td, 'syn.db')
        con = sqlite3.connect(p)
        con.execute('CREATE TABLE events (id INTEGER PRIMARY KEY, serial TEXT, tag INTEGER, name TEXT, '
                    'ring_timestamp INTEGER, body BLOB, decoded_json TEXT, captured_unix INTEGER)')
        for name, rows in e.ev.items():
            for ts, d in rows:
                con.execute('INSERT INTO events (serial, tag, name, ring_timestamp, body, decoded_json, captured_unix) '
                            'VALUES (?,?,?,?,?,?,?)', ('SYN', 0, name, ts, b'', json.dumps(d), 0))
        con.commit()
        con.close()
        return p

    def test_build_and_readiness_prev_day(self):
        e = Ev().met_run(at(8), [1.6] * 13 * 30 + [4.0] * 26 + [8.0] * 13)
        bed = at(22, 30)
        with tempfile.TemporaryDirectory() as td:
            db = self._db(td, e)
            doc = D.build(db, nights=[night(bed, at(30))], profile_path=NOPROF)
            self.assertEqual(doc['days'][0]['date'], '2026-11-20')
            act = RD.prev_day_activity(db, bed, nights=[night(bed, at(30))])
        self.assertEqual(act['active_min'], 39)
        self.assertEqual(act['steps_est'], round(26 * 105 + 13 * 150, -1))
        sc = RD.score_activity(act)
        self.assertEqual(sc['score'], 100.0)
        self.assertIn('steps (est.)', sc['detail'])

    def test_readiness_heavy_and_still_days(self):
        heavy = {'bins': 900, 'recorded_min': 900, 'mean_met': 2.0, 'high_pct': 10, 'active_min': 300, 'high_min': 90, 'steps_est': 25000}
        self.assertLess(RD.score_activity(heavy)['score'], 75)
        still = {'bins': 600, 'recorded_min': 600, 'mean_met': 1.4, 'high_pct': 0, 'active_min': 5, 'high_min': 0, 'steps_est': 800}
        self.assertEqual(RD.score_activity(still)['score'], 85.0)

    def test_weekly_activity_line(self):
        nights = [{'date': '2026-11-2%d' % i, 'kind': 'night', 'bed': '2026-11-2%dT23:00:00-05:00' % (i - 1),
                   'sleep_min_est': 420, 'time_in_bed_min': 460, 'hr_low': 50, 'rmssd_avg': 80} for i in range(3, 10)]
        days = [{'date': '2026-11-2%d' % i, 'recorded_min': 600, 'steps': {'value': 6000 + i * 100}, 'active_min': 30,
                 'active_kcal': None} for i in range(3, 10)]
        days.append({'date': '2026-11-22', 'recorded_min': 20, 'steps': {'value': 50}, 'active_min': 0})  # too little data
        r = W.summarize(nights, dt.date(2026, 11, 29), days)
        self.assertEqual(r['activity']['days'], 7)
        self.assertEqual(r['activity']['steps_avg'], 6600)
        self.assertTrue(any(x.startswith('Activity: ~6,600 steps/day (estimated)') for x in r['lines']))
        self.assertLessEqual(len(r['lines']), W.MAX_LINES)
        self.assertIn('not medical advice', r['lines'][-1])


if __name__ == '__main__':
    unittest.main()
