# SP2L State Machine V5 (rev 5.5)

States:
`SCANNING → BASE_SPIKE_CONFIRMED → CONTEXT_EVALUATION → EXHAUSTION_EVALUATION → E1_PREPARING → E1_PENDING ↔ E1_REPRICING → PULLBACK_DETECTED → E1_PARTIAL/E1_FILLED → E2_VALIDATING → E2_PENDING/E2_PARTIAL → POSITION_ACTIVE → FINALIZING → CLOSED`

Terminal pre-trade outcomes include `REJECTED_CONTEXT`, `REJECTED_EXHAUSTION`, `REJECTED_RISK`, `EXPIRED_UNARMED` (V5.2 B30), `EXPIRED_NO_FILL`, `AMBIGUOUS_DATA_GAP` (V5.5 B39, Shadow), `ERROR_HOLD`.

## Initial candidate
At first valid directional P-Gap: freeze Origin, set LastSpikeCandle, create candidate, reserve two logical slots provisionally, evaluate Context then Exhaustion then Risk.
Only after all pass may E1 be submitted.

## E1 pending and reprice
At every event, fills beat candle-close reprice decisions.

A reprice is allowed only when all are true:
```text
order_status == NEW
executedQty == 0
setup_position_qty == 0
```

At each M1 close:
1. process all raw/order events through the close;
2. reconcile E1;
3. reconcile the setup-linked position;
4. if any partial/full fill or nonzero setup position is found, transition to `E1_PARTIAL` or `E1_FILLED`; **do not reprice**;
5. only with zero fill + zero setup position, evaluate structural extension and re-evaluate Context + Exhaustion;
6. if any hard gate fails, cancel/reconcile E1 and reject-after-arm only if zero fill remains confirmed;
7. if pass + extend, cancel/reconcile old E1;
8. replacement is permitted only after old E1 is confirmed inactive and zero fill/zero position is confirmed again;
9. if no extension, keep current E1 until PullbackStart or another terminal condition.

If a fill is discovered during cancel/reconcile, abandon the replacement path and enter protection flow.

## PullbackStart
First live touch/cross active E1 freezes repricing and starts 4-candle FillWindow.

## Any E1 execution
Any partial/full E1 fill permanently freezes the E1 entry price for this setup.

Rules:
- future E1 repricing is forbidden;
- Context/Exhaustion must not close or move the executed position;
- protect executed quantity immediately with original SL/single TP;
- if E1 is partial, the unfilled remainder stays at the **same original E1 price** through the FillWindow;
- do not cancel/recreate the remainder at a new price;
- E2 remains disabled until E1 reaches full fill;
- if partial E1 remains partial at FillWindow expiry, cancel only the unfilled remainder, keep the executed position protected, and permanently disable E2.

## E2
Only after full E1 + verified protection. Equal size only. If unsafe, continue E1 position without E2.

E2 must add exposure in the same direction and must not reduce/close the position. If the current Tabdeal API requires a `reduceOnly` flag, `false` may be used only after runtime validation confirms that meaning; otherwise Live E2 is blocked.

## Finalize
Persist canonical and counterfactual metadata, release slots, emit UI reset only after durable archive succeeds.

## V5.1 resolutions (B03, B14, B15, B21)
- **E1_PREPARING submit guard (B15):** if `last_trade` is not strictly on the profit side of E1, no order is sent. At the next finalized M1 close, re-evaluate extension, Context, Exhaustion and Risk.
  - If the Spike sequence ends before any E1 was armed: `EXPIRED_UNARMED` with reason `E1_NEVER_ARMED` (V5.2 B30).
- **PULLBACK_DETECTED with confirmed zero fill and zero setup position (B14):** recheck Context and Exhaustion on every M1 close. Repricing stays frozen. A gate failure cancels the still-unfilled E1.
- **Protection failure (B21):** after any E1/E2 execution, protection is mandatory. SL/TP placement and verification are retried a bounded number of times (explicit configuration, tested before Live). If protection still cannot be verified:
  1. block E2 and all new orders;
  2. emergency-close the position using the verified position-close mechanism;
  3. reconcile until flat;
  4. enter `ERROR_HOLD` and alert.

  A Live position is never knowingly left open without verified protection.
- **Re-arm (B03):** a new candidate on the same side requires a directional-sequence break. A P-Gap seen while the market's slots are busy is logged only.

## V5.2 (B30)
`EXPIRED_UNARMED` (reason `E1_NEVER_ARMED`) applies when the spike/run ends before any E1 was successfully armed. `EXPIRED_NO_FILL` applies only to an E1 that was active and received zero fill. This supersedes the V5.1 note that proposed `EXPIRED_NO_FILL`/`E1_NEVER_SUBMITTED`.

## V5.5 (B39)
- **`AMBIGUOUS_DATA_GAP` (Shadow):** a DATA_GAP (including an UNANCHORED minute or a missing-minute hole) overlaps a period where an E1/E2 order, any order, or a position of the setup was active. The setup is finalized without fabricating a close or PnL: simulated orders and the position are discarded as unknowable. It is excluded from confirmed performance statistics, and the symbol returns to normal warmup/scanning.
- **Unarmed setups:** a setup with nothing active when the gap hits follows the normal run-end rules (B29/B30/B31).
- **`ERROR_HOLD` scope:** reserved for runtime/system uncertainty that needs intervention. Missing Shadow market data is historical uncertainty and never an ERROR_HOLD.
