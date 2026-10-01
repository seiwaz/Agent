"""Live chart stream (V5.9): display only, zero trading authority.

The collector publishes the forming M1 (from canonical trades as they arrive), each final
canonical M1 and each post-finalization revision on the Postgres channel `sp2l_live`.
One LISTEN connection per API process fans the events out to Server-Sent-Events clients.
The browser never connects to Tabdeal and computes nothing: every event carries the whole
candle for its minute (identity = symbol + open_time).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

log = logging.getLogger("sp2l.api.live")
CHANNEL = "sp2l_live"
KEEPALIVE_S = 15.0


def psycopg_dsn(sqlalchemy_url: str) -> str:
    return sqlalchemy_url.replace("postgresql+psycopg://", "postgresql://", 1)


class LiveHub:
    def __init__(self, dsn: str, symbol: str) -> None:
        self.dsn = dsn
        self.symbol = symbol
        self.subs: set[asyncio.Queue[str]] = set()
        self._task: asyncio.Task[None] | None = None
        self.connected = False

    def subscribe(self) -> asyncio.Queue[str]:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._listen())
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=5000)
        self.subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        self.subs.discard(q)

    def publish(self, payload: str) -> None:
        for q in list(self.subs):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:  # a stalled client: drop it (it reconnects + snapshot)
                self.subs.discard(q)

    async def _listen(self) -> None:
        import psycopg

        while True:
            try:
                conn = await psycopg.AsyncConnection.connect(self.dsn, autocommit=True)
                async with conn:
                    await conn.execute(f"LISTEN {CHANNEL}")
                    self.connected = True
                    async for n in conn.notifies():
                        try:
                            if json.loads(n.payload).get("symbol") != self.symbol:
                                continue
                        except ValueError:
                            continue
                        self.publish(n.payload)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # DB restart etc.: clients keep their stream, we retry
                log.warning("live LISTEN failed: %r; retrying", e)
            self.connected = False
            await asyncio.sleep(2)


async def sse(
    hub: LiveHub,
    snapshot: Callable[[], Any],
    is_disconnected: Callable[[], Any],
) -> AsyncIterator[str]:
    """Snapshot first (reconnect restores the forming candle), then every event."""
    q = hub.subscribe()  # before the snapshot: nothing between the two is lost
    try:
        yield "retry: 2000\n\n"
        snap = await asyncio.to_thread(snapshot)
        yield f"event: snapshot\ndata: {json.dumps(snap, default=str)}\n\n"
        while True:
            if await is_disconnected():
                break
            try:
                data = await asyncio.wait_for(q.get(), timeout=KEEPALIVE_S)
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            yield f"data: {data}\n\n"
    finally:
        hub.unsubscribe(q)
