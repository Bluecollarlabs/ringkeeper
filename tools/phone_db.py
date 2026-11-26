#!/usr/bin/env python3
"""Phone mode: build data/phone.db (decoded) from the ingest server's server/data/ingest.db.

The ingest service stores the phone's raw ring events with decoded_json = NULL. The analysis
needs decoded_json, so we:

  1. snapshot ingest.db READ-ONLY (sqlite URI mode=ro + online-backup API; consistent while the
     ringkeeper-ingest service keeps writing; ingest.db itself is never modified),
  2. optionally keep only one ring serial (data/source.json "serial"),
  3. run open_oura's offline decoder on the snapshot: `oura --db <tmp> redecode` (no BLE),
  4. check it, then atomically replace data/phone.db (chmod 600).

Rebuilt from scratch on every run -> idempotent, no merge state, no drift from decoder upgrades.
Why a separate DB instead of merging into data/oura.db: the ring is factory-reset before it is
paired to the phone, and its ring_timestamp counter may restart; mixing two clock epochs in one
DB would confuse common.RingClock (it assumes one monotonic counter). data/oura.db (PC sync
history) stays untouched and is used again if you switch back to pc mode.

Usage: python3 -I tools/phone_db.py [build] [--ingest PATH] [--out PATH] [--serial S] [--json]
       python3 -I tools/phone_db.py status [--json]     # last phone upload, counts (read-only)
Env: RINGKEEPER_OURA_BIN (default upstream/open_oura/target/release/oura).
Exit: 0 ok · 3 no phone data yet · 4 ok but last phone upload > 24 h ago (stale) · 1 error
"""
import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools', 'analysis'))
import common as C  # noqa: E402

OURA = os.environ.get('RINGKEEPER_OURA_BIN') or os.path.join(ROOT, 'upstream', 'open_oura', 'target', 'release', 'oura')
STALE_S = 24 * 3600


def status(ingest=C.INGEST_DB, serial=None):
    """Read-only summary of what the phone(s) uploaded."""
    if not os.path.exists(ingest):
        return {'ingest_db': ingest, 'exists': False, 'events': 0, 'last_upload_unix': None}
    con = sqlite3.connect(f'file:{ingest}?mode=ro', uri=True)
    try:
        w, p = ('WHERE serial = ?', (serial,)) if serial else ('', ())
        n, last_cap, max_ts = con.execute(
            f'SELECT COUNT(*), MAX(captured_unix), MAX(ring_timestamp) FROM events {w}', p).fetchone()
        last_sync = con.execute(f'SELECT MAX(last_sync_unix) FROM sync_state {w}', p).fetchone()[0]
        serials = [r[0] for r in con.execute('SELECT DISTINCT serial FROM events ORDER BY serial')]
    finally:
        con.close()
    # last_sync_unix = last upload that carried a cursor (also bumps on duplicate-only uploads);
    # captured_unix = last time NEW rows were inserted. Either proves the phone reached us.
    last = max([x for x in (last_cap, last_sync) if x is not None], default=None)
    age = None if last is None else int(time.time()) - int(last)
    return {'ingest_db': ingest, 'exists': True, 'events': n, 'serials': serials,
            'last_upload_unix': last, 'last_upload': C.iso(last), 'last_new_rows': C.iso(last_cap),
            'age_s': age, 'stale': age is None or age > STALE_S, 'max_ring_timestamp': max_ts}


def build(ingest=C.INGEST_DB, out=C.PHONE_DB, serial=None, oura=OURA):
    if not os.path.exists(ingest):
        raise FileNotFoundError(f'{ingest} (is ringkeeper-ingest running? has the phone uploaded yet?)')
    if not os.access(oura, os.X_OK):
        raise FileNotFoundError(f'oura binary not found: {oura} (cargo build --release in upstream/open_oura)')
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmpdir = tempfile.mkdtemp(prefix='.phone_db-', dir=os.path.dirname(out))  # same FS -> atomic rename
    os.chmod(tmpdir, 0o700)
    try:
        tmp = os.path.join(tmpdir, 'phone.db')
        src = sqlite3.connect(f'file:{ingest}?mode=ro', uri=True)
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
        finally:
            src.close()
        os.chmod(tmp, 0o600)
        dst.execute('PRAGMA journal_mode=DELETE')
        if serial:
            dst.execute('DELETE FROM events WHERE serial != ?', (serial,))
            dst.execute('DELETE FROM sync_state WHERE serial != ?', (serial,))
            dst.commit()
        n = dst.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        dst.close()
        if n == 0:
            return {'events': 0, 'decoded': 0, 'out': out, 'built': False}
        # offline decoder; never scans/connects (redecode only touches the DB)
        r = subprocess.run([oura, '--db', tmp, 'redecode'], capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            raise RuntimeError(f'oura redecode failed ({r.returncode}): {(r.stderr or r.stdout).strip()[-500:]}')
        dst = sqlite3.connect(tmp)
        try:
            ok = dst.execute('PRAGMA integrity_check').fetchone()[0]
            n, dec = dst.execute('SELECT COUNT(*), COUNT(decoded_json) FROM events').fetchone()
            anchors = dst.execute("SELECT COUNT(*) FROM events WHERE name = 'time_sync' "
                                  'AND decoded_json IS NOT NULL').fetchone()[0]
            # open_oura leaves the file in WAL mode: fold the WAL in so the file is self-contained
            dst.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            dst.execute('PRAGMA journal_mode=DELETE')
        finally:
            dst.close()
        if ok != 'ok':
            raise RuntimeError(f'integrity_check failed on decoded copy: {ok}')
        if dec == 0:
            raise RuntimeError(f'redecode decoded 0 of {n} events')
        for suf in ('-wal', '-shm', '-journal'):
            if os.path.exists(tmp + suf):
                os.remove(tmp + suf)
        os.replace(tmp, out)
        for suf in ('-wal', '-shm'):  # stale sidecars of the previous phone.db
            if os.path.exists(out + suf):
                os.remove(out + suf)
        return {'events': n, 'decoded': dec, 'time_sync_anchors': anchors, 'out': out, 'built': True}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description='phone mode: ingest.db -> decoded data/phone.db')
    ap.add_argument('cmd', nargs='?', default='build', choices=('build', 'status'))
    ap.add_argument('--ingest', default=C.INGEST_DB)
    ap.add_argument('--out', default=C.PHONE_DB)
    ap.add_argument('--serial', help='keep only this ring serial (default: data/source.json "serial")')
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args(argv)
    serial = a.serial or C.source_config().get('serial') or None

    st = status(a.ingest, serial)
    res = {'status': st}
    rc = 0
    if a.cmd == 'build':
        if st['events'] == 0:
            res['build'] = {'built': False, 'events': 0, 'out': a.out}
            rc = 3
        else:
            res['build'] = build(a.ingest, a.out, serial)
            rc = 4 if st['stale'] else 0
    if a.json:
        print(json.dumps(res, indent=1))
    else:
        if not st['exists']:
            print(f"phone: no ingest DB at {st['ingest_db']}")
        elif st['events'] == 0:
            print('phone: no uploads yet' + (f' for serial {serial}' if serial else ''))
        else:
            h = st['age_s'] / 3600
            print(f"phone: last upload {st['last_upload'][:16].replace('T', ' ')} ({h:.1f} h ago), "
                  f"{st['events']} events" + (f", serials {','.join(st['serials'])}" if st['serials'] else ''))
            if st['stale']:
                print('phone: WARNING no phone upload for > 24 h '
                      '(Tailscale on? RingKeeper battery-killed? ring in range/charged?)')
        b = res.get('build')
        if b and b.get('built'):
            print(f"phone.db: {b['decoded']}/{b['events']} decoded, {b['time_sync_anchors']} time_sync -> "
                  f"{os.path.relpath(b['out'], ROOT)}")
    return rc


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:
        print(f'phone_db: {type(e).__name__}: {e}', file=sys.stderr)  # noqa: T201
        sys.exit(1)
