/* Chart workspace — feature registry.
 * Every option on the chart is one entry here: to add a feature, add an entry; to remove one,
 * delete it. The workspace builds the toggle, the settings form and the persistence from it.
 *
 *   id, group, label, on (default), settings: [{ key, label, def, min, max, step } | { key, label, type: "bool", def }]
 *   server features (structure / levels): draw(ov, s, c) -> { boxes, lines, marks }
 *     ov = /api/chart/overlays payload, s = this feature's settings, c = colors
 *     query: true when the settings are sent to the server (it computes the feature with them);
 *     otherwise the settings only change how the server's result is drawn
 *   indicators: pane "price" | "own"; attach(chart, pane, s, c) -> { update(bars), remove() }
 *
 * Structure settings (the pivot length) are shared by every structure / level feature. */
"use strict";
(function () {
  const I = window.ChartIndicators;
  const sec = (iso) => Math.floor(Date.parse(iso) / 1000);
  const empty = () => ({ boxes: [], lines: [], marks: [] });

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
    { id: "donchian", group: "Indicators", label: "Donchian channels", on: false, pane: "price",
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
    { id: "rsi", group: "Indicators", label: "RSI", on: false, pane: "own",
      settings: [
        { key: "length", label: "Length", def: 14, min: 2, max: 100, step: 1 },
        { key: "overbought", label: "Overbought", def: 70, min: 50, max: 95, step: 1 },
        { key: "oversold", label: "Oversold", def: 30, min: 5, max: 50, step: 1 }],
      attach(chart, pane, s, c) {
        const line = chart.addSeries(LightweightCharts.LineSeries, { color: `rgba(${c.liq},1)`, lineWidth: 1.5, priceLineVisible: false,
          priceFormat: { type: "price", precision: 1, minMove: 0.1 }, title: `RSI ${s.length}` }, pane);
        for (const [v, col] of [[s.overbought, c.bear], [s.oversold, c.bull]]) {
          line.createPriceLine({ price: v, color: `rgba(${col},0.7)`, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: "" });
        }
        return {
          update(bars) {
            const r = I.rsi(bars, s.length);
            line.setData(bars.map((b, i) => (r[i] === null ? { time: b.t } : { time: b.t, value: r[i] })));
          },
          remove() { chart.removeSeries(line); },
        };
      } },
    { id: "macd", group: "Indicators", label: "MACD", on: false, pane: "own",
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
            hist.setData(bars.map((b, i) => (d.hist[i] === null ? { time: b.t }
              : { time: b.t, value: d.hist[i], color: `rgba(${d.hist[i] >= 0 ? c.bull : c.bear},0.55)` })));
            m.setData(pts(d.macd)); sg.setData(pts(d.signal));
          },
          remove() { for (const x of [hist, m, sg]) chart.removeSeries(x); },
        };
      } },
  ];
  const GROUPS = ["Levels", "Structure", "Indicators"];
  window.ChartFeatures = { FEATURES, GROUPS, STRUCTURE };
})();
