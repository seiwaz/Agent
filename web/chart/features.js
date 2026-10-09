/* Chart workspace — feature registry.
 * Every option on the chart is one entry here: to add a feature, add an entry; to remove one,
 * delete it. The workspace builds the toggle, the settings form and the persistence from it.
 *
 *   id, group, label, on (default), settings: [{ key, label, def, min, max, step } | { key, label, type: "bool", def }]
 *   server features (structure / levels): draw(ov, s, c) -> { boxes, lines, marks }
 *     ov = /api/chart/overlays payload, s = this feature's settings, c = colors
 *     query: true when the settings are sent to the server (it computes the feature with them);
 *     otherwise the settings only change how the server's result is drawn
 *   indicators: pane "price" | "own"; attach(chart, pane, s, c, ws) -> { update(bars), remove() }
 *   settings types: number (min / max / step), "bool", "select" (options: [[value, label]])
 *
 * Structure settings (the pivot length) are shared by every structure / level feature. */
"use strict";
(function () {
  const I = window.ChartIndicators;
  const sec = (iso) => Math.floor(Date.parse(iso) / 1000);
  const empty = () => ({ boxes: [], lines: [], marks: [] });

  /** Two EMAs on the price pane (one feature, each length its own setting). */
  function emaPair(id, label, fast, slow, colors) {
    return { id, group: "Overlays", label, on: false, pane: "price",
      settings: [
        { key: "fast", label: "Fast EMA", def: fast, min: 2, max: 400, step: 1 },
        { key: "slow", label: "Slow EMA", def: slow, min: 2, max: 400, step: 1 }],
      attach(chart, pane, s) {
        const mk = (rgb, n) => chart.addSeries(LightweightCharts.LineSeries, { color: `rgba(${rgb},0.95)`, lineWidth: 1.5,
          priceLineVisible: false, crosshairMarkerVisible: false, title: `EMA ${n}` }, pane);
        const a = mk(colors[0], s.fast), b = mk(colors[1], s.slow);
        return {
          update(bars) {
            const c = bars.map((x) => x.c);
            const pts = (xs) => bars.map((x, i) => (xs[i] === null ? { time: x.t } : { time: x.t, value: xs[i] }));
            a.setData(pts(I.ema(c, s.fast))); b.setData(pts(I.ema(c, s.slow)));
          },
          remove() { chart.removeSeries(a); chart.removeSeries(b); },
        };
      } };
  }

  const STRUCTURE = { id: "structure", label: "Structure", settings: [
    { key: "swing_len", label: "Pivot length (bars each side)", def: 5, min: 2, max: 20, step: 1 }] };

  function zoneBoxes(ov, kind, s, c) {
    const out = empty();
    for (const z of ov.zones || []) {
      if (z.kind !== kind || (!z.valid && !s.history)) continue;
      const bull = z.direction === "LONG";
      const base = kind === "OB" ? (bull ? c.bull : c.bear) : (bull ? c.fvgBull : c.fvgBear);
      const a = z.valid ? 1 : 0.45;
      out.boxes.push({ t1: sec(z.from), t2: sec(z.to), top: z.top, bottom: z.bottom,
        fill: `rgba(${base},${0.16 * a})`, stroke: `rgba(${base},${0.75 * a})`, dash: !z.valid,
        label: z.valid ? kind : "", labelColor: `rgba(${base},1)` });
    }
    return out;
  }
  function eventLines(ov, kind, c) {
    const out = empty();
    const color = kind === "BOS" ? `rgba(${c.text},0.8)` : `rgba(${c.liq},0.95)`;
    for (const e of ov.events || []) {
      if (e.kind !== kind) continue;
      out.lines.push({ t1: sec(e.from), p1: e.level, t2: sec(e.to), p2: e.level, color, width: 1.2, dash: true,
        label: kind === "BOS" ? "BOS" : "CHoCH" });
    }
    return out;
  }

  const FEATURES = [
    { id: "sr", group: "Levels", label: "Support / resistance", on: true, server: true, query: true,
      settings: [
        { key: "sr_tol_atr", label: "Zone width (× ATR)", def: 0.25, min: 0.05, max: 2, step: 0.05 },
        { key: "sr_touches", label: "Minimum touches", def: 2, min: 2, max: 10, step: 1 },
        { key: "sr_max", label: "Zones shown", def: 8, min: 1, max: 30, step: 1 }],
      draw(ov, s, c) {
        const out = empty();
        for (const z of ov.sr || []) {
          const col = z.role === "SUPPORT" ? c.bull : c.bear, a = z.broken ? 0.5 : 1;
          out.boxes.push({ t1: sec(z.from), t2: sec(z.to), top: z.top, bottom: z.bottom,
            fill: `rgba(${col},${0.10 * a})`, stroke: `rgba(${col},${0.55 * a})`, dash: z.broken,
            label: `${z.role === "SUPPORT" ? "S" : "R"} ×${z.touches}`, labelColor: `rgba(${col},1)` });
        }
        return out;
      } },
    { id: "tl", group: "Levels", label: "Trendlines", on: true, server: true, query: true,
      settings: [{ key: "tl_max", label: "Lines per side", def: 3, min: 1, max: 10, step: 1 }],
      draw(ov, s, c) {
        const out = empty();
        for (const l of ov.trendlines || []) {
          const col = l.role === "SUPPORT" ? c.bull : c.bear;
          out.lines.push({ t1: sec(l.from), p1: l.from_price, t2: sec(l.to), p2: l.to_price,
            color: `rgba(${col},${l.broken ? 0.5 : 0.95})`, width: 1.6, dash: l.broken });
        }
        return out;
      } },
    { id: "ob", group: "Structure", label: "Order blocks (OB)", on: true, server: true,
      settings: [{ key: "history", label: "Also mitigated blocks", type: "bool", def: false }],
      draw: (ov, s, c) => zoneBoxes(ov, "OB", s, c) },
    { id: "fvg", group: "Structure", label: "Fair value gaps (FVG)", on: true, server: true,
      settings: [{ key: "history", label: "Also filled gaps", type: "bool", def: false }],
      draw: (ov, s, c) => zoneBoxes(ov, "FVG", s, c) },
    { id: "bos", group: "Structure", label: "BOS", on: true, server: true, draw: (ov, s, c) => eventLines(ov, "BOS", c) },
    { id: "choch", group: "Structure", label: "CHoCH", on: true, server: true, draw: (ov, s, c) => eventLines(ov, "CHOCH", c) },
    { id: "swings", group: "Structure", label: "HH / HL / LH / LL", on: true, server: true,
      draw(ov, s, c) {
        const out = empty();
        for (const w of ov.swings || []) {
          const up = w.label === "HH" || w.label === "HL";
          out.marks.push({ t: sec(w.time), p: w.price, text: w.label, color: `rgba(${up ? c.bull : c.bear},1)`, pos: w.kind === "HIGH" ? "above" : "below" });
        }
        return out;
      } },
    { id: "donchian", group: "Overlays", label: "Donchian channels", on: false, pane: "price",
      settings: [{ key: "length", label: "Length", def: 20, min: 2, max: 400, step: 1 }],
      attach(chart, pane, s, c) {
        const opt = (dash) => ({ color: `rgba(${c.fvgBull},0.9)`, lineWidth: 1, lineStyle: dash ? 2 : 0, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
        const up = chart.addSeries(LightweightCharts.LineSeries, opt(false), pane);
        const lo = chart.addSeries(LightweightCharts.LineSeries, opt(false), pane);
        const mid = chart.addSeries(LightweightCharts.LineSeries, opt(true), pane);
        return {
          update(bars) {
            const d = I.donchian(bars, s.length);
            const pts = (xs) => bars.map((b, i) => (xs[i] === null ? { time: b.t } : { time: b.t, value: xs[i] }));
            up.setData(pts(d.upper)); lo.setData(pts(d.lower)); mid.setData(pts(d.middle));
          },
          remove() { for (const x of [up, lo, mid]) chart.removeSeries(x); },
        };
      } },
    { id: "vwap", group: "Overlays", label: "VWAP (session)", on: false, pane: "price",
      settings: [
        { key: "period", label: "Session", type: "select", def: "day", options: [["day", "Day (UTC)"], ["week", "Week"], ["month", "Month"]] },
        { key: "bands", label: "Bands", type: "bool", def: false },
        { key: "mult", label: "Band × st. dev.", def: 1, min: 0.5, max: 4, step: 0.5 }],
      attach(chart, pane, s, c) {
        const line = (color, w, dash, title) => chart.addSeries(LightweightCharts.LineSeries, { color, lineWidth: w, lineStyle: dash ? 2 : 0,
          priceLineVisible: false, lastValueVisible: !dash, crosshairMarkerVisible: false, title }, pane);
        const v = line("rgba(233,30,99,0.95)", 2, false, `VWAP ${s.period}`);
        const up = s.bands ? line("rgba(233,30,99,0.55)", 1, true, "") : null, lo = s.bands ? line("rgba(233,30,99,0.55)", 1, true, "") : null;
        return {
          update(bars) {
            const d = I.sessionVwap(bars, s.period, s.mult);
            // where a session restarts, the segment into it is transparent (a point's colour paints the
            // segment that starts at it): a break, not a jump from one session to the next
            const ends = (i) => i + 1 < bars.length && I.sessionStart(bars[i + 1].t, s.period) !== I.sessionStart(bars[i].t, s.period);
            const pts = (xs) => bars.map((b, i) => (ends(i) ? { time: b.t, value: xs[i], color: "rgba(0,0,0,0)" } : { time: b.t, value: xs[i] }));
            v.setData(pts(d.vwap));
            if (up) { up.setData(pts(d.upper)); lo.setData(pts(d.lower)); }
          },
          remove() { for (const x of [v, up, lo]) if (x) chart.removeSeries(x); },
        };
      } },
    emaPair("ema_fast", "EMA 9 / 21 (momentum)", 9, 21, ["255,179,0", "41,182,246"]),
    emaPair("ema_slow", "EMA 50 / 200 (trend)", 50, 200, ["102,187,106", "239,83,80"]),
    { id: "bb", group: "Overlays", label: "Bollinger Bands", on: false, pane: "price",
      settings: [
        { key: "length", label: "Length", def: 20, min: 2, max: 200, step: 1 },
        { key: "mult", label: "× st. dev.", def: 2, min: 0.5, max: 5, step: 0.1 }],
      attach(chart, pane, s, c) {
        const opt = (dash) => ({ color: "rgba(126,87,194,0.9)", lineWidth: 1, lineStyle: dash ? 2 : 0, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
        const up = chart.addSeries(LightweightCharts.LineSeries, { ...opt(false), title: `BB ${s.length},${s.mult}` }, pane);
        const lo = chart.addSeries(LightweightCharts.LineSeries, opt(false), pane);
        const mid = chart.addSeries(LightweightCharts.LineSeries, opt(true), pane);
        return {
          update(bars) {
            const d = I.bollinger(bars, s.length, s.mult);
            const pts = (xs) => bars.map((b, i) => (xs[i] === null ? { time: b.t } : { time: b.t, value: xs[i] }));
            up.setData(pts(d.upper)); lo.setData(pts(d.lower)); mid.setData(pts(d.middle));
          },
          remove() { for (const x of [up, lo, mid]) chart.removeSeries(x); },
        };
      } },
    { id: "sessions", group: "Overlays", label: "Sessions (Tokyo / London / New York)", on: false, pane: "price",
      settings: [
        { key: "tokyo", label: "Tokyo 09–18 JST", type: "bool", def: true },
        { key: "london", label: "London 08–17 UK", type: "bool", def: true },
        { key: "newyork", label: "New York 08–17 ET", type: "bool", def: true },
        { key: "opacity", label: "Shade %", def: 7, min: 2, max: 30, step: 1 }],
      attach(chart, pane, s, c, ws) {
        // background bands under the candles, on 5m / 15m / 1h (a 4h or daily bar spans sessions)
        const layer = {
          bars: [],
          view: { zOrder: () => "bottom", renderer: () => ({ draw: (t) => draw(t) }) },
          attached(p) { this.req = p.requestUpdate; }, detached() { this.req = null; },
          updateAllViews() {}, paneViews() { return [this.view]; },
        };
        const ids = I.SESSIONS.map((x) => x.id).filter((id) => s[id]);
        const draw = (target) => {
          const b = layer.bars, tools = ws.tools;
          if (!b.length || !tools || b[1] && b[1].t - b[0].t > 3600) return;
          const r = chart.timeScale().getVisibleLogicalRange();
          if (!r) return;
          const i0 = Math.max(0, Math.floor(r.from)), i1 = Math.min(b.length - 1, Math.ceil(r.to));
          const t0 = b[i0].t, t1 = b[i1].t + 3600;
          target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
            ctx.font = '500 10px "IBM Plex Sans", sans-serif'; ctx.textBaseline = "top"; ctx.textAlign = "left";
            I.sessions(t0, t1, ids).forEach((x) => {
              const xa = tools.x(x.start), xb = tools.x(x.end);
              if (xa === null || xb === null || xb < 0 || xa > mediaSize.width) return;
              const row = I.SESSIONS.findIndex((q) => q.id === x.id);
              ctx.fillStyle = `rgba(${x.color},${s.opacity / 100})`;
              ctx.fillRect(xa, 0, xb - xa, mediaSize.height);
              ctx.fillStyle = `rgba(${x.color},0.9)`;
              ctx.fillRect(xa, row * 4, xb - xa, 3);  // a strip at the top marks each session
              if (xb - xa > 40) ctx.fillText(x.name, Math.max(xa, 0) + 3, 14 + row * 12);
            });
          });
        };
        ws.candles.attachPrimitive(layer);
        return {
          update(bars) { layer.bars = bars; if (layer.req) layer.req(); },
          remove() { ws.candles.detachPrimitive(layer); },
        };
      } },
    { id: "volume", group: "Panes", label: "Volume", on: false, pane: "own",
      settings: [{ key: "ma", label: "Average (bars, 0 = off)", def: 20, min: 0, max: 200, step: 1 }],
      attach(chart, pane, s, c) {
        const hist = chart.addSeries(LightweightCharts.HistogramSeries, { priceLineVisible: false, lastValueVisible: true, title: "Vol",
          priceFormat: { type: "volume" } }, pane);
        const ma = s.ma ? chart.addSeries(LightweightCharts.LineSeries, { color: `rgba(${c.text},0.6)`, lineWidth: 1, priceLineVisible: false,
          lastValueVisible: false, crosshairMarkerVisible: false, priceFormat: { type: "volume" } }, pane) : null;
        return {
          update(bars) {
            hist.setData(bars.map((b) => ({ time: b.t, value: b.v || 0, color: `rgba(${b.c >= b.o ? c.bull : c.bear},0.55)` })));
            if (ma) { const m = I.sma(bars.map((b) => b.v || 0), s.ma); ma.setData(bars.map((b, i) => (m[i] === null ? { time: b.t } : { time: b.t, value: m[i] }))); }
          },
          remove() { for (const x of [hist, ma]) if (x) chart.removeSeries(x); },
        };
      } },
    { id: "atr", group: "Panes", label: "ATR", on: false, pane: "own",
      settings: [
        { key: "length", label: "Length", def: 14, min: 2, max: 100, step: 1 },
        { key: "cost", label: "Round-trip cost % (0 = off)", def: 0.12, min: 0, max: 2, step: 0.01 }],
      attach(chart, pane, s, c) {
        const line = chart.addSeries(LightweightCharts.LineSeries, { color: `rgba(${c.fvgBear},1)`, lineWidth: 1.5, priceLineVisible: false, title: `ATR ${s.length}` }, pane);
        // what a round trip costs at the price (fees + slippage): a move smaller than this pays nothing
        const cost = s.cost ? chart.addSeries(LightweightCharts.LineSeries, { color: `rgba(${c.text},0.5)`, lineWidth: 1, lineStyle: 2,
          priceLineVisible: false, lastValueVisible: true, crosshairMarkerVisible: false, title: "cost" }, pane) : null;
        return {
          update(bars) {
            const a = I.atr(bars, s.length);
            line.setData(bars.map((b, i) => (a[i] === null ? { time: b.t } : { time: b.t, value: a[i] })));
            if (cost) cost.setData(bars.map((b) => ({ time: b.t, value: (b.c * s.cost) / 100 })));
          },
          remove() { for (const x of [line, cost]) if (x) chart.removeSeries(x); },
        };
      } },
    { id: "adx", group: "Panes", label: "ADX / DMI", on: false, pane: "own",
      settings: [
        { key: "length", label: "Length", def: 14, min: 2, max: 100, step: 1 },
        { key: "level", label: "Trend above", def: 25, min: 10, max: 50, step: 1 },
        { key: "di", label: "+DI / −DI", type: "bool", def: true }],
      attach(chart, pane, s, c) {
        const fmt = { type: "price", precision: 1, minMove: 0.1 };
        const adx = chart.addSeries(LightweightCharts.LineSeries, { color: `rgba(${c.text},0.9)`, lineWidth: 2, priceLineVisible: false, priceFormat: fmt, title: `ADX ${s.length}` }, pane);
        adx.createPriceLine({ price: s.level, color: `rgba(${c.text},0.45)`, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: "" });
        const pdi = s.di ? chart.addSeries(LightweightCharts.LineSeries, { color: `rgba(${c.bull},0.9)`, lineWidth: 1, priceLineVisible: false, lastValueVisible: false, priceFormat: fmt }, pane) : null;
        const mdi = s.di ? chart.addSeries(LightweightCharts.LineSeries, { color: `rgba(${c.bear},0.9)`, lineWidth: 1, priceLineVisible: false, lastValueVisible: false, priceFormat: fmt }, pane) : null;
        return {
          update(bars) {
            const d = I.adx(bars, s.length);
            const pts = (xs) => bars.map((b, i) => (xs[i] === null ? { time: b.t } : { time: b.t, value: xs[i] }));
            adx.setData(pts(d.adx));
            if (pdi) { pdi.setData(pts(d.plus)); mdi.setData(pts(d.minus)); }
          },
          remove() { for (const x of [adx, pdi, mdi]) if (x) chart.removeSeries(x); },
        };
      } },
    { id: "rsi", group: "Panes", label: "RSI", on: false, pane: "own",
      settings: [
        { key: "length", label: "Length", def: 14, min: 2, max: 100, step: 1 },
        { key: "overbought", label: "Overbought", def: 70, min: 50, max: 95, step: 1 },
        { key: "oversold", label: "Oversold", def: 30, min: 5, max: 50, step: 1 },
        { key: "midline", label: "50 line (above: bullish filter, below: bearish)", type: "bool", def: true }],
      attach(chart, pane, s, c) {
        const line = chart.addSeries(LightweightCharts.LineSeries, { color: `rgba(${c.liq},1)`, lineWidth: 1.5, priceLineVisible: false,
          priceFormat: { type: "price", precision: 1, minMove: 0.1 }, title: `RSI ${s.length}` }, pane);
        for (const [v, col] of [[s.overbought, c.bear], [s.oversold, c.bull]]) {
          line.createPriceLine({ price: v, color: `rgba(${col},0.7)`, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: "" });
        }
        if (s.midline) line.createPriceLine({ price: 50, color: `rgba(${c.text},0.45)`, lineWidth: 1, lineStyle: 1, axisLabelVisible: false, title: "" });
        return {
          update(bars) {
            const r = I.rsi(bars, s.length);
            line.setData(bars.map((b, i) => (r[i] === null ? { time: b.t } : { time: b.t, value: r[i] })));
          },
          remove() { chart.removeSeries(line); },
        };
      } },
    { id: "macd", group: "Panes", label: "MACD", on: false, pane: "own",
      settings: [
        { key: "fast", label: "Fast EMA", def: 12, min: 2, max: 100, step: 1 },
        { key: "slow", label: "Slow EMA", def: 26, min: 3, max: 200, step: 1 },
        { key: "signal", label: "Signal EMA", def: 9, min: 2, max: 50, step: 1 }],
      attach(chart, pane, s, c) {
        const hist = chart.addSeries(LightweightCharts.HistogramSeries, { priceLineVisible: false, lastValueVisible: false }, pane);
        const m = chart.addSeries(LightweightCharts.LineSeries, { color: `rgba(${c.fvgBull},1)`, lineWidth: 1.5, priceLineVisible: false, title: `MACD ${s.fast},${s.slow},${s.signal}` }, pane);
        const sg = chart.addSeries(LightweightCharts.LineSeries, { color: `rgba(${c.fvgBear},1)`, lineWidth: 1.2, priceLineVisible: false, lastValueVisible: false }, pane);
        return {
          update(bars) {
            const d = I.macd(bars, s.fast, s.slow, s.signal);
            const pts = (xs) => bars.map((b, i) => (xs[i] === null ? { time: b.t } : { time: b.t, value: xs[i] }));
            // four shades: strong while the histogram grows away from zero, faded while it shrinks
            const shade = I.histShades(d.hist);
            const tone = { up: `rgba(${c.bull},0.95)`, upFade: `rgba(${c.bull},0.38)`, down: `rgba(${c.bear},0.95)`, downFade: `rgba(${c.bear},0.38)` };
            hist.setData(bars.map((b, i) => (d.hist[i] === null ? { time: b.t }
              : { time: b.t, value: d.hist[i], color: tone[shade[i]] })));
            m.setData(pts(d.macd)); sg.setData(pts(d.signal));
          },
          remove() { for (const x of [hist, m, sg]) chart.removeSeries(x); },
        };
      } },
  ];
  const GROUPS = ["Levels", "Structure", "Overlays", "Panes"];
  window.ChartFeatures = { FEATURES, GROUPS, STRUCTURE };
})();
