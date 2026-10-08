/* Chart workspace — in-page dialogs (confirm, notice, any content).
 *
 * The browser's own confirm() / alert() and <dialog> are not shown over a full-screen element in
 * every browser (Safari), so the workspace never uses them: these overlays are placed inside the
 * workspace root, which is the full-screen element when the chart is full screen.
 *
 *   const ok = await ChartUI.confirm(root, "Remove every drawing?", { ok: "Remove", danger: true });
 *   await ChartUI.notice(root, "Done.");
 *   const m = ChartUI.open(root, contentElement, { label, onClose }); m.close(); */
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

  /** Show `content` in a modal box inside `root`; Esc or a click outside closes it. */
  function open(root, content, opts) {
    const o = opts || {};
    const box = h("div", { class: `ws-dialog ${o.cls || ""}`, role: "dialog", "aria-modal": "true", "aria-label": o.label || "Dialog" }, content);
    const back = h("div", { class: "ws-modal" }, box);
    let closed = false;
    const close = () => {
      if (closed) return;
      closed = true;
      back.remove();
      document.removeEventListener("keydown", onKey, true);
      if (o.onClose) o.onClose();
    };
    const onKey = (e) => {
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(); }
      else if (o.onEnter && e.key === "Enter" && !/^(TEXTAREA|BUTTON)$/.test((e.target || {}).tagName || "")) { e.preventDefault(); o.onEnter(); }
    };
    back.addEventListener("mousedown", (e) => { if (e.target === back) close(); });
    document.addEventListener("keydown", onKey, true);
    (root || document.body).append(back);
    const first = box.querySelector("[data-autofocus]") || box.querySelector("input, select, button");
    if (first) setTimeout(() => first.focus(), 0);
    return { el: box, close };
  }

  /** A yes / no question; resolves true for OK. */
  function confirm(root, message, opts) {
    const o = opts || {};
    return new Promise((resolve) => {
      let answer = false;
      const ok = h("button", { class: `btn ${o.danger ? "ws-danger" : "ws-primary"}`, type: "button", "data-autofocus": true, onclick: () => { answer = true; m.close(); } }, o.ok || "OK");
      const body = h("div", {},
        o.title ? h("h2", {}, o.title) : null,
        h("p", { class: "ws-dlg-text" }, message),
        h("div", { class: "ws-dlg-actions" }, h("button", { class: "btn", type: "button", onclick: () => m.close() }, o.cancel || "Cancel"), ok));
      const m = open(root, body, { label: o.title || "Confirm", cls: "ws-confirm", onClose: () => resolve(answer), onEnter: () => { answer = true; m.close(); } });
    });
  }

  /** A message with one button. */
  function notice(root, message, opts) {
    const o = opts || {};
    return new Promise((resolve) => {
      const body = h("div", {},
        o.title ? h("h2", {}, o.title) : null,
        h("p", { class: `ws-dlg-text ${o.error ? "neg" : ""}` }, message),
        h("div", { class: "ws-dlg-actions" }, h("button", { class: "btn ws-primary", type: "button", "data-autofocus": true, onclick: () => m.close() }, "OK")));
      const m = open(root, body, { label: o.title || "Notice", cls: "ws-confirm", onClose: resolve, onEnter: () => m.close() });
    });
  }

  window.ChartUI = { open, confirm, notice, h };
})();
