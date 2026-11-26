#!/usr/bin/env python3
"""End-to-end check: Android core uploader payloads -> live server/ingest.py.

Everything is temporary and local (loopback only, no BLE, no tailscale):
  1. snapshot data/oura.db (sqlite online backup, source opened read-only) into a tmpdir
  2. mint a throwaway token into a tmp tokens.json (chmod 600)
  3. start `ingest.py serve` on a free 127.0.0.1 port with a tmp ingest DB
  4. run core's Rust test `e2e_upload_to_ingest_if_configured`, which pages the snapshot
     exactly like Uploader.kt (export_since <= 2000 rows -> upload_payload_json, cursor
     only on the last page -> POST) and prints an E2E_RESULT json line
  5. assert: every row inserted, byte-exact (tag, ring_timestamp, body) sets, server
     cursor == ring cursor; then a second run must be 100 % duplicates (idempotent)
The tmpdir (health data + token) is deleted at the end. The token is never printed.

Usage: python3 -I server/e2e_upload.py [--db data/oura.db]
Needs: mise + cargo (core/mise.toml). Exit 0 = pass.
"""
import argparse
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CORE = os.path.join(ROOT, 'core')


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def snapshot(src, dst):
    s = sqlite3.connect(f'file:{src}?mode=ro', uri=True)
    d = sqlite3.connect(dst)
    with d:
        s.backup(d)
    s.close()
    d.close()


def run_core_e2e(url, token, db):
    env = dict(os.environ, RINGKEEPER_E2E_URL=url, RINGKEEPER_E2E_TOKEN=token, RINGKEEPER_TEST_DB=db)
    p = subprocess.run(['mise', 'exec', '--', 'cargo', 'test', '--lib', '-q', 'e2e_upload_to_ingest_if_configured',
                        '--', '--exact', 'tests::e2e_upload_to_ingest_if_configured', '--nocapture'],
                       cwd=CORE, env=env, capture_output=True, text=True, timeout=900)
    res = [ln for ln in p.stdout.splitlines() if ln.startswith('E2E_RESULT ')]
    if p.returncode != 0 or not res:
        sys.stderr.write(p.stdout[-3000:] + p.stderr[-3000:])
        raise SystemExit(f'core e2e test failed (exit {p.returncode})')
    return json.loads(res[-1].split(' ', 1)[1])


def rowset(db, serial):
    c = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    try:
        return set(c.execute('SELECT tag, ring_timestamp, body FROM events WHERE serial = ?', (serial,)))
    finally:
        c.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=os.path.join(ROOT, 'data', 'oura.db'))
    a = ap.parse_args()
    if not os.path.exists(a.db):
        raise SystemExit(f'no source db {a.db}')
    with tempfile.TemporaryDirectory(prefix='oura-e2e-') as tmp:
        os.chmod(tmp, 0o700)
        src = os.path.join(tmp, 'phone.db')
        snapshot(a.db, src)
        c = sqlite3.connect(f'file:{src}?mode=ro', uri=True)
        serial = c.execute('SELECT serial FROM device LIMIT 1').fetchone()[0]
        n_src = c.execute('SELECT COUNT(*) FROM events WHERE serial = ?', (serial,)).fetchone()[0]
        c.close()
        tokens = os.path.join(tmp, 'tokens.json')
        ingest_db = os.path.join(tmp, 'ingest.db')
        token = subprocess.run([sys.executable, '-I', os.path.join(HERE, 'ingest.py'), '--tokens', tokens,
                                'mint-token', '--name', 'e2e', '--serial', serial],
                               check=True, capture_output=True, text=True).stdout.split()[0]
        port = free_port()
        srv = subprocess.Popen([sys.executable, '-I', os.path.join(HERE, 'ingest.py'), '--tokens', tokens, 'serve',
                                '--host', '127.0.0.1', '--port', str(port), '--db', ingest_db, '--log-level', 'WARNING'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            base = f'http://127.0.0.1:{port}'
            for _ in range(100):
                try:
                    if urllib.request.urlopen(base + '/v1/health', timeout=1).status == 200:
                        break
                except OSError:
                    time.sleep(0.1)
            else:
                raise SystemExit('ingest server did not come up')
            url = base + '/v1/events'
            t0 = time.time()
            r1 = run_core_e2e(url, token, src)
            print(f"run 1: {r1['pages']} pages, sent {r1['sent']}, inserted {r1['inserted']}, "
                  f"duplicates {r1['duplicates']} ({time.time() - t0:.1f}s)")
            assert r1['sent'] == n_src, (r1['sent'], n_src)
            assert r1['pages'] == -(-n_src // 2000), r1['pages']
            assert r1['inserted'] == n_src, 'first upload must insert every row'
            a_rows, b_rows = rowset(src, serial), rowset(ingest_db, serial)
            assert a_rows == b_rows, f'row sets differ: {len(a_rows ^ b_rows)} rows'
            c = sqlite3.connect(f'file:{ingest_db}?mode=ro', uri=True)
            cur = c.execute('SELECT next_cursor FROM sync_state WHERE serial = ?', (serial,)).fetchone()
            n_srv = c.execute('SELECT COUNT(*) FROM events').fetchone()[0]
            c.close()
            assert n_srv == n_src, (n_srv, n_src)
            assert cur and cur[0] == r1['ring_cursor'], (cur, r1['ring_cursor'])
            r2 = run_core_e2e(url, token, src)
            print(f"run 2: {r2['pages']} pages, sent {r2['sent']}, inserted {r2['inserted']}, duplicates {r2['duplicates']}")
            assert r2['inserted'] == 0 and r2['duplicates'] == n_src, 're-upload must be idempotent'
            print(f'PASS: {n_src} rows byte-exact in ingest db, cursor {cur[0]}, re-upload idempotent')
        finally:
            srv.terminate()
            try:
                srv.wait(10)
            except subprocess.TimeoutExpired:
                srv.kill()
    return 0


if __name__ == '__main__':
    sys.exit(main())
