/* Chart workspace — trading on Tabdeal futures from a Long / Short position drawing.
 *
 * The toolbar's Trade button takes the selected position drawing (or the last one placed) and
 * opens a dialog: it asks for the leverage and the margin, shows the size, the loss at the stop
 * and the gain at the target, then places a LIMIT entry at the drawing's entry; once filled, the
 * server sets the position's stop and target (/api/trade/*, sp2l/trading). The panel under the
 * chart lists the open trades (pending / active, live from Tabdeal) with Cancel / Close, and the
 * history. Trading is off unless the server's config enables it, and only behind the dashboard
 * login (the session cookie authorizes every call). */
"use strict";
(function () {
  const PREFS = "ets-trade-prefs";
  const OPEN_EVERY_MS = 3000;
  const HISTORY_EVERY_MS = 30000;

  const h = (tag, attrs, ...kids) => {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
      else if (v !== null && v !== undefined && v !== false) e.setAttribute(k, v === true ? "" : v);
    }
    for (const k of kids.flat()) if (k !== null && k !== undefined && k !== false) e.append(k);
    return e;
  };
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { if (v === null) localStorage.removeItem(k); else localStorage.setItem(k, JSON.stringify(v)); } catch { /* storage unavailable */ } },
  };
  function num(v, d) {
    if (v === null || v === undefined || Number.isNaN(+v)) return "—";
    const n = +v, dd = d !== undefined ? d : Math.abs(n) >= 1000 ? 1 : Math.abs(n) >= 1 ? 4 : 6;
    return n.toLocaleString(undefined, { minimumFractionDigits: dd, maximumFractionDigits: dd });
  }
  const usd = (v) => (v === null || v === undefined ? "—" : `${+v >= 0 ? "+" : "−"}${num(Math.abs(+v), 2)}`);
  const when = (iso) => (iso ? new Date(iso).toISOString().replace("T", " ").slice(5, 16) : "—");
  const tone = (v) => (v === null || v === undefined || +v === 0 ? "" : +v > 0 ? "pos" : "neg");

  class Trading {
    constructor(ws) {
      this.ws = ws; this.cfg = null; this.open = []; this.history = null; this.tab = "open";
      this.expanded = new Set();
      this.collapsed = !!store.get(PREFS, {}).collapsed;
      this.build();
      this.loadConfig();
      this.timer = setInterval(() => { if (!document.hidden) this.refresh(); }, OPEN_EVERY_MS);
      this.histTimer = setInterval(() => { if (!document.hidden && this.tab === "history") this.loadHistory(); }, HISTORY_EVERY_MS);
    }

    /* ---- server ------------------------------------------------------------------------------ */
    async call(method, path, body) {
      const headers = { Accept: "application/json" };
      if (body !== undefined) headers["Content-Type"] = "application/json";
      const r = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
      let data = null;
      try { data = await r.json(); } catch { /* no body */ }
      if (r.status === 401) { location.href = "/login"; throw new Error("login required"); }
      if (!r.ok) { const e = new Error((data && data.detail) || `HTTP ${r.status}`); e.status = r.status; throw e; }
      return data;
    }
    async loadConfig() {
      try { this.cfg = await (await fetch("/api/trade/config", { headers: { Accept: "application/json" } })).json(); } catch { this.cfg = null; }
      this.render();
      this.refresh();
    }
    ready() { return this.cfg && this.cfg.enabled && this.cfg.ready; }
    async refresh() {
      if (!this.ready()) return this.render();
      try {
        const d = await this.call("GET", "/api/trade/trades?scope=open");
        const before = new Set(this.open.map((t) => t.id));
        this.open = d.items; this.problem = d.poll_error ? `Tabdeal: ${d.poll_error}` : "";
        if (this.ws.tools) this.ws.tools.syncTrades(this.open);
        if ([...before].some((id) => !this.open.find((t) => t.id === id))) this.loadHistory();
      } catch (e) {
        this.problem = e.message;
      }
      this.render();
    }
    async loadHistory() {
      if (!this.ready()) return;
      try { this.history = (await this.call("GET", "/api/trade/trades?scope=history&limit=200")).items; } catch (e) { this.problem = e.message; }
      this.render();
    }
    /** Send the linked drawing's stop / target to Tabdeal. */
    async setSlTp(t) {
      const d = this.ws.tools && this.ws.tools.linked(t.id);
      if (t.symbol !== this.ws.symbol || !d) { alert(`Open ${this.ws.display(t.symbol)} on the chart and drag the stop / target of trade #${t.id} first.`); return; }
      if (!confirm(`Set on Tabdeal for #${t.id} (${t.symbol} ${t.side}):\nstop ${num(d.sl)} · target ${num(d.tp)}?`)) return;
      try {
        await this.call("POST", `/api/trade/${t.id}/sltp`, { sl: d.sl, tp: d.tp });
        d.dirty = false; this.ws.tools.save();
      } catch (e) { alert(`Not done: ${e.message}`); }
      await this.refresh();
    }
    async act(t, what) {
      const verb = what === "cancel" ? "Cancel the pending order" : "Close the position at market";
      if (!confirm(`${verb} of trade #${t.id} (${t.symbol} ${t.side}) on Tabdeal?`)) return;
      try { await this.call("POST", `/api/trade/${t.id}/${what}`); } catch (e) { alert(`Not done: ${e.message}`); }
      await this.refresh(); this.loadHistory();
    }

    /* ---- panel ------------------------------------------------------------------------------- */
    build() {
      this.el = {};
      this.el.tabs = h("div", { class: "seg ws-trade-tabs", role: "tablist", "aria-label": "Trades" });
      this.el.status = h("span", { class: "ws-trade-status small" });
      this.el.toggle = h("button", { class: "ws-gear", type: "button", onclick: () => this.setCollapsed(!this.collapsed) });
      this.el.body = h("div", { class: "ws-trade-body" });
      this.el.root = h("section", { class: "ws-trades", "aria-label": "Trades on Tabdeal" },
        h("div", { class: "ws-trade-head" }, this.el.tabs, this.el.status, h("span", { class: "ws-spacer" }), this.el.toggle), this.el.body);
      this.ws.root.append(this.el.root);
      this.setCollapsed(this.collapsed);
    }
    setCollapsed(on) {
      this.collapsed = on;
      store.set(PREFS, { collapsed: on });
      this.el.body.hidden = on;
      this.el.toggle.textContent = on ? "▴" : "▾";
      this.el.toggle.title = on ? "Show the trades" : "Hide the trades";
      this.el.toggle.setAttribute("aria-label", this.el.toggle.title);
      this.ws.root.classList.toggle("ws-trades-open", !on);
    }
    setTab(tab) { this.tab = tab; if (tab === "history" && this.history === null) this.loadHistory(); this.render(); }
    render() {
      const n = this.open.length;
      this.el.tabs.replaceChildren(
        h("button", { type: "button", role: "tab", "aria-selected": String(this.tab === "open"), onclick: () => this.setTab("open") }, `Open trades${n ? ` (${n})` : ""}`),
        h("button", { type: "button", role: "tab", "aria-selected": String(this.tab === "history"), onclick: () => this.setTab("history") }, "History"));
      this.el.status.textContent = this.problem || "";
      this.el.status.className = `ws-trade-status small${this.problem ? " neg" : ""}`;
      this.el.body.replaceChildren(this.content());
    }
    content() {
      if (!this.cfg) return h("p", { class: "ws-trade-empty" }, "Trading status unavailable.");
      if (!this.cfg.enabled) return h("p", { class: "ws-trade-empty" }, "Trading is off. To trade from the chart, set ", h("code", {}, "trading.enabled: true"), " in the server config and restart the API.");
      if (!this.cfg.ready) return h("p", { class: "ws-trade-empty neg" }, `Trading is not ready: ${this.cfg.problem || "unknown"}`);
      return this.tab === "open" ? this.openTable() : this.historyTable();
    }
    table(head, rows, empty) {
      return h("div", { class: "ws-trade-wrap" }, h("table", { class: "ws-trade-table" },
        h("thead", {}, h("tr", {}, head.map((x) => h("th", { scope: "col" }, x)))),
        h("tbody", {}, rows.length ? rows : h("tr", {}, h("td", { colspan: head.length, class: "empty" }, empty)))));
    }
    events(t, span) {
      return h("tr", { class: "ws-trade-events" }, h("td", { colspan: span },
        h("ol", {}, (t.events || []).map((e) => h("li", {}, h("span", { class: "num" }, new Date(e.at * 1000).toISOString().replace("T", " ").slice(5, 19)), " ", e.event))),
        t.last_error ? h("div", { class: "neg" }, `Last error: ${t.last_error}`) : null));
    }
    rowToggle(t) {
      return () => { if (this.expanded.has(t.id)) this.expanded.delete(t.id); else this.expanded.add(t.id); this.render(); };
    }
    openTable() {
      const head = ["#", "Market", "Side", "Status", "Entry", "Size", "Lev.", "Stop", "Target", "Mark", "PnL (USDT)", "ROE", "Liq.", "Opened", ""];
      const rows = [];
      for (const t of this.open) {
        const lv = t.live || {}, active = t.status === "ACTIVE";
        const prot = active ? (t.protected ? h("span", { class: "pos", title: "Stop / target set on the position" }, " ✓") : h("span", { class: "neg", title: t.last_error || "No stop / target on Tabdeal" }, " ⚠ no stop / target")) : null;
        const src = t.origin === "TABDEAL" ? h("span", { class: "ws-badge b-origin", title: "Opened directly on Tabdeal; followed here" }, "Tabdeal") : null;
        const moved = active && this.ws.tools && (this.ws.tools.linked(t.id) || {}).dirty;
        rows.push(h("tr", { class: "ws-trade-row", onclick: this.rowToggle(t) },
          h("td", { class: "num" }, String(t.id)),
          h("td", {}, this.ws.display(t.symbol)),
          h("td", { class: t.side === "LONG" ? "pos" : "neg" }, t.side),
          h("td", {}, h("span", { class: `ws-badge b-${t.status.toLowerCase()}` }, t.status), " ", src, prot),
          h("td", { class: "num" }, active && t.avg_entry ? num(t.avg_entry) : num(t.entry)),
          h("td", { class: "num" }, active && t.filled_qty < t.qty ? `${num(t.filled_qty, 5)} / ${num(t.qty, 5)}` : num(t.qty, 5)),
          h("td", { class: "num" }, `${t.leverage}×`),
          h("td", { class: "num" }, num(t.sl)),
          h("td", { class: "num" }, num(t.tp)),
          h("td", { class: "num" }, lv.mark ? num(lv.mark) : "—"),
          h("td", { class: `num ${tone(lv.upnl)}` }, active ? usd(lv.upnl) : "—"),
          h("td", { class: `num ${tone(lv.roe_pct)}` }, lv.roe_pct === undefined || lv.roe_pct === null ? "—" : `${num(lv.roe_pct, 2)}%`),
          h("td", { class: "num" }, lv.liquidation ? num(lv.liquidation) : "—"),
          h("td", { class: "num" }, when(t.created_at)),
          h("td", { class: "ws-trade-acts" },
            active ? h("button", { class: `btn btn-quiet${moved || !t.protected ? " ws-attn" : ""}`, type: "button", title: "Send the stop / target of this trade's drawing to Tabdeal", onclick: (e) => { e.stopPropagation(); this.setSlTp(t); } }, "SL/TP") : null,
            h("button", { class: "btn btn-quiet", type: "button", onclick: (e) => { e.stopPropagation(); this.act(t, active ? "close" : "cancel"); } }, active ? "Close" : "Cancel"))));
        if (this.expanded.has(t.id)) rows.push(this.events(t, head.length));
      }
      return this.table(head, rows, "No open trades. Place a Long / Short position on the chart, then press Trade (⚡) in the left toolbar. Positions opened directly on Tabdeal appear here by themselves.");
    }
    historyTable() {
      const head = ["#", "Market", "Side", "Result", "Entry", "Exit", "Size", "Lev.", "PnL (USDT)", "Opened", "Closed"];
      const rows = [];
      for (const t of this.history || []) {
        const result = t.status === "CLOSED" ? { TP: "Target", SL: "Stop", MANUAL: "Closed here", CLOSED_ON_TABDEAL: "Closed on Tabdeal" }[t.close_reason] || "Closed" : t.status === "CANCELED" ? "Canceled" : t.status === "REJECTED" ? "Rejected" : t.status;
        rows.push(h("tr", { class: "ws-trade-row", onclick: this.rowToggle(t) },
          h("td", { class: "num" }, String(t.id)),
          h("td", {}, this.ws.display(t.symbol)),
          h("td", { class: t.side === "LONG" ? "pos" : "neg" }, t.side),
          h("td", {}, h("span", { class: `ws-badge b-${t.status.toLowerCase()}`, title: t.last_error || "" }, result), t.origin === "TABDEAL" ? h("span", { class: "ws-badge b-origin" }, " Tabdeal") : null),
          h("td", { class: "num" }, num(t.avg_entry || t.entry)),
          h("td", { class: "num" }, t.exit_price ? num(t.exit_price) : "—"),
          h("td", { class: "num" }, num(t.filled_qty || t.qty, 5)),
          h("td", { class: "num" }, `${t.leverage}×`),
          h("td", { class: `num ${tone(t.realized_pnl)}` }, t.realized_pnl === null ? "—" : usd(t.realized_pnl)),
          h("td", { class: "num" }, when(t.created_at)),
          h("td", { class: "num" }, when(t.closed_at))));
        if (this.expanded.has(t.id)) rows.push(this.events(t, head.length));
      }
      return this.table(head, rows, this.history === null ? "Loading…" : "No closed trades yet.");
    }

    /* ---- the trade dialog -------------------------------------------------------------------- */
    async openDialog(d) {
      if (!d) { alert("Place a Long or Short position on the chart first (left toolbar), then press Trade."); return; }
      if (!this.cfg) await this.loadConfig();
      const sym = this.ws.symbol, side = d.type === "long" ? "LONG" : "SHORT";
      const prefs = store.get(PREFS, {});
      const max = this.cfg ? this.cfg.max_leverage : 1, maxMargin = this.cfg ? this.cfg.max_margin_usdt : 0;
      const lev = h("input", { type: "number", min: 1, max, step: 1, value: Math.min(max, prefs.leverage || 10), required: true });
      const margin = h("input", { type: "number", min: 0.01, max: maxMargin, step: 0.01, value: Math.min(maxMargin, prefs.margin || 10), required: true });
      const calc = h("dl", { class: "ws-dl" });
      const msg = h("p", { class: "ws-dlg-msg", "aria-live": "polite" });
      const go = h("button", { class: `btn ws-go ${side === "LONG" ? "go-long" : "go-short"}`, type: "submit" }, `Place ${side} limit on Tabdeal`);
      const dlg = h("dialog", { class: "ws-dialog", "aria-label": "Trade on Tabdeal" });
      const close = () => { dlg.close(); dlg.remove(); };
      let account = null;
      const update = () => {
        const L = +lev.value, m = +margin.value, qty = (m * L) / d.entry;
        const loss = qty * Math.abs(d.entry - d.sl), gain = qty * Math.abs(d.tp - d.entry);
        const row = (k, v, cls) => [h("dt", {}, k), h("dd", { class: `num ${cls || ""}` }, v)];
        calc.replaceChildren(
          ...row("Entry (limit)", num(d.entry)), ...row("Stop", num(d.sl), "neg"), ...row("Target", num(d.tp), "pos"),
          ...row("Size ≈", `${num(qty, 5)} (${num(qty * d.entry, 2)} USDT)`),
          ...row("Loss at the stop ≈", `−${num(loss, 2)} USDT`, "neg"), ...row("Gain at the target ≈", `+${num(gain, 2)} USDT`, "pos"),
          ...row("R:R", loss ? (gain / loss).toFixed(2) : "—"),
          ...row("Futures wallet", account ? `${num(account.wallet_usdt, 2)} USDT (${num(account.available_usdt, 2)} available)` : "—"));
      };
      const notReady = !this.cfg ? "Trading status unavailable." : !this.cfg.enabled ? "Trading is off: set trading.enabled: true in the server config and restart the API." : !this.cfg.ready ? `Trading is not ready: ${this.cfg.problem}` : "";
      const form = h("form", { method: "dialog", onsubmit: async (e) => {
        e.preventDefault();
        const L = Math.round(+lev.value), m = +margin.value;
        if (!(L >= 1 && L <= max) || !(m > 0 && m <= maxMargin)) { msg.textContent = `Leverage 1–${max}, margin up to ${maxMargin} USDT.`; return; }
        store.set(PREFS, { ...store.get(PREFS, {}), leverage: L, margin: m });
        go.disabled = true; msg.textContent = "Placing the order on Tabdeal…";
        try {
          const t = await this.call("POST", "/api/trade/open", { symbol: sym, side, entry: d.entry, sl: d.sl, tp: d.tp, leverage: L, margin_usdt: m, drawing_id: d.id });
          if (t.status === "REJECTED") { msg.textContent = `Tabdeal rejected it: ${t.last_error}`; go.disabled = false; return; }
          close(); this.tab = "open"; this.setCollapsed(false); this.refresh();
        } catch (err) {
          msg.textContent = err.message; go.disabled = false;
        }
      } },
      h("h2", {}, `${side === "LONG" ? "Long" : "Short"} ${this.ws.display(sym)} on Tabdeal futures`),
      h("p", { class: "small muted" }, "A LIMIT order at the entry. Once it fills, the position gets its stop and target on Tabdeal (cross margin)."),
      h("div", { class: "ws-fields" },
        h("label", { class: "ws-field" }, h("span", {}, `Leverage (1–${max})`), lev),
        h("label", { class: "ws-field" }, h("span", {}, `Margin, USDT (≤ ${maxMargin})`), margin)),
      calc, msg,
      h("div", { class: "ws-dlg-actions" }, h("button", { class: "btn", type: "button", onclick: close }, "Cancel"), go));
      lev.addEventListener("input", update); margin.addEventListener("input", update);
      dlg.append(form);
      dlg.addEventListener("cancel", close);
      this.ws.root.append(dlg);
      dlg.showModal();
      update();
      if (notReady) { msg.textContent = notReady; go.disabled = true; return; }
      try {
        account = await this.call("GET", `/api/trade/account?symbol=${encodeURIComponent(sym)}`);
        if (account.busy) { msg.textContent = `Not now: ${account.busy}.`; go.disabled = true; }
        update();
      } catch (e) { msg.textContent = e.message; }
    }
  }
  window.ChartTrading = Trading;
})();
