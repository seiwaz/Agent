# WebUI / UX V5

Use `ui-ux-pro-max`; if unavailable, BLOCKER.
Frontend is display-only for strategy logic.

## Main workflow
`Spike → Context → Exhaustion → E1 → Pullback → Fill → E2 → Position → single TP/SL → Finalize`

## Context panel
Show live canonical values:
- M5 Regime: TREND/RANGE/TRANSITION
- M5 Trend: BULL/BEAR/NEUTRAL
- CHOP14, ADX14
- RangeHigh/Low, RangePosition
- RangeMiddle PASS/FAIL
- latest confirmed M5 Swing High/Low
- BreakoutContext YES/NO
- HTFAlignment YES/NO
- RangeEdgeOrigin YES/NO
- NearestObstacle and RoomToTP_R
- VolumeRatio, TradeCountRatio, Liquidity PASS/FAIL
- final Context PASS/REJECT + reason codes

## Exhaustion panel
Show:
- TrendAgeBars
- MicrochannelLen
- EMA20_M5
- ATR14_M5
- StretchATR
- SpikeATR
- RangePosition20
- OpposingSwingDistanceATR
- FreshBreakoutException
- Exhaustion PASS/REJECT + sub-reasons

## Candidate history
Tabs/filters:
- TRADED
- REJECTED_CONTEXT
- REJECTED_EXHAUSTION
- REJECTED_RISK
- EXPIRED
- COUNTERFACTUAL RESULT

Rejected candidates remain visible with chart replay and hypothetical outcome clearly marked as non-traded.

## Chart
Overlay M5 swing levels, range bounds/middle zone, nearest obstacle, Spike/P-Gap/Origin, E1 revisions, E2, SL and exactly one TP.

After durable finalization, reset status indicators to SCANNING.
