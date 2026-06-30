"""Edge-summary aggregation — the N-gated, benchmark-relative roll-up (memo §3).

Pure functions over ``LadderOutcome`` rows (dicts). Kept Django-free so they are
unit-testable without ``django.setup()`` and so the binding guardrails are pinned
in one place:

* §3.1 — headline central tendency is the BENCHMARK-EXCESS return
  (``market_excess_return``), NOT raw R. Raw ``realized_r`` is reported only as a
  de-emphasised "gross / risk-normalised" mean.
* §3.2 — hard N-gate: if ``n_matured < N_GATE_THRESHOLD`` the edge + portfolio
  aggregates are ``None`` ("insufficient"), never computed means. ``30 ≤ n < 100``
  is flagged "early / high-variance".
* §3.3 — open positions are a DESCRIPTIVE distribution (counts near-TP / near-SL),
  never a scalar open-R mean; matured and open are never pooled.
* §3.4 — every R / excess number is gross / pre-cost.
* §3.5 — no t-stats / SEs; means + median + 10/50/90 quantiles + N only.
* §3.7 — distributional: quantiles alongside the mean.

The DEPLOYMENT block (fill-rate, mean tiers filled, NO_FILL %) is N-INDEPENDENT
(it describes the population's mechanics, not its edge) and is ALWAYS returned.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

# Hard N-gate threshold (memo §3.2). Below this, edge + portfolio aggregates are
# hidden behind an "insufficient" verdict.
N_GATE_THRESHOLD = 30
# Above the gate but below this, the aggregates are flagged "early / high-variance".
N_EARLY_THRESHOLD = 100

# Classifications that mean a position was opened (NO_FILL / BAD_GEOMETRY did not
# deploy capital). Used for the deployment fill-rate.
_FILLED_TERMINAL = frozenset({"TP_FULL", "SL_HIT", "PARTIAL_TP_THEN_SL", "TIME_STOP"})


def _finite(value: Any) -> float | None:
    """Return ``float(value)`` when finite, else ``None`` (drops NaN / inf / None)."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _mean(values: Sequence[float]) -> float | None:
    return (sum(values) / len(values)) if values else None


def _hit_rate(values: Sequence[float]) -> float | None:
    """Share of matured names with a STRICTLY positive market-excess return.

    Breadth of the edge (how OFTEN we beat the benchmark) alongside its average
    size; equal-weight, size-free, same arrival→exit window as ``excess``. A flat
    ``0.0`` excess is not a hit (strict ``> 0``). ``None`` for an empty window.
    """
    return (sum(1 for v in values if v > 0) / len(values)) if values else None


def _median(values: Sequence[float]) -> float | None:
    return _percentile(values, 50.0)


def _percentile(values: Sequence[float], pct: float) -> float | None:
    """Nearest-rank percentile (same convention as the monitor's roll-up)."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def _quantiles(values: Sequence[float]) -> dict[str, float | None]:
    return {
        "p10": _percentile(values, 10.0),
        "p50": _percentile(values, 50.0),
        "p90": _percentile(values, 90.0),
    }


def _is_truthy(value: Any) -> bool:
    return bool(value) and value is not None


@dataclass
class _Accumulator:
    """Single pass over the rows accumulates here (memo §3 layers).

    Split out of ``build_edge_summary`` so the loop body's branching does not
    pile its cognitive complexity onto the orchestrator.
    """

    n_brief: int = 0
    n_plannable: int = 0
    # Edge layer (terminal only, equal-weight, size-free).
    excess: list[float] = field(default_factory=list)  # market_excess_return (HEADLINE)
    realized_r: list[float] = field(default_factory=list)  # gross/risk-norm (de-emphasised)
    holding_days: list[float] = field(default_factory=list)
    # Portfolio / size layer (terminal only) — per-name risk geometry only. The
    # shared-book aggregates (summed contribution, risk-weighted mean R) were
    # removed: each member sizes independently, so no single capital book exists
    # for this tool (ADR 0012).
    realized_risk_pcts: list[float] = field(default_factory=list)
    tiers_filled: list[float] = field(default_factory=list)
    # Deployment (N-independent) — over the whole plannable population.
    n_filled: int = 0
    n_no_fill: int = 0
    n_terminal: int = 0
    # Open distribution (descriptive only — never a scalar mean).
    n_open: int = 0
    open_near_tp: int = 0
    open_near_sl: int = 0


def _accumulate_terminal(acc: _Accumulator, row: dict[str, Any]) -> None:
    """Fold one terminal (matured) row into the edge / portfolio / deployment layers."""
    acc.n_terminal += 1
    classification = str(row.get("ladder_classification") or "")
    if classification == "NO_FILL":
        acc.n_no_fill += 1
    if classification in _FILLED_TERMINAL:
        acc.n_filled += 1

    ex = _finite(row.get("market_excess_return"))
    if ex is not None:
        acc.excess.append(ex)
    rv = _finite(row.get("realized_r"))
    if rv is not None:
        acc.realized_r.append(rv)
    hd = _finite(row.get("holding_days_elapsed"))
    if hd is not None:
        acc.holding_days.append(hd)
    risk = _finite(row.get("realized_risk_pct"))
    if risk is not None:
        acc.realized_risk_pcts.append(risk)
    tfc = _finite(row.get("tiers_filled_count"))
    if tfc is not None:
        acc.tiers_filled.append(tfc)


def _accumulate_open(acc: _Accumulator, row: dict[str, Any]) -> None:
    """Fold one ongoing row into the descriptive open distribution (§3.3).

    A pending-fill candidate (entry order still live, no tier filled) has deployed
    zero capital and carries no ``open_r`` mark — it is NOT an open position and
    must not inflate ``n_open``. The monitor marks these non-terminal rows with
    ``ladder_classification == "NO_FILL"`` and a NaN ``open_r``; gating on a finite
    ``open_r`` (the live mark) is the exact discriminator (edge-data audit
    2026-06-18). ``n_open`` thus counts markable open positions; ``near_tp`` /
    ``near_sl`` are its directional sub-split (a flat ``open_r == 0`` mark is open
    but in neither bucket, so ``near_tp + near_sl <= n_open``).
    """
    ov = _finite(row.get("open_r"))
    if ov is None:
        return
    acc.n_open += 1
    if ov > 0:
        acc.open_near_tp += 1
    elif ov < 0:
        acc.open_near_sl += 1


def _accumulate(rows: Iterable[dict[str, Any]]) -> _Accumulator:
    acc = _Accumulator()
    for row in rows:
        acc.n_brief += 1
        if not _is_truthy(row.get("plannable")):
            continue
        acc.n_plannable += 1
        if _is_truthy(row.get("terminal")):
            _accumulate_terminal(acc, row)
        else:
            _accumulate_open(acc, row)
    return acc


def _gate_status(*, gated: bool, early: bool) -> str:
    """Map the N-gate flags to the panel status string."""
    if gated:
        return "insufficient"
    return "early" if early else "ok"


def _build_edge(acc: _Accumulator, *, gated: bool, status: str) -> dict[str, Any]:
    """Edge panel — stable keys; when gated the statistic fields are nulled."""
    quantiles_null = {"p10": None, "p50": None, "p90": None}
    return {
        "status": status,
        "n_matured": len(acc.excess),
        "threshold": N_GATE_THRESHOLD,
        # HEADLINE — benchmark-excess (gross, pre-cost), return units.
        "market_excess_mean": None if gated else _mean(acc.excess),
        "market_excess_median": None if gated else _median(acc.excess),
        "market_excess_quantiles": quantiles_null if gated else _quantiles(acc.excess),
        # Breadth — share of matured names that beat the benchmark (strict > 0).
        "hit_rate": None if gated else _hit_rate(acc.excess),
        # De-emphasised gross / risk-normalised R (NOT the headline).
        "gross_realized_r_mean": None if gated else _mean(acc.realized_r),
        "gross_realized_r_median": None if gated else _median(acc.realized_r),
        "gross_realized_r_n": len(acc.realized_r),
        "holding_days_n": len(acc.holding_days),
        "holding_days_p50": None if gated else _percentile(acc.holding_days, 50.0),
        "holding_days_p95": None if gated else _percentile(acc.holding_days, 95.0),
        "gross_of_cost": True,
        "regime_stratified": False,
    }


def _build_portfolio(acc: _Accumulator, *, gated: bool, status: str) -> dict[str, Any]:
    """Portfolio / size panel — per-name risk geometry only.

    The shared-book aggregates (summed contribution, risk-weighted mean R) were
    removed: each member sizes independently, so a single shared capital book
    never existed for this tool (ADR 0012). Only the per-name suggested risk and
    tiers-filled remain. Statistic fields are nulled when gated.
    """
    return {
        "status": status,
        "n_matured": len(acc.excess),
        "threshold": N_GATE_THRESHOLD,
        "mean_realized_risk_pct": None if gated else _mean(acc.realized_risk_pcts),
        "mean_tiers_filled_count": None if gated else _mean(acc.tiers_filled),
        "gross_of_cost": True,
    }


def _build_deployment(acc: _Accumulator) -> dict[str, Any]:
    """Deployment block — ALWAYS returned (N-independent)."""
    n = acc.n_terminal
    return {
        "n_terminal": n,
        "n_filled": acc.n_filled,
        "n_no_fill": acc.n_no_fill,
        "fill_rate": (acc.n_filled / n) if n > 0 else None,
        "no_fill_rate": (acc.n_no_fill / n) if n > 0 else None,
        "mean_tiers_filled_count": _mean(acc.tiers_filled),
    }


def _build_open(acc: _Accumulator) -> dict[str, Any]:
    return {
        "n_open": acc.n_open,
        "near_tp": acc.open_near_tp,
        "near_sl": acc.open_near_sl,
        "note": "descriptive only — excluded from expectancy (memo §3.3)",
    }


def build_edge_summary(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Build the N-gated, benchmark-relative edge summary from store rows.

    ``rows`` are plain dicts with the LadderOutcome field names. Returns the
    response payload (see ``serializers.EdgeSummarySerializer``).
    """
    acc = _accumulate(rows)

    # The N-gate is keyed on the matured (terminal) edge count — how many matured
    # rows actually carry the headline market-excess metric.
    n_matured = len(acc.excess)
    gated = n_matured < N_GATE_THRESHOLD
    early = (not gated) and n_matured < N_EARLY_THRESHOLD
    status = _gate_status(gated=gated, early=early)

    return {
        "n_brief": acc.n_brief,
        "n_plannable": acc.n_plannable,
        "n_terminal": acc.n_terminal,
        "n_matured": n_matured,
        "n_gate_threshold": N_GATE_THRESHOLD,
        "benchmark": "SPY",
        "metric_note": (
            "market_excess_return = forward_return − benchmark_window_return "
            "(same window, raw return units); gross / pre-cost; telemetry / "
            "exploratory only — not confirmatory."
        ),
        "edge": _build_edge(acc, gated=gated, status=status),
        "portfolio": _build_portfolio(acc, gated=gated, status=status),
        "deployment": _build_deployment(acc),
        "open_positions": _build_open(acc),
    }
