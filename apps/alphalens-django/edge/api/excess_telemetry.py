# apps/alphalens-django/edge/api/excess_telemetry.py
"""SPY-relative signal telemetry — the scatter + smoother + band aggregation.

Pure functions over ``LadderOutcome`` dict rows (Django-free, unit-testable),
sibling to ``summary.py``. Telemetry / exploratory ONLY:

* Per-trade BENCHMARK-EXCESS (``market_excess_return``), never raw R.
* N-gate is a DISPLAY gate (imported from ``summary``): below it the smoother +
  band (``trend``) are withheld; the raw ``points`` are always returned.
* The uncertainty band is bootstrapped by TICKER cluster so repeated episodes of
  one ticker cannot fake precision.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import Any

from edge.api.summary import N_GATE_THRESHOLD

SMOOTHER_WINDOW = 20
METRIC_NOTE = (
    "per-trade total excess over SPY across each trade's holding window; all "
    "surfaced candidates, not a user-selected portfolio; gross / pre-cost; "
    "in-sample; telemetry / exploratory only — not confirmatory."
)
BENCHMARK_NOTE = (
    "SPY is a broad-market proxy and does not reflect the sector/factor exposures of these names."
)


def _finite(value: Any) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _iso(value: Any) -> str | None:
    """Coerce a matured_at (date / datetime / ISO str) to a ``YYYY-MM-DD`` string."""
    if value is None or value == "":
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()[:10]
    return str(value)[:10]


def _median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _collect_points(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Terminal + plannable + finite-excess rows carrying a matured_at, sorted."""
    raw: list[dict[str, Any]] = []
    for row in rows:
        if not (row.get("plannable") and row.get("terminal")):
            continue
        excess = _finite(row.get("market_excess_return"))
        date = _iso(row.get("matured_at"))
        if excess is None or date is None:
            continue
        hd = _finite(row.get("holding_days_elapsed"))
        raw.append(
            {
                "date": date,
                "excess": excess,
                "ticker": str(row.get("ticker") or ""),
                "holding_days": None if hd is None else int(hd),
            }
        )
    counts: dict[str, int] = {}
    for p in raw:
        counts[p["ticker"]] = counts.get(p["ticker"], 0) + 1
    for p in raw:
        p["episode_repeat"] = counts[p["ticker"]] > 1
    raw.sort(key=lambda p: (p["date"], p["ticker"]))
    return raw


def build_excess_telemetry(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    points = _collect_points(rows)
    n_total = len(points)
    n_effective = len({p["ticker"] for p in points})
    median_hd = _median([p["holding_days"] for p in points if p["holding_days"] is not None])
    gated = n_total < N_GATE_THRESHOLD
    return {
        "benchmark": "SPY",
        "status": "accumulating" if gated else "ok",
        "gate_threshold": N_GATE_THRESHOLD,
        "n_total": n_total,
        "n_effective": n_effective,
        "median_holding_days": median_hd,
        "smoother_window": SMOOTHER_WINDOW,
        "metric_note": METRIC_NOTE,
        "benchmark_note": BENCHMARK_NOTE,
        "points": points,
        "trend": [],  # populated in Task 2 when not gated
    }
