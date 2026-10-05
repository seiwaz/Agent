"""The shared simulated wallet: one USDT balance for every SMC market.

- Sizing at entry: risk `risk_pct` of the current balance per position; the position's
  margin (notional / `max_leverage`, cross) must fit into the free balance (balance minus the
  margin of every open position on any market), otherwise the quantity is reduced to fit.
- Booking at every exit (TP1, TP2 and the final exit of the rest): realized PnL of that part
  = its quantity x price move - its share of the entry fee - its exit fee (USDT), appended to
  the ledger with the running balance; the signal's PnL / fees are the sums.
Nothing here talks to an exchange.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal

from sqlalchemy import Connection, Engine, text

from sp2l.core.types import Side
from sp2l.smc.lifecycle import ACTIVE_SQL, Part, Tracked
from sp2l.smc.model import Costs, SmcParams


@dataclass(frozen=True, slots=True)
class Size:
    qty: Decimal
    notional: Decimal
    margin: Decimal
    leverage: Decimal  # notional / balance
    note: str | None = None  # MARGIN_LIMITED when the free balance cut the quantity


def floor_step(v: Decimal, step: Decimal) -> Decimal:
    return (v / step).to_integral_value(rounding=ROUND_DOWN) * step


def part_pnl(t: Tracked, part: Part, qty: Decimal, costs: Costs) -> tuple[Decimal, Decimal]:
    """(net PnL, fees) in USDT of one exit of a position of `qty`."""
    q = qty * part.frac
    fees = q * (t.entry * t.fee_in(costs) + part.price * costs.taker_fee)
    sgn = 1 if t.side is Side.LONG else -1
    return q * (part.price - t.entry) * sgn - fees, fees


def ladder_fracs(qty: Decimal, p: SmcParams, step: Decimal) -> tuple[Decimal, Decimal]:
    """Shares of the position closed at TP1 and TP2, in whole quantity steps; a part that
    rounds down to nothing passes its share to the next one."""
    if qty <= 0:
        return p.tp1_frac, p.tp2_frac
    q1 = floor_step(qty * p.tp1_frac, step)
    carry = p.tp1_frac if q1 == 0 else Decimal(0)
    q2 = floor_step(qty * (p.tp2_frac + carry), step)
    q2 = min(q2, qty - q1)
    return q1 / qty, q2 / qty


class Wallet:
    def __init__(self, db: Engine, params: SmcParams, symbols: list[str]) -> None:
        self.db = db
        self.params = params
        self.symbols = sorted(symbols)
        self.id = self._ensure()

    def _ensure(self) -> int:
        """The active wallet; a new one (with its DEPOSIT) when none exists or the configured
        initial balance / markets changed (the old one is closed, never rewritten)."""
        with self.db.begin() as c:
            row = c.execute(
                text("SELECT id, initial_usdt, symbols FROM smc_wallets WHERE ended_at IS NULL")
            ).first()
            if (
                row is not None
                and row[1] == self.params.account_usdt
                and sorted(row[2]) == self.symbols
            ):
                return int(row[0])
            if row is not None:
                c.execute(
                    text("UPDATE smc_wallets SET ended_at = now() WHERE id = :i"), {"i": row[0]}
                )
            wid = int(
                c.execute(
                    text(
                        "INSERT INTO smc_wallets (initial_usdt, symbols) VALUES (:a, :s)"
                        " RETURNING id"
                    ),
                    {"a": self.params.account_usdt, "s": self.symbols},
                ).scalar_one()
            )
            c.execute(
                text(
                    "INSERT INTO smc_wallet_ledger (wallet_id, ts, kind, amount, balance_after)"
                    " VALUES (:w, :t, 'DEPOSIT', :a, :a)"
                ),
                {"w": wid, "t": datetime.now(UTC), "a": self.params.account_usdt},
            )
            return wid

    def balance(self, c: Connection) -> Decimal:
        v = c.execute(
            text(
                "SELECT balance_after FROM smc_wallet_ledger WHERE wallet_id = :w"
                " ORDER BY id DESC LIMIT 1"
            ),
            {"w": self.id},
        ).scalar()
        return Decimal(v) if v is not None else self.params.account_usdt

    def active_count(self, c: Connection) -> int:
        return int(
            c.execute(
                text(
                    "SELECT COUNT(*) FROM smc_signals WHERE wallet_id = :w"
                    " AND state IN " + ACTIVE_SQL
                ),
                {"w": self.id},
            ).scalar_one()
        )

    def open_margin(self, c: Connection) -> Decimal:
        v: Decimal = c.execute(
            text(
                "SELECT COALESCE(SUM(margin * COALESCE(qty_open / NULLIF(qty, 0), 1)), 0)"
                " FROM smc_signals WHERE wallet_id = :w AND state IN " + ACTIVE_SQL
            ),
            {"w": self.id},
        ).scalar_one()
        return Decimal(v)

    def size(self, c: Connection, entry: Decimal, risk_unit: Decimal, step: Decimal) -> Size | str:
        """The position for one new signal, or the reason there is none."""
        p = self.params
        bal = self.balance(c)
        if bal <= 0:
            return "WALLET_EMPTY"
        qty = floor_step(bal * p.risk_pct / risk_unit, step)
        free = bal - self.open_margin(c)
        note = None
        if qty * entry / p.max_leverage > free:
            qty = floor_step(max(free, Decimal(0)) * p.max_leverage / entry, step)
            note = "MARGIN_LIMITED"
        if qty <= 0:
            return "NO_FREE_MARGIN" if note else "SIZE_BELOW_STEP"
        notional = qty * entry
        return Size(qty, notional, notional / p.max_leverage, notional / bal, note)

    def book(
        self,
        c: Connection,
        sid: int,
        symbol: str,
        t: Tracked,
        part: Part,
        qty: Decimal,
        costs: Costs,
    ) -> Decimal:
        """Book one exit of signal `sid` (a part of size 0 books nothing)."""
        if part.frac <= 0 or qty <= 0:
            return Decimal(0)
        pnl, fees = part_pnl(t, part, qty, costs)
        bal = self.balance(c) + pnl
        c.execute(
            text(
                "INSERT INTO smc_wallet_ledger (wallet_id, ts, symbol, signal_id, kind, amount,"
                " balance_after, part, fees) VALUES (:w, :t, :s, :i, 'REALIZED_PNL', :a, :b,"
                " :p, :f)"
            ),
            {
                "w": self.id,
                "t": part.at,
                "s": symbol,
                "i": sid,
                "a": pnl,
                "b": bal,
                "p": part.kind,
                "f": fees,
            },
        )
        c.execute(
            text(
                "UPDATE smc_signals SET pnl_usdt = COALESCE(pnl_usdt, 0) + :p,"
                " fees_usdt = COALESCE(fees_usdt, 0) + :f WHERE id = :i"
            ),
            {"p": pnl, "f": fees, "i": sid},
        )
        return pnl
