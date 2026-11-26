#!/usr/bin/env python3
"""Per-calendar-day daytime/evening metrics + steps (stdlib only, read-only DB).

Usage (from repo root):
    python3 -I tools/analysis/day.py [--db PATH] [--nightly data/nightly.json] [--date 2026-11-25]
                                     [--out data/days.json] [--json]

Reads the DB picked by data/source.json (pc -> data/oura.db, phone -> data/phone.db) read-only,
plus data/nightly.json (sleep windows from nights.py), and writes data/days.json (gitignored --
personal health data, never commit it).

Days are local calendar days (system zone / RINGKEEPER_TZ, DST-safe). "Day" metrics use only time OUTSIDE
sleep windows (nightly.json nights + naps, plus any bedtime_period in the DB).

Signals and how they are used (details + caveats in tools/analysis/README.md "Day / evening"):

- activity_information 0x50: per-minute MET bins. n bins per event, END-stamped: bin i covers
  [T - (n-i)*60, T - (n-i-1)*60) (13 bins arrive ~13 min apart; native parser: batched samples end at
  the event time). -> intensity minutes, active minutes (MET >= 3), MET-minutes, active kcal.
- Steps: the ring's REAL_STEPS events (real_step_event_feature_1/2, 0x7e/0x7f) are 30-s gait-feature
  windows, NOT counts; turning them into steps needs Oura's proprietary `steps_motion_decoder`
  TorchScript model (upstream tools/run_activity_model.py), which we don't ship. So the headline step
  count is always **estimated** from MET minutes with upstream's oura-summary rule (105 steps/min at
  2.5-7 MET, 150 at >= 7 MET). Reported alongside, never as the headline:
    * gait windows / walking minutes from 0x7e(+0x7f) when present (REAL_STEPS on),
    * `ring_counter`: sum of the activity_information `state` byte -- it is 0 in sleep and rises with
      MET (255 = saturated in a 2-min 8-11 MET burst), so it may be a per-event step counter, but that
      is an unverified hypothesis.
- Heart rate: ibi_and_amplitude_event 0x60 (amplitude > 0) and green_ibi_quality_event 0x80
  (quality <= 1), 300-2000 ms, +-25 % local-median filter. 5-min medians (>= 10 beats) outside sleep
  -> daytime HR min/avg/max; hrv_event 0x5d samples outside sleep -> daytime RMSSD.
- Exercise HR (EXERCISE_HR): ehr_trace_event 0x73 has NO decoder upstream (layout unknown), so
  sessions come from event timing (clusters, gap > 5 min splits) and feature_session feature_id 3
  start/stop; HR inside a session from the beat streams above; ehr_acm_intensity_event 0x74 (decoded
  u16 `intensity`) gives mean/max intensity. MET bouts (>= 3 MET for >= 5 min, gaps <= 2 min) are
  listed as estimated activity sessions.
- Wear time: minutes with skin temp (temp_event probe 0) >= 30 C or >= 3 valid beats.
- Evening (wind-down): for each night, the 3 h before bed: HR 15-min medians, first vs last hour,
  slope (bpm/h); RMSSD (hrv_event, else 5-min beat RMSSD) first vs last hour; mean MET; last minute
  with MET >= 2.5 ("last activity") and with MET >= 3. The evening belongs to the calendar day of
  (bed - 6 h), so a 00:30 bedtime is that previous day's evening.
- Active kcal = sum max(0, MET - 1) * weight / 60 per minute (upstream oura-summary). Weight from
  data/profile.json {"weight_kg"} if present, else user_information 0x5c unless it is the firmware
  default profile (40 y / 75 kg / sex 2 / 176 cm), else null (no calories).

Everything here is an estimate, not medical advice.
"""
import argparse
import bisect
import datetime as dt
import json
import math
import os
import statistics as st
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

DAYS_JSON = os.path.join(C.DATA_DIR, 'days.json')
PROFILE_JSON = os.path.join(C.DATA_DIR, 'profile.json')
SCHEMA = 1

DAY_NAMES = (
    'time_sync', 'bedtime_period', 'activity_information', 'ibi_and_amplitude_event',
    'green_ibi_quality_event', 'hrv_event', 'temp_event', 'motion_event', 'user_information',
    'feature_session', 'real_step_event_feature_1', 'real_step_event_feature_2',
    'ehr_trace_event', 'ehr_acm_intensity_event',
)
FIRMWARE_DEFAULT_PROFILE = (40, 75, 2, 176)
STEP_RULE = ((7.0, 150), (2.5, 105))        # (MET threshold, steps/min), upstream oura-summary
GAIT_WINDOW_S = 30                           # one 0x7e/0x7f pair = 3 x 10-s decoder sub-windows
GAIT_CADENCE = 1.8                           # steps/s, typical walking (only for the gait estimate)
ACTIVE_MET = 3.0
INTENSITY = (('sedentary', 0, 1.5), ('low', 1.5, 3.0), ('medium', 3.0, 6.0), ('high', 6.0, 99))
WIND_DOWN_S = 3 * 3600
EVENING_SHIFT_S = 6 * 3600
EHR_GAP_S = 300
EXERCISE_FEATURE_ID = 3
LABEL = 'estimated (our rules on ring data, not Oura)'


# ---------------------------------------------------------------- small helpers
def steps_for_met(m):
    for thr, spm in STEP_RULE:
        if m >= thr:
            return spm
    return 0


def day_bounds(date):
    """Local midnight..next midnight as unix seconds (DST-safe)."""
    a = dt.datetime.combine(date, dt.time(0), C.TZ)
    b = dt.datetime.combine(date + dt.timedelta(days=1), dt.time(0), C.TZ)
    return a.timestamp(), b.timestamp()


def hour_of(u):
    return dt.datetime.fromtimestamp(u, C.TZ).hour


def r1(x):
    return None if x is None else round(x, 1)


def slope_per_h(pts):
    """Least-squares slope of [(unix, value)] in value/hour; None with < 3 points."""
    pts = [(t, v) for t, v in pts if v is not None]
    if len(pts) < 3:
        return None
    mt = sum(t for t, _ in pts) / len(pts)
    mv = sum(v for _, v in pts) / len(pts)
    den = sum((t - mt) ** 2 for t, _ in pts)
    if den == 0:
        return None
    return sum((t - mt) * (v - mv) for t, v in pts) / den * 3600


class Windows:
    """Sorted, merged [lo, hi) intervals (sleep windows)."""

    def __init__(self, spans):
        merged = []
        for lo, hi in sorted((float(a), float(b)) for a, b in spans if a is not None and b is not None and b > a):
            if merged and lo <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], hi)
            else:
                merged.append([lo, hi])
        self.spans = merged
        self.los = [s[0] for s in merged]

    def contains(self, u):
        i = bisect.bisect_right(self.los, u) - 1
        return i >= 0 and u < self.spans[i][1]

    def overlap(self, lo, hi):
        return sum(max(0.0, min(hi, b) - max(lo, a)) for a, b in self.spans)


# ---------------------------------------------------------------- signal extraction
def met_minutes(ev, clock):
    """{minute_start_unix (int, floor 60): MET} from activity_information (later event wins).

    Also returns [(event_unix, n_bins, state)] for the ring-counter candidate."""
    mins, evs = {}, []
    for ts, d in ev.get('activity_information', []):
        mets = [m for m in d.get('met', []) if isinstance(m, (int, float))]
        if not mets:
            continue
        T = clock.unix(ts)
        n = len(mets)
        for i, m in enumerate(mets):
            start = T - (n - i) * 60
            mins[int(start // 60 * 60)] = float(m)
        evs.append((T, n, d.get('state')))
    return mins, evs


def _stream_beats(items, T_of, ibis_of, ok_of):
    raw, last_t = [], -math.inf
    for it in items:
        T = T_of(it)
        ibis = ibis_of(it)
        tail, stamps = 0.0, []
        for i in range(len(ibis) - 1, -1, -1):
            tail += ibis[i] / 1000.0
            stamps.append(T - tail)
        stamps.reverse()
        for k, (t, ibi) in enumerate(zip(stamps, ibis)):
            t = max(t, last_t)
            last_t = t
            raw.append([t, ibi, bool(ok_of(it, k) and 300 <= ibi <= 2000)])
    vidx = [i for i, b in enumerate(raw) if b[2]]
    vals = [raw[i][1] for i in vidx]
    for j, i in enumerate(vidx):
        m = st.median(vals[max(0, j - 5):j + 6])
        if abs(raw[i][1] - m) > 0.25 * m:
            raw[i][2] = False
    return [(t, ibi) for t, ibi, ok in raw if ok]


def beats(ev, clock):
    """Valid beats per stream: {'ibi': [(t, ibi_ms)], 'green': [...]} (each time-ordered)."""
    def amp_ok(x, k):
        a = x[1].get('amplitude') or []
        return k < len(a) and a[k] > 0

    def q_ok(x, k):
        q = x[1].get('quality') or []
        return k < len(q) and q[k] <= 1

    T = lambda x: clock.unix(x[0])  # noqa: E731
    ibis = lambda x: x[1].get('ibi_ms', [])  # noqa: E731
    return {'ibi': _stream_beats(ev.get('ibi_and_amplitude_event', []), T, ibis, amp_ok),
            'green': _stream_beats(ev.get('green_ibi_quality_event', []), T, ibis, q_ok)}


def hr_bins(streams, bin_s=300, min_beats=10):
    """{bin_start: median HR} over both streams combined."""
    acc = {}
    for bs in streams.values():
        for t, ibi in bs:
            acc.setdefault(int(t // bin_s * bin_s), []).append(ibi)
    return {k: 60000.0 / st.median(v) for k, v in acc.items() if len(v) >= min_beats}


def rmssd_bins(streams, bin_s=300, min_pairs=20):
    """5-min RMSSD from successive valid beats (per stream, gaps > 2.5 s break pairs)."""
    acc = {}
    for bs in streams.values():
        for (t0, a), (t1, b) in zip(bs, bs[1:]):
            if t1 - t0 > 2.5:
                continue
            acc.setdefault(int(t1 // bin_s * bin_s), []).append((b - a) ** 2)
    return {k: math.sqrt(sum(v) / len(v)) for k, v in acc.items() if len(v) >= min_pairs}


def hrv_samples(ev, clock):
    """[(centre_unix, hr, rmssd)] from hrv_event (5-min samples ending at the event time)."""
    out = []
    for ts, d in ev.get('hrv_event', []):
        step = d.get('interval_min', 5) * 60
        hs, rs = d.get('hr_bpm', []), d.get('rmssd_ms', [])
        m = len(rs)
        T = clock.unix(ts)
        for i, (h, v) in enumerate(zip(hs, rs)):
            out.append((T - (m - 1 - i) * step - step / 2, h if h > 0 else None, v if v > 0 else None))
    return out


def worn_minutes(ev, clock, streams):
    worn = set()
    for ts, d in ev.get('temp_event', []):
        vs = d.get('temps_c') or []
        if vs and vs[0] >= 30.0:
            worn.add(int(clock.unix(ts) // 60 * 60))
    cnt = {}
    for bs in streams.values():
        for t, _ in bs:
            k = int(t // 60 * 60)
            cnt[k] = cnt.get(k, 0) + 1
    worn.update(k for k, c in cnt.items() if c >= 3)
    return worn


def sleep_windows(ev, clock, nights):
    spans = [(n.get('bed_unix'), n.get('wake_unix')) for n in nights or []]
    for ts, d in ev.get('bedtime_period', []):
        s, e = d.get('bedtime_start_ds'), d.get('bedtime_end_ds')
        if s is not None and e is not None and e > s:
            spans.append((clock.unix(s), clock.unix(e)))
    return Windows(spans)


def gait_times(ev, clock):
    """Unix times of REAL_STEPS gait windows (0x7e; 0x7f-only windows counted if no 0x7e at all)."""
    f1 = [clock.unix(ts) for ts, _ in ev.get('real_step_event_feature_1', [])]
    if f1:
        return f1
    return [clock.unix(ts) for ts, _ in ev.get('real_step_event_feature_2', [])]


def exercise_sessions(ev, clock, streams):
    """EXERCISE_HR sessions from ehr_trace timing + feature_session(3). Payload of 0x73 not decoded."""
    spans = []
    tr = sorted(clock.unix(ts) for ts, _ in ev.get('ehr_trace_event', []))
    if tr:
        lo = prev = tr[0]
        n = 1
        for t in tr[1:]:
            if t - prev > EHR_GAP_S:
                spans.append([lo, prev, n, 'ehr_trace'])
                lo, n = t, 0
            prev = t
            n += 1
        spans.append([lo, prev, n, 'ehr_trace'])
    start = None
    for ts, d in ev.get('feature_session', []):
        if d.get('feature_id') != EXERCISE_FEATURE_ID:
            continue
        u = clock.unix(ts)
        if d.get('session_status') == 1:
            start = u
        elif start is not None:
            spans.append([start, u, 0, 'feature_session'])
            start = None
    # merge overlapping (feature_session bounds + trace clusters describe the same session)
    spans.sort()
    merged = []
    for s in spans:
        if merged and s[0] <= merged[-1][1] + EHR_GAP_S:
            m = merged[-1]
            m[1] = max(m[1], s[1])
            m[2] += s[2]
            if s[3] not in m[3]:
                m[3] = m[3] + '+' + s[3]
        else:
            merged.append(list(s))
    inten = [(clock.unix(ts), d.get('intensity') or []) for ts, d in ev.get('ehr_acm_intensity_event', [])]
    out = []
    for lo, hi, n, src in merged:
        hi = max(hi, lo + 60)
        ibis = [ibi for bs in streams.values() for t, ibi in bs if lo <= t <= hi]
        hrs = [60000.0 / x for x in ibis]
        iv = [x for t, xs in inten if lo - 60 <= t <= hi + 60 for x in xs]
        out.append({'start': C.iso(lo), 'end': C.iso(hi), 'start_unix': round(lo), 'end_unix': round(hi),
                    'duration_min': round((hi - lo) / 60, 1), 'source': src, 'trace_events': n,
                    'hr_avg': r1(st.mean(hrs)) if len(hrs) >= 10 else None,
                    'hr_max': r1(C.quantile(hrs, 0.98)) if len(hrs) >= 10 else None,
                    'intensity_mean': r1(st.mean(iv)) if iv else None, 'intensity_max': max(iv) if iv else None,
                    'label': 'EXERCISE_HR session (0x73 payload not decoded; times + HR from beats)'})
    return out


def met_bouts(mins, sleep, lo, hi, thr=ACTIVE_MET, min_len=5, max_gap=2):
    """Runs of MET >= thr (gaps <= max_gap min) lasting >= min_len minutes, outside sleep."""
    ks = sorted(k for k, m in mins.items() if lo <= k < hi and m >= thr and not sleep.contains(k + 30))
    bouts, cur = [], []
    for k in ks:
        if cur and k - cur[-1] > (max_gap + 1) * 60:
            bouts.append(cur)
            cur = []
        cur.append(k)
    if cur:
        bouts.append(cur)
    out = []
    for b in bouts:
        dur = (b[-1] + 60 - b[0]) / 60
        if dur < min_len:
            continue
        mets = [mins[k] for k in b]
        out.append({'start': C.iso(b[0]), 'end': C.iso(b[-1] + 60), 'start_unix': b[0], 'end_unix': b[-1] + 60,
                    'duration_min': round(dur), 'met_avg': r1(st.mean(mets)), 'met_max': r1(max(mets)),
                    'steps_est': int(round(sum(steps_for_met(m) for m in mets), -1)),
                    'source': 'met', 'label': 'activity bout from MET (estimated)'})
    return out


# ---------------------------------------------------------------- profile
def resolve_weight(ev, profile_path=PROFILE_JSON):
    prof = C.read_json(profile_path, None) if profile_path else None
    if isinstance(prof, dict) and isinstance(prof.get('weight_kg'), (int, float)) and prof['weight_kg'] > 0:
        return float(prof['weight_kg']), 'data/profile.json'
    ui = ev.get('user_information') or []
    if ui:
        d = ui[-1][1]
        key = (d.get('age_years'), d.get('weight_kg'), d.get('sex_code'), d.get('height_cm'))
        if key == FIRMWARE_DEFAULT_PROFILE:
            return None, 'ring profile is the firmware default (40 y / 75 kg) -- set data/profile.json {"weight_kg": ...}'
        if d.get('weight_kg'):
            return float(d['weight_kg']), 'ring user_information (inferred field)'
    return None, 'no weight: set data/profile.json {"weight_kg": ...}'


# ---------------------------------------------------------------- per-day + evening
class Ctx:
    def __init__(self, ev, clock, nights, profile_path=PROFILE_JSON):
        self.ev, self.clock = ev, clock
        self.nights = sorted([n for n in nights or [] if n.get('bed_unix')], key=lambda n: n['bed_unix'])
        self.sleep = sleep_windows(ev, clock, self.nights)
        self.mins, self.act_events = met_minutes(ev, clock)
        self.streams = beats(ev, clock)
        self.hr5 = hr_bins(self.streams)
        self.hr15 = hr_bins(self.streams, 900, 20)
        self.rm5 = rmssd_bins(self.streams)
        self.hrv = hrv_samples(ev, clock)
        self.worn = worn_minutes(ev, clock, self.streams)
        self.gait = gait_times(ev, clock)
        self.exercise = exercise_sessions(ev, clock, self.streams)
        self.weight, self.weight_source = resolve_weight(ev, profile_path)

    def awake(self, u):
        return not self.sleep.contains(u)

    def activity_window(self, lo, hi):
        """Aggregate MET minutes in [lo, hi) outside sleep (used for readiness' previous-day input)."""
        mets = [m for k, m in self.mins.items() if lo <= k < hi and self.awake(k + 30)]
        if not mets:
            return {'bins': 0, 'recorded_min': 0}
        return {'bins': len(mets), 'recorded_min': len(mets), 'mean_met': round(st.mean(mets), 2),
                'high_pct': round(100 * sum(m >= 6 for m in mets) / len(mets), 1),
                'active_min': sum(m >= ACTIVE_MET for m in mets),
                'high_min': sum(m >= 6 for m in mets),
                'steps_est': int(round(sum(steps_for_met(m) for m in mets), -1)),
                'steps_label': 'estimated from MET'}

    def day(self, date):
        lo, hi = day_bounds(date)
        mins = {k: m for k, m in self.mins.items() if lo <= k < hi and self.awake(k + 30)}
        sleep_min = round(self.sleep.overlap(lo, hi) / 60)
        hourly_steps, hourly_met, hourly_rec = [0] * 24, [[] for _ in range(24)], [0] * 24
        for k, m in mins.items():
            h = hour_of(k)
            hourly_steps[h] += steps_for_met(m)
            hourly_met[h].append(m)
            hourly_rec[h] += 1
        inten = {name: sum(a <= m < b for m in mins.values()) for name, a, b in INTENSITY}
        active = inten['medium'] + inten['high']
        met_min = sum(max(0.0, m - 1) for m in mins.values())
        kcal = round(met_min * self.weight / 60) if self.weight else None
        steps_met = int(round(sum(hourly_steps), -1))
        # ring counter candidate: activity_information state bytes for events mostly awake & in this day
        counter = [s for T, n, s in self.act_events
                   if isinstance(s, int) and lo <= T - n * 30 < hi and self.awake(T - n * 30)]
        gait = [t for t in self.gait if lo <= t < hi and self.awake(t)]
        # HR outside sleep
        hr = {k: v for k, v in self.hr5.items() if lo <= k < hi and self.awake(k + 150)}
        hr_hour = [[] for _ in range(24)]
        for k, v in hr.items():
            hr_hour[hour_of(k)].append(v)
        hrv_day = [v for t, _, v in self.hrv if lo <= t < hi and self.awake(t) and v]
        worn_all = sum(lo <= k < hi for k in self.worn)
        worn_day = sum(lo <= k < hi and self.awake(k + 30) for k in self.worn)
        last_act = max((k for k, m in mins.items() if m >= 2.5), default=None)
        sessions = [s for s in self.exercise if lo <= s['start_unix'] < hi]
        bouts = met_bouts(self.mins, self.sleep, lo, hi)
        hv = list(hr.values())
        return {
            'date': date.isoformat(),
            'recorded_min': len(mins), 'sleep_min_excluded': sleep_min,
            'wear_min': worn_all, 'wear_awake_min': worn_day,
            'steps': {
                'value': steps_met if mins else None, 'estimated': True, 'source': 'met_rule',
                'label': 'estimated from MET (105/min at 2.5-7 MET, 150/min at >= 7)',
                'gait_windows': len(gait), 'gait_walking_min': round(len(gait) * GAIT_WINDOW_S / 60, 1),
                'gait_steps_est': int(round(len(gait) * GAIT_WINDOW_S * GAIT_CADENCE, -1)) if gait else None,
                'ring_counter': sum(counter) if counter else None,
                'ring_counter_note': 'activity_information state byte sum; possibly steps, unverified',
                'real_steps_present': bool(self.gait),
            },
            'distance_km_est': round(steps_met * 0.762 / 1000, 2) if mins else None,
            'intensity_min': inten, 'active_min': active,
            'met_min': round(met_min), 'mean_met': r1(st.mean(mins.values())) if mins else None,
            'active_kcal': kcal, 'weight_kg': self.weight, 'weight_source': self.weight_source,
            'hourly': {
                'steps': hourly_steps,
                'met': [r1(st.mean(v)) if v else None for v in hourly_met],
                'recorded_min': hourly_rec,
                'hr': [r1(st.median(v)) if v else None for v in hr_hour],
            },
            'hr': {'min': r1(min(hv)) if hv else None, 'avg': r1(st.mean(hv)) if hv else None,
                   'max': r1(max(hv)) if hv else None, 'samples_5min': len(hv),
                   'label': 'daytime HR: 5-min medians of beats outside sleep'},
            'hrv_rmssd_median': r1(st.median(hrv_day)) if hrv_day else None, 'hrv_samples': len(hrv_day),
            'last_activity': C.iso(last_act + 60) if last_act is not None else None,
            'exercise_sessions': sessions,
            'activity_bouts': bouts,
            'label': LABEL,
        }

    def evening(self, night):
        bed = float(night['bed_unix'])
        lo = bed - WIND_DOWN_S
        hr15 = sorted((k, v) for k, v in self.hr15.items() if lo <= k and k + 900 <= bed + 1)
        first = [v for k, v in hr15 if k < lo + 3600]
        last = [v for k, v in hr15 if k >= bed - 3600]
        hrv = [(t, v) for t, _, v in self.hrv if lo <= t < bed and v]
        hrv_src = 'hrv_event'
        if len(hrv) < 3:
            hrv = [(k + 150, v) for k, v in self.rm5.items() if lo <= k and k + 300 <= bed + 1]
            hrv_src = 'beats (5-min RMSSD)'
        hf = [v for t, v in hrv if t < lo + 3600]
        hl = [v for t, v in hrv if t >= bed - 3600]
        mets = [(k, m) for k, m in self.mins.items() if lo <= k < bed]
        look = [(k, m) for k, m in self.mins.items() if bed - 6 * 3600 <= k < bed]
        last25 = max((k for k, m in look if m >= 2.5), default=None)
        last3 = max((k for k, m in look if m >= ACTIVE_MET), default=None)
        hr_cov = sum(1 for k in self.hr5 if lo <= k < bed) * 5
        ev_date = C.local_date(bed - EVENING_SHIFT_S)
        out = {
            'date': ev_date, 'night_date': night.get('date'), 'bed': C.iso(bed), 'bed_unix': round(bed),
            'window': [C.iso(lo), C.iso(bed)], 'window_h': WIND_DOWN_S / 3600,
            'hr_coverage_min': hr_cov,
            'hr_15min': [[C.iso(k), r1(v)] for k, v in hr15],
            'hr_first_hour': r1(st.mean(first)) if first else None,
            'hr_last_hour': r1(st.mean(last)) if last else None,
            'hr_slope_bpm_per_h': r1(slope_per_h([(k + 450, v) for k, v in hr15])),
            'rmssd_first_hour': r1(st.median(hf)) if hf else None,
            'rmssd_last_hour': r1(st.median(hl)) if hl else None,
            'rmssd_slope_ms_per_h': r1(slope_per_h(hrv)), 'rmssd_source': hrv_src if hrv else None,
            'mean_met': r1(st.mean(m for _, m in mets)) if mets else None, 'met_recorded_min': len(mets),
            'last_activity': C.iso(last25 + 60) if last25 is not None else None,
            'last_activity_min_before_bed': round((bed - last25 - 60) / 60) if last25 is not None else None,
            'last_moderate': C.iso(last3 + 60) if last3 is not None else None,
            'last_moderate_min_before_bed': round((bed - last3 - 60) / 60) if last3 is not None else None,
            'label': LABEL,
        }
        if out['hr_first_hour'] is not None and out['hr_last_hour'] is not None:
            d = out['hr_last_hour'] - out['hr_first_hour']
            out['hr_trend'] = 'settling' if d <= -3 else 'rising' if d >= 3 else 'steady'
        else:
            s = out['hr_slope_bpm_per_h']
            out['hr_trend'] = None if s is None else 'settling' if s <= -3 else 'rising' if s >= 3 else 'steady'
        out['partial'] = hr_cov < 120 or len(mets) < 120
        return out


def sources_doc(ev):
    has = lambda n: bool(ev.get(n))  # noqa: E731
    return {
        'activity_information_0x50': {'present': has('activity_information'), 'decoder': 'upstream (MET per minute)'},
        'real_steps_0x7e_0x7f': {'present': has('real_step_event_feature_1') or has('real_step_event_feature_2'),
                                 'decoder': 'part1 raw fields only; step counts need Oura steps_motion_decoder (not shipped)'},
        'ehr_trace_0x73': {'present': has('ehr_trace_event'), 'decoder': 'none upstream (payload layout unknown)'},
        'ehr_acm_intensity_0x74': {'present': has('ehr_acm_intensity_event'), 'decoder': 'upstream (u16 intensity)'},
        'ibi_and_amplitude_0x60': {'present': has('ibi_and_amplitude_event'), 'decoder': 'upstream'},
        'green_ibi_quality_0x80': {'present': has('green_ibi_quality_event'), 'decoder': 'upstream'},
        'user_information_0x5c': {'present': has('user_information'), 'decoder': 'upstream (inferred fields)'},
    }


def load(db_path):
    con = C.open_ro(db_path)
    try:
        ev = C.load_events(con, DAY_NAMES)
    finally:
        con.close()
    return ev, C.RingClock.from_events(ev)


def analyse_ev(ev, clock, nights, profile_path=PROFILE_JSON, dates=None):
    cx = Ctx(ev, clock, nights, profile_path)
    if dates is None:
        stamps = list(cx.mins) + [t for bs in cx.streams.values() for t, _ in bs[::50]]
        dates = sorted({dt.date.fromisoformat(C.local_date(u)) for u in stamps})
    days = [cx.day(d) for d in dates]
    evenings = [cx.evening(n) for n in cx.nights]
    return {'schema': SCHEMA, 'tz': C.TZ_NAME, 'label': LABEL, 'sources': sources_doc(ev),
            'weight_kg': cx.weight, 'weight_source': cx.weight_source,
            'days': days, 'evenings': evenings}, cx


def build(db_path=None, nightly_path=C.NIGHTLY_JSON, profile_path=PROFILE_JSON, nights=None):
    db_path = db_path or C.source_db()
    if nights is None:
        nights = (C.read_json(nightly_path, {}) or {}).get('nights', [])
    ev, clock = load(db_path)
    doc, _ = analyse_ev(ev, clock, nights, profile_path)
    doc['generated'] = C.iso(time.time())
    doc['data_source'] = C.source_config()['mode']
    doc['note'] = 'personal health data - do not commit. ' + LABEL
    return doc


def pick_day(doc, date=None):
    days = doc.get('days') or []
    if date:
        return next((d for d in days if d['date'] == date), None)
    return days[-1] if days else None


def describe(day, evening=None):
    if not day:
        return 'No daytime data.'
    s = day['steps']
    lines = [f"{day['date']}: {day['recorded_min']} min of awake activity data, {day['sleep_min_excluded']} min of sleep excluded, "
             f"worn {day['wear_min']} min"]
    if s['value'] is not None:
        extra = f"; ring counter {s['ring_counter']}" if s['ring_counter'] is not None else ''
        extra += f"; {s['gait_windows']} REAL_STEPS gait windows" if s['real_steps_present'] else ''
        lines.append(f"Steps ~{s['value']} (estimated from MET{extra}); active {day['active_min']} min "
                     f"(MET >= 3), {day['met_min']} MET-min, kcal {day['active_kcal'] if day['active_kcal'] is not None else 'n/a (no weight)'}")
    h = day['hr']
    if h['avg'] is not None:
        lines.append(f"Daytime HR {h['min']:.0f}/{h['avg']:.0f}/{h['max']:.0f} bpm (min/avg/max of {h['samples_5min']} 5-min medians)")
    for x in day['exercise_sessions']:
        lines.append(f"Exercise session {x['start'][11:16]}-{x['end'][11:16]} ({x['duration_min']} min), HR avg {x['hr_avg']}")
    for b in day['activity_bouts']:
        lines.append(f"Activity bout {b['start'][11:16]}-{b['end'][11:16]} ({b['duration_min']} min, avg {b['met_avg']} MET, ~{b['steps_est']} steps)")
    if day['last_activity']:
        lines.append(f"Last activity (MET >= 2.5): {day['last_activity'][11:16]}")
    if evening:
        e = evening
        lines.append(f"Evening {e['date']} (3 h before {e['bed'][11:16]} bed{', partial data' if e['partial'] else ''}): "
                     f"HR {e['hr_first_hour']} -> {e['hr_last_hour']} bpm ({e['hr_trend']}), "
                     f"RMSSD {e['rmssd_first_hour']} -> {e['rmssd_last_hour']} ms, last activity "
                     f"{(e['last_activity'] or '-')[11:16]} ({e['last_activity_min_before_bed']} min before bed)")
    return '\n'.join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description='per-day daytime/evening metrics + steps -> data/days.json')
    ap.add_argument('--db', default=None, help='default: per data/source.json')
    ap.add_argument('--nightly', default=C.NIGHTLY_JSON)
    ap.add_argument('--profile', default=PROFILE_JSON)
    ap.add_argument('--out', default=DAYS_JSON, help="'-' = don't write")
    ap.add_argument('--date', default=None)
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args(argv)
    doc = build(a.db, a.nightly, a.profile)
    if a.out and a.out != '-':
        C.write_json_atomic(a.out, doc)
    if a.json:
        print(json.dumps(doc, indent=1))
    else:
        for d in doc['days'] if not a.date else [pick_day(doc, a.date)]:
            ev = next((e for e in doc['evenings'] if d and e['date'] == d['date']), None)
            print(describe(d, ev))
            print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
