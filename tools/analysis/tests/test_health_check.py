"""Tests for tools/health_check.py (temp files, stub loopback HTTP server; no real data)."""
import http.server
import os
import sqlite3
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
import health_check as H  # noqa: E402

NOW = 1795600000.0


class OK(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header('Content-Length', '2')
        self.end_headers()
        self.wfile.write(b'{}')

    def log_message(self, *a):
        pass


def make_db(path, last_sync, battery=None):
    con = sqlite3.connect(path)
    con.executescript('CREATE TABLE sync_state (serial TEXT, next_cursor INT, last_sync_unix INT);'
                      'CREATE TABLE events (id INTEGER PRIMARY KEY, name TEXT, ring_timestamp INT, decoded_json TEXT);')
    con.execute("INSERT INTO sync_state VALUES ('S', 1, ?)", (last_sync,))
    con.execute("INSERT INTO events (name, ring_timestamp, decoded_json) VALUES ('time_sync', 0, ?)",
                ('{"unix_time": %d}' % (NOW - 7200),))
    if battery is not None:
        con.execute("INSERT INTO events (name, ring_timestamp, decoded_json) VALUES ('debug_data', 600, ?)",
                    ('{"kind":"battery_level_changed","battery_pct":%d,"voltage_mv":3900}' % battery,))
    con.commit()
    con.close()


class TestHealthCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), OK)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        u = f'http://127.0.0.1:{self.srv.server_address[1]}/'
        self.urls = {'ingest': u, 'dashboard': u, 'ollama': u}
        self.db = os.path.join(self.tmp.name, 'oura.db')
        self.bk = os.path.join(self.tmp.name, 'backups')
        os.makedirs(os.path.join(self.bk, '2026-11-24'))
        with open(os.path.join(self.bk, '2026-11-24', 'SHA256SUMS'), 'w'):
            pass
        os.utime(os.path.join(self.bk, '2026-11-24', 'SHA256SUMS'), (NOW - 3600, NOW - 3600))

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def run_check(self, **kw):
        args = dict(now=NOW, cfg={'mode': 'pc'}, urls=self.urls, backup_dir=self.bk, pc_db=self.db,
                    ingest=os.path.join(self.tmp.name, 'none.db'), disk_paths=[self.tmp.name])
        return H.run(**(args | kw))

    def test_all_ok(self):
        make_db(self.db, NOW - 3600, battery=80)
        r = self.run_check()
        self.assertTrue(r['ok'], r['issues'])
        self.assertEqual(r['checks']['battery']['pct'], 80)

    def test_issues(self):
        make_db(self.db, NOW - 30 * 3600, battery=12)
        os.utime(os.path.join(self.bk, '2026-11-24', 'SHA256SUMS'), (NOW - 3 * 86400,) * 2)
        urls = self.urls | {'ollama': 'http://127.0.0.1:9/'}
        r = self.run_check(urls=urls)
        self.assertFalse(r['ok'])
        got = {i['check'] for i in r['issues']}
        self.assertEqual(got, {'data_age', 'battery', 'ollama', 'backups'})

    def test_no_battery_is_not_an_issue_and_no_data_is(self):
        r = self.run_check()  # db missing
        self.assertEqual({i['check'] for i in r['issues']}, {'data_age'})
        self.assertIsNone(r['checks']['battery']['pct'])

    def test_phone_mode_no_uploads(self):
        make_db(self.db, NOW - 60)
        r = self.run_check(cfg={'mode': 'phone'})
        self.assertIn('data_age', {i['check'] for i in r['issues']})
        self.assertEqual(r['checks']['data_age']['mode'], 'phone')

    def test_no_backups(self):
        make_db(self.db, NOW - 60)
        r = self.run_check(backup_dir=os.path.join(self.tmp.name, 'nope'))
        self.assertEqual({i['check'] for i in r['issues']}, {'backups'})


if __name__ == '__main__':
    unittest.main()
