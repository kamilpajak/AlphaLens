"""Headless overlay-stats computation for vol-target experiments.

Encapsulates the metrics dict that overlay-bearing experiment scripts
emit per (period, ADV, cost) cell. Pulled into the package proper so:

  - The "Sharpe-improvement vs ungated BASE" success metric mandated by
    ADR 0007 has one canonical implementation, not docstring-only.
  - Per-rebalance turnover (varying with the actual top-N snapshot
    diff at each rebalance) replaces the scalar-mean approximation
    flagged in PR #44 zen review.
  - The dynamic-cost formula stays unit-testable rather than buried in
    an experiment-driver private function.

No engine knowledge: the caller passes a portfolio-returns Series, the
list of per-rebalance top-N snapshots (for turnover), a `VolTargeter`,
and the cost / annualisation parameters. Output is a flat dict with
canonical key names.
"""

from __future__ import annotations

from collections.abc import Sequence

import pandas as pd

from alphalens.backtest.metrics import sharpe
from alphalens.risk_overlay.vol_target import VolTargeter, apply_vol_target

# Headline keys exported from compute_overlay_stats. Locking this list
# keeps the audit-JSON schema stable across overlay-script revisions.
HEADLINE_KEYS: tuple[str, ...] = (
    "n",
    "mean_scale",
    "min_scale",
    "max_scale",
    "sharpe_unscaled_gross",
    "sharpe_unscaled_net",
    "sharpe_scaled_gross",
    "sharpe_scaled_net",
    "sharpe_improvement_net",
    "cost_drag_ann",
)


def per_period_turnover(top_n_snapshots: Sequence[Sequence[str]]) -> pd.Series:
    """Per-rebalance turnover series — names exiting top-N divided by N.

    First snapshot has no predecessor, so its turnover is 0.0 by
    convention. The returned Series uses a positional index 0..len-1;
    callers can re-index onto the calendar of `BacktestReport`
    rebalances if needed.
    """
    snapshots = [frozenset(s) for s in top_n_snapshots]
    if not snapshots:
        return pd.Series([], dtype=float, name="turnover")

    out = [0.0]
    for prev, nxt in zip(snapshots[:-1], snapshots[1:]):
        size = max(len(prev), 1)
        exits = prev - nxt
        out.append(len(exits) / size)
    return pd.Series(out, index=pd.RangeIndex(len(out)), name="turnover")


def dynamic_cost_drag(
    scales: pd.Series,
    base_turnover: pd.Series,
    cost_half_spread_bps: float,
) -> pd.Series:
    """Per-period cost drag for a vol-targeted overlay.

    ``turnover_t = base_turnover_t * scale_t + |scale_t - scale_{t-1}|``
    accounts for both the position-side cost (scaled to current size) and
    the leverage-side cost (rebalancing the multiplier itself). cost_bps
    is single-leg half-spread; round-trip on a turnover unit is one full
    spread, hence the direct multiply.
    """
    if scales.empty:
        return pd.Series([], dtype=float, index=scales.index, name="cost")

    aligned = base_turnover.reindex(scales.index).fillna(0.0)
    scale_changes = scales.diff().abs().fillna(0.0)
    turnover_t = aligned * scales + scale_changes
    return (turnover_t * (cost_half_spread_bps / 10_000.0)).rename("cost")


def compute_overlay_stats(
    *,
    raw_returns: pd.Series,
    targeter: VolTargeter,
    top_n_snapshots: Sequence[Sequence[str]],
    cost_half_spread_bps: float,
    periods_per_year: int,
) -> dict[str, float]:
    """Compute the canonical overlay-vs-base metrics dict.

    Parameters
    ----------
    raw_returns
        Per-rebalance portfolio returns from the ungated BASE
        (`BacktestReport.portfolio_returns`).
    targeter
        Configured `VolTargeter` (target_vol, lookback, max_leverage).
    top_n_snapshots
        Per-rebalance top-N ticker lists, in the same order as
        `raw_returns.index`. Used for true per-period turnover.
    cost_half_spread_bps
        Single-leg half-spread (bps); applied to per-period turnover to
        produce per-period cost drag.
    periods_per_year
        Rebalances per year (e.g. 52 for stride=5 weekly).

    Returns
    -------
    dict with the keys in :data:`HEADLINE_KEYS`. ``n == 0`` when
    ``raw_returns`` is empty (caller can short-circuit).
    """
    if raw_returns.empty:
        return {**dict.fromkeys(HEADLINE_KEYS, 0.0), "n": 0}

    base_turnover = per_period_turnover(top_n_snapshots)
    base_turnover.index = raw_returns.index[: len(base_turnover)]

    scales = targeter.scale_series(raw_returns)
    scaled_gross = apply_vol_target(raw_returns, targeter)

    # Costs at scale=1.0 (BASE) and at the actual scaling profile.
    base_unit_scales = pd.Series(1.0, index=raw_returns.index, name="scale")
    base_cost = dynamic_cost_drag(base_unit_scales, base_turnover, cost_half_spread_bps)
    scaled_cost = dynamic_cost_drag(scales, base_turnover, cost_half_spread_bps)

    unscaled_net = raw_returns - base_cost.reindex(raw_returns.index).fillna(0.0)
    scaled_net = scaled_gross - scaled_cost.reindex(scaled_gross.index).fillna(0.0)

    sharpe_un_gross = sharpe(raw_returns.tolist(), periods_per_year=int(periods_per_year))
    sharpe_un_net = sharpe(unscaled_net.tolist(), periods_per_year=int(periods_per_year))
    sharpe_sc_gross = sharpe(scaled_gross.tolist(), periods_per_year=int(periods_per_year))
    sharpe_sc_net = sharpe(scaled_net.tolist(), periods_per_year=int(periods_per_year))

    return {
        "n": len(raw_returns),
        "mean_scale": float(scales.mean()),
        "min_scale": float(scales.min()),
        "max_scale": float(scales.max()),
        "sharpe_unscaled_gross": float(sharpe_un_gross),
        "sharpe_unscaled_net": float(sharpe_un_net),
        "sharpe_scaled_gross": float(sharpe_sc_gross),
        "sharpe_scaled_net": float(sharpe_sc_net),
        "sharpe_improvement_net": float(sharpe_sc_net - sharpe_un_net),
        "cost_drag_ann": float(scaled_cost.mean() * periods_per_year),
    }
