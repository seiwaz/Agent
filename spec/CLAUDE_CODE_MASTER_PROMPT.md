# Claude Code Master Prompt — SP2L V5

Target: Claude Code / Opus 5.5 / Medium effort.

## DO NOT CODE FIRST
Read every V5 file. Your first work product must contain:
1. architecture plan
2. repository tree
3. rule-ID map
4. rule-to-function/component matrix
5. state-transition table
6. DB schema/migrations plan
7. exact indicator math plan
8. Tabdeal runtime-validation plan
9. test plan
10. BLOCKERS
11. assumptions (non-trading-behavior only)

Any ambiguity that could change trading behavior is a BLOCKER. Do not infer from common trading practice.

## Absolute rules
- V1–V4 obsolete.
- Use `SP2L_RULES.yaml` then master/context/exhaustion specs as authority.
- No ML, online learning, automatic tuning or rule mutation.
- M1 execution, M5 context.
- only finalized M5 for gates.
- exact strict/equality rules as written.
- Context and Exhaustion are re-evaluated before every zero-fill E1 submit/replacement.
- E1 reprice is allowed ONLY when `order_status == NEW AND executedQty == 0 AND setup_position_qty == 0`.
- If pending E1 has zero fill/zero setup position and a hard gate becomes invalid, safely cancel/reconcile and reject.
- Any partial or full E1 fill permanently freezes the E1 price for that setup.
- After a partial E1 fill, the unfilled remainder stays at the original E1 price through the FillWindow; NEVER move it to the latest Spike candle.
- A partial/full fill discovered during cancel/reconcile aborts replacement and enters protection flow.
- never create a replacement until prior E1 is confirmed inactive with executedQty=0 AND setup_position_qty=0.
- no retroactive fills.
- one TP only = 1R.
- no TP2, partial TP, AB=CD target or trailing target.
- E2 midpoint/equal-size only after full E1 + protection.
- E2 is a same-direction add-to-position order, never a reduce/close order. Use `reduceOnly=false` only if current Tabdeal runtime validation confirms the exact semantics; otherwise disable Live E2.
- complete setup worst-case <=1% wallet including configured costs.
- Cross 10x.
- Shadow initial 100 USDT.
- collector always on.
- frontend cannot calculate strategy state.
- rejected candidates and counterfactual results must be stored/displayed but never influence runtime.

## Prevent these common implementation guesses
Do NOT:
- use candle color to define Spike ending;
- use forming M5 data;
- treat equal highs/lows as pivots/P-Gaps/breakouts;
- use SMA instead of Wilder ADX/ATR where specified;
- reorder Regime precedence;
- include current M5 in liquidity median reference;
- treat wick-through as M5 breakout;
- treat broken swing levels as future obstacles;
- clamp RangePosition before logic;
- assume no obstacle means rejection (it means +INF room/pass);
- apply Context/Exhaustion to close or move an already-filled position;
- reprice E1 after any partial/full fill;
- move a partially filled E1 remainder to a new price;
- assume E2 order flags that could reduce/close a position without runtime validation;
- use rejected-candidate counterfactual to tune or alter live rules;
- invent a second TP.

## UI
Use `ui-ux-pro-max` or report BLOCKER. Context and Exhaustion must be transparent: raw values, thresholds, pass/fail and reason codes.

## Live
Do not enable until all mandatory runtime-validation items pass. Unknown exchange behavior => Live disabled/fail closed.
