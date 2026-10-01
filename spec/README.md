# SP2L Automated Trading Bot — Specification V5

**Target:** Claude Code / Opus 5.5 / Medium effort  
**Status:** AUTHORITATIVE IMPLEMENTATION PACKAGE  
**V1–V4 are obsolete.**

V5 combines the core SP2L execution model extracted from the complete training video with a deterministic **M5 Context Engine** and a separate **Exhaustion Gate**. The purpose is to stop treating every valid M1 Spike+P-Gap as tradeable.

## Non-negotiable runtime principles

- M1 = entry/execution timeframe.
- M5 = context timeframe.
- No ML, online learning, self-optimization or automatic rule mutation.
- No undocumented assumptions.
- Unknown/ambiguous state => fail closed.
- Shadow starts with 100 USDT.
- Cross, 10x leverage.
- Complete E1+E2 modeled worst-case loss <= 1% wallet including configured costs.
- Exactly one TP = 1R.
- E2 = midpoint(E1,SL), equal quantity, only after full E1 fill + verified protection.
- Market-data collector runs continuously and stores raw trades + finalized OHLCV.
- Backtest/Replay/Shadow/Live use the same canonical strategy engine.
- Frontend renders backend truth only.

## Decision pipeline

```text
M1 directional sequence + P-Gap
        ↓
BASE SP2L CANDIDATE
        ↓
M5 CONTEXT ENGINE
        ↓
EXHAUSTION GATE
        ↓
RISK / LIQUIDITY / ROOM CHECKS
        ↓
TRADEABLE SETUP
        ↓
E1 → PullbackStart → Fill → E2 → single TP / SL
```

## File precedence

1. `SP2L_RULES.yaml`
2. `SP2L_MASTER_SPEC.md`
3. `CONTEXT_ENGINE_SPEC.md`
4. `EXHAUSTION_GATE_SPEC.md`
5. `SP2L_STATE_MACHINE.md`
6. `RISK_ENGINE_SPEC.md`
7. `TABDEAL_INTEGRATION_REQUIREMENTS.md`
8. `BACKTEST_SHADOW_SPEC.md`
9. remaining documents

If any contradiction remains, Claude must report a BLOCKER instead of resolving it by intuition.
