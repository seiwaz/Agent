/* Chart workspace — drawing layer (a Lightweight Charts v5 series primitive).
 * Draws what the features hand it, in time / price coordinates:
 *   boxes  { t1, t2, top, bottom, fill, stroke, dash, label, labelColor }   (behind the candles)
 *   lines  { t1, p1, t2, p2, color, width, dash, label }                   (above)
 *   marks  { t, p, text, color, pos: "above" | "below" }                   (above)
 * A drawing spans exactly [t1, t2]: nothing is extended past its own end or the last bar. */
"use strict";
(function () {
  class OverlayLayer {
    constructor() { this.items = { boxes: [], lines: [], marks: [] }; this.times = []; this._view = new View(this); }
    attached(p) { this.chart = p.chart; this.series = p.series; this.request = p.requestUpdate; }
    detached() { this.chart = this.series = this.request = null; }
    set(items, times) { this.items = items; this.times = times; if (this.request) this.request(); }
    updateAllViews() {}
    paneViews() { return [this._view]; }
    /** Bar time (s) -> x, interpolated between bars; before the first bar extrapolated. */
    x(t) {
      const ts = this.times, n = ts.length;
      if (!n || !this.chart) return null;
      let logical;
      if (t <= ts[0]) logical = n > 1 ? (t - ts[0]) / (ts[1] - ts[0]) : 0;
      else if (t >= ts[n - 1]) logical = n - 1;
      else {
        let lo = 0, hi = n - 1;
        while (hi - lo > 1) { const m = (lo + hi) >> 1; if (ts[m] <= t) lo = m; else hi = m; }
        logical = lo + (t - ts[lo]) / (ts[hi] - ts[lo]);
      }
      return this.chart.timeScale().logicalToCoordinate(logical);
    }
    y(p) { return this.series ? this.series.priceToCoordinate(p) : null; }
  }
  class View {
    constructor(layer) { this.layer = layer; }
    zOrder() { return "normal"; }
    renderer() { return new Renderer(this.layer); }
  }
  const FONT = '500 11px "IBM Plex Sans", sans-serif';
  class Renderer {
    constructor(layer) { this.l = layer; }
    drawBackground(target) {
      target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
        for (const b of this.l.items.boxes) {
          const x1 = this.l.x(b.t1), x2 = this.l.x(b.t2), y1 = this.l.y(b.top), y2 = this.l.y(b.bottom);
          if ([x1, x2, y1, y2].some((v) => v === null) || x2 < 0 || x1 > mediaSize.width) continue;
          const w = Math.max(2, x2 - x1), h = Math.max(1, y2 - y1);
          ctx.fillStyle = b.fill; ctx.fillRect(x1, y1, w, h);
          if (b.stroke) { ctx.strokeStyle = b.stroke; ctx.lineWidth = 1; ctx.setLineDash(b.dash ? [4, 3] : []); ctx.strokeRect(x1 + 0.5, y1 + 0.5, w - 1, h - 1); ctx.setLineDash([]); }
        }
      });
    }
    draw(target) {
      target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
        ctx.font = FONT; ctx.textBaseline = "middle";
        for (const b of this.l.items.boxes) {  // box labels above the candles
          if (!b.label) continue;
          const x1 = this.l.x(b.t1), y1 = this.l.y(b.top), x2 = this.l.x(b.t2);
          if (x1 === null || y1 === null || x2 === null || x2 < 0 || x1 > mediaSize.width) continue;
          ctx.fillStyle = b.labelColor || b.stroke; ctx.textAlign = "left";
          ctx.fillText(b.label, Math.max(2, x1) + 4, y1 + 8);
        }
        for (const ln of this.l.items.lines) {
          const x1 = this.l.x(ln.t1), x2 = this.l.x(ln.t2), y1 = this.l.y(ln.p1), y2 = this.l.y(ln.p2);
          if ([x1, x2, y1, y2].some((v) => v === null)) continue;
          ctx.strokeStyle = ln.color; ctx.lineWidth = ln.width || 1.5; ctx.setLineDash(ln.dash ? [5, 4] : []);
          ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke(); ctx.setLineDash([]);
          if (ln.label) { ctx.fillStyle = ln.color; ctx.textAlign = "center"; ctx.fillText(ln.label, (x1 + x2) / 2, Math.min(y1, y2) - 8); }
        }
        for (const m of this.l.items.marks) {
          const x = this.l.x(m.t), y = this.l.y(m.p);
          if (x === null || y === null) continue;
          ctx.fillStyle = m.color; ctx.textAlign = "center";
          ctx.fillText(m.text, x, m.pos === "above" ? y - 10 : y + 11);
        }
      });
    }
  }
  window.ChartDraw = { OverlayLayer };
})();
