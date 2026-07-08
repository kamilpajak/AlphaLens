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
import random
from collections.abc import Iterable, Sequence
from typing import Any

from edge.api.summary import N_GATE_THRESHOLD

SMOOTHER_WINDOW = 20
BOOTSTRAP_ITERS = 500
BOOTSTRAP_SEED = 12345
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


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    rank = pct / 100.0 * (len(sorted_vals) - 1)
    lo_i = int(math.floor(rank))
    hi_i = int(math.ceil(rank))
    if lo_i == hi_i:
        return sorted_vals[lo_i]
    frac = rank - lo_i
    return sorted_vals[lo_i] * (1 - frac) + sorted_vals[hi_i] * frac


def _cluster_bootstrap_ci(
    slice_points: Sequence[dict[str, Any]], *, iters: int, rng: random.Random
) -> tuple[float, float]:
    """95% CI of the slice mean, resampling by TICKER cluster (not by row).

    Groups the slice's excesses by ticker, then each iteration draws len(clusters)
    tickers WITH replacement and pools their rows. Fewer unique tickers -> higher
    between-draw variance -> wider CI (repeated episodes of one ticker cannot fake
    precision). A single-cluster slice yields a zero-width CI (undefined from one
    cluster) — the display gate + n_effective cover that degenerate case.
    """
    clusters: dict[str, list[float]] = {}
    for p in slice_points:
        clusters.setdefault(p["ticker"], []).append(p["excess"])
    keys = list(clusters.keys())
    if not keys:
        return (0.0, 0.0)
    means: list[float] = []
    for _ in range(iters):
        pooled: list[float] = []
        for _ in range(len(keys)):
            pooled.extend(clusters[keys[rng.randrange(len(keys))]])
        means.append(sum(pooled) / len(pooled))
    means.sort()
    return (_percentile(means, 2.5), _percentile(means, 97.5))


def _trailing_trend(
    points: Sequence[dict[str, Any]],
    *,
    window: int = SMOOTHER_WINDOW,
    iters: int = BOOTSTRAP_ITERS,
    seed: int = BOOTSTRAP_SEED,
) -> list[dict[str, Any]]:
    """One {date, mean, lo, hi} per distinct date — trailing-window over ordered pts.

    ``points`` MUST already be sorted by (date, ticker) (as ``_collect_points``
    returns them). For each distinct date d we take the last ``window`` points up to
    and including the last point on d, take their mean, and a ticker-clustered
    bootstrap CI. Deterministic: a single ``random.Random(seed)`` walks all dates.
    """
    rng = random.Random(seed)
    trend: list[dict[str, Any]] = []
    # Map each distinct date to the index of its LAST point in the sorted list.
    # ``points`` is pre-sorted by (date, ticker), so each date's key is inserted at
    # its first occurrence and the dict preserves that insertion order (Python 3.7+)
    # — iterating ``.items()`` below therefore walks the dates chronologically.
    last_index_by_date: dict[str, int] = {}
    for i, p in enumerate(points):
        last_index_by_date[p["date"]] = i
    for date, last_i in last_index_by_date.items():
        window_slice = points[max(0, last_i - window + 1) : last_i + 1]
        excesses = [p["excess"] for p in window_slice]
        mean = sum(excesses) / len(excesses)
        lo, hi = _cluster_bootstrap_ci(window_slice, iters=iters, rng=rng)
        trend.append({"date": date, "mean": mean, "lo": lo, "hi": hi})
    return trend


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
        "trend": [] if gated else _trailing_trend(points),
    }
