/* Chart workspace — drawing tools (left toolbar): trend line, horizontal line, long / short
 * position, price range and path. A position drawing is a measurement: once price has traded
 * through its entry, the part from the entry to the current price (or to the stop / target that
 * was hit) is shaded darker green (in profit) or red (at a loss) across the drawing's width. The Trade button (⚡) sends the
 * selected or last position drawing to the trade dialog (web/chart/trading.js); drawing alone
 * never creates an order. Every open trade — placed here or directly on Tabdeal — has a position
 * drawing linked to it (`trade`), kept in line with the exchange; dragging its stop / target
 * marks it moved until the panel's SL/TP button sends them to Tabdeal. When the trade closes (or
 * is canceled) its drawing is removed. A position drawing shows
 * its labels (entry, stop, target, R:R, result) only while it is selected.
 *
 * Drawings are kept per market in localStorage, in time (UTC seconds) / price, so they appear on
 * every timeframe. Interaction: pick a tool and click on the price pane (trend line, range: two
 * clicks; horizontal line, long, short: one click; path: clicks, then double-click / Enter); pick
 * the same tool again to cancel. Click a drawing to select it, drag a handle to edit a point or
 * the drawing to move it, Delete removes it, Esc cancels. */
"use strict";
(function () {
  const STORE = "smc-drawings-v1";
  const HIT = 7;  // px
  const TOOLS = [
    { id: "trend", label: "Trend line", points: 2,
      icon: '<path d="M4 20 20 4"/><circle cx="4" cy="20" r="2"/><circle cx="20" cy="4" r="2"/>' },
    { id: "hline", label: "Horizontal line", points: 1,
      icon: '<path d="M2 12h20"/><circle cx="12" cy="12" r="2"/>' },
    { id: "long", label: "Long position", points: 1,
      icon: '<rect x="4" y="3" width="16" height="9" rx="1"/><rect x="4" y="12" width="16" height="6" rx="1" opacity=".5"/><path d="M8 9l4-4 4 4"/>' },
    { id: "short", label: "Short position", points: 1,
      icon: '<rect x="4" y="12" width="16" height="9" rx="1"/><rect x="4" y="6" width="16" height="6" rx="1" opacity=".5"/><path d="M8 15l4 4 4-4"/>' },
    { id: "range", label: "Price range", points: 2,
      icon: '<path d="M12 3v18"/><path d="M8 7l4-4 4 4"/><path d="M8 17l4 4 4-4"/><path d="M4 3h16M4 21h16" opacity=".6"/>' },
    { id: "path", label: "Path", points: Infinity,
      icon: '<path d="M3 18l6-8 5 5 7-10"/><path d="M17 5h4v4"/>' },
    { id: "vline", label: "Vertical line", points: 1,
      icon: '<path d="M12 2v20"/><circle cx="12" cy="12" r="2"/>' },
    { id: "daterange", label: "Date range", points: 2,
      icon: '<path d="M3 12h18"/><path d="M7 8l-4 4 4 4"/><path d="M17 8l4 4-4 4"/><path d="M3 4v16M21 4v16" opacity=".6"/>' },
    { id: "avwap", label: "Anchored VWAP", points: 1,
      icon: '<path d="M4 20c4-1 5-9 9-10s5 3 7-2"/><path d="M4 22v-6" stroke-width="2.4"/><circle cx="4" cy="16" r="1.6"/>' },
    { id: "fib", label: "Fib retracement", points: 2,
      icon: '<path d="M3 4h18M3 9h18M3 13h18M3 20h18" opacity=".75"/><path d="M5 20 19 4" stroke-dasharray="2 2"/>' },
  ];
  const FIB = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];
  const FIB_TONE = ["120,123,134", "242,54,69", "255,152,0", "76,175,80", "8,153,129", "0,188,212", "120,123,134"];
  const NAMES = Object.fromEntries(TOOLS.map((t) => [t.id, t.label]));

  /* ---- per-drawing settings (it.st), kept with the drawing --------------------------------- */
  const DASH = { solid: [], dashed: [7, 4], dotted: [2, 3] };
  const hex = (rgb) => `#${rgb.split(",").map((v) => (+v).toString(16).padStart(2, "0")).join("")}`;
  const rgba = (h, a) => { const n = parseInt(h.slice(1), 16); return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`; };
  const COLORED = new Set(["trend", "hline", "vline", "path", "avwap", "daterange"]);
  const LINED = new Set(["trend", "hline", "vline", "path", "avwap", "fib"]);
  const SETTINGS = new Set(["trend", "hline", "vline", "path", "avwap", "fib"]);
  function defaults(type) {
    if (type === "fib") {
      return { width: 1, style: "solid", levels: FIB.map((v, k) => ({ v, on: true, color: hex(FIB_TONE[k]) })),
        labels: true, prices: true, fill: true, extend: false, reverse: false };
    }
    if (type === "avwap") return { color: "#e91e63", width: 2, style: "solid", label: true };
    return { color: null, width: 2, style: "solid", label: true };  // color null: the theme's line colour
  }
  /** A drawing's settings over the defaults of its type. */
  function st(it) {
    const d = defaults(it.type), s = it.st || {};
    return { ...d, ...s, levels: s.levels || d.levels };
  }
  const VDATE = new Intl.DateTimeFormat(undefined, { month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  const TRASH = '<path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M6 6l1 14h10l1-14"/>';
  const TRADE = '<path d="M13 2 4 14h7l-1 8 9-12h-7z"/>';

  const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  function svg(paths) {
    const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("fill", "none"); s.setAttribute("stroke", "currentColor");
    s.setAttribute("stroke-width", "1.8"); s.setAttribute("stroke-linecap", "round"); s.setAttribute("stroke-linejoin", "round");
    s.setAttribute("aria-hidden", "true");
    s.innerHTML = paths;  // static, trusted markup
    return s;
  }
  const uid = () => Math.random().toString(36).slice(2, 10);
  function distSeg(px, py, x1, y1, x2, y2) {
    const dx = x2 - x1, dy = y2 - y1, l2 = dx * dx + dy * dy;
    const t = l2 ? Math.max(0, Math.min(1, ((px - x1) * dx + (py - y1) * dy) / l2)) : 0;
    return Math.hypot(px - (x1 + t * dx), py - (y1 + t * dy));
  }

  class DrawingTools {
    constructor(ws) {
      this.ws = ws; this.tool = null; this.draft = null; this.sel = null; this.drag = null;
      this.items = this.load(ws.symbol);
      this.layer = new Layer(this);
      ws.candles.attachPrimitive(this.layer);
      this.bar = this.buildBar();
      this.dbar = document.createElement("div");  // the selected drawing's mini toolbar
      this.dbar.className = "ws-dbar"; this.dbar.hidden = true; this.dbar.setAttribute("role", "toolbar");
      this.dbar.setAttribute("aria-label", "Selected drawing");
      ws.el.slot.append(this.dbar);
      this.paneLayers = [];
      const host = ws.el.chart;
      host.addEventListener("pointerdown", (e) => this.down(e), true);
      for (const t of ["mousedown", "touchstart"]) host.addEventListener(t, (e) => { if (this.eat) { e.stopPropagation(); e.preventDefault(); } }, true);
      host.addEventListener("pointermove", (e) => this.move(e), true);
      host.addEventListener("dblclick", (e) => {
        if (this.draft && this.draft.type === "path") { e.stopPropagation(); this.finish(); return; }
        const q = this.point(e), hit = q && !this.tool && this.hitTest(q.x, q.y);
        if (hit && SETTINGS.has(hit.item.type)) { e.stopPropagation(); this.sel = hit.item.id; this.redraw(); this.settings(hit.item); }
      }, true);
      window.addEventListener("pointerup", (e) => this.up(e));
      window.addEventListener("keydown", (e) => this.key(e));
    }

    /* ---- toolbar ----------------------------------------------------------------------------- */
    buildBar() {
      const bar = document.createElement("div");
      bar.className = "ws-draw"; bar.setAttribute("role", "toolbar"); bar.setAttribute("aria-label", "Drawing tools"); bar.setAttribute("aria-orientation", "vertical");
      this.buttons = {};
      for (const t of TOOLS) {
        const b = document.createElement("button");
        b.type = "button"; b.title = t.label; b.setAttribute("aria-label", t.label); b.setAttribute("aria-pressed", "false");
        b.append(svg(t.icon));
        b.addEventListener("click", () => this.pick(this.tool === t.id ? null : t.id));
        this.buttons[t.id] = b; bar.append(b);
      }
      const sep = document.createElement("span"); sep.className = "ws-draw-sep"; bar.append(sep);
      const trade = document.createElement("button");
      trade.type = "button"; trade.className = "ws-trade-btn";
      trade.title = "Trade on Tabdeal: the selected (or last) Long / Short position";
      trade.setAttribute("aria-label", trade.title);
      trade.append(svg(TRADE));
      trade.addEventListener("click", () => { if (this.ws.trading) this.ws.trading.openDialog(this.lastPosition()); });
      bar.append(trade);
      const del = document.createElement("button");
      del.type = "button"; del.title = "Remove all drawings"; del.setAttribute("aria-label", "Remove all drawings");
      del.append(svg(TRASH));
      del.addEventListener("click", async () => {
        const mine = this.items.filter((x) => !(x.trade && x.status));  // an open trade keeps its drawing
        if (!mine.length) return;
        if (!(await ChartUI.confirm(this.ws.root, `Remove ${mine.length} drawing${mine.length > 1 ? "s" : ""} on this market?`, { ok: "Remove", danger: true }))) return;
        this.items = this.items.filter((x) => x.trade && x.status); this.sel = null; this.save(); this.redraw();
      });
      bar.append(del);
      return bar;
    }
    pick(id) {
      this.tool = id; this.draft = null; this.sel = null;
      for (const [k, b] of Object.entries(this.buttons)) b.setAttribute("aria-pressed", String(k === id));
      this.ws.el.chart.classList.toggle("ws-drawing", !!id);
      this.ws.chart.applyOptions({ handleScroll: !id, handleScale: !id });
      this.redraw();
    }

    /* ---- storage ----------------------------------------------------------------------------- */
    load(sym) {
      try { return (JSON.parse(localStorage.getItem(STORE) || "{}")[sym] || []); } catch { return []; }
    }
    save() {
      try {
        const all = JSON.parse(localStorage.getItem(STORE) || "{}");
        all[this.ws.symbol] = this.items;
        localStorage.setItem(STORE, JSON.stringify(all));
      } catch { /* storage unavailable */ }
    }
    setSymbol(sym) { this.pick(null); this.items = this.load(sym); this.redraw(); }
    /** Link a position drawing to every open trade of this market (creating one for a trade
     * opened on Tabdeal), with the exchange's entry, stop and target unless the user moved them. */
    syncTrades(trades) {
      const b = this.ws.bars, step = this.step();
      if (!b.length || this.ws.barsFor !== this.ws.symbol) return;  // right after a market switch
      const open = new Set();
      let changed = false;
      for (const t of trades) {
        if (t.symbol !== this.ws.symbol) continue;
        open.add(t.id);
        const type = t.side === "LONG" ? "long" : "short", dir = type === "long" ? 1 : -1;
        const entry = +(t.status === "ACTIVE" && t.avg_entry ? t.avg_entry : t.entry);
        let it = this.items.find((x) => x.trade === t.id) || (t.drawing_id && this.items.find((x) => x.id === t.drawing_id && !x.trade));
        if (!it) {
          const r = b.slice(-14), atr = r.reduce((s, x) => s + (x.h - x.l), 0) / r.length, risk = 1.5 * atr;
          let t1 = Math.floor(Date.parse(t.filled_at || t.created_at) / 1000);
          const now = b[b.length - 1].t;
          if (!Number.isFinite(t1) || t1 > now + step || t1 < now - 400 * 86400) t1 = now;  // implausible time: start at the current bar
          it = { id: `trade-${t.id}`, type, t1, t2: Math.max(t1 + 20 * step, b[b.length - 1].t + 10 * step), entry,
            sl: t.sl !== null ? +t.sl : entry - dir * risk, tp: t.tp !== null ? +t.tp : entry + dir * 2 * risk };
          this.items.push(it); changed = true;
        }
        const unset = t.sl === null && t.tp === null;
        const was = JSON.stringify([it.trade, it.status, it.unset, it.entry, it.sl, it.tp, it.t2]);
        Object.assign(it, { trade: t.id, status: t.status, origin: t.origin, unset });
        if (!Number.isFinite(it.t1) || it.t1 < b[b.length - 1].t - 400 * 86400) it.t1 = Math.floor(Date.parse(t.filled_at || t.created_at) / 1000) || b[b.length - 1].t;
        if (!(it.t1 > b[b.length - 1].t - 400 * 86400)) it.t1 = b[b.length - 1].t;
        if (!it.dirty) {
          if (t.origin === "TABDEAL" || t.status === "ACTIVE") it.entry = entry;
          if (t.sl !== null) it.sl = +t.sl;
          if (t.tp !== null) it.tp = +t.tp;
        }
        if (t.status === "ACTIVE" && it.t2 < b[b.length - 1].t + 3 * step) it.t2 = b[b.length - 1].t + 10 * step;
        if (JSON.stringify([it.trade, it.status, it.unset, it.entry, it.sl, it.tp, it.t2]) !== was) changed = true;
      }
      // a trade that closed (or was canceled): its drawing goes too, older leftovers included
      const kept = this.items.filter((it) => !it.trade || open.has(it.trade));
      if (kept.length !== this.items.length) {
        if (this.sel && !kept.some((it) => it.id === this.sel)) this.sel = null;
        this.items = kept; changed = true;
      }
      if (changed) { this.save(); this.redraw(); }
    }
    linked(tradeId) { return this.items.find((x) => x.trade === tradeId) || null; }
    /** The selected position drawing, else the last one placed (not one of an open trade). */
    lastPosition() {
      const isPos = (x) => (x.type === "long" || x.type === "short") && !(x.trade && x.status);
      const sel = this.items.find((x) => x.id === this.sel && isPos(x));
      return sel || [...this.items].reverse().find(isPos) || null;
    }
    /** Where price took a position drawing: from the bar that traded through the entry to the
     * stop / target hit (the stop first when one bar reaches both), else to the last bar inside
     * the drawing. null while price has not reached the entry. */
    outcome(it) {
      const b = this.ws.bars, long = it.type === "long";
      if (it.trade && it.status) {  // an open trade: Tabdeal decides its end; shade entry → now
        if (it.status !== "ACTIVE" || !b.length) return null;
        const k = b.findIndex((x) => x.t + this.step() > it.t1), last = b[b.length - 1];
        return k < 0 ? null : { t0: b[k].t, t1: last.t, price: last.c, hit: null };
      }
      let i = b.findIndex((x) => x.t >= it.t1 && x.l <= it.entry && x.h >= it.entry);
      if (i < 0 || b[i].t > it.t2) return null;
      for (let j = i; j < b.length && b[j].t <= it.t2; j++) {
        const x = b[j];
        if (long ? x.l <= it.sl : x.h >= it.sl) return { t0: b[i].t, t1: x.t, price: it.sl, hit: "stop" };
        if (long ? x.h >= it.tp : x.l <= it.tp) return { t0: b[i].t, t1: x.t, price: it.tp, hit: "target" };
      }
      const last = b.filter((x) => x.t <= it.t2).pop();
      return { t0: b[i].t, t1: last.t, price: last.c, hit: null };
    }

    /* ---- coordinates ------------------------------------------------------------------------- */
    step() { return { "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400 }[this.ws.tf]; }
    /** UTC seconds -> x, extrapolated past either end of the loaded bars. */
    x(t) {
      const b = this.ws.bars, n = b.length, ts = this.ws.chart.timeScale();
      if (!n) return null;
      let lg;
      if (t <= b[0].t) lg = (t - b[0].t) / this.step();
      else if (t >= b[n - 1].t) lg = n - 1 + (t - b[n - 1].t) / this.step();
      else {
        let lo = 0, hi = n - 1;
        while (hi - lo > 1) { const m = (lo + hi) >> 1; if (b[m].t <= t) lo = m; else hi = m; }
        lg = lo + (t - b[lo].t) / (b[hi].t - b[lo].t);
      }
      // between two bars (e.g. a fill at 15:20 on 1h bars opening at :30): the library takes
      // whole bar indexes only, so interpolate between the two neighbours
      const i = Math.floor(lg), f = lg - i;
      const a = ts.logicalToCoordinate(i);
      if (a === null || f === 0) return a;
      const c = ts.logicalToCoordinate(i + 1);
      return c === null ? a : a + f * (c - a);
    }
    /** x -> UTC seconds of the nearest bar (past the last bar: whole steps after it). */
    t(x) {
      const b = this.ws.bars, n = b.length;
      const lg = Math.round(this.ws.chart.timeScale().coordinateToLogical(x));
      if (!n || lg === null || Number.isNaN(lg)) return null;
      if (lg < 0) return b[0].t + lg * this.step();
      if (lg > n - 1) return b[n - 1].t + (lg - (n - 1)) * this.step();
      return b[lg].t;
    }
    y(p) { return this.ws.candles.priceToCoordinate(p); }
    p(y) { return this.ws.candles.coordinateToPrice(y); }
    point(e) {
      const r = this.ws.el.chart.getBoundingClientRect();
      const x = e.clientX - r.left, y = e.clientY - r.top;
      const pane = this.ws.chart.panes()[0].getHeight();
      const width = this.ws.chart.timeScale().width();
      if (y < 0 || y > pane || x < 0 || x > width) return null;
      const t = this.t(x), p = this.p(y);
      return t === null || p === null ? null : { x, y, t, p };
    }

    /* ---- creating ---------------------------------------------------------------------------- */
    newItem(type, q) {
      if (type === "hline") return { id: uid(), type, p: q.p };
      if (type === "vline" || type === "avwap") return { id: uid(), type, t: q.t };
      if (type === "long" || type === "short") {
        const b = this.ws.bars.slice(-14);
        const atr = b.length ? b.reduce((s, x) => s + (x.h - x.l), 0) / b.length : q.p * 0.01;
        const risk = 1.5 * atr, dir = type === "long" ? 1 : -1;
        return { id: uid(), type, t1: q.t, t2: q.t + 20 * this.step(), entry: q.p, sl: q.p - dir * risk, tp: q.p + dir * 2 * risk };
      }
      return { id: uid(), type, a: [{ t: q.t, p: q.p }, { t: q.t, p: q.p }] };
    }
    finish() {
      const d = this.draft;
      this.draft = null;
      if (d && d.type === "path") {  // drop the point that followed the mouse, and repeats (double-click)
        d.a.pop();
        d.a = d.a.filter((pt, i) => i === 0 || pt.t !== d.a[i - 1].t || pt.p !== d.a[i - 1].p);
      }
      if (d && (d.type !== "path" || d.a.length >= 2)) { this.items.push(d); this.sel = d.id; this.save(); }
      this.pick(null);
      this.sel = d ? d.id : null;
      this.redraw();
    }

    /* ---- pointer ----------------------------------------------------------------------------- */
    down(e) {
      this.eat = false;
      if (e.button !== 0) return;
      const q = this.point(e);
      if (this.tool) {
        if (!q) return;
        this.eat = true; e.stopPropagation(); e.preventDefault();
        const spec = TOOLS.find((x) => x.id === this.tool);
        if (!this.draft) {
          this.draft = this.newItem(this.tool, q);
          if (spec.points === 1) return this.finish();
          if (this.draft.type === "path") this.draft.a = [{ t: q.t, p: q.p }, { t: q.t, p: q.p }];
          return this.redraw();
        }
        if (this.draft.type === "path") { this.draft.a.push({ t: q.t, p: q.p }); return this.redraw(); }
        return this.finish();  // the second point of a two-point tool
      }
      if (!q) return;
      const hit = this.hitTest(q.x, q.y);
      if (!hit) { if (this.sel) { this.sel = null; this.redraw(); } return; }
      this.eat = true; e.stopPropagation(); e.preventDefault();
      this.sel = hit.item.id;
      const fixed = hit.item.trade && hit.item.status && !["sl", "tp", "right"].includes(hit.handle);
      this.drag = fixed ? null : { item: hit.item, handle: hit.handle, start: q, orig: JSON.parse(JSON.stringify(hit.item)) };
      if (fixed) return this.redraw();
      this.ws.chart.applyOptions({ handleScroll: false, handleScale: false });
      this.redraw();
    }
    move(e) {
      const q = this.point(e);
      if (this.draft && q) {
        if (this.draft.a) this.draft.a[this.draft.a.length - 1] = { t: q.t, p: q.p };
        return this.redraw();
      }
      if (this.drag && q) return this.dragTo(q);
      if (!this.tool && q) this.ws.el.chart.classList.toggle("ws-hover", !!this.hitTest(q.x, q.y));
    }
    up() {
      if (!this.drag) return;
      this.drag = null;
      this.ws.chart.applyOptions({ handleScroll: true, handleScale: true });
      this.save();
    }
    dragTo(q) {
      const { item: it, handle: h, start, orig: o } = this.drag;
      const dt = q.t - start.t, dp = q.p - start.p;
      if (h === "body") {
        if (o.a) it.a = o.a.map((pt) => ({ t: pt.t + dt, p: pt.p + dp }));
        if (o.type === "hline") it.p = o.p + dp;
        if (o.type === "vline") it.t = o.t + dt;
        if (o.type === "avwap") it.t = q.t;  // the anchor follows the pointer, bar by bar
        if (o.entry !== undefined) Object.assign(it, { t1: o.t1 + dt, t2: o.t2 + dt, entry: o.entry + dp, sl: o.sl + dp, tp: o.tp + dp });
      } else if (typeof h === "number") it.a[h] = { t: q.t, p: q.p };
      else if (h === "tp" || h === "sl") it[h] = q.p;
      else if (h === "entry") Object.assign(it, { entry: q.p, t1: q.t });
      else if (h === "right") it.t2 = Math.max(it.t1 + this.step(), q.t);
      else if (h === "anchor") it.t = q.t;
      if (it.trade && it.status && (h === "sl" || h === "tp")) it.dirty = true;  // to send with SL/TP
      this.redraw();
    }
    key(e) {
      const typing = /^(INPUT|TEXTAREA|SELECT)$/.test((document.activeElement || {}).tagName || "");
      if (typing) return;
      if (e.key === "Escape" && (this.tool || this.draft)) { this.pick(null); return; }
      if (e.key === "Enter" && this.draft && this.draft.type === "path") { this.draft.a.push(this.draft.a[this.draft.a.length - 1]); this.finish(); return; }
      if ((e.key === "Delete" || e.key === "Backspace") && this.sel) {
        this.items = this.items.filter((x) => x.id !== this.sel); this.sel = null; this.save(); this.redraw();
      }
    }

    /* ---- geometry ---------------------------------------------------------------------------- */
    /** Screen geometry of a drawing: { handles: [[key, x, y]], segs: [[x1,y1,x2,y2]], rects: [[x1,y1,x2,y2]] } */
    geom(it) {
      const g = { handles: [], segs: [], rects: [] };
      if (it.type === "vline") {
        const x = this.x(it.t), hgt = this.ws.chart.panes()[0].getHeight();
        if (x === null) return g;
        g.segs.push([x, 0, x, hgt]); g.handles.push(["body", x, hgt / 2]);
        return g;
      }
      if (it.type === "avwap") {
        const b = this.ws.bars, i0 = b.findIndex((x) => x.t + this.step() > it.t);
        if (i0 < 0) return g;
        const v = window.ChartIndicators.anchoredVwap(b, i0);
        let prev = null;
        for (let i = i0; i < b.length; i++) {
          const pt = [this.x(b[i].t), this.y(v[i])];
          if (pt[0] === null || pt[1] === null) continue;
          if (prev) g.segs.push([...prev, ...pt]);
          prev = pt;
        }
        const ax = this.x(b[i0].t), ay = this.y(v[i0]);
        if (ax !== null && ay !== null) g.handles.push(["anchor", ax, ay]);
        g.vwap = v[b.length - 1];
        return g;
      }
      if (it.type === "hline") {
        const y = this.y(it.p), w = this.ws.chart.timeScale().width();
        if (y === null) return g;
        g.segs.push([0, y, w, y]); g.handles.push(["body", w / 2, y]);
        return g;
      }
      if (it.entry !== undefined) {
        const x1 = this.x(it.t1), x2 = this.x(it.t2), ye = this.y(it.entry), yt = this.y(it.tp), ys = this.y(it.sl);
        if ([x1, x2, ye, yt, ys].some((v) => v === null)) return g;
        g.rects.push([x1, Math.min(ye, yt), x2, Math.max(ye, yt)], [x1, Math.min(ye, ys), x2, Math.max(ye, ys)]);
        g.handles.push(["tp", x1, yt], ["sl", x1, ys], ["entry", x1, ye], ["right", x2, ye]);
        return g;
      }
      const pts = it.a.map((pt) => [this.x(pt.t), this.y(pt.p)]);
      if (pts.some(([x, y]) => x === null || y === null)) return g;
      if (it.type === "range" || it.type === "daterange") {
        const [[x1, y1], [x2, y2]] = pts;
        g.rects.push([Math.min(x1, x2), Math.min(y1, y2), Math.max(x1, x2), Math.max(y1, y2)]);
      } else if (it.type === "fib") {
        const s = st(it), [[x1], [x2]] = pts, [a, b] = it.a, l = Math.min(x1, x2);
        const r = s.extend ? Math.max(Math.max(x1, x2), this.ws.chart.timeScale().width()) : Math.max(x1, x2);
        const [lo, hi] = s.reverse ? [a.p, b.p] : [b.p, a.p];  // level 0 at the second point (reverse: the first)
        g.levels = s.levels.filter((x) => x.on).sort((p, q) => p.v - q.v)
          .map((x) => { const p = lo + (hi - lo) * x.v; return [x.v, p, this.y(p), x.color]; });
        for (const [, , y] of g.levels) if (y !== null) g.segs.push([l, y, r, y]);
        g.diag = [...pts[0], ...pts[1]]; g.left = l; g.right = r;
      } else for (let i = 1; i < pts.length; i++) g.segs.push([...pts[i - 1], ...pts[i]]);
      pts.forEach(([x, y], i) => g.handles.push([i, x, y]));
      return g;
    }
    hitTest(x, y) {
      for (let k = this.items.length - 1; k >= 0; k--) {  // the newest on top
        const it = this.items[k], g = this.geom(it);
        const hd = g.handles.find(([key, hx, hy]) => key !== "body" && Math.hypot(x - hx, y - hy) <= HIT);
        if (hd && it.id === this.sel) return { item: it, handle: hd[0] };
        if (g.segs.some((s) => distSeg(x, y, ...s) <= HIT) || g.rects.some(([a, b, c, d]) => x >= a && x <= c && y >= b && y <= d)) {
          return { item: it, handle: hd ? hd[0] : "body" };
        }
      }
      return null;
    }
    redraw() {
      this.layer.update();
      for (const l of this.paneLayers) l.update();
      this.syncDbar();
    }
    lineHex() { return hex(css("--c-fvg-bull")); }
    remove(id) {
      this.items = this.items.filter((x) => x.id !== id);
      if (this.sel === id) this.sel = null;
      this.save(); this.redraw();
    }
    /** Vertical lines across the indicator panes too: a layer on each pane's first series. */
    attachPanes() {
      this.paneLayers = [];
      this.ws.chart.panes().forEach((pane, i) => {
        const s = i > 0 && pane.getSeries()[0];
        if (!s) return;
        const l = new PaneLayer(this);
        s.attachPrimitive(l);
        this.paneLayers.push(l);
      });
    }

    /* ---- the selected drawing: mini toolbar and settings --------------------------------------- */
    styleControls(s, apply) {
      const H = ChartUI.h;
      return [
        H("select", { title: "Line width", "aria-label": "Line width", onchange: (e) => apply({ width: +e.target.value }) },
          [1, 2, 3, 4].map((w) => H("option", { value: w, selected: s.width === w }, `${w}px`))),
        H("select", { title: "Line style", "aria-label": "Line style", onchange: (e) => apply({ style: e.target.value }) },
          [["solid", "───"], ["dashed", "- - -"], ["dotted", "·····"]].map(([v, t]) => H("option", { value: v, selected: s.style === v }, t))),
      ];
    }
    syncDbar() {
      const it = this.items.find((x) => x.id === this.sel);
      if (!it || this.tool || this.draft || (it.trade && it.status)) { this.dbar.hidden = true; this.dbarFor = null; return; }
      this.dbar.hidden = false;
      if (this.dbarFor === it.id) return;  // rebuilt only when the selection changes (keeps focus)
      this.dbarFor = it.id;
      const H = ChartUI.h, s = st(it);
      const apply = (patch) => { it.st = { ...(it.st || {}), ...patch }; this.save(); this.layer.update(); for (const l of this.paneLayers) l.update(); };
      const kids = [H("span", { class: "ws-dbar-name" }, NAMES[it.type] || it.type)];
      if (COLORED.has(it.type)) kids.push(H("input", { type: "color", title: "Colour", "aria-label": "Colour", value: s.color || this.lineHex(), oninput: (e) => apply({ color: e.target.value }) }));
      if (LINED.has(it.type)) kids.push(...this.styleControls(s, apply));
      if (SETTINGS.has(it.type)) kids.push(H("button", { type: "button", title: "Settings", "aria-label": "Drawing settings", onclick: () => this.settings(it) }, "⚙"));
      kids.push(H("button", { type: "button", title: "Delete (Del)", "aria-label": "Delete drawing", onclick: () => this.remove(it.id) }, "🗑"));
      this.dbar.replaceChildren(...kids);
    }
    /** The drawing's settings window (double-click a drawing, or ⚙ on its toolbar). */
    settings(it) {
      const H = ChartUI.h, s = st(it);
      const apply = (patch) => {
        it.st = { ...(it.st || {}), ...patch }; this.save();
        this.dbarFor = null; this.redraw();
      };
      const row = (label, ...kids) => H("div", { class: "ws-set-row" }, H("span", {}, label), ...kids);
      const check = (k, text) => H("label", {}, H("input", { type: "checkbox", checked: !!s[k], onchange: (e) => apply({ [k]: e.target.checked }) }), ` ${text}`);
      const parts = [H("h2", {}, `${NAMES[it.type]} — settings`)];
      if (it.type !== "fib") parts.push(row("Colour", H("input", { type: "color", value: s.color || this.lineHex(), oninput: (e) => apply({ color: e.target.value }) })));
      parts.push(row("Line", ...this.styleControls(s, apply)));
      if (it.type === "vline") parts.push(row("Label", check("label", "date and time at the bottom")));
      if (it.type === "hline") parts.push(row("Label", check("label", "price at the right")));
      if (it.type === "avwap") parts.push(row("Label", check("label", "AVWAP value at the end")));
      if (it.type === "fib") {
        const levels = s.levels.map((x) => ({ ...x }));
        const setLevels = () => apply({ levels: levels.map((x) => ({ ...x })) });
        parts.push(H("div", { class: "ws-fib-levels" }, levels.map((x) => H("label", {},
          H("input", { type: "checkbox", checked: x.on, "aria-label": `Level ${x.v}`, onchange: (e) => { x.on = e.target.checked; setLevels(); } }),
          H("input", { type: "number", step: "0.001", value: x.v, "aria-label": "Level value", onchange: (e) => { const v = Number(e.target.value); if (Number.isFinite(v)) { x.v = v; setLevels(); } } }),
          H("input", { type: "color", value: x.color, "aria-label": "Level colour", oninput: (e) => { x.color = e.target.value; setLevels(); } })))));
        parts.push(row("Show", check("labels", "levels"), check("prices", "prices"), check("fill", "background")));
        parts.push(row("Lines", check("extend", "extend to the right"), check("reverse", "reverse (0 ↔ 1)")));
      }
      const m = ChartUI.open(this.ws.root, H("div", { class: "ws-set" }, parts,
        H("div", { class: "ws-dlg-actions" },
          H("button", { class: "btn", type: "button", onclick: () => { delete it.st; this.save(); this.dbarFor = null; this.redraw(); m.close(); } }, "Defaults"),
          H("button", { class: "btn ws-primary", type: "button", onclick: () => m.close() }, "Done"))), { label: "Drawing settings" });
    }
  }

  /* ---- rendering ------------------------------------------------------------------------------- */
  class Layer {
    constructor(tools) { this.tools = tools; this.view = { zOrder: () => "top", renderer: () => ({ draw: (t) => this.draw(t) }) }; }
    attached(p) { this.request = p.requestUpdate; }
    detached() { this.request = null; }
    update() { if (this.request) this.request(); }
    updateAllViews() {}
    paneViews() { return [this.view]; }
    draw(target) {
      const T = this.tools;
      const col = { line: `rgb(${css("--c-fvg-bull")})`, bull: css("--c-bull"), bear: css("--c-bear"), text: css("--c-text"), surface: css("--surface") };
      const mf = T.ws.format && T.ws.format(T.ws.symbol);  // the market's precision (BTC 1, XRP 5)
      const fmt = (v) => (mf ? v.toFixed(mf.precision) : Math.abs(v) >= 1000 ? v.toFixed(1) : Math.abs(v) >= 1 ? v.toFixed(4) : v.toPrecision(5));
      target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
        ctx.font = '500 11px "IBM Plex Sans", sans-serif'; ctx.textBaseline = "middle";
        const label = (text, x, y, color, align) => {
          const w = ctx.measureText(text).width + 10;
          const lx = align === "center" ? x - w / 2 : align === "right" ? x - w : x;
          ctx.fillStyle = color; ctx.fillRect(lx, y - 9, w, 18);
          ctx.fillStyle = "#fff"; ctx.textAlign = "left"; ctx.fillText(text, lx + 5, y);
        };
        const items = T.draft ? [...T.items, T.draft] : T.items;
        for (const it of items) {
          const g = T.geom(it), selected = it.id === T.sel || it === T.draft;
          if (it.entry !== undefined) {
            const long = it.type === "long";
            const [rt, rs] = g.rects;
            if (!rt) continue;
            ctx.fillStyle = `rgba(${col.bull},0.18)`; ctx.fillRect(rt[0], rt[1], rt[2] - rt[0], rt[3] - rt[1]);
            ctx.fillStyle = `rgba(${col.bear},0.18)`; ctx.fillRect(rs[0], rs[1], rs[2] - rs[0], rs[3] - rs[1]);
            const ye = T.y(it.entry), x1 = rt[0], x2 = rt[2];
            const out = it === T.draft ? null : T.outcome(it);
            let pnl = "";
            if (out) {  // the part price has covered: darker green in profit, darker red at a loss
              const up = long ? out.price >= it.entry : out.price <= it.entry;
              const ox1 = x1, ox2 = x2, yp = T.y(out.price);  // the whole width, entry → price line
              if (yp !== null && ox2 > ox1) {
                ctx.fillStyle = `rgba(${up ? col.bull : col.bear},0.42)`;
                ctx.fillRect(ox1, Math.min(ye, yp), ox2 - ox1, Math.abs(yp - ye));
              }
              const pct = ((out.price - it.entry) / it.entry) * 100 * (long ? 1 : -1);
              pnl = ` · ${out.hit ? (out.hit === "target" ? "Target hit " : "Stopped ") : ""}${pct >= 0 ? "+" : "−"}${Math.abs(pct).toFixed(2)}%`;
            }
            ctx.strokeStyle = `rgba(${col.text},0.6)`; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(x1, ye); ctx.lineTo(x2, ye); ctx.stroke();
            const reward = Math.abs(it.tp - it.entry), risk = Math.abs(it.entry - it.sl);
            const pct = (v) => `${((v / it.entry) * 100).toFixed(2)}%`;
            const vx1 = Math.max(x1, 0), vx2 = Math.min(x2, T.ws.chart.timeScale().width());
            const cx = vx2 > vx1 ? (vx1 + vx2) / 2 : (x1 + x2) / 2;  // labels on the visible part
            // labels only while the drawing is selected (clicked) or being placed
            if (selected) label(`Target ${fmt(it.tp)} (${long ? "+" : "−"}${pct(reward)})`, cx, T.y(it.tp) + (long ? -11 : 11), `rgba(${col.bull},0.95)`, "center");
            if (selected) label(`Stop ${fmt(it.sl)} (${long ? "−" : "+"}${pct(risk)})`, cx, T.y(it.sl) + (long ? 11 : -11), `rgba(${col.bear},0.95)`, "center");
            if (it.trade && it.status && (it.unset || it.dirty)) {  // not (yet) the stop / target on Tabdeal
              ctx.save(); ctx.setLineDash([5, 4]); ctx.lineWidth = 1.2;
              ctx.strokeStyle = `rgba(${col.bull},0.9)`; ctx.strokeRect(rt[0], rt[1], rt[2] - rt[0], rt[3] - rt[1]);
              ctx.strokeStyle = `rgba(${col.bear},0.9)`; ctx.strokeRect(rs[0], rs[1], rs[2] - rs[0], rs[3] - rs[1]);
              ctx.restore();
            }
            const tag = it.trade && it.status ? `#${it.trade} ${it.status === "ACTIVE" ? "open" : "pending"}${it.origin === "TABDEAL" ? " (Tabdeal)" : ""} · ` : "";
            if (selected) label(`${tag}${long ? "Long" : "Short"} ${fmt(it.entry)} · R:R ${risk ? (reward / risk).toFixed(2) : "—"}${pnl}`, cx, ye, it.trade && it.status ? "rgba(70,90,150,0.95)" : "rgba(90,98,110,0.95)", "center");
            if (selected && it.trade && it.status && (it.unset || it.dirty)) {
              label(it.dirty ? "Moved: press SL/TP below to apply on Tabdeal" : "No stop / target on Tabdeal: drag them, then SL/TP", Math.max(x1, 0) + 4, Math.min(rt[1], rs[1]) - 34, "rgba(196,118,0,0.95)", "left");
            }
          } else if (it.type === "daterange") {
            const r = g.rects[0];
            if (!r) continue;
            const s = st(it), [a, b] = it.a, fwd = b.t >= a.t, c = s.color || col.line;
            ctx.fillStyle = s.color ? rgba(s.color, 0.12) : c.replace("rgb(", "rgba(").replace(")", ",0.12)"); ctx.fillRect(r[0], r[1], r[2] - r[0], r[3] - r[1]);
            ctx.strokeStyle = c; ctx.lineWidth = 1.2;
            const my = (r[1] + r[3]) / 2, xa = T.x(a.t), xb = T.x(b.t);
            ctx.beginPath(); ctx.moveTo(xa, my); ctx.lineTo(xb, my); ctx.stroke();
            ctx.beginPath(); ctx.moveTo(xb + (fwd ? -6 : 6), my - 5); ctx.lineTo(xb, my); ctx.lineTo(xb + (fwd ? -6 : 6), my + 5); ctx.stroke();
            const secs = Math.abs(b.t - a.t), bars = Math.round(secs / T.step());
            const d = Math.floor(secs / 86400), hh = Math.floor((secs % 86400) / 3600), mm = Math.floor((secs % 3600) / 60);
            const dur = [d ? `${d}d` : "", hh ? `${hh}h` : "", mm && !d ? `${mm}m` : ""].filter(Boolean).join(" ") || "0m";
            const vol = T.ws.bars.filter((x) => x.t >= Math.min(a.t, b.t) && x.t <= Math.max(a.t, b.t)).reduce((s, x) => s + (x.v || 0), 0);
            label(`${bars} bars · ${dur}${vol ? ` · vol ${vol >= 1000 ? (vol / 1000).toFixed(1) + "K" : vol.toFixed(2)}` : ""}`, (r[0] + r[2]) / 2, r[3] + 12, c, "center");
          } else if (it.type === "fib") {
            if (!g.levels) continue;
            const s = st(it), l = g.left, rr = g.right;
            if (s.fill) {
              for (let k = 0; k < g.levels.length - 1; k++) {  // a light band between two levels
                const y1 = g.levels[k][2], y2 = g.levels[k + 1][2];
                if (y1 === null || y2 === null) continue;
                ctx.fillStyle = rgba(g.levels[k + 1][3], 0.08); ctx.fillRect(l, Math.min(y1, y2), rr - l, Math.abs(y2 - y1));
              }
            }
            ctx.save(); ctx.setLineDash(DASH[s.style] || []);
            g.levels.forEach(([f, p, y, color]) => {
              if (y === null) return;
              ctx.strokeStyle = rgba(color, 0.95); ctx.lineWidth = s.width;
              ctx.beginPath(); ctx.moveTo(l, y); ctx.lineTo(rr, y); ctx.stroke();
              const txt = s.labels && s.prices ? `${f} (${fmt(p)})` : s.labels ? `${f}` : s.prices ? fmt(p) : "";
              if (txt) { ctx.fillStyle = color; ctx.textAlign = "right"; ctx.fillText(txt, l - 4, y); }
            });
            ctx.restore();
            ctx.save(); ctx.setLineDash([4, 4]); ctx.strokeStyle = `rgba(${col.text},0.45)`; ctx.lineWidth = 1;
            ctx.beginPath(); ctx.moveTo(g.diag[0], g.diag[1]); ctx.lineTo(g.diag[2], g.diag[3]); ctx.stroke(); ctx.restore();
          } else if (it.type === "avwap") {
            const s = st(it);
            ctx.save(); ctx.strokeStyle = s.color; ctx.lineWidth = s.width; ctx.setLineDash(DASH[s.style] || []);
            ctx.beginPath();
            g.segs.forEach(([x1, y1, x2, y2], k) => { if (k === 0) ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); });
            ctx.stroke(); ctx.restore();
            if (s.label && g.segs.length && g.vwap !== undefined && g.vwap !== null) {
              const [, , xe, ye2] = g.segs[g.segs.length - 1];
              label(`AVWAP ${fmt(g.vwap)}`, xe - 4, ye2 - 14, s.color, "right");
            }
          } else if (it.type === "range") {
            const r = g.rects[0];
            if (!r) continue;
            const [a, b] = it.a, up = b.p >= a.p;
            const c = up ? col.bull : col.bear;
            ctx.fillStyle = `rgba(${c},0.14)`; ctx.fillRect(r[0], r[1], r[2] - r[0], r[3] - r[1]);
            ctx.strokeStyle = `rgba(${c},0.9)`; ctx.lineWidth = 1.2;
            const mx = (r[0] + r[2]) / 2, y1 = T.y(a.p), y2 = T.y(b.p);
            ctx.beginPath(); ctx.moveTo(mx, y1); ctx.lineTo(mx, y2); ctx.stroke();
            ctx.beginPath(); ctx.moveTo(mx - 5, y2 + (up ? 6 : -6)); ctx.lineTo(mx, y2); ctx.lineTo(mx + 5, y2 + (up ? 6 : -6)); ctx.stroke();
            const bars = Math.round(Math.abs(b.t - a.t) / T.step());
            label(`${up ? "+" : "−"}${fmt(Math.abs(b.p - a.p))} (${(((b.p - a.p) / a.p) * 100).toFixed(2)}%) · ${bars} bars`, mx, up ? r[1] - 12 : r[3] + 12, `rgba(${c},0.95)`, "center");
          } else {
            const s = st(it), lc = s.color || col.line;
            ctx.save(); ctx.strokeStyle = lc; ctx.lineWidth = s.width; ctx.setLineDash(DASH[s.style] || []);
            for (const [x1, y1, x2, y2] of g.segs) { ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke(); }
            ctx.restore(); ctx.strokeStyle = lc; ctx.lineWidth = s.width;
            if (it.type === "path" && g.segs.length) {  // arrow at the end
              const [x1, y1, x2, y2] = g.segs[g.segs.length - 1], ang = Math.atan2(y2 - y1, x2 - x1);
              ctx.beginPath(); ctx.moveTo(x2, y2); ctx.lineTo(x2 - 9 * Math.cos(ang - 0.45), y2 - 9 * Math.sin(ang - 0.45));
              ctx.moveTo(x2, y2); ctx.lineTo(x2 - 9 * Math.cos(ang + 0.45), y2 - 9 * Math.sin(ang + 0.45)); ctx.stroke();
            }
            if (it.type === "hline" && s.label && g.segs.length) label(fmt(it.p), mediaSize.width - 2, g.segs[0][1], lc, "right");
            if (it.type === "vline" && s.label && g.segs.length) label(VDATE.format(new Date(it.t * 1000)), g.segs[0][0], mediaSize.height - 12, lc, "center");
          }
          if (selected) {
            for (const [key, hx, hy] of g.handles) {
              if (key === "body" && it.type !== "hline" && it.type !== "vline") continue;
              ctx.fillStyle = col.surface; ctx.strokeStyle = col.line; ctx.lineWidth = 1.5;
              ctx.beginPath(); ctx.arc(hx, hy, 4.5, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
            }
          }
        }
      });
    }
  }
  /** Vertical lines on an indicator pane (the price pane draws them with everything else). */
  class PaneLayer {
    constructor(tools) { this.tools = tools; this.view = { zOrder: () => "top", renderer: () => ({ draw: (t) => this.draw(t) }) }; }
    attached(p) { this.request = p.requestUpdate; }
    detached() { this.request = null; }
    update() { if (this.request) this.request(); }
    updateAllViews() {}
    paneViews() { return [this.view]; }
    draw(target) {
      const T = this.tools, line = `rgb(${css("--c-fvg-bull")})`;
      target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
        for (const it of T.items) {
          if (it.type !== "vline") continue;
          const x = T.x(it.t);
          if (x === null) continue;
          const s = st(it);
          ctx.save(); ctx.strokeStyle = s.color || line; ctx.lineWidth = s.width; ctx.setLineDash(DASH[s.style] || []);
          ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, mediaSize.height); ctx.stroke(); ctx.restore();
        }
      });
    }
  }
  window.ChartDrawingTools = DrawingTools;
  window.ChartDrawingToolList = TOOLS.map((t) => t.id);
})();
