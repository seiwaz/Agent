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
  window.ChartIndicators = { sma, ema, donchian, rsi, macd };
})();
