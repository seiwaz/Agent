"""Indicators of the playbook, computed on closed bars only (index i uses bars <= i).

None where an indicator is not defined yet (its warm-up)."""

from __future__ import annotations

from collections.abc import Sequence

Series = list[float | None]


def ema(xs: Sequence[float], n: int) -> Series:
    """Exponential moving average, seeded with the simple average of the first n values."""
    out: Series = [None] * len(xs)
    if len(xs) < n or n < 1:
        return out
    k = 2.0 / (n + 1)
    v = sum(xs[:n]) / n
    out[n - 1] = v
    for i in range(n, len(xs)):
        v = xs[i] * k + v * (1 - k)
        out[i] = v
    return out


def true_range(h: Sequence[float], lo: Sequence[float], c: Sequence[float]) -> list[float]:
    return [h[0] - lo[0]] + [
        max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1])) for i in range(1, len(c))
    ]


def wilder(xs: Sequence[float], n: int, start: int = 0) -> Series:
    """Wilder's average: the simple average of the first n values (from `start`), then
    (previous x (n - 1) + x) / n."""
    out: Series = [None] * len(xs)
    if len(xs) - start < n:
        return out
    v = sum(xs[start:start + n]) / n
    out[start + n - 1] = v
    for i in range(start + n, len(xs)):
        v = (v * (n - 1) + xs[i]) / n
        out[i] = v
    return out


def atr(h: Sequence[float], lo: Sequence[float], c: Sequence[float], n: int) -> Series:
    """Wilder ATR(n)."""
    return wilder(true_range(h, lo, c), n)


def adx(h: Sequence[float], lo: Sequence[float], c: Sequence[float], n: int = 14) -> Series:
    """Wilder ADX(n)."""
    m = len(c)
    out: Series = [None] * m
    if m < 2 * n + 1:
        return out
    tr = true_range(h, lo, c)
    pdm = [0.0] * m
    mdm = [0.0] * m
    for i in range(1, m):
        up, dn = h[i] - h[i - 1], lo[i - 1] - lo[i]
        pdm[i] = up if up > dn and up > 0 else 0.0
        mdm[i] = dn if dn > up and dn > 0 else 0.0
    # Wilder sums from bar 1 (bar 0 has no previous close)
    str_, sp, sm = wilder(tr, n, 1), wilder(pdm, n, 1), wilder(mdm, n, 1)
    dx: list[float] = []
    first = None
    for i in range(m):
        a, p, q = str_[i], sp[i], sm[i]
        if a is None or p is None or q is None:
            continue
        if first is None:
            first = i
        pdi, mdi = (100 * p / a, 100 * q / a) if a > 0 else (0.0, 0.0)
        s = pdi + mdi
        dx.append(100 * abs(pdi - mdi) / s if s > 0 else 0.0)
    if first is None:
        return out
    avg = wilder(dx, n)
    for j, v in enumerate(avg):
        out[first + j] = v
    return out


def prev_max(xs: Sequence[float], n: int) -> Series:
    """max(xs[i - n : i]): the highest of the n bars BEFORE i (bar i excluded)."""
    from collections import deque

    out: Series = [None] * len(xs)
    q: deque[int] = deque()
    for i in range(len(xs)):
        while q and q[0] < i - n:
            q.popleft()
        if i >= n:
            out[i] = xs[q[0]]
        while q and xs[q[-1]] <= xs[i]:
            q.pop()
        q.append(i)
    return out


def prev_min(xs: Sequence[float], n: int) -> Series:
    neg = prev_max([-x for x in xs], n)
    return [None if v is None else -v for v in neg]


def pivots(xs: Sequence[float], k: int, high: bool) -> list[int]:
    """Indexes of swing highs (high=True) / lows: beyond the k bars before it and at least
    level with the k bars after it (of equal highs the first is the swing). Pivot p is known
    at the close of bar p + k."""
    out = []
    for p in range(k, len(xs) - k):
        x = xs[p]
        left, right = xs[p - k:p], xs[p + 1:p + k + 1]
        if high and all(x > y for y in left) and all(x >= y for y in right):
            out.append(p)
        elif not high and all(x < y for y in left) and all(x <= y for y in right):
            out.append(p)
    return out


def previous_highs(h: Sequence[float], c: Sequence[float], k: int,
                   window: int) -> list[tuple[float, int] | None]:
    """The previous high known before bar i: the highest swing high (k bars each side) of the
    last `window` bars that no close has gone above since; (price, its bar) or None.

    Unlike a rolling n-bar high it does not drop when an old high leaves a short window: it
    stays until price closes above it (then the next swing high takes over) or it is older
    than `window` bars."""
    pv = pivots(h, k, True)
    nxt = 0
    live: list[int] = []  # unbroken swing highs
    out: list[tuple[float, int] | None] = []
    for i in range(len(c)):
        while nxt < len(pv) and pv[nxt] + k <= i - 1:  # confirmed by the close of bar i - 1
            live.append(pv[nxt])
            nxt += 1
        live = [p for p in live if p >= i - window]
        top = max(live, key=lambda p: (h[p], p)) if live else None
        out.append((h[top], top) if top is not None else None)
        live = [p for p in live if c[i] <= h[p]]  # a close above breaks it
    return out
