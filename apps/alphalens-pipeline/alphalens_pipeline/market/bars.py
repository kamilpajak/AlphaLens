"""Pure arithmetic over minute bars, independent of who fetched them.

The arrival opening-window VWAP is the shared price anchor of two tiers that
must not import each other: the measurement tier (the broker-free population
replay in :mod:`alphalens_pipeline.feedback`) and the selection tier (the
entry-fill primitives in :mod:`alphalens_pipeline.thematic.trade_setup`). It
lives here, infra-side (ADR 0011), so both can read it without either
depending on the other.

Nothing here does I/O. A caller supplies a sequence of Polygon-shaped bar
mappings and a UTC window; the functions return a number or ``None``. The
bar FETCHER itself (and the Polygon client it reaches for) stays in
:mod:`alphalens_pipeline.feedback.bar_window`.

The arithmetic is load-bearing on published numbers: ``window_vwap`` is the
arrival anchor behind ``market_excess_return`` (the /edge headline) and the
population monitor's reference close. Re-ordering a composition in it can
move the last bits of an already-published figure, so edit it only with a
deliberate, registered reason.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from typing import Any

# Opening window (minutes from the session open) over which the arrival /
# horizon VWAP is taken. 30 min damps opening-auction noise vs the single open
# print; cheap to retune (one constant).
ARRIVAL_VWAP_WINDOW_MIN = 30


def window_vwap(
    bars: Sequence[Mapping[str, Any]],
    start: dt.datetime,
    end: dt.datetime,
) -> float | None:
    """Volume-weighted close over bars whose start ``t`` is in ``[start, end)``.

    Returns ``None`` when no bar falls in the window. Degrades to the simple
    mean of closes when total volume is zero (an all-zero-volume thin-name
    window) so a VWAP is still produced rather than a divide-by-zero.
    """
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    pairs: list[tuple[float, float]] = []
    for bar in bars:
        t = bar.get("t")
        close = bar.get("c")
        if t is None or close is None or not (start_ms <= t < end_ms):
            continue
        pairs.append((float(close), float(bar.get("v") or 0.0)))
    if not pairs:
        return None
    total_vol = sum(v for _, v in pairs)
    if total_vol == 0:
        return sum(c for c, _ in pairs) / len(pairs)
    return sum(c * v for c, v in pairs) / total_vol
