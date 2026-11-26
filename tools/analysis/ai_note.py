#!/usr/bin/env python3
"""Local-LLM morning note + Q&A over DERIVED sleep metrics (Ollama on this PC only).

Health data never leaves the house: the only network call is an HTTP POST to a local
Ollama server, and the URL is checked to be loopback (127.0.0.1 / ::1 / localhost)
before anything is sent. No cloud API, no fallbacks to remote services.

What the model sees (build_context): per-night summary numbers from data/nightly.json
(time in bed, estimated sleep/stages, resting HR, HRV, skin temp, movement), the
baseline comparison (baselines.py), a 7-night compact trend, and — if a period log
exists — cycle day / phase / days until next period (cycle.py). Never raw ring events,
epochs, series, or anything identifying.

Usage (from repo root):
    python3 -I tools/analysis/ai_note.py [note] [--json-out data/ai_note.json] [--timeout 90]
    python3 -I tools/analysis/ai_note.py ask "Why was my HRV lower last night?" [--json]
    python3 -I tools/analysis/ai_note.py context        # print the exact JSON the model gets

`note` always prints a note and exits 0: if Ollama is down, times out, or the reply fails the
strict check (≤4 model lines + fixed disclaimer line, length caps, no diagnosis words, numbers
must come from the context), it falls back to the sleep_note.py template (source="template").
Env: RINGKEEPER_AI_URL (default http://127.0.0.1:11434), RINGKEEPER_AI_MODEL.
"""
import argparse
import datetime as dt
import ipaddress
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import baselines as B  # noqa: E402
import common as C  # noqa: E402
import sleep_note as SN  # noqa: E402

DEFAULT_URL = os.environ.get('RINGKEEPER_AI_URL', 'http://127.0.0.1:11434')
# qwen3 4B instruct-2507 (Q4_K_M, 2.5 GB file, ~3.2 GB loaded at 4k ctx): fits the RTX 3050 6 GB
# fully on GPU with room to spare; non-thinking variant -> short, direct answers. See README.
DEFAULT_MODEL = os.environ.get('RINGKEEPER_AI_MODEL', 'qwen3:4b-instruct-2507-q4_K_M')
AI_NOTE_JSON = os.path.join(C.DATA_DIR, 'ai_note.json')
DISCLAIMER = 'Local AI summary of ring estimates, for awareness only, not medical advice.'
MAX_MODEL_LINES = 4          # + the fixed disclaimer line = 5
MAX_LINE_CHARS = 220
MAX_NOTE_CHARS = 700
MAX_ANSWER_LINES = 8
MAX_ANSWER_CHARS = 1200
NUM_CTX = 4096

# Words that would turn an awareness note into a medical claim. Matched case-insensitively
# as word prefixes ("diagnos" catches diagnose/diagnosis/diagnosed).
BANNED = (
    'diagnos', 'disease', 'disorder', 'syndrome', 'condition', 'symptom', 'illness', 'sick',
    'infection', 'fever', 'covid', 'flu', 'apnea', 'apnoea', 'insomnia', 'narcolep', 'arrhythm',
    'afib', 'a-fib', 'atrial', 'tachycard', 'bradycard', 'hypertens', 'hypotens', 'cardiac',
    'heart attack', 'stroke', 'diabet', 'cancer', 'tumou?r', 'depress', 'anxiety disorder',
    'medication', 'medicine', 'prescri', 'dosage', 'supplement', 'melatonin', 'treat', 'cure',
    'pregnan', 'fertil', 'ovulat', 'contracep', 'birth control', 'abnormal', 'patholog',
    'clinical', 'emergency', 'see a doctor', 'consult',
)
BANNED_RE = re.compile(r'\b(?:' + '|'.join(BANNED) + r')', re.I)
# Q&A is allowed a bit more vocabulary ("condition", "treat yourself"), but never named illnesses,
# diagnoses or dosing. A model refusal that names the illness also trips this -> safe canned reply.
ANSWER_BANNED_RE = re.compile(r'\b(?:diagnos|disease|disorder|syndrome|apnea|apnoea|insomnia|narcolep|arrhythm|'
                              r'afib|atrial|tachycard|bradycard|infection|fever|covid|hypertens|diabet|cancer|'
                              r'pregnan|ovulat|contracep|prescri|dosage|melatonin|\d+\s?mg\b)', re.I)

SYSTEM_NOTE = (
    "You write a short, friendly morning note about last night's sleep for the person who wore "
    "a sleep ring. Use ONLY the numbers in the JSON you are given; never invent or recompute numbers. "
    "Stage minutes are estimates from a home-made heuristic: call them 'estimated'. "
    "Comparisons with 'your usual' are only allowed for metrics whose status is high, low or ok; "
    "if status is insufficient, say the ring is still learning your normal and do NOT call anything "
    "typical, normal, usual, great or good for you. Mention estimated sleep time and one or two stage "
    "or heart numbers at most; do not list everything. "
    "Write durations like the *_hm fields (e.g. '7h 05m'), not raw minutes. "
    "Rules: plain text, at most 4 lines, each line ONE short sentence under 120 characters, "
    "second person ('you'), warm but not cheesy, "
    "no markdown, no bullet points, no emojis, no headings. "
    "This is NOT medical: never name or hint at any illness, condition, diagnosis, medication or treatment, "
    "never say anything is abnormal, never tell the reader to see a doctor. "
    "Do not mention fertility, ovulation or pregnancy. One gentle, everyday suggestion "
    "(e.g. wind-down, consistent bedtime, daylight) is fine if it fits the data."
)
SYSTEM_ASK = (
    "You answer questions about the user's own sleep-ring data on their own computer. "
    "Use ONLY the JSON context; if the answer is not in it, say so briefly. Never invent numbers. "
    "Stage values are heuristic estimates; baselines need several nights before comparisons mean much. "
    "You are not a doctor: do not diagnose, do not name illnesses as explanations, do not recommend "
    "medication or doses. Everyday habits (sleep timing, caffeine, alcohol, exercise timing, light) are fine "
    "as general ideas. If the question is clearly medical or about fertility/contraception, say this tool "
    "can't answer that and suggest asking a qualified professional. Plain text, at most 6 short lines."
)


class AIError(Exception):
    pass


# ------------------------------------------------------------------ context (derived only)

def _r(x, d=1):
    return None if x is None else round(float(x), d)


def _clock(iso_s):
    return dt.datetime.fromisoformat(iso_s).strftime('%H:%M') if iso_s else None


def _cycle_block(nights, log_path=None):
    """Minimal cycle context if a period log exists; never fatal; no fertility fields."""
    try:
        import cycle as CY
        log_path = log_path or CY.CYCLE_LOG
        if not os.path.exists(log_path):
            return None
        periods, settings = CY.load_log(log_path)
        if not periods:
            return None
        res = CY.analyze(nights, [{'start': p['start'].isoformat(), 'end': p['end'] and p['end'].isoformat(),
                                   'flow': p['flow'], 'symptoms': p['symptoms']} for p in periods],
                         settings=settings)
    except Exception:
        return None
    if res.get('status') != 'ok' or not res.get('current'):
        return {'status': res.get('status')}
    cur, pred = res['current'], res.get('prediction') or {}
    phase = cur.get('phase')
    return {
        'status': 'ok', 'cycle_day': cur.get('cycle_day'),
        'phase': 'mid-cycle' if phase == 'fertile' else phase,
        'in_period': cur.get('in_period'),
        'next_period_in_days': pred.get('days_until'), 'prediction_confidence': pred.get('confidence'),
        'note': 'estimates from skin temperature and a period log',
    }


def build_context(nights, cycle_log=None, with_cycle=True):
    ns = [x for x in nights if x.get('kind', 'night') == 'night']
    if not ns:
        return None
    n = ns[-1]
    s = n.get('stages') or {}
    ev = B.evaluate(nights)
    words = {'rhr': 'resting_hr_bpm', 'rmssd': 'hrv_rmssd_ms', 'temp_dev': 'skin_temp_vs_usual_c'}
    base = {}
    for k, m in ev.get('metrics', {}).items():
        w14 = (m.get('windows') or {}).get('14') or {}
        d = {'last_night': m.get('value'), 'status': m.get('status')}
        if w14.get('median') is not None and k != 'temp_dev':
            d['usual_14_night_median'] = w14['median']
        if m.get('need_more'):
            d['nights_needed_before_comparing'] = m['need_more']
        base[words.get(k, k)] = d
    ctx = {
        'night': {
            'date_woke': n.get('date'),
            'bedtime': _clock(n.get('bed')), 'wake_time': _clock(n.get('wake')),
            'time_in_bed_min': _r(n.get('time_in_bed_min'), 0), 'time_in_bed_hm': SN.hm(n.get('time_in_bed_min')),
            'asleep_min_est': _r(n.get('sleep_min_est'), 0),
            'asleep_est_hm': SN.hm(n.get('sleep_min_est')) if n.get('sleep_min_est') is not None else None,
            'fell_asleep_after_min_est': _r(n.get('latency_min_est'), 0),
            'awake_after_falling_asleep_min_est': _r(n.get('waso_min_est'), 0),
            'sleep_efficiency_pct_est': _r(n.get('efficiency_pct_est'), 0),
            'resting_hr_bpm': _r(n.get('hr_low'), 0), 'median_hr_bpm': _r(n.get('hr_median'), 0),
            'hrv_rmssd_avg_ms': _r(n.get('rmssd_avg'), 0),
            'skin_temp_avg_c': _r(n.get('temp_avg_c'), 1),
            'movement_pct_of_night': _r(n.get('movement_index_pct'), 0),
        },
        'stages_estimated': ({'minutes': {k: _r(v, 0) for k, v in (s.get('minutes') or {}).items()},
                              'hm': {k: SN.hm(v) for k, v in (s.get('minutes') or {}).items()},
                              'pct_of_sleep': {k: _r(v, 0) for k, v in (s.get('pct_of_sleep') or {}).items()}}
                             if s.get('ok') else None),
        'vs_your_usual': base,
        'nights_on_file': len(ns),
        'recent_nights': [{'date_woke': x.get('date'), 'asleep_min_est': _r(x.get('sleep_min_est'), 0),
                           'resting_hr_bpm': _r(x.get('hr_low'), 0), 'hrv_ms': _r(x.get('rmssd_avg'), 0)}
                          for x in ns[-7:]],
    }
    if with_cycle:
        cyc = _cycle_block(nights, cycle_log)
        if cyc:
            ctx['cycle'] = cyc
    return ctx


# ------------------------------------------------------------------ local HTTP client

def assert_loopback(url):
    """Refuse anything that is not this machine: the privacy guarantee lives here."""
    u = urllib.parse.urlparse(url)
    if u.scheme not in ('http', 'https') or not u.hostname:
        raise AIError(f'bad AI url: {url!r}')
    h = u.hostname
    if h == 'localhost':
        return
    try:
        if ipaddress.ip_address(h).is_loopback:
            return
    except ValueError:
        pass
    raise AIError(f'refusing non-loopback AI endpoint {h!r}: health data stays on this PC')


def chat(messages, url=DEFAULT_URL, model=DEFAULT_MODEL, timeout=90, num_predict=220, temperature=0.4):
    assert_loopback(url)
    body = json.dumps({
        'model': model, 'messages': messages, 'stream': False,
        'options': {'temperature': temperature, 'num_predict': num_predict, 'num_ctx': NUM_CTX, 'seed': 7},
        'keep_alive': '10m',
    }).encode()
    req = urllib.request.Request(url.rstrip('/') + '/api/chat', data=body,
                                 headers={'Content-Type': 'application/json'}, method='POST')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never via a proxy
    t0 = time.monotonic()
    try:
        with opener.open(req, timeout=timeout) as resp:
            j = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        with e:
            detail = e.read(300).decode('utf-8', 'replace')
        raise AIError(f'HTTP {e.code}: {detail}') from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise AIError(f'{type(e).__name__}: {e}') from None
    if j.get('error'):
        raise AIError(str(j['error']))
    text = ((j.get('message') or {}).get('content') or '')
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.S).strip()
    ec, ed = j.get('eval_count'), j.get('eval_duration')
    stats = {'model': j.get('model', model), 'wall_s': round(time.monotonic() - t0, 2),
             'eval_tokens': ec, 'tokens_per_s': round(ec / ed * 1e9, 1) if ec and ed else None}
    return text, stats


# ------------------------------------------------------------------ strict output check

def _numbers_in(obj, out=None):
    out = set() if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            _numbers_in(v, out)
            if (isinstance(v, (int, float)) and not isinstance(v, bool)
                    and (k.endswith('_min') or k.endswith('_min_est'))):
                # minutes may be phrased as hours / "Xh Ym"
                out.update({round(v / 60, 1), float(int(v // 60)), float(int(round(v)) % 60), float(round(v / 60))})
    elif isinstance(obj, list):
        for v in obj:
            _numbers_in(v, out)
    elif isinstance(obj, bool):
        pass
    elif isinstance(obj, (int, float)):
        out.update({float(obj), float(round(obj)), round(float(obj), 1), abs(float(obj)), abs(round(float(obj), 1))})
    elif isinstance(obj, str):
        for m in re.findall(r'\d+(?:\.\d+)?', obj):
            out.add(float(m))
    return out


def _minutes_under(ctx):
    """'stages_estimated.minutes' values are plain dict values without *_min keys: add h/m forms."""
    out = set()
    st_ = (ctx or {}).get('stages_estimated') or {}
    for v in (st_.get('minutes') or {}).values():
        if v is not None:
            out.update({round(v / 60, 1), float(int(v // 60)), float(int(round(v)) % 60), float(round(v / 60))})
    return out


def unknown_numbers(text, ctx):
    """Numbers in `text` not traceable to the context (tolerance 1 unit; small ints 0-12 allowed)."""
    allowed = _numbers_in(ctx) | _minutes_under(ctx)
    bad = []
    for m in re.findall(r'(?<![\w.])[-+]?\d+(?:\.\d+)?', text):
        v = abs(float(m))
        if v <= 12 and v == int(v):
            continue  # "4 nights", "2 more", "7am" ... too ambiguous to police
        if any(abs(v - a) <= (0.11 if v != int(v) else 1.0) for a in allowed):
            continue
        bad.append(m)
    return bad


def check_note(text, ctx):
    """Return (clean_lines, None) or (None, reason)."""
    if not text or not text.strip():
        return None, 'empty'
    lines = [re.sub(r'^\s*(?:[-*•]|\d+[.)])\s+', '', ln).strip() for ln in text.strip().splitlines()]
    lines = [ln.replace('**', '').strip() for ln in lines if ln.strip()]
    # models like to answer in one paragraph: one sentence per line is the house style
    lines = [s.strip() for ln in lines for s in re.split(r'(?<=[.!?])\s+(?=[A-Z])', ln) if s.strip()]
    if not lines:
        return None, 'empty'
    if len(lines) > MAX_MODEL_LINES:
        return None, f'too many lines ({len(lines)})'
    if any(len(ln) > MAX_LINE_CHARS for ln in lines):
        return None, 'line too long'
    if sum(len(ln) for ln in lines) > MAX_NOTE_CHARS:
        return None, 'too long'
    if re.search(r'https?://|www\.|#', ' '.join(lines)):
        return None, 'link/markdown'
    m = BANNED_RE.search(' '.join(lines))
    if m:
        return None, f'banned word: {m.group(0)!r}'
    # no comparison claims before baselines exist ("typical", "normal", "usual" ...)
    statuses = {d.get('status') for d in ((ctx or {}).get('vs_your_usual') or {}).values()}
    if not statuses & {'ok', 'high', 'low'}:
        joined = re.sub(r'learning (?:what\'?s |what is )?(?:your )?(?:usual|normal|typical)\w*', '', ' '.join(lines), flags=re.I)
        m = re.search(r'\b(?:typical|usual|normal|average|higher than|lower than|better than|worse than)\b',
                      joined, re.I)
        if m:
            return None, f'comparison without baseline: {m.group(0)!r}'
    m = re.search(r'\b(?:healthy|unhealthy|dangerous|worrying|concerning|alarming)\b', ' '.join(lines), re.I)
    if m:
        return None, f'judgement word: {m.group(0)!r}'
    bad = unknown_numbers(' '.join(lines), ctx)
    if bad:
        return None, f'numbers not in context: {bad[:5]}'
    return lines, None


def check_answer(text):
    if not text or not text.strip():
        return None, 'empty'
    lines = [ln.rstrip() for ln in text.strip().splitlines() if ln.strip()]
    if len(lines) > MAX_ANSWER_LINES or sum(len(x) for x in lines) > MAX_ANSWER_CHARS:
        return None, 'too long'
    m = ANSWER_BANNED_RE.search(text)
    if m:
        return None, f'banned phrase: {m.group(0)!r}'
    return lines, None


# ------------------------------------------------------------------ note / ask

def _ctx_json(ctx):
    return json.dumps(ctx, separators=(',', ':'), ensure_ascii=False)


def make_note(nights, url=DEFAULT_URL, model=DEFAULT_MODEL, timeout=90, cycle_log=None):
    """Always returns a note dict; source='ai' or 'template' (with `ai_error`)."""
    now = C.iso(dt.datetime.now(C.TZ).timestamp())
    tmpl = SN.build_note(nights)
    ctx = build_context(nights, cycle_log)
    night_date = (ctx or {}).get('night', {}).get('date_woke')
    base = {'generated': now, 'night_date': night_date, 'model': model, 'local_only': True}
    if ctx is None:
        return base | {'source': 'template', 'text': tmpl['text'], 'lines': tmpl['text'].split('\n'),
                       'ai_error': 'no nights'}
    msgs = [{'role': 'system', 'content': SYSTEM_NOTE},
            {'role': 'user', 'content': 'Data (JSON):\n' + _ctx_json(ctx) +
             '\n\nWrite the morning note now (max 4 short lines).'}]
    err, stats, rejected = None, {}, []
    for attempt in range(2):  # one retry with a nudge if the check fails
        try:
            text, stats = chat(msgs, url=url, model=model, timeout=timeout)
        except AIError as e:
            err = str(e)
            break  # server down / timeout: do not wait twice
        lines, err = check_note(text, ctx)
        if lines:
            lines = lines + [DISCLAIMER]
            return base | {'source': 'ai', 'text': '\n'.join(lines), 'lines': lines, 'attempts': attempt + 1,
                           'rejected': rejected, 'stats': stats}
        rejected.append(err)
        msgs = msgs + [{'role': 'assistant', 'content': text},
                       {'role': 'user', 'content': f'That reply was rejected ({err}). Rewrite it: max 4 short '
                        'plain lines, only numbers from the JSON, no medical words.'}]
    return base | {'source': 'template', 'text': tmpl['text'], 'lines': tmpl['text'].split('\n'),
                   'ai_error': err, 'rejected': rejected, 'stats': stats}


def ask(nights, question, url=DEFAULT_URL, model=DEFAULT_MODEL, timeout=120, cycle_log=None):
    question = (question or '').strip()[:500]
    ctx = build_context(nights, cycle_log)
    out = {'question': question, 'model': model, 'local_only': True,
           'generated': C.iso(dt.datetime.now(C.TZ).timestamp())}
    if not question:
        return out | {'ok': False, 'answer': 'Ask a question about your sleep data.', 'error': 'empty question'}
    if ctx is None:
        return out | {'ok': False, 'answer': 'No nights recorded yet, so there is nothing to answer from.',
                      'error': 'no nights'}
    msgs = [{'role': 'system', 'content': SYSTEM_ASK},
            {'role': 'user', 'content': 'Data (JSON):\n' + _ctx_json(ctx) + '\n\nQuestion: ' + question}]
    try:
        text, stats = chat(msgs, url=url, model=model, timeout=timeout, num_predict=320, temperature=0.3)
    except AIError as e:
        return out | {'ok': False, 'answer': 'The local AI is not reachable right now (is ollama running?).',
                      'error': str(e)}
    lines, err = check_answer(text)
    if not lines:
        return out | {'ok': False, 'stats': stats, 'error': err,
                      'answer': "I can describe your ring numbers and trends, but I can't answer that one. "
                                'For health questions, please ask a qualified professional.'}
    return out | {'ok': True, 'answer': '\n'.join(lines), 'stats': stats}


# ------------------------------------------------------------------ CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description='local-LLM sleep note / Q&A (Ollama on loopback only)')
    ap.add_argument('cmd', nargs='?', default='note', choices=('note', 'ask', 'context'))
    ap.add_argument('question', nargs='?', default='')
    ap.add_argument('--nightly', default=C.NIGHTLY_JSON)
    ap.add_argument('--json-out', default=AI_NOTE_JSON, help="note: where to write JSON ('-' = don't)")
    ap.add_argument('--url', default=DEFAULT_URL)
    ap.add_argument('--model', default=DEFAULT_MODEL)
    ap.add_argument('--timeout', type=float, default=None)
    ap.add_argument('--cycle-log', default=None)
    ap.add_argument('--json', action='store_true', help='print JSON instead of text')
    a = ap.parse_args(argv)
    try:
        assert_loopback(a.url)
    except AIError as e:
        print(f'ai_note: {e}', file=sys.stderr)
        return 2
    nights = (C.read_json(a.nightly, {}) or {}).get('nights', [])
    if a.cmd == 'context':
        print(json.dumps(build_context(nights, a.cycle_log), indent=1, ensure_ascii=False))
        return 0
    if a.cmd == 'ask':
        res = ask(nights, a.question, url=a.url, model=a.model, timeout=a.timeout or 120, cycle_log=a.cycle_log)
        print(json.dumps(res, indent=1, ensure_ascii=False) if a.json else res['answer'])
        return 0 if res['ok'] else 1
    note = make_note(nights, url=a.url, model=a.model, timeout=a.timeout or 90, cycle_log=a.cycle_log)
    if a.json_out and a.json_out != '-':
        C.write_json_atomic(a.json_out, note)
    if note['source'] != 'ai':
        print(f"ai_note: fell back to template ({note.get('ai_error')})", file=sys.stderr)
    print(json.dumps(note, indent=1, ensure_ascii=False) if a.json else note['text'])
    return 0


if __name__ == '__main__':
    sys.exit(main())
