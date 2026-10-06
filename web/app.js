/* SMC Console — render-only client.
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
  expand: '<path d="M15 3h6v6"/><path d="M9 21H3v-6"/><path d="M21 3l-7 7"/><path d="M3 21l7-7"/>',
  shrink: '<path d="M4 14h6v6"/><path d="M20 10h-6V4"/><path d="M14 10l7-7"/><path d="M3 21l7-7"/>',
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
const TREND = { BULLISH: ["ok", "up", "Bullish"], BEARISH: ["bad", "down", "Bearish"], UNDEFINED: ["neutral", "flat", "Undefined"] };
const STATE = { PENDING: ["info", "Pending entry"], OPEN: ["warn", "Open"],
  TP: ["ok", "Target hit"], SL: ["bad", "Stopped out"],
  TIME_STOP: ["neutral", "Time stop"], EXPIRED: ["neutral", "Expired"], MISSED: ["neutral", "Missed"], TIMEOUT: ["neutral", "Timed out"],
  CANCELLED: ["neutral", "Cancelled"] };
const ACTIVE = ["PENDING", "OPEN"], FILLED = ["OPEN"];
const isActive = (x) => ACTIVE.includes(x.state), isFilled = (x) => FILLED.includes(x.state);
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
const TFS = ["1m", "5m", "15m", "1h", "4h"];
const TF_SEC = { "1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400 };
const TF_OFFSET = { "1m": 0, "5m": 0, "15m": 0, "1h": 1800, "4h": 1800 };  // Tabdeal's chart grid
const TF_BARS = { "1m": 600, "5m": 500, "15m": 400, "1h": 300, "4h": 180 };
const LAYERS = [
  ["ob", "Order blocks", "--c-bull"], ["fvg", "Fair value gaps", "--c-fvg-bull"], ["structure", "BOS / CHoCH", "--c-text"],
  ["liquidity", "Liquidity", "--c-liq"], ["pd", "Premium / discount", "--c-pd-hi"], ["htf", "Higher-TF zones", "--c-fvg-bear"],
  ["positions", "Positions", "--c-bull"],
];
const prefs = loadPrefs();
const state = { view: "chart", symbol: prefs.symbol || null, tf: prefs.tf || "15m", mode: prefs.mode === "debug" ? "debug" : "setups",
  layers: Object.assign({ ob: true, fvg: true, structure: true, liquidity: true, pd: false, htf: true, positions: true }, prefs.layers || {}),
  markets: {}, symbols: [], chart: null, series: null, layer: null, data: [], times: [], byTime: new Map(), analysis: null,
  signals: [], allSignals: [], focus: null, overview: null, radar: null, live: null, lastPrice: null,
  sigFilter: "all", sigSym: "all", btSym: null, selectedSig: null, known: null, alerts: !!prefs.alerts, params: {} };
function loadPrefs() { try { return JSON.parse(localStorage.getItem("smc-prefs") || "{}"); } catch { return {}; } }
function savePrefs() { try { localStorage.setItem("smc-prefs", JSON.stringify({ symbol: state.symbol, tf: state.tf, mode: state.mode, layers: state.layers, alerts: state.alerts })); } catch { /* storage unavailable */ } }

/* ---- what the chart draws (display only) --------------------------------------------------
 * "Setups" (default) draws only what the engine can trade: the zone setups the backend selected
 * (fresh, valid, with the bias, near the price or tied to a position) with their FVG, sweep,
 * structure break and target levels, plus the positions. "All zones (debug)" draws every zone,
 * gap, break, liquidity level and setup the backend returned. */
function setupEvent(s) {
  return { id: s.event.id, kind: s.event.kind, direction: s.direction, level: s.event.level, from: s.event.from, to: s.event.to };
}
function shown() {
  const a = state.analysis;
  if (!a || !a.ready) return a;
  if (state.mode === "debug") return { ...a, setups: a.setups_all || [], debug: true };
  const setups = a.setups || [];
  const own = setups.map(setupEvent), ids = new Set(own.map((e) => e.id));
  const events = [...own, ...(a.events || []).slice(-3).filter((e) => !ids.has(e.id))];
  return { ...a, zones: [], htf_zones: [], liquidity: [], events, setups };
}
/* Positions on the chart: the active ones; a closed one only after "Show on chart" (Signals). */
function shownPositions() {
  return state.signals.filter((x) => isActive(x) || x.id === state.focus);
}

/* ---- chart: SMC drawing layer (series primitive) -------------------------------------------- */
function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
const rgba = (v, a) => `rgba(${css(v)},${a})`;
const secs = (iso) => Math.floor(Date.parse(iso) / 1000);

/** x pixel of an epoch-seconds time, between/after bars as well (gaps keep their spacing). */
function xOf(t) {
  const ts = state.chart.timeScale();
  const T = state.times;
  if (!T.length) return null;
  const step = TF_SEC[state.tf];
  let lo = 0, hi = T.length - 1;
  if (t <= T[0]) return ts.logicalToCoordinate((t - T[0]) / step);
  if (t >= T[hi]) return ts.logicalToCoordinate(hi + (t - T[hi]) / step);
  while (hi - lo > 1) { const m = (lo + hi) >> 1; if (T[m] <= t) lo = m; else hi = m; }
  return ts.logicalToCoordinate(lo + (t - T[lo]) / (T[hi] - T[lo]));
}
class SmcLayer {
  constructor() { this.views = [new LayerView("bottom"), new LayerView("top")]; }
  attached(p) { this.request = p.requestUpdate; }
  detached() { this.request = null; }
  paneViews() { return this.views; }
  updateAllViews() {}
  update() { if (this.request) this.request(); }
}
class LayerView {
  constructor(z) { this.z = z; }
  zOrder() { return this.z; }
  renderer() { return { draw: (target) => target.useMediaCoordinateSpace(({ context, mediaSize }) => (this.z === "bottom" ? drawBelow : drawAbove)(context, mediaSize)) }; }
}
const yOf = (p) => state.series.priceToCoordinate(+p);
function span(from, to, width) {
  const x1 = from ? xOf(secs(from)) : 0;
  const x2 = to ? xOf(secs(to)) : width;
  if (x1 === null || x2 === null) return null;
  if (x2 < 0 || x1 > width) return null;  // entirely outside the visible pane
  return [Math.max(-2, x1), Math.min(width + 2, x2)];
}
/* Labels of one frame: each new one avoids the boxes already placed and keeps a margin from the
 * pane edges (some browsers clip the outermost pixels of the chart canvas). */
const EDGE = 14;
let placed = [];
let zoneLabels = [];  // zone boxes are drawn under the candles, their labels over everything
function place(bx, by, w, h, size) {
  const hits = (y) => placed.some((r) => bx < r.x + r.w && r.x < bx + w && y < r.y + r.h && r.y < y + h);
  let y = Math.min(Math.max(by, EDGE), size.height - h - EDGE);
  for (let k = 0; k < 8 && hits(y); k++) {
    const down = y + h + 3 <= size.height - h - EDGE ? y + h + 3 : null;
    y = down !== null ? down : Math.max(EDGE, y - (h + 3) * (k + 1));
  }
  placed.push({ x: bx, y, w, h });
  return y;
}
/** A readable label pill, always kept inside the pane. align: left edge at x / right edge at x. */
function pill(ctx, text, x, y, color, size, align) {
  ctx.font = "600 11px 'IBM Plex Sans', sans-serif";
  ctx.textBaseline = "middle";
  ctx.textAlign = "left";
  const w = ctx.measureText(text).width + 10, h = 17;
  let bx = align === "right" ? x - w : align === "center" ? x - w / 2 : x;
  bx = Math.min(Math.max(bx, 3), size.width - w - 3);
  const by = place(bx, y - h / 2, w, h, size);
  ctx.fillStyle = rgba(color, 0.92);
  ctx.beginPath(); ctx.roundRect(bx, by, w, h, 4); ctx.fill();
  ctx.fillStyle = rgba("--c-on", 1);
  ctx.fillText(text, bx + 5, by + h / 2 + 0.5);
}
function plainLabel(ctx, text, x, y, color, size, align) {
  ctx.font = "500 11px 'IBM Plex Sans', sans-serif";
  ctx.textBaseline = "middle";
  const w = ctx.measureText(text).width;
  let tx = align === "right" ? x - w : x;
  tx = Math.min(Math.max(tx, 3), size.width - w - 3);
  y = place(tx - 2, y - 7, w + 4, 14, size) + 7;
  ctx.textAlign = "left";
  ctx.lineWidth = 3; ctx.strokeStyle = rgba("--c-pill", 0.85); ctx.strokeText(text, tx, y);
  ctx.fillStyle = rgba(color, 1); ctx.fillText(text, tx, y);
}
function zoneColor(z) {
  if (z.kind === "OB") return z.direction === "LONG" ? "--c-bull" : "--c-bear";
  return z.direction === "LONG" ? "--c-fvg-bull" : "--c-fvg-bear";
}
let hatchCache = null;
function hatch(ctx, color) {
  const key = `${color}|${css(color)}`;
  if (hatchCache && hatchCache.key === key) return hatchCache.pattern;
  const c = document.createElement("canvas"); c.width = 8; c.height = 8;
  const g = c.getContext("2d");
  g.strokeStyle = rgba(color, 0.35); g.lineWidth = 1;
  g.beginPath(); g.moveTo(0, 8); g.lineTo(8, 0); g.stroke();
  hatchCache = { key, pattern: ctx.createPattern(c, "repeat") };
  return hatchCache.pattern;
}
const invalid = (z) => z.status === "MITIGATED" || z.status === "EXPIRED" || z.status === "FILLED";
/* Fresh = price has not come back to the zone since it formed: solid, bright border and a
 * "Fresh" label. A touched zone stays valid but is drawn quieter. */
function drawZone(ctx, z, size, htf) {
  if (invalid(z)) return;  // invalid zones are never drawn
  const s = span(z.from, z.to, size.width);
  const y1 = yOf(z.top), y2 = yOf(z.bottom);
  if (!s || y1 === null || y2 === null) return;
  const c = zoneColor(z), fresh = !z.tested;
  const h = Math.max(2, y2 - y1);
  const light = z.kind === "FVG";  // imbalance: lighter fill
  ctx.fillStyle = rgba(c, htf ? 0.08 : z.kind === "OB" ? (fresh ? 0.24 : 0.14) : light ? (fresh ? 0.1 : 0.05) : (fresh ? 0.2 : 0.12));
  ctx.fillRect(s[0], y1, s[1] - s[0], h);
  if (htf) { ctx.fillStyle = hatch(ctx, c); ctx.fillRect(s[0], y1, s[1] - s[0], h); hatchCache = null; }
  ctx.strokeStyle = rgba(c, fresh ? 1 : htf ? 0.8 : 0.55);
  ctx.lineWidth = fresh ? 2 : htf ? 1.4 : 1;
  ctx.setLineDash(htf ? [6, 3] : z.kind === "FVG" ? [3, 2] : []);
  ctx.strokeRect(s[0], y1, s[1] - s[0], h);
  ctx.setLineDash([]);
  const text = `${fresh ? "Fresh " : ""}${htf ? `${z.tf} ` : ""}${z.kind}${z.n > 1 ? ` ×${z.n}` : ""}`;
  // higher-timeframe labels sit at the right edge, own-timeframe labels at the zone's visible start
  if (htf) zoneLabels.push([text, s[1] - 4, y1 + 10, c, "right", fresh]);
  else if (s[1] - s[0] > 30) zoneLabels.push([text, Math.max(s[0], 0) + 3, y1 + 10, c, "left", fresh]);
}
/** The gaps the move from an order block to its break left (any size). Open ones are fair
 * value gaps like any other; a filled one is a faint box over its three candles only. */
function drawGaps(ctx, z, size, htf, drawn) {
  for (const g of z.gaps || []) {
    if (g.status !== "FILLED") { if (!drawn.has(g.id)) { drawn.add(g.id); drawZone(ctx, g, size, htf); } continue; }
    const s = span(g.from, g.to, size.width), y1 = yOf(g.top), y2 = yOf(g.bottom);
    if (!s || y1 === null || y2 === null) continue;
    const c = zoneColor(g);
    ctx.fillStyle = rgba(c, 0.1); ctx.fillRect(s[0], y1, s[1] - s[0], Math.max(2, y2 - y1));
    ctx.strokeStyle = rgba(c, 0.5); ctx.lineWidth = 1; ctx.setLineDash([2, 2]);
    ctx.strokeRect(s[0], y1, s[1] - s[0], Math.max(2, y2 - y1)); ctx.setLineDash([]);
  }
}
/* A zone setup: the order block coloured by its state, the FVG right after it and the sweep
 * that started it (dashed line at the swept level, dot at the wick). */
const SETUP_COLOR = { armed: "--c-armed", pending: "--c-pending", open: "--c-open", used: "--c-text", invalid: "--c-text", expired: "--c-text" };
const setupColor = (s) => SETUP_COLOR[s.state] || (s.direction === "LONG" ? "--c-bull" : "--c-bear");
const SPENT = ["used", "invalid", "expired"];
function ageText(min) {
  if (min === null || min === undefined) return "";
  return min < 60 ? `${min}m` : min < 1440 ? `${Math.floor(min / 60)}h` : `${Math.floor(min / 1440)}d`;
}
function drawSetup(ctx, s, size) {
  const W = size.width, c = setupColor(s), spent = SPENT.includes(s.state);
  const ob = span(s.ob.time, null, W), y1 = yOf(s.ob.top), y2 = yOf(s.ob.bottom);
  if (ob && y1 !== null && y2 !== null) {
    const h = Math.max(2, y2 - y1);
    ctx.fillStyle = rgba(c, spent ? 0.05 : 0.2); ctx.fillRect(ob[0], y1, ob[1] - ob[0], h);
    ctx.strokeStyle = rgba(c, spent ? 0.35 : 1); ctx.lineWidth = spent ? 1 : 2; ctx.setLineDash(spent ? [3, 3] : []);
    ctx.strokeRect(ob[0], y1, ob[1] - ob[0], h); ctx.setLineDash([]);
    const pl = s.plan;
    const why = pl && !pl.accepted && pl.reasons.length ? ` · ${pl.reasons.map((r) => r.code).join(", ")}` : "";
    const rr = pl && pl.tp ? ` · ${(+pl.tp.net_r).toFixed(2)}R` : "";
    const cost = pl && pl.cost_frac !== null && pl.cost_frac !== undefined ? ` · cost ${pct(pl.cost_frac, 0)}` : "";
    zoneLabels.push([`${s.tf} OB · ${ageText(s.age_min)} · ${s.state}${rr}${cost}${why}`, Math.max(ob[0], 0) + 3, y1 + 10, c, "left", !spent]);
  }
  const fc = s.direction === "LONG" ? "--c-fvg-bull" : "--c-fvg-bear";
  const fv = span(s.fvg.time, null, W), f1 = yOf(s.fvg.top), f2 = yOf(s.fvg.bottom);
  if (fv && f1 !== null && f2 !== null) {
    const h = Math.max(2, f2 - f1);
    ctx.fillStyle = rgba(fc, spent ? 0.04 : 0.14); ctx.fillRect(fv[0], f1, fv[1] - fv[0], h);
    ctx.strokeStyle = rgba(fc, spent ? 0.3 : 0.85); ctx.lineWidth = 1; ctx.setLineDash([3, 2]);
    ctx.strokeRect(fv[0], f1, fv[1] - fv[0], h); ctx.setLineDash([]);
    if (!spent) zoneLabels.push([`${s.tf} FVG`, Math.max(fv[0], 0) + 3, f1 + (s.direction === "LONG" ? 10 : -10), fc, "left", false]);
  }
  if (!s.sweep) return;
  const xs = xOf(secs(s.sweep.time)), yl = yOf(s.sweep.level), yw = yOf(s.sweep.wick);
  if (xs !== null && yl !== null && yw !== null && xs > -40 && xs < W + 40) {
    ctx.strokeStyle = rgba("--c-liq", spent ? 0.4 : 0.95); ctx.lineWidth = 1.4; ctx.setLineDash([4, 3]);
    ctx.beginPath(); ctx.moveTo(xs - 36, yl); ctx.lineTo(xs + 14, yl); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = rgba("--c-liq", spent ? 0.4 : 1); ctx.beginPath(); ctx.arc(xs, yw, 3.5, 0, Math.PI * 2); ctx.fill();
    if (!spent) zoneLabels.push(["Sweep", xs - 36, yl + (s.direction === "LONG" ? 11 : -11), "--c-liq", "left", false, "plain"]);
  }
}
/** Planned stop and target of a setup that has no order yet (dotted, from its break). */
function drawPlan(ctx, s, size, drawnPx) {
  const pl = s.plan, W = size.width;
  if (!pl || pl.entry === null) return;
  const x0 = xOf(secs(s.event.to));
  if (x0 === null || x0 > W) return;
  const long = s.direction === "LONG";
  const line = (px, c, text, dash) => {
    const y = yOf(px);
    if (y === null || drawnPx.has(`${px}`)) return;
    drawnPx.add(`${px}`);
    ctx.strokeStyle = rgba(c, 0.75); ctx.lineWidth = 1; ctx.setLineDash(dash);
    ctx.beginPath(); ctx.moveTo(Math.max(x0, 0), y); ctx.lineTo(W, y); ctx.stroke(); ctx.setLineDash([]);
    plainLabel(ctx, text, W - 6, y + (long ? -9 : 9), c, size, "right");
  };
  if (pl.sl) line(pl.sl, "--c-bear", `planned SL ${pxs(pl.sl)}`, [2, 3]);
  if (pl.tp) {
    line(pl.tp.price, "--c-bull", `planned TP ${pxs(pl.tp.price)} · ${(+pl.tp.net_r).toFixed(2)}R`, [1, 3]);
    if (pl.tp.level) line(pl.tp.level, "--c-liq", `${pl.tp.source} ${pxs(pl.tp.level)}`, [1, 2]);
  }
  if (pl.range_mid) line(pl.range_mid, "--c-text", `50 % ${pxs(pl.range_mid)}`, [1, 4]);
}
function drawBelow(ctx, size) {
  zoneLabels = [];
  drawBelowZones(ctx, size);
}
function drawBelowZones(ctx, size) {
  const a = shown();
  if (!a || !a.ready || !state.series) return;
  const W = size.width, L = state.layers;
  if (L.pd && a.range) {
    const s = span(a.range.from, null, W), yh = yOf(a.range.high), ye = yOf(a.range.eq), yl = yOf(a.range.low);
    if (s && yh !== null && yl !== null) {
      ctx.fillStyle = rgba("--c-pd-hi", 0.06); ctx.fillRect(s[0], yh, s[1] - s[0], ye - yh);
      ctx.fillStyle = rgba("--c-pd-lo", 0.06); ctx.fillRect(s[0], ye, s[1] - s[0], yl - ye);
      ctx.strokeStyle = rgba("--c-text", 0.4); ctx.setLineDash([2, 4]);
      ctx.beginPath(); ctx.moveTo(s[0], ye); ctx.lineTo(s[1], ye); ctx.stroke(); ctx.setLineDash([]);
      plainLabel(ctx, "Premium", W - 6, yh + 10, "--c-pd-hi", size, "right");
      plainLabel(ctx, "Equilibrium", W - 6, ye - 9, "--c-text", size, "right");
      plainLabel(ctx, "Discount", W - 6, yl - 10, "--c-pd-lo", size, "right");
    }
  }
  const drawn = new Set();
  if (L.htf) {
    for (const z of a.htf_zones || []) if ((z.kind === "OB" && L.ob) || (z.kind === "FVG" && L.fvg)) { drawn.add(z.id); drawZone(ctx, z, size, true); }
    if (L.ob && L.fvg) for (const z of a.htf_zones || []) drawGaps(ctx, z, size, true, drawn);
  }
  const own = (a.zones || []).filter((z) => (z.kind === "OB" && L.ob) || (z.kind === "FVG" && L.fvg));
  for (const z of own) { drawn.add(z.id); drawZone(ctx, z, size, false); }
  if (L.ob && L.fvg) for (const z of own) drawGaps(ctx, z, size, false, drawn);
  if (L.ob) for (const st of a.setups || []) drawSetup(ctx, st, size);
}
function drawAbove(ctx, size) {
  placed = [];  // same start on every redraw: labels never drift
  const a = shown();
  if (!state.series) return;
  const W = size.width, L = state.layers;
  if (a && a.ready && L.structure) {
    for (const e of a.events || []) {
      const s = span(e.from, e.to, W), y = yOf(e.level);
      if (!s || y === null) continue;
      const c = e.direction === "LONG" ? "--c-bull" : "--c-bear";
      ctx.strokeStyle = rgba(c, 0.95); ctx.lineWidth = 1.3; ctx.setLineDash(e.kind === "CHOCH" ? [6, 3] : [2, 2]);
      ctx.beginPath(); ctx.moveTo(s[0], y); ctx.lineTo(s[1], y); ctx.stroke(); ctx.setLineDash([]);
      if (s[1] - s[0] > 24) plainLabel(ctx, e.kind === "CHOCH" ? "CHoCH" : "BOS", (s[0] + s[1]) / 2 - 14, y + (e.direction === "LONG" ? -9 : 9), c, size, "left");
    }
  }
  if (a && a.ready && L.liquidity) {
    for (const lq of a.liquidity || []) {
      const s = span(lq.from, null, W), y = yOf(lq.price);
      if (!s || y === null) continue;
      ctx.strokeStyle = rgba("--c-liq", 0.9); ctx.setLineDash([1, 3]); ctx.lineWidth = 1.6;
      ctx.beginPath(); ctx.moveTo(s[0], y); ctx.lineTo(s[1], y); ctx.stroke(); ctx.setLineDash([]);
      plainLabel(ctx, `${lq.kind} ${pxs(lq.price)}`, W - 6, y + (lq.kind === "BSL" ? -9 : 9), "--c-liq", size, "right");
    }
  }
  if (a && a.ready && L.liquidity && !a.debug) {
    const drawnPx = new Set();
    for (const st of a.setups || []) if (!st.signal_id) drawPlan(ctx, st, size, drawnPx);
  }
  if (L.positions) for (const p of shownPositions()) drawPosition(ctx, p, size);
  // zone labels last: over candles and lines; live zones first so theirs keep their place
  for (const [text, x, y, c, align, , kind] of [...zoneLabels].sort((u, v) => v[5] - u[5])) {
    if (kind === "plain") plainLabel(ctx, text, x, y, c, size, align); else pill(ctx, text, x, y, c, size, align);
  }
}
/** TradingView-style long/short position object: risk box, reward box to the final target,
 * entry line, target, the time stop / order expiry, the result. */
function vMark(ctx, iso, text, c, size) {
  const x = iso ? xOf(secs(iso)) : null;
  if (x === null || x < 0 || x > size.width) return;
  ctx.strokeStyle = rgba(c, 0.8); ctx.lineWidth = 1; ctx.setLineDash([3, 4]);
  ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, size.height); ctx.stroke(); ctx.setLineDash([]);
  plainLabel(ctx, text, x + 4, EDGE + 8, c, size, "left");
}
function drawPosition(ctx, p, size) {
  const W = size.width;
  const open = isActive(p), filled = isFilled(p);
  const s = span(p.created_at, open ? null : p.closed_at, W);
  const fin = p.tp;
  const ye = yOf(p.entry), ys = yOf(p.sl), yt = fin === null ? null : yOf(fin);
  if (!s || ye === null || ys === null) return;
  const x1 = s[0], x2 = Math.max(s[1], x1 + 24);
  const focus = state.focus === p.id;
  const a = open || focus ? 1 : 0.3;  // history stays in the background
  if (yt !== null) {
    ctx.fillStyle = rgba("--c-bull", 0.12 * a); ctx.fillRect(x1, Math.min(ye, yt), x2 - x1, Math.abs(yt - ye));
    ctx.strokeStyle = rgba("--c-bull", 0.85 * a); ctx.lineWidth = focus ? 2 : 1.2; ctx.strokeRect(x1, Math.min(ye, yt), x2 - x1, Math.abs(yt - ye));
  }
  ctx.fillStyle = rgba("--c-bear", 0.14 * a); ctx.fillRect(x1, Math.min(ye, ys), x2 - x1, Math.abs(ys - ye));
  ctx.strokeStyle = rgba("--c-bear", 0.85 * a); ctx.lineWidth = focus ? 2 : 1.2; ctx.strokeRect(x1, Math.min(ye, ys), x2 - x1, Math.abs(ys - ye));
  ctx.strokeStyle = rgba("--c-text", 0.95 * a); ctx.setLineDash(p.state === "PENDING" ? [4, 3] : []); ctx.lineWidth = 1.6;
  ctx.beginPath(); ctx.moveTo(x1, ye); ctx.lineTo(x2, ye); ctx.stroke(); ctx.setLineDash([]);
  if (p.filled_at) {
    const xf = xOf(secs(p.filled_at));
    if (xf !== null) { ctx.fillStyle = rgba("--c-text", a); ctx.beginPath(); ctx.arc(xf, ye, 3.5, 0, Math.PI * 2); ctx.fill(); }
  }
  for (const part of p.parts || []) {  // every exit: a cross at its price and time
    const xx = xOf(secs(part.at)), yx = yOf(part.price);
    if (xx === null || yx === null) continue;
    ctx.strokeStyle = rgba("--c-text", a); ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(xx - 4, yx - 4); ctx.lineTo(xx + 4, yx + 4); ctx.moveTo(xx + 4, yx - 4); ctx.lineTo(xx - 4, yx + 4); ctx.stroke();
  }
  const long = p.side === "LONG";
  const money = open ? (filled ? ` · ${rText(p.open_r)} · ${usdSigned(p.open_pnl)} $` : ` · limit ${pxs(p.entry)}`)
    : ` · ${STATE[p.state] ? STATE[p.state][1] : p.state} ${rText(p.result_r)}${p.pnl_usdt !== null ? ` · ${usdSigned(p.pnl_usdt)} $` : ""}`;
  const yLab = long ? Math.min(yt === null ? ye : yt, ye) - 11 : Math.max(yt === null ? ye : yt, ye) + 11;
  pill(ctx, `${long ? "Long" : "Short"}${money}`, Math.max(x1, 0) + 2, yLab, long ? "--c-bull" : "--c-bear", size, "left");
  if (!open) return;
  const off = (y) => y < EDGE || y > size.height - EDGE;
  if (p.tp && yt !== null) {
    if (off(yt)) pill(ctx, `TP ${pxs(p.tp)} ${yt < EDGE ? "↑" : "↓"}`, x2 - 6, yt < EDGE ? EDGE : size.height - EDGE, "--c-bull", size, "right");
    else plainLabel(ctx, `TP ${pxs(p.tp)}`, x2 - 6, yt + (long ? 10 : -10), "--c-bull", size, "right");
  }
  const slText = `SL ${pxs(p.sl)}`;
  if (off(ys)) pill(ctx, `${slText} ${ys < EDGE ? "↑" : "↓"}`, x2 - 6, ys < EDGE ? EDGE : size.height - EDGE, "--c-bear", size, "right");
  else plainLabel(ctx, slText, x2 - 6, ys + (long ? -10 : 10), "--c-bear", size, "right");
  if (p.state === "PENDING") vMark(ctx, p.deadline, "order expires", "--c-pending", size);
  if (p.time_stop_at) vMark(ctx, p.time_stop_at, "time stop", "--c-armed", size);
}

function chartTheme() {
  return { layout: { background: { color: css("--surface") }, textColor: css("--text-2"), fontFamily: "IBM Plex Sans, sans-serif" },
    grid: { vertLines: { color: css("--border") }, horzLines: { color: css("--border") } },
    rightPriceScale: { borderColor: css("--border") },
    crosshair: { mode: 0 },
    localization: { timeFormatter: (t) => `${fmtDay.format(new Date(t * 1000))} ${fmtClock.format(new Date(t * 1000))}` },
    timeScale: { borderColor: css("--border"), timeVisible: true, secondsVisible: false, rightOffset: 12,
      tickMarkFormatter: (t, type) => (type < 3 ? fmtDay : fmtClock).format(new Date(t * 1000)) } };
}
function seriesColors() {
  return { upColor: css("--ok"), borderUpColor: css("--ok"), wickUpColor: css("--ok"),
    downColor: css("--bad"), borderDownColor: css("--bad"), wickDownColor: css("--bad") };
}
function ensureChart() {
  if (state.chart || !window.LightweightCharts) return;
  state.chart = LightweightCharts.createChart($("chart"), { autoSize: true, ...chartTheme() });
  state.series = state.chart.addCandlestickSeries(seriesColors());
  state.layer = new SmcLayer();
  state.series.attachPrimitive(state.layer);
  state.chart.subscribeCrosshairMove(showOhlc);
  const ts = state.chart.timeScale();
  ts.subscribeVisibleLogicalRangeChange(() => { $("go-live").hidden = !(ts.scrollPosition() < -3); });
  $("go-live").addEventListener("click", () => ts.scrollToRealTime());
}
function applyPriceFormat() {
  const m = state.markets[state.symbol];
  if (!state.series || !m) return;
  state.series.applyOptions({ priceFormat: { type: "price", precision: m.decimals, minMove: +m.tick } });
}
function restyle() {
  if (!state.chart) return;
  state.chart.applyOptions(chartTheme());
  state.series.applyOptions(seriesColors());
  hatchCache = null;
  state.layer.update();
}
const toBar = (c) => ({ time: secs(c.open_time), open: +c.open, high: +c.high, low: +c.low, close: +c.close });
function setCandles(d, keepView) {
  ensureChart();
  if (!state.series) return;
  applyPriceFormat();
  state.byTime = new Map(d.items.map((c) => [secs(c.open_time), c]));
  state.data = d.items.map(toBar);
  state.times = state.data.map((b) => b.time);
  const ts = state.chart.timeScale();
  const away = keepView && ts.scrollPosition() < -3;
  const r = away ? ts.getVisibleLogicalRange() : null;
  state.series.setData(state.data);
  if (r) ts.setVisibleLogicalRange(r);
  if (state.data.length) setPrice(state.data[state.data.length - 1].close, false);
  state.layer.update();
}
function bucketOf(t) { const s = TF_SEC[state.tf], o = TF_OFFSET[state.tf]; return Math.floor((t - o) / s) * s + o; }
/** Fold a live M1 candle into the selected timeframe's forming bar (display only). */
function applyMinute(m) {
  if (!state.series || !state.data.length) return;
  const b = bucketOf(secs(m.t));
  const last = state.data[state.data.length - 1];
  if (b < last.time) return;  // an older bucket: the next candle reload brings it
  let bar;
  if (b === last.time) bar = { time: b, open: last.open, high: Math.max(last.high, +m.h), low: Math.min(last.low, +m.l), close: +m.c };
  else { bar = { time: b, open: +m.o, high: +m.h, low: +m.l, close: +m.c }; state.data.push(bar); state.times.push(b); }
  state.data[state.data.length - 1] = bar;
  state.series.update(bar);
  state.byTime.set(b, { open_time: new Date(b * 1000).toISOString(), open: bar.open, high: bar.high, low: bar.low, close: bar.close, forming: true });
  setPrice(+m.c, true);
  state.layer.update();
  touchCheck(+m.h, +m.l);
}
/* When a live tick reaches a drawn zone or liquidity line, ask the backend for the new state
 * right away instead of waiting for the next poll. The backend decides what changed (touched,
 * filled, swept); this only decides when to ask. */
let touchTimer = null, lastTouchFetch = 0;
function touchCheck(h, l) {
  const a = state.analysis;
  if (!a || !a.ready || touchTimer) return;
  const reaches = (z) => {
    const long = z.direction === "LONG", top = +z.top, bot = +z.bottom;
    return (!z.tested && (long ? l <= top : h >= bot)) || (z.kind === "FVG" && (long ? l <= bot : h >= top));
  };
  const setupZones = (a.setups || []).flatMap((st) => [
    { kind: "FVG", direction: st.direction, top: st.fvg.top, bottom: st.fvg.bottom, tested: st.state !== "waiting" },
    { kind: "OB", direction: st.direction, top: st.ob.top, bottom: st.ob.bottom, tested: false }]);
  const zs = [...(a.zones || []), ...(a.htf_zones || []), ...setupZones];
  const hit = [...zs, ...zs.flatMap((z) => (z.gaps || []).filter((g) => g.status !== "FILLED"))].some(reaches)
    || (a.liquidity || []).some((lq) => (lq.kind === "BSL" ? h > +lq.price : l < +lq.price));
  if (!hit) return;
  const wait = Math.max(300, 1500 - (Date.now() - lastTouchFetch));
  touchTimer = setTimeout(() => {
    touchTimer = null; lastTouchFetch = Date.now();
    refreshAnalysis().then(() => { if (state.view === "chart") refreshSlow(); }).catch(() => {});
  }, wait);
}
function setPrice(p, live) {
  if (p === null || p === undefined) return;
  state.lastPrice = +p;
  $("chart-price").textContent = pxs(p);
  $("chart-price").title = live ? "latest trade (live)" : "last close";
}
function startLive() {
  if (!window.EventSource) return;
  if (state.live) { state.live.close(); state.live = null; }
  const sym = state.symbol;
  const es = new EventSource(`/api/live/stream?symbol=${encodeURIComponent(sym)}`);
  state.live = es;
  const mark = (up) => { state.streamUp = up; $("chart-live").hidden = !(up && state.feedUp); };
  es.addEventListener("open", () => mark(true));
  es.addEventListener("error", () => mark(false));
  es.addEventListener("snapshot", (e) => { if (sym !== state.symbol) return; const s = JSON.parse(e.data); for (const f of s.forming || []) applyMinute(f); if (s.price) setPrice(s.price, true); });
  es.addEventListener("message", (e) => {
    if (sym !== state.symbol) return;
    const ev = JSON.parse(e.data);
    if (ev.o === undefined || ev.status === "DATA_GAP") return;
    applyMinute(ev);
    if (ev.price) setPrice(ev.price, true);
  });
}
function showOhlc(param) {
  const box = $("chart-ohlc");
  const c = param && param.time !== undefined ? state.byTime.get(param.time) : null;
  if (!c) { box.textContent = ""; return; }
  box.textContent = `${fmtDay.format(new Date(c.open_time))} ${fmtClock.format(new Date(c.open_time))} · O ${pxs(c.open)}  H ${pxs(c.high)}  L ${pxs(c.low)}  C ${pxs(c.close)}${c.forming ? " · forming" : ""}`;
}
function renderLegend() {
  const items = [];
  const sw = (c, text) => el("li", {}, el("span", { class: "sw", style: `--c:${rgba(c, 0.35)};--b:${rgba(c, 0.95)}` }), text);
  const ln = (c, text) => el("li", {}, el("span", { class: "ln", style: `--c:${c}` }), text);
  const ps = state.params[state.symbol];
  const zt = ps ? (ps.groups.flatMap((g) => g.items).find((i) => i.key === "zone_tf") || {}).value : "1h";
  if (state.mode === "setups") {
    if (state.layers.ob) items.push(sw("--c-bull", `${zt} setup OB, waiting (long)`), sw("--c-bear", "waiting (short)"),
      sw("--c-armed", "armed: price in the FVG"), sw("--c-pending", "limit order resting"), sw("--c-open", "position open"));
    if (state.layers.ob) items.push(sw("--c-fvg-bull", "its FVG"), ln(rgba("--c-liq", 1), "liquidity sweep (dot = wick)"));
    if (state.layers.liquidity) items.push(ln(rgba("--c-bull", 1), "planned SL (OB wick) · TP (front-run) and the HH / LL it refers to · 50 % of the range, dotted"));
    if (state.layers.positions) items.push(ln(css("--text-2"), "positions: entry, SL, TP, time stop / expiry"));
    items.push(el("li", { class: "muted" }, "Setups: sweep → BOS/CHoCH → OB + FVG, fresh, with the bias, passing the filters, near the price or with a position. Label: timeframe · age · state · net R · cost share. Rejected setups: All zones (debug)"));
  } else {
    if (state.layers.ob) items.push(sw("--c-bull", "Bullish OB"), sw("--c-bear", "Bearish OB"));
    if (state.layers.fvg) items.push(sw("--c-fvg-bull", "Bullish FVG"), sw("--c-fvg-bear", "Bearish FVG"));
    if (state.layers.htf) items.push(ln(css("--text-2"), "Higher-TF zone (hatched, label on the right)"));
    if (state.layers.structure) items.push(ln(css("--text-2"), "BOS / CHoCH"));
    if (state.layers.liquidity) items.push(ln(rgba("--c-liq", 1), "BSL / SSL liquidity"));
    items.push(el("li", { class: "muted" }, "Debug: every valid zone, every gap behind an order block (dotted = filled), every setup in any state · Fresh = untouched"));
  }
  $("chart-legend").replaceChildren(...items);
}
function renderControls() {
  $("tf-seg").replaceChildren(...TFS.map((tf) => {
    const role = state.radar && state.radar.timeframes ? (state.radar.timeframes.find((x) => x.tf === tf) || {}).role : "";
    return el("button", { type: "button", role: "tab", "aria-selected": String(tf === state.tf), title: role ? `${tf}: ${role}` : tf,
      onclick: () => { if (state.tf !== tf) { state.tf = tf; savePrefs(); renderControls(); loadChart(false); } } },
    tf, role ? el("span", { class: "r" }, role.split(" ")[0][0]) : null);
  }));
  const modeSeg = el("div", { class: "seg seg-mini", role: "tablist", "aria-label": "Chart detail" },
    [["setups", "Setups"], ["debug", "All zones (debug)"]].map(([k, l]) => el("button", { type: "button", role: "tab", "aria-selected": String(state.mode === k), "data-mode": k,
      title: k === "setups" ? "Only the setups the engine can trade, and the positions" : "Every zone, gap, break, liquidity level and setup", onclick: () => { state.mode = k; savePrefs(); renderControls(); renderLegend(); if (state.layer) state.layer.update(); } }, l)));
  $("layer-chips").replaceChildren(modeSeg, ...LAYERS.map(([k, name, c]) => el("button", { type: "button", class: "chip", "aria-pressed": String(!!state.layers[k]),
    onclick: () => { state.layers[k] = !state.layers[k]; savePrefs(); renderControls(); renderLegend(); if (state.layer) state.layer.update(); } },
  el("span", { class: "sw", style: `--c:${rgba(c, 0.9)}` }), name)));
  $("chart-symbol").textContent = `${symName(state.symbol)} · ${state.tf}`;
}
function renderSymbols() {
  $("sym-seg").replaceChildren(...state.symbols.map((s) => {
    const m = state.markets[s];
    const busy = state.allSignals.some((x) => x.symbol === s && isActive(x));
    return el("button", { type: "button", role: "tab", "aria-selected": String(s === state.symbol), onclick: () => selectSymbol(s) },
      el("span", {}, m ? m.display : s, busy ? el("span", { class: "pos-dot", title: "open position" }) : null),
      el("span", { class: "p" }, m ? pxs(m.price, s) : "—"));
  }));
}
function selectSymbol(s) {
  if (s === state.symbol) return;
  state.symbol = s; state.radar = null; state.analysis = null; state.focus = null;
  savePrefs(); renderSymbols(); renderControls();
  loadParams(s).then(renderLegend).catch(() => {});
  startLive();
  refreshView().catch(() => {});
}
async function loadChart(keepView) {
  const tf = state.tf, sym = state.symbol;
  $("chart-note").hidden = true;
  try {
    const [c, a] = await Promise.all([
      api(q(`/api/market/candles?tf=${tf}&limit=${TF_BARS[tf]}`)),
      api(q(`/api/smc/analysis?tf=${tf}&bars=${TF_BARS[tf]}`)),
    ]);
    if (tf !== state.tf || sym !== state.symbol) return;
    state.analysis = a;
    state.signals = state.allSignals.filter((x) => x.symbol === sym);
    setCandles(c, keepView);
    if (!c.items.length) { $("chart-note").textContent = "No market history yet — the engine loads it from Tabdeal on start."; $("chart-note").hidden = false; }
  } catch (e) {
    $("chart-note").textContent = "Chart data unavailable — retrying."; $("chart-note").hidden = false;
  }
}
async function refreshAnalysis() {
  const tf = state.tf, sym = state.symbol;
  const a = await api(q(`/api/smc/analysis?tf=${tf}&bars=${TF_BARS[tf]}`));
  if (tf !== state.tf || sym !== state.symbol) return;
  state.analysis = a;
  state.signals = state.allSignals.filter((x) => x.symbol === sym);
  if (state.layer) state.layer.update();
}

/* ---- fullscreen ------------------------------------------------------------------------------- */
function isFull() { return document.fullscreenElement === $("chart-card") || $("chart-card").classList.contains("maximized"); }
function renderFullBtn() {
  const on = isFull();
  $("full-btn").replaceChildren(icon(on ? "shrink" : "expand"));
  $("full-btn").setAttribute("aria-pressed", String(on));
  $("full-btn").setAttribute("aria-label", on ? "Leave full screen" : "Full screen chart");
}
function toggleFull() {
  const card = $("chart-card");
  if (isFull()) {
    card.classList.remove("maximized"); document.body.classList.remove("has-max");
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    renderFullBtn();
    return;
  }
  // the page-filling mode applies at once; true full screen is added where the browser allows it
  card.classList.add("maximized"); document.body.classList.add("has-max");
  renderFullBtn();
  if (card.requestFullscreen) card.requestFullscreen().catch(() => {});
}
function onFullscreenChange() {
  if (document.fullscreenElement) state.realFull = true;
  else if (state.realFull) {  // left with Esc / the browser's own control: leave both modes
    state.realFull = false;
    $("chart-card").classList.remove("maximized"); document.body.classList.remove("has-max");
  }
  renderFullBtn();
}

/* ---- side panels ---------------------------------------------------------------------------- */
function renderKpis(w, r) {
  const k = (tone, label, value, detail, ic) => el("div", { class: `kpi t-${tone}` }, el("div", { class: "k" }, label),
    el("div", { class: "v" }, ic ? icon(ic) : null, value), el("div", { class: "d" }, detail || " "));
  const m = state.markets[state.symbol] || {};
  const bias = (r && r.bias) || "UNDEFINED";
  const [bt, bi, bl] = TREND[bias];
  const smc = m.smc || {}, run = smc.runner, hist = smc.history || {};
  const nOpen = state.allSignals.filter(isActive).length;
  const maxPos = state.params[state.symbol] ? (state.params[state.symbol].groups.flatMap((g) => g.items).find((i) => i.key === "max_positions") || {}).value : 2;
  const ret = w && w.ready ? w.return_pct : null;
  $("kpis").replaceChildren(
    w && w.ready ? k(+w.equity >= +w.initial ? "ok" : "bad", "Wallet equity · USDT", usd(w.equity), `balance ${usd(w.balance)} · ${ret >= 0 ? "+" : ""}${pct(ret, 2)}`, "wallet")
      : k("neutral", "Wallet", "—", "starts with the engine", "wallet"),
    k(nOpen ? "warn" : "neutral", "Positions", `${nOpen} / ${maxPos ?? 2}`, w && w.ready ? `margin ${usd(w.margin_used)} · free ${usd(w.free)}` : "", nOpen ? "target" : "flat"),
    k(w && +w.unrealized > 0 ? "ok" : w && +w.unrealized < 0 ? "bad" : "neutral", "Open PnL · USDT", w && w.ready ? usdSigned(w.unrealized) : "—", w && w.ready ? `realized ${usdSigned(w.realized)} · fees ${usd(w.fees)}` : ""),
    k(bt, `${symName(state.symbol)} bias · ${(r && r.timeframes && r.timeframes.find((x) => x.role.startsWith("Bias")) || {}).tf || ""}`, bl, "higher-timeframe structure", bi),
    k(run && run.status === "RUNNING" ? "ok" : "bad", "Engine", run ? (run.status === "RUNNING" ? "Running" : "Stale") : "Not started", run ? `heartbeat ${ago(run.heartbeat_age_s)}` : "start: python -m sp2l smc", run && run.status === "RUNNING" ? "check" : "alert"),
    k(hist.days >= hist.target_days - 1 ? "ok" : "warn", `History · ${symName(state.symbol)}`, hist.days !== null && hist.days !== undefined ? `${hist.days} d` : "—", `target ${hist.target_days ?? "—"} d · Tabdeal chart`),
  );
}
function posCard(p) {
  const long = p.side === "LONG";
  const live = isFilled(p);
  return el("button", { type: "button", class: `pos-card t-${long ? "ok" : "bad"}`, title: "Show on chart",
    onclick: () => { if (p.symbol !== state.symbol) selectSymbol(p.symbol); state.focus = p.id; if (state.layer) state.layer.update(); } },
  el("div", { class: "top" }, el("span", {}, el("span", { class: "sym" }, symName(p.symbol)), " ", sideTag(p.side), " ", stateTag(p.state)),
    el("span", { class: `pnl ${toneOf(p.open_pnl)}` }, live ? `${usdSigned(p.open_pnl)} $` : "—")),
  el("div", { class: "levels" },
    el("div", { class: "t-info" }, el("div", { class: "k" }, "Entry"), el("div", { class: "v num" }, pxs(p.entry, p.symbol))),
    el("div", { class: "t-bad" }, el("div", { class: "k" }, "Stop loss"), el("div", { class: "v num" }, pxs(p.sl, p.symbol))),
    el("div", { class: "t-ok" }, el("div", { class: "k" }, "Take profit"), el("div", { class: "v num" }, pxs(p.tp, p.symbol)))),
  el("div", { class: "meta" }, el("span", {}, live ? rText(p.open_r) : `limit order at ${pxs(p.entry, p.symbol)}`), el("span", {}, `net ${p.net_rr ? (+p.net_rr).toFixed(2) : "—"}R at TP`),
    el("span", { class: "num" }, `qty ${grp(String(+p.qty))}`), el("span", { class: "num" }, `${usd(p.notional)} $ · margin ${usd(p.margin)} $`),
    el("span", {}, live ? "opened " : "placed ", when(p.filled_at || p.created_at)),
    p.time_stop_at ? el("span", { title: "Closed at market if neither the target nor the stop is hit by then" }, "time stop ", when(p.time_stop_at)) : null,
    p.deadline ? el("span", { title: live ? "The rest is closed at market by then" : "The limit order is cancelled if not filled by then" },
      live ? "time limit " : "expires ", when(p.deadline)) : null));
}
function renderTicket(w) {
  const act = state.allSignals.filter(isActive);
  const body = $("ticket-body");
  const mini = w && w.ready ? el("div", { class: "wallet-mini" },
    el("div", {}, el("div", { class: "k" }, "Balance"), el("div", { class: "v" }, `${usd(w.balance)}`)),
    el("div", {}, el("div", { class: "k" }, "Equity"), el("div", { class: `v ${toneOf(+w.equity - +w.initial)}` }, `${usd(w.equity)}`)),
    el("div", {}, el("div", { class: "k" }, "Free"), el("div", { class: "v" }, `${usd(w.free)}`))) : null;
  $("ticket-state").replaceChildren(act.length ? tag("warn", `${act.length} open`, "target") : tag("neutral", "Flat", "clock"));
  if (!act.length) {
    const last = state.allSignals.find((s) => s.closed_at);
    body.replaceChildren(mini || "", el("div", { class: "ticket-empty" },
      el("p", { class: "muted small" }, `One shared ${w && w.ready ? usd(w.initial, 0) : "100"} USDT wallet for ${state.symbols.map(symName).join(" and ")}. A limit order rests at the order block of a 1h setup (break of structure → order block with its FVG) with the 4h bias, from the moment price first trades into the FVG; the stop sits beyond the OB's wick, the target at the previous HH / LL.`),
      last ? el("p", { class: "small" }, "Last: ", symName(last.symbol), " ", sideTag(last.side), " ", stateTag(last.state), " ", rEl(last.result_r), last.pnl_usdt !== null ? ` · ${usdSigned(last.pnl_usdt)} $` : "") : null));
    return;
  }
  body.replaceChildren(mini || "", el("div", { class: "pos-list" }, act.map(posCard)));
}
function renderRadar(r) {
  if (!r || !r.ready) { $("ladder").replaceChildren(el("li", { class: "t-neutral" }, el("span", {}, "No data yet"))); return; }
  const [bt, bic, bl] = TREND[r.bias];
  $("ladder-bias").replaceChildren(tag(bt, `${symName(state.symbol)} · ${bl.toLowerCase()}`, bic));
  $("ladder").replaceChildren(...[...r.timeframes].reverse().map((t) => {
    const [tone, ic, lab] = TREND[t.trend];
    const ev = t.last_event;
    return el("li", { class: `t-${tone}` }, el("span", { class: "tf" }, t.tf),
      el("div", {}, el("div", { class: "role" }, t.role || "Context"),
        el("div", { class: "ev" }, ev ? `${ev.kind === "CHOCH" ? "CHoCH" : "BOS"} ${ev.direction === "LONG" ? "up" : "down"} @ ${pxs(ev.level)} · ` : "no break yet", ev ? when(ev.time) : null)),
      el("span", { class: "tr" }, icon(ic), lab));
  }));
  const stTone = { waiting: "info", armed: "warn", pending: "info", open: "violet", used: "neutral", invalid: "neutral", expired: "neutral" };
  $("poi-list").replaceChildren(...(r.setups.length ? r.setups.map((z) => el("li", { class: `t-${z.direction === "LONG" ? "ok" : "bad"}`, title: "Show on chart",
    onclick: () => { state.tf = z.tf; savePrefs(); renderControls(); loadChart(false); } },
  el("div", {}, el("div", { class: "z" }, `${z.tf} OB · ${ageText(z.age_min)} · `, tag(stTone[z.state] || "neutral", z.state)),
    el("div", { class: "px num" }, `${pxs(z.ob.bottom)} – ${pxs(z.ob.top)}`),
    z.plan ? el("div", { class: "px small" }, z.plan.accepted ? `TP ${(+z.plan.tp.net_r).toFixed(2)}R` : z.plan.reasons.map((x) => x.code).join(", ")) : null),
  el("div", { class: "dist" }, z.distance_atr !== null ? `${(+z.distance_atr).toFixed(1)} ATR` : "—", el("div", { class: "px" }, "away")))) : [el("li", { class: "t-neutral" }, el("span", { class: "muted small" }, r.bias === "UNDEFINED" ? "No bias, so no setup is tradable." : "No fresh setup with the bias near the price."))]));
  $("trig-list").replaceChildren(...(r.recent.length ? r.recent.map((t) => el("li", { class: `t-${stTone[t.state] || "neutral"}` },
    el("div", { class: "top" }, sideTag(t.direction), el("b", {}, `${t.tf} ${t.event.kind === "CHOCH" ? "CHoCH" : "BOS"}`), when(t.confirmed_at), tag(stTone[t.state] || "neutral", t.state),
      t.aligned ? null : el("span", { class: "muted" }, "against the bias")),
    el("div", { class: "why" }, `${t.plan && !t.plan.accepted ? `${t.plan.reasons.map((r) => r.code).join(", ")} · ` : ""}${t.sweep ? `sweep ${pxs(t.sweep.level)} (wick ${pxs(t.sweep.wick)}) · ` : ""}OB ${pxs(t.ob.bottom)} – ${pxs(t.ob.top)} · FVG ${pxs(t.fvg.bottom)} – ${pxs(t.fvg.top)}`))) : [el("li", { class: "t-neutral" }, el("span", { class: "muted small" }, "No complete setup in the lookback."))]));
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
  state.focus = s.id;
  if (s.symbol !== state.symbol) { state.symbol = s.symbol; savePrefs(); renderSymbols(); startLive(); }
  location.hash = "#/chart";
  setTimeout(() => {
    if (!state.chart) return;
    const t = secs(s.created_at);
    const i = state.times.findIndex((x) => x >= t);
    if (i >= 0) state.chart.timeScale().setVisibleLogicalRange({ from: i - 60, to: i + 60 });
    state.layer.update();
  }, 600);
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
      const at = v.entry_ref === "fvg_mid" ? "50 % of the FVG after the order block" : "the order-block edge touching the FVG";
      if (!v.confirm_exec) return `Limit order (maker) at ${at}, resting from the setup; armed when price first trades into the FVG, cancelled ${v.pending_expiry_min / 60} h later. Only a fresh order block.`;
      const win = (+v.confirm_window_min || +v.pending_expiry_min) / 60;
      return v.confirm_entry === "limit"
        ? `When price first trades into the FVG, wait up to ${win} h for a ${v.exec_tf} BOS / CHoCH with the setup; then a limit order (maker) at ${at}, cancelled ${v.pending_expiry_min / 60} h later. Only a fresh order block.`
        : `When price first trades into the FVG, wait up to ${win} h for a ${v.exec_tf} BOS / CHoCH with the setup and enter at market at its close.`;
    })()),
    step("ok", "Exit", `${v.exec_tf} · M1`, `${v.sl_ref === "ob_height" ? "Stop beyond the order block by its own height (long: OB low − OB height)." : "Stop one tick beyond the order block's wick."} ${+v.tp_rr > 0 ? `One target at ${v.tp_rr} × the stop distance (price R:R 1:${v.tp_rr}).` : `One target, from the market, never from the stop: the ${v.tp_ref === "swing" ? "last confirmed" : "previous"} ${v.zone_tf} HH (long: its high) / LL (short: its low). None beyond the entry: no trade. The TP sits ${v.tp_front_run_atr} ATR before that level.`} Filters (they reject, never move stop or target): ${v.require_discount ? "long entry in the lower half of sweep wick → HH (short: upper half), " : ""}net R at the TP ≥ ${v.min_net_rr}${+v.max_cost_frac > 0 ? `, costs ≤ ${pct(v.max_cost_frac, 0)} of the stop` : ""}${v.exit_on_choch ? `; exit at a ${v.zone_tf} CHoCH against the trade` : ""}.${+v.time_stop_min > 0 ? ` Time stop ${v.time_stop_min / 60} h,` : ""} Closed at market after ${v.max_hold_min / 60} h. Size: ${pct(v.risk_pct)} of the shared wallet, ≤ ${v.max_leverage}x.`));
  $("param-grid").replaceChildren(...p.groups.map((g) => el("section", { class: "card" }, el("h2", {}, g.name),
    el("dl", { class: "kv" }, g.items.flatMap((i) => [el("dt", {}, el("code", {}, i.key)), el("dd", { class: "num" }, Array.isArray(i.value) ? i.value.join(", ") || "—" : show(i.value))])))),
  el("section", { class: "card" }, el("h2", {}, "Costs (from config)"), el("dl", { class: "kv" },
    ...Object.entries(p.costs).flatMap(([k, x]) => [el("dt", {}, el("code", {}, k)), el("dd", { class: "num" }, pct(x, 3))]))));
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
  if (state.lastPrice === null && state.markets[state.symbol].price) setPrice(state.markets[state.symbol].price, false);
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
  state.feedUp = feedUp;
  $("chart-live").hidden = !(state.streamUp && state.feedUp);
  $("foot-ver").textContent = `SMC · ${o.symbols.map(symName).join(" + ")} · shared wallet`;
}
function route() {
  const v = (location.hash.replace("#/", "") || "chart").split("?")[0];
  state.view = ["chart", "signals", "performance", "strategy", "system"].includes(v) ? v : "chart";
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
    if (state.view === "chart") {
      await refreshAnalysis();
      renderKpis(o.wallet, state.radar);
      renderTicket(o.wallet);
    }
  } catch { /* transient; next tick retries */ }
}
async function refreshSlow() {
  try {
    if (state.view === "chart") {
      state.radar = await api(q("/api/smc/radar"));
      renderRadar(state.radar);
      renderControls();
    }
  } catch { /* transient */ }
}
async function refreshView() {
  if (!state.overview) await refreshFast();
  if (state.view === "chart") { await loadChart(true); await refreshSlow(); await refreshFast(); }
  else if (state.view === "signals") await renderSignals();
  else if (state.view === "performance") { await Promise.all(state.symbols.map(loadParams)).catch(() => {}); await renderPerformance(); }
  else if (state.view === "strategy") await renderStrategy();
  else if (state.view === "system") await renderSystem();
}
function setTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem("smc-theme", t); } catch { /* storage unavailable */ }
  $("theme-btn").replaceChildren(icon(t === "dark" ? "sun" : "moon"));
  restyle();
  renderLegend();
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
  renderFullBtn();
  $("full-btn").addEventListener("click", toggleFull);
  document.addEventListener("fullscreenchange", onFullscreenChange);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && $("chart-card").classList.contains("maximized")) toggleFull(); });
  $("tz-label").textContent = `Times in ${tzText()}`;
  try { await refreshFast(); } catch { /* retried below */ }
  if (state.symbol) loadParams(state.symbol).then(renderLegend).catch(() => {});
  renderControls();
  renderLegend();
  window.addEventListener("hashchange", route);
  route();
  startLive();
  setInterval(refreshFast, 5000);
  setInterval(refreshSlow, 20000);
  setInterval(() => { if (state.view === "chart") loadChart(true); }, 60000);
}
init();
