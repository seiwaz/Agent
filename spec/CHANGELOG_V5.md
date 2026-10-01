# V5 Changelog

V1–V4 obsolete.

Added hard M5 Context Engine before E1:
- confirmed 2L/2R pivots
- M5 trend
- ADX14/CHOP14 regime
- range-middle rejection
- Breakout / HTF alignment / Range-edge requirement
- HTF-opposite rejection without breakout
- RoomToTP >=1R
- low-liquidity rejection
- re-evaluation before every E1 revision

Added Exhaustion Gate:
- TrendAgeBars
- MicrochannelLen
- StretchATR
- SpikeATR
- RangePosition20 / opposing swing distance
- FreshBreakout exception

Added mandatory rejected-candidate persistence and counterfactual Shadow outcomes.
Added full Context/Exhaustion visibility to WebUI.

## V5.1 (2026-09-26)
Applied BLOCKER decisions B01–B26 (see DECISIONS.md, section V5.1).
- `spec.version` 5.0 → 5.1.
- The V5.0 `SP2L_RULES.yaml` sha256 was `462d723401594412125ecfdc814fd03e42e5eb07927c2f7dce17bce7cdac8bb3`.
- Added provisional interpretations B27–B32, pending confirmation.

## V5.2 (2026-09-26)
Applied decisions B27–B32 (`spec.version` 5.1 → 5.2). B31 supersedes the B05 rule "zero-trade minute = missing". Open: the B27 BreakoutContext-level reference; provisional B33 and B34.

## V5.3 (2026-09-26)
Revised B27 (BreakoutContext = highest/lowest crossed pre-Spike level) and decided B33 and B34 (`spec.version` 5.2 → 5.3). New provisional items: B35–B37.

B27 clarification (same day): frozen EligibleBreakoutLevels; the BreakoutLevel may advance until PullbackStart or the first E1 fill. This resolves B35/B36; B37 stays provisional.

## V5.4 (2026-09-26)
B37 (pivot data quality) and B38 (path-relative obstacles; B10 reworded). `spec.version` 5.3 → 5.4. No open provisional items.

## V5.5 (2026-09-26)
B39 (Shadow restart = journal replay; `AMBIGUOUS_DATA_GAP`), B40–B45 (Live/validation blockers and the host-sleep policy). `spec.version` 5.4 → 5.5.

## V5.8 (2026-09-28)
- `spec.version` 5.7 → 5.8. The market_data section gains history_recovery, and warmup_semantics gains the independence and bootstrap rules. market.leverage_policy added (see DECISIONS.md, section V5.8).
- Strategy thresholds unchanged.

## V5.9 (2026-09-28)
- `spec.version` 5.8 → 5.9. `pgap.qualification` was added (strong directional impulse C2 with body/range ≥ 0.60; strong gap ≥ 0.15 × body and ≥ 2 ticks). `market_data.live_forming_candle` was added (display only). See DECISIONS.md, section V5.9.

## V5.10 (2026-09-29)
- `spec.version` 5.9 → 5.10. `context.liquidity.unknown_trade_count` = PASS_IF_VOLUME_NOT_LOW_ELSE_LIQUIDITY_UNKNOWN (three-valued evaluation of the unchanged conjunction). See DECISIONS.md, section V5.10.
- Strategy thresholds unchanged.

## V5.11 (2026-09-29)
- `spec.version` 5.10 → 5.11. `risk.net_at_tp` added (reject RISK_NET_TP_NOT_POSITIVE unless E1 alone exited at TP is profitable after configured costs). See DECISIONS.md, section V5.11.
- No threshold changed; the floor follows from the configured fee rates.

## V5.12 (2026-09-30)
- `spec.version` 5.11 → 5.12. `pgap.qualification.enforced` = false: the V5.9 P-Gap quality filter is no longer a rule (measured and recorded only). See DECISIONS.md, section V5.12.
