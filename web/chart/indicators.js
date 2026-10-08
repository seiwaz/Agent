/* Chart workspace — indicator math (pure functions, no DOM).
 * Input: bars [{ t, o, h, l, c }] sorted by time. Output: arrays aligned with the bars, null
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
  window.ChartIndicators = { sma, ema, donchian, rsi, macd, histShades, anchoredVwap };
})();
