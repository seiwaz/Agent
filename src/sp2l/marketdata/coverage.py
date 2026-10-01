"""Proven feed coverage per connection and merged across connections (V5.5 B46 test).

Each connection contributes closed intervals [coverage_from, healthy_until] of *proven*
coverage (pong-based, see collector.py). The merged coverage is the union of all intervals
of all connections. A span is covered only if one merged interval contains it entirely;
anything else is uncovered (DATA_GAP). Nothing here weakens the single-connection rule:
with one connection the union is exactly that connection's proven coverage.
"""

from __future__ import annotations

from datetime import datetime, timedelta


class Intervals:
    """Closed intervals of proven coverage for one connection (append-only, pruned)."""

    def __init__(self) -> None:
        self.spans: list[list[datetime]] = []

    def open(self, start: datetime) -> None:
        self.spans.append([start, start])

    def extend(self, until: datetime) -> None:
        if self.spans and until > self.spans[-1][1]:
            self.spans[-1][1] = until

    def prune(self, before: datetime) -> None:
        self.spans = [s for s in self.spans if s[1] >= before] or self.spans[-1:]


class MergedCoverage:
    def __init__(self) -> None:
        self.sources: dict[str, Intervals] = {}

    def source(self, name: str) -> Intervals:
        return self.sources.setdefault(name, Intervals())

    def merged(self) -> list[tuple[datetime, datetime]]:
        spans = sorted((s[0], s[1]) for src in self.sources.values() for s in src.spans)
        out: list[tuple[datetime, datetime]] = []
        for a, b in spans:
            if out and a <= out[-1][1]:
                if b > out[-1][1]:
                    out[-1] = (out[-1][0], b)
            else:
                out.append((a, b))
        return out

    def covers(self, start: datetime, end: datetime) -> bool:
        return any(a <= start and end <= b for a, b in self.merged())

    def covers_point(self, t: datetime) -> bool:
        return self.covers(t, t)

    def component_start(self, t: datetime) -> datetime | None:
        """Start of the merged interval containing t (post-gap anchor, B34)."""
        return next((a for a, b in self.merged() if a <= t <= b), None)

    def end(self) -> datetime | None:
        """End of the latest merged interval: the merged proven watermark."""
        m = self.merged()
        return m[-1][1] if m else None

    def prune(self, before: datetime) -> None:
        for src in self.sources.values():
            src.prune(before - timedelta(minutes=1))
