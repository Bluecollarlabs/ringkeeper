#!/usr/bin/env python3
"""ringkeeper health check -> JSON {ok, issues[], checks{}} on stdout. Exit 0 ok, 1 issues.

    python3 -I tools/health_check.py [--pretty]

Read-only and offline (loopback HTTP only, no BLE). Checks:
  data_age   newest ring data for the data/source.json mode is <= 24 h old
             (pc: data/oura.db sync_state.last_sync_unix; phone: last upload in
             server/data/ingest.db, see tools/phone_db.py status)
  battery    latest decoded ring battery level (debug_data battery_level_changed) >= 20 %
             (skipped when none is decoded; reports its age)
  ingest     ringkeeper-ingest answers GET http://127.0.0.1:$RINGKEEPER_INGEST_PORT/v1/health (8092)
  dashboard  dash_api answers GET http://127.0.0.1:$RINGKEEPER_DASH_PORT/api/health (8091)
  ollama     Ollama answers GET $RINGKEEPER_AI_URL/api/version (http://127.0.0.1:11434)
  disk       free space on the repo and backup filesystems >= 10 %
  backups    newest snapshot in $RINGKEEPER_BACKUP_DIR (~/backups/ringkeeper) is <= 2 days old
Each issue: {check, severity (warn|error), message}. Not scheduled here (run it from cron/systemd timers if you like).
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, 'analysis'))
sys.path.insert(0, HERE)
import common as C  # noqa: E402

DATA_MAX_AGE_S = 24 * 3600
BACKUP_MAX_AGE_S = 2 * 86400
BATTERY_MIN = 20
DISK_MIN_FREE_PCT = 10.0
_ENV = os.environ.get
URLS = {'ingest': f"http://127.0.0.1:{_ENV('RINGKEEPER_INGEST_PORT', '8092')}/v1/health",
        'dashboard': f"http://127.0.0.1:{_ENV('RINGKEEPER_DASH_PORT', '8091')}/api/health",
        'ollama': _ENV('RINGKEEPER_AI_URL', 'http://127.0.0.1:11434').rstrip('/') + '/api/version'}
BACKUP_DIR = os.path.expanduser(os.path.expandvars(_ENV('RINGKEEPER_BACKUP_DIR', '~/backups/ringkeeper')))


def _age_h(age_s):
    return None if age_s is None else round(age_s / 3600, 1)


def check_data_age(cfg, now, pc_db=C.PC_DB, ingest=C.INGEST_DB):
    if cfg['mode'] == 'phone':
        import phone_db
        st = phone_db.status(ingest, cfg.get('serial'))
        last, src = st.get('last_upload_unix'), 'last phone upload'
    else:
        last, src = None, 'last PC sync'
        if os.path.exists(pc_db):
            try:
                con = C.open_ro(pc_db)
                try:
                    last = con.execute('SELECT MAX(last_sync_unix) FROM sync_state').fetchone()[0]
                finally:
                    con.close()
            except sqlite3.Error:
                last = None
    age = None if last is None else now - last
    res = {'mode': cfg['mode'], 'source': src, 'last_unix': last, 'last': C.iso(last), 'age_h': _age_h(age)}
    if last is None:
        return res, f'no {src} found ({cfg["mode"]} mode)'
    if age > DATA_MAX_AGE_S:
        return res, f'{src} was {_age_h(age)} h ago (> 24 h)'
    return res, None


def check_battery(db, now):
    try:
        b = C.latest_battery(db)
    except Exception as e:
        return {'error': type(e).__name__}, None
    if not b or b.get('pct') is None:
        return {'pct': None, 'note': 'no decoded battery event'}, None
    res = {'pct': b['pct'], 'at': b['at'], 'age_h': _age_h(None if b['at_unix'] is None else now - b['at_unix'])}
    if b['pct'] < BATTERY_MIN:
        return res, f"ring battery {b['pct']}% (< {BATTERY_MIN}%) as of {b['at'] or 'unknown time'}"
    return res, None


def check_http(name, url, timeout=3.0):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # loopback only, never a proxy
    try:
        with opener.open(url, timeout=timeout) as r:
            ok = 200 <= r.status < 300
            return {'url': url, 'status': r.status}, None if ok else f'{name} returned HTTP {r.status}'
    except Exception as e:
        return {'url': url, 'error': type(e).__name__}, f'{name} is down ({url}: {type(e).__name__})'


def check_disk(paths):
    res, issues = {}, []
    seen = set()
    for p in paths:
        while p and not os.path.exists(p):
            p = os.path.dirname(p)
        if not p:
            continue
        dev = os.stat(p).st_dev
        if dev in seen:
            continue
        seen.add(dev)
        u = shutil.disk_usage(p)
        free = round(100 * u.free / u.total, 1)
        res[p] = {'free_pct': free, 'free_gb': round(u.free / 1e9, 1)}
        if free < DISK_MIN_FREE_PCT:
            issues.append(f'disk {p}: only {free}% free (< {DISK_MIN_FREE_PCT:g}%)')
    return res, '; '.join(issues) or None


def check_backups(backup_dir, now):
    snaps = sorted(d for d in (os.listdir(backup_dir) if os.path.isdir(backup_dir) else [])
                   if len(d) == 10 and d[4] == '-' and d[7] == '-' and os.path.isdir(os.path.join(backup_dir, d)))
    if not snaps:
        return {'dir': backup_dir, 'latest': None}, f'no backups in {backup_dir}'
    d = os.path.join(backup_dir, snaps[-1])
    sums = os.path.join(d, 'SHA256SUMS')
    mt = os.path.getmtime(sums if os.path.exists(sums) else d)
    age = now - mt
    res = {'dir': backup_dir, 'latest': snaps[-1], 'age_h': _age_h(age), 'count': len(snaps)}
    if age > BACKUP_MAX_AGE_S:
        return res, f'newest backup {snaps[-1]} is {_age_h(age)} h old (> 2 days)'
    return res, None


def run(now=None, cfg=None, urls=None, backup_dir=BACKUP_DIR, pc_db=C.PC_DB, ingest=C.INGEST_DB, db=None, disk_paths=None):
    now = now or time.time()
    urls = urls or URLS
    checks, issues = {}, []

    def add(name, fn, severity='error'):
        try:
            res, msg = fn()
        except Exception as e:  # a broken check is itself an issue
            res, msg = {'error': f'{type(e).__name__}: {e}'}, f'{name} check failed: {type(e).__name__}'
        checks[name] = res
        if msg:
            issues.append({'check': name, 'severity': severity, 'message': msg})

    try:
        cfg = cfg or C.source_config()
    except ValueError as e:
        cfg = {'mode': 'pc'}
        issues.append({'check': 'config', 'severity': 'error', 'message': str(e)})
    if db is None:
        db = os.path.join(os.path.dirname(pc_db), 'phone.db') if cfg['mode'] == 'phone' else pc_db
    add('data_age', lambda: check_data_age(cfg, now, pc_db, ingest), 'warn')
    add('battery', lambda: check_battery(db, now), 'warn')
    for name in ('ingest', 'dashboard', 'ollama'):
        add(name, lambda n=name: check_http(n, urls[n]), 'warn' if name == 'ollama' else 'error')
    add('disk', lambda: check_disk(disk_paths or [ROOT, backup_dir]))
    add('backups', lambda: check_backups(backup_dir, now), 'warn')
    return {'ok': not issues, 'time': C.iso(now), 'data_source': cfg['mode'], 'issues': issues, 'checks': checks}


def main(argv=None):
    ap = argparse.ArgumentParser(description='ringkeeper health check (JSON; exit 0 ok / 1 issues)')
    ap.add_argument('--pretty', action='store_true')
    a = ap.parse_args(argv)
    res = run()
    print(json.dumps(res, indent=1 if a.pretty else None))
    return 0 if res['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
