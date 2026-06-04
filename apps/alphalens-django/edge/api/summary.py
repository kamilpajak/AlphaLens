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


def build_edge_summary(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Build the N-gated, benchmark-relative edge summary from store rows.

    ``rows`` are plain dicts with the LadderOutcome field names. Returns the
    response payload (see ``serializers.EdgeSummarySerializer``).
    """
    n_brief = 0
    n_plannable = 0

    # Edge layer (terminal only, equal-weight, size-free).
    excess: list[float] = []  # market_excess_return (HEADLINE)
    realized_r: list[float] = []  # gross / risk-normalised (de-emphasised)
    holding_days: list[float] = []

    # Portfolio / size layer (terminal only).
    contributions: list[float] = []
    realized_risk_pcts: list[float] = []
    risk_weighted_r_num = 0.0
    risk_weighted_r_den = 0.0
    tiers_filled: list[float] = []

    # Deployment (N-independent) — over the whole plannable population.
    n_filled = 0
    n_no_fill = 0
    n_terminal = 0

    # Open distribution (descriptive only — never a scalar mean).
    n_open = 0
    open_near_tp = 0
    open_near_sl = 0

    for row in rows:
        n_brief += 1
        if not _is_truthy(row.get("plannable")):
            continue
        n_plannable += 1

        if _is_truthy(row.get("terminal")):
            n_terminal += 1
            classification = str(row.get("ladder_classification") or "")
            if classification == "NO_FILL":
                n_no_fill += 1
            if classification in _FILLED_TERMINAL:
                n_filled += 1

            ex = _finite(row.get("market_excess_return"))
            if ex is not None:
                excess.append(ex)
            rv = _finite(row.get("realized_r"))
            if rv is not None:
                realized_r.append(rv)
            hd = _finite(row.get("holding_days_elapsed"))
            if hd is not None:
                holding_days.append(hd)
            contrib = _finite(row.get("realized_return_pct_of_book"))
            if contrib is not None:
                contributions.append(contrib)
            risk = _finite(row.get("realized_risk_pct"))
            if risk is not None:
                realized_risk_pcts.append(risk)
                if rv is not None:
                    risk_weighted_r_num += rv * risk
                    risk_weighted_r_den += risk
            tfc = _finite(row.get("tiers_filled_count"))
            if tfc is not None:
                tiers_filled.append(tfc)
        else:
            # Ongoing — descriptive distribution only (§3.3).
            n_open += 1
            ov = _finite(row.get("open_r"))
            if ov is not None and ov > 0:
                open_near_tp += 1
            elif ov is not None and ov < 0:
                open_near_sl += 1

    # The N-gate is keyed on the matured (terminal) edge count. Use the count of
    # excess values when available, else the realized_r count, so the gate
    # reflects how many matured rows actually carry the headline metric.
    n_matured_excess = len(excess)
    n_matured_realized = len(realized_r)
    n_terminal_total = n_terminal

    gated = n_matured_excess < N_GATE_THRESHOLD
    early = (not gated) and n_matured_excess < N_EARLY_THRESHOLD

    # Stable shape: the panels ALWAYS carry the same keys. When gated, the
    # statistic fields are ``None`` (the frontend branches on ``status``); the
    # N-gate hides the numbers by nulling them, never by dropping the keys.
    status = "insufficient" if gated else ("early" if early else "ok")
    quantiles_null = {"p10": None, "p50": None, "p90": None}
    edge: dict[str, Any] = {
        "status": status,
        "n_matured": n_matured_excess,
        "threshold": N_GATE_THRESHOLD,
        # HEADLINE — benchmark-excess (gross, pre-cost), return units.
        "market_excess_mean": None if gated else _mean(excess),
        "market_excess_median": None if gated else _median(excess),
        "market_excess_quantiles": quantiles_null if gated else _quantiles(excess),
        # De-emphasised gross / risk-normalised R (NOT the headline).
        "gross_realized_r_mean": None if gated else _mean(realized_r),
        "gross_realized_r_median": None if gated else _median(realized_r),
        "gross_realized_r_n": n_matured_realized,
        "holding_days_n": len(holding_days),
        "holding_days_p50": None if gated else _percentile(holding_days, 50.0),
        "holding_days_p95": None if gated else _percentile(holding_days, 95.0),
        "gross_of_cost": True,
        "regime_stratified": False,
    }
    portfolio: dict[str, Any] = {
        "status": status,
        "n_matured": n_matured_excess,
        "threshold": N_GATE_THRESHOLD,
        "total_realized_contribution_pct_of_book": (
            None if gated else (sum(contributions) if contributions else None)
        ),
        "size_weighted_realized_r": (
            None
            if gated
            else (risk_weighted_r_num / risk_weighted_r_den if risk_weighted_r_den > 0 else None)
        ),
        "mean_realized_risk_pct": None if gated else _mean(realized_risk_pcts),
        "mean_tiers_filled_count": None if gated else _mean(tiers_filled),
        "gross_of_cost": True,
    }

    # Deployment block — ALWAYS returned (N-independent).
    deployment = {
        "n_terminal": n_terminal_total,
        "n_filled": n_filled,
        "n_no_fill": n_no_fill,
        "fill_rate": (n_filled / n_terminal_total) if n_terminal_total > 0 else None,
        "no_fill_rate": (n_no_fill / n_terminal_total) if n_terminal_total > 0 else None,
        "mean_tiers_filled_count": _mean(tiers_filled),
    }

    open_positions = {
        "n_open": n_open,
        "near_tp": open_near_tp,
        "near_sl": open_near_sl,
        "note": "descriptive only — excluded from expectancy (memo §3.3)",
    }

    return {
        "n_brief": n_brief,
        "n_plannable": n_plannable,
        "n_terminal": n_terminal_total,
        "n_matured": n_matured_excess,
        "n_gate_threshold": N_GATE_THRESHOLD,
        "benchmark": "SPY",
        "metric_note": (
            "market_excess_return = forward_return − benchmark_window_return "
            "(same window, raw return units); gross / pre-cost; telemetry / "
            "exploratory only — not confirmatory."
        ),
        "edge": edge,
        "portfolio": portfolio,
        "deployment": deployment,
        "open_positions": open_positions,
    }
