"""Tests for tools/analysis/ai_note.py against a MOCKED local Ollama endpoint (synthetic data only).

No real model, no network beyond 127.0.0.1. Run from repo root:
    python3 -I -m unittest discover -s tools/analysis/tests -v
"""
import contextlib
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import ai_note as A  # noqa: E402
import sleep_note as SN  # noqa: E402
from test_analysis import fake_nights  # noqa: E402

GOOD = ("You slept about 7h 10m, with roughly 1h 30m of estimated deep sleep.\n"
        "Your resting heart rate was 50 bpm.\n"
        "Try a calm wind-down tonight.")


class Mock(http.server.BaseHTTPRequestHandler):
    replies = []      # queue of (status, body_dict) or ('sleep', seconds)
    seen = []         # request bodies

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(n))
        Mock.seen.append((self.path, body))
        item = Mock.replies.pop(0) if Mock.replies else (200, {'message': {'content': GOOD}})
        if item[0] == 'sleep':
            time.sleep(item[1])
            item = (200, {'message': {'content': GOOD}})
        status, payload = item
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except BrokenPipeError:  # client timed out (test_timeout_falls_back)
            pass

    def log_message(self, *a):
        pass


def reply(text, **kw):
    return (200, {'model': 'mock', 'message': {'role': 'assistant', 'content': text},
                  'eval_count': 40, 'eval_duration': 800_000_000} | kw)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Mock)
        cls.url = f'http://127.0.0.1:{cls.srv.server_address[1]}'
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        Mock.replies, Mock.seen = [], []
        self.nights = fake_nights(1)
        self.tmp = tempfile.TemporaryDirectory()
        self.nolog = os.path.join(self.tmp.name, 'no_cycle_log.json')

    def tearDown(self):
        self.tmp.cleanup()

    def note(self, nights=None, **kw):
        return A.make_note(self.nights if nights is None else nights, url=self.url, model='mock',
                           timeout=kw.pop('timeout', 5), cycle_log=self.nolog, **kw)


class TestContext(Base):
    def test_derived_only(self):
        ns = fake_nights(3)
        ns[-1].update(epochs30='0123' * 100, series={'hr': [50] * 99}, bed_unix=1, wake_unix=2)
        ns[-1]['stages'].update(epochs30='012' * 50, epochs5=[1, 2, 3])
        ctx = A.build_context(ns, cycle_log=self.nolog)
        s = json.dumps(ctx)
        for raw in ('epochs', 'series', 'bed_unix', 'wake_unix', 'ibi', 'decoded'):
            self.assertNotIn(raw, s)
        self.assertEqual(ctx['night']['asleep_est_hm'], '7h 10m')
        self.assertEqual(ctx['stages_estimated']['minutes']['deep'], 90)
        self.assertEqual(len(ctx['recent_nights']), 3)
        self.assertNotIn('cycle', ctx)
        self.assertLess(len(s), 2500)  # compact

    def test_no_nights(self):
        self.assertIsNone(A.build_context([], cycle_log=self.nolog))


class TestLoopbackOnly(Base):
    def test_rejects_remote(self):
        for u in ('http://api.openai.com', 'https://192.168.1.10:11434', 'http://10.0.0.2', 'http://example.com',
                  'ftp://127.0.0.1', 'http://127.0.0.1.evil.com'):
            with self.assertRaises(A.AIError, msg=u):
                A.assert_loopback(u)
        for u in ('http://127.0.0.1:11434', 'http://localhost:11434', 'http://[::1]:11434', 'http://127.0.0.2'):
            A.assert_loopback(u)

    def test_remote_never_contacted(self):
        n = A.make_note(self.nights, url='http://203.0.113.5:11434', model='m', timeout=1, cycle_log=self.nolog)
        self.assertEqual(n['source'], 'template')
        self.assertIn('non-loopback', n['ai_error'])

    def test_cli_refuses_remote(self):
        self.assertEqual(A.main(['note', '--url', 'http://8.8.8.8', '--json-out', '-']), 2)


class TestNote(Base):
    def test_good_reply(self):
        Mock.replies = [reply(GOOD)]
        n = self.note()
        self.assertEqual(n['source'], 'ai', n)
        self.assertEqual(n['lines'][-1], A.DISCLAIMER)
        self.assertLessEqual(len(n['lines']), 5)
        self.assertEqual(n['stats']['tokens_per_s'], 50.0)
        path, body = Mock.seen[0]
        self.assertEqual(path, '/api/chat')
        self.assertFalse(body['stream'])
        self.assertIn('"asleep_est_hm":"7h 10m"', body['messages'][1]['content'])

    def test_paragraph_split_into_lines(self):
        Mock.replies = [reply('You slept about 7h 10m. Your resting heart rate was 50 bpm. Try a wind-down.')]
        n = self.note()
        self.assertEqual(n['source'], 'ai')
        self.assertEqual(len(n['lines']), 4)

    def test_think_block_stripped(self):
        Mock.replies = [reply('<think>hmm 999</think>\n' + GOOD)]
        self.assertEqual(self.note()['source'], 'ai')

    def test_diagnosis_word_falls_back(self):
        bad = 'Your low HRV could mean sleep apnea.\nConsider seeing a doctor.'
        Mock.replies = [reply(bad), reply(bad)]
        n = self.note()
        self.assertEqual(n['source'], 'template')
        self.assertIn('banned', n['ai_error'])
        self.assertEqual(n['text'], SN.build_note(self.nights)['text'])
        self.assertEqual(len(Mock.seen), 2)  # one retry

    def test_retry_then_ok(self):
        Mock.replies = [reply('You might have insomnia.'), reply(GOOD)]
        n = self.note()
        self.assertEqual(n['source'], 'ai')
        self.assertEqual(n['attempts'], 2)
        self.assertEqual(len(n['rejected']), 1)
        self.assertIn('rejected', Mock.seen[1][1]['messages'][-1]['content'])

    def test_too_long(self):
        long = '\n'.join(f'Line {i}.' for i in range(1, 8))
        Mock.replies = [reply(long), reply('x' * 300)]
        n = self.note()
        self.assertEqual(n['source'], 'template')

    def test_invented_number(self):
        Mock.replies = [reply('You slept 9h 41m and your resting heart rate was 38 bpm.')] * 2
        n = self.note()
        self.assertEqual(n['source'], 'template')
        self.assertIn('numbers not in context', n['ai_error'])

    def test_comparison_without_baseline(self):
        Mock.replies = [reply('Your resting heart rate was 50 bpm, lower than usual.')] * 2
        n = self.note()
        self.assertEqual(n['source'], 'template')
        self.assertIn('comparison', n['ai_error'])

    def test_comparison_allowed_with_baseline(self):
        ns = fake_nights(20)
        Mock.replies = [reply('Your resting heart rate was 50 bpm, right in your usual range.')]
        self.assertEqual(self.note(ns)['source'], 'ai')

    def test_learning_your_normal_ok(self):
        Mock.replies = [reply('Your resting heart rate was 50 bpm; the ring is still learning your normal.')]
        self.assertEqual(self.note()['source'], 'ai')

    def test_server_error_falls_back(self):
        Mock.replies = [(500, {'error': 'model not found'})]
        n = self.note()
        self.assertEqual(n['source'], 'template')
        self.assertEqual(len(Mock.seen), 1)  # no retry on transport errors

    def test_error_field(self):
        Mock.replies = [(200, {'error': 'out of memory'})]
        self.assertIn('out of memory', self.note()['ai_error'])

    def test_timeout_falls_back(self):
        Mock.replies = [('sleep', 1.5)]
        t0 = time.monotonic()
        n = self.note(timeout=0.5)
        self.assertLess(time.monotonic() - t0, 1.4)
        self.assertEqual(n['source'], 'template')

    def test_server_down(self):
        n = A.make_note(self.nights, url='http://127.0.0.1:9', model='m', timeout=2, cycle_log=self.nolog)
        self.assertEqual(n['source'], 'template')

    def test_no_nights(self):
        n = self.note([])
        self.assertEqual(n['source'], 'template')
        self.assertEqual(Mock.seen, [])

    def test_cli_writes_json(self):
        nightly = os.path.join(self.tmp.name, 'nightly.json')
        out = os.path.join(self.tmp.name, 'ai.json')
        with open(nightly, 'w') as f:
            json.dump({'nights': self.nights}, f)
        Mock.replies = [reply(GOOD)]
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            rc = A.main(['note', '--nightly', nightly, '--json-out', out, '--url', self.url, '--model', 'mock',
                         '--cycle-log', self.nolog])
        self.assertIn('7h 10m', buf.getvalue())
        self.assertEqual(rc, 0)
        j = json.load(open(out))
        self.assertEqual(j['source'], 'ai')
        self.assertEqual(j['night_date'], self.nights[-1]['date'])
        self.assertTrue(j['local_only'])


class TestAsk(Base):
    def q(self, question, **kw):
        return A.ask(self.nights, question, url=self.url, model='mock', timeout=5, cycle_log=self.nolog, **kw)

    def test_answer(self):
        Mock.replies = [reply('You had about 1h 30m of estimated deep sleep.')]
        r = self.q('How much deep sleep did I get?')
        self.assertTrue(r['ok'])
        self.assertIn('deep sleep', r['answer'])
        self.assertIn('Question: How much deep sleep', Mock.seen[0][1]['messages'][1]['content'])

    def test_diagnosis_blocked(self):
        Mock.replies = [reply('Based on this, you probably have sleep apnea.')]
        r = self.q('Do I have apnea?')
        self.assertFalse(r['ok'])
        self.assertIn('qualified professional', r['answer'])

    def test_empty_and_down(self):
        self.assertFalse(self.q('  ')['ok'])
        r = A.ask(self.nights, 'hi', url='http://127.0.0.1:9', timeout=1, cycle_log=self.nolog)
        self.assertFalse(r['ok'])
        self.assertIn('not reachable', r['answer'])


if __name__ == '__main__':
    unittest.main()
