/* Chart workspace — the chart, its toolbar and its options; trading from position drawings is in
 * web/chart/trading.js (the panel under the chart).
 *
 *   const ws = new ChartWorkspace(rootElement, { api, symbol, display });
 *   ws.setSymbol("XRPUSDT"); ws.setTrends(list); ws.applyMinute(m); ws.restyle(); ws.focusTime(iso)
 *
 * Candles come from Tabdeal's own chart feed (through /api/chart/candles): the newest page on
 * load, the forming bar every TAIL_EVERY_MS, and older pages as the view is scrolled left, back
 * to the market's listing. Everything the user can switch on or off comes from
 * window.ChartFeatures (one entry per feature). Structure and level features are computed by the
 * server over every loaded closed bar (/api/chart/overlays); indicators are computed here.
 * Switching the timeframe re-applies every active feature to the new timeframe. The options and
 * their settings are kept in localStorage. */
"use strict";
(function () {
  const { FEATURES, GROUPS, STRUCTURE } = window.ChartFeatures;
  const { OverlayLayer } = window.ChartDraw;
  const TFS = ["5m", "15m", "1h", "4h", "1d"];
  const TF_SEC = { "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400 };
  const BARS = 500;
  const STORE = "smc-chart-v1";
  const OVERLAY_EVERY_MS = 60000;
  const TAIL_EVERY_MS = 10000;  // the forming bar from Tabdeal when no live stream feeds it
  const OLDER_AT = 30;  // load older bars once fewer than this many are left of the view

  const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const h = (tag, attrs, ...kids) => {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
      else if (v !== null && v !== undefined && v !== false) e.setAttribute(k, v === true ? "" : v);
    }
    for (const k of kids.flat()) if (k !== null && k !== undefined && k !== false) e.append(k);
    return e;
  };
  const bar = (b) => ({ t: b.t, o: b.o, h: b.h, l: b.l, c: b.c, v: b.v || 0 });
  const fmtOf = (o) => new Intl.DateTimeFormat(undefined, { hourCycle: "h23", ...o });
  const LOCAL = {  // axis and crosshair in the browser's time zone
    long: fmtOf({ year: "2-digit", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" }),
    year: fmtOf({ year: "numeric" }), month: fmtOf({ month: "short" }), day: fmtOf({ day: "numeric" }),
    time: fmtOf({ hour: "2-digit", minute: "2-digit" }),
    readout: fmtOf({ year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }),
  };
  const defaults = (settings) => Object.fromEntries((settings || []).map((x) => [x.key, x.def]));

  class ChartWorkspace {
    constructor(root, opts) {
      this.root = root; this.api = opts.api; this.symbol = opts.symbol; this.display = opts.display || ((s) => s);
      this.format = opts.format || (() => null);  // symbol -> { precision, minMove } of its price
      this.cfg = this.load();
      this.tf = TFS.includes(this.cfg.tf) ? this.cfg.tf : "1h";
      this.bars = []; this.overlays = null; this.handles = []; this.trends = [];
      this.build();
      this.chart = LightweightCharts.createChart(this.el.chart, { autoSize: true, ...this.theme() });
      this.candles = this.chart.addSeries(LightweightCharts.CandlestickSeries, this.candleColors(), 0);
      this.applyFormat();
      this.layer = new OverlayLayer();
      this.candles.attachPrimitive(this.layer);
      this.chart.subscribeCrosshairMove((p) => this.readout(p));
      this.tools = new ChartDrawingTools(this);
      this.el.slot.prepend(this.tools.bar);
      this.trading = window.ChartTrading ? new ChartTrading(this) : null;
      document.addEventListener("fullscreenchange", () => this.renderFullBtn());
      this.timer = setInterval(() => this.refreshOverlays(), OVERLAY_EVERY_MS);
      this.tailTimer = setInterval(() => this.refreshTail(), TAIL_EVERY_MS);
      this.chart.timeScale().subscribeVisibleLogicalRangeChange((r) => { if (r && r.from < OLDER_AT) this.loadOlder(); });
      this.reload(false);
    }

    /* ---- settings ---------------------------------------------------------------------------- */
    load() {
      let saved = {};
      try { saved = JSON.parse(localStorage.getItem(STORE) || "{}"); } catch { /* storage unavailable */ }
      const f = {};
      for (const x of FEATURES) {
        const s = (saved.features || {})[x.id] || {};
        f[x.id] = { on: s.on === undefined ? x.on : !!s.on, s: { ...defaults(x.settings), ...(s.s || {}) } };
      }
      return { tf: saved.tf, features: f, structure: { ...defaults(STRUCTURE.settings), ...(saved.structure || {}) } };
    }
    save() {
      this.cfg.tf = this.tf;
      try { localStorage.setItem(STORE, JSON.stringify(this.cfg)); } catch { /* storage unavailable */ }
    }
    on(id) { return this.cfg.features[id].on; }

    /* ---- DOM --------------------------------------------------------------------------------- */
    build() {
      const el = {};
      el.tfs = h("div", { class: "seg ws-tfs", role: "tablist", "aria-label": "Timeframe" });
      el.trends = h("div", { class: "ws-trends", "aria-label": "Trend per timeframe" });
      el.layersBtn = h("button", { class: "btn ws-layers-btn", type: "button", "aria-expanded": "false", "aria-controls": "ws-panel",
        onclick: () => this.togglePanel() }, "Indicators & layers");
      el.price = h("span", { class: "ws-price num" }, "—");
      el.title = h("span", { class: "ws-title" }, "—");
      el.full = h("button", { class: "icon-btn", type: "button", "aria-pressed": "false", title: "Full screen (Esc to leave)", onclick: () => this.toggleFull() });
      el.bar = h("div", { class: "ws-bar" },
        h("div", { class: "ws-head" }, el.title, el.price), el.tfs, el.trends, h("div", { class: "ws-spacer" }), el.layersBtn, el.full);
      el.panel = h("div", { class: "ws-panel", id: "ws-panel", hidden: true });
      el.chart = h("div", { class: "ws-chart", role: "img", "aria-label": "Candlestick chart with the selected structure, levels and indicators" });
      el.readout = h("div", { class: "ws-readout num small", "aria-live": "off" });
      el.note = h("div", { class: "ws-note", hidden: true });
      el.slot = h("div", { class: "ws-slot" }, el.chart, el.readout, el.note);
      this.root.replaceChildren(el.bar, el.panel, el.slot);
      this.el = el;
      this.renderTfs(); this.renderPanel(); this.renderFullBtn(); this.renderTitle();
    }
    renderTitle() { this.el.title.textContent = `${this.display(this.symbol)} · ${this.tf}`; }
    renderTfs() {
      this.el.tfs.replaceChildren(...TFS.map((tf) => h("button", { type: "button", role: "tab", "aria-selected": String(tf === this.tf),
        onclick: () => this.setTf(tf) }, tf)));
    }
    setTrends(list) {
      this.trends = list || [];
      const by = Object.fromEntries(this.trends.map((x) => [x.tf, x.trend]));
      this.el.trends.replaceChildren(...TFS.map((tf) => trendChip(tf, by[tf])));
    }
    renderPanel() {
      const p = this.el.panel;
      const groups = GROUPS.map((g) => {
        const items = FEATURES.filter((x) => x.group === g).map((x) => this.featureRow(x));
        const head = h("div", { class: "ws-group-head" }, h("h3", {}, g),
          g === "Structure" ? this.settingsToggle(STRUCTURE, () => this.structureForm()) : null);
        return h("section", { class: "ws-group" }, head, ...items);
      });
      p.replaceChildren(...groups);
    }
    featureRow(x) {
      const st = this.cfg.features[x.id];
      const box = h("input", { type: "checkbox", id: `ws-f-${x.id}`, checked: st.on, onchange: (e) => {
        st.on = e.target.checked; this.save();
        if (x.server) this.redraw(); else this.rebuildIndicators();
      } });
      const row = h("div", { class: "ws-row" }, h("label", { for: `ws-f-${x.id}` }, box, x.label),
        x.settings && x.settings.length ? this.settingsToggle(x, () => this.featureForm(x)) : null);
      return h("div", { class: "ws-item" }, row, h("div", { class: "ws-form", id: `ws-form-${x.id}`, hidden: true }));
    }
    settingsToggle(x, form) {
      return h("button", { class: "ws-gear", type: "button", "aria-label": `${x.label} settings`, title: "Settings",
        onclick: () => {
          const box = this.root.querySelector(`#ws-form-${x.id}`);
          if (!box) return;
          box.hidden = !box.hidden;
          if (!box.hidden) box.replaceChildren(form());
        } }, "⚙");
    }
    fields(settings, values, onChange) {
      const input = (f) => (f.type === "bool"
        ? h("input", { type: "checkbox", checked: !!values[f.key], onchange: (e) => { values[f.key] = e.target.checked; this.save(); onChange(); } })
        : h("input", { type: "number", value: values[f.key], min: f.min, max: f.max, step: f.step || 1, onchange: (e) => {
          let v = Number(e.target.value);
          if (!Number.isFinite(v)) v = f.def;
          v = Math.min(f.max, Math.max(f.min, v));
          e.target.value = v; values[f.key] = v; this.save(); onChange();
        } }));
      return h("div", { class: "ws-fields" }, settings.map((f) => h("label", { class: `ws-field${f.type === "bool" ? " ws-check" : ""}` },
        h("span", {}, f.label), input(f))),
      h("button", { class: "btn btn-quiet", type: "button", onclick: (e) => {
        for (const f of settings) values[f.key] = f.def;
        this.save(); onChange();
        const box = e.target.closest(".ws-form");
        if (box) box.replaceChildren(this.fields(settings, values, onChange));
      } }, "Reset"));
    }
    featureForm(x) {
      const st = this.cfg.features[x.id];
      // structure settings only change the drawing; level settings are computed by the server
      const onChange = !x.server ? () => this.rebuildIndicators() : x.query ? () => this.refreshOverlays() : () => this.redraw();
      return this.fields(x.settings, st.s, onChange);
    }
    structureForm() { return this.fields(STRUCTURE.settings, this.cfg.structure, () => this.refreshOverlays()); }
    togglePanel() {
      const open = this.el.panel.hidden;
      this.el.panel.hidden = !open;
      this.el.layersBtn.setAttribute("aria-expanded", String(open));
    }

    /* ---- data -------------------------------------------------------------------------------- */
    q(path) { return `${path}${path.includes("?") ? "&" : "?"}symbol=${encodeURIComponent(this.symbol)}`; }
    overlayQuery() {
      const s = { swing_len: this.cfg.structure.swing_len };
      for (const x of FEATURES) if (x.query) Object.assign(s, this.cfg.features[x.id].s);
      return Object.entries(s).map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("&");
    }
    async reload(keepView) {
      const tf = this.tf, sym = this.symbol;
      this.note("");
      this.more = true;
      try {
        const c = await this.api(this.q(`/api/chart/candles?tf=${tf}&limit=${BARS}`));
        if (tf !== this.tf || sym !== this.symbol) return;
        this.setBars(c.items.map(bar), keepView);
        this.barsFor = sym;  // the bars now belong to this market (drawings of its trades may use them)
        if (!c.items.length) { this.note("No market history from Tabdeal for this timeframe yet."); return; }
        this.rebuildIndicators();
        await this.refreshOverlays();
      } catch {
        this.note("Tabdeal chart data unavailable — retrying.");
      }
    }
    async refreshOverlays() {
      if (!this.bars.length) return this.reload(false);
      const tf = this.tf, sym = this.symbol;
      try {
        const o = await this.api(this.q(`/api/chart/overlays?tf=${tf}&from=${this.bars[0].t}&${this.overlayQuery()}`));
        if (tf !== this.tf || sym !== this.symbol) return;
        this.overlays = o;
        this.note(o.ready ? "" : `Structure not ready: ${o.reason || "too few bars"}.`);
        this.redraw();
      } catch { /* the next tick retries */ }
    }
    /** The newest bars from Tabdeal: the forming bar updates, a new bar is appended. */
    async refreshTail() {
      if (!this.bars.length || this.loadingTail) return;
      const tf = this.tf, sym = this.symbol;
      this.loadingTail = true;
      try {
        const c = await this.api(this.q(`/api/chart/candles?tf=${tf}&limit=3`));
        if (tf !== this.tf || sym !== this.symbol) return;
        let added = false;
        for (const b of c.items.map(bar)) {
          const last = this.bars[this.bars.length - 1];
          if (b.t < last.t) continue;
          if (b.t === last.t) Object.assign(last, b);
          else { this.bars.push(b); added = true; }
          this.candles.update({ time: b.t, open: b.o, high: b.h, low: b.l, close: b.c });
        }
        this.el.price.textContent = fmt(this.bars[this.bars.length - 1].c);
        if (added) { this.layer.set(this.layer.items, this.bars.map((x) => x.t)); this.refreshOverlays(); }
        this.updateIndicators();
      } catch { /* the next tick retries */ } finally { this.loadingTail = false; }
    }
    /** Scrolled near the left edge: the previous page of bars from Tabdeal. */
    async loadOlder() {
      if (this.loadingOlder || !this.more || !this.bars.length) return;
      const tf = this.tf, sym = this.symbol, first = this.bars[0].t;
      this.loadingOlder = true;
      try {
        const c = await this.api(this.q(`/api/chart/candles?tf=${tf}&limit=${BARS}&before=${first}`));
        if (tf !== this.tf || sym !== this.symbol || this.bars[0].t !== first) return;
        this.more = !!c.more;
        const older = c.items.map(bar).filter((b) => b.t < first);
        if (!older.length) return;
        const ts = this.chart.timeScale(), r = ts.getVisibleLogicalRange();
        this.bars = [...older, ...this.bars];
        this.candles.setData(this.bars.map((b) => ({ time: b.t, open: b.o, high: b.h, low: b.l, close: b.c })));
        if (r) ts.setVisibleLogicalRange({ from: r.from + older.length, to: r.to + older.length });
        this.layer.set(this.layer.items, this.bars.map((b) => b.t));
        this.updateIndicators();
        if (this.tools) this.tools.redraw();
        this.refreshOverlays();
      } catch { /* scrolling again retries */ } finally { this.loadingOlder = false; }
    }
    setBars(bars, keepView) {
      const ts = this.chart.timeScale();
      const away = keepView && ts.scrollPosition() < -3;
      const r = away ? ts.getVisibleLogicalRange() : null;
      this.bars = bars;
      this.candles.setData(bars.map((b) => ({ time: b.t, open: b.o, high: b.h, low: b.l, close: b.c })));
      if (r) ts.setVisibleLogicalRange(r);
      if (bars.length) this.el.price.textContent = fmt(bars[bars.length - 1].c);
      this.layer.set(this.layer.items, bars.map((b) => b.t));
      if (this.tools) this.tools.redraw();
    }
    /** The latest traded price (every second while a position is open): moves the forming bar. */
    livePrice(price, at) {
      if (!this.bars.length || !price) return;
      const last = this.bars[this.bars.length - 1];
      if (at && at >= last.t + TF_SEC[this.tf]) return;  // a new bar: the tail refresh adds it
      Object.assign(last, { c: price, h: Math.max(last.h, price), l: Math.min(last.l, price) });
      this.candles.update({ time: last.t, open: last.o, high: last.h, low: last.l, close: last.c });
      this.el.price.textContent = fmt(price);
      if (this.tools) this.tools.redraw();
    }
    /** Fold a live 1-minute candle into the shown timeframe's forming bar. */
    applyMinute(m) {
      if (!this.bars.length || m.o === undefined) return;
      const step = TF_SEC[this.tf], last = this.bars[this.bars.length - 1];
      const off = ((last.t % step) + step) % step;
      const t = Math.floor(Date.parse(m.t) / 1000), b = Math.floor((t - off) / step) * step + off;
      if (b < last.t) return;
      if (b === last.t) Object.assign(last, { h: Math.max(last.h, +m.h), l: Math.min(last.l, +m.l), c: +m.c });
      else { this.bars.push({ t: b, o: +m.o, h: +m.h, l: +m.l, c: +m.c }); this.layer.set(this.layer.items, this.bars.map((x) => x.t)); }
      const x = this.bars[this.bars.length - 1];
      this.candles.update({ time: x.t, open: x.o, high: x.h, low: x.l, close: x.c });
      this.el.price.textContent = fmt(x.c);
      if (b > last.t) this.refreshOverlays();  // a bar closed: its structure is now known
      clearTimeout(this.indTimer);
      this.indTimer = setTimeout(() => this.updateIndicators(), 1500);
    }

    /* ---- drawing ----------------------------------------------------------------------------- */
    colors() {
      return { bull: css("--c-bull"), bear: css("--c-bear"), fvgBull: css("--c-fvg-bull"), fvgBear: css("--c-fvg-bear"),
        liq: css("--c-liq"), text: css("--c-text") };
    }
    redraw() {
      const items = { boxes: [], lines: [], marks: [] };
      const o = this.overlays;
      if (o && o.ready) {
        const c = this.colors();
        for (const x of FEATURES) {
          if (!x.server || !this.on(x.id)) continue;
          const d = x.draw(o, this.cfg.features[x.id].s, c);
          items.boxes.push(...d.boxes); items.lines.push(...d.lines); items.marks.push(...d.marks);
        }
      }
      this.layer.set(items, this.bars.map((b) => b.t));
    }
    rebuildIndicators() {
      for (const hd of this.handles) hd.remove();
      this.handles = [];
      const c = this.colors();
      let pane = 1;
      for (const x of FEATURES) {
        if (x.server || !this.on(x.id)) continue;
        const idx = x.pane === "own" ? pane++ : 0;
        this.handles.push(x.attach(this.chart, idx, this.cfg.features[x.id].s, c));
      }
      const panes = this.chart.panes();
      panes.forEach((p, i) => p.setStretchFactor(i === 0 ? 1 : 0.28));
      this.updateIndicators();
    }
    updateIndicators() { for (const hd of this.handles) hd.update(this.bars); }

    /* ---- controls ---------------------------------------------------------------------------- */
    setTf(tf) {
      if (tf === this.tf) return;
      this.tf = tf; this.save(); this.renderTfs(); this.renderTitle();
      this.overlays = null; this.layer.set({ boxes: [], lines: [], marks: [] }, []);
      this.reload(false);
    }
    applyFormat() {
      const f = this.format(this.symbol);
      if (f) this.candles.applyOptions({ priceFormat: { type: "price", precision: f.precision, minMove: f.minMove } });
    }
    setSymbol(sym) {
      if (sym === this.symbol) return;
      this.symbol = sym; this.renderTitle(); this.applyFormat();
      this.tools.setSymbol(sym);
      this.overlays = null; this.layer.set({ boxes: [], lines: [], marks: [] }, []);
      this.reload(false);
    }
    focusTime(iso) {
      const t = Math.floor(Date.parse(iso) / 1000), i = this.bars.findIndex((b) => b.t >= t);
      if (i >= 0) this.chart.timeScale().setVisibleLogicalRange({ from: i - 80, to: i + 40 });
    }
    note(text) { this.el.note.textContent = text; this.el.note.hidden = !text; }
    readout(p) {
      const b = p && p.time !== undefined ? this.bars.find((x) => x.t === p.time) : null;
      this.el.readout.textContent = b ? `${LOCAL.readout.format(new Date(b.t * 1000))} · O ${fmt(b.o)}  H ${fmt(b.h)}  L ${fmt(b.l)}  C ${fmt(b.c)}` : "";
    }
    isFull() { return document.fullscreenElement === this.root || this.root.classList.contains("ws-max"); }
    toggleFull() {
      if (this.isFull()) {
        this.root.classList.remove("ws-max"); document.body.classList.remove("has-max");
        if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
      } else {
        this.root.classList.add("ws-max"); document.body.classList.add("has-max");
        if (this.root.requestFullscreen) this.root.requestFullscreen().catch(() => {});
      }
      this.renderFullBtn();
    }
    renderFullBtn() {
      if (!document.fullscreenElement && this.wasReal) { this.root.classList.remove("ws-max"); document.body.classList.remove("has-max"); }
      this.wasReal = !!document.fullscreenElement;
      const on = this.isFull();
      this.el.full.textContent = on ? "⤡" : "⤢";
      this.el.full.setAttribute("aria-pressed", String(on));
      this.el.full.setAttribute("aria-label", on ? "Leave full screen" : "Full screen chart");
    }
    theme() {
      return {
        layout: { background: { color: css("--surface") }, textColor: css("--text-2"), fontFamily: "IBM Plex Sans, sans-serif",
          attributionLogo: false,  // credited in the page footer; the logo needs an inline style (CSP)
          panes: { separatorColor: css("--border-strong"), separatorHoverColor: css("--surface-2"), enableResize: true } },
        grid: { vertLines: { color: css("--border") }, horzLines: { color: css("--border") } },
        rightPriceScale: { borderColor: css("--border") },
        crosshair: { mode: 0 },
        // the browser's time zone (e.g. Tehran), as on Tabdeal and in the rest of the dashboard
        localization: { timeFormatter: (t) => LOCAL.long.format(new Date(t * 1000)) },
        timeScale: { borderColor: css("--border"), timeVisible: true, secondsVisible: false, rightOffset: 8,
          tickMarkFormatter: (t, kind) => (kind === 0 ? LOCAL.year : kind === 1 ? LOCAL.month : kind === 2 ? LOCAL.day : LOCAL.time).format(new Date(t * 1000)) },
      };
    }
    candleColors() {
      const up = css("--ok"), dn = css("--bad");
      return { upColor: up, borderUpColor: up, wickUpColor: up, downColor: dn, borderDownColor: dn, wickDownColor: dn };
    }
    restyle() {
      this.chart.applyOptions(this.theme());
      this.candles.applyOptions(this.candleColors());
      this.rebuildIndicators(); this.redraw();
    }
  }

  function trendChip(tf, trend) {
    const t = trend === "BULLISH" ? "bull" : trend === "BEARISH" ? "bear" : "none";
    const label = trend === "BULLISH" ? "Bullish" : trend === "BEARISH" ? "Bearish" : "No trend";
    return h("span", { class: `tf-trend t-${t}`, title: `${tf}: ${label} (market structure)` }, h("b", {}, tf),
      t === "bull" ? "▲" : t === "bear" ? "▼" : "–");
  }
  function fmt(v) {
    if (v === null || v === undefined) return "—";
    const n = +v, d = n >= 1000 ? 1 : n >= 1 ? 4 : 6;
    return n.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
  }
  window.ChartWorkspace = ChartWorkspace;
  window.ChartTrendChip = trendChip;
  window.ChartTimeframes = TFS;
})();
