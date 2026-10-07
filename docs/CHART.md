# Chart workspace

The dashboard's **Chart** tab: price, market structure, levels and indicators for one market on
5m / 15m / 1h / 4h / 1d. Display only: it opens no position and sends no order.

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

The top bar shows the market-structure trend of each timeframe: green ▲ bullish, red ▼ bearish,
grey – none. Full screen (⤢) keeps the toolbar, the trend strip and the options.

## Code

| file | role |
|---|---|
| `src/sp2l/chart/overlays.py` | S/R zones, trendlines, labelled swings, events, zones, trend (pure, tested in `tests/chart`) |
| `src/sp2l/chart/service.py` | loads the stored bars, caches per closed bar and parameters |
| `src/sp2l/api/app.py` | `/api/chart/candles`, `/api/chart/overlays`, `/api/chart/trends` |
| `web/chart/features.js` | **the feature registry: one entry per switch** |
| `web/chart/indicators.js` | indicator math (pure) |
| `web/chart/draw.js` | the drawing layer (bounded boxes, segments, labels) |
| `web/chart/workspace.js` | toolbar, options panel, panes, data, live candles, full screen |
| `tests/ui/test_chart.py` | the workspace in a real browser |

### Adding or removing an item

- **Remove:** delete its entry in `FEATURES` (`web/chart/features.js`). Nothing else refers to it.
- **Add an indicator:** add an entry with `group: "Indicators"`, `pane: "price"` or `"own"`,
  `settings` and `attach(chart, pane, s, c)` returning `{ update(bars), remove() }`; put the math
  in `indicators.js`.
- **Add a structure / level item:** compute it in `overlays.py` (add it to the `overlays()`
  payload), then add an entry with `server: true` and `draw(ov, s, c)` returning
  `{ boxes, lines, marks }`. If its settings change the computation, mark it `query: true` and
  accept them in `/api/chart/overlays`.
