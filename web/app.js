/* SMC Console — render-only client.
 * Every zone, structure break, signal, state, reason and number shown here comes from the
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
const px2 = (v) => (v === null || v === undefined ? "—" : grp((+v).toFixed(2)));
function rText(v) { if (v === null || v === undefined) return "—"; const n = +v; return `${n > 0 ? "+" : ""}${n.toFixed(2)}R`; }
function rEl(v) { return el("span", { class: `num ${v === null || v === undefined ? "" : +v > 0 ? "pos" : +v < 0 ? "neg" : ""}` }, rText(v)); }
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
const TREND = { BULLISH: ["ok", "up", "Bullish"], BEARISH: ["bad", "down", "Bearish"], UNDEFINED: ["neutral", "flat", "Undefined"] };
const STATE = { PENDING: ["info", "Pending entry"], OPEN: ["warn", "Open"], TP: ["ok", "Target hit"], SL: ["bad", "Stopped out"],
  EXPIRED: ["neutral", "Expired"], MISSED: ["neutral", "Missed"], TIMEOUT: ["neutral", "Timed out"], CANCELLED: ["neutral", "Cancelled"] };
const stateTag = (s) => { const [t, l] = STATE[s] || ["neutral", s]; return tag(t, l); };
function table(target, headers, data, rowFn, emptyText) {
  const t = typeof target === "string" ? $(target) : target;
  t.replaceChildren(el("thead", {}, el("tr", {}, headers.map((h) => el("th", { scope: "col" }, h)))));
  const body = el("tbody");
  if (!data || !data.length) body.append(el("tr", {}, el("td", { colspan: headers.length, class: "empty" }, emptyText || "Nothing yet")));
  else for (const r of data) body.append(rowFn(r));
  t.append(body);
}
function kv(target, pairs) {
  $(target).replaceChildren(...pairs.flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, v instanceof Node ? v : show(v))]));
}
async function api(path) {
  const r = await fetch(path, { headers: { Accept: "application/json" } });
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
}

/* ---- state ---------------------------------------------------------------------------------- */
const TFS = ["1m", "5m", "15m", "1h", "4h"];
const TF_SEC = { "1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400 };
const TF_OFFSET = { "1m": 0, "5m": 0, "15m": 0, "1h": 1800, "4h": 1800 };  // Tabdeal's chart grid
const TF_BARS = { "1m": 600, "5m": 500, "15m": 400, "1h": 300, "4h": 180 };
const LAYERS = [
  ["ob", "Order blocks", "--c-bull"], ["fvg", "Fair value gaps", "--c-fvg-bull"], ["structure", "BOS / CHoCH", "--c-text"],
  ["liquidity", "Liquidity", "--c-liq"], ["pd", "Premium / discount", "--c-pd-hi"], ["htf", "Higher-TF zones", "--c-fvg-bear"],
  ["positions", "Positions", "--c-bull"], ["mitigated", "Mitigated", "--c-text"],
];
const prefs = loadPrefs();
const state = { view: "chart", tf: prefs.tf || "15m", layers: prefs.layers || { ob: true, fvg: true, structure: true, liquidity: true, pd: false, htf: true, positions: true, mitigated: false },
  chart: null, series: null, layer: null, data: [], times: [], byTime: new Map(), analysis: null, signals: [], focus: null,
  overview: null, radar: null, live: null, lastPrice: null, sigFilter: "all", selectedSig: null, known: null, alerts: !!prefs.alerts };
function loadPrefs() { try { return JSON.parse(localStorage.getItem("smc-prefs") || "{}"); } catch { return {}; } }
function savePrefs() { try { localStorage.setItem("smc-prefs", JSON.stringify({ tf: state.tf, layers: state.layers, alerts: state.alerts })); } catch { /* storage unavailable */ } }

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
  constructor() { this.views = [new LayerView(this, "bottom"), new LayerView(this, "top")]; }
  attached(p) { this.series = p.series; this.request = p.requestUpdate; }
  detached() { this.request = null; }
  paneViews() { return this.views; }
  updateAllViews() {}
  update() { if (this.request) this.request(); }
}
class LayerView {
  constructor(src, z) { this.src = src; this.z = z; }
  zOrder() { return this.z; }
  renderer() { return { draw: (target) => target.useMediaCoordinateSpace(({ context, mediaSize }) => (this.z === "bottom" ? drawBelow : drawAbove)(context, mediaSize)) }; }
}
const yOf = (p) => state.series.priceToCoordinate(+p);
function span(from, to, width) {
  const x1 = from ? xOf(secs(from)) : 0;
  const x2 = to ? xOf(secs(to)) : width;
  if (x1 === null || x2 === null) return null;
  return [Math.max(-10, x1), Math.min(width + 10, x2)];
}
function label(ctx, text, x, y, color, align, bg) {
  ctx.font = "500 11px 'IBM Plex Sans', sans-serif";
  ctx.textAlign = align || "left";
  ctx.textBaseline = "middle";
  if (bg) {
    const w = ctx.measureText(text).width + 8;
    const bx = align === "right" ? x - w : align === "center" ? x - w / 2 : x;
    ctx.fillStyle = bg; ctx.beginPath(); ctx.roundRect(bx, y - 8, w, 16, 3); ctx.fill();
    ctx.fillStyle = color; ctx.fillText(text, align === "right" ? x - 4 : align === "center" ? x : x + 4, y);
    return;
  }
  ctx.fillStyle = color; ctx.fillText(text, x, y);
}
function zoneColor(z) {
  if (z.kind === "OB") return z.direction === "LONG" ? "--c-bull" : "--c-bear";
  return z.direction === "LONG" ? "--c-fvg-bull" : "--c-fvg-bear";
}
function drawZone(ctx, z, W, htf) {
  const s = span(z.from, z.to, W);
  const y1 = yOf(z.top), y2 = yOf(z.bottom);
  if (!s || y1 === null || y2 === null) return;
  const dead = z.status === "MITIGATED" || z.status === "EXPIRED";
  const c = zoneColor(z);
  ctx.fillStyle = rgba(c, dead ? 0.05 : htf ? 0.10 : z.kind === "OB" ? 0.16 : 0.13);
  ctx.fillRect(s[0], y1, s[1] - s[0], y2 - y1);
  ctx.strokeStyle = rgba(c, dead ? 0.25 : htf ? 0.9 : 0.55);
  ctx.lineWidth = htf ? 1.5 : 1;
  ctx.setLineDash(z.kind === "FVG" || dead ? [4, 3] : []);
  ctx.strokeRect(s[0], y1, s[1] - s[0], y2 - y1);
  ctx.setLineDash([]);
  if (s[1] - s[0] > 34) label(ctx, `${htf ? `${z.tf} ` : ""}${z.kind}${z.tested ? "" : " ●"}`, s[0] + 2, y1 + 8, rgba(c, dead ? 0.5 : 1));
}
function drawBelow(ctx, size) {
  const a = state.analysis;
  if (!a || !a.ready || !state.series) return;
  const W = size.width, L = state.layers;
  if (L.pd && a.range) {
    const s = span(a.range.from, null, W), yh = yOf(a.range.high), ye = yOf(a.range.eq), yl = yOf(a.range.low);
    if (s && yh !== null && yl !== null) {
      ctx.fillStyle = rgba("--c-pd-hi", 0.05); ctx.fillRect(s[0], yh, s[1] - s[0], ye - yh);
      ctx.fillStyle = rgba("--c-pd-lo", 0.05); ctx.fillRect(s[0], ye, s[1] - s[0], yl - ye);
      ctx.strokeStyle = rgba("--c-text", 0.35); ctx.setLineDash([2, 4]);
      ctx.beginPath(); ctx.moveTo(s[0], ye); ctx.lineTo(s[1], ye); ctx.stroke(); ctx.setLineDash([]);
      label(ctx, "Premium", W - 60, yh + 10, rgba("--c-pd-hi", 0.8), "right");
      label(ctx, "Equilibrium", W - 60, ye - 8, rgba("--c-text", 0.6), "right");
      label(ctx, "Discount", W - 60, yl - 10, rgba("--c-pd-lo", 0.8), "right");
    }
  }
  if (L.htf) for (const z of a.htf_zones || []) if ((z.kind === "OB" && L.ob) || (z.kind === "FVG" && L.fvg)) drawZone(ctx, z, W, true);
  for (const z of a.zones || []) {
    if ((z.kind === "OB" && !L.ob) || (z.kind === "FVG" && !L.fvg)) continue;
    if ((z.status === "MITIGATED" || z.status === "EXPIRED") && !L.mitigated) continue;
    drawZone(ctx, z, W, false);
  }
}
function drawAbove(ctx, size) {
  const a = state.analysis;
  if (!state.series) return;
  const W = size.width, L = state.layers;
  if (a && a.ready && L.structure) {
    for (const e of a.events || []) {
      const s = span(e.from, e.to, W), y = yOf(e.level);
      if (!s || y === null) continue;
      const c = e.direction === "LONG" ? "--c-bull" : "--c-bear";
      ctx.strokeStyle = rgba(c, 0.85); ctx.lineWidth = 1; ctx.setLineDash(e.kind === "CHOCH" ? [5, 3] : [2, 2]);
      ctx.beginPath(); ctx.moveTo(s[0], y); ctx.lineTo(s[1], y); ctx.stroke(); ctx.setLineDash([]);
      label(ctx, e.kind === "CHOCH" ? "CHoCH" : "BOS", (s[0] + s[1]) / 2, y + (e.direction === "LONG" ? -8 : 8), rgba(c, 1), "center");
    }
  }
  if (a && a.ready && L.liquidity) {
    for (const q of a.liquidity || []) {
      const s = span(q.from, null, W), y = yOf(q.price);
      if (!s || y === null) continue;
      ctx.strokeStyle = rgba("--c-liq", 0.8); ctx.setLineDash([1, 3]); ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.moveTo(s[0], y); ctx.lineTo(s[1], y); ctx.stroke(); ctx.setLineDash([]);
      label(ctx, `${q.kind} ${px2(q.price)}`, s[0] + 4, y + (q.kind === "BSL" ? -8 : 8), rgba("--c-liq", 1));
    }
  }
  if (L.positions) for (const p of state.signals) drawPosition(ctx, p, W);
}
/** TradingView-style long/short position object: risk box, reward box, entry line, result. */
function drawPosition(ctx, p, W) {
  const open = p.state === "PENDING" || p.state === "OPEN";
  const s = span(p.created_at, open ? null : p.closed_at, W);
  const ye = yOf(p.entry), ys = yOf(p.sl), yt = yOf(p.tp);
  if (!s || ye === null || ys === null || yt === null) return;
  const x1 = s[0], x2 = Math.max(s[1], x1 + 24);
  const focus = state.focus === p.id;
  const a = open || focus ? 1 : 0.55;
  ctx.fillStyle = rgba("--c-bull", 0.17 * a); ctx.fillRect(x1, Math.min(ye, yt), x2 - x1, Math.abs(yt - ye));
  ctx.fillStyle = rgba("--c-bear", 0.17 * a); ctx.fillRect(x1, Math.min(ye, ys), x2 - x1, Math.abs(ys - ye));
  ctx.lineWidth = focus ? 2 : 1;
  ctx.strokeStyle = rgba("--c-bull", 0.7 * a); ctx.strokeRect(x1, Math.min(ye, yt), x2 - x1, Math.abs(yt - ye));
  ctx.strokeStyle = rgba("--c-bear", 0.7 * a); ctx.strokeRect(x1, Math.min(ye, ys), x2 - x1, Math.abs(ys - ye));
  ctx.strokeStyle = rgba("--c-text", 0.9 * a); ctx.setLineDash(p.state === "PENDING" ? [4, 3] : []); ctx.lineWidth = 1.5;
  ctx.beginPath(); ctx.moveTo(x1, ye); ctx.lineTo(x2, ye); ctx.stroke(); ctx.setLineDash([]);
  if (p.filled_at) {
    const xf = xOf(secs(p.filled_at));
    if (xf !== null) { ctx.fillStyle = rgba("--c-text", a); ctx.beginPath(); ctx.arc(xf, ye, 3.5, 0, Math.PI * 2); ctx.fill(); }
  }
  if (p.exit_price && p.closed_at) {
    const xx = xOf(secs(p.closed_at)), yx = yOf(p.exit_price);
    if (xx !== null && yx !== null) { ctx.strokeStyle = rgba("--c-text", a); ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(xx - 4, yx - 4); ctx.lineTo(xx + 4, yx + 4); ctx.moveTo(xx + 4, yx - 4); ctx.lineTo(xx - 4, yx + 4); ctx.stroke(); }
  }
  const long = p.side === "LONG";
  const head = `${long ? "Long" : "Short"} · ${p.net_rr ? `${(+p.net_rr).toFixed(2)}R` : ""}`;
  const tail = open ? (p.state === "OPEN" ? ` · ${rText(p.open_r)}` : " · pending") : ` · ${STATE[p.state] ? STATE[p.state][1] : p.state} ${p.result_r !== null ? rText(p.result_r) : ""}`;
  const yLab = long ? Math.min(yt, ye) - 10 : Math.max(yt, ye) + 10;
  label(ctx, head + tail, x1, yLab, css("--surface"), "left", rgba(long ? "--c-bull" : "--c-bear", 0.95 * a));
  if (open) {
    label(ctx, `TP ${px2(p.tp)}`, x2 - 2, yt + (long ? 9 : -9), rgba("--c-bull", 1), "right");
    label(ctx, `SL ${px2(p.sl)}`, x2 - 2, ys + (long ? -9 : 9), rgba("--c-bear", 1), "right");
  }
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
function ensureChart() {
  if (state.chart || !window.LightweightCharts) return;
  state.chart = LightweightCharts.createChart($("chart"), { autoSize: true, ...chartTheme() });
  state.series = state.chart.addCandlestickSeries({
    upColor: css("--ok"), borderUpColor: css("--ok"), wickUpColor: css("--ok"),
    downColor: css("--bad"), borderDownColor: css("--bad"), wickDownColor: css("--bad"),
    priceFormat: { type: "price", precision: 2, minMove: 0.01 },
  });
  state.layer = new SmcLayer();
  state.series.attachPrimitive(state.layer);
  state.chart.subscribeCrosshairMove(showOhlc);
  const ts = state.chart.timeScale();
  ts.subscribeVisibleLogicalRangeChange(() => { $("go-live").hidden = !(ts.scrollPosition() < -3); });
  $("go-live").addEventListener("click", () => ts.scrollToRealTime());
}
function restyle() {
  if (!state.chart) return;
  state.chart.applyOptions(chartTheme());
  state.series.applyOptions({ upColor: css("--ok"), borderUpColor: css("--ok"), wickUpColor: css("--ok"),
    downColor: css("--bad"), borderDownColor: css("--bad"), wickDownColor: css("--bad") });
  state.layer.update();
}
const toBar = (c) => ({ time: secs(c.open_time), open: +c.open, high: +c.high, low: +c.low, close: +c.close });
function setCandles(d, keepView) {
  ensureChart();
  if (!state.series) return;
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
}
function setPrice(p, live) {
  if (p === null || p === undefined) return;
  state.lastPrice = +p;
  $("chart-price").textContent = px2(p);
  $("chart-price").title = live ? "latest trade (live)" : "last close";
}
function startLive() {
  if (!window.EventSource || state.live) return;
  const es = new EventSource("/api/live/stream");
  state.live = es;
  const mark = (up) => { state.streamUp = up; $("chart-live").hidden = !(up && state.feedUp); };
  es.addEventListener("open", () => mark(true));
  es.addEventListener("error", () => mark(false));
  es.addEventListener("snapshot", (e) => { const s = JSON.parse(e.data); for (const f of s.forming || []) applyMinute(f); if (s.price) setPrice(s.price, true); });
  es.addEventListener("message", (e) => {
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
  box.textContent = `${fmtDay.format(new Date(c.open_time))} ${fmtClock.format(new Date(c.open_time))} · O ${px2(c.open)}  H ${px2(c.high)}  L ${px2(c.low)}  C ${px2(c.close)}${c.forming ? " · forming" : ""}`;
}
function renderLegend() {
  const items = [];
  const sw = (c, b, text) => el("li", {}, el("span", { class: "sw", style: `--c:${rgba(c, 0.25)};--b:${rgba(c, 0.8)}` }), text);
  if (state.layers.ob) items.push(sw("--c-bull", 0, "Bullish OB"), sw("--c-bear", 0, "Bearish OB"));
  if (state.layers.fvg) items.push(sw("--c-fvg-bull", 0, "Bullish FVG"), sw("--c-fvg-bear", 0, "Bearish FVG"));
  if (state.layers.structure) items.push(el("li", {}, el("span", { class: "ln", style: `--c:${css("--text-2")}` }), "BOS / CHoCH (dashed)"));
  if (state.layers.liquidity) items.push(el("li", {}, el("span", { class: "ln", style: `--c:${rgba("--c-liq", 1)}` }), "BSL / SSL liquidity"));
  items.push(el("li", { class: "muted" }, "● = untested zone"));
  $("chart-legend").replaceChildren(...items);
}
function renderControls() {
  $("tf-seg").replaceChildren(...TFS.map((tf) => {
    const role = state.radar && state.radar.timeframes ? (state.radar.timeframes.find((x) => x.tf === tf) || {}).role : "";
    return el("button", { type: "button", role: "tab", "aria-selected": String(tf === state.tf), title: role || null,
      onclick: () => { if (state.tf !== tf) { state.tf = tf; savePrefs(); renderControls(); loadChart(false); } } },
    tf, role ? el("span", { class: "r" }, role.split(" ")[0][0]) : null);
  }));
  $("layer-chips").replaceChildren(...LAYERS.map(([k, name, c]) => el("button", { type: "button", class: "chip", "aria-pressed": String(!!state.layers[k]),
    onclick: () => { state.layers[k] = !state.layers[k]; savePrefs(); renderControls(); renderLegend(); if (state.layer) state.layer.update(); } },
  el("span", { class: "sw", style: `--c:${rgba(c, 0.8)}` }), name)));
}
async function loadChart(keepView) {
  const tf = state.tf;
  $("chart-note").hidden = true;
  try {
    const [c, a, s] = await Promise.all([
      api(`/api/market/candles?tf=${tf}&limit=${TF_BARS[tf]}`),
      api(`/api/smc/analysis?tf=${tf}&bars=${TF_BARS[tf]}`),
      api("/api/smc/signals?limit=100"),
    ]);
    if (tf !== state.tf) return;
    state.analysis = a;
    state.signals = s.items;
    setCandles(c, keepView);
    if (!c.items.length) { $("chart-note").textContent = "No market history yet — the engine loads it from Tabdeal on start."; $("chart-note").hidden = false; }
  } catch (e) {
    $("chart-note").textContent = "Chart data unavailable — retrying."; $("chart-note").hidden = false;
  }
}
async function refreshAnalysis() {
  const tf = state.tf;
  const [a, s] = await Promise.all([api(`/api/smc/analysis?tf=${tf}&bars=${TF_BARS[tf]}`), api("/api/smc/signals?limit=100")]);
  if (tf !== state.tf) return;
  state.analysis = a;
  state.signals = s.items;
  notifyChanges(s.items);
  if (state.layer) state.layer.update();
}

/* ---- side panels ---------------------------------------------------------------------------- */
function renderKpis(o, r, perf) {
  const k = (tone, label, value, detail, ic) => el("div", { class: `kpi t-${tone}` }, el("div", { class: "k" }, label),
    el("div", { class: "v" }, ic ? icon(ic) : null, value), el("div", { class: "d" }, detail || " "));
  const bias = (r && r.bias) || "UNDEFINED";
  const [bt, bi, bl] = TREND[bias];
  const act = state.signals.find((s) => s.state === "OPEN" || s.state === "PENDING");
  const smc = o.smc || {}, run = smc.runner;
  const hist = smc.history || {};
  $("kpis").replaceChildren(
    k("info", o.symbol_display || "Price", state.lastPrice !== null ? px2(state.lastPrice) : px2(o.market.last_price), o.market.age_s !== null ? `last trade ${ago(o.market.age_s)}` : "from exchange history"),
    k(bt, `Bias · ${(r && r.timeframes && r.timeframes.find((x) => x.role.startsWith("Bias")) || {}).tf || ""}`, bl, "higher-timeframe structure", bi),
    act ? k(act.side === "LONG" ? "ok" : "bad", "Position", `${act.side === "LONG" ? "Long" : "Short"} ${act.state === "OPEN" ? rText(act.open_r) : "pending"}`, `entry ${px2(act.entry)}`, act.side === "LONG" ? "up" : "down")
      : k("neutral", "Position", "Flat", "waiting for a qualified trigger", "flat"),
    k(+perf.total_r > 0 ? "ok" : +perf.total_r < 0 ? "bad" : "neutral", "Live result", rText(perf.total_r), `${perf.closed} closed · win ${pct(perf.win_rate)}`),
    k(run && run.status === "RUNNING" ? "ok" : "bad", "Engine", run ? (run.status === "RUNNING" ? "Running" : "Stale") : "Not started", run ? `heartbeat ${ago(run.heartbeat_age_s)}` : "start: python -m sp2l smc", run && run.status === "RUNNING" ? "check" : "alert"),
    k(hist.days >= hist.target_days - 1 ? "ok" : "warn", "History", hist.days !== null && hist.days !== undefined ? `${hist.days} d` : "—", `target ${hist.target_days} d · Tabdeal chart`),
  );
}
function renderTicket() {
  const act = state.signals.find((s) => s.state === "OPEN") || state.signals.find((s) => s.state === "PENDING");
  const body = $("ticket-body");
  if (!act) {
    $("ticket-state").replaceChildren(tag("neutral", "Flat", "clock"));
    const last = state.signals.find((s) => s.closed_at);
    body.replaceChildren(el("div", { class: "ticket-empty" }, el("div", { class: "big" }, "No open position"),
      el("p", { class: "muted small" }, "A position opens when an M1 break reacts inside a higher-timeframe zone aligned with the bias and the target pays the minimum R after fees."),
      last ? el("p", { class: "small" }, "Last: ", sideTag(last.side), " ", stateTag(last.state), " ", rEl(last.result_r)) : null));
    return;
  }
  $("ticket-state").replaceChildren(stateTag(act.state));
  const d = act.detail || {};
  const risk = Math.abs(+act.entry - +act.sl), reward = Math.abs(+act.tp - +act.entry);
  const tot = risk + reward || 1;
  const factors = Object.entries(d.factors || {});
  const ft = (state.radar && state.radar.factor_text) || {};
  body.replaceChildren(
    el("div", { class: "ticket-hero" }, el("div", {}, sideTag(act.side), " ", el("span", { class: "muted small" }, act.trigger_kind === "CHOCH" ? "M1 CHoCH" : "M1 BOS", act.poi_tf ? ` in ${act.poi_tf} ${(d.poi || {}).kind || "zone"}` : "")),
      el("div", { class: "big num" }, act.state === "OPEN" ? rText(act.open_r) : "—")),
    el("div", { class: "rr-bar", role: "img", "aria-label": `Risk ${risk.toFixed(2)}, reward ${reward.toFixed(2)}` },
      el("span", { class: "loss", style: `width:${(risk / tot) * 100}%` }), el("span", { class: "win", style: `width:${(reward / tot) * 100}%` })),
    el("div", { class: "rr-scale" }, el("span", {}, "Stop"), el("span", {}, `net ${act.net_rr ? (+act.net_rr).toFixed(2) : "—"}R after fees`), el("span", {}, "Target")),
    el("div", { class: "levels" },
      el("div", { class: "t-info" }, el("div", { class: "k" }, "Entry"), el("div", { class: "v num" }, px2(act.entry))),
      el("div", { class: "t-bad" }, el("div", { class: "k" }, "Stop loss"), el("div", { class: "v num" }, px2(act.sl))),
      el("div", { class: "t-ok" }, el("div", { class: "k" }, "Take profit"), el("div", { class: "v num" }, px2(act.tp)))),
    el("dl", { class: "kv small" },
      el("dt", {}, "Target"), el("dd", {}, show(act.tp_source)),
      el("dt", {}, "Score"), el("dd", {}, `${act.score} / ${factors.length}`),
      el("dt", {}, "Advisory size"), el("dd", { class: "num" }, act.qty ? `${grp((+act.qty).toFixed(3))} · ${px2(act.notional)} USDT · ${(+act.leverage).toFixed(1)}x` : "—"),
      el("dt", {}, "Opened"), el("dd", {}, when(act.filled_at || act.created_at))),
    el("div", { class: "factors" }, factors.map(([f, on]) => el("span", { class: `factor${on ? " on" : ""}`, title: ft[f] || f }, ft[f] || f))));
}
function renderRadar(r) {
  if (!r || !r.ready) { $("ladder").replaceChildren(el("li", { class: "t-neutral" }, el("span", {}, "No data yet"))); return; }
  const [bt, bic, bl] = TREND[r.bias];
  $("ladder-bias").replaceChildren(tag(bt, `Bias ${bl.toLowerCase()}`, bic));
  $("ladder").replaceChildren(...[...r.timeframes].reverse().map((t) => {
    const [tone, ic, lab] = TREND[t.trend];
    const ev = t.last_event;
    return el("li", { class: `t-${tone}` }, el("span", { class: "tf" }, t.tf),
      el("div", {}, el("div", { class: "role" }, t.role || "Context"), el("div", { class: "ev" }, ev ? `${ev.kind === "CHOCH" ? "CHoCH" : "BOS"} ${ev.direction === "LONG" ? "up" : "down"} @ ${px2(ev.level)} · ` : "no break yet", ev ? when(ev.time) : null)),
      el("span", { class: "tr" }, icon(ic), lab));
  }));
  $("poi-list").replaceChildren(...(r.pois.length ? r.pois.map((z) => el("li", { class: `t-${z.direction === "LONG" ? "ok" : "bad"}`, title: "Show on chart",
    onclick: () => { state.tf = z.tf; savePrefs(); renderControls(); loadChart(false); } },
  el("div", {}, el("div", { class: "z" }, `${z.tf} ${z.kind === "OB" ? "Order block" : "Fair value gap"}`, z.tested ? "" : " · fresh"),
    el("div", { class: "px num" }, `${px2(z.bottom)} – ${px2(z.top)}`)),
  el("div", { class: "dist" }, z.distance !== null ? pct(z.distance, 2) : "—", el("div", { class: "px" }, "away")))) : [el("li", { class: "t-neutral" }, el("span", { class: "muted small" }, r.bias === "UNDEFINED" ? "No bias, so no zones are tradable." : "No unmitigated zone with the bias right now."))]));
  $("trig-list").replaceChildren(...(r.triggers.length ? r.triggers.map((t) => el("li", { class: `t-${t.accepted ? "ok" : "neutral"}` },
    el("div", { class: "top" }, sideTag(t.direction), el("b", {}, t.kind === "CHOCH" ? "CHoCH" : "BOS"), when(t.time), t.accepted ? tag("ok", "Qualified") : null,
      t.poi_tf ? el("span", { class: "muted" }, `in ${t.poi_tf} zone`) : null),
    el("div", { class: "why" }, t.accepted ? `Score ${t.score} · net ${t.net_rr}R` : t.reasons.map((x) => x.text).join(" · ")))) : [el("li", { class: "t-neutral" }, el("span", { class: "muted small" }, "No M1 structure break in the window."))]));
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
      const side = s.side === "LONG" ? "Long" : "Short";
      if (before === undefined) toast(s.side === "LONG" ? "ok" : "bad", `New ${side} signal`, `Entry ${px2(s.entry)} · SL ${px2(s.sl)} · TP ${px2(s.tp)}`);
      else if (s.state === "OPEN") toast("info", `${side} filled`, `Entry ${px2(s.entry)}`);
      else toast(s.state === "TP" ? "ok" : s.state === "SL" ? "bad" : "neutral", `${side} ${STATE[s.state] ? STATE[s.state][1].toLowerCase() : s.state}`, rText(s.result_r));
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
async function renderSignals() {
  const filters = [["all", "All"], ["active", "Active"], ["closed", "Closed"]];
  $("sig-filter").replaceChildren(...filters.map(([k, l]) => el("button", { type: "button", role: "tab", "aria-selected": String(state.sigFilter === k),
    onclick: () => { state.sigFilter = k; renderSignals(); } }, l)));
  const d = await api(`/api/smc/signals?state=${state.sigFilter}&limit=300`);
  table("sig-table", ["Created", "Side", "Trigger", "POI", "Entry", "SL", "TP", "Net R:R", "Score", "State", "Result"], d.items, (s) => {
    const tr = el("tr", { class: "click", "aria-current": String(state.selectedSig === s.id), onclick: () => { state.selectedSig = s.id; renderSignals(); } },
      el("td", {}, when(s.created_at)), el("td", {}, sideTag(s.side)), el("td", {}, s.trigger_kind === "CHOCH" ? "CHoCH" : "BOS"),
      el("td", {}, s.poi_tf ? `${s.poi_tf} ${(s.detail.poi || {}).kind || ""}` : "—"),
      el("td", { class: "num" }, px2(s.entry)), el("td", { class: "num" }, px2(s.sl)), el("td", { class: "num" }, px2(s.tp)),
      el("td", { class: "num" }, s.net_rr ? `${s.net_rr}R` : "—"), el("td", { class: "num" }, s.score), el("td", {}, stateTag(s.state)),
      el("td", {}, s.state === "OPEN" ? rEl(s.open_r) : rEl(s.result_r)));
    return tr;
  }, "No signals yet. They appear here the moment the engine creates one.");
  const sel = d.items.find((s) => s.id === state.selectedSig);
  if (!sel) return;
  const ev = await api(`/api/smc/signals/${sel.id}/events`);
  const ft = (state.radar && state.radar.factor_text) || {};
  $("sig-detail").replaceChildren(
    el("div", { class: "ticket-hero" }, el("div", {}, sideTag(sel.side), " ", stateTag(sel.state)), el("div", { class: "big num" }, sel.state === "OPEN" ? rText(sel.open_r) : rText(sel.result_r))),
    el("div", { class: "levels" },
      el("div", { class: "t-info" }, el("div", { class: "k" }, "Entry"), el("div", { class: "v num" }, px2(sel.entry))),
      el("div", { class: "t-bad" }, el("div", { class: "k" }, "Stop loss"), el("div", { class: "v num" }, px2(sel.sl))),
      el("div", { class: "t-ok" }, el("div", { class: "k" }, "Take profit"), el("div", { class: "v num" }, px2(sel.tp)))),
    el("dl", { class: "kv small" }, el("dt", {}, "Target"), el("dd", {}, show(sel.tp_source)), el("dt", {}, "POI zone"),
      el("dd", { class: "num" }, sel.detail.poi ? `${sel.detail.poi.tf} ${sel.detail.poi.kind} ${px2(sel.detail.poi.bottom)} – ${px2(sel.detail.poi.top)}` : "—"),
      el("dt", {}, "Exit"), el("dd", { class: "num" }, px2(sel.exit_price))),
    el("div", { class: "factors" }, Object.entries(sel.detail.factors || {}).map(([f, on]) => el("span", { class: `factor${on ? " on" : ""}` }, ft[f] || f))),
    el("ol", { class: "timeline" }, ev.items.map((e) => el("li", { class: `t-${{ CREATED: "info", FILLED: "warn", TP: "ok", SL: "bad" }[e.kind] || "neutral"}` },
      el("b", {}, e.kind), " ", when(e.ts), e.price ? el("span", { class: "num muted" }, ` @ ${px2(e.price)}`) : null,
      e.detail && e.detail.result_r ? el("span", {}, " · ", rEl(e.detail.result_r)) : null))),
    el("p", {}, el("button", { class: "btn", type: "button", onclick: () => showOnChart(sel) }, icon("target"), "Show on chart")));
}
function showOnChart(s) {
  state.focus = s.id;
  location.hash = "#/chart";
  setTimeout(() => {
    if (!state.chart) return;
    const t = secs(s.created_at);
    const i = state.times.findIndex((x) => x >= t);
    if (i >= 0) state.chart.timeScale().setVisibleLogicalRange({ from: i - 60, to: i + 60 });
    state.layer.update();
  }, 300);
}

/* ---- performance view ------------------------------------------------------------------------- */
function stats(target, d) {
  const s = (k, v, tone) => el("div", { class: "stat" }, el("div", { class: "k" }, k), el("div", { class: `v num ${tone || ""}` }, v));
  $(target).replaceChildren(
    s("Closed trades", show(d.closed)), s("Win rate", pct(d.win_rate)), s("Total", rText(d.total_r), +d.total_r > 0 ? "pos" : +d.total_r < 0 ? "neg" : ""),
    s("Average", rText(d.avg_r)), s("Profit factor", show(d.profit_factor)), s("Max drawdown", d.max_drawdown_r ? `${d.max_drawdown_r}R` : "—"));
}
function drawEquity(id, points) {
  const box = $(id);
  if (!points || points.length < 2) { box.replaceChildren(el("p", { class: "empty" }, "The equity curve appears after two closed trades.")); return; }
  const W = 1000, H = 220, P = 24;
  const ys = points.map((p) => +p.r);
  const lo = Math.min(0, ...ys), hi = Math.max(0, ...ys);
  const x = (i) => P + (i / (points.length - 1)) * (W - 2 * P);
  const y = (v) => H - P - ((v - lo) / (hi - lo || 1)) * (H - 2 * P);
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`); svg.setAttribute("preserveAspectRatio", "none");
  const zero = document.createElementNS(ns, "line");
  zero.setAttribute("x1", P); zero.setAttribute("x2", W - P); zero.setAttribute("y1", y(0)); zero.setAttribute("y2", y(0));
  zero.setAttribute("stroke", css("--border-strong")); zero.setAttribute("stroke-dasharray", "4 4");
  const path = document.createElementNS(ns, "path");
  path.setAttribute("d", ys.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(""));
  path.setAttribute("fill", "none"); path.setAttribute("stroke", ys[ys.length - 1] >= 0 ? css("--ok") : css("--bad")); path.setAttribute("stroke-width", "2");
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
  const p = await api("/api/smc/performance");
  stats("live-stats", p);
  drawEquity("live-equity", p.equity);
  $("bt-meta").textContent = "running…";
  const b = await api("/api/smc/backtest?days=30");
  if (!b.ready) { $("bt-meta").textContent = "no history yet"; return; }
  $("bt-meta").textContent = `${fmtDay.format(new Date(b.from))} – ${fmtDay.format(new Date(b.to))} · ${grp(b.bars)} M1 bars · ${b.seconds}s`;
  $("bt-note").textContent = +b.total_r < 0
    ? `With the current parameters and fees this backtest loses ${rText(b.total_r)} over ${b.closed} trades. Treat live signals as experimental until the parameters show a positive expectancy here.`
    : "";
  stats("bt-stats", b);
  drawEquity("bt-equity", b.equity);
  const reasons = (state.params && state.params.reasons) || {};
  const st = b.stats;
  bars("bt-funnel", [["TRIGGERS", st.triggers, "M1 triggers"], ...Object.entries(st.rejections).map(([k, n]) => [k, n, reasons[k] || k]), ["ACCEPTED", st.accepted, "Qualified"], ["TRADED", st.signals, "Traded (capacity, one per POI)"]],
    { TRIGGERS: "neutral", ACCEPTED: "ok", TRADED: "ok" });
  bars("bt-outcomes", Object.entries(st.states).map(([k, n]) => [k, n, STATE[k] ? STATE[k][1] : k]), { TP: "ok", SL: "bad", OPEN: "warn", PENDING: "info", EXPIRED: "neutral", MISSED: "neutral", TIMEOUT: "neutral" });
  table("bt-trades", ["Created", "Side", "POI", "Entry", "SL", "TP", "Score", "State", "Result"], [...b.trades].reverse().slice(0, 60), (t) => el("tr", {},
    el("td", {}, when(t.created_at)), el("td", {}, sideTag(t.side)), el("td", {}, show(t.poi_tf)), el("td", { class: "num" }, px2(t.entry)),
    el("td", { class: "num" }, px2(t.sl)), el("td", { class: "num" }, px2(t.tp)), el("td", { class: "num" }, t.score), el("td", {}, stateTag(t.state)), el("td", {}, rEl(t.result_r))));
}

/* ---- strategy view ----------------------------------------------------------------------------- */
async function renderStrategy() {
  const p = state.params || (state.params = await api("/api/smc/params"));
  const v = Object.fromEntries(p.groups.flatMap((g) => g.items.map((i) => [i.key, i.value])));
  $("strat-ver").textContent = `${p.version} · parameters ${p.params_hash}`;
  const step = (tone, h, tfs, text) => el("li", { class: `t-${tone}` }, el("div", { class: "h" }, h), el("div", { class: "tfs" }, tfs), el("p", {}, text));
  $("model-flow").replaceChildren(
    step("info", "Bias", `${v.bias_tf} (+ ${v.confirm_bias_tf})`, "The trend of the bias timeframe's last BOS / CHoCH decides the only direction that can be traded."),
    step("violet", "Point of interest", v.poi_tfs.join(" → "), "An unmitigated order block or fair value gap with the bias. Price has to react inside it."),
    step("warn", "Trigger", v.trigger_tf, "An M1 BOS / CHoCH whose order block overlaps the zone confirms the reaction (liquidity sweep and displacement add score)."),
    step("ok", "Execution", `${v.trigger_tf} · ${v.entry_mode}`, `Stop ${v.sl_mode === "poi" ? "beyond the zone" : "beyond the M1 block"}, target the nearest liquidity paying ≥ ${v.min_net_rr}R after fees.`));
  $("param-grid").replaceChildren(...p.groups.map((g) => el("section", { class: "card" }, el("h2", {}, g.name),
    el("dl", { class: "kv" }, g.items.flatMap((i) => [el("dt", {}, el("code", {}, i.key)), el("dd", { class: "num" }, Array.isArray(i.value) ? i.value.join(", ") || "—" : show(i.value))])))),
  el("section", { class: "card" }, el("h2", {}, "Costs (from config)"), el("dl", { class: "kv" },
    ...Object.entries(p.costs).flatMap(([k, x]) => [el("dt", {}, el("code", {}, k)), el("dd", { class: "num" }, pct(x, 3))]))));
  kv("factor-list", Object.entries(p.factors).map(([k, t]) => [el("code", {}, k), t]));
  kv("reason-list", Object.entries(p.reasons).map(([k, t]) => [el("code", {}, k), t]));
}

/* ---- system view -------------------------------------------------------------------------------- */
async function renderSystem() {
  const [o, q] = await Promise.all([api("/api/overview"), api("/api/market/quality?minutes=180")]);
  const run = o.smc.runner, h = o.smc.history;
  kv("sys-runner", run ? [["Status", run.status === "RUNNING" ? tag("ok", "Running") : tag("bad", "Stale")], ["Heartbeat", ago(run.heartbeat_age_s)],
    ["Started", when(run.started_at)], ["Last M1 analysed", when(run.last_m1)], ["Parameters", el("code", {}, show(run.params_hash))],
    ["Config parameters", el("code", {}, o.smc.params_hash)], ["Costs", o.costs_problem ? tag("bad", "Missing") : tag("ok", "Configured")]]
    : [["Status", tag("bad", "Not started")], ["Start", el("code", {}, "python -m sp2l smc")]]);
  kv("sys-history", [["Source", "Tabdeal chart history (1-minute)"], ["From", when(h.first)], ["To", when(h.last)], ["Depth", h.days !== null ? `${h.days} days` : "—"],
    ["Target", `${h.target_days} days`], ["Series lag", o.smc.series_lag_s !== null ? `${o.smc.series_lag_s} s` : "—"],
    ["Last refresh", run && run.history && run.history.error ? tag("warn", "Retrying") : tag("ok", "OK")]]);
  const c = o.collector;
  kv("sys-collector", [["Status", c.status === "CONNECTED" ? tag("ok", "Connected") : tag(c.status === "NO_DATA" ? "neutral" : "warn", c.status)],
    ["Heartbeat", ago(c.heartbeat_age_s)], ["Run started", when(c.run && c.run.started_at)], ["Last 60 min", o.market.quality.label]]);
  $("dq-strip").replaceChildren(...q.timeline.map((m) => el("span", { class: m.status === "OK" ? "ok" : m.status === "SYNTHETIC" ? "syn" : "gap", title: `${fmtClock.format(new Date(m.minute))} ${m.status}` })));
  table("sys-gaps", ["From", "To", "Reason", "Timeframe"], q.gaps, (g) => el("tr", {}, el("td", {}, when(g.gap_start)), el("td", {}, when(g.gap_end)), el("td", {}, show(g.reason)), el("td", {}, show(g.timeframe))), "No gaps recorded");
}

/* ---- top bar, routing, refresh ----------------------------------------------------------------- */
function renderTop(o) {
  $("symbol").textContent = o.symbol_display;
  $("chart-symbol").textContent = `${o.symbol_display} · ${state.tf}`;
  const run = o.smc.runner;
  const rp = $("runner-pill");
  rp.style.setProperty("--tone", css(run && run.status === "RUNNING" ? "--ok" : "--bad"));
  rp.replaceChildren(el("span", { class: "dot" }), run && run.status === "RUNNING" ? "Engine running" : "Engine stopped");
  const fp = $("feed-pill");
  fp.style.setProperty("--tone", css(o.collector.status === "CONNECTED" ? "--ok" : "--warn"));
  fp.replaceChildren(el("span", { class: "dot" }), o.collector.status === "CONNECTED" ? "Live feed" : "History only");
  fp.title = o.collector.status === "CONNECTED" ? "Live trades from the collector" : "No live collector: candles come from Tabdeal history, refreshed every minute";
  state.feedUp = o.collector.status === "CONNECTED";
  $("chart-live").hidden = !(state.streamUp && state.feedUp);
  $("foot-ver").textContent = `SMC ${o.smc.params_hash}`;
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
    const [o, perf] = await Promise.all([api("/api/overview"), api("/api/smc/performance")]);
    state.overview = o;
    renderTop(o);
    if (state.view === "chart") {
      await refreshAnalysis();
      renderKpis(o, state.radar, perf);
      renderTicket();
    } else {
      const s = await api("/api/smc/signals?limit=100");
      notifyChanges(s.items);
      state.signals = s.items;
    }
  } catch { /* transient; next tick retries */ }
}
async function refreshSlow() {
  try {
    if (state.view === "chart") {
      state.radar = await api("/api/smc/radar");
      renderRadar(state.radar);
      renderControls();
    }
  } catch { /* transient */ }
}
async function refreshView() {
  if (state.view === "chart") { await loadChart(true); await refreshSlow(); await refreshFast(); }
  else if (state.view === "signals") await renderSignals();
  else if (state.view === "performance") await renderPerformance();
  else if (state.view === "strategy") await renderStrategy();
  else if (state.view === "system") await renderSystem();
}
function setTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem("smc-theme", t); } catch { /* storage unavailable */ }
  $("theme-btn").replaceChildren(icon(t === "dark" ? "sun" : "moon"));
  restyle();
}
function init() {
  let theme = "dark";
  try { theme = localStorage.getItem("smc-theme") || (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark"); } catch { /* default */ }
  setTheme(theme);
  $("theme-btn").addEventListener("click", () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));
  renderAlertBtn();
  $("alert-btn").addEventListener("click", () => {
    state.alerts = !state.alerts; savePrefs(); renderAlertBtn();
    if (state.alerts && window.Notification && Notification.permission === "default") Notification.requestPermission();
  });
  $("tz-label").textContent = `Times in ${tzText()}`;
  api("/api/smc/params").then((p) => { state.params = p; }).catch(() => {});
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
