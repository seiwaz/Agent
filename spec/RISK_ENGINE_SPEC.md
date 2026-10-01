# Risk Engine V5 (rev 5.5)

`RiskBudget = wallet_balance * 0.01`.

`D1=abs(E1-SL)`, E2 midpoint => `D2=0.5*D1`.
Equal size Q raw stop risk if both filled: `1.5*Q*D1`.

Final Q must satisfy worst-case modeled loss including configured fees/cost allowance <= RiskBudget. Round quantity DOWN to current symbol step, then recompute.

On EVERY E1 revision recompute E1, SL geometry, R, single TP, E2, Q, fees, margin and liquidation safety.

Required: Cross, 10x. Verify from exchange in Live.

Liquidation after modeled E1+E2 exposure must be at least 1R beyond SL:
- Long `liq <= SL-R`
- Short `liq >= SL+R`

Do not use another exchange's maintenance formula as authoritative. If Tabdeal data cannot validate safety => reject/disable E2 or Live as applicable.

## V5.1 resolutions (B16, B17, B18, B23)
- **wallet_balance (B16):**
  - Live: the exchange wallet balance excluding unrealized PnL.
  - Shadow: initial balance + realized PnL − fees.
- **Costs (B16):** fee rates and the SL slippage allowance are runtime configuration backed by validation evidence from the actual Tabdeal API and account tier. They are never hardcoded.
- **E2 (B23):** E2 is rounded toward SL. `D2 = abs(E2_rounded − SL)`. Complete-setup risk uses `Q*(D1 + D2)` plus costs; it is never assumed to be 1.5R.
- **Quantity (B17):**
  - `Q_final = min(Q_risk_limit, Q_margin_limit)`, with equal E1/E2 quantity.
  - Round down to the quantity step, then recheck minQty, minNotional, cost-adjusted risk and liquidation safety.
  - When margin binds, expose `MARGIN_CAPPED_QTY` in diagnostics.
  - Reject only if the resulting Q is below exchange minimums or otherwise unsafe.
  - Never resize after E1 has filled.
  - The Q_margin_limit formula is defined in the V5.2 section below (B28).
- **Liquidation (B18):**
  - Shadow records `LIQ_UNVERIFIED` and continues the simulation.
  - Live requires verified exchange liquidation data (`liquidationPrice` from positionRisk).

## V5.2 (B28)
For Shadow/provisional sizing of linear USDT contracts, margin is reserved for both equal-size legs:
- `required_margin(Q) = Q*E1/leverage + Q*E2/leverage`
- `Q_margin_limit = available_margin * leverage / (E1 + E2)`
- `Q_final = min(Q_risk_limit, Q_margin_limit)`, rounded down to the step.

Live must not rely on this theoretical formula until Tabdeal's margin behavior and fields are runtime-validated.

## V5.11 — net profit at TP must be positive
A setup is never armed unless a win is profitable after costs. Per unit, with E1 alone filled and exited at the single TP:
- `net_tp_per_unit = abs(TP − E1) − E1*entry_fee_rate − TP*exit_fee_rate`
- Reject `RISK_NET_TP_NOT_POSITIVE` iff `net_tp_per_unit <= 0` (zero is rejected).
- Rates come from the same runtime cost configuration as the risk budget (never hardcoded). E2 is not assumed to fill; if it fills, the result only improves.
- The sign does not depend on Q. The rule is part of every Risk evaluation: before the first E1 submit and on every E1 revision. A revision that fails cancels the zero-fill E1 as `RISK_INVALIDATED_BEFORE_FILL` (existing rule). After any fill nothing changes (E1 frozen).
- With the current Tabdeal rates (maker 0.08 %, taker 0.095 %) this is `R/E1 > (maker + taker)/(1 − taker) ≈ 17.52 bps`.
