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
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from alphalens_pipeline.feedback.bar_window import (
    ARRIVAL_VWAP_WINDOW_MIN,
    _window_vwap,
)
from alphalens_pipeline.thematic.trade_setup import builder, ladder

# Re-export the canonical stop-ATR-buffer constant so callers do not need
# to reach into the private builder namespace directly.
STOP_ATR_BUFFER_K: float = builder._STOP_ATR_BUFFER

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
            ``{"limit": float, "tag": str}``.
        tp_tranches: Ordered tuple of take-profit dicts, each with at least
            ``{"limit": float, "tag": str}``.
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

    price = float(earliest["o"])
    if math.isnan(price):
        return ArmFill(fill_price=None, fill_ts_ms=None, status="NO_FILL")

    return ArmFill(
        fill_price=price,
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
    if vwap is None or math.isnan(vwap):
        return ArmFill(fill_price=None, fill_ts_ms=None, status="NO_FILL")

    return ArmFill(
        fill_price=float(vwap),
        fill_ts_ms=None,  # VWAP is a composite; no single bar timestamp
        status="OK",
    )


def arm_disaster_stop(
    arm_blended: float,
    atr: float,
    close: float,
    *,
    k: float = STOP_ATR_BUFFER_K,
) -> float:
    """Compute the disaster stop for a given arm's blended entry.

    Applies the standard stop-ATR-buffer below ``arm_blended`` using
    ``builder._jitter_stop``, then re-validates against the −25% disaster
    floor (``arm_blended * builder._DISASTER_FLOOR_FRAC``).  The floor is
    the same guard used in the full builder pipeline so that no arm can
    silently produce a stop that implies more than 25% risk from entry.

    Args:
        arm_blended: Blended entry reference price for the arm.
        atr: Average True Range (absolute price units, must be > 0).
        close: Closing price at setup time (used by ``_jitter_stop`` to
            detect round-ATR multiples that are stop-hunt targets).
        k: ATR buffer multiplier.  Defaults to ``STOP_ATR_BUFFER_K``
            (``builder._STOP_ATR_BUFFER``).

    Returns:
        Hard stop price (always finite; floored at ``arm_blended * 0.75``).
        Returns ``float("nan")`` when any input is NaN or ``atr <= 0``, which
        signals an undefined stop to callers that guard upstream.
    """
    if math.isnan(arm_blended) or math.isnan(atr) or math.isnan(close) or atr <= 0:
        return float("nan")
    raw_stop = arm_blended - k * atr
    jittered = builder._jitter_stop(close, raw_stop, atr)
    floor = arm_blended * builder._DISASTER_FLOOR_FRAC
    return max(jittered, floor)


def build_baseline_arm(trade_setup: Mapping[str, Any]) -> ArmSetup:
    """Verbatim-passthrough control arm wrapping a pre-computed TradeSetup.

    Carries the source setup's ``entry_tiers``, ``tp_tranches``, and
    ``disaster_stop`` directly into an :class:`ArmSetup` without any
    geometry transformation.  The ``status`` is taken from the source's
    parse state (key ``"status"``).

    This arm is the *control* in the entry-model counterfactual: it
    reproduces exactly what the production builder decided, so all other
    arms can be measured relative to it.

    Args:
        trade_setup: Mapping with at minimum the keys ``"status"``,
            ``"entry_tiers"``, ``"tp_tranches"``, and ``"disaster_stop"``.

    Returns:
        :class:`ArmSetup` with ``arm="baseline"`` and geometry from the
        source mapping.
    """
    return ArmSetup(
        arm="baseline",
        status=str(trade_setup.get("status", "UNKNOWN")),
        arm_blended=None,
        disaster_stop=trade_setup.get("disaster_stop"),
        entry_tiers=tuple(trade_setup.get("entry_tiers", ())),
        tp_tranches=tuple(trade_setup.get("tp_tranches", ())),
        geometry_collapsed=False,
    )


def build_narrow_tiers_arm(
    *,
    close: float,
    atr: float,
    mults: tuple[float, ...] = (0.10, 0.175, 0.25),
    min_spacing_mult: float,
    min_stop_dist_mult: float,
) -> ArmSetup:
    """Build a compact dip-buy arm with tiers clustered near the close.

    Candidate tiers are placed at ``close - m*atr`` for each ``m`` in
    ``mults``.  The arm's disaster stop is computed via
    :func:`arm_disaster_stop` using the *deepest* candidate as the blended
    reference (conservative: anchors the stop to the lowest proposed entry).
    Tiers that violate ``min_spacing_mult`` or ``min_stop_dist_mult``
    constraints are dropped by :func:`ladder.build_entry_tiers`; if fewer
    tiers survive than candidates, ``geometry_collapsed`` is set ``True``.

    Args:
        close: Most-recent closing price (> 0).
        atr: Average True Range in absolute price units (> 0).
        mults: ATR offset multipliers for candidate tier placement.
        min_spacing_mult: Minimum gap between adjacent tiers (× ATR).
        min_stop_dist_mult: Minimum distance from each tier to the stop (× ATR).

    Returns:
        :class:`ArmSetup` with ``arm="narrow_tiers"``.  ``status`` is
        ``"NO_STRUCTURE"`` when ``atr <= 0`` or ``close <= 0``; ``"BAD_GEOMETRY"``
        when the −25% floor raises the stop above the min-stop-distance
        (all tiers fail the stop-distance filter after floor enforcement);
        ``"OK"`` otherwise (even when geometry_collapsed is True — collapse
        is informational, not an error).
    """
    if math.isnan(atr) or math.isnan(close) or atr <= 0 or close <= 0:
        return ArmSetup(
            arm="narrow_tiers",
            status="NO_STRUCTURE",
            arm_blended=None,
            disaster_stop=None,
            entry_tiers=(),
            tp_tranches=(),
            geometry_collapsed=False,
        )

    candidates = [(close - m * atr, "narrow") for m in mults]
    # Use the deepest candidate as the blended reference for the stop —
    # conservative: anchors the stop to the furthest-out proposed entry.
    deepest_candidate = min(p for p, _ in candidates)
    stop = arm_disaster_stop(arm_blended=deepest_candidate, atr=atr, close=close)

    chosen = ladder.build_entry_tiers(
        close,
        atr,
        candidates,
        stop,
        min_spacing_mult=min_spacing_mult,
        min_stop_dist_mult=min_stop_dist_mult,
    )

    geometry_collapsed = len(chosen) < len(mults)

    if not chosen:
        # All tiers filtered — the floor-enforced stop left no room.
        return ArmSetup(
            arm="narrow_tiers",
            status="BAD_GEOMETRY",
            arm_blended=None,
            disaster_stop=stop,
            entry_tiers=(),
            tp_tranches=(),
            geometry_collapsed=geometry_collapsed,
        )

    tier_dicts = tuple({"limit": float(p), "tag": tag} for p, tag in chosen)
    arm_blended = sum(t["limit"] for t in tier_dicts) / len(tier_dicts)

    return ArmSetup(
        arm="narrow_tiers",
        status="OK",
        arm_blended=arm_blended,
        disaster_stop=stop,
        entry_tiers=tier_dicts,
        tp_tranches=(),
        geometry_collapsed=geometry_collapsed,
    )


def build_single_at_close_arm(
    *,
    close: float,
    atr: float,
    just_below_mult: float = 0.0,
) -> ArmSetup:
    """Build a single-entry arm placed at (or just below) the closing price.

    Suitable for momentum / breakout setups where the trade rationale is
    "buy at market now" rather than "wait for a dip".  A small
    ``just_below_mult`` can shift the tier fractionally below the close to
    avoid buying into a gap open.

    Args:
        close: Most-recent closing price (> 0).
        atr: Average True Range in absolute price units (> 0).
        just_below_mult: ATR fraction subtracted from ``close`` to place the
            tier.  ``0.0`` (default) = exactly at close.

    Returns:
        :class:`ArmSetup` with ``arm="single_at_close"``.  ``status`` is
        ``"NO_STRUCTURE"`` when ``atr <= 0`` or ``close <= 0``; ``"OK"``
        otherwise.
    """
    if math.isnan(atr) or math.isnan(close) or atr <= 0 or close <= 0:
        return ArmSetup(
            arm="single_at_close",
            status="NO_STRUCTURE",
            arm_blended=None,
            disaster_stop=None,
            entry_tiers=(),
            tp_tranches=(),
            geometry_collapsed=False,
        )

    limit = close - just_below_mult * atr
    stop = arm_disaster_stop(arm_blended=limit, atr=atr, close=close)

    tier_dicts: tuple[Mapping[str, Any], ...] = ({"limit": limit, "tag": "single_at_close"},)

    return ArmSetup(
        arm="single_at_close",
        status="OK",
        arm_blended=limit,
        disaster_stop=stop,
        entry_tiers=tier_dicts,
        tp_tranches=(),
        geometry_collapsed=False,
    )


__all__ = [
    "STOP_ATR_BUFFER_K",
    "ArmFill",
    "ArmSetup",
    "arm_disaster_stop",
    "build_baseline_arm",
    "build_narrow_tiers_arm",
    "build_single_at_close_arm",
    "market_at_arrival_fill",
    "vwap_arrival_fill",
]
