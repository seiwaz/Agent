# Runtime Validation Checklist V5

Mandatory before Live:
- symbol/tick/qty filters
- server time/drift
- raw trade WebSocket completeness/reconnect
- Cross/10x state
- cancel/replace race with zero fill
- cancel/replace race with partial/full fill
- no duplicate E1
- E1/E2 aggregation behavior
- native SL and exactly one TP behavior
- CONTRACT_PRICE semantics if used
- partial fills
- liquidationPrice behavior after adding E2
- restart in E1_PENDING / cancel-in-flight / E1_PARTIAL / E2_PENDING / open position
- disable-live behavior
- V5.1: accepted price/quantity increments, minQty and minNotional established by probe (B26)
- V5.1: positionSlTp with workingType=CONTRACT_PRICE triggers on traded price; SL/TP coverage after adding E1 remainder / E2 (B24)
- V5.1: same-side LIMIT increases a one-way position without any reduceOnly flag (E2-04)
- V5.1: protection retry limits and emergency close (`DELETE /fapi/v1/position`) reach flat and ERROR_HOLD (B21)
- V5.1: fee rates for the account tier and SL slippage allowance recorded as evidence (B16)
- V5.1: pending E2 / E1 remainder cannot re-open a position after SL/TP closes it

Any unresolved mandatory item => `LIVE_AUTOMATION_DISABLED`.

## V5.5
- **SERVER_TIME_DRIFT (B42):** estimate `offset = server_time - (send + receive) / 2` over at least 20 samples. Live requires worst absolute measured offset <= 500 ms, maintained over time.
- **CROSS_10X (B40/B41):** the API must read exactly 10x. Cross margin must be observed on a real (minimum-size, explicitly approved) position or another authoritative API response, never inferred while flat. Until then it is `CROSS_MARGIN_UNVERIFIED`.
- **B44:** no validation order while available balance is 0. Dedicated collateral must be provided first, and every order test needs explicit approval. minQty/minNotional are never guessed.
