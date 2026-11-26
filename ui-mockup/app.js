/* ringkeeper UI mockup — vanilla JS, no deps. All data below is FAKE (seeded). */
'use strict';
(function () {
  // ---------- helpers ----------
  const $ = (s, r = document) => r.querySelector(s);
  function mulberry32(a) {
    return function () {
      a |= 0; a = (a + 0x6D2B79F5) | 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }
  const rand = mulberry32(20261124);
  const gauss = () => { let u = 0; while (!u) u = rand(); const v = rand(); return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v); };
  const clamp = (x, a, b) => Math.max(a, Math.min(b, x));
  const mean = a => a.reduce((s, x) => s + x, 0) / a.length;
  const sd = a => { const m = mean(a); return Math.sqrt(mean(a.map(x => (x - m) ** 2))); };
  const r0 = x => Math.round(x);
  const r1 = x => Math.round(x * 10) / 10;
  const fmtInt = x => Math.round(x).toLocaleString('en-US');
  const fmtDur = m => `${Math.floor(m / 60)}h ${String(Math.round(m % 60)).padStart(2, '0')}m`;
  function fmtClock(min) { // minutes from midnight (may exceed 1440)
    min = ((Math.round(min) % 1440) + 1440) % 1440;
    const h = Math.floor(min / 60), m = min % 60;
    return `${((h + 11) % 12) + 1}:${String(m).padStart(2, '0')} ${h < 12 ? 'AM' : 'PM'}`;
  }
  const shortClock = min => { const h = Math.floor((((min % 1440) + 1440) % 1440) / 60); return h === 0 ? '12a' : h === 12 ? '12p' : h < 12 ? h + 'a' : (h - 12) + 'p'; };
  const fmtDate = (d, o) => d.toLocaleDateString('en-US', o || { weekday: 'short', month: 'short', day: 'numeric' });
  const cToF = c => c * 1.8;
  const signed = (x, d = 0) => (x > 0 ? '+' : x < 0 ? '−' : '±') + Math.abs(x).toFixed(d);
  const pct = (x, b) => (x - b) / b * 100;

  // ---------- fake data (deterministic) ----------
  const DAYS = 30;
  const NOW = new Date();
  const today = new Date(NOW); today.setHours(0, 0, 0, 0);
  const dayDate = i => { const d = new Date(today); d.setDate(d.getDate() - (DAYS - 1 - i)); return d; };
  const EVENT = DAYS - 1 - 8; // a late night out 8 nights ago
  const STEP_GOAL = 8000;

  // ---------- cycle data (fake; own RNG so the other screens' numbers are unchanged) ----------
  const addDays = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };
  const dkey = d => d.toDateString();
  const CYC = (function () {
    const cr = mulberry32(28031), cg = () => { let u = 0; while (!u) u = cr(); return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * cr()); };
    const CUR_DAY = 18;
    // trueOv is only used to shape the fake temps; the UI shows the ovulation *estimated* by the temp-shift rule
    const cycles = [{ len: 29, per: 5, trueOv: 15 }, { len: 27, per: 4, trueOv: 13 }, { len: 30, per: 5, trueOv: 16 }, { len: null, per: 5, trueOv: 14 }];
    const cur = cycles[cycles.length - 1];
    cur.start = addDays(today, -(CUR_DAY - 1));
    for (let c = cycles.length - 2; c >= 0; c--) cycles[c].start = addDays(cycles[c + 1].start, -cycles[c].len);
    const nights = [], byDate = {};
    cycles.forEach((c, ci) => {
      const L = c.len || CUR_DAY, o = c.trueOv;
      for (let cd = 1; cd <= L; cd++) {
        // biphasic: low follicular, small dip at ovulation, ~+0.3 °C rise over 2 nights, drop just before the next period
        let t = cd <= o ? -0.13 - (cd === o ? 0.04 : 0) : cd === o + 1 ? -0.02 : cd === o + 2 ? 0.09 : 0.16;
        if (c.len && cd === c.len - 1) t = 0.06;
        if (c.len && cd === c.len) t = -0.04;
        const n = { date: addDays(c.start, cd - 1), ci, cd, t: t + cg() * 0.04, period: cd <= c.per, excluded: null };
        nights.push(n); byDate[dkey(n.date)] = n;
      }
    });
    return { cycles, cur, nights, byDate, CUR_DAY };
  })();

  const days = [];
  for (let i = 0; i < DAYS; i++) {
    const date = dayDate(i), dow = date.getDay();
    const wkend = dow === 0 || dow === 6;           // sleep ending on a weekend morning
    const cyc = Math.sin(2 * Math.PI * (i + 9) / 28); // ~monthly temperature rhythm
    const ev = i === EVENT ? 1 : 0, after = i === EVENT + 1 ? 1 : 0;
    const hrv = clamp(43 + 4 * (i / DAYS) + gauss() * 4.2 - 3.5 * Math.max(0, cyc) - 14 * ev - 5 * after, 22, 75);
    const rhr = clamp(57.5 - (hrv - 44) * 0.15 + gauss() * 1.0 + 1.4 * Math.max(0, cyc) + 5 * ev + 2 * after, 49, 70);
    const tn = gauss() * 0.07, cn = CYC.byDate[dkey(date)];
    const temp = (cn ? cn.t : 0.2 * cyc + tn) + 0.33 * ev + 0.1 * after;
    if (cn) { cn.t = temp; if (ev) cn.excluded = 'late night, short sleep'; }
    const total = clamp(438 + (wkend ? 32 : 0) + gauss() * 24 - 88 * ev, 300, 540);
    const eff = clamp(89 + gauss() * 2.2 - 6 * ev, 76, 96);
    const latency = clamp(11 + gauss() * 4.5 - 6 * ev, 3, 35);
    const deepF = clamp(0.17 + gauss() * 0.022 - 0.045 * ev, 0.08, 0.25);
    const remF = clamp(0.22 + gauss() * 0.022 - 0.06 * ev, 0.1, 0.3);
    const inBed = total / (eff / 100);
    const bedStart = (wkend ? 23 * 60 + 45 : 23 * 60 + 4) + gauss() * 18 + 105 * ev; // minutes after prev midnight
    const spo2 = clamp(97.1 + gauss() * 0.5, 95, 99.4);
    const resp = clamp(14.6 + gauss() * 0.35 + 0.6 * ev, 12.5, 17);
    const steps = r0(clamp(7600 + gauss() * 1900 + (dow === 6 ? 4300 : 0) - 2600 * after, 2300, 18000));
    const activeCal = r0(clamp(steps * 0.041 + gauss() * 35, 90, 900));
    const sedentaryH = clamp(9.5 - steps / 3000 + gauss() * 0.7, 3, 12);
    days.push({ i, date, hrv, rhr, temp, total, eff, latency, deep: total * deepF, rem: total * remF,
      light: total * (1 - deepF - remF), inBed, awake: inBed - total, bedStart, spo2, resp, steps, activeCal, sedentaryH });
  }
  const last = days[DAYS - 1];

  // cycle: temp-shift rule (3 nights above the max of the previous 6), averages, predictions, calendar marks
  (function () {
    const valid = CYC.nights.filter(n => !n.excluded);
    CYC.cycles.forEach((c, ci) => {
      c.shift = null; c.estOv = null; c.fertile = null; c.luteal = null;
      for (let p = 6; p + 2 < valid.length; p++) {
        const n0 = valid[p];
        if (n0.ci !== ci || n0.cd < 8 || valid[p + 2].ci !== ci) continue;
        const prev = valid.slice(p - 6, p), thr = Math.max(...prev.map(n => n.t));
        if (valid.slice(p, p + 3).every(n => n.t > thr)) { c.shift = { thr, from: prev[0].date, first: n0, confirm: valid[p + 2] }; c.estOv = n0.cd - 1; break; }
      }
      if (c.estOv) { c.fertile = [c.estOv - 5, c.estOv + 1]; if (c.len) c.luteal = c.len - c.estOv; }
    });
    const done = CYC.cycles.filter(c => c.len);
    CYC.done = done;
    CYC.avgLen = mean(done.map(c => c.len));
    CYC.avgPer = mean(CYC.cycles.map(c => c.per));
    CYC.avgLuteal = mean(done.filter(c => c.luteal).map(c => c.luteal));
    CYC.predLen = Math.round(CYC.avgLen);
    CYC.nextStart = addDays(CYC.cur.start, CYC.predLen);
    CYC.daysToNext = CYC.predLen - CYC.CUR_DAY + 1;
    CYC.nextOvDay = CYC.predLen - Math.round(CYC.avgLuteal);
    CYC.nextFertile = [addDays(CYC.nextStart, CYC.nextOvDay - 6), addDays(CYC.nextStart, CYC.nextOvDay)];
    const fol = [], lut = [];
    CYC.nights.forEach(n => { const c = CYC.cycles[n.ci]; if (n.excluded || !c.estOv) return; if (n.cd <= c.estOv) fol.push(n.t); else if (n.cd > c.estOv + 2 && (!c.len || n.cd < c.len - 1)) lut.push(n.t); });
    CYC.folAvg = mean(fol); CYC.lutAvg = mean(lut);
    const cal = {}, mark = (d, k) => { const key = dkey(d); (cal[key] = cal[key] || {})[k] = true; };
    CYC.cycles.forEach(c => {
      for (let i = 0; i < c.per; i++) mark(addDays(c.start, i), 'period');
      if (c.estOv) { for (let cd = c.fertile[0]; cd <= c.fertile[1]; cd++) mark(addDays(c.start, cd - 1), 'fert'); mark(addDays(c.start, c.estOv - 1), 'ov'); }
    });
    for (let i = 0; i < Math.round(CYC.avgPer); i++) mark(addDays(CYC.nextStart, i), 'pred');
    for (let d = CYC.nextFertile[0]; d <= CYC.nextFertile[1]; d = addDays(d, 1)) mark(d, 'fertPred');
    CYC.cal = cal;
  })();

  // last night's hypnogram (5-min epochs): 0 awake, 1 REM, 2 light, 3 deep
  const hyp = (function () {
    const n = Math.round(last.inBed / 5), ep = [];
    const push = (s, min) => { const k = Math.round(min / 5); for (let j = 0; j < k; j++) ep.push(s); };
    push(0, Math.max(5, last.latency));
    for (let c = 0; ep.length < n; c++) {
      push(2, 12 + rand() * 10);
      push(3, Math.max(0, 40 - c * 10 + gauss() * 5));
      push(2, 10 + rand() * 15);
      if (rand() < 0.55) push(0, 5 + rand() * 12);
      push(1, Math.min(32, 6 + c * 6 + gauss() * 4));
    }
    ep.length = n; ep[n - 1] = 0;
    return ep;
  })();
  // make last night's totals match its hypnogram
  (function () {
    const cnt = [0, 0, 0, 0]; hyp.forEach(s => cnt[s]++);
    last.awake = cnt[0] * 5; last.rem = cnt[1] * 5; last.light = cnt[2] * 5; last.deep = cnt[3] * 5;
    last.total = last.rem + last.light + last.deep; last.inBed = hyp.length * 5; last.eff = last.total / last.inBed * 100;
  })();
  const wakeMin = last.bedStart + last.inBed;

  // overnight HR / HRV per epoch
  const overnight = (function () {
    const n = hyp.length, hr = [], hv = [];
    for (let k = 0; k < n; k++) {
      const x = k / (n - 1);
      const dip = Math.exp(-((x - 0.58) ** 2) / 0.09);        // nadir ~60% through the night
      const st = hyp[k];
      hr.push(last.rhr + 7 * (1 - dip) - 1.2 + (st === 1 ? 2.5 : st === 0 ? 5 : st === 3 ? -1.2 : 0) + gauss() * 0.8);
      hv.push(Math.max(12, last.hrv * (0.72 + 0.5 * dip) + (st === 3 ? 7 : st === 1 ? -5 : st === 0 ? -9 : 0) + gauss() * 3));
    }
    const smooth = a => a.map((v, k) => mean(a.slice(Math.max(0, k - 1), k + 2)));
    return { hr: smooth(hr), hrv: smooth(hv) };
  })();

  // today's intraday MET (15-min bins); generated for full day, displayed up to "now"
  const met = (function () {
    const out = [];
    for (let b = 0; b < 96; b++) {
      const h = b / 4; let m;
      if (h < 7) m = 0.92 + rand() * 0.06;
      else if (h < 8) m = 1.3 + rand() * 0.6;
      else if (h >= 8.25 && h < 8.75) m = 3.2 + rand() * 0.8;      // walk to train
      else if (h < 9) m = 1.3 + rand() * 0.4;
      else if (h >= 12.25 && h < 12.5) m = 3.5 + rand() * 0.7;      // lunch walk
      else if (h >= 17.5 && h < 18.25) m = 3.4 + rand() * 1.2;      // evening walk
      else if (h >= 23) m = 0.95;
      else m = 1.25 + rand() * 0.35 + (rand() < 0.08 ? 0.5 + rand() * 0.5 : 0);
      out.push(m);
    }
    return out;
  })();
  const nowBin = clamp(Math.floor((NOW.getHours() * 60 + NOW.getMinutes()) / 15), 0, 95);
  const binSteps = m => Math.max(0, m - 1.4) * 620;
  last.steps = r0(met.slice(0, nowBin + 1).reduce((s, m) => s + binSteps(m), 0));
  last.activeCal = r0(met.slice(0, nowBin + 1).reduce((s, m) => s + Math.max(0, m - 1) * 15 * 1.05 * 60 / 60, 0) * 0.9);
  last.sedentaryH = met.slice(28, nowBin + 1).filter(m => m < 1.6).length / 4;

  // baselines + our own transparent scores
  const DEF = { hrv: { m: 44, s: 5 }, rhr: { m: 57.5, s: 1.5 }, temp: { m: 0, s: 0.15 } };
  function baseline(key, i, win = 14) {
    const a = []; for (let k = Math.max(0, i - win); k < i; k++) a.push(days[k][key]);
    if (a.length < 4) return { m: DEF[key].m, s: DEF[key].s };
    return { m: mean(a), s: Math.max(sd(a), DEF[key].s * 0.5) };
  }
  function sleepParts(d) {
    return {
      duration: 40 * clamp((d.total - 300) / 210, 0, 1),           // 5h → 0, 8.5h → full
      efficiency: 25 * clamp((d.eff - 78) / 17, 0, 1),             // 78% → 0, 95% → full
      restorative: 20 * clamp(((d.deep + d.rem) / d.total - 0.25) / 0.25, 0, 1), // deep+REM 25–50%
      latency: 15 * clamp(1 - Math.abs(d.latency - 12) / 25, 0, 1), // ~12 min is ideal
    };
  }
  function recoveryParts(d) {
    const bh = baseline('hrv', d.i), br = baseline('rhr', d.i);
    return {
      hrv: clamp(10 * (d.hrv - bh.m) / bh.s, -25, 15),
      rhr: clamp(-7 * (d.rhr - br.m) / br.s, -20, 10),
      temp: -25 * Math.max(0, Math.abs(d.temp) - 0.3),
      sleep: (d.sleepScore - 75) * 0.3,
    };
  }
  function activityParts(d) {
    const wk = days.slice(Math.max(0, d.i - 6), d.i + 1);
    return {
      today: 45 * Math.min(1, d.steps / STEP_GOAL),
      week: 35 * Math.min(1, mean(wk.map(x => x.steps)) / STEP_GOAL),
      moving: 20 * clamp(1 - (d.sedentaryH - 4) / 8, 0, 1),
    };
  }
  const sum = o => Object.values(o).reduce((s, x) => s + x, 0);
  days.forEach(d => {
    d.sleepScore = r0(clamp(sum(sleepParts(d)), 0, 100));
    d.recovery = r0(clamp(72 + sum(recoveryParts(d)), 15, 99));
    d.activity = r0(clamp(sum(activityParts(d)), 0, 100));
  });

  // device + sync history (fake)
  const minsAgo = m => new Date(NOW.getTime() - m * 60000);
  const syncs = [
    [12, 'ok', 384], [168, 'ok', 2210], [311, 'ok', 1890], [446, 'ok', 5120],
    [905, 'fail', 0], [1004, 'ok', 2740], [1270, 'ok', 1960], [1490, 'ok', 3310],
  ].map(([m, st, ev]) => ({ t: minsAgo(m), st, ev }));
  const DEVICE = { battery: 64, firmware: '2.1.3', api: '2.1.0', model: 'Ring Gen 5 (demo)', serial: '••••-7Q2F', phone: 'Android · ringkeeper app v0.1 (demo)', pc: "Your PC · via Tailscale" };
  const ago = t => { const m = Math.round((NOW - t) / 60000); return m < 60 ? `${m} min ago` : m < 1440 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} d ago`; };
  const syncTime = t => (t.toDateString() === NOW.toDateString() ? 'Today ' : 'Yesterday ') + t.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });

  // ---------- SVG building blocks ----------
  const W = 340;
  let seq = 0; const charts = {};
  function frame(id, svg, readoutDefault) {
    return `<div class="chart" data-chart="${id}"><div class="readout">${readoutDefault || '&nbsp;'}</div>${svg}</div>`;
  }
  function lineChart(o) {
    const id = 'c' + (++seq), h = o.h || 150, pL = 34, pR = 8, pT = 8, pB = 20;
    const n = o.series[0].v.length, nx = o.nx || n;
    let all = []; o.series.forEach(s => s.v.forEach(x => x != null && all.push(x)));
    if (o.band) all = all.concat(o.band.lo, o.band.hi).filter(x => x != null);
    if (o.goal != null) all.push(o.goal);
    let mn = o.min != null ? o.min : Math.min(...all), mx = o.max != null ? o.max : Math.max(...all);
    const pad = (mx - mn) * 0.12 || 1; if (o.min == null) mn -= pad; if (o.max == null) mx += pad;
    const X = i => pL + (W - pL - pR) * (nx === 1 ? 0.5 : i / (nx - 1));
    const Y = v => pT + (h - pT - pB) * (1 - (v - mn) / (mx - mn));
    const yf = o.yFmt || (v => r0(v));
    let s = `<svg viewBox="0 0 ${W} ${h}" role="img" aria-label="${o.label || 'chart'}"><defs><linearGradient id="g${id}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${o.series[0].color}" stop-opacity=".35"/><stop offset="1" stop-color="${o.series[0].color}" stop-opacity="0"/></linearGradient></defs>`;
    for (let k = 0; k <= 2; k++) { const v = mn + (mx - mn) * (0.1 + 0.4 * k); s += `<line class="gridl" x1="${pL}" x2="${W - pR}" y1="${Y(v)}" y2="${Y(v)}"/><text x="${pL - 6}" y="${Y(v) + 3}" text-anchor="end">${yf(v)}</text>`; }
    if (o.band) {
      // indices with a band value (real-data baselines start after the first night)
      const bi = o.band.lo.map((v, i) => i).filter(i => o.band.lo[i] != null && o.band.hi[i] != null);
      const up = bi.map(i => `${X(i)},${Y(o.band.hi[i])}`), dn = bi.map(i => `${X(i)},${Y(o.band.lo[i])}`).reverse();
      if (bi.length === 1) { const i = bi[0], x0 = X(i) - 4, x1 = X(i) + 4; s += `<rect x="${x0}" y="${Y(o.band.hi[i])}" width="${x1 - x0}" height="${Math.max(1, Y(o.band.lo[i]) - Y(o.band.hi[i]))}" fill="${o.series[0].color}" opacity=".13"/>`; }
      else if (bi.length) s += `<polygon points="${up.concat(dn).join(' ')}" fill="${o.series[0].color}" opacity=".13"/>`;
      const mi = bi.filter(i => o.band.mid[i] != null);
      if (mi.length > 1) s += `<polyline points="${mi.map(i => `${X(i)},${Y(o.band.mid[i])}`).join(' ')}" fill="none" stroke="${o.series[0].color}" stroke-opacity=".45" stroke-dasharray="3 4"/>`;
    }
    if (o.goal != null) s += `<line class="goal" x1="${pL}" x2="${W - pR}" y1="${Y(o.goal)}" y2="${Y(o.goal)}"/>`;
    o.series.forEach((se, si) => {
      let d = '', started = false, lastPt = null, firstX = null;
      se.v.forEach((v, i) => { if (v == null) { started = false; return; } d += (started ? 'L' : 'M') + X(i).toFixed(1) + ',' + Y(v).toFixed(1); started = true; lastPt = [X(i), Y(v)]; if (firstX == null) firstX = X(i); });
      if (se.fill && lastPt) s += `<path d="${d}L${lastPt[0]},${h - pB}L${firstX},${h - pB}Z" fill="url(#g${id})"/>`;
      s += `<path d="${d}" fill="none" stroke="${se.color}" stroke-width="${se.w || 2.2}" stroke-linejoin="round" stroke-linecap="round"/>`;
      if (o.dots && si === 0) se.v.forEach((v, i) => { if (v != null) s += `<circle cx="${X(i)}" cy="${Y(v)}" r="2.6" fill="${se.color}"/>`; });
      if (lastPt && o.endDot !== false) s += `<circle cx="${lastPt[0]}" cy="${lastPt[1]}" r="4" fill="${se.color}" class="cur-dot"/>`;
    });
    (o.xLabels || []).forEach(([i, t]) => { s += `<text x="${X(i)}" y="${h - 5}" text-anchor="${i === 0 ? 'start' : i >= nx - 1 ? 'end' : 'middle'}">${t}</text>`; });
    s += `<line class="cursor" y1="${pT}" y2="${h - pB}" x1="-10" x2="-10" visibility="hidden"/><circle class="cur-dot" r="5" fill="${o.series[0].color}" visibility="hidden"/></svg>`;
    const xs = []; for (let i = 0; i < nx; i++) xs.push(X(i));
    charts[id] = { xs, ys: o.series[0].v.map(v => (v == null ? null : Y(v))), readout: o.readout, def: o.readoutDefault };
    return frame(id, s, o.readoutDefault);
  }
  function barChart(o) {
    const id = 'c' + (++seq), h = o.h || 140, pL = 34, pR = 8, pT = 8, pB = 20, n = o.v.length;
    const mx = (Math.max(...o.v, o.goal || 0) || 1) * 1.12, bw = (W - pL - pR) / n;
    const Y = v => pT + (h - pT - pB) * (1 - v / mx);
    let s = `<svg viewBox="0 0 ${W} ${h}" role="img" aria-label="${o.label || 'bar chart'}">`;
    for (let k = 1; k <= 2; k++) { const v = mx * 0.45 * k; s += `<line class="gridl" x1="${pL}" x2="${W - pR}" y1="${Y(v)}" y2="${Y(v)}"/><text x="${pL - 6}" y="${Y(v) + 3}" text-anchor="end">${o.yFmt ? o.yFmt(v) : r0(v)}</text>`; }
    const xs = [];
    o.v.forEach((v, i) => {
      const x = pL + bw * i + bw * 0.18, w = bw * 0.64; xs.push(x + w / 2);
      const hl = o.hl != null ? i === o.hl : i === n - 1;
      s += `<rect x="${x}" y="${Y(v)}" width="${w}" height="${Math.max(2, h - pB - Y(v))}" rx="${Math.min(7, w / 2)}" fill="${o.color}" opacity="${hl ? 1 : 0.55}"/>`;
      s += `<text x="${x + w / 2}" y="${h - 5}" text-anchor="middle">${o.labels[i]}</text>`;
    });
    if (o.goal) s += `<line class="goal" x1="${pL}" x2="${W - pR}" y1="${Y(o.goal)}" y2="${Y(o.goal)}"/><text x="${W - pR}" y="${Y(o.goal) - 4}" text-anchor="end">goal</text>`;
    s += `<line class="cursor" y1="${pT}" y2="${h - pB}" x1="-10" x2="-10" visibility="hidden"/></svg>`;
    charts[id] = { xs, ys: null, readout: o.readout, def: o.readoutDefault };
    return frame(id, s, o.readoutDefault);
  }
  const STAGES = [['Awake', 'var(--awake)'], ['REM', 'var(--rem)'], ['Light', 'var(--light)'], ['Deep', 'var(--deep)']];
  function hypnogram(o) {
    o = o || { ep: hyp, bedStart: last.bedStart, wake: wakeMin, hr: overnight.hr };
    const hyp = o.ep, last = { bedStart: o.bedStart }, wakeMin = o.wake, overnight = { hr: o.hr };
    const id = 'c' + (++seq), h = 150, pL = 44, pR = 8, pT = 6, pB = 20, n = hyp.length;
    const rowH = (h - pT - pB) / 4, X = k => pL + (W - pL - pR) * k / n;
    let s = `<svg viewBox="0 0 ${W} ${h}" role="img" aria-label="Sleep stages over the night">`;
    STAGES.forEach(([name], r) => { s += `<line class="gridl" x1="${pL}" x2="${W - pR}" y1="${pT + rowH * (r + 0.5)}" y2="${pT + rowH * (r + 0.5)}" opacity=".5"/><text x="${pL - 6}" y="${pT + rowH * (r + 0.5) + 3}" text-anchor="end">${name}</text>`; });
    for (let k = 0; k < n;) {
      let j = k; while (j < n && hyp[j] === hyp[k]) j++;
      const st = hyp[k];
      s += `<rect x="${X(k) + 0.4}" y="${pT + rowH * st + rowH * 0.18}" width="${Math.max(1.5, X(j) - X(k) - 0.8)}" height="${rowH * 0.64}" rx="3" fill="${STAGES[st][1]}"/>`;
      k = j;
    }
    const first = Math.ceil(last.bedStart / 120) * 120;
    for (let t = first; t < wakeMin - 30; t += 120) { const k = (t - last.bedStart) / 5; s += `<text x="${X(k)}" y="${h - 5}" text-anchor="middle">${shortClock(t)}</text>`; }
    s += `<line class="cursor" y1="${pT}" y2="${h - pB}" x1="-10" x2="-10" visibility="hidden"/></svg>`;
    const xs = hyp.map((_, k) => X(k + 0.5));
    charts[id] = { xs, ys: null, readout: k => `${fmtClock(last.bedStart + k * 5)} · <b>${STAGES[hyp[k]][0]}</b>${overnight.hr[k] == null ? '' : ` · HR ${r0(overnight.hr[k])} bpm`}`, def: `${fmtClock(last.bedStart)} → ${fmtClock(wakeMin)}${o.note ? ' · ' + o.note : ''}` };
    return frame(id, s, charts[id].def);
  }
  function ring(score, color, label, sub, txt) {
    const r = 34, c = 2 * Math.PI * r;
    return `<div class="ring"><svg viewBox="0 0 84 84"><circle cx="42" cy="42" r="${r}" class="ring-bg"/><circle cx="42" cy="42" r="${r}" class="ring-fg" stroke="${color}" style="--c:${c.toFixed(1)}" stroke-dasharray="${c.toFixed(1)}" stroke-dashoffset="${(c * (1 - score / 100)).toFixed(1)}" transform="rotate(-90 42 42)"/><text x="42" y="50" text-anchor="middle" class="ring-num">${txt != null ? txt : score}</text></svg><div class="ring-label">${label}</div><div class="ring-sub">${sub}</div></div>`;
  }
  function spark(vals, color) {
    vals = vals.filter(v => v != null);
    if (vals.length < 2) return '';
    const mn = Math.min(...vals), mx = Math.max(...vals), w = 100, h = 26;
    const pts = vals.map((v, i) => `${(i / (vals.length - 1) * w).toFixed(1)},${(2 + (h - 4) * (1 - (v - mn) / (mx - mn || 1))).toFixed(1)}`);
    return `<svg class="spark-line" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><polyline points="${pts.join(' ')}" fill="none" stroke="${color}" stroke-width="1.8" vector-effect="non-scaling-stroke" stroke-linejoin="round"/></svg>`;
  }
  const label = s => (s >= 85 ? 'Optimal' : s >= 70 ? 'Good' : s >= 60 ? 'Fair' : 'Pay attention');
  const deltaCls = (x, goodIfUp, eps) => Math.abs(x) < eps ? 'neutral' : (x > 0) === goodIfUp ? 'good' : 'bad';

  // ---------- icons ----------
  const I = {
    today: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="4.2"/><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4"/></svg>',
    sleep: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5z"/></svg>',
    activity: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12h4l2.5-6 5 12 2.5-6h4"/></svg>',
    trends: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 20h18"/><path d="M4 15l5-5 4 3 7-7"/><path d="M15 6h5v5"/></svg>',
    chat: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3h11A2.5 2.5 0 0 1 20 5.5v8a2.5 2.5 0 0 1-2.5 2.5H10l-4.5 4v-4h0A1.5 1.5 0 0 1 4 14.5z"/></svg>',
    cycle: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M12 3.5a8.5 8.5 0 1 1-8.1 5.9"/><circle cx="5.2" cy="6.2" r="1.6" fill="currentColor" stroke="none"/><path d="M12 8.5s3 3.2 3 5.2a3 3 0 0 1-6 0c0-2 3-5.2 3-5.2z"/></svg>',
    info: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 11v5.5M12 7.6v.2"/></svg>',
    sun: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>',
    moon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5z"/></svg>',
    ringIco: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><ellipse cx="12" cy="13" rx="8" ry="6.5"/><ellipse cx="12" cy="13" rx="5" ry="3.8"/></svg>',
    spark: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2l1.8 5.6L19.5 9.5l-5.7 1.9L12 17l-1.8-5.6L4.5 9.5l5.7-1.9zM19 15l.9 2.6 2.6.9-2.6.9L19 22l-.9-2.6-2.6-.9 2.6-.9z"/></svg>',
    send: '<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M3 20.5l18-8.5L3 3.5l2.5 7L15 12l-9.5 1.5z"/></svg>',
    lock: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><rect x="4.5" y="10.5" width="15" height="10" rx="2.5"/><path d="M8 10.5V7.5a4 4 0 0 1 8 0v3"/></svg>',
  };

  // ---------- screens ----------
  const state = { tab: 'today', sleepMetric: 'dur', range: 30, cycRange: 'all', calMonth: '0' };
  const last7 = days.slice(-7), prevBase = { hrv: baseline('hrv', DAYS - 1), rhr: baseline('rhr', DAYS - 1) };

  function aiSummary() {
    const dh = pct(last.hrv, prevBase.hrv.m);
    const sleepTxt = fmtDur(last.total);
    if (last.recovery >= 80) return `Recovery looks strong: HRV is ${r0(Math.abs(dh))}% ${dh >= 0 ? 'above' : 'below'} your 14-day baseline after ${sleepTxt} of sleep. Good day to push a bit harder.`;
    if (last.recovery >= 65) return `Solid, not peak. ${sleepTxt} of sleep, HRV ${dh >= 0 ? 'slightly above' : 'a little under'} baseline (${r0(last.hrv)} ms). A normal training day is fine. Keep bedtime near ${fmtClock(last.bedStart)}.`;
    return `Take it easier today: HRV is ${r0(Math.abs(dh))}% below baseline and resting HR is up. Prioritize an early night.`;
  }

  function renderToday() {
    const h = NOW.getHours(), greet = h < 12 ? 'Good morning' : h < 17 ? 'Good afternoon' : 'Good evening';
    const dh = pct(last.hrv, prevBase.hrv.m), dr = last.rhr - prevBase.rhr.m, dtF = cToF(last.temp);
    const sp = sleepParts(last), rp = recoveryParts(last), ap = activityParts(last);
    const f = (k, v, max) => `<li><span>${k}</span><span class="num">${max != null ? `${r0(v)} / ${max}` : signed(v, 0)}</span></li>`;
    $('#screen-today').innerHTML = `
      <div class="greet"><h2>${greet}</h2><div class="muted small">${fmtDate(NOW, { weekday: 'long', month: 'long', day: 'numeric' })}</div></div>
      <div class="card">
        <div class="rings">
          ${ring(last.sleepScore, 'var(--sleep)', 'Sleep', label(last.sleepScore))}
          ${ring(last.recovery, 'var(--rec)', 'Recovery', label(last.recovery))}
          ${ring(last.activity, 'var(--act)', 'Activity', 'so far today')}
        </div>
        <div class="ours">Our scores: own open formulas, not Oura's</div>
        <details><summary>What went into these?</summary>
          <ul class="factors">
            <li><b>Sleep</b><span></span></li>
            ${f('Duration (vs 8h)', sp.duration, 40)}${f('Efficiency', sp.efficiency, 25)}${f('Deep + REM share', sp.restorative, 20)}${f('Fell asleep quickly', sp.latency, 15)}
            <li><b>Recovery</b><span class="faint">base 72</span></li>
            ${f('HRV vs baseline', rp.hrv)}${f('Resting HR vs baseline', rp.rhr)}${f('Temperature', rp.temp)}${f('Last night’s sleep', rp.sleep)}
            <li><b>Activity</b><span></span></li>
            ${f('Steps today', ap.today, 45)}${f('7-day steps', ap.week, 35)}${f('Moving through the day', ap.moving, 20)}
          </ul>
        </details>
      </div>
      <div class="card ai-card" data-goto="chat">
        <div class="spark">${I.spark}</div>
        <div><div class="who">Health AI says…</div><p>${aiSummary()}</p></div>
      </div>
      <div class="stats">
        <div class="stat"><div class="k">Resting HR</div><div class="v">${r0(last.rhr)}<small>bpm</small></div><div class="d ${deltaCls(dr, false, 0.5)}">${signed(dr, 1)} vs baseline</div>${spark(last7.map(d => d.rhr), 'var(--bad)')}</div>
        <div class="stat"><div class="k">HRV (RMSSD)</div><div class="v">${r0(last.hrv)}<small>ms</small></div><div class="d ${deltaCls(dh, true, 3)}">${signed(dh, 0)}% vs baseline</div>${spark(last7.map(d => d.hrv), 'var(--rec)')}</div>
        <div class="stat"><div class="k">Skin temp</div><div class="v">${signed(dtF, 1)}<small>°F</small></div><div class="d ${Math.abs(last.temp) > 0.3 ? 'bad' : 'neutral'}">deviation from normal</div>${spark(last7.map(d => d.temp), 'var(--act)')}</div>
        <div class="stat"><div class="k">SpO₂ (night avg)</div><div class="v">${r1(last.spo2)}<small>%</small></div><div class="d neutral">resp. ${r1(last.resp)} /min</div>${spark(last7.map(d => d.spo2), 'var(--sleep)')}</div>
      </div>
      <div class="card syncbar" data-open-device>
        <span><span class="pulse"></span>Synced ${ago(syncs[0].t)}</span>
        <span class="batt">Ring <span class="batt-ico"><i style="width:${DEVICE.battery}%"></i></span>${DEVICE.battery}%</span>
      </div>`;
  }

  function renderSleep() {
    const tot = last.total, st = [['Awake', last.awake, 'var(--awake)'], ['REM', last.rem, 'var(--rem)'], ['Light', last.light, 'var(--light)'], ['Deep', last.deep, 'var(--deep)']];
    const d14 = days.slice(-14), lab = d14.map(d => fmtDate(d.date, { month: 'short', day: 'numeric' }));
    const dur = state.sleepMetric === 'dur';
    const vals = d14.map(d => (dur ? d.total / 60 : d.sleepScore));
    const nadirK = overnight.hr.indexOf(Math.min(...overnight.hr));
    const nl = hyp.length, xl = [[0, fmtClock(last.bedStart).replace(' ', '')], [Math.floor(nl / 2), fmtClock(last.bedStart + nl * 2.5)], [nl - 1, fmtClock(wakeMin)]];
    $('#screen-sleep').innerHTML = `
      <div class="card">
        <h3>Last night <span class="tag">${fmtDate(dayDate(DAYS - 2), { weekday: 'short' })} → ${fmtDate(NOW, { weekday: 'short' })}</span></h3>
        <div class="row"><div><div class="big num">${fmtDur(tot)}</div><div class="muted small">asleep · ${fmtClock(last.bedStart)} – ${fmtClock(wakeMin)}</div></div>
        <div style="width:84px">${ring(last.sleepScore, 'var(--sleep)', '', '')}</div></div>
        ${hypnogram()}
        <div class="stage-grid">${st.map(([k, v, c]) => `<div class="stage" style="border-color:${c}"><div class="k">${k}</div><div class="v">${fmtDur(v).replace('0h ', '')}</div><div class="p">${k === 'Awake' ? r0(v / last.inBed * 100) + '% of bed' : r0(v / tot * 100) + '%'}</div></div>`).join('')}</div>
        <div class="hint">Drag across the chart to scrub. Stages are estimated by our own model.</div>
      </div>
      <div class="stats four">
        <div class="stat"><div class="k">Total</div><div class="v">${fmtDur(tot).replace('m', '')}</div></div>
        <div class="stat"><div class="k">Efficiency</div><div class="v">${r0(last.eff)}<small>%</small></div></div>
        <div class="stat"><div class="k">Latency</div><div class="v">${r0(last.latency)}<small>min</small></div></div>
        <div class="stat"><div class="k">In bed</div><div class="v">${fmtDur(last.inBed).replace('m', '')}</div></div>
      </div>
      <div class="card">
        <h3>Heart rate overnight <span class="tag">lowest ${r0(overnight.hr[nadirK])} bpm at ${fmtClock(last.bedStart + nadirK * 5)}</span></h3>
        ${lineChart({ label: 'Overnight heart rate', h: 120, series: [{ v: overnight.hr, color: 'var(--bad)', fill: true }], xLabels: xl, endDot: false, readout: k => `${fmtClock(last.bedStart + k * 5)} · ${r0(overnight.hr[k])} bpm`, readoutDefault: `avg ${r0(mean(overnight.hr))} bpm` })}
        <h3 style="margin-top:14px">HRV overnight <span class="tag">avg ${r0(mean(overnight.hrv))} ms</span></h3>
        ${lineChart({ label: 'Overnight HRV', h: 120, series: [{ v: overnight.hrv, color: 'var(--rec)', fill: true }], xLabels: xl, endDot: false, readout: k => `${fmtClock(last.bedStart + k * 5)} · ${r0(overnight.hrv[k])} ms`, readoutDefault: `peak ${r0(Math.max(...overnight.hrv))} ms` })}
      </div>
      <div class="card">
        <h3>14-day trend <span class="seg" data-seg="sleepMetric"><button data-v="dur" class="${dur ? 'on' : ''}">Duration</button><button data-v="score" class="${dur ? '' : 'on'}">Score</button></span></h3>
        ${lineChart({ label: '14-day sleep trend', series: [{ v: vals, color: 'var(--sleep)', fill: true }], dots: true, goal: dur ? 8 : null, yFmt: v => (dur ? r1(v) + 'h' : r0(v)), xLabels: [[0, lab[0]], [7, lab[7]], [13, lab[13]]], readout: i => `${fmtDate(d14[i].date)} · ${fmtDur(d14[i].total)} · score ${d14[i].sleepScore} · eff ${r0(d14[i].eff)}%`, readoutDefault: dur ? `avg ${fmtDur(mean(d14.map(d => d.total)))} · dashed line = 8h` : `avg score ${r0(mean(d14.map(d => d.sleepScore)))}` })}
      </div>`;
  }

  function renderActivity() {
    const d = last, prog = Math.min(100, d.steps / STEP_GOAL * 100);
    const metV = met.map((m, b) => (b <= nowBin ? m : null));
    const w7 = days.slice(-7);
    $('#screen-activity').innerHTML = `
      <div class="card">
        <h3>Today <span class="tag">updated ${ago(syncs[0].t)}</span></h3>
        <div class="row"><div><div class="big num">${fmtInt(d.steps)}</div><div class="muted small">steps of ${fmtInt(STEP_GOAL)} goal</div></div>
        <div style="width:84px">${ring(d.activity, 'var(--act)', '', '')}</div></div>
        <div class="progress"><i style="width:${prog}%"></i></div>
        <div class="stats four" style="margin-top:12px">
          <div class="stat"><div class="k">Active cal</div><div class="v">${fmtInt(d.activeCal)}</div></div>
          <div class="stat"><div class="k">Distance</div><div class="v">${(d.steps * 0.00044).toFixed(1)}<small>mi</small></div></div>
          <div class="stat"><div class="k">Sitting</div><div class="v">${r1(d.sedentaryH)}<small>h</small></div></div>
          <div class="stat"><div class="k">Score</div><div class="v">${d.activity}</div></div>
        </div>
      </div>
      <div class="card">
        <h3>Intensity today <span class="tag">MET, 15-min</span></h3>
        ${lineChart({ label: 'Intraday MET', h: 140, nx: 96, min: 0.6, series: [{ v: metV, color: 'var(--act)', fill: true, w: 1.8 }], yFmt: v => r1(v), xLabels: [[0, '12a'], [24, '6a'], [48, '12p'], [72, '6p'], [95, '12a']], readout: b => (metV[b] == null ? `${fmtClock(b * 15)} · not yet` : `${fmtClock(b * 15)} · ${r1(met[b])} MET · ~${fmtInt(binSteps(met[b]))} steps`), readoutDefault: `peak ${r1(Math.max(...metV.filter(v => v != null)))} MET · walking ≈ 3–4 MET` })}
        <div class="legend"><span><i style="background:var(--act)"></i>MET (1 = resting)</span></div>
      </div>
      <div class="card">
        <h3>This week <span class="tag">avg ${fmtInt(mean(w7.slice(0, 6).map(x => x.steps)))} steps/day</span></h3>
        ${barChart({ label: 'Weekly steps', v: w7.map(x => x.steps), labels: w7.map(x => fmtDate(x.date, { weekday: 'narrow' })), color: 'var(--act)', goal: STEP_GOAL, yFmt: v => r1(v / 1000) + 'k', readout: i => `${fmtDate(w7[i].date)} · ${fmtInt(w7[i].steps)} steps · ${fmtInt(w7[i].activeCal)} cal${i === 6 ? ' (so far)' : ''}`, readoutDefault: 'Tap a bar for details' })}
        ${barChart({ label: 'Weekly active calories', h: 110, v: w7.map(x => x.activeCal), labels: w7.map(x => fmtDate(x.date, { weekday: 'narrow' })), color: 'var(--bad)', readout: i => `${fmtDate(w7[i].date)} · ${fmtInt(w7[i].activeCal)} active cal`, readoutDefault: 'Active calories' })}
      </div>`;
  }

  function trendCard(key, title, unit, color, fmtV, bandFixed) {
    const n = state.range, ds = days.slice(-n), off = DAYS - n;
    const v = ds.map(d => (key === 'temp' ? cToF(d[key]) : d[key]));
    let band;
    if (bandFixed) band = { lo: v.map(() => -bandFixed), hi: v.map(() => bandFixed), mid: v.map(() => 0) };
    else {
      const b = ds.map((d, j) => baseline(key, off + j));
      band = { lo: b.map(x => x.m - x.s), hi: b.map(x => x.m + x.s), mid: b.map(x => x.m) };
    }
    const lab = ds.map(d => fmtDate(d.date, { month: 'short', day: 'numeric' }));
    const lv = v[v.length - 1], bm = band.mid[band.mid.length - 1];
    const inBand = lv >= band.lo[n - 1] && lv <= band.hi[n - 1];
    return `<div class="card">
      <h3>${title} <span class="tag">${inBand ? 'within normal range' : lv > band.hi[n - 1] ? 'above normal range' : 'below normal range'}</span></h3>
      <div class="row" style="margin-bottom:6px"><div><span class="big num" style="font-size:26px">${fmtV(lv)}</span> <span class="muted small">${unit} latest</span></div>
      <div class="small muted" style="text-align:right">baseline<br><b class="num">${fmtV(bm)} ${unit}</b></div></div>
      ${lineChart({ label: title, series: [{ v, color, fill: false }], band, dots: n <= 14, xLabels: [[0, lab[0]], [Math.floor(n / 2), lab[Math.floor(n / 2)]], [n - 1, lab[n - 1]]], yFmt: fmtV, readout: i => `${fmtDate(ds[i].date)} · ${fmtV(v[i])} ${unit} · normal ${fmtV(band.lo[i])}–${fmtV(band.hi[i])}`, readoutDefault: bandFixed ? `Shaded = normal range (±${bandFixed} ${unit})` : 'Shaded = your normal range (14-day baseline ±1 SD)' })}
    </div>`;
  }
  function renderTrends() {
    $('#screen-trends').innerHTML = `
      <div class="row" style="margin-top:12px"><div class="muted small">Your numbers vs your own baseline</div>
      <span class="seg" data-seg="range"><button data-v="14" class="${state.range === 14 ? 'on' : ''}">14 days</button><button data-v="30" class="${state.range === 30 ? 'on' : ''}">30 days</button></span></div>
      ${trendCard('hrv', 'HRV (RMSSD)', 'ms', 'var(--rec)', v => r0(v))}
      ${trendCard('rhr', 'Resting heart rate', 'bpm', 'var(--bad)', v => r0(v))}
      ${trendCard('temp', 'Skin temp deviation', '°F', 'var(--act)', v => signed(v, 1), 0.5)}
      <div class="hint">Temperature band is a fixed ±0.5 °F around your normal. Slow ~monthly waves are expected.</div>`;
  }

  // ---------- cycle ----------
  const PH = { menstrual: ['Menstrual', 'var(--cyc)'], follicular: ['Follicular', 'var(--fol)'], ovulation: ['Ovulation window', 'var(--fert)'], luteal: ['Luteal', 'var(--lut)'] };
  const fmtMD = d => fmtDate(d, { month: 'short', day: 'numeric' });
  function phaseOf(c, cd) {
    if (cd <= c.per) return 'menstrual';
    if (c.fertile && cd >= c.fertile[0] && cd <= c.fertile[1]) return 'ovulation';
    if (!c.fertile || cd < c.fertile[0]) return 'follicular';
    return 'luteal';
  }
  function cycleRing() {
    const c = CYC.cur, L = Math.max(CYC.predLen, CYC.CUR_DAY), cd = CYC.CUR_DAY, R = 78, cx = 100, cy = 100, gap = 0.007;
    const P = f => [cx + R * Math.sin(2 * Math.PI * f), cy - R * Math.cos(2 * Math.PI * f)];
    const arc = (a, b) => { const [x0, y0] = P(a), [x1, y1] = P(b); return `M${x0.toFixed(1)},${y0.toFixed(1)} A${R},${R} 0 ${b - a > 0.5 ? 1 : 0} 1 ${x1.toFixed(1)},${y1.toFixed(1)}`; };
    const segs = []; let run = null;
    for (let d = 1; d <= L; d++) { const ph = phaseOf(c, d); if (!run || run.ph !== ph) { run = { ph, a: d, b: d }; segs.push(run); } else run.b = d; }
    let s = `<svg viewBox="0 0 200 200" class="cyc-ring" role="img" aria-label="Cycle day ${cd} of about ${L}"><circle cx="${cx}" cy="${cy}" r="${R}" class="ring-bg" style="stroke-width:16"/>`;
    segs.forEach(g => {
      s += `<path d="${arc((g.a - 1) / L + gap, g.b / L - gap)}" stroke="${PH[g.ph][1]}" stroke-width="14" fill="none" opacity=".28"/>`;
      if (g.a <= cd) s += `<path class="cyc-arc" d="${arc((g.a - 1) / L + gap, Math.min(g.b, cd) / L - (g.b <= cd ? gap : 0))}" stroke="${PH[g.ph][1]}" stroke-width="14" fill="none"/>`;
    });
    const [mx, my] = P(cd / L);
    s += `<circle cx="${mx.toFixed(1)}" cy="${my.toFixed(1)}" r="9" fill="var(--card)" stroke="var(--text)" stroke-width="3"/>`;
    const [tx, ty] = P(0);
    s += `<text x="${tx + 4}" y="${ty - 12}" class="cyc-tick">day 1</text>`;
    s += `<text x="100" y="88" text-anchor="middle" class="cyc-sm">Cycle day</text><text x="100" y="120" text-anchor="middle" class="cyc-day">${cd}</text><text x="100" y="141" text-anchor="middle" class="cyc-sm">of ~${CYC.predLen}</text></svg>`;
    return s;
  }
  function cycleTempChart() {
    const all = state.cycRange === 'all';
    const ns = all ? CYC.nights : CYC.nights.filter(n => n.ci === CYC.cycles.length - 1);
    const id = 'c' + (++seq), h = 180, pL = 34, pR = 8, pT = 20, pB = 20, n = ns.length, bw = (W - pL - pR) / n;
    const mn = -0.35, mx = 0.5;
    const X = i => pL + bw * (i + 0.5), Y = v => pT + (h - pT - pB) * (1 - (clamp(v, mn, mx) - mn) / (mx - mn));
    const idx = {}; ns.forEach((x, i) => { idx[dkey(x.date)] = i; });
    let s = `<svg viewBox="0 0 ${W} ${h}" role="img" aria-label="Nightly skin temperature deviation across cycles">`;
    for (let i = 0; i < n;) {
      if (!ns[i].period) { i++; continue; }
      let j = i; while (j < n && ns[j].period) j++;
      s += `<rect x="${(pL + bw * i).toFixed(1)}" y="${pT}" width="${(bw * (j - i)).toFixed(1)}" height="${h - pT - pB}" fill="var(--cyc)" opacity=".17" rx="2"/>`;
      i = j;
    }
    [-0.2, 0, 0.2, 0.4].forEach(v => { s += `<line class="gridl" x1="${pL}" x2="${W - pR}" y1="${Y(v)}" y2="${Y(v)}"${v === 0 ? ' stroke-dasharray="2 3" style="stroke:var(--faint)"' : ''}/><text x="${pL - 6}" y="${Y(v) + 3}" text-anchor="end">${v === 0 ? '0' : signed(v, 1)}°</text>`; });
    let labeled = false;
    CYC.cycles.slice().reverse().forEach(c => {
      if (!c.shift) return;
      const b = idx[dkey(c.shift.confirm.date)]; if (b == null) return;
      const a = idx[dkey(c.shift.from)];
      s += `<line x1="${(a == null ? pL : X(a) - bw / 2).toFixed(1)}" x2="${(X(b) + bw / 2).toFixed(1)}" y1="${Y(c.shift.thr).toFixed(1)}" y2="${Y(c.shift.thr).toFixed(1)}" stroke="var(--fert)" stroke-width="1.3" stroke-dasharray="4 3"/>`;
      const o = idx[dkey(addDays(c.start, c.estOv - 1))];
      if (o == null) return;
      s += `<line x1="${X(o)}" x2="${X(o)}" y1="${pT - 6}" y2="${h - pB}" stroke="var(--fert)" stroke-width="1.2" opacity=".75"/><circle cx="${X(o)}" cy="${pT - 8}" r="3.5" fill="var(--fert)"/>`;
      if (!labeled) { s += `<text x="${X(o) - 6}" y="${pT - 5}" text-anchor="end" style="fill:var(--fert)">est. ovulation</text>`; labeled = true; }
    });
    let d = '', on = false;
    ns.forEach((x, i) => { if (x.excluded) return; d += (on ? 'L' : 'M') + X(i).toFixed(1) + ',' + Y(x.t).toFixed(1); on = true; });
    s += `<path d="${d}" fill="none" stroke="var(--act)" stroke-width="${all ? 1.7 : 2.2}" stroke-linejoin="round"/>`;
    ns.forEach((x, i) => {
      if (x.excluded) s += `<circle cx="${X(i)}" cy="${Y(x.t)}" r="3" fill="var(--card)" stroke="var(--muted)" stroke-width="1.3"/>`;
      else s += `<circle cx="${X(i)}" cy="${Y(x.t)}" r="${all ? 1.6 : 2.8}" fill="var(--act)"/>`;
    });
    const xl = all ? CYC.cycles.map(c => [idx[dkey(c.start)], fmtMD(c.start)]) : [[0, 'Day 1'], [Math.floor(n / 2), 'Day ' + (Math.floor(n / 2) + 1)], [n - 1, 'Today']];
    xl.forEach(([i, t]) => { if (i != null) s += `<text x="${i === 0 ? pL : X(i)}" y="${h - 5}" text-anchor="${i === 0 ? 'start' : i >= n - 1 ? 'end' : 'middle'}">${t}</text>`; });
    s += `<line class="cursor" y1="${pT}" y2="${h - pB}" x1="-10" x2="-10" visibility="hidden"/><circle class="cur-dot" r="5" fill="var(--act)" visibility="hidden"/></svg>`;
    const def = `Follicular avg ${signed(CYC.folAvg, 2)} °C → after ovulation ${signed(CYC.lutAvg, 2)} °C (shift <b>${signed(CYC.lutAvg - CYC.folAvg, 2)} °C</b>)`;
    charts[id] = {
      xs: ns.map((_, i) => X(i)), ys: ns.map(x => (x.excluded ? null : Y(x.t))), def,
      readout: i => {
        const x = ns[i], c = CYC.cycles[x.ci], tags = [];
        if (x.period) tags.push('<span style="color:var(--cyc)">period</span>');
        if (c.estOv === x.cd) tags.push('<span style="color:var(--fert)">est. ovulation</span>');
        if (c.shift && c.shift.confirm === x) tags.push('<span style="color:var(--fert)">temp shift confirmed</span>');
        if (x.excluded) tags.push(`excluded (${x.excluded})`);
        return `${fmtDate(x.date)} · day ${x.cd} · <b>${signed(x.t, 2)} °C</b> (${signed(cToF(x.t), 1)} °F)${tags.length ? ' · ' + tags.join(' · ') : ''}`;
      },
    };
    return frame(id, s, def);
  }
  function cycleCalendar() {
    const off = +state.calMonth, base = new Date(today.getFullYear(), today.getMonth() + off, 1);
    const first = base.getDay(), dim = new Date(base.getFullYear(), base.getMonth() + 1, 0).getDate();
    let cells = ['S', 'M', 'T', 'W', 'T', 'F', 'S'].map(w => `<div class="wd">${w}</div>`).join('');
    for (let i = 0; i < first; i++) cells += '<div></div>';
    for (let dd = 1; dd <= dim; dd++) {
      const dt = new Date(base.getFullYear(), base.getMonth(), dd), m = CYC.cal[dkey(dt)] || {}, isToday = dkey(dt) === dkey(today);
      const cls = ['d', m.period && 'period', m.pred && 'pred', (m.fert || m.fertPred) && 'fert', m.ov && 'ov', isToday && 'today'].filter(Boolean).join(' ');
      const why = [m.period && 'period (logged)', m.pred && 'predicted period', (m.fert || m.fertPred) && 'fertile window (estimate)', m.ov && 'estimated ovulation', isToday && 'today'].filter(Boolean).join(', ');
      cells += `<div class="${cls}"${why ? ` title="${why}"` : ''}>${dd}</div>`;
    }
    const mName = k => new Date(today.getFullYear(), today.getMonth() + k, 1).toLocaleDateString('en-US', { month: 'short' });
    return `<div class="card">
      <h3>Calendar <span class="seg" data-seg="calMonth"><button data-v="-1" class="${off === -1 ? 'on' : ''}">${mName(-1)}</button><button data-v="0" class="${off === 0 ? 'on' : ''}">${mName(0)}</button><button data-v="1" class="${off === 1 ? 'on' : ''}">${mName(1)}</button></span></h3>
      <div class="cal-title">${base.toLocaleDateString('en-US', { month: 'long', year: 'numeric' })}</div>
      <div class="cal">${cells}</div>
      <div class="legend"><span><i class="lg-period"></i>Period (logged)</span><span><i class="lg-pred"></i>Predicted period</span><span><i class="lg-fert"></i>Fertile window <em>(estimate)</em></span><span><i class="lg-ov"></i>Est. ovulation</span></div>
      <div class="hint">Next period predicted ~${fmtMD(CYC.nextStart)} (±2 days). Fertile window = 5 days before to 1 day after estimated ovulation; it’s a rough estimate, not a safe/unsafe signal.</div>
    </div>`;
  }
  function renderCycle() {
    const c = CYC.cur, cd = CYC.CUR_DAY, ph = phaseOf(c, cd), L = CYC.predLen;
    const rng = (a, b) => (a === b ? `day ${a}` : `days ${a}–${b}`);
    const fw = c.fertile || [CYC.nextOvDay - 5, CYC.nextOvDay + 1];
    const phases = [['menstrual', rng(1, c.per), 'logged'], ['follicular', rng(c.per + 1, fw[0] - 1), ''], ['ovulation', rng(fw[0], fw[1]), 'estimate'], ['luteal', rng(fw[1] + 1, L), 'until next period']];
    const maxLen = Math.max(...CYC.done.map(x => x.len)) + 1;
    const seg = (a, b, cls) => `<i class="${cls}" style="left:${((a - 1) / maxLen * 100).toFixed(2)}%;width:${((b - a + 1) / maxLen * 100).toFixed(2)}%"></i>`;
    const flows = ['Spotting', 'Light', 'Medium', 'Heavy'];
    const symptoms = ['Cramps', 'Bloating', 'Headache', 'Tender breasts', 'Mood changes', 'Fatigue', 'Acne', 'Cravings', 'Back pain', 'Trouble sleeping'];
    const lens = CYC.done.map(x => x.len);
    $('#screen-cycle').innerHTML = `
      <div class="card">
        <h3>This cycle <span class="tag">started ${fmtMD(c.start)}</span></h3>
        <div class="cyc-top">
          <div class="cyc-ring-wrap">${cycleRing()}</div>
          <div>
            <div class="cyc-phase" style="color:${PH[ph][1]}">${PH[ph][0]} phase</div>
            <div class="muted small">Period expected in ~${CYC.daysToNext} days (${fmtMD(CYC.nextStart)})</div>
            <ul class="phase-list">${phases.map(([k, r, note]) => `<li class="${k === ph ? 'on' : ''}"><i style="background:${PH[k][1]}"></i><span>${PH[k][0]}</span><span class="faint">${r}${note ? ` · ${note}` : ''}</span></li>`).join('')}</ul>
          </div>
        </div>
        ${c.shift ? `<div class="shift-note"><b>Temp shift detected</b> on day ${c.shift.confirm.cd} (3 nights above the highest of the previous 6), so ovulation was probably around <b>day ${c.estOv}</b>.</div>` : `<div class="shift-note">No temperature shift detected yet this cycle.</div>`}
      </div>
      <div class="card notice" role="note">
        <div class="notice-ico">${I.info}</div>
        <div>
          <div class="notice-title">Not birth control</div>
          <p>This tracker is for awareness only and isn’t validated for preventing pregnancy. If you want temperature-based birth control, use an FDA-cleared method (e.g. Natural Cycles).</p>
          <p>Hormonal birth control (pill, patch, ring, implant, hormonal IUD) usually suppresses ovulation, so the temperature shift won’t appear and phases here won’t mean much.</p>
        </div>
      </div>
      <div class="card">
        <h3>Skin temperature <span class="seg" data-seg="cycRange"><button data-v="all" class="${state.cycRange === 'all' ? 'on' : ''}">${CYC.cycles.length} cycles</button><button data-v="cur" class="${state.cycRange === 'cur' ? 'on' : ''}">This cycle</button></span></h3>
        ${cycleTempChart()}
        <div class="legend"><span><i style="background:var(--act)"></i>Nightly deviation</span><span><i style="background:var(--cyc);opacity:.45"></i>Period</span><span><i style="background:var(--fert)"></i>Est. ovulation</span><span><i class="lg-thr"></i>Highest of previous 6 nights</span></div>
        <div class="hint">Rule: when 3 nights in a row are above the highest of the 6 before them, a shift is confirmed and ovulation is estimated as the night before the rise. Hollow dots (late nights, illness) are skipped. Drag to scrub.</div>
      </div>
      ${cycleCalendar()}
      <div class="card">
        <h3>Log <span class="tag">${fmtDate(today)}</span></h3>
        <div class="btns"><button class="btn cyc-btn" data-demo="Period start logged (demo)">Start period</button><button class="btn" data-demo="Period end logged (demo)">End period</button></div>
        <div class="log-label">Flow</div>
        <div class="chips wrap">${flows.map(f => `<button class="chip" data-toggle="one">${f}</button>`).join('')}</div>
        <div class="log-label">Symptoms</div>
        <div class="chips wrap">${symptoms.map(f => `<button class="chip" data-toggle>${f}</button>`).join('')}</div>
        <div class="hint">Demo: nothing is saved yet.</div>
      </div>
      <div class="card">
        <h3>Cycle stats <span class="tag">last ${CYC.done.length} cycles</span></h3>
        <div class="stats">
          <div class="stat"><div class="k">Avg cycle</div><div class="v">${r1(CYC.avgLen)}<small>d</small></div></div>
          <div class="stat"><div class="k">Avg period</div><div class="v">${r1(CYC.avgPer)}<small>d</small></div></div>
          <div class="stat"><div class="k">Range</div><div class="v">${Math.min(...lens)}–${Math.max(...lens)}<small>d</small></div></div>
          <div class="stat"><div class="k">Luteal phase</div><div class="v">~${r0(CYC.avgLuteal)}<small>d</small></div></div>
        </div>
        <ul class="cyc-list">${CYC.done.slice().reverse().map(x => `<li>
          <div class="row"><span><b>${fmtMD(x.start)} – ${fmtMD(addDays(x.start, x.len - 1))}</b></span><span class="muted small">${x.len} days · period ${x.per} · ov ~day ${x.estOv || '?'}</span></div>
          <div class="cyc-bar">${seg(1, x.len, 'track')}${seg(1, x.per, 'p')}${x.fertile ? seg(x.fertile[0], x.fertile[1], 'f') : ''}${x.estOv ? seg(x.estOv, x.estOv, 'o') : ''}</div>
        </li>`).join('')}</ul>
      </div>
      <div class="card privacy">${I.lock}<p><b>Cycle data stays on your phone and your PC.</b> It’s never sent to Oura or any other cloud, and you can delete it any time.</p></div>`;
  }

  function renderChat() {
    const wk = days.slice(-7), best = wk.reduce((a, b) => (b.sleepScore > a.sleepScore ? b : a)), worst = wk.reduce((a, b) => (b.sleepScore < a.sleepScore ? b : a));
    const ev = days[EVENT], evB = baseline('hrv', EVENT), evR = baseline('rhr', EVENT);
    const deepAvg = mean(days.slice(-14).map(d => d.deep)), deepPct = mean(days.slice(-14).map(d => d.deep / d.total * 100));
    const wd = d => fmtDate(d.date, { weekday: 'long' });
    $('#screen-chat').innerHTML = `
      <div class="chat">
        <div class="day-sep">Sample conversation · demo</div>
        <div class="msg me">How did I sleep this week?</div>
        <div class="msg ai"><div class="who">Health AI</div>Pretty well overall. You averaged <b>${fmtDur(mean(wk.map(d => d.total)))}</b> a night at <b>${r0(mean(wk.map(d => d.eff)))}%</b> efficiency, with an average sleep score of <b>${r0(mean(wk.map(d => d.sleepScore)))}</b>.
          <ul><li>Best night: ${wd(best)} (${best.sleepScore}, ${fmtDur(best.total)})</li><li>Roughest: ${wd(worst)} (${worst.sleepScore}, ${fmtDur(worst.total)})</li><li>Bedtimes ranged ${fmtClock(Math.min(...wk.map(d => d.bedStart)))}–${fmtClock(Math.max(...wk.map(d => d.bedStart)))}</li></ul>
          <div class="cite">Based on 7 nights · ring data on your PC</div></div>
        <div class="msg me">Why was my HRV so low on ${wd(ev)}?</div>
        <div class="msg ai"><div class="who">Health AI</div>That night stands out. HRV was <b>${r0(ev.hrv)} ms</b> vs your usual ~${r0(evB.m)}, resting HR <b>${r0(ev.rhr)} bpm</b> (usually ~${r0(evR.m)}) and skin temp ran <b>${signed(cToF(ev.temp), 1)} °F</b>. You also went to bed late (${fmtClock(ev.bedStart)}) and slept only ${fmtDur(ev.total)}.
          That pattern usually comes from a late night, alcohol, a heavy late meal or a bug coming on. You were back to normal within two nights.<div class="cite">Not medical advice.</div></div>
        <div class="msg me">Am I getting enough deep sleep?</div>
        <div class="msg ai"><div class="who">Health AI</div>Looks fine. Over 14 nights you averaged <b>${r0(deepAvg)} min</b> of deep sleep (<b>${r0(deepPct)}%</b> of sleep). That's inside the typical 13–23% adult range. Most of it comes in the first 3 hours, so a consistent bedtime helps more than sleeping in.
          <div class="cite">Deep sleep is estimated from movement, HR and HRV, so treat it as a rough number.</div></div>
        <div class="msg me">Is my cycle regular? When should I expect my next period?</div>
        <div class="msg ai"><div class="who">Health AI</div>Your last three cycles were <b>${CYC.done.map(c => c.len).join(', ')} days</b> (average ${r1(CYC.avgLen)}). A few days of variation like that is common; typical adult cycles run about 21–35 days. Each cycle your skin temperature rose by about <b>${signed(CYC.lutAvg - CYC.folAvg, 1)} °C</b> and stayed up, which usually means ovulation has happened. This cycle the shift showed up on day ${CYC.cur.shift ? CYC.cur.shift.confirm.cd : '–'}.
          Your next period will probably start <b>around ${fmtDate(CYC.nextStart, { weekday: 'short', month: 'short', day: 'numeric' })}</b>, give or take a couple of days. If your cycles start changing a lot, or periods get very painful or heavy, that's worth bringing up with a doctor.
          <div class="cite">Estimates for awareness only. This is not birth control and can’t tell you which days are “safe”. On hormonal birth control the temperature shift usually won’t appear.</div></div>
      </div>
      <div class="composer">
        <div class="chips">${['Compare to last month', 'When’s my next period?', 'Am I getting sick?', 'Best time to work out?', 'Weekly summary'].map(t => `<button class="chip" data-demo>${t}</button>`).join('')}</div>
        <div class="box"><input type="text" placeholder="Ask about your sleep, HRV, activity…" aria-label="Message" disabled><button class="send" aria-label="Send" data-demo>${I.send}</button></div>
        <div class="note">Demo: chat isn’t connected yet. Answers would be generated on your PC.</div>
      </div>`;
  }

  function renderDeviceReal() {
    const R = REAL, j = R.j, top = Object.entries(j.event_counts).slice(0, 10);
    $('#sheet-body').innerHTML = `
      <div class="device-hero"><span style="color:var(--sleep)">${I.ringIco}</span><div><h2>Your ring (real)</h2><div class="muted small">Read directly over BLE by ringkeeper</div></div></div>
      <div class="card"><h3>Ring</h3><ul class="kv">
        <li><span>Battery</span>${j.device.battery && j.device.battery.pct != null ? `<span><span class="batt">${battTxt(j)}</span> <small class="faint">as of ${new Date(j.device.battery.at).toLocaleString('en-US', { weekday: 'short', hour: 'numeric', minute: '2-digit' })}</small></span>` : '<span class="muted">unknown (not decoded yet)</span>'}</li>
        <li><span>Firmware</span><span>${j.device.firmware}${j.device.api ? ` (API ${j.device.api})` : ''}</span></li>
        <li><span>Last sync</span><span>${R.lastSync.toLocaleString('en-US', { weekday: 'short', hour: 'numeric', minute: '2-digit' })} · ${syncAgo(R.lastSync)}</span></li>
        <li><span>Nights of data</span><span>${j.nights}</span></li>
        <li><span>Events stored</span><span>${fmtInt(j.total_events)}</span></li>
        <li><span>Exported</span><span>${new Date(j.generated).toLocaleString('en-US', { hour: 'numeric', minute: '2-digit', month: 'short', day: 'numeric' })}</span></li>
      </ul></div>
      <div class="card"><h3>Event counts</h3><ul class="kv synclist">${top.map(([k, v]) => `<li><span>${k}</span><span>${fmtInt(v)}</span></li>`).join('')}</ul></div>
      ${privacyCard()}
      <div class="card privacy">${I.lock}<p><b>Private by design.</b> This data came straight off the ring into a local database on your PC. No Oura cloud.</p></div>`;
  }
  // ---------- Privacy: GET /api/export (ZIP), POST /api/delete (phrase + PIN; moves to trash, never hard-deletes) ----------
  const DEL_PHRASE = 'DELETE MY DATA';
  function privacyCard() {
    return `<div class="card" id="privacyCard"><h3>Privacy <span class="tag">your data, your control</span></h3>
      <p class="muted small">Download everything as a ZIP (nightly metrics, cycle log, every raw ring event as CSV), or remove it from this PC.</p>
      <div class="btns"><a class="btn" id="exportBtn" href="/api/export" download>Export my data</a><button type="button" class="btn danger" id="delStart">Delete my data…</button></div>
      <form id="delForm" class="del-form" hidden autocomplete="off">
        <p class="small">This moves all ring data, nightly results and your cycle log off the dashboard into a <b>trash folder on your PC</b> (kept for at least 30 days, then you can empty it). Export first if you want a copy.</p>
        <label class="small">Type <b>${DEL_PHRASE}</b><input id="delPhrase" type="text" autocapitalize="characters" spellcheck="false" placeholder="${DEL_PHRASE}"></label>
        <label class="small">Dashboard PIN<input id="delPin" type="password" inputmode="numeric" maxlength="12" placeholder="PIN"></label>
        <div class="btns"><button type="button" class="btn" id="delCancel">Cancel</button><button type="submit" class="btn danger" id="delGo" disabled>Delete</button></div>
        <p class="small" id="delMsg" role="status"></p>
      </form></div>`;
  }
  async function doDelete() {
    const go = $('#delGo'), msg = $('#delMsg');
    go.disabled = true; msg.textContent = 'Deleting…';
    try {
      const r = await fetch('/api/delete', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ confirm: $('#delPhrase').value.trim(), pin: $('#delPin').value.trim() }) });
      const j = await r.json().catch(() => ({}));
      if (r.ok && j.ok) {
        $('#delForm').innerHTML = `<p><b>Done.</b> ${j.moved.length} item${j.moved.length === 1 ? '' : 's'} moved to a trash folder on the PC (recoverable until at least ${esc(j.keep_until)}). Backups on the PC are separate; ask whoever runs the PC to remove them too if you want.</p><button type="button" class="btn" onclick="location.reload()">Reload</button>`;
      } else { msg.textContent = j.error || `Failed (HTTP ${r.status})`; go.disabled = false; }
    } catch (e) { msg.textContent = 'Could not reach the PC.'; go.disabled = false; }
  }
  document.addEventListener('input', e => {
    if (e.target.id === 'delPhrase' || e.target.id === 'delPin') {
      const ok = $('#delPhrase').value.trim() === DEL_PHRASE && /^\d{4,12}$/.test($('#delPin').value.trim());
      $('#delGo').disabled = !ok;
    }
  });
  document.addEventListener('submit', e => { if (e.target.id === 'delForm') { e.preventDefault(); if (!$('#delGo').disabled) doDelete(); } });
  document.addEventListener('click', e => {
    if (e.target.closest('#delStart')) { $('#delForm').hidden = false; $('#delStart').disabled = true; $('#delPhrase').focus(); }
    else if (e.target.closest('#delCancel')) { $('#delForm').hidden = true; $('#delStart').disabled = false; $('#delPhrase').value = $('#delPin').value = ''; $('#delMsg').textContent = ''; $('#delGo').disabled = true; }
  });
  function renderDevice() {
    if (isReal()) return renderDeviceReal();
    $('#sheet-body').innerHTML = `
      <div class="device-hero"><span style="color:var(--sleep)">${I.ringIco}</span><div><h2>${DEVICE.model}</h2><div class="muted small">Serial ${DEVICE.serial} · paired with this phone</div></div></div>
      <div class="card"><h3>Ring</h3><ul class="kv">
        <li><span>Battery</span><span><span class="batt"><span class="batt-ico"><i style="width:${DEVICE.battery}%"></i></span>${DEVICE.battery}% · ~3 days left</span></span></li>
        <li><span>Firmware</span><span>${DEVICE.firmware} (API ${DEVICE.api})</span></li>
        <li><span>Firmware updates</span><span class="muted">Off (need Oura cloud)</span></li>
        <li><span>Last sync</span><span>${syncTime(syncs[0].t)} · ${ago(syncs[0].t)}</span></li>
        <li><span>Phone</span><span>${DEVICE.phone}</span></li>
        <li><span>Uploads to</span><span>${DEVICE.pc}</span></li>
      </ul></div>
      <div class="btns"><button class="btn primary" data-demo="Sync started (demo)">Sync now</button><button class="btn" data-demo="Export would create a ZIP (demo)">Export my data</button><button class="btn danger" data-demo="Would ask to confirm first (demo)">Delete all data</button></div>
      <div class="card"><h3>Sync history</h3><ul class="kv synclist">
        ${syncs.map(s => `<li><span>${syncTime(s.t)}</span><span class="${s.st === 'ok' ? 'ok' : 'fail'}">${s.st === 'ok' ? `✓ ${fmtInt(s.ev)} events` : '✕ Ring out of range'}</span></li>`).join('')}
      </ul></div>
      <div class="card privacy">${I.lock}<p><b>Private by design.</b> Data stays on your phone and your PC. No Oura cloud, no third-party servers. The phone uploads over a private Tailscale link, and you can export or delete everything at any time.</p></div>`;
  }

  if (/[?&]debug/.test(location.search)) { const pre = document.createElement('pre'); pre.id = 'debug'; pre.textContent = JSON.stringify(days.map(d => [d.sleepScore, d.recovery, d.activity, r0(d.total), r0(d.hrv), d.steps])); document.body.appendChild(pre); }

  // ---------- REAL data mode (ui-mockup/real-data.json, written by tools/export_dashboard.py) ----------
  let REAL = null;
  const isoMin = s => +s.slice(11, 13) * 60 + +s.slice(14, 16) + +s.slice(17, 19) / 60; // local wall-clock minutes from the ISO string
  const isReal = () => !!REAL && state.mode === 'real';
  // display only: linearly bridge interior gaps so filled charts don't draw across them (raw values stay in readouts)
  const bridge = a => a.map((v, i) => {
    if (v != null) return v;
    let p = i - 1, q = i + 1; while (p >= 0 && a[p] == null) p--; while (q < a.length && a[q] == null) q++;
    return p >= 0 && q < a.length ? a[p] + (a[q] - a[p]) * (i - p) / (q - p) : null;
  });
  const EST = '<span class="tag est-tag">estimated (our heuristic, not Oura)</span>';
  function prepReal(j) {
    const s = j.series, e = j.estimated, bedStart = isoMin(j.night.bed), inBed = j.night.time_in_bed_min;
    const d = {
      j, bedStart, inBed, wake: bedStart + inBed, bedDate: new Date(j.night.bed), wakeDate: new Date(j.night.wake), lastSync: new Date(j.last_sync),
      // stages from tools/analysis (5-min Oura-app-style view; t0 = floor30(bed)); old exports had series.stages
      ep: (j.stages && j.stages.epochs5 && j.stages.epochs5.length) ? j.stages.epochs5 : (s.stages || []),
      stBed: j.stages && j.stages.t0 ? isoMin(j.stages.t0) : bedStart,
      stLabel: (j.stages && j.stages.label) || e.label || 'estimated',
      pct: e.pct_of_sleep || (j.stages && j.stages.pct_of_sleep) || null,
      sleeps: (j.nightly || []).filter(x => (x.kind || 'night') === 'night'),
      note: j.sleep_note || null, aiNote: j.ai_note || null, base: j.baselines || null,
      hr: s.hr, hrv: s.hrv || s.rmssd, temp: s.temp_c, motion: s.motion,
      total: e.asleep_min, awake: e.minutes.awake, rem: e.minutes.rem, light: e.minutes.light, deep: e.minutes.deep,
      eff: e.efficiency_pct, latency: e.latency_min,
    };
    d.sleepScore = r0(clamp(sum(sleepParts(d)), 0, 100));
    return d;
  }
  const demoBadge = what => `<div class="card mode-note demo"><b>DEMO</b> ${what || 'This screen still shows generated demo data.'}</div>`;
  const nNights = n => `${n} night${n === 1 ? '' : 's'}`;
  const needsNights = (what, n) => `<div class="card mode-note"><b>Needs more nights</b><p>${/[.!?]$/.test(what) ? what : what + ' needs more data.'} Only <b>${nNights(REAL.j.nights)}</b> of real ring data so far; this needs about ${n}.</p></div>`;
  const esc = t => String(t).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  function noteCard(R) {
    // local-AI note (Ollama on this PC) when exported for this night; template note is the fallback
    const ai = R.aiNote && R.aiNote.text ? R.aiNote : null, N = ai || R.note;
    if (!N || !N.text) return '';
    const lines = N.lines || N.text.split('\n');
    const tag = (ai ? 'local AI · ' : '') + (N.generated ? 'generated ' + fmtClock(isoMin(N.generated)) : 'auto');
    return `<div class="card note-card${ai ? ' ai' : ''}" id="sleepNote" data-goto="sleep"${ai ? ` title="Written by ${esc(ai.model || 'a local model')} on this PC; nothing left the house"` : ''}>
      <h3>Morning note <span class="tag">${esc(tag)}</span></h3>
      ${lines.map((l, i) => `<p class="note-line${i === lines.length - 1 ? ' muted small' : ''}">${esc(l)}</p>`).join('')}
    </div>`;
  }
  const baseNeed = R => (R.base && R.base.status === 'insufficient' ? R.base.need_more : 0);
  const syncAgo = t => { const m = Math.round((Date.now() - t) / 60000); return m < 1 ? 'just now' : m < 60 ? `${m} min ago` : m < 1440 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} d ago`; };
  const xlReal = R => { const n = R.ep.length; return [[0, fmtClock(R.bedStart).replace(' ', '')], [Math.floor(n / 2), fmtClock(R.bedStart + n * 2.5).replace(' ', '')], [n - 1, fmtClock(R.wake).replace(' ', '')]]; };

  // ---------- Readiness (REAL): j.readiness from tools/analysis/readiness.py (our formula, not Oura's) ----------
  const RD = R => (R.j.readiness && R.j.readiness.contributors ? R.j.readiness : null);
  function readinessRing(R) {
    const rd = RD(R);
    if (!rd || rd.score == null) return ring(0, 'var(--rec)', 'Readiness', 'no data', '—');
    return ring(rd.score, 'var(--rec)', 'Readiness', rd.provisional ? 'calibrating · provisional' : (rd.band || 'ours'), rd.provisional ? `${rd.score}*` : rd.score);
  }
  function readinessNote(R) {
    const rd = RD(R);
    if (!rd || rd.score == null) return 'Readiness: no data for last night.';
    return rd.provisional
      ? `Readiness* is <b>provisional</b>: calibrating with rules of thumb until your own baseline exists (${rd.need_more} more night${rd.need_more === 1 ? '' : 's'}). Our score, not Oura's. Awareness only, not a fitness-for-duty call.`
      : `Readiness: our open formula vs your own baseline, not Oura's. Awareness only, not a fitness-for-duty call.`;
  }
  function readinessDetails(R) {
    const rd = RD(R);
    if (!rd || rd.score == null) return '';
    const li = c => c.available
      ? `<li id="rd-${c.key}"><span>${esc(c.name)}<br><small class="faint">${esc(c.detail)}${c.basis === 'absolute' && (c.key === 'rhr' || c.key === 'hrv') ? ' · provisional' : ''}</small></span><span class="num">${c.score}<small class="faint"> × ${c.share_pct}%</small></span></li>`
      : `<li id="rd-${c.key}" class="faint"><span>${esc(c.name)}<br><small>${esc(c.detail)}</small></span><span>—</span></li>`;
    return `<details id="readinessDetails"><summary>What went into readiness${rd.provisional ? ' (provisional)' : ''}?</summary>
      <ul class="factors rd-factors">${rd.contributors.map(li).join('')}</ul>
      <p class="muted small">${esc(rd.note || '')}</p>
    </details>`;
  }
  const battTxt = j => (j.device && j.device.battery && j.device.battery.pct != null
    ? `Ring <span class="batt-ico"><i style="width:${j.device.battery.pct}%"></i></span>${j.device.battery.pct}%` : 'Ring · battery unknown');

  // ---------- Day / evening (REAL): j.day from tools/analysis/day.py (steps are ESTIMATED from MET) ----------
  const DAYD = R => (R.j.day && !R.j.day.error ? R.j.day : null);
  const dayPick = R => { const D = DAYD(R); return D ? (D.today || D.latest || null) : null; };
  const isoClock = s => (s ? fmtClock(isoMin(s)) : '—');
  const kStep = v => (v >= 10000 ? r1(v / 1000) + 'k' : fmtInt(v));
  function stepsRing(R) {
    const d = dayPick(R), D = DAYD(R);
    if (!d || d.steps.value == null) return ring(0, 'var(--act)', 'Steps', 'no day data yet', '—');
    const today = d.date === D.today_date;
    return ring(Math.min(100, d.steps.value / STEP_GOAL * 100), 'var(--act)', 'Steps (est.)', today ? 'today so far' : fmtDate(new Date(d.date + 'T12:00'), { month: 'short', day: 'numeric' }), kStep(d.steps.value));
  }
  function eveningCard(R) {
    const D = DAYD(R), e = D && D.evening;
    if (!e) return '';
    const hv = (e.hr_15min || []).map(x => x[1]);
    const trend = e.hr_trend ? { settling: 'settling down', rising: 'rising', steady: 'steady' }[e.hr_trend] : 'not enough data';
    const hrTxt = e.hr_first_hour != null && e.hr_last_hour != null ? `${r0(e.hr_first_hour)} → ${r0(e.hr_last_hour)} bpm` : e.hr_last_hour != null ? `${r0(e.hr_last_hour)} bpm last hour` : '—';
    const rmTxt = e.rmssd_first_hour != null && e.rmssd_last_hour != null ? `${r0(e.rmssd_first_hour)} → ${r0(e.rmssd_last_hour)} ms` : e.rmssd_last_hour != null ? `${r0(e.rmssd_last_hour)} ms last hour` : '—';
    return `<div class="card" id="eveningCard">
      <h3>Evening wind-down <span class="tag">3 h before bed at ${isoClock(e.bed)}${e.partial ? ' · partial data' : ''}</span></h3>
      <div class="stats">
        <div class="stat"><div class="k">Heart rate</div><div class="v" style="font-size:17px">${hrTxt}</div><div class="d neutral">${esc(trend)}${e.hr_slope_bpm_per_h != null ? ` · ${e.hr_slope_bpm_per_h > 0 ? '+' : ''}${r1(e.hr_slope_bpm_per_h)} bpm/h` : ''}</div>${spark(hv, 'var(--bad)')}</div>
        <div class="stat"><div class="k">Last activity</div><div class="v" style="font-size:17px">${isoClock(e.last_activity)}</div><div class="d neutral">${e.last_activity_min_before_bed != null ? `${fmtDur(e.last_activity_min_before_bed).replace(/^0h /, '')} before bed` : 'none in the 6 h before bed'} · MET ≥ 2.5</div></div>
        <div class="stat"><div class="k">HRV (RMSSD)</div><div class="v" style="font-size:17px">${rmTxt}</div><div class="d neutral">first → last hour${e.rmssd_source ? ' · ' + esc(e.rmssd_source) : ''}</div></div>
        <div class="stat"><div class="k">Evening movement</div><div class="v" style="font-size:17px">${e.mean_met != null ? r1(e.mean_met) + '<small>MET avg</small>' : '—'}</div><div class="d neutral">${e.met_recorded_min} min recorded</div></div>
      </div>
      <div class="hint">${e.partial ? 'Ring data covers only part of the 3 h window (it was put on / synced late). ' : ''}Estimated from ring HR, HRV and MET; our rules, not Oura.</div>
    </div>`;
  }
  function renderActivityReal() {
    const R = REAL, D = DAYD(R), el = $('#screen-activity');
    if (!D) { el.innerHTML = `<div class="card mode-note"><b>No day data</b><p>${esc((R.j.day && R.j.day.error) || 'Run tools/export_dashboard.py to compute daytime activity.')}</p></div>`; return; }
    const d = dayPick(R);
    if (!d) { el.innerHTML = `<div class="card mode-note" id="actNoData"><b>No daytime data yet</b><p>The ring has only synced sleep so far. Wear it through the day and sync; steps (estimated), active minutes and daytime heart rate show up here.</p></div>`; return; }
    const today = d.date === D.today_date, st = d.steps, H = d.hourly, hh = new Date().getHours();
    const prog = Math.min(100, (st.value || 0) / STEP_GOAL * 100);
    const hrs = H.steps.map((_, i) => (i % 6 === 0 ? shortClock(i * 60) : ''));
    const sess = (d.exercise_sessions || []).map(x => ({ ...x, kind: 'Exercise HR' })).concat((d.activity_bouts || []).map(x => ({ ...x, kind: 'Activity (MET)' }))).sort((a, b) => a.start_unix - b.start_unix);
    const ds = (D.days || []).filter(x => x.steps && x.steps.value != null);
    const im = d.intensity_min || {};
    const src = D.sources || {}, rs = src.real_steps_0x7e_0x7f || {}, eh = src.ehr_trace_0x73 || {};
    el.innerHTML = `
      ${today ? '' : `<div class="card mode-note" id="actStale"><b>No data for today yet</b><p>Showing ${fmtDate(new Date(d.date + 'T12:00'), { weekday: 'long', month: 'short', day: 'numeric' })}, the latest day with awake ring data. Sync to update.</p></div>`}
      <div class="card" id="actToday">
        <h3>${today ? 'Today' : 'Latest day'} <span class="tag est-tag">steps estimated from MET</span></h3>
        <div class="row"><div><div class="big num" id="actSteps">${st.value == null ? '—' : fmtInt(st.value)}</div><div class="muted small">steps (est.) of ${fmtInt(STEP_GOAL)} goal</div></div>
        <div style="width:84px">${ring(prog, 'var(--act)', '', '', r0(prog) + '%')}</div></div>
        <div class="progress"><i style="width:${prog}%"></i></div>
        <div class="stats four" style="margin-top:12px">
          <div class="stat"><div class="k">Active</div><div class="v">${d.active_min}<small>min</small></div></div>
          <div class="stat"><div class="k">Active cal</div><div class="v">${d.active_kcal != null ? fmtInt(d.active_kcal) : '—'}</div></div>
          <div class="stat"><div class="k">Distance</div><div class="v">${d.distance_km_est != null ? r1(d.distance_km_est * 0.621) : '—'}<small>mi</small></div></div>
          <div class="stat"><div class="k">Worn</div><div class="v">${fmtDur(d.wear_awake_min).replace(/^0h /, '')}</div></div>
        </div>
        <div class="hint">Based on ${fmtInt(d.recorded_min)} awake minutes of MET data (${fmtInt(d.sleep_min_excluded)} min of sleep excluded).${d.active_kcal == null ? ' Calories need your weight: ' + esc(d.weight_source || '') + '.' : ''}${st.ring_counter != null ? ` Ring activity counter: ${fmtInt(st.ring_counter)} (possibly steps, unverified).` : ''}${st.real_steps_present ? ` REAL_STEPS: ${st.gait_windows} gait windows ≈ ${r0(st.gait_walking_min)} min walking.` : ''}</div>
      </div>
      <div class="card" id="actHourly">
        <h3>By hour <span class="tag">steps (est.) · MET</span></h3>
        ${barChart({ label: 'Hourly steps (estimated)', h: 120, v: H.steps, labels: hrs, color: 'var(--act)', hl: today ? hh : -1, yFmt: v => kStep(v), readout: i => `${shortClock(i * 60)}–${shortClock(i * 60 + 60)} · ~${fmtInt(H.steps[i])} steps · ${H.met[i] == null ? 'no data' : r1(H.met[i]) + ' MET avg'} · ${H.recorded_min[i]} min${H.hr[i] != null ? ` · HR ${r0(H.hr[i])}` : ''}`, readoutDefault: 'Tap a bar · steps estimated from MET' })}
        ${H.met.some(v => v != null) ? lineChart({ label: 'Hourly MET', h: 110, nx: 24, min: 0.6, series: [{ v: H.met, color: 'var(--act)', fill: true, w: 1.8 }], dots: true, endDot: false, yFmt: v => r1(v), xLabels: [[0, '12a'], [6, '6a'], [12, '12p'], [18, '6p'], [23, '11p']], readout: i => `${shortClock(i * 60)} · ${H.met[i] == null ? 'no data' : r1(H.met[i]) + ' MET'}`, readoutDefault: `mean ${r1(d.mean_met)} MET · walking ≈ 3–4 MET` }) : ''}
        <div class="stats four" style="margin-top:10px">
          <div class="stat"><div class="k">Sedentary</div><div class="v">${im.sedentary || 0}<small>m</small></div></div>
          <div class="stat"><div class="k">Low</div><div class="v">${im.low || 0}<small>m</small></div></div>
          <div class="stat"><div class="k">Medium</div><div class="v">${im.medium || 0}<small>m</small></div></div>
          <div class="stat"><div class="k">High</div><div class="v">${im.high || 0}<small>m</small></div></div>
        </div>
        <div class="hint">Sedentary &lt; 1.5 MET · low 1.5–3 · medium 3–6 · high ≥ 6. Active minutes = medium + high.</div>
      </div>
      <div class="card" id="actSessions">
        <h3>Exercise sessions <span class="tag">${sess.length || 'none'}</span></h3>
        ${sess.length ? `<ul class="factors">${sess.map(x => `<li><span>${isoClock(x.start)}–${isoClock(x.end)} · ${esc(x.kind)}<br><small class="faint">${x.duration_min} min${x.met_avg != null ? ` · ${r1(x.met_avg)} MET avg · ~${fmtInt(x.steps_est)} steps` : ''}${x.hr_avg != null ? ` · HR ${r0(x.hr_avg)} avg, ${r0(x.hr_max)} max` : ''}</small></span><span></span></li>`).join('')}</ul>`
          : '<p class="muted small">No sessions on this day.</p>'}
        <div class="hint">Exercise HR (0x73) ${eh.present ? 'is recording' : 'has not produced data yet (just enabled)'}; its payload isn't decoded upstream, so sessions use timing + beat HR. Activity bouts = ≥ 5 min at ≥ 3 MET (estimated).</div>
      </div>
      <div class="card" id="actHR">
        <h3>Daytime heart rate <span class="tag">outside sleep</span></h3>
        ${d.hr.avg != null ? `<div class="stats four"><div class="stat"><div class="k">Min</div><div class="v">${r0(d.hr.min)}</div></div><div class="stat"><div class="k">Avg</div><div class="v">${r0(d.hr.avg)}</div></div><div class="stat"><div class="k">Max</div><div class="v">${r0(d.hr.max)}</div></div><div class="stat"><div class="k">5-min</div><div class="v">${d.hr.samples_5min}</div></div></div>
          ${H.hr.filter(v => v != null).length > 1 ? lineChart({ label: 'Hourly daytime HR', h: 110, nx: 24, series: [{ v: H.hr, color: 'var(--bad)', fill: false }], dots: true, endDot: false, xLabels: [[0, '12a'], [6, '6a'], [12, '12p'], [18, '6p'], [23, '11p']], readout: i => `${shortClock(i * 60)} · ${H.hr[i] == null ? 'no data' : r0(H.hr[i]) + ' bpm'}`, readoutDefault: 'hourly median, bpm' }) : ''}`
          : '<p class="muted small">No daytime heart-rate data on this day.</p>'}
      </div>
      ${ds.length > 1 ? `<div class="card" id="actDays"><h3>Recent days <span class="tag">steps (est.)</span></h3>
        ${barChart({ label: 'Daily steps (estimated)', v: ds.map(x => x.steps.value), labels: ds.map(x => fmtDate(new Date(x.date + 'T12:00'), { weekday: 'narrow' })), color: 'var(--act)', goal: STEP_GOAL, yFmt: v => r1(v / 1000) + 'k', readout: i => `${fmtDate(new Date(ds[i].date + 'T12:00'))} · ~${fmtInt(ds[i].steps.value)} steps · ${ds[i].active_min} active min · ${fmtInt(ds[i].recorded_min)} min recorded`, readoutDefault: 'Partial days (ring off / not synced) count low' })}</div>` : ''}
      <div class="card mode-note"><b>How steps are estimated</b><p>The ring's REAL_STEPS stream (${rs.present ? 'present' : 'not recorded yet'}) carries gait features, not counts; turning them into steps needs Oura's proprietary model. So steps here are estimated from MET minutes (105/min at 2.5–7 MET, 150/min above). Not medical advice.</p></div>`;
  }

  function renderTodayReal() {
    const R = REAL, S = R.j.summary, h = NOW.getHours(), greet = h < 12 ? 'Good morning' : h < 17 ? 'Good afternoon' : 'Good evening';
    const sp = sleepParts(R), tF = c => r1(c * 1.8 + 32);
    const f = (k, v, max) => `<li><span>${k}</span><span class="num">${r0(v)} / ${max}</span></li>`;
    $('#screen-today').innerHTML = `
      <div class="greet"><h2>${greet}</h2><div class="muted small">${fmtDate(NOW, { weekday: 'long', month: 'long', day: 'numeric' })} · real ring data, ${nNights(R.j.nights)}</div></div>
      ${noteCard(R)}
      <div class="card">
        <div class="rings">
          ${ring(R.sleepScore, 'var(--sleep)', 'Sleep', 'estimated')}
          ${readinessRing(R)}
          ${stepsRing(R)}
        </div>
        <div class="ours">Sleep score: our open formula on <b>estimated</b> stages (not Oura). ${readinessNote(R)}</div>
        <details><summary>What went into the sleep score?</summary>
          <ul class="factors">
            ${f('Duration (vs 8h)', sp.duration, 40)}${f('Efficiency (est.)', sp.efficiency, 25)}${f('Deep + REM share (est.)', sp.restorative, 20)}${f('Fell asleep quickly (est.)', sp.latency, 15)}
          </ul>
        </details>
        ${readinessDetails(R)}
      </div>
      <div class="card ai-card" data-goto="sleep">
        <div class="spark">${I.moon}</div>
        <div><div class="who">Last night, measured</div><p>In bed <b>${fmtClock(R.bedStart)} – ${fmtClock(R.wake)}</b> (${fmtDur(R.inBed)}). Sleeping heart rate settled around <b>${r0(S.hr_median)} bpm</b>, lowest <b>${r0(S.hr_low)} bpm</b> at ${fmtClock(isoMin(S.hr_low_at))}. Average HRV (RMSSD) <b>${r0(S.rmssd_avg)} ms</b>. Tap for the full night.</p></div>
      </div>
      ${eveningCard(R)}
      <div class="stats">
        <div class="stat"><div class="k">Sleeping HR</div><div class="v">${r0(S.hr_median)}<small>bpm</small></div><div class="d neutral">median · low ${r0(S.hr_low)}</div>${spark(R.hr, 'var(--bad)')}</div>
        <div class="stat"><div class="k">HRV (RMSSD)</div><div class="v">${r0(S.rmssd_avg)}<small>ms</small></div><div class="d neutral">night avg · ${baseNeed(R) ? `baseline in ${baseNeed(R)} nights` : 'vs baseline: ' + esc((R.base && R.base.metrics.rmssd && R.base.metrics.rmssd.status) || '—')}</div>${spark(R.hrv, 'var(--rec)')}</div>
        <div class="stat"><div class="k">Skin temp</div><div class="v">${tF(S.temp_avg_c)}<small>°F</small></div><div class="d neutral">${r1(S.temp_avg_c)} °C avg · deviation needs baseline</div>${spark(R.temp, 'var(--act)')}</div>
        ${R.j.spo2.median != null
          ? `<div class="stat"><div class="k">SpO₂ (est.)</div><div class="v">${r0(R.j.spo2.median)}<small>%</small></div><div class="d neutral">median · low ${r0(R.j.spo2.low)} · ${r1(R.j.spo2.pct_below_90)}% of night &lt;90</div>${spark((R.j.series.spo2 || []).filter(v => v != null), 'var(--sleep)')}</div>`
          : `<div class="stat"><div class="k">SpO₂</div><div class="v" style="font-size:17px">no data</div><div class="d neutral">${fmtInt(R.j.spo2.r_samples)} raw R samples captured</div></div>`}
      </div>
      <div class="card syncbar" data-open-device>
        <span><span class="pulse"></span>Synced ${syncAgo(R.lastSync)}</span>
        <span class="batt">${battTxt(R.j)}</span>
      </div>`;
  }

  function renderSleepReal() {
    const R = REAL, S = R.j.summary, tot = R.total;
    const st = [['Awake', R.awake, 'var(--awake)'], ['REM', R.rem, 'var(--rem)'], ['Light', R.light, 'var(--light)'], ['Deep', R.deep, 'var(--deep)']];
    const xl = xlReal(R), at = k => fmtClock(R.bedStart + k * 5 + 2.5);
    const tempF = R.temp.map(c => (c == null ? null : c * 1.8 + 32));
    $('#screen-sleep').innerHTML = `
      <div class="card">
        <h3>Last night <span class="tag">${fmtDate(R.wakeDate, { weekday: 'short', month: 'short', day: 'numeric' })} · real</span></h3>
        <div class="row"><div><div class="big num">${fmtDur(R.inBed)}</div><div class="muted small">in bed (measured) · ${fmtClock(R.bedStart)} – ${fmtClock(R.wake)}</div><div class="muted small">~${fmtDur(tot)} asleep (estimated)</div></div>
        <div style="width:84px">${ring(R.sleepScore, 'var(--sleep)', '', '')}</div></div>
        <h3 style="margin-top:10px">Sleep stages <span class="tag est-tag">${esc(R.stLabel)}</span></h3>
        ${hypnogram({ ep: R.ep, bedStart: R.stBed, wake: R.stBed + R.ep.length * 5, hr: R.ep.map((_, k) => R.hr[Math.round(k + (R.stBed - R.bedStart) / 5)]), note: 'estimated, 5-min view' })}
        <div class="stage-grid">${st.map(([k, v, c]) => `<div class="stage" style="border-color:${c}"><div class="k">${k}</div><div class="v">${fmtDur(v).replace('0h ', '')}</div><div class="p">${k === 'Awake' ? r0(v / R.inBed * 100) + '% of bed' : r0(R.pct && R.pct[k.toLowerCase()] != null ? R.pct[k.toLowerCase()] : v / tot * 100) + '% of sleep'}</div></div>`).join('')}</div>
        <div class="hint">Stages are estimated from movement, heart rate, HRV and skin temperature with our own transparent rules (tools/analysis, not Oura's model)${R.j.stages && R.j.stages.checks_total ? ` · ${R.j.stages.checks_passed}/${R.j.stages.checks_total} sanity checks passed` : ''}. Not validated &mdash; treat as approximate.</div>
      </div>
      <div class="stats four">
        <div class="stat"><div class="k">In bed</div><div class="v">${fmtDur(R.inBed).replace('m', '')}</div><div class="d neutral">measured</div></div>
        <div class="stat"><div class="k">Efficiency</div><div class="v">${r0(R.eff)}<small>%</small></div><div class="d neutral">est.</div></div>
        <div class="stat"><div class="k">Latency</div><div class="v">${r0(R.latency)}<small>min</small></div><div class="d neutral">est.</div></div>
        <div class="stat"><div class="k">Asleep</div><div class="v">${fmtDur(tot).replace('m', '')}</div><div class="d neutral">est.</div></div>
      </div>
      <div class="card">
        <h3>Heart rate overnight <span class="tag">lowest ${r0(S.hr_low)} bpm at ${fmtClock(isoMin(S.hr_low_at))}</span></h3>
        ${lineChart({ label: 'Overnight heart rate', h: 120, series: [{ v: bridge(R.hr), color: 'var(--bad)', fill: true }], xLabels: xl, endDot: false, readout: k => `${at(k)} · ${R.hr[k] == null ? 'no reading' : r0(R.hr[k]) + ' bpm'}`, readoutDefault: `median ${r0(S.hr_median)} bpm · 5-min medians of beat data` })}
        <h3 style="margin-top:14px">HRV overnight <span class="tag">avg ${r0(S.rmssd_avg)} ms</span></h3>
        ${lineChart({ label: 'Overnight HRV', h: 120, series: [{ v: bridge(R.hrv), color: 'var(--rec)', fill: true }], xLabels: xl, endDot: false, readout: k => `${at(k)} · ${R.hrv[k] == null ? 'no reading' : r0(R.hrv[k]) + ' ms RMSSD'}`, readoutDefault: `peak ${r0(S.rmssd_max)} ms · ring's own 5-min RMSSD` })}
      </div>
      <div class="card">
        <h3>Skin temperature <span class="tag">avg ${r1(S.temp_avg_c * 1.8 + 32)} °F</span></h3>
        ${lineChart({ label: 'Overnight skin temperature', h: 120, series: [{ v: bridge(tempF), color: 'var(--act)', fill: false }], yFmt: v => r1(v), xLabels: xl, endDot: false, readout: k => `${at(k)} · ${R.temp[k] == null ? 'no reading' : `${r1(tempF[k])} °F (${r1(R.temp[k])} °C)`}`, readoutDefault: `${r1(S.temp_avg_c)} °C avg · absolute (no baseline yet)` })}
        <h3 style="margin-top:14px">Movement <span class="tag">restlessness 0–100</span></h3>
        ${lineChart({ label: 'Overnight movement', h: 100, min: 0, max: 100, series: [{ v: R.motion, color: 'var(--sleep)', fill: true, w: 1.6 }], xLabels: xl, endDot: false, readout: k => `${at(k)} · ${r0(R.motion[k])} · ${R.j.series.motion_seconds[k]} s moving`, readoutDefault: 'From accelerometer (ACM) + motion events' })}
      </div>
      ${nightList(R)}`;
  }
  function nightList(R) {
    const ns = (R.j.nightly || []).slice().reverse();
    if (!ns.length) return needsNights('The 14-day trend', 7);
    return `<div class="card"><h3>Nights on file <span class="tag">${nNights(R.sleeps.length)}${ns.length > R.sleeps.length ? ` + ${ns.length - R.sleeps.length} nap(s)` : ''}</span></h3>
      <ul class="factors night-list">${ns.map(x => `<li><span>${fmtDate(new Date(x.wake), { weekday: 'short', month: 'short', day: 'numeric' })}${x.kind === 'nap' ? ' · nap' : ''}<br><span class="muted small">${fmtClock(isoMin(x.bed))}–${fmtClock(isoMin(x.wake))} · ${fmtDur(x.time_in_bed_min)} in bed${x.sleep_min_est != null ? ` · ~${fmtDur(x.sleep_min_est)} asleep` : ''}</span></span>
        <span class="num small" style="text-align:right">RHR ${x.hr_low == null ? '—' : r0(x.hr_low)} · HRV ${x.rmssd_avg == null ? '—' : r0(x.rmssd_avg)}<br><span class="muted">${x.temp_avg_c == null ? '' : r1(x.temp_avg_c) + ' °C'}${x.stage_pct ? ` · deep ${r0(x.stage_pct.deep)}% REM ${r0(x.stage_pct.rem)}%` : ''}</span></span></li>`).join('')}</ul>
      ${R.sleeps.length < 7 ? '<div class="hint">A 14-day sleep trend chart appears once about a week of nights is on file.</div>' : ''}</div>`;
  }
  function wrapDemo(fn, note) { return () => { fn(); if (isReal()) { const el = $('#screen-' + state.tab); el.insertAdjacentHTML('afterbegin', demoBadge(note)); } }; }
  function realTrendCard(key, field, title, unit, color, fmtV) {
    const R = REAL, ns = R.sleeps, hist = (R.base && R.base.history) || [];
    const byDate = {}; hist.forEach(h => { byDate[h.date] = h[key]; });
    const v = ns.map(x => x[field]);
    const hb = ns.map(x => byDate[x.date] || {});
    const band = { lo: hb.map(b => (b.low14 == null ? null : b.low14)), hi: hb.map(b => (b.high14 == null ? null : b.high14)), mid: hb.map(b => (b.median14 == null ? null : b.median14)) };
    const m = (R.base && R.base.metrics && R.base.metrics[key]) || {};
    const n = ns.length, lab = ns.map(x => fmtDate(new Date(x.wake), { month: 'short', day: 'numeric' }));
    const lv = v[n - 1], bm = band.mid[n - 1];
    const tag = m.status === 'insufficient' ? `flags after ${m.need_more} more nights` : m.status === 'high' ? 'above your usual' : m.status === 'low' ? 'below your usual' : m.status === 'ok' ? 'within your usual range' : 'no value';
    return `<div class="card">
      <h3>${title} <span class="tag">${tag}</span></h3>
      <div class="row" style="margin-bottom:6px"><div><span class="big num" style="font-size:26px">${lv == null ? '—' : fmtV(lv)}</span> <span class="muted small">${unit} last night</span></div>
      <div class="small muted" style="text-align:right">baseline (median)<br><b class="num">${bm == null ? '—' : fmtV(bm) + ' ' + unit}</b></div></div>
      ${lineChart({ label: title, series: [{ v, color, fill: false }], band, dots: true, xLabels: [[0, lab[0]], [n - 1, lab[n - 1]]], yFmt: fmtV, readout: i => `${lab[i]} · ${v[i] == null ? 'no value' : fmtV(v[i]) + ' ' + unit}${band.lo[i] == null ? ' · no baseline yet' : ` · usual ${fmtV(band.lo[i])}–${fmtV(band.hi[i])}`}`, readoutDefault: 'Shaded = your usual range (median ± 2 MAD of up to 14 earlier nights)' })}
    </div>`;
  }
  function renderTrendsReal() {
    const R = REAL, n = R.sleeps.length, b = R.base || {};
    const status = b.status === 'insufficient'
      ? `<div class="card mode-note"><b>Still learning your normal</b><p>${nNights(n)} on file. High/low flags start after ${b.min_nights || 5} nights &mdash; <b>need ${b.need_more} more night${b.need_more === 1 ? '' : 's'}</b>.</p></div>` : '';
    if (n < 2) {
      $('#screen-trends').innerHTML = `${status}<div class="card mode-note" id="trendsNeedMore"><b>Need more nights</b><p>Trends plot each night's resting HR, HRV and skin temperature against your own baseline. Only <b>${nNights(n)}</b> of real ring data so far; the charts appear from 2 nights.</p></div>`;
      return;
    }
    $('#screen-trends').innerHTML = `
      <div class="row" style="margin-top:12px"><div class="muted small">Per night vs your own baseline · real ring data</div></div>
      ${status}
      ${realTrendCard('rhr', 'hr_low', 'Resting heart rate', 'bpm', 'var(--bad)', v => r0(v))}
      ${realTrendCard('rmssd', 'rmssd_avg', 'HRV (RMSSD)', 'ms', 'var(--rec)', v => r0(v))}
      ${realTrendCard('temp_dev', 'temp_avg_c', 'Skin temperature', '°C', 'var(--act)', v => r1(v))}
      <div class="hint">Resting HR = lowest 5-min HR while asleep. Temperature is the night's average skin temp; deviation from your usual is used for flags.</div>`;
  }

  // ---------- wiring ----------
  // ---------- Cycle (REAL): j.cycle from tools/analysis/cycle.py (awareness only) ----------
  const pDay = s => new Date(s + 'T12:00:00'); // local noon: avoids the UTC date shift
  const PHR = { menstrual: 'menstrual', follicular: 'follicular', fertile: 'ovulation', luteal: 'luteal', late: 'luteal' };
  function cycleNotice(c) {
    return `<div class="card notice" role="note" id="cycDisclaimer">
      <div class="notice-ico">${I.info}</div>
      <div>
        <div class="notice-title">Not birth control</div>
        <p>${esc(c.disclaimer || 'Awareness only - not birth control.')}</p>
        <p>Want temperature-based birth control? Use an FDA-cleared method (e.g. Natural Cycles).</p>
      </div>
    </div>`;
  }
  function realCycleChart(c) {
    const ts = c.temps || [];
    if (!ts.length) return '<div class="muted small">No nightly skin temperature yet &mdash; wear the ring to sleep and sync in the morning.</div>';
    const id = 'c' + (++seq), h = 180, pL = 34, pR = 8, pT = 20, pB = 20, n = ts.length, bw = (W - pL - pR) / n;
    const devs = ts.map(t => t.dev_c).filter(v => v != null);
    const mn = Math.min(-0.35, ...devs.map(v => v - 0.05)), mx = Math.max(0.5, ...devs.map(v => v + 0.05));
    const X = i => pL + bw * (i + 0.5), Y = v => pT + (h - pT - pB) * (1 - (clamp(v, mn, mx) - mn) / (mx - mn));
    const idx = {}; ts.forEach((t, i) => { idx[t.date] = i; });
    let s = `<svg viewBox="0 0 ${W} ${h}" role="img" aria-label="Nightly skin temperature deviation">`;
    for (let i = 0; i < n;) {
      if (!ts[i].period) { i++; continue; }
      let j = i; while (j < n && ts[j].period) j++;
      s += `<rect x="${(pL + bw * i).toFixed(1)}" y="${pT}" width="${(bw * (j - i)).toFixed(1)}" height="${h - pT - pB}" fill="var(--cyc)" opacity=".17" rx="2"/>`;
      i = j;
    }
    [-0.2, 0, 0.2, 0.4].forEach(v => { s += `<line class="gridl" x1="${pL}" x2="${W - pR}" y1="${Y(v)}" y2="${Y(v)}"${v === 0 ? ' stroke-dasharray="2 3" style="stroke:var(--faint)"' : ''}/><text x="${pL - 6}" y="${Y(v) + 3}" text-anchor="end">${v === 0 ? '0' : signed(v, 1)}°</text>`; });
    let labeled = false;
    (c.cycles || []).forEach(cy => {
      const o = cy.est_ovulation && idx[cy.est_ovulation.date];
      if (o == null) return;
      s += `<line x1="${X(o)}" x2="${X(o)}" y1="${pT - 6}" y2="${h - pB}" stroke="var(--fert)" stroke-width="1.2" opacity=".75"/><circle cx="${X(o)}" cy="${pT - 8}" r="3.5" fill="var(--fert)"/>`;
      if (!labeled) { s += `<text x="${X(o) - 6}" y="${pT - 5}" text-anchor="end" style="fill:var(--fert)">est. ovulation</text>`; labeled = true; }
    });
    let d = '', on = false;
    ts.forEach((t, i) => { if (t.excluded || t.dev_c == null) { on = false; return; } d += (on ? 'L' : 'M') + X(i).toFixed(1) + ',' + Y(t.dev_c).toFixed(1); on = true; });
    s += `<path d="${d}" fill="none" stroke="var(--act)" stroke-width="2" stroke-linejoin="round"/>`;
    ts.forEach((t, i) => {
      if (t.dev_c == null) return;
      s += t.excluded ? `<circle cx="${X(i)}" cy="${Y(t.dev_c)}" r="3" fill="var(--card)" stroke="var(--muted)" stroke-width="1.3"/>` : `<circle cx="${X(i)}" cy="${Y(t.dev_c)}" r="${n > 40 ? 1.8 : 2.8}" fill="var(--act)"/>`;
    });
    const lab = i => fmtDate(pDay(ts[i].date), { month: 'short', day: 'numeric' });
    (n === 1 ? [[0, lab(0)]] : [[0, lab(0)], [n - 1, lab(n - 1)]]).forEach(([i, t]) => { s += `<text x="${i === 0 ? pL : X(i)}" y="${h - 5}" text-anchor="${i === 0 ? 'start' : 'end'}">${t}</text>`; });
    s += `<line class="cursor" y1="${pT}" y2="${h - pB}" x1="-10" x2="-10" visibility="hidden"/><circle class="cur-dot" r="5" fill="var(--act)" visibility="hidden"/></svg>`;
    const def = `${nNights(devs.length)} with skin temp · deviation vs your rolling median`;
    charts[id] = {
      xs: ts.map((_, i) => X(i)), ys: ts.map(t => (t.dev_c == null || t.excluded ? null : Y(t.dev_c))), def,
      readout: i => {
        const t = ts[i], tags = [];
        if (t.cd) tags.push('day ' + t.cd);
        if (t.period) tags.push('<span style="color:var(--cyc)">period</span>');
        if ((c.cycles || []).some(cy => cy.est_ovulation && cy.est_ovulation.date === t.date)) tags.push('<span style="color:var(--fert)">est. ovulation</span>');
        if (t.excluded) tags.push('skipped: ' + esc(t.excluded));
        if (t.baseline === 'provisional') tags.push('provisional baseline');
        return `${fmtDate(pDay(t.date))} · ${t.dev_c == null ? 'no value' : `<b>${signed(t.dev_c, 2)} °C</b> (${signed(cToF(t.dev_c), 1)} °F)`}${t.temp_c != null ? ` · ${t.temp_c.toFixed(2)} °C` : ''}${tags.length ? ' · ' + tags.join(' · ') : ''}`;
      },
    };
    return frame(id, s, def);
  }
  const cycTempCard = c => `<div class="card" id="cycTemp">
      <h3>Skin temperature <span class="tag">real · nightly</span></h3>
      ${realCycleChart(c)}
      <div class="legend"><span><i style="background:var(--act)"></i>Nightly deviation</span><span><i style="background:var(--cyc);opacity:.45"></i>Period</span><span><i style="background:var(--fert)"></i>Est. ovulation</span></div>
      <div class="hint">A sustained rise of ~0.2&ndash;0.4 °C after ovulation is typical. Rule: 3 nights above the highest of the previous 6, staying up (plus a smoothed check). Hollow dots were skipped (fever, short night, poor sensor contact).</div>
    </div>`;
  const logHowTo = c => `<div class="card" id="cycLog">
      <h3>Logging <span class="tag">not in the app yet</span></h3>
      <p class="small">Period logging isn’t wired into the app yet. Log the dates on the PC that runs RingKeeper:</p>
      <pre class="small" style="white-space:pre-wrap;margin:6px 0">python3 -I tools/analysis/cycle.py --log-start YYYY-MM-DD [--flow medium]
python3 -I tools/analysis/cycle.py --log-end YYYY-MM-DD</pre>
      <div class="hint">Saved to ${esc(c.log_path || 'data/cycle_log.json')} on your PC only (never committed, never sent to a cloud).</div>
    </div>`;
  const cycPrivacy = `<div class="card privacy">${I.lock}<p><b>Cycle data stays on your phone and your PC.</b> It’s never sent to Oura or any other cloud, and you can delete it any time.</p></div>`;
  function renderCycleReal() {
    const c = REAL.j.cycle, el = $('#screen-cycle');
    if (!c) { el.innerHTML = `<div class="card mode-note"><b>No cycle data in this export</b><p>Re-run tools/export_dashboard.py to add the cycle block.</p></div>`; return; }
    const msgs = (c.messages || []).map(m => `<li>${esc(m)}</li>`).join('');
    if (c.status !== 'ok') {
      const nn = (c.need && c.need.nights) || 0;
      el.innerHTML = `
      <div class="card" id="cycOnboard">
        <h3>Start cycle tracking <span class="tag">${c.status === 'error' ? 'error' : 'setup'}</span></h3>
        ${c.status === 'error' ? `<p class="small">Cycle analysis failed: ${esc(c.error || '')}</p>` : ''}
        <ol class="small" style="padding-left:18px;margin:6px 0">
          <li><b>Log the first day of your next period</b> (day 1 = first day of real bleeding, not spotting). An end date and flow are optional.</li>
          <li><b>Wear the ring to sleep</b> every night you can. Skin temperature is read during sleep${nn ? ` &mdash; about <b>${nn} more night${nn === 1 ? '' : 's'}</b> before a temperature shift can be checked` : ''}.</li>
          <li>After one full cycle (two logged period starts) predictions use <i>your</i> average; until then they assume 28 days (low confidence).</li>
          <li>If you use hormonal birth control, say so: it usually removes the temperature shift.</li>
        </ol>
        ${msgs ? `<ul class="small muted" style="padding-left:18px;margin:6px 0 0">${msgs}</ul>` : ''}
      </div>
      ${cycleNotice(c)}
      ${cycTempCard(c)}
      ${logHowTo(c)}
      ${cycPrivacy}`;
      return;
    }
    const cu = c.current, p = c.prediction, st_ = c.stats || {}, fw = cu.fertile_window;
    const ph = PH[PHR[cu.phase]] || PH.follicular;
    const md = s => fmtDate(pDay(s), { month: 'short', day: 'numeric' });
    const done = (c.cycles || []).filter(x => x.complete).slice(-6).reverse();
    el.innerHTML = `
      <div class="card" id="cycNow">
        <h3>This cycle <span class="tag">started ${md(cu.cycle_start)} · real</span></h3>
        <div class="row" style="align-items:flex-end"><div><div class="muted small">Cycle day</div><div class="big num" style="font-size:40px">${cu.cycle_day}</div></div>
          <div style="text-align:right"><div class="cyc-phase" style="color:${cu.phase === 'late' ? 'var(--cyc)' : ph[1]}">${esc(cu.phase_label)}</div>
          <div class="muted small">Next period ~<b>${md(p.next_period_start)}</b> (±${p.window_days} d)<br>${esc(p.confidence)} confidence · ${esc(p.basis)}</div></div></div>
        <div class="shift-note">${cu.shift_detected
          ? `<b>Temperature shift detected</b> &mdash; ovulation probably around <b>day ${cu.est_ovulation_cd}</b> (${md(cu.est_ovulation_date)}), ${esc(cu.est_ovulation_basis)}.`
          : `No temperature shift detected yet. Ovulation guess: ~day ${cu.est_ovulation_cd} from the ${esc(cu.est_ovulation_basis)}.`}
          <br>Fertile window <i>(estimate)</i>: ${md(fw.start)} &ndash; ${md(fw.end)}. Not a safe/unsafe signal.</div>
        ${msgs ? `<ul class="small muted" style="padding-left:18px;margin:8px 0 0">${msgs}</ul>` : ''}
      </div>
      ${cycleNotice(c)}
      ${cycTempCard(c)}
      <div class="card" id="cycStats">
        <h3>Cycle stats <span class="tag">${st_.complete_cycles || 0} complete cycle${st_.complete_cycles === 1 ? '' : 's'}</span></h3>
        <div class="stats">
          <div class="stat"><div class="k">Avg cycle</div><div class="v">${st_.avg_cycle_len != null ? st_.avg_cycle_len : '—'}<small>d</small></div></div>
          <div class="stat"><div class="k">Avg period</div><div class="v">${st_.avg_period_len != null ? st_.avg_period_len : '—'}<small>d</small></div></div>
          <div class="stat"><div class="k">Range</div><div class="v">${st_.range ? st_.range.join('–') : '—'}<small>d</small></div></div>
          <div class="stat"><div class="k">Luteal phase</div><div class="v">${st_.avg_luteal_len != null ? '~' + r0(st_.avg_luteal_len) : '—'}<small>d</small></div></div>
        </div>
        ${done.length ? `<ul class="cyc-list">${done.map(x => `<li><div class="row"><span><b>${md(x.start)} – ${md(x.end_date)}</b></span><span class="muted small">${x.length} days${x.period_len ? ' · period ' + x.period_len : ''} · ov ${x.est_ovulation ? '~day ' + x.est_ovulation.cd + ' (' + esc(x.est_ovulation.method) + ')' : 'not detected'}</span></div></li>`).join('')}</ul>` : '<div class="hint">Stats fill in once a full cycle is logged.</div>'}
      </div>
      ${logHowTo(c)}
      ${cycPrivacy}`;
  }

  // ---------- live local-AI chat (Real mode): POST /api/ask on the same origin (tools/dash_api.py) ----------
  const CHAT = { log: [], busy: false };
  const MAX_Q = 300;
  const REAL_CHIPS = ['How did I sleep last night?', 'How much deep sleep did I get?', 'What was my resting heart rate?', 'How is my HRV trending?', 'When did I fall asleep?'];
  const chatMsg = m => m.me
    ? `<div class="msg me">${esc(m.text)}</div>`
    : `<div class="msg ai${m.err ? ' err' : ''}"><div class="who">Health AI · local</div>${esc(m.text).replace(/\n/g, '<br>')}${m.cite ? `<div class="cite">${esc(m.cite)}</div>` : ''}</div>`;
  const typingMsg = '<div class="msg ai typing" id="aiTyping" aria-label="Local AI is typing"><div class="who">Health AI · local</div><span class="dots"><i></i><i></i><i></i></span></div>';
  function renderChatReal() {
    const n = REAL.j.nights;
    $('#screen-chat').innerHTML = `
      <div class="chat" id="chatLog">
        <div class="day-sep">Live · answered by a local model on your PC</div>
        <div class="msg ai"><div class="who">Health AI · local</div>Ask me about your ring data (${nNights(n)} so far). I only see derived numbers like sleep time, estimated stages, resting HR, HRV and skin temperature, and nothing leaves the house.<div class="cite">Awareness only, not medical advice.</div></div>
        ${CHAT.log.map(chatMsg).join('')}${CHAT.busy ? typingMsg : ''}
      </div>
      <form class="composer" id="askForm" autocomplete="off">
        <div class="chips">${REAL_CHIPS.map(t => `<button type="button" class="chip" data-ask="${esc(t)}">${esc(t)}</button>`).join('')}</div>
        <div class="box"><input type="text" id="askInput" maxlength="${MAX_Q}" placeholder="Ask about your sleep, HRV, heart rate…" aria-label="Message"${CHAT.busy ? ' disabled' : ''}><button class="send live" type="submit" aria-label="Send"${CHAT.busy ? ' disabled' : ''}>${I.send}</button></div>
        <div class="note">Answered by a local model on this PC (Ollama) from derived numbers only.</div>
      </form>`;
    const f = $('#askForm');
    f.addEventListener('submit', e => { e.preventDefault(); askAI($('#askInput').value); });
    f.querySelectorAll('[data-ask]').forEach(b => b.addEventListener('click', e => { e.stopPropagation(); askAI(b.dataset.ask); }));
  }
  function chatRefresh() { if (state.tab === 'chat' && isReal()) { renderChatReal(); window.scrollTo(0, document.body.scrollHeight); const i = $('#askInput'); if (i && !CHAT.busy && matchMedia('(pointer: fine)').matches) i.focus({ preventScroll: true }); } }
  async function askAI(q) {
    q = (q || '').trim().replace(/\s+/g, ' ');
    if (!q || CHAT.busy) return;
    if (q.length > MAX_Q) return toast(`Keep questions under ${MAX_Q} characters`);
    CHAT.log.push({ me: true, text: q }); CHAT.busy = true; chatRefresh();
    let m;
    try {
      const ctl = new AbortController(), to = setTimeout(() => ctl.abort(), 120000);
      const r = await fetch('api/ask', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ question: q }), signal: ctl.signal, cache: 'no-store' });
      clearTimeout(to);
      const j = await r.json().catch(() => ({}));
      if (r.ok && j.answer) m = { text: j.answer, err: !j.ok, cite: `${j.source || 'local AI'}${j.model ? ' · ' + j.model : ''}${j.wall_s ? ' · ' + j.wall_s + ' s' : ''} · not medical advice` };
      else m = { text: j.error || `The local AI server answered ${r.status}.`, err: true };
    } catch (e) {
      m = { text: e.name === 'AbortError' ? 'The local AI took too long to answer. Try again in a moment.' : 'Can’t reach the local AI server (are you offline, or is the PC asleep?).', err: true };
    }
    CHAT.log.push(m); CHAT.busy = false; chatRefresh();
  }

  const TABS = [['today', 'Today'], ['sleep', 'Sleep'], ['activity', 'Activity'], ['trends', 'Trends'], ['cycle', 'Cycle'], ['chat', 'AI']];
  const pick = (real, demo) => () => (isReal() ? real() : demo());
  const RENDER = {
    today: pick(renderTodayReal, renderToday), sleep: pick(renderSleepReal, renderSleep),
    activity: pick(renderActivityReal, renderActivity),
    trends: pick(renderTrendsReal, renderTrends),
    cycle: () => (isReal() && REAL.j.cycle ? renderCycleReal() : wrapDemo(renderCycle, 'Cycle tracking needs weeks of nightly temperature; generated demo data below.')()),
    chat: () => (isReal() ? renderChatReal() : renderChat()),
  };
  function applyMode() {
    const real = isReal();
    document.documentElement.dataset.mode = real ? 'real' : 'demo';
    const b = $('.demo-banner');
    b.classList.toggle('real', real);
    b.innerHTML = real
      ? `<span class="dot"></span>Real ring data &mdash; ${nNights(REAL.j.nights)} (last ${fmtDate(REAL.wakeDate, { month: 'short', day: 'numeric' })}). Today, Sleep, Activity (steps estimated), Trends, Cycle &amp; AI are real.`
      : '<span class="dot"></span>Demo data &mdash; generated, not from a real ring';
    const mt = $('#modeToggle');
    if (mt) mt.querySelectorAll('button').forEach(x => x.classList.toggle('on', x.dataset.mode === state.mode));
    $('#deviceBtn').innerHTML = I.ringIco + `<span class="badge">${real ? ((REAL.j.device && REAL.j.device.battery && REAL.j.device.battery.pct != null) ? REAL.j.device.battery.pct : '?') : DEVICE.battery}</span>`;
  }
  function setMode(m) {
    state.mode = m;
    try { localStorage.setItem('oura-mock-mode', m); } catch (e) { /* ignore */ }
    applyMode(); show(state.tab);
  }
  $('#tabs').innerHTML = TABS.map(([k, t]) => `<button data-tab="${k}" aria-label="${t}">${I[k]}<span>${t}</span></button>`).join('');

  function show(tab) {
    state.tab = tab;
    TABS.forEach(([k]) => { const el = $('#screen-' + k); el.hidden = k !== tab; });
    document.querySelectorAll('#tabs button').forEach(b => b.classList.toggle('on', b.dataset.tab === tab));
    const el = $('#screen-' + tab); RENDER[tab](); $('#title').textContent = el.dataset.title;
    if (location.hash !== '#' + tab) history.replaceState(null, '', '#' + tab);
    window.scrollTo(0, tab === 'chat' ? document.body.scrollHeight : 0);
  }

  let toastT;
  function toast(msg) { const t = $('#toast'); t.textContent = msg; t.classList.add('show'); clearTimeout(toastT); toastT = setTimeout(() => t.classList.remove('show'), 1800); }
  const sheet = $('#sheet');
  function openSheet() { renderDevice(); sheet.classList.add('open'); sheet.setAttribute('aria-hidden', 'false'); }
  function closeSheet() { sheet.classList.remove('open'); sheet.setAttribute('aria-hidden', 'true'); }

  function setTheme(t) {
    document.documentElement.dataset.theme = t;
    $('#themeBtn').innerHTML = t === 'dark' ? I.sun : I.moon;
    document.querySelector('meta[name=theme-color]').content = t === 'dark' ? '#0b0f17' : '#f3f5fa';
    try { localStorage.setItem('oura-mock-theme', t); } catch (e) { /* ignore */ }
  }
  let saved = 'dark'; try { saved = localStorage.getItem('oura-mock-theme') || 'dark'; } catch (e) { /* ignore */ }
  const qt = (location.search.match(/[?&]theme=(dark|light)/) || [])[1];
  setTheme(qt || saved);

  document.addEventListener('click', e => {
    const t = e.target;
    const tab = t.closest('[data-tab]'); if (tab) return show(tab.dataset.tab);
    const go = t.closest('[data-goto]'); if (go) return show(go.dataset.goto);
    if (t.closest('#themeBtn')) return setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
    if (t.closest('#deviceBtn') || t.closest('[data-open-device]')) return openSheet();
    if (t.closest('[data-close]')) return closeSheet();
    const seg = t.closest('.seg button');
    if (seg) { const key = seg.parentElement.dataset.seg, v = seg.dataset.v; state[key] = key === 'range' ? +v : v; RENDER[state.tab](); return; }
    const tg = t.closest('[data-toggle]');
    if (tg) {
      if (tg.dataset.toggle === 'one') tg.parentElement.querySelectorAll('.on').forEach(b => b !== tg && b.classList.remove('on'));
      tg.classList.toggle('on'); return toast('Demo: logging isn’t saved yet');
    }
    const demo = t.closest('[data-demo]'); if (demo) return toast(demo.dataset.demo || 'Demo only: not connected yet');
  });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeSheet(); });

  // chart scrubbing (touch + mouse)
  function scrub(e) {
    const el = e.target.closest && e.target.closest('.chart'); if (!el) return;
    const c = charts[el.dataset.chart]; if (!c) return;
    const svg = el.querySelector('svg'), r = svg.getBoundingClientRect();
    const x = (e.clientX - r.left) / r.width * W;
    let best = 0; c.xs.forEach((cx, i) => { if (Math.abs(cx - x) < Math.abs(c.xs[best] - x)) best = i; });
    const cur = svg.querySelector('.cursor'); cur.setAttribute('x1', c.xs[best]); cur.setAttribute('x2', c.xs[best]); cur.setAttribute('visibility', 'visible');
    const dot = svg.querySelector('circle.cur-dot[visibility]');
    if (dot && c.ys) { const y = c.ys[best]; if (y == null) dot.setAttribute('visibility', 'hidden'); else { dot.setAttribute('cx', c.xs[best]); dot.setAttribute('cy', y); dot.setAttribute('visibility', 'visible'); } }
    if (c.readout) el.querySelector('.readout').innerHTML = c.readout(best);
  }
  function unscrub(e) {
    const el = e.target.closest && e.target.closest('.chart'); if (!el || e.pointerType === 'touch') return;
    const c = charts[el.dataset.chart]; const svg = el.querySelector('svg');
    svg.querySelector('.cursor').setAttribute('visibility', 'hidden');
    const dot = svg.querySelector('circle.cur-dot[visibility]'); if (dot) dot.setAttribute('visibility', 'hidden');
    el.querySelector('.readout').innerHTML = (c && c.def) || '&nbsp;';
  }
  document.addEventListener('pointerdown', scrub);
  document.addEventListener('pointermove', scrub);
  document.addEventListener('pointerout', e => { if (!e.relatedTarget || !e.target.closest('.chart')?.contains(e.relatedTarget)) unscrub(e); });

  const start = location.hash.slice(1);
  const qm = (location.search.match(/[?&]mode=(real|demo)/) || [])[1];
  function boot() {
    if (REAL) {
      let pref = null; try { pref = localStorage.getItem('oura-mock-mode'); } catch (e) { /* ignore */ }
      state.mode = qm || pref || 'real';
      const tg = document.createElement('div');
      tg.className = 'seg mode-toggle'; tg.id = 'modeToggle'; tg.setAttribute('role', 'group'); tg.setAttribute('aria-label', 'Data source');
      tg.innerHTML = `<button data-mode="real">Real · ${REAL.j.nights} night${REAL.j.nights === 1 ? '' : 's'}</button><button data-mode="demo">Demo</button>`;
      $('.top-actions').prepend(tg);
      tg.addEventListener('click', e => { const b = e.target.closest('[data-mode]'); if (b) { e.stopPropagation(); setMode(b.dataset.mode); } });
    } else state.mode = 'demo';
    applyMode();
    show(RENDER[start] ? start : 'today');
    if (start === 'device') openSheet();
  }
  // PWA: installable to the phone home screen; sw.js = network-first for data, cached static shell
  if ('serviceWorker' in navigator && window.isSecureContext) {
    window.addEventListener('load', () => navigator.serviceWorker.register('sw.js', { scope: './' }).catch(() => { /* optional */ }));
  }
  fetch('real-data.json', { cache: 'no-store' })
    .then(r => (r.ok ? r.json() : null))
    .then(j => { if (j && j.series && j.night) REAL = prepReal(j); })
    .catch(() => { /* no real data: demo only */ })
    .finally(boot);
})();
