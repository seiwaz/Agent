/* Chart workspace — drawing tools (left toolbar): trend line, horizontal line, long / short
 * position, price range and path. Display only: a position drawing is a measurement, it never
 * creates an order.
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
  ];
  const TRASH = '<path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M6 6l1 14h10l1-14"/>';

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
      const host = ws.el.chart;
      host.addEventListener("pointerdown", (e) => this.down(e), true);
      for (const t of ["mousedown", "touchstart"]) host.addEventListener(t, (e) => { if (this.eat) { e.stopPropagation(); e.preventDefault(); } }, true);
      host.addEventListener("pointermove", (e) => this.move(e), true);
      host.addEventListener("dblclick", (e) => { if (this.draft && this.draft.type === "path") { e.stopPropagation(); this.finish(); } }, true);
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
      const del = document.createElement("button");
      del.type = "button"; del.title = "Remove all drawings"; del.setAttribute("aria-label", "Remove all drawings");
      del.append(svg(TRASH));
      del.addEventListener("click", () => { if (this.items.length && confirm("Remove every drawing on this market?")) { this.items = []; this.sel = null; this.save(); this.redraw(); } });
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
      return ts.logicalToCoordinate(lg);
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
      this.drag = { item: hit.item, handle: hit.handle, start: q, orig: JSON.parse(JSON.stringify(hit.item)) };
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
        if (o.entry !== undefined) Object.assign(it, { t1: o.t1 + dt, t2: o.t2 + dt, entry: o.entry + dp, sl: o.sl + dp, tp: o.tp + dp });
      } else if (typeof h === "number") it.a[h] = { t: q.t, p: q.p };
      else if (h === "tp" || h === "sl") it[h] = q.p;
      else if (h === "entry") Object.assign(it, { entry: q.p, t1: q.t });
      else if (h === "right") it.t2 = Math.max(it.t1 + this.step(), q.t);
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
      if (it.type === "range") {
        const [[x1, y1], [x2, y2]] = pts;
        g.rects.push([Math.min(x1, x2), Math.min(y1, y2), Math.max(x1, x2), Math.max(y1, y2)]);
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
    redraw() { this.layer.update(); }
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
      const fmt = (v) => (Math.abs(v) >= 1000 ? v.toFixed(1) : Math.abs(v) >= 1 ? v.toFixed(4) : v.toPrecision(5));
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
            ctx.strokeStyle = `rgba(${col.text},0.6)`; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(x1, ye); ctx.lineTo(x2, ye); ctx.stroke();
            const reward = Math.abs(it.tp - it.entry), risk = Math.abs(it.entry - it.sl);
            const pct = (v) => `${((v / it.entry) * 100).toFixed(2)}%`;
            const cx = (x1 + x2) / 2;
            label(`Target ${fmt(it.tp)} (${long ? "+" : "−"}${pct(reward)})`, cx, T.y(it.tp) + (long ? -11 : 11), `rgba(${col.bull},0.95)`, "center");
            label(`Stop ${fmt(it.sl)} (${long ? "−" : "+"}${pct(risk)})`, cx, T.y(it.sl) + (long ? 11 : -11), `rgba(${col.bear},0.95)`, "center");
            label(`${long ? "Long" : "Short"} ${fmt(it.entry)} · R:R ${risk ? (reward / risk).toFixed(2) : "—"}`, cx, ye, "rgba(90,98,110,0.95)", "center");
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
            ctx.strokeStyle = col.line; ctx.lineWidth = 1.8;
            for (const [x1, y1, x2, y2] of g.segs) { ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke(); }
            if (it.type === "path" && g.segs.length) {  // arrow at the end
              const [x1, y1, x2, y2] = g.segs[g.segs.length - 1], ang = Math.atan2(y2 - y1, x2 - x1);
              ctx.beginPath(); ctx.moveTo(x2, y2); ctx.lineTo(x2 - 9 * Math.cos(ang - 0.45), y2 - 9 * Math.sin(ang - 0.45));
              ctx.moveTo(x2, y2); ctx.lineTo(x2 - 9 * Math.cos(ang + 0.45), y2 - 9 * Math.sin(ang + 0.45)); ctx.stroke();
            }
            if (it.type === "hline" && g.segs.length) label(fmt(it.p), mediaSize.width - 2, g.segs[0][1], col.line, "right");
          }
          if (selected) {
            for (const [key, hx, hy] of g.handles) {
              if (key === "body" && it.type !== "hline") continue;
              ctx.fillStyle = col.surface; ctx.strokeStyle = col.line; ctx.lineWidth = 1.5;
              ctx.beginPath(); ctx.arc(hx, hy, 4.5, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
            }
          }
        }
      });
    }
  }
  window.ChartDrawingTools = DrawingTools;
  window.ChartDrawingToolList = TOOLS.map((t) => t.id);
})();
