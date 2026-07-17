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

import json
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

# Provenance mirror of ``BreakevenLens.preregistered_ref`` for lenses whose
# parameters were written down BEFORE registration. The slim Django image cannot
# import the pipeline registry (workspace doctrine), so the non-None refs are
# mirrored here; drift is pinned by a research-side parity test
# (tests/feedback/test_breakeven_lenses.py::test_django_summary_mirrors_preregistered_refs).
_LENS_PREREGISTERED_REF: dict[str, str] = {
    "be_0p5r_trail0p6": "exit_geometry_2026_06_30 s7 be0.5/trail0.6",
    # One line on purpose: the research-side parity test greps for the exact
    # '"lens_id": "ref"' substring, so the ref must not be paren-wrapped.
    "atr_bracket_1p5": "betlejem5_comparative bezpazery v1 (bracket 1.5xATR, floor 0.6%, ceiling 52w-high)",  # noqa: E501
}


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


def _payoff_ratio(values: Sequence[float]) -> float | None:
    """``|avg_win / avg_loss|`` over wins (``> 0``) / losses (``<= 0``).

    Ported from the exit-geometry diagnostic's ``_expectancy`` (same win/loss
    convention). Degenerate math → ``None``, never ``inf`` (JSON-unsafe): no
    losses, or losses averaging exactly 0, leave the ratio undefined.
    """
    wins = [v for v in values if v > 0]
    losses = [v for v in values if v <= 0]
    if not losses:
        return None
    avg_loss = sum(losses) / len(losses)
    if avg_loss == 0:
        return None
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    return abs(avg_win / avg_loss)


def _breakeven_win_rate(values: Sequence[float]) -> float | None:
    """``|avg_loss| / (avg_win + |avg_loss|)`` — the win rate that breaks even.

    Ported from the exit-geometry diagnostic's ``_expectancy``. ``0.0`` when
    there are wins but no losses; ``None`` when the denominator is not
    positive (empty pool, or wins and losses both averaging 0).
    """
    wins = [v for v in values if v > 0]
    losses = [v for v in values if v <= 0]
    if not wins and not losses:
        return None
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0
    denominator = avg_win + abs(avg_loss)
    if denominator <= 0:
        return None
    return abs(avg_loss) / denominator


def build_validation_base_rate(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """The ``/v1/days`` ``meta.validation.edge_base_rate`` block (honesty context).

    Pool = ``plannable AND terminal AND realized_r finite`` — the SAME pool as
    the edge panel's ``gross_realized_r_n`` (NO_FILL terminals carry a NULL
    ``realized_r`` and drop out), over ALL dates (no window param) so the two
    surfaces agree. N-gated like /edge (memo §3.2): below ``N_GATE_THRESHOLD``
    the statistics are ``None`` — otherwise /v1/days would leak sub-gate means
    /edge refuses to show. ``n_matured`` + ``as_of`` always survive the gate.

    ``as_of`` = max ``matured_at`` among the CONTRIBUTING rows (describes the
    data itself, stable across identical mirror rebuilds); ``None`` for an
    empty pool.
    """
    realized: list[float] = []
    matured_dates: list[Any] = []
    for row in rows:
        if not _is_truthy(row.get("plannable")) or not _is_truthy(row.get("terminal")):
            continue
        value = _finite(row.get("realized_r"))
        if value is None:
            continue
        realized.append(value)
        matured_at = row.get("matured_at")
        if matured_at is not None:
            matured_dates.append(matured_at)

    n_matured = len(realized)
    gated = n_matured < N_GATE_THRESHOLD
    return {
        "n_matured": n_matured,
        "mean_realized_r": None if gated else _mean(realized),
        "payoff_ratio": None if gated else _payoff_ratio(realized),
        "breakeven_win_rate": None if gated else _breakeven_win_rate(realized),
        "as_of": max(matured_dates) if matured_dates else None,
    }


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


def _breakeven_map(row: dict[str, Any]) -> dict[str, Any]:
    """Parse a row's ``breakeven_realized_r_json`` into ``{lens_id: value}``.

    Tolerates the empty-string / None / malformed cases (a non-resolved row), all
    of which yield an empty map. Never raises.
    """
    raw = row.get("breakeven_realized_r_json")
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


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
    # Break-even exit-stop WHAT-IF (terminal only) — realized R per lens_id, parsed
    # from breakeven_realized_r_json. Display-only, in-sample; the registry of lens
    # labels/status lives client-side, so this is keyed by lens_id only.
    breakeven_r: dict[str, list[float]] = field(default_factory=dict)
    # The realized-R baseline the what-if is compared against, restricted PER LENS to
    # exactly the rows that fed that lens's counterfactual (finite break-even R AND
    # finite realized_r). Keeping it same-cohort (not the panel-wide realized mean)
    # guards the "vs realized" comparison against superset drift — a fill carrying
    # realized_r but no break-even value must not lift the baseline. A never-filled
    # NO_FILL counterfactual (break-even value, realized_r None) counts toward the
    # lens n but has no realized outcome here, so this list can be SHORTER than
    # ``breakeven_r[lens_id]``.
    breakeven_realized_baseline: dict[str, list[float]] = field(default_factory=dict)
    # Paired helped/harmed tallies over EXACTLY the baseline cohort above (finite
    # lens R AND finite realized_r): lens R strictly above the row's realized R is
    # "helped", strictly below is "harmed", an exact tie counts to neither — so
    # helped + harmed <= len(breakeven_realized_baseline[lens_id]).
    breakeven_helped: dict[str, int] = field(default_factory=dict)
    breakeven_harmed: dict[str, int] = field(default_factory=dict)
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
    # ``rv`` is the row's OWN realized R — the same value for every lens this row
    # feeds, so it is the shared baseline for each lens_id below.
    for lens_id, value in _breakeven_map(row).items():
        rb = _finite(value)
        if rb is not None:
            acc.breakeven_r.setdefault(lens_id, []).append(rb)
            # Same-cohort realized baseline: pair this lens's counterfactual with
            # the row's realized_r. A never-filled NO_FILL has a break-even value
            # but rv is None → it drops out of the baseline only (still counts in n).
            if rv is not None:
                acc.breakeven_realized_baseline.setdefault(lens_id, []).append(rv)
                # Paired direction tally (strict inequality; a tie feeds neither).
                acc.breakeven_helped.setdefault(lens_id, 0)
                acc.breakeven_harmed.setdefault(lens_id, 0)
                if rb > rv:
                    acc.breakeven_helped[lens_id] += 1
                elif rb < rv:
                    acc.breakeven_harmed[lens_id] += 1


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


def _build_whatif(acc: _Accumulator, *, gated: bool, status: str) -> dict[str, Any]:
    """Break-even what-if panel — per-lens R aggregates keyed by ``lens_id``.

    DISPLAY-ONLY, in-sample counterfactual: the realized headline is untouched and
    these means are recomputed under an alternative exit-stop on the SAME picks.
    N-gated exactly like the edge panel (the per-lens ``n`` survives the gate so the
    UI can show coverage; ``mean_r`` / ``median_r`` are nulled below the gate). The
    lens labels + ``in_sample``/``validated`` status live client-side (the registry),
    so this block is keyed by ``lens_id`` only.

    Coverage note (zen review): a per-lens ``n`` is the count of matured rows that
    carry a finite break-even R, so it can be SMALLER than ``n_matured`` (a NO_FILL
    terminal has no fill and no break-even value). The UI must read lens ``n`` as
    fill-coverage, not as the gate count. ``in_sample`` is block-level ``True`` while
    every registered lens is ``status="in_sample"``; once a lens graduates the SPA
    registry surfaces per-lens status (the slim Django image cannot read the registry).
    """
    lenses = {
        lens_id: {
            "n": len(values),
            "mean_r": None if gated else _mean(values),
            "median_r": None if gated else _median(values),
            # Same-cohort realized baseline (this lens's own contributing rows), so
            # "vs realized" compares the identical picks the counterfactual scored —
            # not the panel-wide gross mean. ``realized_r_baseline_n`` can be < ``n``
            # (never-filled NO_FILL counterfactuals have no realized outcome).
            "realized_r_baseline": None
            if gated
            else _mean(acc.breakeven_realized_baseline.get(lens_id, [])),
            "realized_r_baseline_n": len(acc.breakeven_realized_baseline.get(lens_id, [])),
            # Paired per-row direction counts over the baseline cohort (strict
            # inequality; ties feed neither side). Like the means they reveal the
            # effect's direction, so they are nulled below the N-gate.
            "n_helped": None if gated else acc.breakeven_helped.get(lens_id, 0),
            "n_harmed": None if gated else acc.breakeven_harmed.get(lens_id, 0),
            # Provenance ref for a lens whose parameters were pre-registered in a
            # design memo (null for in-sample-tuned lenses).
            "preregistered_ref": _LENS_PREREGISTERED_REF.get(lens_id),
        }
        for lens_id, values in sorted(acc.breakeven_r.items())
    }
    return {
        "status": status,
        "n_matured": len(acc.excess),
        "threshold": N_GATE_THRESHOLD,
        "in_sample": True,
        "note": (
            "counterfactual: realized R recomputed under an alternative exit-stop on "
            "the SAME picks + price paths; in-sample (tuned on this sample) and NOT "
            "validated — never the realized result."
        ),
        "lenses": lenses,
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
        "whatif": _build_whatif(acc, gated=gated, status=status),
        "deployment": _build_deployment(acc),
        "open_positions": _build_open(acc),
    }
