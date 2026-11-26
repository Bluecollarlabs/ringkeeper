"""Tests for the ingest service.  Run:  python3 -I -m unittest discover -s server -v

Sample rows are read at test time from a temporary read-only copy of
../data/oura.db (never written back, never committed). If that db is absent,
synthetic rows are used instead.
"""

import http.client
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ingest  # noqa: E402

SOURCE_DB = HERE.parent / "data" / "oura.db"
TOKEN = "test-token-" + "x" * 20
OTHER_TOKEN = "other-token-" + "y" * 20


def load_sample_rows(tmp: Path, limit: int = 500):
    """Return (serial, rows[(tag, ts, body, name)]) from a temp copy of data/oura.db."""
    if SOURCE_DB.exists():
        copy = tmp / "src-copy.db"
        src = sqlite3.connect(f"file:{SOURCE_DB}?mode=ro", uri=True)
        dst = sqlite3.connect(copy)
        src.backup(dst)  # consistent snapshot incl. WAL
        src.close()
        dst.close()
        os.chmod(copy, 0o400)
        c = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
        serial = c.execute("SELECT serial FROM events LIMIT 1").fetchone()[0]
        rows = c.execute(
            "SELECT tag, ring_timestamp, body, name FROM events WHERE serial=?"
            " ORDER BY id LIMIT ?", (serial, limit)).fetchall()
        c.close()
        return serial, [(t, ts, bytes(b), n) for t, ts, b, n in rows], True
    rows = [(0x60, 1000 + i, bytes([i % 256, 1, 2, 3]), "ibi_and_amplitude_event")
            for i in range(limit)]
    return "TESTSERIAL0001", rows, False


class IngestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ingest.setup_logging("CRITICAL")
        cls.tmp = Path(tempfile.mkdtemp(prefix="ringkeeper-ingest-test-"))
        cls.serial, cls.rows, cls.real = load_sample_rows(cls.tmp)
        cls.tokens = cls.tmp / "tokens.json"
        cls.tokens.write_text(json.dumps({"devices": [
            {"name": "phone", "token_sha256": ingest.sha256_hex(TOKEN), "serials": [cls.serial]},
            {"name": "other", "token_sha256": ingest.sha256_hex(OTHER_TOKEN),
             "serials": ["SOMEOTHERRING"]},
        ]}))
        os.chmod(cls.tokens, 0o600)
        cls.db_path = cls.tmp / "data" / "ingest.db"
        cls.srv = ingest.make_server("127.0.0.1", 0, cls.db_path, cls.tokens)
        cls.port = cls.srv.server_address[1]
        cls.th = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.th.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.srv.store.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # helpers -------------------------------------------------------------
    def req(self, method, path, body=None, token=TOKEN, ctype="application/json", raw=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {}
        if token:
            h["Authorization"] = f"Bearer {token}"
        if ctype:
            h["Content-Type"] = ctype
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        out = r.status, json.loads(r.read() or b"{}")
        c.close()
        return out

    def payload(self, rows, cursor=None, serial=None):
        return {"serial": serial or self.serial, "cursor": cursor,
                "rows": [{"tag": t, "ring_timestamp": ts, "body_hex": b.hex()}
                         for t, ts, b, *_ in rows]}

    def db(self):
        return sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)

    # tests ---------------------------------------------------------------
    def test_health(self):
        st, js = self.req("GET", "/v1/health", token=None)
        self.assertEqual(st, 200)
        self.assertTrue(js["ok"])

    def test_insert_idempotent_and_matches_source(self):
        half = len(self.rows) // 2
        st, js = self.req("POST", "/v1/events", self.payload(self.rows[:half], cursor=100))
        self.assertEqual(st, 200, js)
        first = js["inserted"]
        # overlapping re-upload: only the new half is inserted
        st, js = self.req("POST", "/v1/events", self.payload(self.rows, cursor=200))
        self.assertEqual(st, 200, js)
        st, js = self.req("POST", "/v1/events", self.payload(self.rows, cursor=200))
        self.assertEqual(js["inserted"], 0)
        self.assertEqual(js["duplicates"], len(self.rows))

        uniq = {(t, ts, b) for t, ts, b, _ in self.rows}
        c = self.db()
        got = c.execute("SELECT tag, ring_timestamp, body, name, decoded_json FROM events"
                        " WHERE serial=?", (self.serial,)).fetchall()
        self.assertEqual(len(got), len(uniq))
        self.assertLessEqual(first, len(got))
        self.assertEqual({(t, ts, bytes(b)) for t, ts, b, *_ in got}, uniq)
        # tag name table agrees with what open_oura wrote
        src_names = {(t, ts, b): n for t, ts, b, n in self.rows}
        for t, ts, b, n, dj in got:
            self.assertEqual(n, src_names[(t, ts, bytes(b))])
            self.assertIsNone(dj)
        # cursor monotonic
        self.req("POST", "/v1/events", self.payload([], cursor=50))
        cur = c.execute("SELECT next_cursor FROM sync_state WHERE serial=?",
                        (self.serial,)).fetchone()[0]
        self.assertEqual(cur, 200)
        c.close()

    def test_schema_matches_open_oura(self):
        if not self.real:
            self.skipTest("no data/oura.db")
        self.req("GET", "/v1/health", token=None)
        src = sqlite3.connect(f"file:{SOURCE_DB}?mode=ro", uri=True)
        norm = lambda s: " ".join((s or "").replace("IF NOT EXISTS ", "").split())  # noqa: E731
        q = "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
        a = [(t, n, norm(s)) for t, n, s in src.execute(q)]
        d = self.db()
        b = [(t, n, norm(s)) for t, n, s in d.execute(q)]
        src.close()
        d.close()
        self.assertEqual(a, b)

    def test_auth(self):
        p = self.payload(self.rows[:1])
        self.assertEqual(self.req("POST", "/v1/events", p, token=None)[0], 401)
        self.assertEqual(self.req("POST", "/v1/events", p, token="nope")[0], 401)
        self.assertEqual(self.req("POST", "/v1/events", p, token=OTHER_TOKEN)[0], 403)

    def test_validation(self):
        bad = [
            {"serial": "bad serial!", "rows": []},
            {"serial": self.serial, "rows": "x"},
            {"serial": self.serial, "rows": [{"tag": 256, "ring_timestamp": 1, "body_hex": ""}]},
            {"serial": self.serial, "rows": [{"tag": True, "ring_timestamp": 1, "body_hex": ""}]},
            {"serial": self.serial, "rows": [{"tag": 1, "ring_timestamp": -1, "body_hex": ""}]},
            {"serial": self.serial, "rows": [{"tag": 1, "ring_timestamp": 1, "body_hex": "abc"}]},
            {"serial": self.serial, "rows": [{"tag": 1, "ring_timestamp": 1, "body_hex": "zz"}]},
            {"serial": self.serial, "rows": [{"tag": 1, "ring_timestamp": 1,
                                              "body_hex": "00" * 256}]},
            {"serial": self.serial, "rows": [], "extra": 1},
            {"serial": self.serial, "rows": [], "cursor": "5"},
            [],
        ]
        for b in bad:
            st, js = self.req("POST", "/v1/events", b)
            self.assertEqual(st, 400, (b, js))
        self.assertEqual(self.req("POST", "/v1/events", raw=b"{not json")[0], 400)
        self.assertEqual(self.req("POST", "/v1/events", {"serial": self.serial, "rows": []},
                                  ctype="text/plain")[0], 415)
        too_many = {"serial": self.serial, "rows": [
            {"tag": 1, "ring_timestamp": 1, "body_hex": ""}] * (ingest.MAX_ROWS + 1)}
        self.assertEqual(self.req("POST", "/v1/events", too_many)[0], 400)

    def test_body_too_large(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.putrequest("POST", "/v1/events")
        c.putheader("Authorization", f"Bearer {TOKEN}")
        c.putheader("Content-Type", "application/json")
        c.putheader("Content-Length", str(ingest.MAX_BODY_BYTES + 1))
        c.endheaders()
        r = c.getresponse()
        self.assertEqual(r.status, 413)
        c.close()

    def test_404(self):
        self.assertEqual(self.req("GET", "/v1/events")[0], 404)
        self.assertEqual(self.req("POST", "/v1/nope", {})[0], 404)

    def test_token_file_perms_enforced(self):
        t = self.tmp / "loose.json"
        t.write_text(json.dumps({"devices": [
            {"name": "x", "token_sha256": ingest.sha256_hex(TOKEN)}]}))
        os.chmod(t, 0o644)
        self.assertIsNone(ingest.Tokens(t).authenticate(TOKEN))
        os.chmod(t, 0o600)
        self.assertIsNotNone(ingest.Tokens(t).authenticate(TOKEN))


if __name__ == "__main__":
    unittest.main()
