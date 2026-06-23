"""Non-touch entry fill primitives for the entry-grid counterfactual.

Provides two fill models that do NOT require a live broker order:

``market_at_arrival_fill``
    Simulates a market-on-open order: fills at the open price of the first
    bar inside the session window (``[arrival_open_ms, arrival_close_ms]``).
    Pre-market bars (``t < arrival_open_ms``) and later-session bars
    (``t > arrival_close_ms``) are excluded, keeping the fill PIT-safe.

``vwap_arrival_fill``
    Fills at the volume-weighted average close price over the first
    ``ARRIVAL_VWAP_WINDOW_MIN`` minutes of the session, delegating to the
    canonical :func:`alphalens_pipeline.feedback.bar_window._window_vwap`
    primitive so the arithmetic stays in one place.

Both return an :class:`ArmFill` frozen dataclass.  ``ArmSetup`` is defined
here too (used by Task 3 arm builders) so the two dataclasses share one
canonical module.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from alphalens_pipeline.feedback.bar_window import (
    ARRIVAL_VWAP_WINDOW_MIN,
    _window_vwap,
)

# Tolerance for detecting a late open: if the first in-window bar starts more
# than 2 minutes after the nominal session open, ``late_open`` is set True.
_LATE_OPEN_TOLERANCE_MS: int = 120_000  # 2 minutes in milliseconds


@dataclass(frozen=True)
class ArmFill:
    """Result of a single non-touch fill attempt.

    Attributes:
        fill_price: Execution price, or ``None`` when ``status == "NO_FILL"``.
        fill_ts_ms: Timestamp (epoch ms) of the filled bar, or ``None``.
        late_open: True when the first available bar arrived more than
            ``_LATE_OPEN_TOLERANCE_MS`` after the nominal session open —
            indicating an auction delay or data gap.
        status: ``"OK"`` on success, ``"NO_FILL"`` when no bar falls in
            the requested window.
    """

    fill_price: float | None
    fill_ts_ms: int | None
    late_open: bool = False
    status: str = "OK"


@dataclass(frozen=True)
class ArmSetup:
    """Fully-resolved entry-arm geometry produced by the Task-3 builders.

    Attributes:
        arm: Arm identifier, e.g. ``"dip_buy"`` or ``"breakout"``.
        status: ``"OK"`` or a failure code such as ``"NO_STRUCTURE"``.
        arm_blended: Blended entry reference price for the arm (None on failure).
        disaster_stop: Hard stop price below which the position is closed
            regardless of ladder TTL (None on failure).
        entry_tiers: Ordered tuple of tier dicts, each with at least
            ``{"price": float, "weight": float}``.
        tp_tranches: Ordered tuple of take-profit dicts, each with at least
            ``{"price": float, "weight": float}``.
        geometry_collapsed: True when the computed levels are too compressed
            to be tradeable (entry ≥ tp or stop ≥ entry).
    """

    arm: str
    status: str
    arm_blended: float | None
    disaster_stop: float | None
    entry_tiers: tuple[Mapping[str, Any], ...]
    tp_tranches: tuple[Mapping[str, Any], ...]
    geometry_collapsed: bool = False


def market_at_arrival_fill(
    bars: Sequence[dict[str, Any]],
    *,
    arrival_open_ms: int,
    arrival_close_ms: int,
) -> ArmFill:
    """Simulate a market-at-open fill at session arrival.

    Filters ``bars`` to those with ``arrival_open_ms <= t <= arrival_close_ms``,
    picks the earliest, and returns its open price as the fill.

    Args:
        bars: Sequence of OHLCV bar dicts with keys ``t`` (epoch ms), ``o``,
            ``h``, ``l``, ``c``, ``v``.
        arrival_open_ms: Epoch ms of the session open (RTH start).  Bars
            with ``t < arrival_open_ms`` are treated as pre-market and skipped.
        arrival_close_ms: Epoch ms of the session close.  Bars with
            ``t > arrival_close_ms`` belong to a later session and are skipped.

    Returns:
        :class:`ArmFill` with ``status="OK"`` on success, or ``"NO_FILL"`` when
        no bar falls in ``[arrival_open_ms, arrival_close_ms]``.
    """
    in_window = [
        bar
        for bar in bars
        if bar.get("t") is not None and arrival_open_ms <= int(bar["t"]) <= arrival_close_ms
    ]
    if not in_window:
        return ArmFill(fill_price=None, fill_ts_ms=None, status="NO_FILL")

    earliest = min(in_window, key=lambda b: int(b["t"]))
    first_ts_ms = int(earliest["t"])
    late_open = first_ts_ms > arrival_open_ms + _LATE_OPEN_TOLERANCE_MS

    return ArmFill(
        fill_price=float(earliest["o"]),
        fill_ts_ms=first_ts_ms,
        late_open=late_open,
        status="OK",
    )


def vwap_arrival_fill(
    bars: Sequence[dict[str, Any]],
    *,
    arrival_open_ms: int,
    window_min: int = ARRIVAL_VWAP_WINDOW_MIN,
) -> ArmFill:
    """Fill at the VWAP of the first ``window_min`` minutes of the session.

    Delegates to :func:`alphalens_pipeline.feedback.bar_window._window_vwap`
    so the arithmetic (close-price weighted by volume, zero-volume fallback
    to unweighted mean of closes) stays in one canonical place.

    Note: ``_window_vwap`` uses an exclusive window end ``[start, end)``, so
    bars at exactly ``arrival_open_ms + window_min * 60_000`` are excluded.
    This matches the existing population-monitor VWAP convention.

    Args:
        bars: Sequence of OHLCV bar dicts (same schema as
            :func:`market_at_arrival_fill`).
        arrival_open_ms: Epoch ms of the session open (RTH start).
        window_min: Length of the VWAP window in minutes.  Defaults to
            :data:`alphalens_pipeline.feedback.bar_window.ARRIVAL_VWAP_WINDOW_MIN`.

    Returns:
        :class:`ArmFill` with ``status="OK"`` and ``fill_price`` set to the
        VWAP, or ``status="NO_FILL"`` when ``_window_vwap`` returns ``None``
        (no bars in the window).
    """
    window_end_ms = arrival_open_ms + window_min * 60_000

    # _window_vwap expects datetime objects; convert from epoch ms.
    start_dt = dt.datetime.fromtimestamp(arrival_open_ms / 1000.0, tz=dt.UTC)
    end_dt = dt.datetime.fromtimestamp(window_end_ms / 1000.0, tz=dt.UTC)

    vwap = _window_vwap(bars, start_dt, end_dt)
    if vwap is None:
        return ArmFill(fill_price=None, fill_ts_ms=None, status="NO_FILL")

    return ArmFill(
        fill_price=float(vwap),
        fill_ts_ms=None,  # VWAP is a composite; no single bar timestamp
        status="OK",
    )


__all__ = [
    "ArmFill",
    "ArmSetup",
    "market_at_arrival_fill",
    "vwap_arrival_fill",
]
