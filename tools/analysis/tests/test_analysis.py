"""Synthetic-data tests for tools/analysis (stdlib unittest; no real health data).

Run from repo root:
    python3 -I -m unittest discover -s tools/analysis/tests -v
"""
import json
import math
import os
import random
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import baselines as B  # noqa: E402
import common as C  # noqa: E402
import nights as N  # noqa: E402
import sleep_note as SN  # noqa: E402
import stages as S  # noqa: E402

U0 = 1_790_000_000 - 4 * 3600   # ring ds=0 maps here (~23:00 local the evening before)
BED_DS, HOURS = 18_000, 7.5     # bed 30 min after ring epoch, 7.5 h in bed
CYCLE_MIN = 90


def truth(t_min):
    """Synthetic ground-truth stage at minutes since bed."""
    if t_min < 15 or t_min > HOURS * 60 - 15:
        return S.WAKE
    s = t_min - 15
    c, pos = int(s // CYCLE_MIN), s % CYCLE_MIN
    if c <= 1 and 20 <= pos < 55:
        return S.DEEP
    if c >= 2 and 60 <= pos < 85:
        return S.REM
    if c == 1 and 65 <= pos < 75:
        return S.REM
    return S.LIGHT


def make_db(path, seed=1, drift=1.0002, hr_offset=0.0, temp_offset=0.0, bed_ds=BED_DS):
    rnd = random.Random(seed)
    con = sqlite3.connect(path)
    con.execute('''CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, serial TEXT NOT NULL,
        tag INTEGER NOT NULL, name TEXT NOT NULL, ring_timestamp INTEGER NOT NULL, body BLOB NOT NULL,
        decoded_json TEXT, captured_unix INTEGER NOT NULL, UNIQUE(serial, tag, ring_timestamp, body))''')
    rows = []

    def add(name, ds, d):
        rows.append(('S', 0, name, int(ds), json.dumps(d).encode(), json.dumps(d), 0))

    end_ds = bed_ds + int(HOURS * 36000)
    # time_sync anchors: ring clock runs slightly fast (drift)
    add('time_sync', 100, {'unix_time': U0 + 10})
    add('time_sync', end_ds + 3000, {'unix_time': int(U0 + 10 + (end_ds + 2900) / 10 / drift)})
    add('bedtime_period', end_ds + 10, {'bedtime_start_ds': bed_ds, 'bedtime_end_ds': end_ds - 600, 'duration_hours': 7.4})
    add('bedtime_period', end_ds + 20, {'bedtime_start_ds': bed_ds, 'bedtime_end_ds': end_ds, 'duration_hours': HOURS})
    # 30-s epochs
    ds, beat_buf, temp_buf = bed_ds, [], []
    hr_slow = 0.0
    while ds < end_ds:
        tm = (ds - bed_ds) / 600.0
        stg = truth(tm)
        base = 60 - 4 * tm / (HOURS * 60) + hr_offset
        if stg == S.WAKE:
            hr, jit, mad, ms = base + 12, 30, 0.2, 20
        elif stg == S.DEEP:
            hr, jit, mad, ms = base - 6, 60, 0.005, 0
        elif stg == S.REM:
            hr_slow = 0.7 * hr_slow + rnd.gauss(0, 3)
            hr, jit, mad, ms = base + 4 + hr_slow, 15, 0.006, 0
        else:
            hr, jit, mad, ms = base + rnd.gauss(0, 0.8), 35, 0.006, 0
        if stg != S.WAKE and rnd.random() < 0.01:
            mad, ms = 0.05, 3  # brief movement in sleep
        add('sleep_acm_period', ds + 300, {'acm_mad': [20 + rnd.random(), 25, 1, mad, mad * 2, mad / 2]})
        if ms:
            add('motion_event', ds + 300, {'motion_seconds': ms, 'orientation': 3})
        # beats for this 30 s, grouped 6 per ibi event
        t = 0.0
        while t < 30.0:
            ibi = 60000.0 / hr + rnd.choice((-1, 1)) * jit / 2 + rnd.gauss(0, 5)
            beat_buf.append(int(ibi))
            t += ibi / 1000.0
            if len(beat_buf) == 6:
                add('ibi_and_amplitude_event', ds + int(t * 10), {'ibi_ms': beat_buf, 'hr_bpm': [round(60000 / b) for b in beat_buf],
                                                                 'amplitude': [900] * 6})
                beat_buf = []
        tp = 35.0 + temp_offset + (0.4 if stg == S.DEEP else 0.0) + rnd.gauss(0, 0.03)
        temp_buf.append(round(tp, 2))
        if len(temp_buf) == 7:
            add('sleep_temp_event', ds + 300, {'temps_c': temp_buf})
            temp_buf = []
        ds += 300
    # hrv_event every 30 min with 6 x 5-min samples
    for e in range(bed_ds + 18000, end_ds + 1, 18000):
        add('hrv_event', e, {'hr_bpm': [int(55 + hr_offset)] * 6, 'interval_min': 5,
                             'rmssd_ms': [int(60 - hr_offset * 2)] * 6})
    con.executemany('INSERT OR IGNORE INTO events (serial, tag, name, ring_timestamp, body, decoded_json, captured_unix) '
                    'VALUES (?,?,?,?,?,?,?)', rows)
    con.commit()
    con.close()


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = os.path.join(cls.tmp.name, 'syn.db')
        make_db(cls.db)
        cls.res, cls.clock = N.analyse(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()


class TestClockAndSegmentation(Base):
    def test_clock_single_anchor_and_interp(self):
        c = C.RingClock([(0, 1000.0)])
        self.assertEqual(c.unix(100), 1010.0)
        c = C.RingClock([(0, 1000.0), (1000, 1100.0), (2000, 1210.0)])
        self.assertAlmostEqual(c.unix(500), 1050.0)
        self.assertAlmostEqual(c.unix(1500), 1155.0)
        self.assertAlmostEqual(c.unix(3000), 1320.0)  # extrapolate with last segment rate

    def test_clock_drops_backwards_anchor(self):
        c = C.RingClock([(0, 1000.0), (1000, 900.0)])
        self.assertEqual(len(c.warnings), 1)

    def test_bedtime_dedupe(self):
        self.assertEqual(len(self.res), 1)
        m = self.res[0][0]
        self.assertAlmostEqual(m['time_in_bed_min'], HOURS * 60, delta=1.0)
        self.assertEqual(m['kind'], 'night')

    def test_readonly(self):
        con = C.open_ro(self.db)
        with self.assertRaises(sqlite3.OperationalError):
            con.execute('DELETE FROM events')
        con.close()


class TestSignalsAndMetrics(Base):
    def test_hr_recovered(self):
        m, sig, _ = self.res[0]
        self.assertGreater(m['coverage_pct']['hr'], 95)
        self.assertGreater(m['coverage_pct']['acm'], 95)
        self.assertTrue(48 < m['hr_low'] < 56, m['hr_low'])
        self.assertTrue(53 < m['hr_median'] < 60, m['hr_median'])

    def test_rmssd_temp_movement(self):
        m = self.res[0][0]
        self.assertEqual(m['rmssd_avg'], 60)
        self.assertGreater(m['rmssd_samples'], 50)
        self.assertTrue(34.9 < m['temp_avg_c'] < 35.3)
        self.assertTrue(3 < m['movement_index_pct'] < 15, m['movement_index_pct'])


class TestStages(Base):
    def test_agreement_with_truth(self):
        m, sig, stg = self.res[0]
        self.assertTrue(stg['ok'])
        ep = stg['epochs30']
        tr = [truth(k * 0.5 + (sig['t0'] - sig['bed_u']) / 60) for k in range(len(ep))]
        acc = sum(a == b for a, b in zip(ep, tr)) / len(ep)
        self.assertGreater(acc, 0.7, f'accuracy {acc:.2f}')
        wake_tr = [t == S.WAKE for t in tr]
        wake_ok = sum((a == S.WAKE) == w for a, w in zip(ep, wake_tr)) / len(ep)
        self.assertGreater(wake_ok, 0.95)

    def test_sanity_checks(self):
        stg = self.res[0][2]
        byname = {c['check']: c for c in stg['checks']}
        self.assertTrue(byname['deep in first half']['pass'])
        self.assertTrue(byname['REM in second half']['pass'])
        self.assertTrue(byname['first REM after onset']['pass'])
        self.assertTrue(stg['latency_min'] >= 10)

    def test_aggregate_5min_rule(self):
        W, R, L, D = S.WAKE, S.REM, S.LIGHT, S.DEEP
        self.assertEqual(S.aggregate_5min([W] * 4 + [L] * 6), [W])
        self.assertEqual(S.aggregate_5min([D] * 5 + [L] * 5), [D])  # tie -> deep
        self.assertEqual(S.aggregate_5min([R] * 5 + [L] * 5 + [L] * 4), [R])  # short tail dropped
        self.assertEqual(S.aggregate_5min([L] * 10 + [R] * 6), [L, R])

    def test_short_night_rejected(self):
        sig = {'n': 50, 'hr': [55] * 50}
        self.assertFalse(S.stage_night(sig)['ok'])

    def test_self_eval_text(self):
        m, _, stg = self.res[0]
        txt = S.self_eval_text(m, stg)
        self.assertIn('not validated', txt)
        self.assertIn('sanity checks', txt)


def fake_nights(k, rhr=50.0, rmssd=80.0, temp=35.1, jitter=True):
    out = []
    for i in range(k):
        j = (i % 3 - 1) if jitter else 0
        out.append({'date': f'2026-10-{i + 1:02d}', 'kind': 'night', 'bed': f'2026-10-{i + 1:02d}T23:00:00-04:00',
                    'wake': f'2026-10-{i + 2:02d}T07:00:00-04:00' if i < 29 else '2026-10-31T07:00:00-04:00',
                    'bed_unix': 1_790_000_000 + i * 86400, 'time_in_bed_min': 480, 'sleep_min_est': 430,
                    'hr_low': rhr + j, 'rmssd_avg': rmssd + 3 * j, 'temp_avg_c': temp + 0.05 * j,
                    'stages': {'ok': True, 'minutes': {'awake': 50, 'rem': 90, 'light': 250, 'deep': 90}}})
    return out


class TestBaselines(unittest.TestCase):
    def test_single_night(self):
        ev = B.evaluate(fake_nights(1))
        for m in ev['metrics'].values():
            self.assertEqual(m['status'], 'insufficient')
            self.assertEqual(m['need_more'], B.MIN_NIGHTS)
        self.assertIsNone(ev['metrics']['temp_dev']['value'])

    def test_no_nights(self):
        self.assertEqual(B.evaluate([])['nights_available'], 0)

    def test_flags(self):
        ns = fake_nights(20)
        ns[-1].update(hr_low=60, rmssd_avg=40, temp_avg_c=35.9)
        ev = B.evaluate(ns)
        self.assertEqual(ev['metrics']['rhr']['status'], 'high')
        self.assertEqual(ev['metrics']['rmssd']['status'], 'low')
        self.assertEqual(ev['metrics']['temp_dev']['status'], 'high')
        self.assertAlmostEqual(ev['metrics']['temp_dev']['value'], 0.8, places=2)

    def test_within_range_and_mad_floor(self):
        ns = fake_nights(10, jitter=False)        # MAD = 0 -> floors apply
        ns[-1].update(hr_low=51.5, temp_avg_c=35.25)
        ev = B.evaluate(ns)
        self.assertEqual(ev['metrics']['rhr']['status'], 'ok')
        self.assertEqual(ev['metrics']['temp_dev']['status'], 'ok')

    def test_windows_and_history(self):
        ns = fake_nights(40)
        ev = B.evaluate(ns)
        self.assertEqual(ev['metrics']['rhr']['windows']['14']['n'], 14)
        self.assertEqual(ev['metrics']['rhr']['windows']['30']['n'], 30)
        self.assertEqual(len(B.history(ns)), 40)

    def test_naps_ignored(self):
        ns = fake_nights(6)
        ns.append(dict(ns[-1], kind='nap', hr_low=90))
        self.assertEqual(B.evaluate(ns)['metrics']['rhr']['status'], 'ok')


class TestSleepNote(unittest.TestCase):
    def test_note_first_night(self):
        note = SN.build_note(fake_nights(1))
        lines = note['text'].split('\n')
        self.assertLessEqual(len(lines), 5)
        self.assertIn('estimated', note['text'])
        self.assertIn('more night', note['text'])
        self.assertIn('not medical advice', note['text'])

    def test_note_flags(self):
        ns = fake_nights(20)
        ns[-1].update(hr_low=60, temp_avg_c=35.6)
        txt = SN.build_note(ns)['text']
        self.assertIn('resting HR was higher than your usual', txt)
        self.assertIn('warmer', txt)
        self.assertLessEqual(len(txt.split('\n')), 5)
        for bad in ('fever', 'ill', 'sick', 'diagnos'):
            self.assertNotIn(bad, txt.lower())

    def test_note_empty(self):
        self.assertIn('No night', SN.build_note([])['text'])

    def test_note_from_synthetic_db(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            db = os.path.join(tmp.name, 's.db')
            make_db(db, seed=3)
            out = os.path.join(tmp.name, 'nightly.json')
            N.main(['--db', db, '--out', out, '--quiet'])
            doc = C.read_json(out)
            self.assertEqual(len(doc['nights']), 1)
            self.assertEqual(oct(os.stat(out).st_mode & 0o777), '0o600')
            note = SN.build_note(doc['nights'])
            self.assertIn('Stages (estimated)', note['text'])
            json.dumps(note)
        finally:
            tmp.cleanup()


class TestMerge(unittest.TestCase):
    def test_merge_keeps_history(self):
        old = fake_nights(3)
        new = [dict(old[-1], hr_low=99)]
        merged = N.merge(old, new)
        self.assertEqual(len(merged), 3)
        self.assertEqual(merged[-1]['hr_low'], 99)


class TestStats(unittest.TestCase):
    def test_robust(self):
        self.assertEqual(C.median([3, None, 1, 2]), 2)
        self.assertAlmostEqual(C.mad([1, 2, 3, 4, 100]), 1.4826)
        self.assertAlmostEqual(C.quantile([0, 10], 0.25), 2.5)
        self.assertTrue(math.isclose(C.robust_z([1, 2, 3])[1], 0.0))


if __name__ == '__main__':
    unittest.main()
