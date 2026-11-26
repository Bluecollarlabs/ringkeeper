"""Shared helpers for the ringkeeper analysis module (stdlib only).

- read-only DB access (sqlite URI mode=ro)
- ring clock: ring timestamps are deciseconds since the ring's epoch; we map them to
  unix seconds with piecewise-linear interpolation between `time_sync` anchors
- small robust-statistics helpers (median / MAD / quantile)

Health data never leaves data/ (gitignored). Nothing here writes to the DB.
"""
import bisect
import collections
import datetime as dt
import json
import math
import os
import sqlite3
import statistics as st
import zoneinfo

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
DATA_DIR = os.path.join(ROOT, 'data')
PC_DB = os.path.join(DATA_DIR, 'oura.db')          # PC BLE sync (open_oura `oura sync`)
PHONE_DB = os.path.join(DATA_DIR, 'phone.db')       # derived from server/data/ingest.db (tools/phone_db.py)
INGEST_DB = os.path.join(ROOT, 'server', 'data', 'ingest.db')
SOURCE_JSON = os.path.join(DATA_DIR, 'source.json')  # {"mode": "pc"|"phone"}; missing -> pc
MODES = ('pc', 'phone')


def source_config(path=None):
    """Data-source switch. Env RINGKEEPER_SOURCE (pc|phone) overrides data/source.json.

    Keys: mode (pc|phone, default pc), serial (optional: phone mode keeps only this ring serial).
    """
    cfg = {'mode': 'pc'}
    try:
        with open(path or SOURCE_JSON) as f:
            doc = json.load(f)
        if isinstance(doc, dict):
            cfg.update(doc)
    except (FileNotFoundError, ValueError):
        pass
    env = os.environ.get('RINGKEEPER_SOURCE')
    if env:
        cfg['mode'] = env
    cfg['mode'] = str(cfg.get('mode', 'pc')).strip().lower()
    if cfg['mode'] not in MODES:
        raise ValueError(f"data source mode must be one of {MODES}, got {cfg['mode']!r} ({path or SOURCE_JSON})")
    return cfg


def source_db(cfg=None):
    """DB the analysis/export should read for the configured mode."""
    return PHONE_DB if (cfg or source_config())['mode'] == 'phone' else PC_DB


DEFAULT_DB = source_db()
NIGHTLY_JSON = os.path.join(DATA_DIR, 'nightly.json')


def _local_tz_name():
    """IANA zone for local calendar days: env RINGKEEPER_TZ, else the system zone, else UTC."""
    env = os.environ.get('RINGKEEPER_TZ')
    if env:
        return env
    try:
        target = os.path.realpath('/etc/localtime')
        if 'zoneinfo/' in target:
            name = target.split('zoneinfo/', 1)[1]
            zoneinfo.ZoneInfo(name)
            return name
    except (OSError, zoneinfo.ZoneInfoNotFoundError, ValueError):
        pass
    return 'UTC'


TZ_NAME = _local_tz_name()
TZ = zoneinfo.ZoneInfo(TZ_NAME)

# Event names used by the analysis.
NAMES = (
    'time_sync', 'bedtime_period', 'sleep_acm_period', 'motion_event',
    'ibi_and_amplitude_event', 'hrv_event', 'sleep_temp_event',
)


def open_ro(db_path=DEFAULT_DB):
    """Open the SQLite DB strictly read-only."""
    if not os.path.exists(db_path):
        raise FileNotFoundError(db_path)
    return sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)


def load_events(con, names=NAMES):
    """Return {name: [(ring_ts, decoded_dict), ...]} ordered by ring timestamp."""
    ev = collections.defaultdict(list)
    q = ('SELECT name, ring_timestamp, decoded_json FROM events '
         'WHERE decoded_json IS NOT NULL AND name IN (%s) ORDER BY ring_timestamp, id'
         % ','.join('?' * len(names)))
    for name, ts, js in con.execute(q, tuple(names)):
        try:
            ev[name].append((ts, json.loads(js)))
        except ValueError:
            pass
    return ev


class RingClock:
    """Ring deciseconds -> unix seconds.

    Anchors are (ring_ts_ds, unix_s) pairs from `time_sync` events. Between anchors we
    interpolate linearly (corrects ring clock drift); outside, we extrapolate with the
    nearest segment's rate (or rate 1.0 with a single anchor).
    Limitation: assumes one ring boot (monotonic counter). A ring reboot resets the
    counter; anchors that go backwards in unix time are dropped with a warning.
    """

    def __init__(self, anchors):
        pts = sorted({int(t): float(u) for t, u in anchors}.items())
        if not pts:
            raise ValueError('no time_sync anchors: cannot convert ring timestamps')
        clean = [pts[0]]
        self.warnings = []
        for t, u in pts[1:]:
            if u <= clean[-1][1]:
                self.warnings.append(f'non-monotonic time_sync at ds={t} dropped (ring reboot?)')
                continue
            clean.append((t, u))
        self.ts = [p[0] for p in clean]
        self.us = [p[1] for p in clean]

    @classmethod
    def from_events(cls, ev):
        return cls((ts, d['unix_time']) for ts, d in ev.get('time_sync', []) if 'unix_time' in d)

    def _seg(self, i):
        (t0, u0), (t1, u1) = (self.ts[i], self.us[i]), (self.ts[i + 1], self.us[i + 1])
        return t0, u0, (u1 - u0) / ((t1 - t0) / 10.0)

    def unix(self, ds):
        if len(self.ts) == 1:
            return self.us[0] + (ds - self.ts[0]) / 10.0
        i = bisect.bisect_right(self.ts, ds) - 1
        i = min(max(i, 0), len(self.ts) - 2)
        t0, u0, rate = self._seg(i)
        return u0 + (ds - t0) / 10.0 * rate


def iso(u):
    return None if u is None else dt.datetime.fromtimestamp(u, TZ).isoformat(timespec='seconds')


def local_date(u):
    return dt.datetime.fromtimestamp(u, TZ).date().isoformat()


def median(xs):
    xs = [x for x in xs if x is not None]
    return st.median(xs) if xs else None


def mad(xs, scale=1.4826):
    """Median absolute deviation, scaled by 1.4826 so it is SD-comparable for normal data."""
    xs = [x for x in xs if x is not None]
    if not xs:
        return None
    m = st.median(xs)
    return scale * st.median(abs(x - m) for x in xs)


def quantile(xs, p):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    k = (len(xs) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def rnd(x, d=1):
    return None if x is None else round(x, d)


def robust_z(xs):
    """Robust z-scores (median/MAD) for a list that may contain None."""
    m, s = median(xs), mad(xs)
    if m is None:
        return [None] * len(xs)
    s = s or 1e-9
    return [None if x is None else (x - m) / s for x in xs]


def rolling(xs, half, fn):
    """Centered rolling window (k-half..k+half) applied to non-None values."""
    n = len(xs)
    out = []
    for k in range(n):
        w = [x for x in xs[max(0, k - half):min(n, k + half + 1)] if x is not None]
        out.append(fn(w) if w else None)
    return out


def latest_battery(db_path=None):
    """Latest decoded ring battery level: debug_data kind=battery_level_changed (else readings
    battery_percent). Returns {pct, voltage_mv, at_unix, at, source} or None. Read-only."""
    db_path = db_path or source_db()
    if not os.path.exists(db_path):
        return None
    con = open_ro(db_path)
    try:
        row = con.execute("SELECT ring_timestamp, decoded_json FROM events WHERE name = 'debug_data' "
                          "AND decoded_json LIKE '%battery_level_changed%' ORDER BY ring_timestamp DESC, id DESC LIMIT 1").fetchone()
        if row:
            d = json.loads(row[1])
            try:
                at = RingClock.from_events(load_events(con, ('time_sync',))).unix(row[0])
            except ValueError:
                at = None
            return {'pct': d.get('battery_pct'), 'voltage_mv': d.get('voltage_mv'), 'at_unix': at and int(at),
                    'at': iso(at), 'source': 'debug_data battery_level_changed'}
        try:
            r = con.execute("SELECT value, captured_unix FROM readings WHERE kind = 'battery_percent' "
                            "ORDER BY captured_unix DESC, id DESC LIMIT 1").fetchone()
        except sqlite3.OperationalError:
            r = None
        if r:
            return {'pct': r[0], 'voltage_mv': None, 'at_unix': r[1], 'at': iso(r[1]), 'source': 'readings'}
        return None
    finally:
        con.close()


def write_json_atomic(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(obj, f, indent=1, separators=(',', ':'))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default
