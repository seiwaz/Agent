/* SP2L Dashboard — render-only client (UI-02).
 * Every state, label, tone, number and reason shown here is computed by the backend API.
 * This file holds NO strategy thresholds, gate math or state derivation: it fetches JSON,
 * formats it for people (local time, digit grouping, percent) and renders it.
 */
"use strict";

/* ---- icons (Lucide-style strokes, static markup) ---------------------------------------- */
const ICONS = {
  check: '<path d="M20 6 9 17l-5-5"/>',
  x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
  alert: '<path d="m21.73 18-8-14a2 2 0 0 0-3.46 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
  info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
  dot: '<circle cx="12" cy="12" r="5"/>',
  clock: '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
  pause: '<rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/>',
  play: '<path d="m6 3 14 9-14 9V3z"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/>',
  moon: '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
  lock: '<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
  dashboard: '<rect x="3" y="3" width="7" height="9" rx="1"/><rect x="14" y="3" width="7" height="5" rx="1"/><rect x="14" y="12" width="7" height="9" rx="1"/><rect x="3" y="16" width="7" height="5" rx="1"/>',
  candidates: '<path d="M8 6h13M8 12h13M8 18h13"/><path d="M3 6h.01M3 12h.01M3 18h.01"/>',
  history: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l4 2"/>',
  analytics: '<path d="M3 3v18h18"/><path d="m7 15 4-4 3 3 5-6"/>',
  diagnostics: '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
  indicators: '<path d="M3 17l5-5 4 4 8-8"/><path d="M14 8h6v6"/>',
  up: '<path d="m18 15-6-6-6 6"/>',
  down: '<path d="m6 9 6 6 6-6"/>',
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
    else if (k === "style") n.style.cssText = v;  // CSSOM: allowed under the page CSP
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

/* ---- formatting (display only) ------------------------------------------------------------ */
const TZ = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
const fmtClock = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
const fmtDay = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" });
const fmtHM = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", hour12: false });
const fmtOffset = new Intl.DateTimeFormat("en-US", { timeZoneName: "shortOffset" });
function tzText() {
  const part = fmtOffset.formatToParts(new Date()).find((p) => p.type === "timeZoneName");
  return `${TZ} (${part ? part.value.replace("GMT", "UTC") : "local"})`;
}
/** Local time with the day when it is not today; exact UTC in the tooltip. */
function when(iso) {
  if (!iso) return el("span", {}, "—");
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return el("span", {}, show(iso));
  const today = new Date().toDateString() === d.toDateString();
  return el("time", { datetime: d.toISOString(), title: `${d.toISOString().replace(".000", "")} UTC`, class: "num" },
    today ? fmtClock.format(d) : `${fmtDay.format(d)} ${fmtClock.format(d)}`);
}
/** Group the integer digits of an exact decimal string without converting it to a float. */
function grp(v) {
  if (v === null || v === undefined || v === "") return "—";
  let s = String(v);
  if (/^-?\d+\.\d*0$/.test(s)) s = s.replace(/0+$/, "").replace(/\.$/, "");  // storage padding
  if (!/^-?\d+(\.\d+)?$/.test(s)) return s;
  const [i, f] = s.split(".");
  return i.replace(/\B(?=(\d{3})+(?!\d))/g, ",") + (f ? `.${f}` : "");
}
function pct(fraction, digits) {
  if (fraction === null || fraction === undefined) return "—";
  return new Intl.NumberFormat(undefined, { style: "percent", maximumFractionDigits: digits ?? 2 }).format(fraction);
}
function ago(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 60) return `${seconds} s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  return `${Math.floor(seconds / 3600)} h ago`;
}

/* ---- tone tags: color is always paired with text and an icon ------------------------------- */
const TONE_ICON = { ok: "check", bad: "x", warn: "alert", info: "dot", neutral: "clock" };
function tag(tone, label, code) {
  return el("span", { class: `tag-tone t-${tone || "neutral"}`, title: code ? `code: ${code}` : null },
    icon(TONE_ICON[tone] || "dot"), show(label));
}
function sideTag(side) { return el("span", { class: "side" }, show(side)); }
function codeEl(code) { return code ? el("code", {}, code) : null; }
function table(target, headers, rowsData, rowFn, emptyText) {
  const t = typeof target === "string" ? $(target) : target;
  if (!t) return;
  t.replaceChildren();
  t.append(el("thead", {}, el("tr", {}, headers.map((h) => el("th", { scope: "col" }, h)))));
  const body = el("tbody");
  if (!rowsData || rowsData.length === 0) {
    body.append(el("tr", {}, el("td", { colspan: headers.length, class: "empty" }, emptyText || "Nothing recorded yet")));
  } else {
    for (const r of rowsData) body.append(rowFn(r));
  }
  t.append(body);
}
function kv(target, pairs) {
  $(target).replaceChildren(...pairs.flatMap(([k, v, cls]) => [el("dt", {}, k), el("dd", { class: cls || null }, v instanceof Node ? v : show(v))]));
}
function progress(p) {
  const bar = el("div", { class: `progress${p.complete ? " done" : ""}`, role: "progressbar",
    "aria-valuemin": "0", "aria-valuemax": String(p.target), "aria-valuenow": String(p.current),
    "aria-label": "Context warmup" }, el("span"));
  bar.firstChild.style.width = `${p.percent ?? 0}%`;  // backend-computed percent
  return bar;
}
async function api(path) {
  const r = await fetch(path, { headers: { Accept: "application/json" } });
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
}

/* ---- state ---------------------------------------------------------------------------------- */
const state = { view: "dashboard", bucket: null, selected: null, paused: false, overview: null,
  candles: [], chart: null, series: null, priceLines: [], equity: {}, zones: [], zoneLayer: null,
  data: null, byTime: new Map(), forming: new Map(), live: null, livePrice: null };

/* ---- routing: #/dashboard, #/candidates[/KEY], #/history, #/analytics, #/diagnostics --------- */
const VIEWS = ["dashboard", "candidates", "history", "indicators", "analytics", "diagnostics"];
function route() {
  const parts = location.hash.replace(/^#\/?/, "").split("/");
  const v = VIEWS.includes(parts[0]) ? parts[0] : "dashboard";
  state.view = v;
  if (v === "candidates" && parts[1]) state.selected = decodeURIComponent(parts[1]);
  for (const name of VIEWS) $(`view-${name}`).hidden = name !== v;
  for (const a of $("nav").querySelectorAll("a")) {
    if (a.dataset.view === v) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
  const slot = $(v === "candidates" ? "chart-slot-candidates" : "chart-slot-dashboard");
  if ($("chart").parentElement !== slot) slot.append($("chart"), $("go-live"));
  refreshFast(); refreshSlow();
}

/* ---- dashboard: status ------------------------------------------------------------------------ */
function renderStatus(sys) {
  const tile = (name, s, extra) => el("div", { class: `status-tile t-${s.tone}` },
    el("div", { class: "k" }, name),
    el("div", { class: "v" }, icon(TONE_ICON[s.tone] || "dot"), s.label),
    el("div", { class: "d", title: s.code ? `code: ${s.code}` : `state: ${s.state}` }, extra || show(s.detail)));
  const sh = sys.shadow;
  $("status-tiles").replaceChildren(
    tile("Market data", sys.data),
    tile("Strategy", sys.strategy),
    tile("Shadow", sh, sh.activity_at ? el("span", {}, "Last recorded activity ", when(sh.activity_at)) : null),
    tile("Real trading", sys.live));

  const b = sys.blocker;
  const box = $("blocker");
  box.className = `blocker t-${b.tone}`;
  const kids = [el("div", { class: "lead" }, icon(TONE_ICON[b.tone] || "info"),
    el("div", {}, el("div", { class: "lbl" }, b.blocking ? "Current blocker" : "Right now"), el("b", { title: `code: ${b.code}` }, b.text)))];
  const w = sys.feed.warmup;
  if (!w.complete && b.code === "CONTEXT_WARMUP") {
    kids.push(el("div", { class: "progress-row" }, progress(w),
      el("span", { class: "muted small" }, `${w.current} / ${w.target} finalized M5 bars in the current unbroken segment`,
        w.remaining_text ? ` · ${w.remaining_text}` : "")));
  }
  const more = [...sys.other_blockers, ...sys.notes];
  if (more.length) {
    kids.push(el("ul", {}, more.map((n) => el("li", { class: `t-${n.tone}`, title: `code: ${n.code}` },
      icon(TONE_ICON[n.tone] || "info"), n.text))));
  }
  box.replaceChildren(...kids);
}

function renderMarketData(sys) {
  const f = sys.feed;
  const rows = [
    ["Status", tag(sys.data.tone, sys.data.label, sys.data.state)],
    ["Connections", `${f.connections_up} / ${f.connections_total}`, "num"],
    ["Coverage", tag(f.coverage.tone, f.coverage.label)],
    ["Warmup", f.warmup.complete ? tag("ok", "Context ready") : `${f.warmup.current} / ${f.warmup.target} M5 bars`],
  ];
  if (f.repair && f.repair.enabled) {
    rows.push(["Gap repair (last hour)", f.repair.pending ? tag("info", "Repairing a short gap")
      : `${f.repair.repaired_last_hour} repaired · ${f.repair.unrecovered_last_hour} unrecoverable`]);
  }
  if (f.connections_up < f.connections_total) {
    for (const c of f.connections) rows.push([c.name, tag(c.tone, c.label)]);
  }
  if (f.conflicts.current_run) rows.push(["Payload conflicts (current run)", tag("warn", String(f.conflicts.current_run))]);
  else if (f.conflicts.last_24h) rows.push(["Payload conflicts (last 24 h)", tag("warn", String(f.conflicts.last_24h))]);
  const dl = el("dl", { class: "kv" });
  dl.append(...rows.flatMap(([k, v, cls]) => [el("dt", {}, k), el("dd", { class: cls || null }, v)]));
  $("market-data").replaceChildren(...[dl, f.warmup.complete ? null : el("div", { style: "margin-top:10px" }, progress(f.warmup))].filter(Boolean));
}

function renderChartHead(o) {
  const m = o.market;
  $("symbol").textContent = m.symbol;
  $("chart-symbol").textContent = m.symbol;
  $("chart-tf").textContent = m.timeframe;
  if (!state.livePrice || Date.now() - state.livePrice.at > 30000) {  // the live feed wins
    $("chart-price").textContent = grp(m.last_price);
    $("chart-price").title = m.last_trade_at ? `last trade ${m.last_trade_at}` : "";
  }
  $("chart-age").replaceChildren("updated ", ago(m.age_s));
  $("chart-quality").replaceChildren(tag(m.quality.tone, `Data: ${m.quality.label}`, `${m.quality.window}`));
}

function renderStrategySummary(o, list) {
  const sys = o.system;
  const active = o.active[0];
  const box = $("strategy-summary");
  if (active) {
    box.replaceChildren(el("div", { class: "stack" },
      el("div", { class: "pos-head" }, sideTag(active.side), tag(active.result.tone, active.result.label, active.status)),
      el("div", {}, active.stage_label),
      el("a", { class: "link", href: `#/candidates/${encodeURIComponent(active.setup_key)}` }, "Open setup details")));
    return;
  }
  const last = list && list.items && list.items[0];
  box.replaceChildren(el("div", { class: "stack" },
    el("div", { class: "big" }, "No active setup"),
    el("div", { class: "muted" }, sys.blocker.text),
    last ? el("div", {}, el("h3", { style: "margin:4px 0 6px" }, "Last candidate"), candButton(last)) : null));
}

/* ---- setup (dashboard card + candidate detail) ------------------------------------------------- */
function flowEl(flow) {
  return el("ol", { class: "flow", "aria-label": "SP2L flow" }, flow.map((f) =>
    el("li", { class: `t-${f.tone}`, title: `${f.help || ""}${f.help ? " · " : ""}state: ${f.status}` },
      el("span", { class: "n" }, f.name),
      el("span", { class: "s" }, icon(TONE_ICON[f.tone] || "dot"), f.label))));
}
function stopReason(d) {
  const s = d.summary;
  const failed = d.flow_human.find((f) => ["Rejected", "Expired", "Uncertain"].includes(f.label));
  if (!failed && !s.reason_human) return null;
  // every recorded reason of the stage that stopped it, besides the primary one
  const stage = s.status === "REJECTED_EXHAUSTION" ? d.exhaustion_summary : s.status === "REJECTED_CONTEXT" ? d.context_summary : null;
  const primary = s.reason_human ? s.reason_human.code : null;
  const sub = (stage ? stage.reasons : []).filter((r) => r.code !== primary);
  return el("div", { class: `stop-reason t-${s.result.tone}` },
    el("span", { class: "h" }, failed ? `Stopped at ${failed.name}: ${failed.label}` : s.result.label),
    s.reason_human ? el("span", {}, s.reason_human.label) : null,
    sub.length ? el("span", { class: "small muted" }, s.status === "REJECTED_EXHAUSTION" ? "Because: " : "Also: ", sub.map((r) => r.label).join(" · ")) : null,
    el("span", { class: "codes" }, "Internal codes: ",
      ...[s.reason_human ? s.reason_human.code : s.status, ...sub.map((r) => r.code)].flatMap((c, i) => [i ? " · " : null, codeEl(c)])));
}
function gateCard(title, summary, gates, emptyText) {
  if (!summary) return el("div", { class: "gate-card" }, el("h3", {}, title), el("p", { class: "empty small" }, emptyText));
  const techRow = (g) => el("tr", { class: g.threshold === "" ? "final" : null },
    el("td", {}, g.gate),
    el("td", { class: "num", title: Object.keys(g.exact || {}).length ? `exact: ${JSON.stringify(g.exact)}` : null }, show(g.value)),
    el("td", { class: "wrap" }, show(g.threshold)),
    el("td", {}, show(g.result)),
    el("td", {}, codeEl(g.reason)));
  const tbl = el("table");
  const wrap = el("div", { class: "table-wrap" }, tbl);
  const details = el("details", { class: "tech" }, el("summary", {}, "Technical detail: raw values, rules, exact values, reason codes"), wrap);
  const card = el("div", { class: "gate-card" },
    el("div", { class: "card-head" }, el("h3", { style: "margin:0" }, title), tag(summary.result.tone, summary.result.label, summary.result.code)),
    el("p", { class: "q" }, summary.question),
    el("div", { class: "rows" }, summary.rows.map((r) => el("div", { class: "r" },
      el("span", {}, r.name), el("span", { class: `v t-${r.tone}`, title: r.code ? `code: ${r.code}` : null }, r.value)))),
    details);
  table(tbl, ["Gate", "Raw value", "Rule", "Result", "Reason code"], gates, techRow, "not evaluated");
  return card;
}
function levelsEl(d) {
  const lv = d.levels || {};
  const items = [["E1", lv.e1], ["E2 (toward stop)", lv.e2 ?? lv.e2_reference], ["Stop loss", lv.sl],
    ["Target (single)", lv.tp], ["R (E1 to stop)", lv.r], ["Qty per entry", d.qty],
    ["Breakout level", d.breakout_level ? d.breakout_level.price : null],
    ["Origin", d.setup.side === "LONG" ? d.setup.origin_low : d.setup.origin_high]];
  return el("div", { class: "levels-grid" }, items.filter(([, v]) => v !== null && v !== undefined)
    .map(([k, v]) => el("div", { class: "lv" }, el("div", { class: "k" }, k), el("div", { class: "v num" }, grp(v)))));
}
function fillWindowEl(fw) {
  if (!fw) return null;
  return el("div", { style: "margin-top:16px" }, el("h3", { style: "margin-top:0" }, `Fill window · candle ${fw.candle} of ${fw.of}`),
    el("div", { class: "fw", "aria-label": `candle ${fw.candle} of ${fw.of}` },
      Array.from({ length: fw.of }, (_, i) => el("span", { class: i < fw.candle ? "on" : null }))),
    el("div", { class: "small muted", style: "margin-top:4px" }, "Pullback started ", when(fw.pullback_minute)));
}
/* P-Gap quality card: backend values and verdicts rendered as-is (V5.9) */
function pgapQualityEl(q) {
  if (!q) return null;
  const head = el("div", { class: "pq-head" }, el("h3", {}, "P-Gap quality"),
    q.measured ? tag(q.valid ? "ok" : "warn", q.valid ? "Valid" : "Rejected", (q.reason_codes || []).join(", ") || "PGAP_VALID")
      : tag("neutral", "Not measured"));
  if (!q.measured) return el("div", { class: "pq" }, head, el("p", { class: "muted small" }, q.text));
  return el("div", { class: "pq" }, head,
    el("dl", { class: "kv pq-rows" }, ...q.rows.flatMap((r) => [el("dt", {}, r.label),
      el("dd", { class: r.ok ? "" : "pq-bad" }, el("span", { class: "num" }, r.value),
        r.required ? el("span", { class: "muted small" }, ` · required ${r.required}`) : null)])),
    el("div", { class: `pq-result ${q.valid ? "ok" : "bad"}`, title: (q.reason_codes || []).length ? `code: ${q.reason_codes.join(", ")}` : null },
      el("span", { class: "muted small" }, "Result "), el("b", {}, q.result)));
}
function setupBody(d) {
  const s = d.summary;
  return [
    el("div", { class: "detail-head" }, sideTag(s.side), tag(s.result.tone, s.result.label, s.status),
      el("span", { class: "muted small" }, "Detected ", when(s.created_at)),
      el("code", { class: "small", title: "setup key" }, s.setup_key)),
    flowEl(d.flow_human),
    stopReason(d),
    pgapQualityEl(d.pgap_quality),
    levelsEl(d),
    fillWindowEl(d.fill_window),
    el("div", { class: "setup-grid" },
      gateCard("Context", d.context_summary, d.context_gates, "Not evaluated for this candidate"),
      gateCard("Exhaustion", d.exhaustion_summary, d.exhaustion_gates, "Not evaluated (Context did not pass first)")),
  ].filter(Boolean);
}
function technical(d) {
  const sec = (title, id) => el("details", { class: "tech" }, el("summary", {}, title), el("div", { class: "table-wrap" }, el("table", { id })));
  const box = $("detail-tech");
  box.replaceChildren(sec("E1 revision history", "t-rev"), sec("Orders", "t-orders"), sec("Fills", "t-fills"),
    sec("Event log", "t-events"), sec("Counterfactual (hypothetical, never affects Shadow)", "t-cf"));
  table("t-rev", ["Rev", "E1", "SL", "TP", "E2", "R", "Qty", "Worst loss", "Costs", "At"], d.e1_revisions, (r) =>
    el("tr", {}, ...[r.revision, r.e1, r.sl, r.tp, r.e2_reference, r.r, r.qty, r.modeled_worst_loss, r.modeled_costs]
      .map((v) => el("td", { class: "num" }, grp(v))), el("td", {}, when(r.created_at))), "Entry was never placed");
  table("t-orders", ["Leg", "Rev", "Price", "Qty", "Status", "Executed", "Updated"], d.orders, (o) =>
    el("tr", {}, el("td", {}, o.leg), el("td", { class: "num" }, show(o.revision_id)), el("td", { class: "num" }, grp(o.price)),
      el("td", { class: "num" }, grp(o.qty)), el("td", {}, codeEl(o.status)), el("td", { class: "num" }, grp(o.executed_qty)),
      el("td", {}, when(o.updated_at))), "No orders");
  table("t-fills", ["Leg", "Time", "Price", "Qty", "Fee"], d.fills, (f) =>
    el("tr", {}, el("td", {}, f.leg), el("td", {}, when(f.ts)), el("td", { class: "num" }, grp(f.price)),
      el("td", { class: "num" }, grp(f.qty)), el("td", { class: "num" }, grp(f.fee))), "No fills");
  table("t-events", ["Time", "Event", "Payload"], [...d.events].reverse(), (e) =>
    el("tr", {}, el("td", {}, when(e.ts)), el("td", {}, codeEl(e.event_type)), el("td", { class: "wrap mono small" }, JSON.stringify(e.payload))), "No events");
  const cf = d.counterfactual;
  table("t-cf", ["Stage", "Outcome", "Result (R, gross)", "E2 filled"], cf ? [cf] : [], (c) =>
    el("tr", {}, el("td", {}, show(c.rejection_stage)), el("td", {}, codeEl(c.outcome)), el("td", { class: "num" }, show(c.result_r)),
      el("td", {}, show(c.e2_filled))), "No counterfactual for this candidate");
}

function renderDashboardSetup(d) {
  // candidate-specific panels only when there is an active setup (the empty state lives in
  // the Strategy card, so nothing is repeated)
  $("setup-card").hidden = !d;
  if (d) $("setup-body").replaceChildren(...setupBody(d));
}

/* ---- shadow ------------------------------------------------------------------------------------ */
function kpis(stats, wallet) {
  const k = (name, v, tone, d) => el("div", { class: "kpi" }, el("div", { class: "k" }, name),
    el("div", { class: `v num${tone ? ` t-${tone}` : ""}` }, v), d ? el("div", { class: "d" }, d) : null);
  return el("div", { class: "kpi-grid" },
    k("Shadow wallet", wallet && wallet.balance !== null ? `${grp(wallet.balance)}` : "—", null, "USDT"),
    k("Net PnL", stats.net_pnl === null ? "—" : grp(stats.net_pnl), stats.net_pnl_tone, "USDT · confirmed"),
    k("Confirmed trades", String(stats.trades), null, `${stats.wins} won · ${stats.losses} lost`),
    k("Win rate", stats.win_rate_pct === null ? "—" : `${stats.win_rate_pct}%`, null, "confirmed trades"),
    k("Expectancy", stats.expectancy === null ? "—" : grp(stats.expectancy), null, "USDT per trade"),
    k("Max drawdown", grp(stats.max_drawdown), null, `USDT · ${show(stats.max_drawdown_pct)}% of peak`));
}
function renderPosition(p) {
  const card = $("position-card");
  if (!p) { card.hidden = true; return; }
  card.hidden = false;
  const rows = [["E1", grp(p.e1)], ["E2", p.e2_state], ["Average entry", grp(p.avg_entry)], ["Quantity", grp(p.qty)],
    ["Stop loss", grp(p.sl)], ["Target", grp(p.tp)], ["Current price", grp(p.last_price)],
    ["Move from E1", p.move_r === null ? "—" : `${p.move_r} R`], ["Open for", p.duration], ["Liquidation", p.liquidation]];
  const dl = el("dl", { class: "kv" });
  dl.append(...rows.flatMap(([k, v]) => [el("dt", {}, k), el("dd", { class: "num" }, show(v))]));
  $("position-body").replaceChildren(
    el("div", { class: "pos-head" }, sideTag(p.side), el("span", { class: `pnl num t-${p.unrealized_tone || "neutral"}` },
      p.unrealized_pnl === null ? "—" : `${grp(p.unrealized_pnl)} USDT`), el("span", { class: "muted small" }, "unrealized")),
    dl, el("a", { class: "link", href: `#/candidates/${encodeURIComponent(p.setup_key)}` }, "Setup details"));
}
function tradesTable(id, trades, empty) {
  table(id, ["Closed", "Side", "Result", "Net PnL (USDT)", "Setup"], trades, (t) =>
    el("tr", {}, el("td", {}, when(t.exit_at)), el("td", {}, sideTag(t.side)),
      el("td", {}, tag(t.exit_kind === "TP" ? "ok" : t.exit_kind === "SL" ? "bad" : "neutral",
        t.exit_kind === "TP" ? "Target hit" : t.exit_kind === "SL" ? "Stop hit" : show(t.exit_kind), t.exit_kind)),
      el("td", { class: "num" }, grp(t.net_pnl_display)),
      el("td", {}, el("a", { class: "link", href: `#/candidates/${encodeURIComponent(t.setup_key)}` }, "details"))), empty);
}
function renderShadow(o) {
  const p = o.performance;
  const box = $("shadow-body");
  if (!p || !p.session) {
    box.replaceChildren(el("div", { class: "card" }, el("p", { class: "empty" }, el("b", {}, "Shadow has not started. "),
      o.system.shadow.detail || "", ". Results appear here once a Shadow session is running.")));
    return;
  }
  box.replaceChildren(kpis(p.stats, o.wallet),
    el("div", { class: "shadow-grid" },
      el("section", { class: "card" }, el("h2", {}, "Recent confirmed trades"), el("div", { class: "table-wrap" }, el("table", { id: "recent-trades" }))),
      el("section", { class: "card" }, el("h2", {}, "Equity · confirmed trades"), el("div", { class: "equity", id: "equity-dash", role: "img", "aria-label": "Confirmed Shadow equity curve" }))));
  tradesTable("recent-trades", p.recent_trades.slice(0, 8), "No confirmed trades yet");
  drawEquity("equity-dash", p.stats.equity);
}

/* ---- charts ------------------------------------------------------------------------------------ */
function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
function chartTheme() {
  return { layout: { background: { color: css("--surface") }, textColor: css("--text-2"), fontFamily: "IBM Plex Sans, sans-serif" },
    grid: { vertLines: { color: css("--border") }, horzLines: { color: css("--border") } },
    rightPriceScale: { borderColor: css("--border") },
    localization: { timeFormatter: (t) => fmtClock.format(new Date(t * 1000)) },
    // axis ticks in the same local time zone as the rest of the page (library default is UTC)
    timeScale: { borderColor: css("--border"), timeVisible: true, secondsVisible: false,
      tickMarkFormatter: (t, type) => (type < 3 ? fmtDay : fmtHM).format(new Date(t * 1000)) } };
}
function ensureChart() {
  if (state.chart || !window.LightweightCharts) return;
  window.sp2lChartCreates = (window.sp2lChartCreates || 0) + 1;  // test hook: created once
  state.chart = LightweightCharts.createChart($("chart"), { autoSize: true, ...chartTheme() });
  state.chart.subscribeCrosshairMove(showOhlc);
  const ts = state.chart.timeScale();
  ts.subscribeVisibleLogicalRangeChange(() => { $("go-live").hidden = !(ts.scrollPosition() < -3); });
  $("go-live").addEventListener("click", () => ts.scrollToRealTime());
  state.series = state.chart.addCandlestickSeries({
    upColor: css("--ok"), borderUpColor: css("--ok"), wickUpColor: css("--ok"),
    downColor: "rgba(0,0,0,0)", borderDownColor: css("--bad"), wickDownColor: css("--bad"),
  });
}
const secs = (iso) => Math.floor(Date.parse(iso) / 1000);  // chart library needs epoch seconds
/* candle lineage names (the backend's quality codes; the code stays in the tooltip) */
const QUALITY = { LIVE_RECONCILED: "live, reconciled with Tabdeal",
  RECENT_TRADES_REPAIRED: "repaired exactly from Tabdeal recent trades",
  TABDEAL_HISTORY_REPAIRED: "restored from Tabdeal chart history (OHLCV only, trade count unknown)",
  REST_REPAIRED: "repaired exactly from Tabdeal recent trades",
  LIVE_WS_ONLY: "live, not reconciled (REST unavailable)", LIVE_PROVEN_RAW: "live trades (before reconciliation)",
  SYNTHETIC_NO_TRADE: "no trades (proven quiet)", REPAIRED_TABDEAL: "repaired from Tabdeal",
  LIVE_FORMING: "Live candle (forming — not final, never used by the strategy)",
  CONFLICTED: "recorded before the multi-fill fix", DATA_GAP: "no data (unrecovered)" };
const REPAIR_TYPE = { EXACT_RAW_REPAIR: "Exact (trades and order recovered)",
  CANDLE_HISTORY_REPAIR: "Candle history (OHLCV only)" };
const toBar = (c) => (c.missing ? { time: secs(c.open_time) }
  : { time: secs(c.open_time), open: +c.open, high: +c.high, low: +c.low, close: +c.close });
function setDataKeepView() {
  // replacing the data never yanks a user who is inspecting older history back to "now"
  const ts = state.chart.timeScale();
  const away = ts.scrollPosition() < -3;
  const r = away ? ts.getVisibleLogicalRange() : null;
  state.series.setData(state.data);
  if (r) ts.setVisibleLogicalRange(r);
}
function renderCandles(d) {
  ensureChart();
  state.candles = d.items.filter((c) => !c.missing);
  state.byTime = new Map(d.items.map((c) => [secs(c.open_time), c]));
  // a minute without a canonical candle is drawn as an empty slot, never bridged
  state.data = d.items.map(toBar);
  const lastFinal = state.data.length ? state.data[state.data.length - 1].time : 0;
  // live-delivered candles (forming or already final) newer than the stored series; once the
  // stored series contains a minute, the stored canonical candle is used
  for (const [t, c] of [...state.forming].sort((a, b) => a[0] - b[0])) {
    if (t > lastFinal) { state.byTime.set(t, c); state.data.push(toBar(c)); } else state.forming.delete(t);
  }
  if (!state.series) return;
  setDataKeepView();
}
/* ---- live chart (V5.9): backend candles applied as-is; display only --------------------------- */
function upsertBar(c) {
  const t = secs(c.open_time), bar = toBar(c);
  state.byTime.set(t, c);
  if (!state.series || !state.data) return;
  const last = state.data.length ? state.data[state.data.length - 1].time : -1;
  if (t >= last) {  // the common case: in-place update of the newest candle, no redraw
    if (t === last) state.data[state.data.length - 1] = bar; else state.data.push(bar);
    state.series.update(bar);
    return;
  }
  const i = state.data.findIndex((b) => b.time >= t);  // an older minute (final/revision)
  if (i >= 0 && state.data[i].time === t) state.data[i] = bar; else state.data.splice(i < 0 ? state.data.length : i, 0, bar);
  setDataKeepView();
}
function livePrice(price) {
  if (price === null || price === undefined) return;
  state.livePrice = { price, at: Date.now() };
  $("chart-price").textContent = grp(price);
  $("chart-price").title = "latest canonical trade (live)";
}
function applyLive(ev) {
  if (ev.type === "snapshot") {
    for (const f of ev.forming || []) applyLive({ ...f, type: "trade" });
    livePrice(ev.price);
    return;
  }
  const t = secs(ev.t);
  const cur = state.byTime ? state.byTime.get(t) : null;
  if (ev.type === "trade") {
    if (cur && !cur.live) return;  // the canonical final candle always wins
    if (cur && cur.live && cur.trade_count > ev.n) return;  // never step back
    const c = { open_time: ev.t, open: ev.o, high: ev.h, low: ev.l, close: ev.c, volume: ev.v,
      trade_count: ev.n, quality: "LIVE_FORMING", live: true };
    state.forming.set(t, c);
    upsertBar(c);
    if (ev.price) livePrice(ev.price);
    if (ev.recv_ts) {  // trade receive -> browser update (same clock on the server test)
      const lat = window.sp2lLatency || (window.sp2lLatency = []);
      lat.push(Date.now() - Date.parse(ev.recv_ts));
      if (lat.length > 5000) lat.shift();
    }
    return;
  }
  // final / revised: the canonical candle replaces the live one (kept until stored data has it)
  const fin = ev.status === "OK" || ev.type === "revised"
    ? { open_time: ev.t, open: ev.o, high: ev.h, low: ev.l, close: ev.c, volume: ev.v,
      trade_count: ev.n, quality: ev.quality, revision: ev.revision }
    : { open_time: ev.t, missing: true, quality: "DATA_GAP" };
  state.forming.set(t, fin);
  upsertBar(fin);
}
function startLive() {
  if (!window.EventSource || state.live) return;
  const es = new EventSource("/api/live/stream");  // SP2L backend only, never Tabdeal
  state.live = es;
  const mark = (up) => { $("chart-live").hidden = !up; };
  es.addEventListener("open", () => mark(true));
  es.addEventListener("error", () => mark(false));
  es.addEventListener("snapshot", (e) => { mark(true); applyLive(JSON.parse(e.data)); });
  es.addEventListener("message", (e) => applyLive(JSON.parse(e.data)));
}
function showOhlc(param) {
  const box = $("chart-ohlc");
  const c = param && param.time !== undefined && state.byTime ? state.byTime.get(param.time) : null;
  if (!c) { box.textContent = ""; return; }
  box.replaceChildren(fmtClock.format(new Date(c.open_time)), " · ",
    c.missing ? "no candle (data gap)" : c.live ? `Live candle · O ${grp(c.open)}  H ${grp(c.high)}  L ${grp(c.low)}  C ${grp(c.close)}  V ${grp(c.volume)} · ${c.trade_count} trades so far` : `O ${grp(c.open)}  H ${grp(c.high)}  L ${grp(c.low)}  C ${grp(c.close)}  V ${grp(c.volume)} · ${c.trade_count === null ? "trade count unknown" : `${c.trade_count} trades`}`,
    " · ", el("span", { title: `code: ${c.quality}${c.revision ? ` · revision ${c.revision}` : ""}` }, QUALITY[c.quality] || c.quality), c.revision ? " · revised" : "");
}
const LINE_STYLE = { E1: ["--info", 0, "E1"], SL: ["--bad", 0, "Stop loss"], TP: ["--ok", 0, "Target"],
  E2: ["--info", 2, "E2"], E1_PREV: ["--text-3", 1, "Earlier E1"], ORIGIN: ["--text-2", 2, "Origin"] };
const MARKER_STYLE = { PGAP: ["--info", "P-Gap"], SPIKE: ["--info", "Spike"], PULLBACK: ["--warn", "Pullback"],
  FILL: ["--ok", "Fill"], EXIT: ["--text", "Exit"] };
function applyOverlays(ov) {
  if (!state.series) return;
  for (const p of state.priceLines) state.series.removePriceLine(p);
  state.priceLines = [];
  const legend = [];
  const seen = new Set();
  for (const l of (ov ? ov.lines : [])) {
    const [c, style, name] = LINE_STYLE[l.kind] || ["--text-2", 2, l.kind];
    state.priceLines.push(state.series.createPriceLine({ price: +l.price, color: css(c), lineWidth: l.kind === "E1_PREV" ? 1 : 2,
      lineStyle: style, axisLabelVisible: l.kind !== "E1_PREV", title: l.label }));
    if (!seen.has(l.kind)) { seen.add(l.kind); legend.push(el("li", {}, el("span", { class: `sw${style === 2 ? " dash" : style === 1 ? " dot" : ""}`, style: `--c:${css(c)}` }), name)); }
  }
  const have = new Set(state.candles.map((c) => secs(c.open_time)));
  const markers = (ov ? ov.markers : []).filter((m) => have.has(secs(m.time))).map((m) => {
    const [c] = MARKER_STYLE[m.kind] || ["--text-2"];
    return { time: secs(m.time), position: m.position === "above" ? "aboveBar" : "belowBar", color: css(c),
      shape: m.kind === "FILL" ? "circle" : m.position === "above" ? "arrowDown" : "arrowUp", text: m.label };
  });
  state.series.setMarkers(markers);
  for (const m of (ov ? ov.markers : [])) {
    if (!seen.has(m.kind)) { seen.add(m.kind); const [c, name] = MARKER_STYLE[m.kind] || ["--text-2", m.kind]; legend.push(el("li", {}, el("span", { class: "sw", style: `--c:${css(c)}` }), name)); }
  }
  $("chart-legend").replaceChildren(...legend);
}
function drawEquity(id, points) {
  const box = $(id);
  if (!box || !window.LightweightCharts) return;
  let c = state.equity[id];
  if (!c || c.box !== box) {
    const chart = LightweightCharts.createChart(box, { autoSize: true, ...chartTheme() });
    c = state.equity[id] = { box, chart, series: chart.addLineSeries({ color: css("--info"), lineWidth: 2 }) };
  }
  // one point per second for the chart library (display only); exact balances are in History
  const bySec = new Map();
  for (const p of points) if (p.ts !== "start") bySec.set(secs(p.ts), +p.balance);
  c.series.setData([...bySec.entries()].sort((a, b) => a[0] - b[0]).map(([time, value]) => ({ time, value })));
  c.chart.timeScale().fitContent();
}
/* ---- support / resistance zones -------------------------------------------------------------------
   Drawn exactly as /api/market/zones returns them (swing-pivot zones on 15m / 30m / 4h, computed in
   the backend; display only - no strategy code reads them). Resistance red, support green; the
   longer the timeframe, the stronger the fill. Each timeframe can be hidden (kept per viewer). */
const ZONE_ALPHA = { "15m": 0.12, "30m": 0.18, "4h": 0.26 };
function zonePrefs() {
  try { return JSON.parse(localStorage.getItem("sp2l-zones") || "{}"); } catch (_) { return {}; }
}
function zoneOn(tf) { const p = zonePrefs(); return p[tf] !== false; }
class ZoneLayer {
  constructor() { this.zones = []; this.chart = null; this.series = null; this.req = null; }
  attached(p) { this.chart = p.chart; this.series = p.series; this.req = p.requestUpdate; }
  detached() { this.chart = null; this.series = null; this.req = null; }
  set(zones) { this.zones = zones; if (this.req) this.req(); }
  updateAllViews() {}
  paneViews() {
    const layer = this;
    return [{ zOrder: () => "bottom", renderer: () => ({ draw: (target) => layer.draw(target) }) }];
  }
  draw(target) {
    if (!this.series || !this.chart) return;
    const ts = this.chart.timeScale();
    const res = css("--bad"), sup = css("--ok"), txt = css("--text-2");
    let drawn = 0;
    target.useBitmapCoordinateSpace(({ context: ctx, bitmapSize, horizontalPixelRatio: hr, verticalPixelRatio: vr }) => {
      for (const z of this.zones) {
        if (!zoneOn(z.timeframe)) continue;
        const yTop = this.series.priceToCoordinate(+z.top), yBot = this.series.priceToCoordinate(+z.bottom);
        if (yTop === null || yBot === null) continue;
        const x = ts.timeToCoordinate(secs(z.since));
        const x0 = x === null ? 0 : Math.round(x * hr);
        const y0 = Math.round(yTop * vr), h = Math.max(Math.round((yBot - yTop) * vr), 2 * vr);
        const color = z.kind === "RESISTANCE" ? res : sup;
        ctx.globalAlpha = ZONE_ALPHA[z.timeframe] || ZONE_ALPHA["15m"];
        ctx.fillStyle = color;
        ctx.fillRect(x0, y0, bitmapSize.width - x0, h);
        ctx.globalAlpha = 1;
        ctx.strokeStyle = color;
        ctx.lineWidth = Math.max(1, Math.round(vr));
        ctx.setLineDash(z.timeframe === "4h" ? [] : [4 * hr, 3 * hr]);
        ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(bitmapSize.width, y0);
        ctx.moveTo(x0, y0 + h); ctx.lineTo(bitmapSize.width, y0 + h); ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = txt;
        ctx.font = `${Math.round(11 * vr)}px IBM Plex Sans, sans-serif`;
        ctx.fillText(z.label, x0 + 6 * hr, y0 + Math.min(h, 12 * vr) - 2 * vr);
        drawn += 1;
      }
    });
    window.sp2lZonesDrawn = drawn;  // test hook
  }
}
function renderZones(d) {
  if (!state.series) return;
  if (!state.zoneLayer) { state.zoneLayer = new ZoneLayer(); state.series.attachPrimitive(state.zoneLayer); }
  state.zones = d.zones || [];
  state.zoneLayer.set(state.zones);
  const bar = $("zone-bar");
  const count = (tf) => state.zones.filter((z) => z.timeframe === tf).length;
  bar.replaceChildren(el("span", { title: d.note || "" }, "Support / resistance:"), ...(d.timeframes || []).map((tf) => {
    const b = el("button", { type: "button", "aria-pressed": String(zoneOn(tf)), title: `${tf} swing zones (${count(tf)} shown)` },
      el("span", { class: "zs", style: `--a:${ZONE_ALPHA[tf] * 3}` }), `${tf} · ${count(tf)}`);
    b.addEventListener("click", () => {
      const p = zonePrefs(); p[tf] = !zoneOn(tf);
      try { localStorage.setItem("sp2l-zones", JSON.stringify(p)); } catch (_) { /* storage unavailable */ }
      b.setAttribute("aria-pressed", String(zoneOn(tf)));
      state.zoneLayer.set(state.zones);
    });
    return b;
  }));
}
function restyleCharts() {
  if (state.chart) state.chart.applyOptions(chartTheme());
  for (const c of Object.values(state.equity)) c.chart.applyOptions(chartTheme());
}

/* ---- candidates view --------------------------------------------------------------------------- */
const PRIMARY = [[null, "All", "ALL"], ["ACTIVE", "Active", "ACTIVE"], ["TRADED", "Traded", "TRADED"], ["REJECTED", "Rejected", "REJECTED"]];
const MORE = { REJECTED_CONTEXT: "Rejected at Context", REJECTED_EXHAUSTION: "Rejected at Exhaustion",
  REJECTED_RISK: "Rejected at risk check", EXPIRED: "Expired", AMBIGUOUS_DATA_GAP: "Uncertain (data gap)", ERROR_HOLD: "On hold (error)",
  SESSION_ENDED: "Session ended" };
function candButton(c) {
  const b = el("button", { class: "cand", type: "button", "aria-current": String(state.selected === c.setup_key) },
    el("span", { class: "top" }, el("span", { class: "when" }, when(c.created_at)), sideTag(c.side), tag(c.result.tone, c.result.label, c.status)),
    el("span", { class: "stage" }, c.stage_label),
    c.reason_human ? el("span", { class: "why", title: `code: ${c.reason_human.code}` }, c.reason_human.label) : null);
  b.addEventListener("click", () => { location.hash = `#/candidates/${encodeURIComponent(c.setup_key)}`; });
  return b;
}
function renderCandidateList(d) {
  const counts = d.counts || {};
  $("primary-filters").replaceChildren(...PRIMARY.map(([key, name, ck]) => {
    const b = el("button", { type: "button", role: "tab", "aria-selected": String(state.bucket === key) }, name, el("b", {}, show(counts[ck])));
    b.addEventListener("click", () => { state.bucket = key; $("more-filters").value = ""; refreshFast(); });
    return b;
  }));
  const sel = $("more-filters");
  if (!sel.options.length) {
    sel.append(el("option", { value: "" }, "—"), ...Object.entries(MORE).map(([k, v]) => el("option", { value: k }, v)));
    sel.addEventListener("change", () => { state.bucket = sel.value || null; refreshFast(); });
  }
  for (const o of sel.options) if (o.value) o.textContent = `${MORE[o.value]} (${show(counts[o.value])})`;
  const list = $("cand-list");
  list.replaceChildren(...(d.items.length ? d.items.map((c) => el("li", {}, candButton(c)))
    : [el("li", { class: "empty" }, state.bucket ? "No candidates in this filter" : "No candidates detected yet")]));
}
function renderCandidateDetail(d) {
  $("detail-h").textContent = d ? "Candidate detail" : "Candidate detail";
  if (!d) {
    $("detail-body").replaceChildren(el("p", { class: "empty" }, "Select a candidate to see how it was evaluated."));
    $("detail-tech").replaceChildren();
    return;
  }
  $("detail-body").replaceChildren(...setupBody(d));
  technical(d);
}

/* ---- history / analytics / diagnostics ------------------------------------------------------- */
const LEDGER_KIND = { REALIZED_PNL: "Realized PnL", FEE: "Fee", INITIAL: "Starting balance", DEPOSIT: "Deposit" };
function renderHistory(o, outcomes) {
  const p = o.performance;
  tradesTable("trades-table", p && p.session ? p.recent_trades : [], p && p.session ? "No confirmed trades yet" : "Shadow has not started");
  table("outcomes-table", ["Detected", "Side", "Where", "Result", "Reason"], outcomes, (c) =>
    el("tr", {}, el("td", {}, when(c.created_at)), el("td", {}, sideTag(c.side)), el("td", {}, c.stage_label),
      el("td", {}, tag(c.result.tone, c.result.label, c.status)),
      el("td", { class: "wrap", title: c.reason_human ? `code: ${c.reason_human.code}` : null }, c.reason_human ? c.reason_human.label : "—")),
    "No expired, uncertain or held setups");
  table("ledger-table", ["Time", "Entry", "Amount (USDT)", "Balance (USDT)", "Setup"], o.wallet.ledger, (l) =>
    el("tr", {}, el("td", {}, when(l.ts)), el("td", { title: `code: ${l.kind}` }, LEDGER_KIND[l.kind] || show(l.kind)), el("td", { class: "num" }, grp(l.amount)),
      el("td", { class: "num" }, grp(l.balance_after)), el("td", { class: "mono small" }, show(l.setup_key))), "No Shadow session yet");
}
/* display names for recorded enum values (the code stays in the tooltip) */
const STAGE_NAME = { CONTEXT: "Context", EXHAUSTION: "Exhaustion", RISK: "Risk check", E1: "E1" };
const OUTCOME = { TP: "Would hit target", SL: "Would hit stop", EXPIRED_NO_FILL: "Would expire unfilled",
  EXPIRED_UNARMED: "Entry never placed", OPEN: "Still open", AMBIGUOUS: "Uncertain (data gap)", ERROR: "Error" };
function renderAnalytics(o, cf) {
  const p = o.performance;
  $("perf-kpis").replaceChildren(p && p.session ? kpis(p.stats, o.wallet) : el("p", { class: "empty" }, "Shadow has not started — no confirmed results yet."));
  if (p && p.session) drawEquity("equity-chart", p.stats.equity);
  table("ambiguous-table", ["Detected", "Side", "Marked uncertain", "Reason"], p && p.session ? p.ambiguous : [], (a) =>
    el("tr", {}, el("td", {}, when(a.created_at)), el("td", {}, sideTag(a.side)), el("td", {}, when(a.status_ts)),
      el("td", { class: "wrap" }, "Result uncertain due to missing market data ", codeEl("AMBIGUOUS_DATA_GAP"))), "No uncertain results");
  table("cf-table", ["Setup", "Rejected at", "Hypothetical outcome", "Result (R, gross)", "E2 filled"], cf.items, (c) =>
    el("tr", {}, el("td", { class: "mono small" }, c.setup_key), el("td", { title: `code: ${c.rejection_stage}` }, STAGE_NAME[c.rejection_stage] || show(c.rejection_stage)),
      el("td", {}, tag("neutral", OUTCOME[c.outcome] || show(c.outcome), c.outcome)),
      el("td", { class: "num", title: c.result_r ? `exact: ${c.result_r}` : null }, show(c.result_r_display)), el("td", {}, c.e2_filled ? "Yes" : "No")),
    "No counterfactual results yet");
}
function renderSources(col, ig) {
  const d = (col.heartbeat && col.heartbeat.detail) || {};
  const c = d.connections || {};
  const r = d.rest || {};
  const s = (x) => (x === null || x === undefined ? "—" : `${x} s`);
  kv("diag-sources", [
    ["WS A received", grp(c.A ? c.A.received : null), "num"], ["WS B received", grp(c.B ? c.B.received : null), "num"],
    ["REST polls · errors", `${show(d.rest_polls)} · ${show(d.rest_errors)}`, "num"],
    ["REST trades received", grp(d.rest_trades), "num"],
    ["REST-only trades (WS never sent)", grp(d.rest_only), "num"],
    ["WS-only trades (REST covered, never listed)", grp(d.ws_only), "num"],
    ["Reconciliation", r.enabled ? tag(r.healthy ? "ok" : "warn", r.healthy ? "Running" : "REST unavailable") : tag("neutral", "Off")],
    ["Poll interval", s(r.poll_interval_s), "num"],
    ["REST window (50 trades) shortest / typical", `${s(r.window_span_s_min)} / ${s(r.window_span_s_p50)}`, "num"],
    ["Maximum recoverable gap", s(r.max_recoverable_gap_s), "num"],
    ["Candle finalization delay p50 / max", `${s(r.reconcile_latency_s_p50)} / ${s(r.reconcile_latency_s_max)}`, "num"],
    ["Gaps repaired · unrecovered (run)", `${show(d.gaps_repaired)} · ${show(d.gaps_unrecovered)}`, "num"],
    ["Candle revisions (all)", show(ig.revision_total), "num"],
    ["Latest canonical minute", when(ig.latest_canonical_minute)],
  ]);
}
const STAGE_LABEL = { ws_proof: "WebSocket coverage proof", rest_cover: "REST window proves the minute",
  rest_request: "REST request (duration)", match: "Matching / dedup (duration)", finalize: "Canonical M1 final",
  persist: "Persistence (duration)", strategy_eval: "Final → strategy evaluated", e1_decision: "Final → E1 decision" };
function renderLatency(ig) {
  table("diag-latency", ["Stage", "p50", "p90", "p99", "n"], ig.latency_1h || [], (r) =>
    el("tr", {}, el("td", { title: `code: ${r.stage}` }, STAGE_LABEL[r.stage] || r.stage),
      ...[r.p50, r.p90, r.p99, r.n].map((v) => el("td", { class: "num" }, grp(v)))), "No latency samples yet");
}
function renderPgaps(p) {
  table("diag-pgaps", ["Impulse", "Side", "Body / candle", "Gap / body", "Gap", "Result"], p.items, (r) => {
    const v = r.view, rows = v.rows || [];
    const val = (i) => (rows[i] ? el("span", { class: rows[i].ok ? "" : "pq-bad" }, rows[i].value) : "—");
    return el("tr", {}, el("td", {}, when(r.impulse_time)), el("td", {}, sideTag(r.side)),
      el("td", { class: "num" }, val(1)), el("td", { class: "num" }, val(2)), el("td", { class: "num" }, val(3)),
      el("td", { class: "wrap" }, tag(r.outcome.tone, r.outcome.label, r.outcome.code || "PROMOTED"),
        v.measured && !v.valid ? el("div", { class: "muted small" }, v.result) : null));
  }, "No P-Gap detected yet");
}
function renderIntegrity(ig) {
  table("diag-repairs", ["Gap", "Duration", "Outcome", "Method", "Recovered trades", "Why"], ig.repairs, (r) =>
    el("tr", {}, el("td", {}, when(r.gap_start), " → ", when(r.gap_end)),
      el("td", { class: "num" }, `${Math.round((Date.parse(r.gap_end) - Date.parse(r.gap_start)) / 1000)} s`),
      el("td", {}, tag(r.status === "REPAIRED" ? "ok" : "warn", r.status === "REPAIRED" ? "Repaired" : "Unrecovered", r.status)),
      el("td", {}, codeEl(r.method)), el("td", { class: "num" }, show(r.trades_recovered)),
      el("td", { class: "wrap" }, codeEl(r.failure || r.reason))), "No gap has needed repair yet");
  table("diag-revisions", ["Candle", "Rev", "Quality", "Changed", "Reason"], ig.revisions, (r) => {
    const ch = ["high", "low", "close", "volume", "trade_count"].filter((k) => String(r.old[k]) !== String(r.new[k]));
    return el("tr", {}, el("td", {}, `${r.timeframe} · `, when(r.open_time)), el("td", { class: "num" }, r.revision),
      el("td", {}, `${QUALITY[r.old_quality] || r.old_quality} → ${QUALITY[r.new_quality] || r.new_quality}`),
      el("td", { class: "wrap num", title: JSON.stringify({ old: r.old, new: r.new }) }, ch.length ? ch.map((k) => `${k} ${grp(r.old[k])}→${grp(r.new[k])}`).join(" · ") : "lineage only"),
      el("td", { class: "wrap" }, codeEl(r.reason)));
  }, "No canonical candle has been revised");
  table("diag-conflicts", ["Run", "Classification", "Rows", "Sequences"], ig.conflict_audit, (r) =>
    el("tr", {}, el("td", { class: "num" }, show(r.run_id)),
      el("td", {}, tag(r.classification === "TIMESTAMP_CONFLICT" ? "warn" : "neutral",
        r.classification === "TIMESTAMP_CONFLICT" ? "Genuine conflict" : "Multi-fill (legitimate)", r.classification)),
      el("td", { class: "num" }, r.n), el("td", { class: "num" }, r.sequences)), "No same-sequence payload differences");
  kv("diag-lineage", ig.quality_24h.map((r) => [QUALITY[r.quality] || r.quality, show(r.n), "num"]));
  const ct = ig.continuity || {};
  const br = ct.last_break, cause = ct.last_break_cause || {}, lr = ct.last_repair;
  kv("diag-continuity", [
    ["Context", ct.price_context_ready && ct.liquidity_context_ready ? tag("ok", "Context ready")
      : ct.price_context_ready ? tag("info", "Price ready · Liquidity unknown") : tag("neutral", "Warming up")],
    ["Why", el("span", { class: "wrap" }, show(ct.ready_reason))],
    ["Trusted M5 bars", `${show(ct.trusted_m5)} / ${show(ct.warmup_target)}`, "num"],
    ["Last continuity break", br ? el("span", {}, when(br.from), " → ", when(br.until)) : "None in stored history"],
    ["Break cause", br ? codeEl(cause.failure || "UNKNOWN") : "—"],
    ["Last repair source", lr ? show(lr.source) : "—"],
    ["Repair type", lr ? el("span", { title: `code: ${lr.repair_type}` }, REPAIR_TYPE[lr.repair_type] || show(lr.repair_type)) : "—"],
    ["Repaired interval", lr ? el("span", {}, when(lr.interval[0]), " → ", when(lr.interval[1])) : "—"],
    ["Remaining unknown fields", (ct.unknown_fields || []).length ? codeEl(ct.unknown_fields.join(", ")) : "None"],
    ["Liquidity valid in", ct.liquidity_bars_missing ? `${ct.liquidity_bars_missing} M5 bars` : "Now", "num"]]);
}
function renderDiagnostics(o, feed, col, q, v) {
  const hb = col.heartbeat || {};
  const run = col.run || {};
  kv("diag-collector", [["Status", codeEl(col.status)], ["Last heartbeat", ago(col.heartbeat_age_s)],
    ["Coverage lag", hb.coverage_lag_ms === undefined || hb.coverage_lag_ms === null ? "—" : `${hb.coverage_lag_ms} ms`, "num"],
    ["Trades (this run)", grp(hb.trades_total), "num"], ["Late trades", grp(hb.late_total), "num"],
    ["Reconnects (this run)", grp(hb.reconnects), "num"], ["Run", show(run.id), "num"], ["PID", show(run.pid), "num"],
    ["Host", show(run.host)], ["Run started", when(run.started_at)]]);
  const lev = (o.system && o.system.leverage) || {};
  kv("diag-leverage", [["Strategy leverage", `${show(lev.strategy)}x`, "num"],
    ["Exchange leverage", lev.exchange === null || lev.exchange === undefined ? "Not read yet" : `${lev.exchange}x`, "num"],
    ["Live", lev.live_blocker ? tag("bad", "Blocked — exchange leverage must be 10x", lev.live_blocker) : tag("ok", "Leverage matches")],
    ["Checked", when(lev.checked_at)], ["Automatic leverage change", "Never (read-only)"],
    ["Summary", el("span", { class: "wrap" }, show(lev.text))]]);
  const d = feed.dedup || {};
  const live = (feed.live && feed.live.detail) || {};
  kv("diag-merged", [["Merged status (now)", codeEl(live.merged_status)],
    ["Uncovered time", `${show(feed.merged_uncovered_seconds)} s`, "num"],
    ["Data-gap minutes", show(feed.merged_data_gap_minutes), "num"],
    ["Duplicate rate", pct(d.duplicate_rate), "num"],
    ["Payload conflicts (24 h)", feed.conflicts ? tag("warn", String(feed.conflicts)) : "0"],
    ["Payload conflicts (current run)", live.conflicts ? tag("warn", String(live.conflicts)) : show(live.conflicts ?? 0)],
    ["Multi-fill sequences (legitimate)", show(feed.multi_fill_sequences), "num"],
    ["Orphan trades", show(d.orphans), "num"],
    ["Longest continuous M5 segment", feed.m5.longest ? `${feed.m5.longest.bars} bars` : "—", "num"],
    ["Current M5 segment", `${feed.m5.current_bars} / ${feed.m5.warmup_target} bars`, "num"],
    ["Warmup target ever reached", feed.m5.target_reached ? "Yes" : "No"]]);
  const conns = live.connections || {};
  const names = Array.from(new Set([...Object.keys(conns), ...Object.keys(feed.connections)])).sort();
  table("diag-conn", ["Connection", "Now", "Coverage lag", "Sessions", "Disconnects", "Last close", "Close reason"], names, (n) => {
    const c = conns[n] || {};
    const h = feed.connections[n] || { disconnects: 0, closes: [] };
    const last = h.closes[h.closes.length - 1] || {};
    return el("tr", {}, el("td", {}, n), el("td", {}, tag(c.connected && c.confirmed ? "ok" : "warn", c.connected && c.confirmed ? "Connected" : "Reconnecting")),
      el("td", { class: "num" }, c.coverage_lag_ms === undefined || c.coverage_lag_ms === null ? "—" : `${c.coverage_lag_ms} ms`), el("td", { class: "num" }, show(c.sessions)),
      el("td", { class: "num" }, show(h.disconnects)), el("td", {}, when(last.ts)), el("td", { class: "wrap" }, codeEl(last.reason)));
  }, "No connection data yet");
  $("corr-win").textContent = String(feed.correlation_window_s);
  table("diag-corr", ["First close", "Second close", "Apart"], feed.correlated_closes, (c) =>
    el("tr", {}, el("td", {}, `${c.a.conn} · `, when(c.a.ts)), el("td", {}, `${c.b.conn} · `, when(c.b.ts)), el("td", { class: "num" }, `${c.delta_s} s`)),
    "No correlated closes");
  table("diag-gaps", ["Start", "End", "Duration", "Reason"], [...feed.merged_gaps].reverse(), (g) =>
    el("tr", {}, el("td", {}, when(g.start)), el("td", {}, when(g.end)), el("td", { class: "num" }, `${g.seconds} s`), el("td", {}, codeEl(g.reason))),
    "No merged coverage gaps");
  $("dq-strip").replaceChildren(...q.timeline.map((m) => el("span", {
    class: `dq ${{ OK: "ok", SYNTHETIC: "syn", DATA_GAP: "gap" }[m.status]}`,
    title: `${new Date(m.minute).toLocaleTimeString()} · ${m.status}${m.trades !== null ? ` · ${m.trades} trades` : ""}` })));
  table("diag-dq-gaps", ["Start", "End", "Reason", "Scope"], q.gaps, (g) =>
    el("tr", {}, el("td", {}, when(g.gap_start)), el("td", {}, when(g.gap_end)), el("td", {}, codeEl(g.reason)), el("td", {}, show(g.timeframe))),
    "No gaps recorded");
  const dg = col.diagnostics;
  const items = [...dg.host_sleeps.map((g) => ({ ...g, kind: "Host sleep" })), ...dg.long_gaps.map((g) => ({ ...g, kind: "Long coverage gap" }))]
    .sort((a, b) => String(b.gap_start).localeCompare(String(a.gap_start)));
  table("diag-sleep", ["Event", "Start", "End", "Duration", "Reason"], items, (g) =>
    el("tr", {}, el("td", {}, g.kind), el("td", {}, when(g.gap_start)), el("td", {}, when(g.gap_end)),
      el("td", { class: "num" }, `${show(g.seconds)} s`), el("td", {}, codeEl(g.reason))),
    `No host sleep or coverage gap of ${dg.long_gap_threshold_s} s or longer`);
  table("diag-runs", ["Run", "PID", "Mode", "Started", "Ended", "Exit"], col.runs, (r) =>
    el("tr", {}, el("td", { class: "num" }, r.id), el("td", { class: "num" }, r.pid), el("td", {}, r.mode),
      el("td", {}, when(r.started_at)), el("td", {}, when(r.ended_at)),
      el("td", {}, tag(r.run_status === "UNCLEAN_EXIT" ? "bad" : r.run_status === "RUNNING" ? "info" : "neutral",
        { RUNNING: "Running", CLEAN_EXIT: "Clean exit", UNCLEAN_EXIT: "Unclean exit" }[r.run_status] || r.run_status, r.exit_reason || r.run_status))),
    "No runs");
  table("diag-validation", ["Check", "Result", "Last run", "Notes"], v.items, (x) =>
    el("tr", {}, el("td", {}, codeEl(x.item)), el("td", {}, x.passed === null ? tag("neutral", "Not run") : x.passed ? tag("ok", "Passed") : tag("bad", "Failed")),
      el("td", {}, when(x.finished_at)), el("td", { class: "wrap" }, show(x.notes))), "No validation runs");
  kv("diag-spec", [["Spec version", show(o.spec.version)], ["Rules sha256", el("code", { class: "small" }, o.spec.rules_sha256)],
    ["Manifest", o.spec.manifest_ok ? tag("ok", "Verified") : tag("bad", "Mismatch")], ["Real trading", codeEl(o.live.status)],
    ["Failing validation items", show((o.live.failing_items || []).length), "num"]]);
}

/* ---- header ------------------------------------------------------------------------------------ */
function renderHeader(o) {
  $("symbol").textContent = o.symbol_display;
  const disabled = o.live.status === "LIVE_AUTOMATION_DISABLED";
  $("live-pill").replaceChildren(icon(disabled ? "lock" : "alert"), el("span", { class: "txt" }, disabled ? "Real trading disabled" : "Review real-trading status"));
  $("live-pill").title = `code: ${o.live.status}`;
  $("foot-spec").textContent = `SP2L spec ${o.spec.version} · rules ${o.spec.rules_sha256.slice(0, 12)}`;
}

/* ---- indicators (backend values rendered as-is; values change once per M5 close) ---------------- */
function spark(values) {
  // plot only: maps the backend's history to screen coordinates
  const pts = values.map((v, i) => [i, v === null || v === undefined ? null : Number(v)]).filter((p) => p[1] !== null);
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "spark"); svg.setAttribute("viewBox", "0 0 100 30");
  svg.setAttribute("preserveAspectRatio", "none"); svg.setAttribute("aria-hidden", "true");
  if (pts.length > 1) {
    const ys = pts.map((p) => p[1]); const lo = Math.min(...ys), hi = Math.max(...ys);
    const n = Math.max(values.length - 1, 1);
    const line = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
    line.setAttribute("points", pts.map(([i, y]) => `${(i * 100) / n},${hi === lo ? 15 : 28 - ((y - lo) * 26) / (hi - lo)}`).join(" "));
    svg.append(line);
  }
  return svg;
}
function renderIndicators(d) {
  state.indNext = d.available ? Date.parse(d.next_bar_close) : null;
  tickCountdown();
  if (!d.available) { $("ind-bar").replaceChildren(el("b", {}, d.text)); $("ind-grid").replaceChildren(); $("ind-states").replaceChildren(); return; }
  const r = d.readiness;
  $("ind-bar").replaceChildren(el("div", {}, el("span", { class: "muted small" }, "Latest finalized M5 bar "),
    el("b", {}, when(d.bar_open), " – ", fmtClock.format(new Date(d.bar_close)))),
    el("div", { class: "small" }, `Last price ${show(d.last_price)} · `,
      r.price ? tag("ok", "Price context ready") : tag("neutral", `Warmup ${r.segment_bars} / ${r.warmup_target}`), " ",
      r.liquidity ? tag("ok", "Liquidity ready") : tag("info", `Liquidity unknown · ${r.liquidity_bars_missing} bars`)));
  $("ind-note").textContent = d.note;
  $("ind-states").replaceChildren(...d.states.map((x) => el("div", { class: `status-tile t-${x.changed ? "info" : "neutral"}` },
    el("div", { class: "k" }, x.label),
    el("div", { class: "v", title: `code: ${x.value}` }, codeEl(x.value)),
    el("div", { class: "d" }, x.changed ? `changed from ${x.previous}` : "unchanged since the previous bar",
      x.key === "regime" ? ` · ${d.regime_rule}` : x.key === "trend" && d.trend_age_bars ? ` · ${d.trend_age_bars} bars` : ""))));
  const prevBar = state.indBar;
  state.indBar = d.bar_open;
  $("ind-grid").replaceChildren(...d.items.map((x) => {
    const tile = el("div", { class: `ind-tile${prevBar && prevBar !== d.bar_open && x.change && x.change !== "same" ? " flash" : ""}`, title: `exact: ${x.exact ?? "—"}` },
      el("div", { class: "k" }, x.label),
      el("div", { class: "v" }, x.display),
      el("div", { class: `d ${x.change || "same"}` }, x.change === "up" || x.change === "down" ? icon(x.change) : null,
        x.delta_display ? x.delta_display : x.change === "same" ? "no change" : "—",
        x.previous_display ? el("span", { class: "muted" }, ` · was ${x.previous_display}`) : null),
      spark(x.history),
      el("div", { class: "m" }, x.meaning));
    if (tile.classList.contains("flash")) setTimeout(() => tile.classList.remove("flash"), 2500);
    return tile;
  }));
  const keys = ["atr14", "adx14", "chop14", "ema20", "range_position_close", "volume_ratio"];
  const byKey = Object.fromEntries(d.items.map((x) => [x.key, x]));
  const n = d.history_times.length;
  const idx = Array.from({ length: Math.min(n, 12) }, (_, i) => n - 1 - i);
  table("ind-history", ["Bar", ...keys.map((k) => byKey[k].label)], idx, (i) =>
    el("tr", {}, el("td", {}, when(d.history_times[i])),
      ...keys.map((k) => el("td", { class: "num" }, byKey[k].history_display[i]))), "No bars yet");
}
function tickCountdown() {
  const box = $("ind-countdown");
  if (!box) return;
  if (!state.indNext) { box.textContent = "—"; return; }
  const s = Math.max(0, Math.round((state.indNext - Date.now()) / 1000));
  box.textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/* ---- refresh ------------------------------------------------------------------------------------ */
async function refreshFast() {
  try {
    const o = await api("/api/overview");
    state.overview = o;
    renderHeader(o);
    const v = state.view;
    if (v === "dashboard") {
      renderStatus(o.system); renderMarketData(o.system); renderChartHead(o);
      const list = await api("/api/setups?limit=1");
      const active = o.active[0];
      const d = active ? await api(`/api/setups/${encodeURIComponent(active.setup_key)}`) : null;
      renderStrategySummary(o, list); renderDashboardSetup(d); renderShadow(o);
      renderPosition(d ? d.position : null);
      applyOverlays(d ? d.overlays : null);
    } else if (v === "candidates") {
      const q = state.bucket ? `?bucket=${encodeURIComponent(state.bucket)}` : "";
      const list = await api(`/api/setups${q}`);
      if (!state.selected && list.items.length) state.selected = list.items[0].setup_key;
      renderCandidateList(list);
      const d = state.selected ? await api(`/api/setups/${encodeURIComponent(state.selected)}`).catch(() => null) : null;
      renderCandidateDetail(d);
      applyOverlays(d ? d.overlays : null);
    } else if (v === "indicators") {
      renderIndicators(await api("/api/indicators?limit=48"));
    } else if (v === "history") {
      const parts = await Promise.all(["EXPIRED", "AMBIGUOUS_DATA_GAP", "ERROR_HOLD"].map((b) => api(`/api/setups?bucket=${b}`)));
      renderHistory(o, parts.flatMap((p) => p.items).sort((a, b) => String(b.created_at).localeCompare(String(a.created_at))));
    }
  } catch (e) { console.error(e); }
}
async function refreshSlow() {
  try {
    const v = state.view;
    if (v === "dashboard" || v === "candidates") {
      renderCandles(await api("/api/market/candles?tf=1m&limit=240"));
      renderZones(await api("/api/market/zones"));
    }
    if (v === "analytics") { const [o, cf] = await Promise.all([api("/api/overview"), api("/api/counterfactuals?limit=100")]); renderAnalytics(o, cf); }
    if (v === "diagnostics") {
      const [o, feed, col, q, val, ig] = await Promise.all([api("/api/overview"), api("/api/feed?hours=24"), api("/api/collector"),
        api("/api/market/quality?minutes=180"), api("/api/validation"), api("/api/integrity")]);
      renderPgaps(await api("/api/pgaps?limit=50"));
      renderDiagnostics(o, feed, col, q, val);
      renderIntegrity(ig);
      renderSources(col, ig);
      renderLatency(ig);
    }
  } catch (e) { console.error(e); }
}

function setTheme(t) {
  document.documentElement.dataset.theme = t;
  $("theme-btn").replaceChildren(icon(t === "dark" ? "sun" : "moon"));
  try { localStorage.setItem("sp2l-theme", t); } catch (_) { /* storage unavailable */ }
  restyleCharts();
}
function init() {
  let t = "dark";
  try { t = localStorage.getItem("sp2l-theme") || "dark"; } catch (_) { /* default */ }
  setTheme(t);
  $("tz-label").textContent = `Times shown in ${tzText()}`;
  for (const a of $("nav").querySelectorAll("a")) a.prepend(icon(a.dataset.view));
  $("theme-btn").addEventListener("click", () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));
  const pause = $("pause-btn");
  const setPause = (p) => { state.paused = p; pause.setAttribute("aria-pressed", String(p));
    pause.setAttribute("aria-label", p ? "Resume auto-refresh" : "Pause auto-refresh"); pause.replaceChildren(icon(p ? "play" : "pause")); };
  pause.addEventListener("click", () => setPause(!state.paused));
  setPause(false);
  if (/^#setup=/.test(location.hash)) location.hash = `#/candidates/${location.hash.slice(7)}`;  // old deep links
  window.addEventListener("hashchange", route);
  route();
  startLive();
  setInterval(() => { if (!state.paused) refreshFast(); }, 5000);
  setInterval(() => { if (!state.paused && state.view === "indicators") refreshFast(); }, 2000);
  setInterval(tickCountdown, 1000);
  setInterval(() => { if (!state.paused) refreshSlow(); }, 15000);
}
document.addEventListener("DOMContentLoaded", init);
