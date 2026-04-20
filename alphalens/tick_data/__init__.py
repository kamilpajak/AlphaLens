"""Tick-level trade data from Polygon, cached as parquet.

Used by per-ticker cost model validation (spread estimator calibration)
and — when the block-trade monitor ships (issue #2) — daily dark-pool
aggregation.

Cache layout: `~/.alphalens/tick_samples/{TICKER}/{YYYY-MM-DD}.parquet`.
Idempotent fetch: `TickLoader` skips days already on disk unless
`force=True` is passed.
"""

from .loader import TickLoader
from .store import TickStore

__all__ = ["TickLoader", "TickStore"]
