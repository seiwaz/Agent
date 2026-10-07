# Chart workspace

The dashboard's **Chart** tab: price, market structure, levels and indicators for one market on
5m / 15m / 1h / 4h / 1d. Display only: it opens no position and sends no order.

## Data: Tabdeal's chart

Candles come from Tabdeal's own chart feed (`special-margin/plots/history`), so the chart matches
the one on tabdeal.org bar for bar: 5m / 15m on UTC boundaries, 1h / 4h / 1d on Tehran boundaries
(1h and 4h open at hh:30 UTC, 1d at 20:30 UTC). The chart opens on the newest 500 bars and polls
the last bars every 10 s (the last bar is the forming one).

**Scrolling back** past the left edge loads the previous 500 bars, page after page, down to the
market's listing on Tabdeal; the overlays (S/R, trendlines, OB, FVG, structure) are recomputed
over everything loaded. Closed bars are cached in the `chart_bars` table, so a page already seen
is read from the database and not fetched again; the forming bar is never cached. If Tabdeal is
unreachable the API answers 502 and the chart keeps what it has.

## What it shows

Every item has its own switch in **Indicators & layers**, and its settings (⚙). Switches and
settings are kept in the browser (`localStorage`). Changing the timeframe re-applies every active
item to the new timeframe.

| group | item | computed by | drawn |
|---|---|---|---|
| Levels | Support / resistance | server, from confirmed swing pivots within *zone width × ATR* (≥ *minimum touches*) | from the first pivot to the first close through the zone; to the last bar while unbroken, never further |
| Levels | Trendlines | server, two rising swing lows / falling swing highs with no close beyond the line between them | from the first pivot to the first close beyond the line, or to the last bar |
| Structure | OB, FVG | server (`sp2l.smc.structure`) | from the zone's candle to its mitigation / fill, or to the last bar; active zones only unless *Also mitigated / filled* |
| Structure | BOS, CHoCH | server | from the broken swing to the closing break |
| Structure | HH / HL / LH / LL | server | each swing against the previous swing of its kind |
| Indicators | Donchian channels | browser | on the price pane |
| Indicators | RSI, MACD | browser | each in its own pane |

The structure pivot length (⚙ next to *Structure*) applies to every structure and level item.
Server items use closed bars only (a pivot is known *pivot length* bars after it); the forming bar
is drawn and feeds the indicators live.

## Drawing tools (left toolbar)

| tool | place it | edit it |
|---|---|---|
| Trend line | two clicks | drag either end, or the line |
| Horizontal line | one click (its price is labelled at the right edge) | drag the line |
| Long position / Short position | one click at the entry: stop 1.5 × the average bar range away, target at 2R, 20 bars wide | drag the target, the stop, the entry (left) or the width (right); shows the percentages and R:R |
| Price range | two clicks | drag either corner; shows the change, % and bars |
| Path | a click per point, double-click or Enter to finish | drag any point, or the path |

Pick the same tool again or press Esc to cancel. Click a drawing to select it (handles appear),
Delete removes it, the trash button removes every drawing of the market. Drawings are stored per
market in the browser, in time / price, so they appear on every timeframe. A position drawing is a
measurement only: nothing is ever ordered.

The top bar shows the market-structure trend of each timeframe: green ▲ bullish, red ▼ bearish,
grey – none. Full screen (⤢) keeps the toolbar, the trend strip and the options.

## Code

| file | role |
|---|---|
| `src/sp2l/chart/overlays.py` | S/R zones, trendlines, labelled swings, events, zones, trend (pure, tested in `tests/chart`) |
| `src/sp2l/chart/history.py` | Tabdeal's chart feed: newest bars, older pages, `chart_bars` cache |
| `src/sp2l/chart/service.py` | candles, overlays over the loaded bars (cached per closed bar and parameters), trends |
| `src/sp2l/api/app.py` | `/api/chart/candles`, `/api/chart/overlays`, `/api/chart/trends` |
| `web/chart/features.js` | **the feature registry: one entry per switch** |
| `web/chart/indicators.js` | indicator math (pure) |
| `web/chart/draw.js` | the drawing layer (bounded boxes, segments, labels) |
| `web/chart/tools.js` | drawing tools: toolbar, placing, selecting, dragging, storage, rendering |
| `web/chart/workspace.js` | toolbar, options panel, panes, data, scroll-back, live candles, full screen |
| `tests/chart/fake_tabdeal.py` | a deterministic stand-in for the feed (tests) |
| `tests/db/test_chart_history.py` | paging to the listing, cache, overlays over loaded bars |
| `tests/ui/test_chart.py` | the workspace in a real browser, scroll-back included |

### Adding or removing an item

- **Remove:** delete its entry in `FEATURES` (`web/chart/features.js`). Nothing else refers to it.
- **Add an indicator:** add an entry with `group: "Indicators"`, `pane: "price"` or `"own"`,
  `settings` and `attach(chart, pane, s, c)` returning `{ update(bars), remove() }`; put the math
  in `indicators.js`.
- **Add a structure / level item:** compute it in `overlays.py` (add it to the `overlays()`
  payload), then add an entry with `server: true` and `draw(ov, s, c)` returning
  `{ boxes, lines, marks }`. If its settings change the computation, mark it `query: true` and
  accept them in `/api/chart/overlays`.
