"""Setup states, order/position snapshots and the execution-port contract (SM spec rev 5.2)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from sp2l.core.types import Side


class SetupState(StrEnum):
    BASE_SPIKE_CONFIRMED = "BASE_SPIKE_CONFIRMED"
    E1_PREPARING = "E1_PREPARING"  # gates passed, E1 not (yet) safely armed (B15)
    E1_PENDING = "E1_PENDING"
    E1_REPRICING = "E1_REPRICING"  # cancel in flight (intent REPLACE or REJECT)
    PULLBACK_DETECTED = "PULLBACK_DETECTED"
    E1_PARTIAL = "E1_PARTIAL"
    E1_FILLED = "E1_FILLED"
    E2_VALIDATING = "E2_VALIDATING"
    E2_PENDING = "E2_PENDING"
    E2_PARTIAL = "E2_PARTIAL"
    POSITION_ACTIVE = "POSITION_ACTIVE"
    FINALIZING = "FINALIZING"
    CLOSED = "CLOSED"
    REJECTED_CONTEXT = "REJECTED_CONTEXT"
    REJECTED_EXHAUSTION = "REJECTED_EXHAUSTION"
    REJECTED_RISK = "REJECTED_RISK"
    EXPIRED_UNARMED = "EXPIRED_UNARMED"  # B30
    EXPIRED_NO_FILL = "EXPIRED_NO_FILL"
    AMBIGUOUS_DATA_GAP = "AMBIGUOUS_DATA_GAP"  # B39 Shadow: gap while exposed, no fabricated PnL
    ERROR_HOLD = "ERROR_HOLD"


TERMINAL = frozenset(
    {
        SetupState.CLOSED,
        SetupState.REJECTED_CONTEXT,
        SetupState.REJECTED_EXHAUSTION,
        SetupState.REJECTED_RISK,
        SetupState.EXPIRED_UNARMED,
        SetupState.EXPIRED_NO_FILL,
        SetupState.AMBIGUOUS_DATA_GAP,
        SetupState.ERROR_HOLD,
    }
)


class OrderStatus(StrEnum):
    PENDING_SUBMIT = "PENDING_SUBMIT"
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    PENDING_CANCEL = "PENDING_CANCEL"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


INACTIVE = frozenset(
    {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED}
)


class Leg(StrEnum):
    E1 = "E1"
    E2 = "E2"


class ExitKind(StrEnum):
    TP = "TP"
    SL = "SL"
    EMERGENCY = "EMERGENCY"


@dataclass(frozen=True, slots=True)
class OrderSnapshot:
    client_order_id: str
    status: OrderStatus
    price: Decimal
    qty: Decimal
    executed_qty: Decimal

    @property
    def active(self) -> bool:
        return self.status not in INACTIVE


@dataclass(frozen=True, slots=True)
class FillEvent:
    client_order_id: str
    ts: datetime
    price: Decimal
    qty: Decimal
    fee: Decimal
    cumulative_qty: Decimal
    order_qty: Decimal


@dataclass(frozen=True, slots=True)
class ExitEvent:
    kind: ExitKind
    ts: datetime
    price: Decimal
    qty: Decimal
    fee: Decimal
    pnl: Decimal = Decimal(0)  # price PnL of the closed quantity (fees excluded)


class ExecutionPort(Protocol):
    """Exchange-facing contract. Shadow simulates it; Live implements it against Tabdeal."""

    def submit_limit(
        self, client_order_id: str, side: Side, price: Decimal, qty: Decimal, ts: datetime
    ) -> OrderSnapshot: ...

    def cancel(self, client_order_id: str, ts: datetime) -> OrderSnapshot: ...

    def query(self, client_order_id: str) -> OrderSnapshot: ...

    def position_qty(self) -> Decimal:
        """Signed position quantity attributed to the active setup (Long > 0)."""
        ...

    def place_protection(self, side: Side, sl: Decimal, tp: Decimal, ts: datetime) -> bool:
        """Place/update position-level SL and the single TP; True only if verified."""
        ...

    def emergency_close(self, ts: datetime) -> ExitEvent | None:
        """Close the whole position via the verified position-close mechanism (B21)."""
        ...
