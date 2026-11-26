#!/usr/bin/env python3
"""Tiny local dashboard server: static ui-mockup/ files + POST /api/ask (local AI Q&A).

Stdlib only. Binds loopback only (127.0.0.1); tailscale serve exposes it on the tailnet
(https://<RINGKEEPER_PUBLIC_HOST>:<RINGKEEPER_DASH_PUBLIC_PORT> -> 127.0.0.1:<RINGKEEPER_DASH_PORT>). Same origin as the dashboard,
so no CORS is needed or offered.

    python3 -I tools/dash_api.py [--port $RINGKEEPER_DASH_PORT (8091)] [--root ui-mockup]

Routes
  GET/HEAD /...          files under --root only (no traversal, no dotfiles, no symlinks out,
                         no directory listings; "/" -> index.html)
  GET      /api/health   {"ok", "data_source", "nights"}
  POST     /api/ask      {"question": "..."} -> {"answer", "source": "local AI", "ok", ...}
  GET      /api/export   ZIP: README.txt, nightly.json, cycle_log.json, readiness.json, events.csv
                         (tools/privacy.py build_export; source DB per data/source.json)
  POST     /api/delete   {"confirm": "DELETE MY DATA", "pin": "......"} -> moves the user's data into a
                         timestamped trash folder (tools/privacy.py delete_data; never hard-deletes).
                         PIN = secrets/dashboard_pin (600; delete disabled without it). 5 wrong PINs
                         per 15 min (20/day) lock the endpoint.

/api/ask calls tools/analysis/ai_note.py `ask` (Ollama on loopback only, derived metrics only;
the data/source.json mode decides which DB nightly.json was built from). Guards: JSON body
<= 4 KB, question <= 300 chars, one model call at a time (429 when busy), global rate limit
(6 asks / 60 s, 60 / day), cross-origin POSTs refused (Origin must match Host).
"""
import argparse
import collections
import json
import mimetypes
import os
import shutil
import sys
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, 'analysis'))
sys.path.insert(0, HERE)  # tools/privacy.py (python3 -I does not add the script dir)
import ai_note as A  # noqa: E402
import common as C  # noqa: E402
import privacy as P  # noqa: E402

DEFAULT_ROOT = os.path.join(ROOT, 'ui-mockup')
MAX_BODY = 4096
MAX_QUESTION = 300
ASK_TIMEOUT = 90
TYPES = {'.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8',
         '.html': 'text/html; charset=utf-8', '.json': 'application/json; charset=utf-8',
         '.webmanifest': 'application/manifest+json', '.svg': 'image/svg+xml', '.png': 'image/png',
         '.ico': 'image/x-icon', '.txt': 'text/plain; charset=utf-8'}


class RateLimiter:
    """Sliding-window limits shared by all clients (behind tailscale serve every peer looks like 127.0.0.1)."""

    def __init__(self, limits=((6, 60), (60, 86400)), clock=time.monotonic):
        self.limits, self.clock = limits, clock
        self.hits = collections.deque()
        self.lock = threading.Lock()

    def allow(self):
        with self.lock:
            now = self.clock()
            longest = max(w for _, w in self.limits)
            while self.hits and now - self.hits[0] >= longest:
                self.hits.popleft()
            for n, w in self.limits:
                if sum(1 for t in self.hits if now - t < w) >= n:
                    oldest = [t for t in self.hits if now - t < w][0]
                    return False, max(1, int(w - (now - oldest)) + 1)
            self.hits.append(now)
            return True, 0

    def peek(self):
        """Like allow() but does not record a hit (used to count failures only)."""
        with self.lock:
            now = self.clock()
            for n, w in self.limits:
                recent = [t for t in self.hits if now - t < w]
                if len(recent) >= n:
                    return False, max(1, int(w - (now - recent[0])) + 1)
            return True, 0


def safe_path(root, url_path):
    """Map a URL path to a regular file strictly inside root, or None."""
    p = urllib.parse.unquote(urllib.parse.urlsplit(url_path).path)
    if '\x00' in p or '\\' in p:
        return None
    parts = [x for x in p.split('/') if x]
    if any(x in ('.', '..') or x.startswith('.') for x in parts):
        return None
    root_real = os.path.realpath(root)
    full = os.path.realpath(os.path.join(root_real, *parts)) if parts else root_real
    if os.path.isdir(full):
        full = os.path.join(full, 'index.html')
    if os.path.commonpath([root_real, full]) != root_real or not os.path.isfile(full):
        return None
    return full


def load_nights(path=None):
    return (C.read_json(path or C.NIGHTLY_JSON, {}) or {}).get('nights', [])


def make_handler(root, ask_fn, limiter, busy, priv=None):
    priv = priv or {}
    ppaths = priv.get('paths') or P.Paths()
    pin_file = priv.get('pin_file') or P.PIN_FILE
    pin_fail = priv.get('pin_limiter') or RateLimiter(limits=((5, 900), (20, 86400)))
    priv_lock = threading.Lock()

    class H(BaseHTTPRequestHandler):
        server_version = 'oura-dash/1'
        sys_version = ''
        protocol_version = 'HTTP/1.1'

        def log_message(self, fmt, *args):  # never log bodies/questions; path + status only
            sys.stderr.write('dash_api: %s\n' % (fmt % args))

        def _send(self, status, body=b'', ctype='application/json; charset=utf-8', extra=None, head=False):
            self.send_response(status)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Frame-Options', 'DENY')
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if body and not head:
                self.wfile.write(body)

        def _json(self, status, obj, extra=None):
            self._send(status, json.dumps(obj, ensure_ascii=False).encode(), extra={'Cache-Control': 'no-store'} | (extra or {}))

        # ---- static
        def _static(self, head=False):
            path = urllib.parse.urlsplit(self.path).path
            if path == '/api/health':
                cfg = C.source_config()
                return self._json(200, {'ok': True, 'data_source': cfg['mode'], 'nights': len(load_nights())})
            if path == '/api/export':
                return self._export(head)
            if path.startswith('/api/'):
                return self._json(404, {'error': 'not found'})
            f = safe_path(root, self.path)
            if not f:
                return self._send(404, b'not found\n', 'text/plain; charset=utf-8', head=head)
            ext = os.path.splitext(f)[1].lower()
            ctype = TYPES.get(ext) or mimetypes.guess_type(f)[0] or 'application/octet-stream'
            with open(f, 'rb') as fh:
                body = fh.read()
            name = os.path.basename(f)
            cache = 'no-cache' if ext in ('.html', '.json', '.webmanifest') or name == 'sw.js' else 'public, max-age=86400'
            if name == 'real-data.json':
                cache = 'no-store'
            self._send(200, body, ctype, extra={'Cache-Control': cache}, head=head)

        def do_GET(self):  # noqa: N802
            self._static()

        def do_HEAD(self):  # noqa: N802
            self._static(head=True)

        # ---- privacy
        def _export(self, head=False):
            if not priv_lock.acquire(blocking=False):
                return self._json(429, {'error': 'an export or delete is already running'}, extra={'Retry-After': '5'})
            try:
                fh, info = P.build_export(ppaths)
            finally:
                priv_lock.release()
            with fh:
                size = fh.seek(0, os.SEEK_END)
                fh.seek(0)
                name = f'ringkeeper-export-{time.strftime("%Y%m%d-%H%M%S")}.zip'
                self.send_response(200)
                for k, v in (('Content-Type', 'application/zip'), ('Content-Length', str(size)),
                             ('Content-Disposition', f'attachment; filename="{name}"'), ('Cache-Control', 'no-store'),
                             ('X-Content-Type-Options', 'nosniff'), ('Referrer-Policy', 'no-referrer'),
                             ('X-Export-Rows', str(info['rows']))):
                    self.send_header(k, v)
                self.end_headers()
                if not head:
                    shutil.copyfileobj(fh, self.wfile, 1 << 16)

        def _delete(self, doc):
            confirm = doc.get('confirm') if isinstance(doc, dict) else None
            pin = doc.get('pin') if isinstance(doc, dict) else None
            if confirm != P.CONFIRM_PHRASE:
                return self._json(400, {'error': f'type the confirm phrase exactly: {P.CONFIRM_PHRASE}'})
            if P.load_pin(pin_file) is None:
                return self._json(503, {'error': 'delete is disabled: no dashboard PIN is set up on the PC'})
            ok, wait = pin_fail.peek()
            if not ok:
                return self._json(429, {'error': f'too many wrong PINs; try again in {wait} s'}, extra={'Retry-After': str(wait)})
            if not P.check_pin(pin, pin_file):
                pin_fail.allow()
                return self._json(403, {'error': 'wrong PIN'})
            if not priv_lock.acquire(blocking=False):
                return self._json(429, {'error': 'an export or delete is already running'}, extra={'Retry-After': '5'})
            try:
                res = P.delete_data(confirm, ppaths)
            except BlockingIOError:
                return self._json(409, {'error': 'a morning sync is running; try again in a few minutes'})
            except Exception as e:  # report, never half-silently
                sys.stderr.write(f'dash_api: delete failed: {type(e).__name__}\n')
                return self._json(500, {'error': f'delete failed ({type(e).__name__}); nothing further was moved'})
            finally:
                priv_lock.release()
            sys.stderr.write(f"dash_api: data moved to trash ({len(res['moved'])} items)\n")
            return self._json(200, {'ok': True, 'trash_dir': res['trash_dir'], 'keep_until': res['keep_until'],
                                    'moved': res['moved'], 'ingest_rows_moved': res['ingest_rows_moved'],
                                    'not_touched': res['not_touched']})

        # ---- api
        def do_POST(self):  # noqa: N802
            route = urllib.parse.urlsplit(self.path).path
            if route not in ('/api/ask', '/api/delete'):
                self.close_connection = True
                return self._json(404, {'error': 'not found'})
            try:
                n = int(self.headers.get('Content-Length') or -1)
            except ValueError:
                n = -1
            if n < 0 or n > MAX_BODY:
                self.close_connection = True  # body left unread: never reuse this connection
                return self._json(411 if n < 0 else 413, {'error': 'Content-Length required' if n < 0 else 'body too large'})
            raw = self.rfile.read(n)
            origin = self.headers.get('Origin')
            hosts = {self.headers.get('Host', ''), self.headers.get('X-Forwarded-Host', '')} - {''}
            if origin and urllib.parse.urlsplit(origin).netloc not in hosts:
                return self._json(403, {'error': 'cross-origin request refused'})
            if 'application/json' not in (self.headers.get('Content-Type') or ''):
                return self._json(415, {'error': 'send application/json'})
            if route == '/api/delete':
                try:
                    return self._delete(json.loads(raw or b'{}'))
                except ValueError:
                    return self._json(400, {'error': 'invalid JSON'})
            try:
                doc = json.loads(raw or b'{}')
                q = doc.get('question') if isinstance(doc, dict) else None
            except ValueError:
                q = None
            if not isinstance(q, str) or not q.strip():
                return self._json(400, {'error': 'question (string) required'})
            q = ' '.join(q.split())
            if len(q) > MAX_QUESTION:
                return self._json(400, {'error': f'question too long (max {MAX_QUESTION} characters)'})
            if not busy.acquire(blocking=False):
                return self._json(429, {'error': 'the local AI is busy with another question; try again shortly'},
                                  extra={'Retry-After': '5'})
            try:
                ok, wait = limiter.allow()
                if not ok:
                    return self._json(429, {'error': f'too many questions; try again in {wait} s'},
                                      extra={'Retry-After': str(wait)})
                res = ask_fn(q)
            finally:
                busy.release()
            out = {'answer': res.get('answer', ''), 'source': 'local AI', 'ok': bool(res.get('ok')),
                   'model': res.get('model'), 'data_source': C.source_config()['mode'],
                   'wall_s': (res.get('stats') or {}).get('wall_s')}
            if not res.get('ok'):
                out['error'] = res.get('error')
            self._json(200, out)

    return H


def default_ask(q):
    return A.ask(load_nights(), q, timeout=ASK_TIMEOUT)


def make_server(host='127.0.0.1', port=8093, root=DEFAULT_ROOT, ask_fn=default_ask, limiter=None, priv=None):
    """priv: optional {'paths': privacy.Paths, 'pin_file': str, 'pin_limiter': RateLimiter} (tests)."""
    if host not in ('127.0.0.1', '::1', 'localhost'):
        raise SystemExit('dash_api: refusing to bind a non-loopback address (use tailscale serve)')
    h = make_handler(root, ask_fn, limiter or RateLimiter(), threading.Lock(), priv)
    srv = ThreadingHTTPServer((host, port), h)
    srv.daemon_threads = True
    return srv


def main(argv=None):
    ap = argparse.ArgumentParser(description='ringkeeper dashboard: static files + local-AI /api/ask (loopback only)')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=int(os.environ.get('RINGKEEPER_DASH_PORT', '8091')))
    ap.add_argument('--root', default=DEFAULT_ROOT)
    a = ap.parse_args(argv)
    A.assert_loopback(A.DEFAULT_URL)
    srv = make_server(a.host, a.port, a.root)
    print(f'dash_api: serving {a.root} + /api/ask on http://{a.host}:{a.port} '
          f'(data source: {C.source_config()["mode"]}, model: {A.DEFAULT_MODEL})', file=sys.stderr, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
