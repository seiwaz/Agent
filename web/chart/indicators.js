/* Chart workspace — indicator math (pure functions, no DOM).
 * Input: bars [{ t, o, h, l, c, v }] sorted by time. Output: arrays aligned with the bars, null
 * where the indicator has no value yet. Every value at bar i uses bars 0..i only. */
"use strict";
(function () {
  function sma(xs, n) {
    const out = new Array(xs.length).fill(null);
    let s = 0;
    for (let i = 0; i < xs.length; i++) {
      s += xs[i];
      if (i >= n) s -= xs[i - n];
      if (i >= n - 1) out[i] = s / n;
    }
    return out;
  }
  /** EMA seeded with the SMA of the first n values that are not null. */
  function ema(xs, n) {
    const out = new Array(xs.length).fill(null);
    const k = 2 / (n + 1);
    let e = null, s = 0, seen = 0;
    for (let i = 0; i < xs.length; i++) {
      if (xs[i] === null) continue;
      if (e === null) {
        s += xs[i]; seen += 1;
        if (seen === n) { e = s / n; out[i] = e; }
        continue;
      }
      e = xs[i] * k + e * (1 - k);
      out[i] = e;
    }
    return out;
  }
  /** Donchian channel: highest high / lowest low of the last n bars (this bar included). */
  function donchian(bars, n) {
    const up = new Array(bars.length).fill(null), lo = new Array(bars.length).fill(null), mid = new Array(bars.length).fill(null);
    for (let i = n - 1; i < bars.length; i++) {
      let h = -Infinity, l = Infinity;
      for (let j = i - n + 1; j <= i; j++) { h = Math.max(h, bars[j].h); l = Math.min(l, bars[j].l); }
      up[i] = h; lo[i] = l; mid[i] = (h + l) / 2;
    }
    return { upper: up, lower: lo, middle: mid };
  }
  /** RSI with Wilder smoothing. */
  function rsi(bars, n) {
    const out = new Array(bars.length).fill(null);
    let g = 0, l = 0;
    for (let i = 1; i < bars.length; i++) {
      const d = bars[i].c - bars[i - 1].c, up = Math.max(d, 0), dn = Math.max(-d, 0);
      if (i <= n) { g += up; l += dn; if (i === n) { g /= n; l /= n; out[i] = l === 0 ? 100 : 100 - 100 / (1 + g / l); } continue; }
      g = (g * (n - 1) + up) / n; l = (l * (n - 1) + dn) / n;
      out[i] = l === 0 ? 100 : 100 - 100 / (1 + g / l);
    }
    return out;
  }
  /** MACD: EMA(fast) - EMA(slow), its EMA(signal), and the histogram. */
  function macd(bars, fast, slow, signal) {
    const c = bars.map((b) => b.c);
    const f = ema(c, fast), s = ema(c, slow);
    const line = c.map((_, i) => (f[i] === null || s[i] === null ? null : f[i] - s[i]));
    const sig = ema(line, signal);
    const hist = line.map((v, i) => (v === null || sig[i] === null ? null : v - sig[i]));
    return { macd: line, signal: sig, hist };
  }
  /** MACD histogram shade per bar, as TradingView: above zero and growing "up", above zero and
   * shrinking "upFade", below zero and falling further "down", below zero and recovering
   * "downFade" (null where there is no value). */
  function histShades(hist) {
    return hist.map((v, i) => {
      if (v === null) return null;
      const prev = i > 0 ? hist[i - 1] : null;
      const growing = prev === null || v >= prev;
      return v >= 0 ? (growing ? "up" : "upFade") : (growing ? "downFade" : "down");
    });
  }
  /** Anchored VWAP from bar i0: cumulative (typical price × volume) / cumulative volume; bars
   * without volume weigh 1. Values before the anchor are null. */
  function anchoredVwap(bars, i0) {
    const out = bars.map(() => null);
    let pv = 0, vol = 0;
    for (let i = Math.max(0, i0); i < bars.length; i++) {
      const b = bars[i], w = b.v > 0 ? b.v : 1, tp = (b.h + b.l + b.c) / 3;
      pv += tp * w; vol += w;
      out[i] = pv / vol;
    }
    return out;
  }
  /** Wilder's moving average (RMA), seeded with the SMA of the first n values. */
  function rma(xs, n) {
    const out = new Array(xs.length).fill(null);
    let r = null, s = 0, seen = 0;
    for (let i = 0; i < xs.length; i++) {
      if (xs[i] === null) continue;
      if (r === null) { s += xs[i]; seen += 1; if (seen === n) { r = s / n; out[i] = r; } continue; }
      r = (r * (n - 1) + xs[i]) / n;
      out[i] = r;
    }
    return out;
  }
  function trueRange(bars) {
    return bars.map((b, i) => (i === 0 ? b.h - b.l
      : Math.max(b.h - b.l, Math.abs(b.h - bars[i - 1].c), Math.abs(b.l - bars[i - 1].c))));
  }
  /** ATR with Wilder smoothing. */
  function atr(bars, n) { return rma(trueRange(bars), n); }
  /** ADX, +DI and -DI (Wilder, n bars). */
  function adx(bars, n) {
    const pdm = bars.map(() => null), mdm = bars.map(() => null);
    for (let i = 1; i < bars.length; i++) {
      const up = bars[i].h - bars[i - 1].h, dn = bars[i - 1].l - bars[i].l;
      pdm[i] = up > dn && up > 0 ? up : 0;
      mdm[i] = dn > up && dn > 0 ? dn : 0;
    }
    const tr = trueRange(bars).map((v, i) => (i === 0 ? null : v));
    const str = rma(tr, n), sp = rma(pdm, n), sm = rma(mdm, n);
    const plus = bars.map((_, i) => (str[i] ? (100 * sp[i]) / str[i] : null));
    const minus = bars.map((_, i) => (str[i] ? (100 * sm[i]) / str[i] : null));
    const dx = bars.map((_, i) => (plus[i] === null || minus[i] === null ? null
      : plus[i] + minus[i] === 0 ? 0 : (100 * Math.abs(plus[i] - minus[i])) / (plus[i] + minus[i])));
    return { adx: rma(dx, n), plus, minus };
  }
  /** Bollinger Bands: SMA(n) ± k population standard deviations of the close. */
  function bollinger(bars, n, k) {
    const c = bars.map((b) => b.c), mid = sma(c, n);
    const up = c.map(() => null), lo = c.map(() => null), width = c.map(() => null);
    for (let i = n - 1; i < c.length; i++) {
      let v = 0;
      for (let j = i - n + 1; j <= i; j++) v += (c[j] - mid[i]) ** 2;
      const sd = Math.sqrt(v / n);
      up[i] = mid[i] + k * sd; lo[i] = mid[i] - k * sd; width[i] = mid[i] ? (up[i] - lo[i]) / mid[i] : null;
    }
    return { upper: up, middle: mid, lower: lo, width };
  }
  /** Start (UTC seconds) of the session containing t: "day", "week" (Monday) or "month", UTC. */
  function sessionStart(t, period) {
    const d = new Date(t * 1000);
    if (period === "month") return Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), 1) / 1000;
    const day = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()) / 1000;
    if (period === "week") return day - ((d.getUTCDay() + 6) % 7) * 86400;
    return day;
  }
  /** VWAP that restarts every session (UTC day / week / month), with ±k standard-deviation bands
   * of the typical price around it (volume-weighted). Bars without volume weigh 1. */
  function sessionVwap(bars, period, k) {
    const v = bars.map(() => null), up = bars.map(() => null), lo = bars.map(() => null);
    let start = null, pv = 0, pv2 = 0, w = 0;
    for (let i = 0; i < bars.length; i++) {
      const b = bars[i], s = sessionStart(b.t, period);
      if (s !== start) { start = s; pv = 0; pv2 = 0; w = 0; }
      const x = (b.h + b.l + b.c) / 3, q = b.v > 0 ? b.v : 1;
      pv += x * q; pv2 += x * x * q; w += q;
      v[i] = pv / w;
      const sd = Math.sqrt(Math.max(0, pv2 / w - v[i] * v[i]));
      up[i] = v[i] + k * sd; lo[i] = v[i] - k * sd;
    }
    return { vwap: v, upper: up, lower: lo };
  }
  /* Trading sessions in their own time zones (daylight saving included). */
  const SESSIONS = [
    { id: "tokyo", name: "Tokyo", tz: "Asia/Tokyo", from: [9, 0], to: [18, 0], color: "239,83,80" },
    { id: "london", name: "London", tz: "Europe/London", from: [8, 0], to: [17, 0], color: "41,98,255" },
    { id: "newyork", name: "New York", tz: "America/New_York", from: [8, 0], to: [17, 0], color: "255,152,0" },
  ];
  const zoneFmt = {};
  function zoneOffsetMin(ms, tz) {
    const f = zoneFmt[tz] || (zoneFmt[tz] = new Intl.DateTimeFormat("en-US", { timeZone: tz, hourCycle: "h23",
      year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" }));
    const p = Object.fromEntries(f.formatToParts(new Date(ms)).map((x) => [x.type, x.value]));
    return (Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour % 24, +p.minute, +p.second) - ms) / 60000;
  }
  /** UTC seconds of a wall-clock time in a time zone. */
  function zoned(y, m, d, hh, mm, tz) {
    const guess = Date.UTC(y, m, d, hh, mm);
    let t = guess - zoneOffsetMin(guess, tz) * 60000;
    t = guess - zoneOffsetMin(t, tz) * 60000;  // across a DST change
    return t / 1000;
  }
  /** Sessions overlapping [t0, t1] (UTC seconds): [{ id, name, color, start, end }], weekdays only. */
  function sessions(t0, t1, ids) {
    const out = [];
    for (let day = sessionStart(t0, "day") - 86400; day <= t1 + 86400; day += 86400) {
      const d = new Date(day * 1000);
      for (const s of SESSIONS) {
        if (ids && !ids.includes(s.id)) continue;
        const start = zoned(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), s.from[0], s.from[1], s.tz);
        const end = zoned(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), s.to[0], s.to[1], s.tz);
        const wd = new Date((start + zoneOffsetMin(start * 1000, s.tz) * 60) * 1000).getUTCDay();
        if (wd === 0 || wd === 6 || end <= t0 || start >= t1) continue;  // markets closed on weekends
        out.push({ id: s.id, name: s.name, color: s.color, start, end });
      }
    }
    return out;
  }
  window.ChartIndicators = { sma, ema, rma, donchian, rsi, macd, histShades, anchoredVwap, atr, adx, bollinger,
    sessionStart, sessionVwap, sessions, SESSIONS };
})();
