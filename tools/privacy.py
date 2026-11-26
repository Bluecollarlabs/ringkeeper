#!/usr/bin/env python3
"""Your data, your control: export everything as a ZIP, or move it all to a recoverable trash.

Used by tools/dash_api.py (GET /api/export, POST /api/delete) and runnable by hand:

    python3 -I tools/privacy.py export OUT.zip
    python3 -I tools/privacy.py delete --confirm "DELETE MY DATA"     # no PIN on the CLI (local shell = owner)
    python3 -I tools/privacy.py gen-pin                               # create secrets/dashboard_pin (600) if missing

Export ZIP: README.txt, nightly.json, cycle_log.json (if any), readiness.json (if any) and
events.csv (every row of the source DB picked by data/source.json, with unix/local time from
the ring clock and the decoded JSON). Built in a temp file, never stored in the repo.

Delete NEVER hard-deletes. It moves, into one timestamped folder:
  * everything in data/ (oura.db, phone.db, nightly.json, cycle_log.json, logs, ...) except
    source.json (config) and dotfiles (locks)
  * ui-mockup/real-data.json (the dashboard export)
  * server/data/ingest.db rows for the ring serial (events, sync_state, device, readings),
    copied into ingest-rows.db and then deleted from the live DB (ingest.db stays in place)
The folder goes to the freedesktop trash (~/.local/share/Trash/files/ringkeeper-YYYYmmdd-HHMMSS,
with a .trashinfo, so the file manager can restore it) or, if that fails, to
data/.deleted-YYYYmmdd-HHMMSS (gitignored). RESTORE.txt inside says how to undo.
Keep it at least 30 days (`keep_until` in manifest.json); nothing here purges it.
Backups in ~/backups/ringkeeper are NOT touched (14-day rotation in tools/backup.sh).
"""
import argparse
import csv
import datetime as dt
import fcntl
import hmac
import io
import json
import os
import secrets
import shutil
import sqlite3
import stat
import sys
import tempfile
import urllib.parse
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, 'analysis'))
import common as C  # noqa: E402

CONFIRM_PHRASE = 'DELETE MY DATA'
PIN_FILE = os.path.join(ROOT, 'secrets', 'dashboard_pin')
KEEP_DAYS = 30
KEEP_IN_DATA = {'source.json'}


class Paths:
    """All locations in one place so tests can point them at a temp dir."""

    def __init__(self, root=ROOT, trash_home=None):
        self.root = root
        self.data = os.path.join(root, 'data')
        self.ingest = os.path.join(root, 'server', 'data', 'ingest.db')
        self.real_data = os.path.join(root, 'ui-mockup', 'real-data.json')
        self.lock = os.path.join(self.data, '.morning_sync.lock')
        self.trash_home = trash_home if trash_home is not None else os.path.join(
            os.environ.get('XDG_DATA_HOME') or os.path.expanduser('~/.local/share'), 'Trash')

    def source(self):
        try:
            cfg = C.source_config(os.path.join(self.data, 'source.json'))
        except ValueError:
            cfg = {'mode': 'pc'}
        db = os.path.join(self.data, 'phone.db' if cfg['mode'] == 'phone' else 'oura.db')
        return cfg, db


# ---------------------------------------------------------------- export
README = """ringkeeper data export
======================
Exported {when} from your RingKeeper PC (data source: {mode}). This is YOUR data; nothing here came
from or went to the Oura cloud.

nightly.json     per-night metrics computed by ringkeeper (sleep timing, estimated stages,
                 resting HR, HRV/RMSSD, skin temperature, movement). Stages are ESTIMATES
                 from our own rules, not Oura's model.
cycle_log.json   periods you logged (only if you logged any)
readiness.json   our readiness score + breakdown (our formula, not Oura's)
events.csv       every raw ring event in the database ({rows} rows):
                 id, name, tag, ring_timestamp (ring deciseconds), unix_time, local_time
                 ({tz}, from the ring clock), body_hex (raw bytes),
                 decoded_json (open_oura's decode, may be empty)

Not medical data or advice.
"""


def _events_csv(db, out):
    """Write events.csv rows into text stream `out`; returns row count."""
    w = csv.writer(out)
    w.writerow(['id', 'name', 'tag', 'ring_timestamp', 'unix_time', 'local_time', 'body_hex', 'decoded_json'])
    if not os.path.exists(db):
        return 0
    con = C.open_ro(db)
    try:
        try:
            clock = C.RingClock.from_events(C.load_events(con, ('time_sync',)))
        except ValueError:
            clock = None
        n = 0
        for rid, name, tag, ts, body, js in con.execute(
                'SELECT id, name, tag, ring_timestamp, body, decoded_json FROM events ORDER BY ring_timestamp, id'):
            u = clock.unix(ts) if clock else None
            w.writerow([rid, name, tag, ts, '' if u is None else round(u, 1), C.iso(u) or '',
                        bytes(body).hex() if body is not None else '', js or ''])
            n += 1
        return n
    finally:
        con.close()


def build_export(paths=None, fileobj=None):
    """Write the export ZIP into `fileobj` (default: a new TemporaryFile). Returns (fileobj, info)."""
    p = paths or Paths()
    cfg, db = p.source()
    f = fileobj or tempfile.TemporaryFile()
    included = []
    with zipfile.ZipFile(f, 'w', zipfile.ZIP_DEFLATED) as z:
        with z.open('events.csv', 'w', force_zip64=True) as raw:
            txt = io.TextIOWrapper(raw, encoding='utf-8', newline='')
            rows = _events_csv(db, txt)
            txt.flush()
            txt.detach()
        included.append('events.csv')
        for name in ('nightly.json', 'cycle_log.json', 'readiness.json'):
            src = os.path.join(p.data, name)
            if os.path.isfile(src):
                z.write(src, name)
                included.append(name)
        when = dt.datetime.now(C.TZ).isoformat(timespec='seconds')
        z.writestr('README.txt', README.format(when=when, mode=cfg['mode'], rows=rows, tz=C.TZ_NAME))
    f.flush()
    f.seek(0)
    return f, {'rows': rows, 'files': ['README.txt'] + included, 'data_source': cfg['mode']}


# ---------------------------------------------------------------- PIN
def gen_pin(path=PIN_FILE):
    """Create a random 6-digit PIN file (600) if missing. Returns (path, created)."""
    if os.path.exists(path):
        return path, False
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    pin = f'{secrets.randbelow(10 ** 6):06d}'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as fh:
        fh.write(pin + '\n')
    return path, True


def load_pin(path=PIN_FILE):
    """PIN string, or None when missing / unreadable / group- or world-accessible (then delete is disabled)."""
    try:
        st_ = os.stat(path)
        if st_.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            return None
        with open(path) as fh:
            pin = fh.read().strip()
        return pin or None
    except OSError:
        return None


def check_pin(given, path=PIN_FILE):
    pin = load_pin(path)
    return pin is not None and isinstance(given, str) and hmac.compare_digest(given.strip().encode(), pin.encode())


# ---------------------------------------------------------------- delete
def _serials(p, cfg):
    if cfg.get('serial'):
        return [str(cfg['serial'])]
    out = set()
    for db in (os.path.join(p.data, 'oura.db'), os.path.join(p.data, 'phone.db')):
        if os.path.exists(db):
            try:
                con = C.open_ro(db)
                out |= {r[0] for r in con.execute('SELECT serial FROM device') if r[0]}
                con.close()
            except sqlite3.Error:
                pass
    if not out and os.path.exists(p.ingest):  # phone-only setup without a decoded copy: every serial
        con = C.open_ro(p.ingest)
        out = {r[0] for r in con.execute('SELECT DISTINCT serial FROM events')}
        con.close()
    return sorted(out)


def _trash_dir(p, stamp):
    """(folder, trashinfo path or None). Prefer the freedesktop trash; fall back next to data/."""
    name = f'ringkeeper-{stamp}'
    try:
        files = os.path.join(p.trash_home, 'files')
        info = os.path.join(p.trash_home, 'info')
        os.makedirs(files, mode=0o700, exist_ok=True)
        os.makedirs(info, mode=0o700, exist_ok=True)
        d = os.path.join(files, name)
        os.mkdir(d, 0o700)
        return d, os.path.join(info, name + '.trashinfo')
    except OSError:
        os.makedirs(p.data, exist_ok=True)
        d = os.path.join(p.data, f'.deleted-{stamp}')  # dotfile: skipped by the move below; data/ is gitignored
        os.mkdir(d, 0o700)
        return d, None


def _move_ingest_rows(ingest, serials, dest_db):
    """Copy all rows for `serials` into dest_db, then delete them from ingest (one transaction)."""
    if not serials or not os.path.exists(ingest):
        return {}
    con = sqlite3.connect(ingest, timeout=15)
    try:
        con.execute('PRAGMA busy_timeout = 15000')
        con.execute('ATTACH DATABASE ? AS t', (dest_db,))
        counts = {}
        ph = ','.join('?' * len(serials))
        con.execute('BEGIN IMMEDIATE')
        for table in ('events', 'sync_state', 'device', 'readings'):
            if not con.execute("SELECT 1 FROM main.sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                continue
            sql = con.execute("SELECT sql FROM main.sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
            con.execute(sql.replace(f'CREATE TABLE {table}', f'CREATE TABLE IF NOT EXISTS t.{table}', 1))
            con.execute(f'INSERT INTO t.{table} SELECT * FROM main.{table} WHERE serial IN ({ph})', serials)
            counts[table] = con.execute(f'DELETE FROM main.{table} WHERE serial IN ({ph})', serials).rowcount
        con.execute('COMMIT')
        con.execute('DETACH DATABASE t')
        os.chmod(dest_db, 0o600)
        return counts
    except Exception:
        if con.in_transaction:
            con.execute('ROLLBACK')
        raise
    finally:
        con.close()


RESTORE = """ringkeeper: data moved here on {when} by "Delete my data" (nothing was hard-deleted).
Keep until at least {keep}. To undo (stop morning_sync first):

  data/*            -> move every file in data/ back to {root}/data/
  real-data.json    -> {root}/ui-mockup/real-data.json
  ingest-rows.db    -> rows for serial(s) {serials}; to put them back:
      sqlite3 {root}/server/data/ingest.db "ATTACH 'ingest-rows.db' AS t;
        INSERT OR IGNORE INTO events SELECT * FROM t.events;
        INSERT OR REPLACE INTO sync_state SELECT * FROM t.sync_state;
        INSERT OR REPLACE INTO device SELECT * FROM t.device;
        INSERT INTO readings SELECT * FROM t.readings;"
  (or restore the folder from the desktop trash, then do the above)
"""


def delete_data(confirm, paths=None, now=None):
    """Move the user's data into a timestamped trash folder. Raises ValueError on a wrong phrase,
    BlockingIOError if a morning sync is running. Returns a summary dict."""
    if confirm != CONFIRM_PHRASE:
        raise ValueError(f'confirm phrase must be exactly "{CONFIRM_PHRASE}"')
    p = paths or Paths()
    now = now or dt.datetime.now(C.TZ)
    stamp = now.strftime('%Y%m%d-%H%M%S')
    os.makedirs(p.data, exist_ok=True)
    lockf = open(p.lock, 'a')
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)  # same lock as tools/morning_sync.sh
    except BlockingIOError:
        lockf.close()
        raise
    try:
        cfg, _ = p.source()
        serials = _serials(p, cfg)
        dest, info = _trash_dir(p, stamp)
        moved = []
        ddest = os.path.join(dest, 'data')
        os.mkdir(ddest, 0o700)
        if os.path.isdir(p.data):
            for name in sorted(os.listdir(p.data)):
                if name.startswith('.'):
                    continue
                src = os.path.join(p.data, name)
                if name in KEEP_IN_DATA:
                    shutil.copy2(src, os.path.join(ddest, name))
                    continue
                shutil.move(src, os.path.join(ddest, name))
                moved.append('data/' + name)
        if os.path.isfile(p.real_data):
            shutil.move(p.real_data, os.path.join(dest, 'real-data.json'))
            moved.append('ui-mockup/real-data.json')
        rows = _move_ingest_rows(p.ingest, serials, os.path.join(dest, 'ingest-rows.db'))
        keep = (now + dt.timedelta(days=KEEP_DAYS)).date().isoformat()
        man = {'deleted_at': now.isoformat(timespec='seconds'), 'keep_until': keep, 'moved': moved,
               'ingest_rows_moved': rows, 'serials': serials, 'data_source': cfg['mode'],
               'not_touched': ['secrets/ (ring key, PIN)', 'data/source.json', '~/backups/ringkeeper (14-day rotation)']}
        with open(os.path.join(dest, 'manifest.json'), 'w') as fh:
            json.dump(man, fh, indent=1)
        with open(os.path.join(dest, 'RESTORE.txt'), 'w') as fh:
            fh.write(RESTORE.format(when=man['deleted_at'], keep=keep, root=p.root, serials=', '.join(serials) or '-'))
        if info:
            with open(info, 'w') as fh:
                fh.write('[Trash Info]\nPath=%s\nDeletionDate=%s\n' % (
                    urllib.parse.quote(os.path.join(p.root, os.path.basename(dest))), now.strftime('%Y-%m-%dT%H:%M:%S')))
        return {'ok': True, 'trash_dir': dest, **man}
    finally:
        fcntl.flock(lockf, fcntl.LOCK_UN)
        lockf.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description='export / delete your ringkeeper data')
    sub = ap.add_subparsers(dest='cmd', required=True)
    e = sub.add_parser('export')
    e.add_argument('out')
    d = sub.add_parser('delete')
    d.add_argument('--confirm', required=True)
    sub.add_parser('gen-pin')
    a = ap.parse_args(argv)
    if a.cmd == 'export':
        fd = os.open(a.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w+b') as fh:
            _, info = build_export(fileobj=fh)
        print(json.dumps(info))
    elif a.cmd == 'delete':
        print(json.dumps(delete_data(a.confirm), indent=1))
    else:
        path, created = gen_pin()
        print(f'{"created" if created else "exists"}: {path}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
