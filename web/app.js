/* Eiwaz Trading System — render-only client.
 * Every zone, structure break, position, state, reason and number shown here comes from the
 * backend API. This file holds no strategy logic: it fetches JSON, maps prices and times to
 * pixels, formats values for people and renders them. The only aggregation it does is folding
 * live M1 ticks into the bar of the selected timeframe that is still forming (display only).
 */
"use strict";

/* ---- icons ------------------------------------------------------------------------------ */
const ICONS = {
  check: '<path d="M20 6 9 17l-5-5"/>',
  x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
  alert: '<path d="m21.73 18-8-14a2 2 0 0 0-3.46 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
  dot: '<circle cx="12" cy="12" r="5"/>',
  clock: '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/>',
  moon: '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
  bell: '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/>',
  belloff: '<path d="M8.7 3A6 6 0 0 1 18 8a21.3 21.3 0 0 0 .6 5"/><path d="M17 17H3s3-2 3-9a4.67 4.67 0 0 1 .3-1.7"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/><path d="m2 2 20 20"/>',
  up: '<path d="m5 12 7-7 7 7"/><path d="M12 19V5"/>',
  down: '<path d="M12 5v14"/><path d="m19 12-7 7-7-7"/>',
  flat: '<path d="M5 12h14"/>',
  target: '<circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/>',
  wallet: '<path d="M19 7V4a1 1 0 0 0-1-1H5a2 2 0 0 0 0 4h15a1 1 0 0 1 1 1v4h-3a2 2 0 0 0 0 4h3a1 1 0 0 0 1-1v-2a1 1 0 0 0-1-1"/><path d="M3 5v14a2 2 0 0 0 2 2h15a1 1 0 0 0 1-1v-4"/>',
};
function icon(name, cls) {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  s.setAttribute("viewBox", "0 0 24 24");
  s.setAttribute("fill", "none");
  s.setAttribute("stroke", "currentColor");
  s.setAttribute("stroke-width", "2");
  s.setAttribute("stroke-linecap", "round");
  s.setAttribute("stroke-linejoin", "round");
  s.setAttribute("aria-hidden", "true");
  s.setAttribute("class", cls || "i");
  s.innerHTML = ICONS[name];  // static, trusted markup
  return s;
}
function el(tag, attrs, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") n.className = v;
    else if (k === "style") n.style.cssText = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) {
    if (k === null || k === undefined || k === false) continue;
    n.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
  return n;
}
const $ = (id) => document.getElementById(id);
const show = (v) => (v === null || v === undefined || v === "" ? "—" : String(v));

/* ---- formatting ---------------------------------------------------------------------------- */
const TZ = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
const fmtClock = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", hour12: false });
const fmtDay = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" });
const fmtOffset = new Intl.DateTimeFormat("en-US", { timeZoneName: "shortOffset" });
function tzText() {
  const part = fmtOffset.formatToParts(new Date()).find((p) => p.type === "timeZoneName");
  return `${TZ} (${part ? part.value.replace("GMT", "UTC") : "local"})`;
}
function when(iso) {
  if (!iso) return el("span", {}, "—");
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return el("span", {}, show(iso));
  const today = new Date().toDateString() === d.toDateString();
  return el("time", { datetime: d.toISOString(), title: `${d.toISOString().replace(".000", "")} UTC`, class: "num" },
    today ? fmtClock.format(d) : `${fmtDay.format(d)} ${fmtClock.format(d)}`);
}
function grp(v) {
  if (v === null || v === undefined || v === "") return "—";
  let s = String(v);
  if (/^-?\d+\.\d*0$/.test(s)) s = s.replace(/0+$/, "").replace(/\.$/, "");
  if (!/^-?\d+(\.\d+)?$/.test(s)) return s;
  const [i, f] = s.split(".");
  return i.replace(/\B(?=(\d{3})+(?!\d))/g, ",") + (f ? `.${f}` : "");
}
/** A price of `sym` with that market's own number of decimals. */
function pxs(v, sym) {
  if (v === null || v === undefined) return "—";
  const m = state.markets[sym || state.symbol];
  return grp((+v).toFixed(m ? m.decimals : 2));
}
const usd = (v, d) => (v === null || v === undefined ? "—" : `${+v < 0 ? "−" : ""}${grp(Math.abs(+v).toFixed(d ?? 2))}`);
const usdSigned = (v) => (v === null || v === undefined ? "—" : `${+v > 0 ? "+" : +v < 0 ? "−" : ""}${grp(Math.abs(+v).toFixed(2))}`);
function rText(v) { if (v === null || v === undefined) return "—"; const n = +v; return `${n > 0 ? "+" : ""}${n.toFixed(2)}R`; }
const toneOf = (v) => (v === null || v === undefined ? "" : +v > 0 ? "pos" : +v < 0 ? "neg" : "");
function rEl(v) { return el("span", { class: `num ${toneOf(v)}` }, rText(v)); }
function pct(fraction, digits) {
  if (fraction === null || fraction === undefined) return "—";
  return new Intl.NumberFormat(undefined, { style: "percent", maximumFractionDigits: digits ?? 1 }).format(+fraction);
}
function ago(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 60) return `${seconds} s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return `${Math.floor(seconds / 86400)} d ago`;
}
function tag(tone, label, ic) { return el("span", { class: `tag-tone t-${tone}` }, icon(ic || { ok: "check", bad: "x", warn: "alert" }[tone] || "dot"), label); }
function sideTag(side) {
  const long = side === "LONG";
  return el("span", { class: `side t-${long ? "ok" : "bad"}` }, icon(long ? "up" : "down"), long ? "Long" : "Short");
}
const symName = (s) => (state.markets[s] ? state.markets[s].display : s);
const STATE = { PENDING: ["info", "Pending entry"], OPEN: ["warn", "Open"],
  TP: ["ok", "Target hit"], SL: ["bad", "Stopped out"],
  TIME_STOP: ["neutral", "Time stop"], EXPIRED: ["neutral", "Expired"], MISSED: ["neutral", "Missed"], TIMEOUT: ["neutral", "Timed out"],
  CANCELLED: ["neutral", "Cancelled"] };
const FILLED = ["OPEN"];
const isFilled = (x) => FILLED.includes(x.state);
const stateTag = (s) => { const [t, l] = STATE[s] || ["neutral", s]; return tag(t, l); };
function table(target, headers, data, rowFn, emptyText) {
  const t = typeof target === "string" ? $(target) : target;
  t.replaceChildren(el("thead", {}, el("tr", {}, headers.map((h) => el("th", { scope: "col" }, h)))));
  const body = el("tbody");
  if (!data || !data.length) body.append(el("tr", {}, el("td", { colspan: headers.length, class: "empty" }, emptyText || "Nothing yet")));
  else for (const r of data) body.append(rowFn(r));
  t.append(body);
}
async function api(path) {
  const r = await fetch(path, { headers: { Accept: "application/json" } });
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
}
const q = (path, extra) => `${path}${path.includes("?") ? "&" : "?"}symbol=${encodeURIComponent(state.symbol)}${extra || ""}`;

/* ---- state ---------------------------------------------------------------------------------- */
const prefs = loadPrefs();
const state = { view: "chart", symbol: prefs.symbol || null, markets: {}, symbols: [], ws: null, trends: null,
  allSignals: [], overview: null, live: null,
  sigFilter: "all", sigSym: "all", btSym: null, selectedSig: null, known: null, alerts: !!prefs.alerts, params: {} };
function loadPrefs() { try { return JSON.parse(localStorage.getItem("smc-prefs") || "{}"); } catch { return {}; } }
function savePrefs() { try { localStorage.setItem("smc-prefs", JSON.stringify({ symbol: state.symbol, alerts: state.alerts })); } catch { /* storage unavailable */ } }

/* ---- chart workspace (web/chart/*: the chart, its options and indicators) --------------------- */
function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
function ensureWorkspace() {
  if (state.ws || !window.ChartWorkspace || !state.symbol) return state.ws;
  state.ws = new ChartWorkspace($("chart-ws"), { api, symbol: state.symbol, display: symName,
    format: (s) => (state.markets[s] ? { precision: state.markets[s].decimals, minMove: +state.markets[s].tick } : null) });
  if (state.trends) state.ws.setTrends(state.trends);
  return state.ws;
}
function renderTrends(t) {
  state.trends = t.timeframes;
  $("tf-trends").replaceChildren(...ChartTimeframes.map((tf) => ChartTrendChip(tf, (t.timeframes.find((x) => x.tf === tf) || {}).trend)));
  if (state.ws) state.ws.setTrends(t.timeframes);
}
async function refreshTrends() {
  if (!state.symbol) return;
  try { renderTrends(await api(q("/api/chart/trends"))); } catch { /* transient */ }
}
function renderSymbols() {
  $("sym-seg").replaceChildren(...state.symbols.map((s) => {
    const m = state.markets[s];
    return el("button", { type: "button", role: "tab", "aria-selected": String(s === state.symbol), onclick: () => selectSymbol(s) },
      el("span", {}, m ? m.display : s), el("span", { class: "p" }, m ? pxs(m.price, s) : "—"));
  }));
}
function selectSymbol(s) {
  if (s === state.symbol) return;
  state.symbol = s;
  savePrefs(); renderSymbols();
  if (state.ws) state.ws.setSymbol(s);
  refreshTrends();
  startLive();
  refreshView().catch(() => {});
}
/* Live 1-minute candles from the canonical trade stream, folded into the chart's forming bar. */
function startLive() {
  if (!window.EventSource || !state.symbol) return;
  if (state.live) { state.live.close(); state.live = null; }
  const sym = state.symbol;
  const es = new EventSource(`/api/live/stream?symbol=${encodeURIComponent(sym)}`);
  state.live = es;
  const feed = (m) => { if (sym === state.symbol && state.ws && m && m.o !== undefined && m.status !== "DATA_GAP") state.ws.applyMinute(m); };
  es.addEventListener("snapshot", (e) => { for (const f of JSON.parse(e.data).forming || []) feed(f); });
  es.addEventListener("message", (e) => feed(JSON.parse(e.data)));
}

/* ---- alerts ----------------------------------------------------------------------------------- */
function beep() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const o = ctx.createOscillator(), g = ctx.createGain();
    o.frequency.value = 880; g.gain.value = 0.05; o.connect(g); g.connect(ctx.destination);
    o.start(); o.stop(ctx.currentTime + 0.18);
  } catch { /* audio unavailable */ }
}
function toast(tone, title, text) {
  const t = el("div", { class: `toast t-${tone}` }, el("b", {}, title), el("span", {}, text));
  $("toasts").append(t);
  setTimeout(() => t.remove(), 9000);
  if (state.alerts) {
    beep();
    if (window.Notification && Notification.permission === "granted") { try { new Notification(title, { body: text }); } catch { /* not allowed */ } }
  }
}
function notifyChanges(items) {
  const now = new Map(items.map((s) => [s.id, s.state]));
  if (state.known) {
    for (const s of items) {
      const before = state.known.get(s.id);
      if (before === s.state) continue;
      const head = `${symName(s.symbol)} ${s.side === "LONG" ? "Long" : "Short"}`;
      if (before === undefined) toast(s.side === "LONG" ? "ok" : "bad", `New ${head}`, `Entry ${pxs(s.entry, s.symbol)} · SL ${pxs(s.sl, s.symbol)} · TP ${pxs(s.tp, s.symbol)} · ${usd(s.notional)} $`);
      else if (s.state === "OPEN") toast("info", `${head} filled`, `Entry ${pxs(s.entry, s.symbol)}`);
      else toast(s.state === "TP" ? "ok" : s.state === "SL" ? "bad" : "neutral", `${head} ${STATE[s.state] ? STATE[s.state][1].toLowerCase() : s.state}`, `${rText(s.result_r)}${s.pnl_usdt !== null ? ` · ${usdSigned(s.pnl_usdt)} $` : ""}`);
    }
  }
  state.known = now;
}
function renderAlertBtn() {
  const b = $("alert-btn");
  b.replaceChildren(icon(state.alerts ? "bell" : "belloff"));
  b.setAttribute("aria-pressed", String(state.alerts));
}

/* ---- signals view ------------------------------------------------------------------------------ */
function seg(target, items, current, onPick) {
  $(target).replaceChildren(...items.map(([k, l]) => el("button", { type: "button", role: "tab", "aria-selected": String(current === k), onclick: () => onPick(k) }, l)));
}
async function renderSignals() {
  seg("sig-sym", [["all", "All markets"], ...state.symbols.map((s) => [s, symName(s)])], state.sigSym, (k) => { state.sigSym = k; renderSignals(); });
  seg("sig-filter", [["all", "All"], ["active", "Active"], ["closed", "Closed"]], state.sigFilter, (k) => { state.sigFilter = k; renderSignals(); });
  const symq = state.sigSym === "all" ? "" : `&symbol=${encodeURIComponent(state.sigSym)}`;
  const d = await api(`/api/smc/signals?state=${state.sigFilter}&limit=300${symq}`);
  table("sig-table", ["Created", "Market", "Side", "Setup", "Entry", "SL", "TP", "Size $", "Net R:R", "State", "Result", "PnL $"], d.items, (s) => el("tr", {
    class: "click", "aria-current": String(state.selectedSig === s.id), onclick: () => { state.selectedSig = s.id; renderSignals(); } },
  el("td", {}, when(s.created_at)), el("td", {}, symName(s.symbol)), el("td", {}, sideTag(s.side)),
  el("td", {}, `${s.poi_tf || ""} ${s.trigger_kind === "CHOCH" ? "CHoCH" : "BOS"}${s.version ? "" : " · SMC-1.0"}`),
  el("td", { class: "num" }, pxs(s.entry, s.symbol)), el("td", { class: "num" }, pxs(s.sl, s.symbol)),
  el("td", { class: "num" }, pxs(s.tp, s.symbol)),
  el("td", { class: "num" }, usd(s.notional)), el("td", { class: "num" }, s.net_rr ? `${s.net_rr}R` : "—"), el("td", {}, stateTag(s.state)),
  el("td", {}, isFilled(s) ? rEl(s.open_r) : rEl(s.result_r)),
  el("td", { class: `num ${toneOf(isFilled(s) ? s.open_pnl : s.pnl_usdt)}` }, usdSigned(isFilled(s) ? s.open_pnl : s.pnl_usdt))),
  "No positions yet. They appear here the moment the engine opens one.");
  const sel = d.items.find((s) => s.id === state.selectedSig);
  if (!sel) return;
  const ev = await api(`/api/smc/signals/${sel.id}/events`);
  const su = (sel.detail || {}).setup;
  $("sig-detail").replaceChildren(
    el("div", { class: "ticket-hero" }, el("div", {}, el("b", {}, symName(sel.symbol)), " ", sideTag(sel.side), " ", stateTag(sel.state)),
      el("div", { class: "big num" }, isFilled(sel) ? rText(sel.open_r) : rText(sel.result_r))),
    el("div", { class: "levels" },
      el("div", { class: "t-info" }, el("div", { class: "k" }, "Entry"), el("div", { class: "v num" }, pxs(sel.entry, sel.symbol))),
      el("div", { class: "t-bad" }, el("div", { class: "k" }, "Stop loss"), el("div", { class: "v num" }, pxs(sel.sl, sel.symbol))),
      el("div", { class: "t-ok" }, el("div", { class: "k" }, "Take profit"), el("div", { class: "v num" }, pxs(sel.tp, sel.symbol)))),
    el("dl", { class: "kv small" },
      el("dt", {}, "Setup"), el("dd", { class: "num" }, su ? `${su.tf} OB ${pxs(su.ob.bottom, sel.symbol)} – ${pxs(su.ob.top, sel.symbol)} · FVG ${pxs(su.fvg.bottom, sel.symbol)} – ${pxs(su.fvg.top, sel.symbol)}` : "—"),
      el("dt", {}, "Sweep"), el("dd", { class: "num" }, su && su.sweep ? `${pxs(su.sweep.level, sel.symbol)} (wick ${pxs(su.sweep.wick, sel.symbol)})` : "—"),
      el("dt", {}, "Break"), el("dd", {}, su ? `${su.event.kind === "CHOCH" ? "CHoCH" : "BOS"} @ ${pxs(su.event.level, sel.symbol)}` : "—"),
      el("dt", {}, "Size"), el("dd", { class: "num" }, `${grp(String(+sel.qty))} · ${usd(sel.notional)} $ · margin ${usd(sel.margin)} $`),
      el("dt", {}, "Exit"), el("dd", { class: "num" }, pxs(sel.exit_price, sel.symbol)),
      el("dt", {}, "PnL"), el("dd", { class: `num ${toneOf(sel.pnl_usdt)}` }, sel.pnl_usdt !== null ? `${usdSigned(sel.pnl_usdt)} $ (fees ${usd(sel.fees_usdt)} $)` : "—")),
    el("ol", { class: "timeline" }, ev.items.map((e) => el("li", { class: `t-${{ CREATED: "info", FILLED: "warn", TP: "ok", SL: "bad", BOOKED: "violet" }[e.kind] || "neutral"}` },
      el("b", {}, e.kind), " ", when(e.ts), e.price ? el("span", { class: "num muted" }, ` @ ${pxs(e.price, sel.symbol)}`) : null,
      e.detail && e.detail.result_r ? el("span", {}, " · ", rEl(e.detail.result_r)) : null,
      e.detail && e.detail.part ? el("span", {}, ` · ${e.detail.part} ${pct(e.detail.frac, 0)}`) : null,
      e.detail && e.detail.pnl_usdt ? el("span", { class: `num ${toneOf(e.detail.pnl_usdt)}` }, ` · ${usdSigned(e.detail.pnl_usdt)} $`) : null))),
    el("p", {}, el("button", { class: "btn", type: "button", onclick: () => showOnChart(sel) }, icon("target"), "Show on chart")));
}
function showOnChart(s) {
  selectSymbol(s.symbol);
  location.hash = "#/chart";
  setTimeout(() => { if (state.ws) state.ws.focusTime(s.created_at); }, 900);
}

/* ---- performance view ------------------------------------------------------------------------- */
const statBox = (k, v, tone) => el("div", { class: "stat" }, el("div", { class: "k" }, k), el("div", { class: `v num ${tone || ""}` }, v));
function rStats(target, d) {
  $(target).replaceChildren(
    statBox("Closed trades", show(d.closed)), statBox("Win rate", pct(d.win_rate)), statBox("Total", rText(d.total_r), toneOf(d.total_r)),
    statBox("Average", rText(d.avg_r)), statBox("Profit factor", show(d.profit_factor)), statBox("Max drawdown", d.max_drawdown_r ? `${d.max_drawdown_r}R` : "—"));
}
function drawLine(id, values, labels) {
  const box = $(id);
  if (!values || values.length < 2) { box.replaceChildren(el("p", { class: "empty" }, "The curve appears after the first closed position.")); return; }
  const W = 1000, H = 220, P = 28;
  const base = labels && labels.base !== undefined ? labels.base : 0;
  const lo = Math.min(base, ...values), hi = Math.max(base, ...values);
  const x = (i) => P + (i / (values.length - 1)) * (W - 2 * P);
  const y = (v) => H - P - ((v - lo) / (hi - lo || 1)) * (H - 2 * P);
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`); svg.setAttribute("preserveAspectRatio", "none");
  const zero = document.createElementNS(ns, "line");
  zero.setAttribute("x1", P); zero.setAttribute("x2", W - P); zero.setAttribute("y1", y(base)); zero.setAttribute("y2", y(base));
  zero.setAttribute("stroke", css("--border-strong")); zero.setAttribute("stroke-dasharray", "4 4");
  const path = document.createElementNS(ns, "path");
  path.setAttribute("d", values.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(""));
  path.setAttribute("fill", "none"); path.setAttribute("stroke", values[values.length - 1] >= base ? css("--ok") : css("--bad")); path.setAttribute("stroke-width", "2");
  path.setAttribute("vector-effect", "non-scaling-stroke");
  svg.append(zero, path);
  box.replaceChildren(svg);
}
function bars(target, entries, tones) {
  const max = Math.max(1, ...entries.map(([, n]) => n));
  $(target).replaceChildren(...entries.map(([k, n, label]) => el("div", { class: `bar-row t-${(tones && tones[k]) || "info"}` },
    el("span", {}, label || k), el("div", { class: "track" }, el("div", { class: "fill", style: `width:${(n / max) * 100}%` })), el("span", { class: "n" }, n))));
}
async function renderPerformance() {
  const [w, p] = await Promise.all([api("/api/smc/wallet"), api("/api/smc/performance")]);
  if (w.ready) {
    $("wallet-meta").textContent = `wallet #${w.id} · ${w.symbols.map(symName).join(" + ")} · since ${fmtDay.format(new Date(w.started_at))}`;
    $("wallet-stats").replaceChildren(statBox("Initial", `${usd(w.initial)} $`), statBox("Balance", `${usd(w.balance)} $`),
      statBox("Equity", `${usd(w.equity)} $`, toneOf(+w.equity - +w.initial)), statBox("Return", pct(w.return_pct, 2), toneOf(w.return_pct)),
      statBox("Fees paid", `${usd(w.fees)} $`), statBox("Margin in use", `${usd(w.margin_used)} $`));
    drawLine("wallet-curve", w.curve.map((c) => +c.balance), { base: +w.initial });
    table("ledger-table", ["Booked", "Market", "Side", "Exit", "PnL $", "Fees $", "Balance $"], w.ledger, (l) => el("tr", {},
      el("td", {}, when(l.ts)), el("td", {}, symName(l.symbol)), el("td", {}, l.side ? sideTag(l.side) : "—"), el("td", {}, l.part ? stateTag(l.part) : l.state ? stateTag(l.state) : "—"),
      el("td", { class: `num ${toneOf(l.amount)}` }, usdSigned(l.amount)), el("td", { class: "num" }, l.fees !== null && l.fees !== undefined ? usd(l.fees, 4) : "—"),
      el("td", { class: "num" }, usd(l.balance_after))), "No closed position yet");
  }
  table("sym-table", ["Market", "Closed", "Win rate", "Total R", "Avg R", "Profit factor"], state.symbols, (s) => {
    const d = p.by_symbol[s] || {};
    return el("tr", {}, el("td", {}, symName(s)), el("td", { class: "num" }, show(d.closed)), el("td", { class: "num" }, pct(d.win_rate)),
      el("td", {}, rEl(d.total_r)), el("td", {}, rEl(d.avg_r)), el("td", { class: "num" }, show(d.profit_factor)));
  });
  const bsym = state.btSym || state.symbol;
  seg("bt-sym", state.symbols.map((s) => [s, symName(s)]), bsym, (k) => { state.btSym = k; renderPerformance(); });
  $("bt-meta").textContent = "running…";
  const b = await api(`/api/smc/backtest?days=30&symbol=${encodeURIComponent(bsym)}`);
  if (!b.ready) { $("bt-meta").textContent = "no history yet"; return; }
  $("bt-meta").textContent = `${fmtDay.format(new Date(b.from))} – ${fmtDay.format(new Date(b.to))} · ${grp(b.bars)} M1 bars · ${b.seconds}s`;
  $("bt-note").textContent = +b.total_r < 0
    ? `${symName(bsym)}: with the current parameters and fees this backtest loses ${rText(b.total_r)} over ${b.closed} trades. Treat live positions as experimental until the parameters show a positive expectancy here.`
    : "";
  rStats("bt-stats", b);
  drawLine("bt-equity", b.equity.map((e) => +e.r), { base: 0 });
  const reasons = (state.params[bsym] && state.params[bsym].reasons) || {};
  const st = b.stats;
  bars("bt-funnel", [["SETUPS", st.setups, "Armed setups"], ...Object.entries(st.rejections).map(([k, n]) => [k, n, reasons[k] || k]), ["ACCEPTED", st.accepted, "Accepted"], ["TRADED", st.signals, "Traded (capacity)"]],
    { SETUPS: "neutral", ACCEPTED: "ok", TRADED: "ok" });
  bars("bt-outcomes", [...Object.entries(st.exits || {}).map(([k, n]) => [k, n, k]), ...Object.entries(st.states).filter(([k]) => ["EXPIRED", "MISSED", "PENDING", "OPEN"].includes(k)).map(([k, n]) => [k, n, STATE[k] ? STATE[k][1] : k])],
    Object.fromEntries(Object.keys(st.exits || {}).map((k) => [k, k === "SL" ? "bad" : k === "TP" ? "ok" : "neutral"])));
  const tpx = (t) => (t ? pxs(t.price, bsym) : "—");
  table("bt-trades", ["Created", "Side", "Entry", "SL", "TP", "Exit", "Result"], [...b.trades].reverse().slice(0, 60), (t) => el("tr", {},
    el("td", {}, when(t.created_at)), el("td", {}, sideTag(t.side)), el("td", { class: "num" }, pxs(t.entry, bsym)),
    el("td", { class: "num" }, pxs(t.sl, bsym)), el("td", { class: "num" }, tpx(t.tp)),
    el("td", {}, stateTag(t.state)), el("td", {}, rEl(t.result_r))));
}

/* ---- trend view (System B, paper) --------------------------------------------------------------- */
const TREND_ACTION = { BUY: ["ok", "up", "Buy at the next open"], SELL: ["bad", "down", "Sell at the next open"],
  ADD: ["ok", "up", "Add a unit at the next open"], HOLD: ["info", "flat", "Hold the position"], WAIT: ["neutral", "flat", "No position · waiting for a breakout"] };
const kvRows = (target, rows, text) => $(target).replaceChildren(...rows.flatMap(([k, v]) => [el("dt", {}, k), el("dd", text ? {} : { class: "num" }, v)]));
const px2 = (v) => (v === null || v === undefined ? "—" : usd(v, 2));
async function renderTrend() {
  const t = await api("/api/trend?journal_days=120");
  if (!t.ready) {
    $("trend-note").textContent = `System B is not ready: ${t.reason || "no data"}.`;
    return;
  }
  $("trend-note").textContent = "";
  const p = t.params;
  $("trend-meta").textContent = `${t.symbol} · ${t.version} · params ${t.params_hash} · paper since ${t.paper_start} · signals only, no orders`;
  $("trend-asof").textContent = `UTC day ${t.as_of}`;
  const [tone, ic, label] = TREND_ACTION[t.action] || ["neutral", "dot", t.action];
  const nx = t.next || {};
  $("trend-action").replaceChildren(el("p", {}, tag(tone, label, ic),
    nx.at ? el("span", { class: "muted small" }, ` · ${when(nx.at)}`) : ""));
  kvRows("trend-levels", [
    ["Last close", px2(t.close)],
    [`Buy trigger · close above the ${p.entry_len}-day high`, px2(t.entry_level)],
    [`Exit trigger · close below the ${p.exit_len}-day low`, px2(t.exit_level)],
    [`ATR(${p.atr_len})`, px2(t.atr)],
    ...(nx.action === "BUY" ? [["Planned size (est.)", `${(+nx.est_qty).toFixed(6)} BTC · ${usd(nx.est_notional)} $`],
      ["Planned stop (est.)", px2(nx.est_stop)], ["Risk at the stop", `${usd(nx.risk_usdt)} $`]] : []),
  ]);
  const pos = t.position;
  if (pos) kvRows("trend-position", [["Since", pos.entry_day], ["Entry", px2(pos.entry_price)], ["Size", `${(+pos.qty).toFixed(6)} BTC`],
    ["Value", `${usd(pos.notional)} $`], ["Stop", px2(pos.stop)], ["Unrealized", el("span", { class: toneOf(pos.unrealized_usdt) }, `${usdSigned(pos.unrealized_usdt)} $ · ${rText(pos.r)}`)],
    ["Days held", show(pos.days)]]);
  else $("trend-position").replaceChildren(el("dt", {}, "Flat"), el("dd", {}, "No open position"));
  $("trend-wallet-meta").textContent = `fee ${(t.fees.fee * 100).toFixed(3)} % + slippage ${(t.fees.slippage * 100).toFixed(4)} % per fill`;
  const ts = t.trade_stats || {};
  $("trend-stats").replaceChildren(statBox("Initial", `${usd(t.account_usdt)} $`), statBox("Equity", `${usd(t.equity)} $`, toneOf(t.return_pct)),
    statBox("Return", pct(t.return_pct / 100, 2), toneOf(t.return_pct)), statBox("Max drawdown", t.stats.max_drawdown_pct !== undefined ? pct(t.stats.max_drawdown_pct / 100, 1) : "—"),
    statBox("Closed trades", show(ts.trades)), statBox("Win rate", ts.win_rate_pct === null || ts.win_rate_pct === undefined ? "—" : pct(ts.win_rate_pct / 100)));
  drawLine("trend-curve", t.curve.map((c) => +c.equity), { base: +t.account_usdt });
  table("trend-trades", ["Entry", "Exit", "Exit kind", "Entry price", "Exit price", "PnL $", "Result"], [...t.trades].reverse(), (x) => el("tr", {},
    el("td", {}, x.entry_time), el("td", {}, show(x.exit_time)), el("td", {}, x.reason === "STOP" ? tag("bad", "Stop") : tag("neutral", "Channel exit")),
    el("td", { class: "num" }, px2(x.entry_price)), el("td", { class: "num" }, px2(x.exit_price)),
    el("td", { class: `num ${toneOf(x.pnl)}` }, usdSigned(x.pnl)), el("td", {}, rEl(x.r))), "No closed trade yet");
  table("trend-journal", ["UTC day", "Decision", "Close", "Buy trigger", "Exit trigger", "Stop", "BTC held", "Equity $", "Recorded"], t.journal || [], (j) => el("tr", {},
    el("td", {}, j.day), el("td", {}, tag(...(TREND_ACTION[j.action] || ["neutral", "dot"]).slice(0, 1), j.action)),
    el("td", { class: "num" }, px2(j.close)), el("td", { class: "num" }, px2(j.entry_level)), el("td", { class: "num" }, px2(j.exit_level)),
    el("td", { class: "num" }, px2(j.stop)), el("td", { class: "num" }, (+j.position_qty).toFixed(6)), el("td", { class: "num" }, usd(j.equity)),
    el("td", { class: "muted small" }, when(j.recorded_at))), "The journal starts with the first closed UTC day after the trend service starts");
  kvRows("trend-rules", [
    ["Entry", `daily close above the highest high of the previous ${p.entry_len} days, filled at the next 00:00 UTC open`],
    ["Initial stop", `${p.stop_atr} × ATR(${p.atr_len}) below the fill, active from the fill`],
    ["Exit", `daily close below the lowest low of the previous ${p.exit_len} days, at the next open; or the stop`],
    ["Size", `${(p.risk_pct * 100).toFixed(1)} % of equity lost at the stop; at most ${p.max_exposure}× equity (spot, long only)`],
    ["Evidence", "docs/trend/report.md, report2.md, report3.md"],
  ], true);
}

/* ---- strategy view ----------------------------------------------------------------------------- */
async function loadParams(sym) {
  if (!state.params[sym]) state.params[sym] = await api(`/api/smc/params?symbol=${encodeURIComponent(sym)}`);
  return state.params[sym];
}
async function renderStrategy() {
  const p = await loadParams(state.symbol);
  const v = Object.fromEntries(p.groups.flatMap((g) => g.items.map((i) => [i.key, i.value])));
  $("strat-ver").textContent = `${symName(state.symbol)} · ${p.version} · parameters ${p.params_hash}`;
  const step = (tone, h, tfs, text) => el("li", { class: `t-${tone}` }, el("div", { class: "h" }, h), el("div", { class: "tfs" }, tfs), el("p", {}, text));
  $("model-flow").replaceChildren(
    step("info", "Bias", v.bias_tf, "The trend of the bias timeframe's last BOS / CHoCH is the only direction that can be traded."),
    step("violet", "Setup", v.zone_tf, `In this order: ${v.require_sweep ? `liquidity sweep (wick beyond unswept swing lows / equal lows within ${v.eq_tol_atr} ATR, close back inside) → a close beyond structure within ${v.sweep_max_bars} bars` : "a close beyond structure (BOS / CHoCH; no liquidity sweep required)"} → order block = last opposite candle (≥ ${v.ob_min_atr} ATR) with the FVG right after it.`),
    step("warn", "Entry", `${v.zone_tf} → ${v.confirm_exec ? `${v.exec_tf} → ` : ""}M1`, (() => {
      const at = {
        ob_edge: "the order-block edge touching the FVG",
        htf_fvg_ce: `50 % of the ${v.zone_tf} FVG`, fvg_mid: `50 % of the ${v.zone_tf} FVG`,
        ltf_fvg_ce: `50 % of the ${v.exec_tf} FVG of the confirming move (else the edge of its ${v.exec_tf} order block)`,
        ltf_ob_edge: `the edge of the ${v.exec_tf} order block of the confirming move`,
      }[v.entry_ref] || v.entry_ref;
      if (!v.confirm_exec) return `Limit order (maker) at ${at}, resting from the setup; armed when price first trades into the FVG, cancelled ${v.pending_expiry_min / 60} h later. Only a fresh order block.`;
      const win = (+v.confirm_window_min || +v.pending_expiry_min) / 60;
      const zone = v.confirm_in_zone ? ` reacting from a ${v.exec_tf} swing inside the zone (order block + FVG); a ${v.exec_tf} close beyond the order block's far edge first cancels the setup` : "";
      return v.confirm_entry === "limit"
        ? `When price first trades into the FVG, wait up to ${win} h for a ${v.exec_tf} BOS / CHoCH with the setup${zone}; then a limit order (maker) at ${at}, cancelled ${v.pending_expiry_min / 60} h later. Only a fresh, valid order block; one order per setup.`
        : `When price first trades into the FVG, wait up to ${win} h for a ${v.exec_tf} BOS / CHoCH with the setup${zone} and enter at market at its close.`;
    })()),
    step("ok", "Exit", `${v.exec_tf} · M1`, (() => {
      const sl = { ob_height: "Stop beyond the order block by its own height (long: OB low − OB height).",
        structure: `Stop beyond the farther of the order block's wick and the sweep wick, plus ${v.sl_buffer_atr} × ATR(${v.zone_tf}).`,
      }[v.sl_mode] || "Stop one tick beyond the order block's wick.";
      const mode = v.tp_mode === "hh_ll" && +v.tp_rr > 0 ? "fixed" : v.tp_mode;
      const tp = mode === "fixed" ? `One target at ${v.tp_rr} × the stop distance (price R:R 1:${v.tp_rr}).`
        : mode === "liquidity" ? `One target at the nearest unswept liquidity beyond the entry (the displacement high / low, unswept ${v.zone_tf} and 1h swings and equal highs / lows, the previous day's high / low), ${v.tp_front_run_atr} ATR before it; never a farther level.`
        : `One target, from the market, never from the stop: the ${v.tp_ref === "swing" ? "last confirmed" : "previous"} ${v.zone_tf} HH (long: its high) / LL (short: its low), ${v.tp_front_run_atr} ATR before it. None beyond the entry: no trade.`;
      const disc = v.require_discount ? (v.discount_ref === "displacement" ? "order-block edge in the discount (long) / premium (short) half of sweep wick → displacement high / low, " : "long entry in the lower half of sweep wick → HH (short: upper half), ") : "";
      const filters = `${disc}net R at the TP ≥ ${v.min_net_rr}${+v.max_cost_frac > 0 ? `, costs ≤ ${pct(v.max_cost_frac, 0)} of the stop` : ""}${+v.liq_buffer_r > 0 ? `, liquidation ≥ ${v.liq_buffer_r} × the stop beyond the stop (else smaller size)` : ""}`;
      return `${sl} ${tp} Filters (they reject, never move stop or target): ${filters}${v.exit_on_choch ? `; exit at a ${v.zone_tf} CHoCH against the trade` : ""}.${+v.time_stop_min > 0 ? ` Time stop ${v.time_stop_min / 60} h,` : ""} Closed at market after ${v.max_hold_min / 60} h. Size: ${pct(v.risk_pct)} of the shared wallet, ≤ ${v.max_leverage}x.`;
    })()));
  $("param-grid").replaceChildren(...p.groups.map((g) => el("section", { class: "card" }, el("h2", {}, g.name),
    el("dl", { class: "kv" }, g.items.flatMap((i) => [el("dt", {}, el("code", {}, i.key)), el("dd", { class: "num" }, Array.isArray(i.value) ? i.value.join(", ") || "—" : show(i.value))])))),
  el("section", { class: "card" }, el("h2", {}, "Costs (from config)"), el("dl", { class: "kv" },
    ...Object.entries(p.costs).flatMap(([k, x]) => [el("dt", {}, el("code", {}, k)), el("dd", { class: "num" }, pct(x, 3))]),
    ...(p.funding_interval_h !== undefined ? [el("dt", {}, el("code", {}, "funding_interval_h")), el("dd", { class: "num" }, `${p.funding_interval_h} h${+p.costs.funding_rate === 0 ? " (no rate published: modelled 0)" : ""}`)] : []))));
  const kvs = (target, pairs) => $(target).replaceChildren(...pairs.flatMap(([k, t]) => [el("dt", {}, el("code", {}, k)), el("dd", {}, t)]));
  kvs("factor-list", Object.entries(p.rules));
  kvs("reason-list", Object.entries(p.reasons));
}

/* ---- system view -------------------------------------------------------------------------------- */
async function renderSystem() {
  const o = state.overview || await api("/api/overview");
  table("sys-markets", ["Market", "Engine", "Heartbeat", "Last M1 analysed", "History", "Collector", "Collector heartbeat", "Last 60 min"], o.markets, (m) => {
    const run = m.smc.runner, h = m.smc.history, c = m.collector;
    return el("tr", {}, el("td", {}, m.display),
      el("td", {}, run ? (run.status === "RUNNING" ? tag("ok", "Running") : tag("bad", "Stale")) : tag("bad", "Not started")),
      el("td", {}, run ? ago(run.heartbeat_age_s) : "—"), el("td", {}, when(run && run.last_m1)),
      el("td", { class: "num" }, h.days !== null ? `${h.days} / ${h.target_days} d` : "—"),
      el("td", {}, c.status === "CONNECTED" ? tag("ok", "Connected") : tag(c.status === "NO_DATA" ? "neutral" : "warn", c.status)),
      el("td", {}, ago(c.heartbeat_age_s)), el("td", {}, m.market.quality.label));
  });
  $("dq-sym").textContent = symName(state.symbol);
  const qd = await api(q("/api/market/quality?minutes=180"));
  $("dq-strip").replaceChildren(...qd.timeline.map((m) => el("span", { class: m.status === "OK" ? "ok" : m.status === "SYNTHETIC" ? "syn" : "gap", title: `${fmtClock.format(new Date(m.minute))} ${m.status}` })));
  table("sys-gaps", ["From", "To", "Reason", "Timeframe"], qd.gaps, (g) => el("tr", {}, el("td", {}, when(g.gap_start)), el("td", {}, when(g.gap_end)), el("td", {}, show(g.reason)), el("td", {}, show(g.timeframe))), "No gaps recorded");
}

/* ---- top bar, routing, refresh ----------------------------------------------------------------- */
function applyOverview(o) {
  state.overview = o;
  state.symbols = o.symbols;
  for (const m of o.markets) state.markets[m.symbol] = m;
  if (!state.symbol || !state.markets[state.symbol]) state.symbol = o.symbols[0];
  const m = state.markets[state.symbol];
  const runs = o.markets.map((x) => x.smc.runner).filter(Boolean);
  const allRun = runs.length === o.markets.length && runs.every((r) => r.status === "RUNNING");
  const rp = $("runner-pill");
  rp.style.setProperty("--tone", css(allRun ? "--ok" : "--bad"));
  rp.replaceChildren(el("span", { class: "dot" }), allRun ? "Engine running" : "Engine stopped");
  const feedUp = m.collector.status === "CONNECTED";
  const fp = $("feed-pill");
  fp.style.setProperty("--tone", css(feedUp ? "--ok" : "--warn"));
  fp.replaceChildren(el("span", { class: "dot" }), feedUp ? "Live feed" : "History only");
  fp.title = feedUp ? `Live trades for ${m.display}` : "No live collector: candles come from Tabdeal history, refreshed every minute";
  $("foot-ver").textContent = `Eiwaz Trading System · ${o.symbols.map(symName).join(" + ")}`;
}
function route() {
  const v = (location.hash.replace("#/", "") || "chart").split("?")[0];
  state.view = ["chart", "signals", "performance", "strategy", "trend", "system"].includes(v) ? v : "chart";
  document.body.dataset.view = state.view;
  for (const s of document.querySelectorAll(".view")) s.hidden = s.id !== `view-${state.view}`;
  for (const a of document.querySelectorAll("#nav a")) {
    if (a.dataset.view === state.view) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
  refreshView().catch(() => {});
}
async function refreshFast() {
  try {
    const [o, s] = await Promise.all([api("/api/overview"), api("/api/smc/signals?limit=200")]);
    applyOverview(o);
    notifyChanges(s.items);
    state.allSignals = s.items;
    renderSymbols();
  } catch { /* transient; next tick retries */ }
}
async function refreshView() {
  if (!state.overview) await refreshFast();
  if (state.view === "chart") ensureWorkspace();
  else if (state.view === "signals") await renderSignals();
  else if (state.view === "performance") { await Promise.all(state.symbols.map(loadParams)).catch(() => {}); await renderPerformance(); }
  else if (state.view === "strategy") await renderStrategy();
  else if (state.view === "trend") await renderTrend();
  else if (state.view === "system") await renderSystem();
}
function setTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem("smc-theme", t); } catch { /* storage unavailable */ }
  $("theme-btn").replaceChildren(icon(t === "dark" ? "sun" : "moon"));
  if (state.ws) state.ws.restyle();
}
async function init() {
  let theme = "dark";
  try { theme = localStorage.getItem("smc-theme") || (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark"); } catch { /* default */ }
  setTheme(theme);
  $("theme-btn").addEventListener("click", () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));
  renderAlertBtn();
  $("alert-btn").addEventListener("click", () => {
    state.alerts = !state.alerts; savePrefs(); renderAlertBtn();
    if (state.alerts && window.Notification && Notification.permission === "default") Notification.requestPermission();
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && state.ws && state.ws.isFull() && !document.fullscreenElement) state.ws.toggleFull(); });
  $("tz-label").textContent = `Times in ${tzText()}`;
  try { await refreshFast(); } catch { /* retried below */ }
  window.addEventListener("hashchange", route);
  route();
  refreshTrends();
  startLive();
  setInterval(refreshFast, 5000);
  setInterval(refreshTrends, 30000);
}
init();
