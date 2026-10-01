/* Page-level horizontal overflow, card overlap, content overflow and collapsed cards. */
() => {
  const vis = (e) => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const doc = document.scrollingElement;
  const out = { pageOverflow: doc.scrollWidth - window.innerWidth, overlaps: [], overflows: [], narrow: [] };
  const boxes = [...document.querySelectorAll('.card, .status-tile, .kpi, .blocker')].filter(vis);
  for (let i = 0; i < boxes.length; i++) for (let j = i + 1; j < boxes.length; j++) {
    const a = boxes[i], b = boxes[j];
    if (a.contains(b) || b.contains(a)) continue;
    const r = a.getBoundingClientRect(), q = b.getBoundingClientRect();
    const w = Math.min(r.right, q.right) - Math.max(r.left, q.left);
    const h = Math.min(r.bottom, q.bottom) - Math.max(r.top, q.top);
    if (w > 1 && h > 1) out.overlaps.push([a.id || a.className, b.id || b.className, w, h]);
  }
  for (const card of boxes) {
    const cr = card.getBoundingClientRect();
    if (cr.width < 120) out.narrow.push([card.id || card.className, cr.width]);
    for (const e of card.querySelectorAll('*')) {
      if (!vis(e) || e.closest('.table-wrap, .chart, .equity, .cand-list, svg, details:not([open])')) continue;
      const r = e.getBoundingClientRect();
      if (r.right > cr.right + 1 || r.left < cr.left - 1)
        out.overflows.push([card.id || card.className, e.tagName + '.' + e.className, Math.round(r.left), Math.round(r.right), Math.round(cr.right)]);
    }
  }
  return out;
}
