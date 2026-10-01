"""M5 indicators computed over one continuous, anchored series of finalized candles.

Series conventions:
- index 0 is the anchor bar (the first bar of the continuous history);
- `None` = not yet initialized (warmup);
- `UNKNOWN` = mathematically undefined (zero denominator). Undefined is fail-closed:
  in a recursive indicator every later value is UNKNOWN as well, until the caller
  re-anchors. The recovery policy is BLOCKER B06 and is intentionally not invented here.
"""

from __future__ import annotations

from enum import Enum


class _Unknown(Enum):
    UNKNOWN = "UNKNOWN"

    def __repr__(self) -> str:
        return "UNKNOWN"


UNKNOWN = _Unknown.UNKNOWN
Unknown = _Unknown
