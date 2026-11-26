#!/usr/bin/env python3
"""ringkeeper ingest service (docs/ARCHITECTURE.md, component 4).

Stdlib only. Receives raw ring event rows from the Android uploader and stores
them idempotently in an open_oura `oura-store`-schema SQLite db (separate from
data/oura.db).

Endpoints
  GET  /v1/health  -> {"ok": true, ...}          (no auth; no data exposed)
  POST /v1/events  -> bearer token required
       {"serial": "...", "cursor": <int|null>,
        "rows": [{"tag": int, "ring_timestamp": int, "body_hex": "..."}]}

Token file (server/tokens.json, chmod 600, gitignored):
  {"devices": [{"name": "my-phone", "token_sha256": "<hex>",
                "serials": ["5038..."]}]}        # serials optional: [] / missing = any
Only SHA-256 hashes of tokens are stored. Mint one with:
  python3 -I server/ingest.py mint-token --name my-phone [--serial SERIAL]
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import stat
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from event_names import event_name  # noqa: E402

VERSION = "0.1.0"
HERE = Path(__file__).resolve().parent
DEFAULT_DB = HERE / "data" / "ingest.db"
DEFAULT_TOKENS = HERE / "tokens.json"

MAX_BODY_BYTES = 4 * 1024 * 1024  # whole request
MAX_ROWS = 10_000  # per request
MAX_EVENT_BYTES = 255  # ring event payload length is a u8
SERIAL_RE = re.compile(r"^[0-9A-Za-z_-]{1,64}$")
HEX_RE = re.compile(r"^(?:[0-9a-fA-F]{2})*$")
U32_MAX = 0xFFFFFFFF
I63_MAX = (1 << 63) - 1

# Verbatim from open_oura crates/oura-store/src/storage.rs SCHEMA (rev e57c43c).
SCHEMA = """
CREATE TABLE IF NOT EXISTS store_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS device (
    serial        TEXT PRIMARY KEY,
    hardware_id   TEXT,
    firmware      TEXT,
    api_version   TEXT,
    mac           TEXT,
    updated_unix  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_state (
    serial        TEXT PRIMARY KEY,
    next_cursor   INTEGER NOT NULL,
    last_sync_unix INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    serial         TEXT NOT NULL,
    tag            INTEGER NOT NULL,
    name           TEXT NOT NULL,
    ring_timestamp INTEGER NOT NULL,
    body           BLOB NOT NULL,
    decoded_json   TEXT,
    captured_unix  INTEGER NOT NULL,
    UNIQUE(serial, tag, ring_timestamp, body)
);
CREATE INDEX IF NOT EXISTS idx_events_serial_tag ON events(serial, tag);
CREATE INDEX IF NOT EXISTS idx_events_capture ON events(captured_unix, id);
CREATE INDEX IF NOT EXISTS idx_events_tag_time ON events(tag, ring_timestamp);
CREATE INDEX IF NOT EXISTS idx_events_decoded_tag ON events(tag) WHERE decoded_json IS NOT NULL;
CREATE TABLE IF NOT EXISTS readings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    serial        TEXT NOT NULL,
    kind          TEXT NOT NULL,
    value         REAL NOT NULL,
    unit          TEXT,
    captured_unix INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_readings_serial_kind ON readings(serial, kind);
"""

log = logging.getLogger("ringkeeper-ingest")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "msg": record.getMessage(),
        }
        out.update(getattr(record, "fields", {}) or {})
        return json.dumps(out, separators=(",", ":"))


def setup_logging(level: str = "INFO") -> None:
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(JsonFormatter())
    log.handlers[:] = [h]
    log.setLevel(level)
    log.propagate = False


def logf(level: int, msg: str, **fields) -> None:
    log.log(level, msg, extra={"fields": fields})


# --------------------------------------------------------------------------- store


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        self.path = path
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        os.chmod(path, 0o600)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            "INSERT OR IGNORE INTO store_meta(key, value) VALUES ('ingest_created_unix', ?)",
            (str(int(time.time())),),
        )

    def insert(self, serial: str, rows: list[tuple[int, int, bytes]], cursor: int | None) -> int:
        now = int(time.time())
        with self.lock:
            c = self.conn
            c.execute("BEGIN IMMEDIATE")
            try:
                before = c.total_changes
                c.executemany(
                    "INSERT OR IGNORE INTO events"
                    " (serial, tag, name, ring_timestamp, body, decoded_json, captured_unix)"
                    " VALUES (?, ?, ?, ?, ?, NULL, ?)",
                    ((serial, t, event_name(t), ts, body, now) for t, ts, body in rows),
                )
                inserted = c.total_changes - before
                if cursor is not None:
                    # Monotonic: never move the stored cursor backwards.
                    c.execute(
                        "INSERT INTO sync_state(serial, next_cursor, last_sync_unix) VALUES (?, ?, ?)"
                        " ON CONFLICT(serial) DO UPDATE SET"
                        " next_cursor = MAX(next_cursor, excluded.next_cursor),"
                        " last_sync_unix = excluded.last_sync_unix",
                        (serial, cursor, now),
                    )
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
        return inserted

    def stats(self) -> dict:
        with self.lock:
            n = self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        return {"events": n}

    def close(self) -> None:
        self.conn.close()


# --------------------------------------------------------------------------- tokens


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


class Tokens:
    """Hashed per-device bearer tokens; reloaded when the file's mtime changes."""

    def __init__(self, path: Path):
        self.path = path
        self._mtime = None
        self._devices: list[dict] = []
        self._lock = threading.Lock()
        self.reload(force=True)

    def reload(self, force: bool = False) -> None:
        with self._lock:
            try:
                st = self.path.stat()
            except FileNotFoundError:
                if force or self._devices:
                    logf(logging.WARNING, "token file missing; all requests will be rejected",
                         path=str(self.path))
                self._devices, self._mtime = [], None
                return
            if not force and st.st_mtime_ns == self._mtime:
                return
            if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                logf(logging.ERROR, "token file permissions too open (need 600); ignoring it",
                     path=str(self.path), mode=oct(st.st_mode & 0o777))
                self._devices, self._mtime = [], st.st_mtime_ns
                return
            try:
                data = json.loads(self.path.read_text())
                devs = []
                for d in data.get("devices", []):
                    h = str(d["token_sha256"]).lower()
                    if not re.fullmatch(r"[0-9a-f]{64}", h):
                        raise ValueError(f"bad token_sha256 for {d.get('name')!r}")
                    devs.append({"name": str(d.get("name", "?")), "token_sha256": h,
                                 "serials": [str(s) for s in d.get("serials") or []]})
            except Exception as e:  # keep previous config on parse error
                logf(logging.ERROR, "token file invalid; keeping previous", error=str(e))
                return
            self._devices, self._mtime = devs, st.st_mtime_ns
            logf(logging.INFO, "tokens loaded", devices=len(devs))

    def authenticate(self, token: str) -> dict | None:
        self.reload()
        h = sha256_hex(token)
        found = None
        with self._lock:
            for d in self._devices:  # constant-time compare against every entry
                if hmac.compare_digest(d["token_sha256"], h):
                    found = d
        return found


# --------------------------------------------------------------------------- validation


class BadRequest(Exception):
    pass


def _int(v, name: str, lo: int, hi: int) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise BadRequest(f"{name} must be an integer")
    if not lo <= v <= hi:
        raise BadRequest(f"{name} out of range [{lo}, {hi}]")
    return v


def validate_payload(obj) -> tuple[str, list[tuple[int, int, bytes]], int | None]:
    if not isinstance(obj, dict):
        raise BadRequest("body must be a JSON object")
    unknown = set(obj) - {"serial", "rows", "cursor"}
    if unknown:
        raise BadRequest(f"unknown fields: {sorted(unknown)}")
    serial = obj.get("serial")
    if not isinstance(serial, str) or not SERIAL_RE.match(serial):
        raise BadRequest("serial must match [0-9A-Za-z_-]{1,64}")
    cursor = obj.get("cursor")
    if cursor is not None:
        cursor = _int(cursor, "cursor", 0, I63_MAX)
    rows = obj.get("rows")
    if not isinstance(rows, list):
        raise BadRequest("rows must be a list")
    if len(rows) > MAX_ROWS:
        raise BadRequest(f"too many rows (max {MAX_ROWS})")
    out = []
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            raise BadRequest(f"rows[{i}] must be an object")
        extra = set(r) - {"tag", "ring_timestamp", "body_hex"}
        if extra:
            raise BadRequest(f"rows[{i}] unknown fields: {sorted(extra)}")
        tag = _int(r.get("tag"), f"rows[{i}].tag", 0, 255)
        ts = _int(r.get("ring_timestamp"), f"rows[{i}].ring_timestamp", 0, U32_MAX)
        bh = r.get("body_hex")
        if not isinstance(bh, str) or not HEX_RE.match(bh):
            raise BadRequest(f"rows[{i}].body_hex must be an even-length hex string")
        body = bytes.fromhex(bh)
        if len(body) > MAX_EVENT_BYTES:
            raise BadRequest(f"rows[{i}].body_hex too long (max {MAX_EVENT_BYTES} bytes)")
        out.append((tag, ts, body))
    return serial, out, cursor


# --------------------------------------------------------------------------- http


class Handler(BaseHTTPRequestHandler):
    server_version = f"ringkeeper-ingest/{VERSION}"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 30  # socket timeout: slowloris guard

    # quiet default stderr access log; we log structured lines ourselves
    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, payload: dict, extra_headers: dict | None = None) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)
        self._status = code

    def _err(self, code: int, msg: str, **hdrs) -> None:
        # Any error may leave an unread request body on the socket: never reuse it.
        self.close_connection = True
        hdrs.setdefault("Connection", "close")
        self._send(code, {"ok": False, "error": msg}, hdrs or None)

    def handle_one_request(self):
        self._status = None
        self._t0 = time.monotonic()
        self._fields = {}
        super().handle_one_request()
        if self._status is not None:
            logf(logging.INFO, "request", method=self.command, path=self.path,
                 status=self._status, ms=round((time.monotonic() - self._t0) * 1000, 1),
                 client=self.client_address[0], **self._fields)

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/v1/health":
            self._send(200, {"ok": True, "service": "ringkeeper-ingest", "version": VERSION,
                             "time_unix": int(time.time())})
        else:
            self._err(404, "not found")

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/v1/events":
            return self._err(404, "not found")
        # auth before reading the body
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or len(auth) > 512:
            self.close_connection = True
            return self._err(401, "missing bearer token", **{"WWW-Authenticate": "Bearer"})
        dev = self.server.tokens.authenticate(auth[7:].strip())
        if dev is None:
            self.close_connection = True
            return self._err(401, "invalid token", **{"WWW-Authenticate": "Bearer"})
        self._fields["device"] = dev["name"]

        ctype = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            self.close_connection = True
            return self._err(415, "Content-Type must be application/json")
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            return self._err(411, "Content-Length required (no chunked encoding)")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self.close_connection = True
            return self._err(411, "Content-Length required")
        if length < 0 or length > MAX_BODY_BYTES:
            self.close_connection = True
            return self._err(413, f"body too large (max {MAX_BODY_BYTES} bytes)")
        raw = self.rfile.read(length)
        if len(raw) != length:
            self.close_connection = True
            return self._err(400, "short body")
        try:
            obj = json.loads(raw)
            serial, rows, cursor = validate_payload(obj)
        except (ValueError, UnicodeDecodeError) as e:
            return self._err(400, f"invalid JSON: {type(e).__name__}")
        except BadRequest as e:
            return self._err(400, str(e))
        if dev["serials"] and serial not in dev["serials"]:
            self._fields["serial"] = serial
            return self._err(403, "serial not allowed for this token")
        try:
            inserted = self.server.store.insert(serial, rows, cursor)
        except sqlite3.Error as e:
            logf(logging.ERROR, "db error", error=str(e))
            return self._err(503, "storage error; retry later", **{"Retry-After": "60"})
        self._fields.update(serial=serial, rows=len(rows), inserted=inserted, cursor=cursor)
        self._send(200, {"ok": True, "received": len(rows), "inserted": inserted,
                         "duplicates": len(rows) - inserted, "cursor": cursor})


class IngestServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        lvl = logging.DEBUG if isinstance(exc, (ConnectionError, TimeoutError)) else logging.ERROR
        logf(lvl, "connection error", client=client_address[0],
             error=f"{type(exc).__name__}: {exc}")

    def __init__(self, addr, store: Store, tokens: Tokens):
        super().__init__(addr, Handler)
        self.store = store
        self.tokens = tokens


def make_server(host: str, port: int, db: Path, tokens: Path) -> IngestServer:
    return IngestServer((host, port), Store(db), Tokens(tokens))


# --------------------------------------------------------------------------- cli


def cmd_serve(a) -> None:
    if a.host not in ("127.0.0.1", "::1", "localhost") and not a.allow_non_loopback:
        sys.exit("refusing to bind non-loopback without --allow-non-loopback")
    srv = make_server(a.host, a.port, Path(a.db), Path(a.tokens))
    logf(logging.INFO, "listening", host=a.host, port=srv.server_address[1], db=str(a.db),
         version=VERSION)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        srv.store.close()


def cmd_mint(a) -> None:
    path = Path(a.tokens)
    data = {"devices": []}
    if path.exists():
        data = json.loads(path.read_text())
    if any(d.get("name") == a.name for d in data["devices"]):
        sys.exit(f"device {a.name!r} already exists in {path}; remove it first to rotate")
    token = secrets.token_urlsafe(32)
    data["devices"].append({"name": a.name, "token_sha256": sha256_hex(token),
                            "serials": a.serial or [], "created_unix": int(time.time())})
    fd = os.open(str(path) + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(str(path) + ".tmp", path)
    os.chmod(path, 0o600)
    print(token)  # shown once; only the hash is stored
    print(f"(stored hash for {a.name!r} in {path}; copy the token above to the device now)",
          file=sys.stderr)


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--tokens", default=os.environ.get("RINGKEEPER_INGEST_TOKENS", str(DEFAULT_TOKENS)))
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=int(os.environ.get("RINGKEEPER_INGEST_PORT", "8092")))
    s.add_argument("--db", default=os.environ.get("RINGKEEPER_INGEST_DB", str(DEFAULT_DB)))
    s.add_argument("--log-level", default="INFO")
    s.add_argument("--allow-non-loopback", action="store_true")
    m = sub.add_parser("mint-token")
    m.add_argument("--name", required=True)
    m.add_argument("--serial", action="append", help="restrict token to this ring serial")
    a = p.parse_args(argv)
    if a.cmd in (None, "serve"):
        if a.cmd is None:
            a = p.parse_args([*(sys.argv[1:] if argv is None else argv), "serve"])
        setup_logging(a.log_level)
        os.umask(0o077)
        cmd_serve(a)
    elif a.cmd == "mint-token":
        cmd_mint(a)


if __name__ == "__main__":
    main()
