/* Chart workspace — the playbook: four rule-based 1h strategies (sp2l/playbook, /api/playbook).
 *
 * Pick a strategy in the chart's top bar: the chart switches to 1h and draws the strategy's
 * lines, every past setup as a Long / Short position to its target or stop (with its result in R
 * after costs), and the current setup, highlighted. The panel under the chart shows the current
 * setup (with "Draw as position" for the Trade button), the checklist of each rule on the last
 * closed bar (long and short), the backtest and every setup; "Scan markets" runs the strategy on
 * every market and lists where a setup is live.
 *
 *   const pb = new ChartPlaybook(ws); pb.attach(chart, nextPane) -> { update(bars), remove() } */
"use strict";
(function () {
  const h = (tag, attrs, ...kids) => {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
      else if (v !== null && v !== undefined && v !== false) e.setAttribute(k, v === true ? "" : v);
    }
    for (const k of kids.flat()) if (k !== null && k !== undefined && k !== false) e.append(k);
    return e;
  };
  const PREFS = "smc-playbook-v1";
  const REFRESH_MS = 60000;
  const H1 = 3600;
  const STRATEGIES = [
    { id: "donchian", n: 1, label: "Donchian 48/24 breakout",
      rules: ["Long: a close above the highest high of the previous 48 bars, above the 4h EMA 200 (short: the mirror).",
        "Entry at the next bar's open (market). Stop: entry − 2 × N, N = ATR(20) of the signal bar; fixed.",
        "Exit: a close below the lowest low of the previous 24 bars (short: above the 24-bar high), at the next open; or the stop.",
        "Few winners, large ones: many small losses are normal."] },
    { id: "ema", n: 2, label: "EMA 50 pullback",
      rules: ["Trend: EMA 50 (1h) above the 4h EMA 200, close above it, EMA 50 rising over 5 bars, ADX(14) > 20.",
        "Pullback: ≥ 2 ATR(14) from the highest high of the previous 15 bars to the signal bar's low.",
        "Signal candle: its low reaches EMA 50, bullish, closing above it. Entry: buy stop at its high, valid 3 bars; cancelled on a close below its low.",
        "Stop: lowest low of the last 3 bars − 0.5 ATR. Target: 3R (limit). Short: the mirror."] },
    { id: "smc", n: 3, label: "Liquidity sweep + BOS + FVG / OB",
      rules: ["Equal lows (two swing lows within 0.25 ATR) swept: a wick below, a close back above.",
        "Within 12 bars a close above the swing high standing at the sweep (BOS); the move carries a candle with a body ≥ 1.5 ATR and an FVG ≥ 0.5 ATR.",
        "Entry: limit at the top of the order block (the last bearish candle before the move), valid 24 bars; void if the target comes first or a close below the stop.",
        "Stop: sweep low − 0.1 ATR. Target: the high before the equal lows; R:R ≥ 2. Short: the mirror."] },
    { id: "avwap", n: 4, label: "Anchored VWAP pullback",
      rules: ["Anchor: the swing low after a ≥ 3 ATR decline, 12+ bars old, not broken since (searched in the last 120 bars).",
        "VWAP rising over 5 bars, close above it, and a close > 1 ATR above it in the last 15 bars.",
        "Signal candle: low within 0.5 ATR of the VWAP, bullish, closing above it. Entry: buy stop at its high, valid 3 bars; cancelled on a close below the VWAP or its low.",
        "Stop: min(signal low, VWAP) − 0.5 ATR. Target: 2R. Short: the mirror (anchor at a swing high)."] },
  ];
  const COMMON = "Every strategy: closed 1h bars only; with the 4h filter, longs only above (shorts below) the last closed 4h EMA 200; the stop must be 0.7–3 % from the entry, else the setup is rejected. Backtest: one setup at a time, the stop first when stop and target share a bar, Tabdeal's fees and slippage from the config. Not proven: test before real money.";
  const STATUS = { pending: ["Order pending", "b-pending"], open: ["Open", "b-active"], tp: ["Target", "pos"], sl: ["Stop", "neg"],
    exit: ["Exit rule", ""], expired: ["Expired", "b-canceled"], cancelled: ["Cancelled", "b-canceled"], missed: ["Missed (target first)", "b-canceled"],
    rejected: ["Rejected", "b-rejected"] };
  const SERIES_COLORS = { ema4: "150,150,160", hi_in: "41,98,255", lo_in: "41,98,255", lo_out: "255,152,0", hi_out: "255,152,0",
    ema50: "0,188,212", adx: "156,39,176", vwap_lo: "156,39,176", vwap_hi: "233,30,99" };
  const store = {
    get() { try { return JSON.parse(localStorage.getItem(PREFS) || "{}"); } catch { return {}; } },
    set(v) { try { localStorage.setItem(PREFS, JSON.stringify({ ...store.get(), ...v })); } catch { /* storage unavailable */ } },
  };
  const LOCAL = new Intl.DateTimeFormat(undefined, { month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  const when = (t) => (t ? LOCAL.format(new Date(t * 1000)) : "—");
  const rText = (v) => (v === null || v === undefined ? "—" : `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(2)}R`);
  const tone = (v) => (v === null || v === undefined ? "" : v > 0 ? "pos" : v < 0 ? "neg" : "");
  const pct = (v) => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(0)}%`);

  class ChartPlaybook {
    constructor(ws) {
      this.ws = ws;
      const p = store.get();
      this.id = STRATEGIES.some((s) => s.id === p.id) ? p.id : "";
      this.days = [30, 90, 180, 365].includes(p.days) ? p.days : 90;
      this.htf = p.htf !== false;
      this.collapsed = !!p.collapsed;
      this.data = null; this.problem = ""; this.sel = null; this.scan = null; this.gen = 0; this.lastCurrent = undefined;
      // its own drawing layer, positioned with the workspace's bars
      this.layer = new window.ChartDraw.OverlayLayer();
      Object.defineProperty(this.layer, "times", { get: () => ws.layer.times, set() {} });
      ws.candles.attachPrimitive(this.layer);
      this.build();
      this.timer = setInterval(() => { if (this.id && !document.hidden) this.load(); }, REFRESH_MS);
      if (this.id) this.load();
    }
    strategy() { return STRATEGIES.find((s) => s.id === this.id) || null; }
    fmt(v) {
      if (v === null || v === undefined) return "—";
      const f = this.ws.format(this.ws.symbol);
      const d = f ? f.precision : Math.abs(v) >= 1000 ? 1 : Math.abs(v) >= 1 ? 4 : 6;
      return (+v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
    }

    /* ---- controls ---------------------------------------------------------------------------- */
    build() {
      this.select = h("select", { class: "ws-pb-select", "aria-label": "Strategy", title: "Playbook strategy (1h): draws its setups and the current one",
        onchange: (e) => this.choose(e.target.value) },
      h("option", { value: "" }, "Strategy: off"),
      STRATEGIES.map((s) => h("option", { value: s.id, selected: s.id === this.id }, `${s.n}. ${s.label}`)));
      this.ws.el.tfs.after(this.select);
      this.el = {};
      this.el.summary = h("span", { class: "ws-pb-summary small" });
      this.el.toggle = h("button", { class: "ws-gear", type: "button", onclick: () => this.setCollapsed(!this.collapsed) });
      this.el.body = h("div", { class: "ws-pb-body" });
      this.el.root = h("section", { class: "ws-pb", "aria-label": "Playbook strategy", hidden: !this.id },
        h("div", { class: "ws-trade-head" }, h("b", { class: "ws-pb-name" }), this.el.summary, h("span", { class: "ws-spacer" }), this.el.toggle), this.el.body);
      this.ws.root.append(this.el.root);
      this.setCollapsed(this.collapsed);
    }
    setCollapsed(on) {
      this.collapsed = on; store.set({ collapsed: on });
      this.el.body.hidden = on;
      this.el.toggle.textContent = on ? "▴" : "▾";
      this.el.toggle.title = on ? "Show the strategy panel" : "Hide the strategy panel";
      this.el.toggle.setAttribute("aria-label", this.el.toggle.title);
    }
    choose(id) {
      this.id = id; store.set({ id });
      this.select.value = id;
      this.data = null; this.sel = null; this.scan = null; this.lastCurrent = undefined; this.gen++;
      this.el.root.hidden = !id;
      if (id && this.ws.tf !== "1h") this.ws.setTf("1h");  // the rules are written for 1h
      this.ws.rebuildIndicators();
      this.draw(); this.render();
      if (id) this.load();
    }
    setSymbol() {
      this.data = null; this.sel = null; this.lastCurrent = undefined; this.gen++;
      this.draw(); this.render();
      if (this.id) this.load();
    }

    /* ---- data -------------------------------------------------------------------------------- */
    url(sym, brief) {
      return `/api/playbook?symbol=${encodeURIComponent(sym)}&strategy=${this.id}&days=${this.days}&htf=${this.htf}${brief ? "&brief=true" : ""}`;
    }
    async load() {
      if (!this.id) return;
      const gen = this.gen, sym = this.ws.symbol;
      if (!this.data) { this.problem = ""; this.render(); }
      try {
        const d = await this.ws.api(this.url(sym, false));
        if (gen !== this.gen || sym !== this.ws.symbol) return;
        this.problem = d.ready ? "" : (d.reason || "not ready");
        this.data = d.ready ? d : null;
        this.notify();
      } catch (e) {
        if (gen !== this.gen) return;
        this.problem = `Tabdeal data unavailable (${e.message}) — retrying.`;
        setTimeout(() => { if (gen === this.gen) this.load(); }, 15000);
      }
      this.ws.rebuildIndicators();
      this.draw(); this.render();
    }
    /** A new current setup (not on the first load): an event the dashboard turns into an alert. */
    notify() {
      const c = this.data && this.data.current;
      const id = c && c.status !== "rejected" ? `${c.id}:${c.status}` : null;
      if (this.lastCurrent !== undefined && id && id !== this.lastCurrent) {
        window.dispatchEvent(new CustomEvent("playbook-setup", { detail: { symbol: this.ws.symbol, strategy: this.strategy().label, setup: c } }));
      }
      this.lastCurrent = id;
    }

    /* ---- the strategy's lines (an indicator handle of the workspace) ------------------------- */
    attach(chart, nextPane) {
      const d = this.data;
      if (!this.id || !d || this.ws.tf !== "1h") return null;
      const own = d.series.some((s) => s.pane === "own") ? nextPane() : 0;
      const series = d.series.map((s) => {
        const rgb = SERIES_COLORS[s.id] || "120,120,120";
        const x = chart.addSeries(LightweightCharts.LineSeries, { color: `rgba(${rgb},0.9)`, lineWidth: s.id === "ema4" ? 2 : 1.5,
          lineStyle: s.id === "lo_out" || s.id === "hi_out" ? 2 : 0, priceLineVisible: false, lastValueVisible: s.pane === "own",
          crosshairMarkerVisible: false, title: s.label }, s.pane === "own" ? own : 0);
        for (const lv of s.levels || []) x.createPriceLine({ price: lv, color: `rgba(${rgb},0.55)`, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: "" });
        return { s, x };
      });
      return {
        update: (bars) => {
          const t0 = bars.length ? bars[0].t : 0, tN = bars.length ? bars[bars.length - 1].t : 0;
          for (const { s, x } of series) {
            // only where the chart has bars: the strategy's history is longer than the chart's
            x.setData(s.points.filter((p) => p[0] >= t0 && p[0] <= tN).map(([t, v]) => (v === null ? { time: t } : { time: t, value: v })));
          }
        },
        remove: () => { for (const { x } of series) chart.removeSeries(x); },
      };
    }

    /* ---- setups on the chart ----------------------------------------------------------------- */
    draw() {
      const items = { boxes: [], lines: [], marks: [] };
      const d = this.data;
      if (this.id && d && this.ws.tf === "1h") {
        const c = this.ws.colors(), cur = d.current;
        for (const r of d.setups) {
          const isCur = cur && r.id === cur.id;
          if (r.status === "rejected" && !isCur) continue;
          this.drawSetup(items, r, c, isCur, this.sel === r.id);
        }
      }
      this.layer.set(items, this.ws.layer.times);
    }
    drawSetup(items, r, c, isCur, isSel) {
      const long = r.side === "LONG", d = this.data;
      const last = d.asof + H1;
      const start = r.fill_t || r.signal_t + H1;
      const end = r.exit_t ? r.exit_t + H1 : r.status === "open" || r.status === "pending" ? Math.max(last + 8 * H1, r.valid_until || 0)
        : (r.end_t || r.signal_t) + H1;
      const strong = isCur || isSel;
      const a = strong ? 1 : 0.6;
      if (r.status === "rejected") {  // the current pattern a filter rejected: dashed, grey
        items.lines.push({ t1: r.signal_t, t2: last + 6 * H1, p1: r.entry, p2: r.entry, color: `rgba(${c.text},0.6)`, dash: true, label: `${long ? "Long" : "Short"} rejected: ${r.reason}` });
        return;
      }
      if (!r.fill && r.status !== "pending") {  // an order that never filled
        items.lines.push({ t1: r.signal_t + H1, t2: end, p1: r.entry, p2: r.entry, color: `rgba(${c.text},${0.35 * a + 0.1})`, dash: true, width: 1,
          label: strong ? STATUS[r.status][0] : "" });
        return;
      }
      const entry = r.fill || r.entry;
      // no fixed target (Donchian): the profit so far, to the exit or to the last close
      const reach = r.exit_price !== null ? r.exit_price : r.status === "open" ? d.close : null;
      const tp = r.tp !== null ? r.tp : reach !== null && (long ? reach > entry : reach < entry) ? reach : null;
      if (tp !== null) items.boxes.push({ t1: start, t2: end, top: Math.max(entry, tp), bottom: Math.min(entry, tp),
        fill: `rgba(${c.bull},${0.13 * a})`, stroke: strong ? `rgba(${c.bull},0.9)` : null, dash: r.status === "pending", label: "", labelColor: null });
      items.boxes.push({ t1: start, t2: end, top: Math.max(entry, r.sl), bottom: Math.min(entry, r.sl),
        fill: `rgba(${c.bear},${0.13 * a})`, stroke: strong ? `rgba(${c.bear},0.9)` : null, dash: r.status === "pending", label: "", labelColor: null });
      items.lines.push({ t1: start, t2: end, p1: entry, p2: entry, color: `rgba(${c.text},${0.5 * a + 0.2})`, width: strong ? 1.5 : 1, label: "" });
      if (isCur) {  // its label just right of where it starts (the box runs past the last bar)
        items.marks.push({ t: start + 4 * H1, p: long ? Math.max(entry, tp !== null ? tp : entry) : Math.min(entry, tp !== null ? tp : entry),
          text: `NOW · ${long ? "Long" : "Short"} ${r.status === "pending" ? `${r.kind} order` : "open"} ${this.fmt(entry)}${r.rr ? ` · R:R ${r.rr}` : ""}`,
          color: `rgba(${long ? c.bull : c.bear},1)`, pos: long ? "above" : "below" });
      }
      items.marks.push({ t: r.signal_t, p: long ? Math.min(entry, r.sl) : Math.max(entry, r.sl), text: long ? "▲" : "▼",
        color: `rgba(${long ? c.bull : c.bear},${a})`, pos: long ? "below" : "above" });
      if (r.exit_t && r.exit_price !== null) {
        items.marks.push({ t: r.exit_t, p: r.exit_price, text: rText(r.net_r), color: r.net_r > 0 ? `rgba(${c.bull},1)` : `rgba(${c.bear},1)`,
          pos: (r.exit_price >= entry) === long ? "above" : "below" });
      }
      if (strong && r.marks) this.drawMarks(items, r, c);
    }
    /** What made the setup: the levels and zones of its pattern. */
    drawMarks(items, r, c) {
      const m = r.marks, txt = `rgba(${c.text},0.85)`;
      if (m.eq) items.lines.push({ t1: m.eq[0], t2: m.eq[1], p1: m.eq[2], p2: m.eq[2], color: `rgba(${c.liq},0.95)`, dash: true, label: r.side === "LONG" ? "Equal lows" : "Equal highs" });
      if (m.sweep) items.marks.push({ t: m.sweep[0], p: m.sweep[1], text: "sweep", color: `rgba(${c.liq},1)`, pos: r.side === "LONG" ? "below" : "above" });
      if (m.bos) items.lines.push({ t1: m.bos[0], t2: m.bos[1], p1: m.bos[2], p2: m.bos[2], color: txt, label: "BOS" });
      if (m.fvg) items.boxes.push({ t1: m.fvg[0], t2: m.fvg[1] + 6 * H1, top: m.fvg[3], bottom: m.fvg[2], fill: `rgba(${c.fvgBull},0.18)`, stroke: `rgba(${c.fvgBull},0.7)`, label: "FVG", labelColor: `rgba(${c.fvgBull},1)` });
      if (m.ob) items.boxes.push({ t1: m.ob[0], t2: (r.fill_t || r.signal_t) + H1, top: m.ob[1], bottom: m.ob[2], fill: "rgba(156,39,176,0.16)", stroke: "rgba(156,39,176,0.8)", label: "OB", labelColor: "rgba(156,39,176,1)" });
      if (m.anchor) items.marks.push({ t: m.anchor[0], p: m.anchor[1], text: "anchor", color: "rgba(156,39,176,1)", pos: r.side === "LONG" ? "below" : "above" });
      if (m.level !== undefined) items.lines.push({ t1: r.signal_t - 48 * H1, t2: r.signal_t, p1: m.level, p2: m.level, color: "rgba(41,98,255,0.9)", dash: true, label: "48-bar " + (r.side === "LONG" ? "high" : "low") });
    }

    /* ---- the panel --------------------------------------------------------------------------- */
    render() {
      const st = this.strategy();
      this.el.root.hidden = !st;
      if (!st) return;
      this.el.root.querySelector(".ws-pb-name").textContent = `${st.n}. ${st.label}`;
      const d = this.data;
      const cur = d && d.current;
      this.el.summary.textContent = this.problem ? this.problem
        : !d ? "Loading…"
        : `${cur && cur.status !== "rejected" ? `NOW: ${cur.side === "LONG" ? "Long" : "Short"} ${cur.status === "pending" ? "order pending" : "open"} · ` : "No live setup · "}`
          + `${d.days} d: ${d.stats.trades} trades, ${rText(d.stats.total_r)}${this.ws.tf !== "1h" ? " · shown on 1h only" : ""}`;
      this.el.summary.className = `ws-pb-summary small${this.problem ? " neg" : ""}`;
      if (this.collapsed) return;
      const controls = h("div", { class: "ws-pb-controls" },
        h("label", {}, "History ", h("select", { onchange: (e) => { this.days = +e.target.value; store.set({ days: this.days }); this.data = null; this.gen++; this.render(); this.load(); } },
          [30, 90, 180, 365].map((n) => h("option", { value: n, selected: n === this.days }, `${n} days`)))),
        h("label", { class: "ws-check" }, h("input", { type: "checkbox", checked: this.htf, onchange: (e) => { this.htf = e.target.checked; store.set({ htf: this.htf }); this.data = null; this.gen++; this.render(); this.load(); } }), "4h EMA 200 filter"),
        this.ws.tf !== "1h" ? h("button", { class: "btn btn-quiet", type: "button", onclick: () => this.ws.setTf("1h") }, "Show on 1h") : null,
        h("button", { class: "btn btn-quiet", type: "button", onclick: () => this.runScan() }, this.scan && this.scan.running ? "Scanning…" : "Scan markets"),
        h("details", { class: "ws-pb-rules" }, h("summary", {}, "Rules"), h("ol", {}, st.rules.map((x) => h("li", {}, x))), h("p", { class: "small" }, COMMON)));
      if (!d) { this.el.body.replaceChildren(controls, h("p", { class: "ws-trade-empty" }, this.problem || "Loading the strategy…")); return; }
      this.el.body.replaceChildren(...[controls,
        h("div", { class: "ws-pb-grid" }, this.nowCard(cur), this.checksCard(d), this.statsCard(d)),
        this.scan ? this.scanTable() : null,
        this.setupTable(d)].filter(Boolean));
    }
    nowCard(cur) {
      const d = this.data;
      const box = h("div", { class: "ws-pb-card ws-pb-now" }, h("h4", {}, "Current setup"),
        h("p", { class: "small muted" }, `Last closed bar ${when(d.asof)} · price ${this.fmt(d.price)}`));
      if (!cur) { box.append(h("p", {}, "No setup now: see the checklist for what is missing.")); return box; }
      const long = cur.side === "LONG";
      const [label, cls] = STATUS[cur.status] || [cur.status, ""];
      box.append(h("p", { class: "ws-pb-big" }, h("span", { class: `side t-${long ? "ok" : "bad"}` }, long ? "▲ Long" : "▼ Short"), " ", h("span", { class: `ws-badge ${cls}` }, label)));
      if (cur.status === "rejected") box.append(h("p", { class: "neg small" }, `Pattern found, a filter refused it: ${cur.reason}`));
      const entryText = cur.status === "open" ? `${this.fmt(cur.fill)} (filled ${when(cur.fill_t)})`
        : cur.kind === "market" ? `at the next open (≈ ${this.fmt(cur.entry)})`
        : `${cur.kind === "stop" ? (long ? "buy stop" : "sell stop") : (long ? "buy limit" : "sell limit")} ${this.fmt(cur.entry)}`;
      box.append(h("dl", { class: "ws-dl" },
        h("dt", {}, "Entry"), h("dd", { class: "num" }, entryText),
        h("dt", {}, "Stop"), h("dd", { class: "num neg" }, `${this.fmt(cur.sl)} (${cur.stop_pct}%)`),
        h("dt", {}, "Target"), h("dd", { class: "num pos" }, cur.tp !== null ? `${this.fmt(cur.tp)} · R:R ${cur.rr}` : this.data.target),
        cur.valid_until && cur.status === "pending" ? [h("dt", {}, "Valid until"), h("dd", {}, when(cur.valid_until))] : null,
        cur.status === "open" ? [h("dt", {}, "Now"), h("dd", { class: `num ${tone(cur.r)}` }, `${rText(cur.r)} (after costs ${rText(cur.net_r)})`)] : null,
        cur.exit_next ? [h("dt", {}, "Exit"), h("dd", { class: "neg" }, "exit rule met: close at the next open")] : null));
      if (cur.status !== "rejected") {
        box.append(h("button", { class: "btn ws-primary", type: "button", title: "A Long / Short position drawing at these levels: check it, then use Trade ⚡",
          onclick: () => this.drawAsPosition(cur) }, "Draw as position"));
      }
      return box;
    }
    checksCard(d) {
      // its value: a price, a ratio (ATR multiples, ADX, R:R), a percentage or a time
      const val = (c) => {
        const v = c.value;
        if (v === null || v === undefined) return "";
        if (typeof v !== "number") return ` (${v})`;
        return ` (${c.unit === "time" ? when(v) : c.unit === "%" ? `${v.toFixed(2)}%` : c.unit === "x" ? v.toFixed(2) : this.fmt(v)})`;
      };
      const col = (side) => h("div", {}, h("h5", {}, side === "LONG" ? "Long" : "Short"),
        h("ul", { class: "ws-pb-checks" }, (d.checks[side] || []).map((c) => h("li", { class: c.ok ? "ok" : "no" },
          h("span", { class: "mk", "aria-hidden": "true" }, c.ok ? "✓" : "✗"), h("span", { class: "sr-only" }, c.ok ? "met: " : "not met: "),
          h("span", {}, c.label, h("span", { class: "num muted" }, val(c)))))));
      return h("div", { class: "ws-pb-card" }, h("h4", {}, `Checklist · bar ${when(d.asof)}`), h("div", { class: "ws-pb-two" }, col("LONG"), col("SHORT")));
    }
    statsCard(d) {
      const s = d.stats;
      const row = (k, v, cls) => [h("dt", {}, k), h("dd", { class: `num ${cls || ""}` }, v)];
      return h("div", { class: "ws-pb-card" }, h("h4", {}, `Backtest · ${d.days} days, after costs`),
        h("dl", { class: "ws-dl" },
          row("Trades", `${s.trades} (long ${s.long.trades}, short ${s.short.trades})`),
          row("Win rate", pct(s.win_rate)),
          row("Total", rText(s.total_r), tone(s.total_r)),
          row("Average", rText(s.avg_r), tone(s.avg_r)),
          row("Profit factor", s.profit_factor === null ? "—" : String(s.profit_factor)),
          row("Max drawdown", s.max_dd_r ? `−${s.max_dd_r.toFixed(2)}R` : "0R"),
          row("Best / worst", `${rText(s.best_r)} / ${rText(s.worst_r)}`),
          row("Not traded", `${s.expired} expired · ${s.cancelled} cancelled · ${s.missed} missed · ${s.rejected} rejected`)),
        h("p", { class: "small muted" }, "R = the risk to the stop. A test on past bars, not a promise: rules from the guide, not optimised."));
    }
    setupTable(d) {
      const rows = d.setups.filter((r) => r.status !== "rejected").slice(-60).reverse();
      const t = h("table", { class: "ws-trade-table ws-pb-table" },
        h("thead", {}, h("tr", {}, ["Signal", "Side", "Status", "Entry", "Stop", "Target", "Exit", "R (net)"].map((x) => h("th", { scope: "col" }, x)))));
      const body = h("tbody");
      if (!rows.length) body.append(h("tr", {}, h("td", { colspan: 8, class: "empty" }, "No setup in this period")));
      for (const r of rows) {
        const [label, cls] = STATUS[r.status] || [r.status, ""];
        body.append(h("tr", { class: `ws-trade-row${this.sel === r.id ? " sel" : ""}`, title: "Show on the chart", onclick: () => this.focus(r) },
          h("td", {}, when(r.signal_t)), h("td", {}, r.side === "LONG" ? "Long" : "Short"), h("td", {}, h("span", { class: `ws-badge ${cls}` }, label)),
          h("td", { class: "num" }, this.fmt(r.fill || r.entry)), h("td", { class: "num" }, this.fmt(r.sl)), h("td", { class: "num" }, this.fmt(r.tp)),
          h("td", { class: "num" }, r.exit_t ? `${this.fmt(r.exit_price)} · ${when(r.exit_t)}` : "—"), h("td", { class: `num ${tone(r.net_r)}` }, rText(r.net_r))));
      }
      t.append(body);
      return h("div", { class: "ws-pb-list" }, h("h4", {}, "Setups"), t);
    }
    focus(r) {
      this.sel = this.sel === r.id ? null : r.id;
      if (this.ws.tf !== "1h") this.ws.setTf("1h");
      const t = r.signal_t, i = this.ws.bars.findIndex((b) => b.t >= t);
      if (i >= 0) this.ws.chart.timeScale().setVisibleLogicalRange({ from: i - 60, to: i + 60 });
      this.draw(); this.render();
    }
    drawAsPosition(r) {
      const tools = this.ws.tools, long = r.side === "LONG";
      if (!tools) return;
      const entry = r.status === "open" ? r.fill : r.entry;
      const tp = r.tp !== null ? r.tp : entry + (long ? 1 : -1) * 2 * Math.abs(entry - r.sl);  // Donchian: 2R shown, exit by the rule
      const id = `pb-${r.id}`;
      tools.items = tools.items.filter((x) => x.id !== id);
      const t1 = r.fill_t || r.signal_t + H1;
      tools.items.push({ id, type: long ? "long" : "short", t1, t2: t1 + 24 * H1, entry, sl: r.sl, tp });
      tools.save(); tools.sel = id; tools.redraw();
      if (tools.syncDbar) tools.syncDbar();
    }

    /* ---- every market ------------------------------------------------------------------------ */
    async runScan() {
      if (this.scan && this.scan.running) return;
      // the dashboard's markets (web/app.js), else this one
      const markets = typeof state !== "undefined" && state.chartMarkets && state.chartMarkets.length ? state.chartMarkets : [this.ws.symbol];
      const id = this.id, gen = this.gen;
      this.scan = { running: true, id, rows: markets.map((m) => ({ symbol: m, d: null, err: null })) };
      this.render();
      for (const row of this.scan.rows) {
        if (gen !== this.gen) return;
        try { row.d = await this.ws.api(this.url(row.symbol, true)); } catch (e) { row.err = e.message; }
        this.render();
      }
      this.scan.running = false;
      this.render();
    }
    scanTable() {
      const live = (r) => r.d && r.d.current && r.d.current.status !== "rejected";
      const rows = [...this.scan.rows].sort((a, b) => Number(live(b)) - Number(live(a)));
      const t = h("table", { class: "ws-trade-table ws-pb-table" },
        h("thead", {}, h("tr", {}, ["Market", "Now", "Entry", "Stop", "Target", `Backtest ${this.days} d`].map((x) => h("th", { scope: "col" }, x)))));
      const body = h("tbody");
      for (const r of rows) {
        const c = r.d && r.d.current, s = r.d && r.d.stats;
        const name = this.ws.display(r.symbol);
        const now = r.err ? h("span", { class: "neg" }, "unavailable") : !r.d ? "…" : !c ? "—"
          : h("span", {}, h("span", { class: `side t-${c.side === "LONG" ? "ok" : "bad"}` }, c.side === "LONG" ? "▲ Long" : "▼ Short"), " ", (STATUS[c.status] || [c.status])[0]);
        body.append(h("tr", { class: "ws-trade-row", title: `Show ${name}`, onclick: () => this.goto(r.symbol) },
          h("td", {}, name), h("td", {}, now),
          h("td", { class: "num" }, c ? String(+(+(c.fill || c.entry)).toPrecision(6)) : "—"), h("td", { class: "num" }, c ? String(+(+c.sl).toPrecision(6)) : "—"),
          h("td", { class: "num" }, c && c.tp !== null ? String(+(+c.tp).toPrecision(6)) : "—"),
          h("td", { class: `num ${s ? tone(s.total_r) : ""}` }, s ? `${s.trades} · ${rText(s.total_r)}` : "—")));
      }
      t.append(body);
      return h("div", { class: "ws-pb-list" }, h("h4", {}, `Markets · ${this.strategy().label}${this.scan.running ? " (scanning…)" : ""}`), t);
    }
    goto(symbol) {
      if (typeof selectSymbol === "function") selectSymbol(symbol);  // the dashboard's market switch
      else this.ws.setSymbol(symbol);
    }
  }
  window.ChartPlaybook = ChartPlaybook;
})();
