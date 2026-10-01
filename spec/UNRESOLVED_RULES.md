# Unresolved / Blockers V5 (rev 5.5)

Exchange facts that only runtime probes can settle (they block **Live only**; V6.0: these are not
strategy choices and are never decided by assumption):
1. Current Tabdeal native SL/TP trigger execution semantics.
2. Exact same-symbol position aggregation / positionId behavior after E2.
3. Tabdeal maintenance-margin/liquidation formula.
4. Whether raw trade stream is complete enough for exact historical intrabar replay.
5. Exchange-specific behavior when cancel acknowledgement races a fill.

Strategy ambiguities are no longer blockers (V6.0 governance): decide the sensible option and record the decision with its rationale in `DECISIONS.md` (V6.0 governance, owner-approved 2026-10-01). Live-only runtime-validation blockers (B22, B40–B42, B44, B45, B50, B51) are exchange facts, not choices, and stay open until proven by the runtime probes; Live automation stays disabled.

## V5.1 status
Items 1–5 block **Live only** (B22) and are closed by runtime probes (RUNTIME_VALIDATION_CHECKLIST.md).
Strategy BLOCKERS B01–B26 were decided on 2026-09-26 (DECISIONS.md, section V5.1). PROVISIONAL
interpretations B27–B32 are listed in `SP2L_RULES.yaml` (`provisional`) and `docs/BLOCKERS.md` and await confirmation.

## V5.2 status
B27–B32 were decided on 2026-09-26, except the BreakoutContext-level reference part of B27, which stays open. New provisional items: B33 and B34 (`SP2L_RULES.yaml` `provisional`).

## V5.3 status
B27, B33 and B34 were decided on 2026-09-26. The B27 clarification settled B35 and B36. The remaining provisional item B37 is listed in `SP2L_RULES.yaml` (`provisional`) and `docs/BLOCKERS.md`.

## V5.4 status
B37 and B38 were decided on 2026-09-26. No provisional strategy interpretations remain open. Live stays blocked by the runtime probes (B22).

## V5.5 status
B39 was decided. B40, B41, B42 and B44 remain Live blockers pending user actions and approved probes. B45 is a production-hosting requirement.
