# Chart workspace

The dashboard's **Chart** tab: price, market structure, levels and indicators for one market on
5m / 15m / 1h / 4h / 1d, and trading on Tabdeal futures from a Long / Short position drawing
(the Trade button, below). Nothing else on the chart sends an order.

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

A position drawing is shaded by what price did: once a bar has traded through its entry, the part
from the entry to the current price — or to the stop / target that was hit (the stop first when
one bar reaches both) — is filled darker green in profit or darker red at a loss, and the entry
label adds the result ("+1.20 %", "Target hit +3.00 %", "Stopped −1.50 %").

## Trading from the chart (Tabdeal futures)

1. Place a **Long position** or **Short position** and drag its entry, stop and target.
2. Press **Trade** (⚡, left toolbar). It takes the selected position drawing, or the last one
   placed, and asks for the **leverage** (1 … `trading.max_leverage`) and the **margin** in USDT
   (≤ `trading.max_margin_usdt`); it shows the size, the loss at the stop, the gain at the
   target, R:R and the futures wallet.
3. **Place** sends: the leverage, then a LIMIT GTC order at the entry. When (part of) it fills,
   the server sets the position's stop and target on Tabdeal (`positionSlTp`,
   `trading.working_type`), again whenever more of it fills.

The panel under the chart (▾ / ▴ hides it) has two tabs:

- **Open trades** — pending (order resting) and active (position open) trades with entry, size
  (filled / ordered), leverage, stop, target, mark price, unrealized PnL, ROE, liquidation price
  and ✓ once the stop / target are set on Tabdeal (⚠ while they are not). **Cancel** cancels a
  pending order (a part already filled stays as an active trade); **Close** closes the position
  at market (and cancels any unfilled rest of the entry). Click a row for the trade's log.
- **History** — closed (target, stop, closed here, closed on Tabdeal), canceled and rejected
  trades with entry, exit and realized PnL.

Tabdeal has no futures user stream, so the API server polls it every `trading.poll_s` seconds
while a trade is open — with or without a browser — and the table follows the exchange: a fill,
a stop or target, a cancel or a close made in Tabdeal's own app all show up here.

Safety:
- One open trade per market. Refused while the market already has a position or open orders on
  Tabdeal (position-level stop / target would cover them too).
- Refused when the loss at the stop would liquidate the cross-margin wallet first, or the margin
  is more than the available balance.
- An entry remainder still resting when the position ends is canceled (it would open a new,
  unprotected position).
- `trading.enabled`, and only behind the dashboard login (`auth.enabled`): every page and API
  call needs a session (HttpOnly, SameSite=Strict cookie), POST only from the dashboard's own
  origin, and the API key file `~/.config/sp2l/tabdeal.env` (mode 600; the key needs trading
  permission). The key never leaves the server.

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
| `web/chart/trading.js` | the Trade dialog and the open / history panel |
| `src/sp2l/trading/` | Tabdeal client (allow-listed signed endpoints), trade manager, `/api/trade/*` |
| `tests/db/test_trading.py` | trading against a simulated Tabdeal (`tests/trading/fake_exchange.py`) |
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
