"""Tests for tools/dash_api.py (static path safety + /api/ask guards) with a stubbed ask function.

No model, no real data: a temp web root and a fake ask_fn. Run from repo root:
    python3 -I -m unittest discover -s tools/analysis/tests -v
"""
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE))))
import dash_api as D  # noqa: E402


class TestDashApi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = self.tmp.name
        self.root = os.path.join(base, 'www')
        os.makedirs(os.path.join(self.root, 'icons'))
        for p, body in (('index.html', '<h1>hi</h1>'), ('app.js', 'x=1'), ('.secret', 's'), ('icons/a.png', 'png')):
            with open(os.path.join(self.root, p), 'w') as f:
                f.write(body)
        with open(os.path.join(base, 'outside.txt'), 'w') as f:
            f.write('nope')
        os.symlink(os.path.join(base, 'outside.txt'), os.path.join(self.root, 'link.txt'))
        self.asked = []

        def fake_ask(q):
            self.asked.append(q)
            return {'ok': True, 'answer': f'echo: {q}', 'model': 'stub', 'stats': {'wall_s': 0.01}}
        t = [0.0]
        self.clock = t
        lim = D.RateLimiter(limits=((3, 60),), clock=lambda: t[0])
        self.srv = D.make_server('127.0.0.1', 0, self.root, ask_fn=fake_ask, limiter=lim)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data, r

    def ask(self, q, **h):
        hd = {'Content-Type': 'application/json'} | h
        s, d, _ = self.req('POST', '/api/ask', json.dumps({'question': q}), hd)
        return s, json.loads(d)

    def test_static_ok(self):
        s, d, r = self.req('GET', '/')
        self.assertEqual((s, d), (200, b'<h1>hi</h1>'))
        self.assertIn('text/html', r.getheader('Content-Type'))
        self.assertEqual(self.req('GET', '/icons/a.png')[0], 200)
        self.assertEqual(self.req('HEAD', '/app.js')[1], b'')

    def test_traversal_blocked(self):
        for p in ('/../outside.txt', '/%2e%2e/outside.txt', '/..%2foutside.txt', '/icons/../../outside.txt',
                  '/.secret', '/link.txt', '/icons/', '/missing', '/%00'):
            self.assertEqual(self.req('GET', p)[0], 404, p)

    def test_safe_path_unit(self):
        self.assertIsNone(D.safe_path(self.root, '/..\\outside.txt'))
        self.assertTrue(D.safe_path(self.root, '/app.js?v=1').endswith('app.js'))

    def test_ask_ok(self):
        s, j = self.ask('  How   did I sleep? ')
        self.assertEqual(s, 200)
        self.assertEqual(j['source'], 'local AI')
        self.assertEqual(j['answer'], 'echo: How did I sleep?')
        self.assertTrue(j['ok'])

    def test_ask_validation(self):
        self.assertEqual(self.ask('x' * (D.MAX_QUESTION + 1))[0], 400)
        self.assertEqual(self.ask('   ')[0], 400)
        s, _, _ = self.req('POST', '/api/ask', 'question=hi', {'Content-Type': 'application/x-www-form-urlencoded'})
        self.assertEqual(s, 415)
        s, _, _ = self.req('POST', '/api/ask', 'x' * (D.MAX_BODY + 1), {'Content-Type': 'application/json'})
        self.assertEqual(s, 413)
        self.assertEqual(self.ask('hi', Origin='https://evil.example')[0], 403)
        self.assertEqual(self.ask('hi', Origin=f'http://127.0.0.1:{self.port}')[0], 200)
        self.assertEqual(self.req('POST', '/api/other', '{}', {'Content-Type': 'application/json'})[0], 404)
        self.assertEqual(self.asked, ['hi'])

    def test_rate_limit(self):
        for _ in range(3):
            self.assertEqual(self.ask('q')[0], 200)
        s, j = self.ask('q')
        self.assertEqual(s, 429)
        self.clock[0] = 61
        self.assertEqual(self.ask('q')[0], 200)

    def test_refuses_public_bind(self):
        with self.assertRaises(SystemExit):
            D.make_server('0.0.0.0', 0, self.root)


if __name__ == '__main__':
    unittest.main()
