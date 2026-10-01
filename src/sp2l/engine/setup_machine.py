"""One SP2L setup, from Spike confirmation to terminal state (SP2L_STATE_MACHINE rev 5.2).

Synchronous and deterministic: every exchange interaction goes through an ExecutionPort
(Shadow simulator or Live adapter) and every gate decision through a Gates evaluator.

Key rules enforced here:
- E1 reprice only when order_status == NEW and executedQty == 0 and setup position == 0
  (E1-03). Fills always beat candle-close decisions.
- Cancel/reconcile before any replacement. A fill or position found during the race
  aborts the replacement and enters the protection flow (E1-05/06).
- Any E1 fill freezes the E1 price. A partial remainder stays at the original price until
  it fills, the FillWindow expires, or the position ends (E1-07/08).
- B02: pre-PullbackStart, every sequence-continuing M1 becomes LastSpikeCandle.
- B14: after PullbackStart with confirmed zero fill, gates are still rechecked each M1
  close; a failure cancels the unfilled E1. Repricing stays frozen.
- B15: E1 is only sent when the last trade is strictly on the profit side of E1.
- B21: protection is mandatory after any execution. Bounded retries, then block,
  emergency close, reconcile to flat, ERROR_HOLD.
- B30: EXPIRED_UNARMED if the run ends before E1 was ever armed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

from sp2l.core.types import Side
from sp2l.engine.model import (
    INACTIVE,
    TERMINAL,
    ExecutionPort,
    ExitEvent,
    FillEvent,
    Leg,
    OrderSnapshot,
    OrderStatus,
    SetupState,
)
from sp2l.marketdata.m1_builder import MINUTE, M1Result, floor_minute
from sp2l.strategy.context.engine import ContextSnapshot
from sp2l.strategy.context.levels import EligibleLevel, FrozenLevel, select_breakout_level
from sp2l.strategy.exhaustion.engine import ExhaustionSnapshot
from sp2l.strategy.levels import SetupLevels, compute_levels
from sp2l.strategy.risk.engine import RiskResult
from sp2l.strategy.spike import Spike
from sp2l.strategy.submit_guard import e1_submit_safe


@dataclass(slots=True)
class GateResult:
    context: ContextSnapshot | None  # None = not applicable (counterfactual base SP2L)
    exhaustion: ExhaustionSnapshot | None
    risk: RiskResult | None

    def failed_state(self) -> SetupState | None:
        if self.context is not None and not self.context.passed:
            return SetupState.REJECTED_CONTEXT
        if self.exhaustion is not None and not self.exhaustion.passed:
            return SetupState.REJECTED_EXHAUSTION
        if self.risk is not None and not self.risk.passed:
            return SetupState.REJECTED_RISK
        return None

    def reasons(self) -> list[str]:
        out: list[str] = []
        if self.context is not None:
            out += self.context.reasons
        if self.exhaustion is not None and self.exhaustion.reason:
            out.append(self.exhaustion.reason)
        if self.risk is not None:
            out += self.risk.reasons
        return out


class Gates(Protocol):
    def evaluate(
        self,
        *,
        side: Side,
        eval_time: datetime,
        levels: SetupLevels,
        spike: Spike,
        frozen: FrozenLevel | None,
        with_risk: bool,
    ) -> GateResult: ...

    def e2_allowed(self) -> bool:
        """Live: E2 add-to-position semantics runtime-validated (E2-04). Shadow: True."""
        ...

    def eligible_breakout_levels(self, side: Side, spike: Spike) -> tuple[EligibleLevel, ...]:
        """B27: the setup's EligibleBreakoutLevels, frozen once at setup creation."""
        ...


@dataclass(frozen=True, slots=True)
class MachineConfig:
    setup_id: str
    tick: Decimal
    fill_window_candles: int = 4
    protection_attempts: int = 3  # B21: explicit, tested before Live
    cancel_wait_closes: int = 2  # unconfirmed cancel beyond this -> ERROR_HOLD


class CancelIntent(StrEnum):
    REPLACE = "REPLACE"
    REJECT = "REJECT"
    EXPIRE = "EXPIRE"


INVALIDATED = {
    SetupState.REJECTED_CONTEXT: "CONTEXT_INVALIDATED_BEFORE_FILL",
    SetupState.REJECTED_EXHAUSTION: "EXHAUSTION_INVALIDATED_BEFORE_FILL",
    SetupState.REJECTED_RISK: "RISK_INVALIDATED_BEFORE_FILL",
}


@dataclass(slots=True)
class _PendingCancel:
    intent: CancelIntent
    reject_state: SetupState | None = None
    reasons: list[str] = field(default_factory=list)
    new_levels: SetupLevels | None = None
    new_qty: Decimal | None = None
    waited: int = 0


class SetupMachine:
    def __init__(
        self,
        cfg: MachineConfig,
        port: ExecutionPort,
        gates: Gates,
        spike: Spike,
        frozen: FrozenLevel | None,
    ) -> None:
        self.cfg = cfg
        self.port = port
        self.gates = gates
        self.spike = spike
        self.side = spike.side
        self.frozen = frozen  # current BreakoutLevel (None = BreakoutContext false so far)
        self.eligible_levels: tuple[EligibleLevel, ...] | None = None
        self.breakout_locked = frozen is not None
        self.state = SetupState.BASE_SPIKE_CONFIRMED
        self.levels = compute_levels(self.side, spike.origin, spike.last, cfg.tick)
        self.qty: Decimal | None = None
        self.revision = -1
        self.ever_armed = False
        self.e1_id: str | None = None
        self.e1_filled = Decimal(0)
        self.e1_full = False
        self.e2_id: str | None = None
        self.e2_filled = Decimal(0)
        self.e2_enabled = True
        self.pb_minute: datetime | None = None
        self.pending: _PendingCancel | None = None
        self.primary_reason: str | None = None
        self.reasons: list[str] = []
        self.evaluations: list[tuple[int, datetime, GateResult]] = []
        self.eval_count = 0  # persisted; survives restore (in-memory list may be empty)
        # material-event hook (continuous persistence); never influences decisions
        self.observer: Callable[[SetupMachine, str, dict[str, Any]], None] | None = None
        self.candidate_id: str | None = None  # assigned by the engine (persistence key)
        # (revision, levels, qty, submitted_at, evaluation seq or None, LastSpike open_time)
        self.revisions: list[tuple[int, SetupLevels, Decimal, datetime, int | None, datetime]] = []
        # (client_order_id, leg, revision, price, qty, submitted_at)
        self.submissions: list[tuple[str, Leg, int, Decimal, Decimal, datetime]] = []
        # (ts, from_state or None, to_state)
        self.transitions: list[tuple[datetime, SetupState | None, SetupState]] = []
        self.created_at: datetime | None = None
        self.exits: list[ExitEvent] = []
        self.fills: list[FillEvent] = []
        self.events: list[dict[str, Any]] = []

    # ---- public API --------------------------------------------------------------------

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL

    def start(self, eval_time: datetime, last_trade: Decimal | None) -> None:
        """Initial Context -> Exhaustion -> Risk, then try to arm E1 (B15)."""
        self.created_at = eval_time
        self.transitions.append((eval_time, None, self.state))
        self._log("STATE", eval_time, frm=None, to=str(self.state))
        res = self._evaluate(eval_time, self.levels, with_risk=True)
        failed = res.failed_state()
        if failed is not None:
            self._finish(failed, res.reasons(), eval_time)
            return
        assert res.risk is not None and res.risk.qty is not None
        self._arm(self.levels, res.risk.qty, eval_time, last_trade)

    def resume_resting_e1(
        self, levels: SetupLevels, qty: Decimal, ts: datetime, pb_minute: datetime | None
    ) -> None:
        """Counterfactual only: continue an E1 that was already resting in the real setup.

        It is not a new submission, so the B15 marketability guard does not apply.
        """
        self.created_at = ts
        self.transitions.append((ts, None, self.state))
        self._log("STATE", ts, frm=None, to=str(self.state))
        self.levels = levels
        self.qty = qty
        self.revision += 1
        cid = f"{self.cfg.setup_id}-E1-{self.revision}"
        self.port.submit_limit(cid, self.side, levels.e1, qty, ts)
        self.e1_id = cid
        self.ever_armed = True
        self.revisions.append((self.revision, levels, qty, ts, None, self.spike.last.open_time))
        self.submissions.append((cid, Leg.E1, self.revision, levels.e1, qty, ts))
        self._log("E1_SUBMITTED", ts, rev=self.revision, e1=str(levels.e1), qty=str(qty), cid=cid)
        self.pb_minute = pb_minute
        if pb_minute is not None:
            self.spike.freeze()
        self._set(
            SetupState.PULLBACK_DETECTED if pb_minute is not None else SetupState.E1_PENDING, ts
        )

    def exposure(self) -> tuple[list[str], Decimal]:
        """(active order ids, setup position qty): anything a missed trade could change."""
        if self.terminal:
            return [], Decimal(0)
        active = [c for c in (self.e1_id, self.e2_id) if c and self.port.query(c).active]
        return active, self.port.position_qty()

    def finalize_ambiguous(self, ts: datetime, detail: dict[str, Any]) -> None:
        """V5.5 B39: a DATA_GAP overlapped live exposure. No close/PnL is fabricated; the
        setup is excluded from confirmed performance statistics."""
        self._log("DATA_GAP_WHILE_ACTIVE", ts, **detail)
        self._position(ts)
        self._finish(SetupState.AMBIGUOUS_DATA_GAP, ["DATA_GAP_WHILE_ACTIVE"], ts)

    def on_trade(self, price: Decimal, ts: datetime) -> None:
        """PullbackStart: first live trade at/through the active E1 (PB-01)."""
        if self.pb_minute is not None or self.e1_id is None:
            return
        if self.state not in (SetupState.E1_PENDING, SetupState.E1_REPRICING):
            return
        e1 = self.levels.e1
        touched = price <= e1 if self.side is Side.LONG else price >= e1
        if touched:
            self._pullback(ts)

    def on_fill(self, fill: FillEvent) -> None:
        self.fills.append(fill)
        self._log(
            "FILL",
            fill.ts,
            cid=fill.client_order_id,
            price=str(fill.price),
            qty=str(fill.qty),
            cumulative=str(fill.cumulative_qty),
        )
        self._order_update(fill.client_order_id, fill.ts)
        self._position(fill.ts)
        if fill.client_order_id == self.e1_id:
            self._on_e1_executed(fill.cumulative_qty, fill.order_qty, fill.ts)
        elif fill.client_order_id == self.e2_id:
            self._on_e2_executed(fill.cumulative_qty, fill.order_qty, fill.ts)

    def on_exit(self, ex: ExitEvent) -> None:
        """TP/SL (or emergency) closed the position: cancel remaining entries, finalize."""
        self.exits.append(ex)
        self._log("EXIT", ex.ts, exit_kind=str(ex.kind), price=str(ex.price))
        self._position(ex.ts)
        self._cancel_open_entries(ex.ts)
        if self.state is not SetupState.ERROR_HOLD:
            self._set(SetupState.FINALIZING, ex.ts)
            self._set(SetupState.CLOSED, ex.ts)

    def on_m1_close(self, m1: M1Result, eval_time: datetime, last_trade: Decimal | None) -> None:
        if self.terminal or self.state is SetupState.FINALIZING:
            return
        handler = {
            SetupState.E1_PREPARING: self._close_unarmed,
            SetupState.E1_PENDING: self._close_pending,
            SetupState.E1_REPRICING: self._close_repricing,
            SetupState.PULLBACK_DETECTED: self._close_pullback,
            SetupState.E1_PARTIAL: self._close_partial,
        }.get(self.state)
        if handler is not None:
            if self.pb_minute is not None and self.state in (
                SetupState.PULLBACK_DETECTED,
                SetupState.E1_PARTIAL,
            ):
                n = (m1.open_time - self.pb_minute) // MINUTE + 1
                self._log("FILL_WINDOW", eval_time, candle=n, of=self.cfg.fill_window_candles)
            handler(m1, eval_time, last_trade)

    # ---- arming / repricing -----------------------------------------------------------

    def _arm(
        self, levels: SetupLevels, qty: Decimal, eval_time: datetime, last_trade: Decimal | None
    ) -> None:
        self.levels = levels
        self.qty = qty
        if not e1_submit_safe(self.side, levels.e1, last_trade):
            self._log("E1_NOT_SAFE", eval_time, e1=str(levels.e1), last=str(last_trade))
            self._set(SetupState.E1_PREPARING, eval_time)
            return
        self.revision += 1
        cid = f"{self.cfg.setup_id}-E1-{self.revision}"
        snap = self.port.submit_limit(cid, self.side, levels.e1, qty, eval_time)
        if snap.status is OrderStatus.REJECTED:
            self._finish(SetupState.ERROR_HOLD, ["E1_SUBMIT_REJECTED"], eval_time)
            return
        self.e1_id = cid
        self.ever_armed = True
        last_eval = self.eval_count - 1 if self.eval_count else None
        self.revisions.append(
            (self.revision, levels, qty, eval_time, last_eval, self.spike.last.open_time)
        )
        self.submissions.append((cid, Leg.E1, self.revision, levels.e1, qty, eval_time))
        self._log(
            "E1_SUBMITTED", eval_time, rev=self.revision, e1=str(levels.e1), qty=str(qty), cid=cid
        )
        self._set(SetupState.E1_PENDING, eval_time)

    def _close_unarmed(self, m1: M1Result, eval_time: datetime, last: Decimal | None) -> None:
        """E1_PREPARING: no active order. Extend or the run has ended (B15, B30)."""
        if not self.spike.try_extend(m1):
            if self.ever_armed:
                self._finish(SetupState.EXPIRED_NO_FILL, ["E1_NOT_REARMED"], eval_time)
            else:
                self._finish(SetupState.EXPIRED_UNARMED, ["E1_NEVER_ARMED"], eval_time)
            return
        levels = compute_levels(self.side, self.spike.origin, self.spike.last, self.cfg.tick)
        res = self._evaluate(eval_time, levels, with_risk=True)
        failed = res.failed_state()
        if failed is not None:
            reasons = ([INVALIDATED[failed]] if self.ever_armed else []) + res.reasons()
            self._finish(failed, reasons, eval_time)
            return
        assert res.risk is not None and res.risk.qty is not None
        self._arm(levels, res.risk.qty, eval_time, last)

    def _close_pending(self, m1: M1Result, eval_time: datetime, last: Decimal | None) -> None:
        """E1_PENDING at M1 close: the 11-step procedure (MS§6)."""
        snap = self._reconcile_e1(eval_time)
        if snap is None:
            return  # fill/position found -> protection flow already entered
        if snap.status is not OrderStatus.NEW:
            self._finish(SetupState.ERROR_HOLD, [f"E1_UNEXPECTED_STATUS_{snap.status}"], eval_time)
            return
        if self.spike.try_extend(m1):
            levels = compute_levels(self.side, self.spike.origin, self.spike.last, self.cfg.tick)
            res = self._evaluate(eval_time, levels, with_risk=True)
            failed = res.failed_state()
            if failed is not None:
                self.pending = _PendingCancel(CancelIntent.REJECT, failed, res.reasons())
            elif levels.e1 == self.levels.e1 and res.risk is not None and res.risk.qty == self.qty:
                return  # nothing to revise
            else:
                assert res.risk is not None
                self.pending = _PendingCancel(
                    CancelIntent.REPLACE, new_levels=levels, new_qty=res.risk.qty
                )
        else:
            res = self._evaluate(eval_time, self.levels, with_risk=False)
            failed = res.failed_state()
            if failed is None:
                return
            self.pending = _PendingCancel(CancelIntent.REJECT, failed, res.reasons())
        self._cancel_e1(eval_time, last)

    def _close_repricing(self, m1: M1Result, eval_time: datetime, last: Decimal | None) -> None:
        assert self.pending is not None and self.e1_id is not None
        self._order_update(self.e1_id, eval_time)
        self._resolve_cancel(self.port.query(self.e1_id), eval_time, last)
        if self.state is SetupState.E1_REPRICING:
            self.pending.waited += 1
            if self.pending.waited > self.cfg.cancel_wait_closes:
                self._finish(SetupState.ERROR_HOLD, ["CANCEL_UNCONFIRMED"], eval_time)

    def _close_pullback(self, m1: M1Result, eval_time: datetime, last: Decimal | None) -> None:
        snap = self._reconcile_e1(eval_time)
        if snap is None:
            return
        if self._window_expired(m1):
            self.pending = _PendingCancel(CancelIntent.EXPIRE)
            self._cancel_e1(eval_time, last)
            return
        if snap.status is not OrderStatus.NEW:
            self._finish(SetupState.ERROR_HOLD, [f"E1_UNEXPECTED_STATUS_{snap.status}"], eval_time)
            return
        res = self._evaluate(eval_time, self.levels, with_risk=False)  # B14
        failed = res.failed_state()
        if failed is not None:
            self.pending = _PendingCancel(CancelIntent.REJECT, failed, res.reasons())
            self._cancel_e1(eval_time, last)

    def _close_partial(self, m1: M1Result, eval_time: datetime, last: Decimal | None) -> None:
        """FW-02: at close of #4 cancel only the remainder; keep protection; E2 off for good."""
        if not self._window_expired(m1):
            return
        assert self.e1_id is not None
        snap = self.port.cancel(self.e1_id, eval_time)
        self._order_update(self.e1_id, eval_time)
        if snap.executed_qty > self.e1_filled:
            self._on_e1_executed(snap.executed_qty, snap.qty, eval_time)
        if self.state is SetupState.E1_PARTIAL:
            if snap.status not in INACTIVE:
                self._finish(SetupState.ERROR_HOLD, ["E1_REMAINDER_CANCEL_UNCONFIRMED"], eval_time)
                return
            self.e2_enabled = False
            self._set(SetupState.POSITION_ACTIVE, eval_time)

    def _cancel_e1(self, ts: datetime, last: Decimal | None) -> None:
        assert self.e1_id is not None
        self._set(SetupState.E1_REPRICING, ts)
        snap = self.port.cancel(self.e1_id, ts)
        self._order_update(self.e1_id, ts)
        self._resolve_cancel(snap, ts, last)

    def _resolve_cancel(self, snap: OrderSnapshot, ts: datetime, last: Decimal | None) -> None:
        """Replacement/reject only after the old E1 is inactive with zero fill and position."""
        assert self.pending is not None
        pos = self.port.position_qty()
        if snap.executed_qty > 0 or pos != 0:
            self.pending = None
            self._log("FILL_DURING_CANCEL", ts, executed=str(snap.executed_qty), pos=str(pos))
            self._on_e1_executed(max(snap.executed_qty, abs(pos)), snap.qty, ts)
            return
        if snap.status not in INACTIVE:
            return  # still in flight: stay in E1_REPRICING
        pending, self.pending = self.pending, None
        self.e1_id = None
        if pending.intent is CancelIntent.REPLACE:
            assert pending.new_levels is not None and pending.new_qty is not None
            self._arm(pending.new_levels, pending.new_qty, ts, last)
        elif pending.intent is CancelIntent.EXPIRE:
            self._finish(SetupState.EXPIRED_NO_FILL, ["FILL_WINDOW_EXPIRED"], ts)
        else:
            assert pending.reject_state is not None
            self._finish(
                pending.reject_state, [INVALIDATED[pending.reject_state], *pending.reasons], ts
            )

    # ---- execution / protection --------------------------------------------------------

    def _reconcile_e1(self, ts: datetime) -> OrderSnapshot | None:
        """Steps 2-4: returns the snapshot, or None if a fill/position was found."""
        assert self.e1_id is not None
        snap = self.port.query(self.e1_id)
        pos = self.port.position_qty()
        if snap.executed_qty > self.e1_filled or (pos != 0 and self.e1_filled == 0):
            self._on_e1_executed(max(snap.executed_qty, abs(pos)), snap.qty, ts)
            return None
        return snap

    def _pullback(self, ts: datetime) -> None:
        self.pb_minute = floor_minute(ts)
        self.spike.freeze()
        self.breakout_locked = True  # B27: BreakoutLevel frozen permanently
        self._log("PULLBACK_START", ts, minute=self.pb_minute.isoformat())
        if self.state is SetupState.E1_PENDING:
            self._set(SetupState.PULLBACK_DETECTED, ts)

    def _window_expired(self, m1: M1Result) -> bool:
        if self.pb_minute is None:
            return False
        candle_no = (m1.open_time - self.pb_minute) // MINUTE + 1
        return candle_no >= self.cfg.fill_window_candles

    def _on_e1_executed(self, cumulative: Decimal, order_qty: Decimal, ts: datetime) -> None:
        if cumulative <= self.e1_filled and self.state not in (
            SetupState.E1_PENDING,
            SetupState.E1_REPRICING,
            SetupState.PULLBACK_DETECTED,
        ):
            return
        if self.pb_minute is None:
            self._pullback(ts)  # a fill implies the touch
        self.spike.freeze()
        self.breakout_locked = True
        self.pending = None
        self.e1_filled = cumulative
        self.e1_full = cumulative >= order_qty
        self._set(SetupState.E1_FILLED if self.e1_full else SetupState.E1_PARTIAL, ts)
        if not self._protect(ts):
            return
        if self.e1_full:
            self._set(SetupState.E2_VALIDATING, ts)
            self._maybe_submit_e2(ts)

    def _maybe_submit_e2(self, ts: datetime) -> None:
        if not (self.e2_enabled and self.gates.e2_allowed()) or self.qty is None:
            self._log("E2_SKIPPED", ts)
            self._set(SetupState.POSITION_ACTIVE, ts)
            return
        cid = f"{self.cfg.setup_id}-E2-0"
        snap = self.port.submit_limit(cid, self.side, self.levels.e2, self.qty, ts)
        if snap.status is OrderStatus.REJECTED:
            self._log("E2_REJECTED", ts)
            self._set(SetupState.POSITION_ACTIVE, ts)
            return
        self.e2_id = cid
        self.submissions.append((cid, Leg.E2, 0, self.levels.e2, self.qty, ts))
        self._log("E2_SUBMITTED", ts, e2=str(self.levels.e2), qty=str(self.qty), cid=cid)
        self._set(SetupState.E2_PENDING, ts)

    def _on_e2_executed(self, cumulative: Decimal, order_qty: Decimal, ts: datetime) -> None:
        self.e2_filled = cumulative
        full = cumulative >= order_qty
        self._set(SetupState.POSITION_ACTIVE if full else SetupState.E2_PARTIAL, ts)
        self._protect(ts)

    def _protect(self, ts: datetime) -> bool:
        """B21: bounded retries; on failure block, emergency close, flat, ERROR_HOLD."""
        for attempt in range(1, self.cfg.protection_attempts + 1):
            if self.port.place_protection(self.side, self.levels.sl, self.levels.tp, ts):
                self._log("PROTECTION_VERIFIED", ts, attempt=attempt)
                return True
            self._log("PROTECTION_FAILED", ts, attempt=attempt)
        self._cancel_open_entries(ts)
        ex = self.port.emergency_close(ts)
        if ex is not None:
            self.exits.append(ex)
        flat = self.port.position_qty() == 0
        self._log("EMERGENCY_CLOSE", ts, flat=flat, price=str(ex.price) if ex else None)
        self._position(ts)
        self._finish(
            SetupState.ERROR_HOLD,
            ["PROTECTION_UNVERIFIED", "EMERGENCY_CLOSED" if flat else "NOT_FLAT_AFTER_CLOSE"],
            ts,
        )
        return False

    def _cancel_open_entries(self, ts: datetime) -> None:
        for cid in (self.e1_id, self.e2_id):
            if cid is None:
                continue
            snap = self.port.query(cid)
            if snap.active:
                self.port.cancel(cid, ts)
                self._order_update(cid, ts)

    # ---- bookkeeping -------------------------------------------------------------------

    def _evaluate(self, eval_time: datetime, levels: SetupLevels, *, with_risk: bool) -> GateResult:
        if not self.breakout_locked:
            # B27: select from the frozen eligible set; may become true / advance while
            # pre-Pullback with zero fill; locked at PullbackStart or first E1 fill
            if self.eligible_levels is None:
                self.eligible_levels = self.gates.eligible_breakout_levels(self.side, self.spike)
            level = select_breakout_level(self.side, self.eligible_levels, self.spike.candles)
            if level != self.frozen:
                self.frozen = level
                self._log("BREAKOUT_LEVEL", eval_time, price=str(level.price if level else None))
        res = self.gates.evaluate(
            side=self.side,
            eval_time=eval_time,
            levels=levels,
            spike=self.spike,
            frozen=self.frozen,
            with_risk=with_risk,
        )
        seq = self.eval_count
        self.eval_count += 1
        self.evaluations.append((seq, eval_time, res))
        self._log("EVALUATION", eval_time, seq=seq, with_risk=with_risk)
        return res

    def _finish(self, state: SetupState, reasons: list[str], ts: datetime) -> None:
        self.reasons = reasons
        self.primary_reason = reasons[0] if reasons else None
        self._set(state, ts)

    def _set(self, state: SetupState, ts: datetime) -> None:
        if state is not self.state:
            frm = self.state
            self.transitions.append((ts, frm, state))
            self.state = state  # update first: observers checkpoint the new state
            self._log("STATE", ts, frm=str(frm), to=str(state))

    def _order_update(self, cid: str, ts: datetime) -> None:
        snap = self.port.query(cid)
        self._log(
            "ORDER_UPDATE", ts, cid=cid, status=snap.status.value, executed=str(snap.executed_qty)
        )

    def _position(self, ts: datetime) -> None:
        self._log("POSITION", ts, qty=str(self.port.position_qty()))

    def _log(self, kind: str, ts: datetime, **data: Any) -> None:
        event = {"kind": kind, "ts": ts.isoformat(), **data}
        self.events.append(event)
        if self.observer is not None:
            self.observer(self, kind, event)
