"""Tests for tools/privacy.py + /api/export, /api/delete in tools/dash_api.py.

Synthetic temp tree only (fake data/, server/data/ingest.db, trash) -- never the real data.
"""
import csv
import http.client
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
import dash_api as D  # noqa: E402
import privacy as P  # noqa: E402

SCHEMA = """
CREATE TABLE device (serial TEXT PRIMARY KEY, hardware_id TEXT, firmware TEXT, api_version TEXT, mac TEXT, updated_unix INTEGER NOT NULL);
CREATE TABLE sync_state (serial TEXT PRIMARY KEY, next_cursor INTEGER NOT NULL, last_sync_unix INTEGER NOT NULL);
CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, serial TEXT NOT NULL, tag INTEGER NOT NULL, name TEXT NOT NULL,
    ring_timestamp INTEGER NOT NULL, body BLOB NOT NULL, decoded_json TEXT, captured_unix INTEGER NOT NULL,
    UNIQUE(serial, tag, ring_timestamp, body));
CREATE TABLE readings (id INTEGER PRIMARY KEY AUTOINCREMENT, serial TEXT NOT NULL, kind TEXT NOT NULL, value REAL NOT NULL,
    unit TEXT, captured_unix INTEGER NOT NULL);
"""


def rd(path):
    with open(path) as f:
        return f.read()


def make_db(path, serials=('S1',), with_device=True):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    for s in serials:
        if with_device:
            con.execute("INSERT INTO device VALUES (?, NULL, '2.1', '2.1', NULL, 0)", (s,))
        con.execute('INSERT INTO sync_state VALUES (?, 5, 100)', (s,))
        con.execute("INSERT INTO events (serial, tag, name, ring_timestamp, body, decoded_json, captured_unix) "
                    "VALUES (?, 66, 'time_sync', 1000, x'01', '{\"unix_time\": 1795000000}', 1)", (s,))
        con.execute("INSERT INTO events (serial, tag, name, ring_timestamp, body, decoded_json, captured_unix) "
                    "VALUES (?, 96, 'ibi_and_amplitude_event', 1100, x'abcd', NULL, 1)", (s,))
        con.execute("INSERT INTO readings (serial, kind, value, unit, captured_unix) VALUES (?, 'battery_percent', 70, '%', 1)", (s,))
    con.commit()
    con.close()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        r = self.root = self.tmp.name
        for d in ('data/logs', 'server/data', 'ui-mockup', 'secrets'):
            os.makedirs(os.path.join(r, d))
        make_db(os.path.join(r, 'data', 'oura.db'))
        make_db(os.path.join(r, 'server', 'data', 'ingest.db'), serials=('S1', 'S2'), with_device=False)
        for name, obj in (('nightly.json', {'nights': [{'date': '2026-11-25'}]}), ('source.json', {'mode': 'pc'}),
                          ('cycle_log.json', []), ('logs/x.log', 'log')):
            with open(os.path.join(r, 'data', name), 'w') as f:
                f.write(obj if isinstance(obj, str) else json.dumps(obj))
        with open(os.path.join(r, 'ui-mockup', 'real-data.json'), 'w') as f:
            f.write('{}')
        self.paths = P.Paths(root=r, trash_home=os.path.join(r, 'Trash'))
        self.pin_file = os.path.join(r, 'secrets', 'dashboard_pin')

    def tearDown(self):
        self.tmp.cleanup()


class TestPrivacy(Base):
    def test_export_zip(self):
        fh, info = P.build_export(self.paths)
        z = zipfile.ZipFile(fh)
        names = set(z.namelist())
        self.assertEqual(names, {'README.txt', 'events.csv', 'nightly.json', 'cycle_log.json'})
        rows = list(csv.DictReader(io.StringIO(z.read('events.csv').decode())))
        self.assertEqual(len(rows), 2)
        self.assertEqual(info['rows'], 2)
        self.assertEqual(rows[1]['body_hex'], 'abcd')
        self.assertEqual(float(rows[1]['unix_time']), 1795000010.0)  # +100 ds
        self.assertIn('ringkeeper data export', z.read('README.txt').decode())
        fh.close()

    def test_delete_wrong_phrase(self):
        with self.assertRaises(ValueError):
            P.delete_data('delete my data', self.paths)
        self.assertTrue(os.path.exists(os.path.join(self.root, 'data', 'oura.db')))

    def test_delete_moves_everything_recoverably(self):
        res = P.delete_data(P.CONFIRM_PHRASE, self.paths)
        dest = res['trash_dir']
        self.assertTrue(dest.startswith(os.path.join(self.root, 'Trash', 'files')))
        self.assertTrue(os.path.exists(os.path.join(self.root, 'Trash', 'info', os.path.basename(dest) + '.trashinfo')))
        # moved, not deleted
        for name in ('oura.db', 'nightly.json', 'cycle_log.json', 'logs'):
            self.assertFalse(os.path.exists(os.path.join(self.root, 'data', name)), name)
            self.assertTrue(os.path.exists(os.path.join(dest, 'data', name)), name)
        self.assertTrue(os.path.exists(os.path.join(self.root, 'data', 'source.json')))  # config kept
        self.assertFalse(os.path.exists(os.path.join(self.root, 'ui-mockup', 'real-data.json')))
        self.assertTrue(os.path.exists(os.path.join(dest, 'real-data.json')))
        # ingest rows for S1 moved, S2 untouched, ingest.db still in place
        self.assertEqual(res['serials'], ['S1'])
        live = sqlite3.connect(self.paths.ingest)
        self.assertEqual(live.execute('SELECT DISTINCT serial FROM events').fetchall(), [('S2',)])
        live.close()
        t = sqlite3.connect(os.path.join(dest, 'ingest-rows.db'))
        self.assertEqual(t.execute('SELECT COUNT(*) FROM events WHERE serial = "S1"').fetchone()[0], 2)
        t.close()
        self.assertEqual(res['ingest_rows_moved']['events'], 2)
        self.assertTrue(os.path.exists(os.path.join(dest, 'RESTORE.txt')))
        man = json.loads(rd(os.path.join(dest, 'manifest.json')))
        self.assertIn('keep_until', man)
        self.assertEqual(oct(os.stat(dest).st_mode & 0o777), '0o700')

    def test_delete_fallback_and_lock(self):
        bad = P.Paths(root=self.root, trash_home='/proc/nope-trash')
        import fcntl
        with open(bad.lock, 'a') as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            with self.assertRaises(BlockingIOError):
                P.delete_data(P.CONFIRM_PHRASE, bad)
        res = P.delete_data(P.CONFIRM_PHRASE, bad)
        self.assertIn(os.path.join('data', '.deleted-'), res['trash_dir'])
        self.assertTrue(os.path.exists(os.path.join(res['trash_dir'], 'data', 'oura.db')))

    def test_pin(self):
        path, created = P.gen_pin(self.pin_file)
        self.assertTrue(created)
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), '0o600')
        pin = rd(path).strip()
        self.assertRegex(pin, r'^\d{6}$')
        self.assertTrue(P.check_pin(pin, path))
        self.assertFalse(P.check_pin('000000' if pin != '000000' else '111111', path))
        self.assertFalse(P.gen_pin(path)[1])
        os.chmod(path, 0o644)
        self.assertIsNone(P.load_pin(path))  # loose perms -> delete disabled


class TestEndpoints(Base):
    def setUp(self):
        super().setUp()
        P.gen_pin(self.pin_file)
        self.pin = rd(self.pin_file).strip()
        www = os.path.join(self.root, 'ui-mockup')
        self.srv = D.make_server('127.0.0.1', 0, www, ask_fn=lambda q: {}, priv={
            'paths': self.paths, 'pin_file': self.pin_file,
            'pin_limiter': D.RateLimiter(limits=((2, 900),))})
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data, r

    def delete(self, doc, **h):
        s, d, _ = self.req('POST', '/api/delete', json.dumps(doc), {'Content-Type': 'application/json'} | h)
        return s, json.loads(d)

    def test_export_endpoint(self):
        s, d, r = self.req('GET', '/api/export')
        self.assertEqual(s, 200)
        self.assertEqual(r.getheader('Content-Type'), 'application/zip')
        self.assertIn('attachment', r.getheader('Content-Disposition'))
        self.assertIn('events.csv', zipfile.ZipFile(io.BytesIO(d)).namelist())

    def test_delete_guards(self):
        self.assertEqual(self.delete({'confirm': 'yes', 'pin': self.pin})[0], 400)
        self.assertEqual(self.delete({'confirm': P.CONFIRM_PHRASE, 'pin': self.pin}, Origin='https://evil.example')[0], 403)
        wrong = '000000' if self.pin != '000000' else '111111'
        self.assertEqual(self.delete({'confirm': P.CONFIRM_PHRASE, 'pin': wrong})[0], 403)
        self.assertEqual(self.delete({'confirm': P.CONFIRM_PHRASE, 'pin': wrong})[0], 403)
        s, j = self.delete({'confirm': P.CONFIRM_PHRASE, 'pin': self.pin})
        self.assertEqual(s, 429)  # locked out after 2 failures, even with the right PIN
        self.assertTrue(os.path.exists(os.path.join(self.root, 'data', 'oura.db')))
        s, _, _ = self.req('POST', '/api/delete', 'confirm=x', {'Content-Type': 'application/x-www-form-urlencoded'})
        self.assertEqual(s, 415)

    def test_delete_ok(self):
        s, j = self.delete({'confirm': P.CONFIRM_PHRASE, 'pin': self.pin})
        self.assertEqual(s, 200, j)
        self.assertTrue(j['ok'])
        self.assertIn('data/oura.db', j['moved'])
        self.assertFalse(os.path.exists(os.path.join(self.root, 'data', 'oura.db')))

    def test_delete_disabled_without_pin(self):
        os.chmod(self.pin_file, 0o640)
        self.assertEqual(self.delete({'confirm': P.CONFIRM_PHRASE, 'pin': self.pin})[0], 503)


if __name__ == '__main__':
    unittest.main()
