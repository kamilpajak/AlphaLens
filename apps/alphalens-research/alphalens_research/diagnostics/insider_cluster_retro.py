"""Insider-cluster retrospective helpers (pre-registered estimation stage).

Spec: ``docs/research/preregistration/params_insider_cluster_retro_2026_09.json``
and ``docs/research/insider_cluster_retro_design_2026_09_03.md`` (LOCKED,
COMPLETE — results in section 13).

The event rules (leg qualification, cluster detection, the acceptance-keyed
arrival rule, the cached acceptance fetch) were PROMOTED to the pipeline tier
(``alphalens_pipeline.events.insider_cluster``, epic #1293) so the live lane
can run inside the VPS image; they are re-exported here so the runner script
and the stage-1 parity test keep their ``icr.*`` names. What stays research-only
is the retrospective's own machinery: the open-to-close event CAR with the
split guard, nearest-neighbour control matching, the paired difference with an
arrival-session cluster bootstrap, and the planning rule.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
from alphalens_pipeline.events.insider_cluster import (  # noqa: F401 — re-exports
    ACCEPT_RE,
    CLUSTER_MIN_INSIDERS,
    CLUSTER_MIN_USD,
    CLUSTER_MIN_USD_CHECK,
    CLUSTER_WINDOW_SESSIONS,
    DEDUP_SESSIONS,
    FEE_ROUND_TRIP,
    HORIZON_SESSIONS_PRIMARY,
    HORIZON_SESSIONS_SECONDARY,
    LEG_MIN_USD,
    PRE_OPEN_CUTOFF_ET,
    SPLIT_RATIO_HI,
    SPLIT_RATIO_LO,
    accession_urls,
    arrival_session,
    detect_clusters,
    fetch_acceptance,
    qualifying_legs,
)
from alphalens_pipeline.paper.calendar import DEFAULT_EXCHANGE, advance_trading_sessions

# Retrospective-only constants (matching + planning rule).
MATCH_VARS = ("ret_20d", "ret_6m", "vol_20d", "log_dv_20d")
CONTROLS_PER_EVENT = 5
CALIPER_SD = 1.5
PLANNING_BOUND = -0.005  # lower 90% CI bound, net of fees


def _row(prices: pd.DataFrame, day: dt.date) -> pd.Series | None:
    ts = pd.Timestamp(day)
    if ts not in prices.index:
        return None
    row = prices.loc[ts]
    # a duplicated index label would return a frame; take its first row
    return row.iloc[0] if isinstance(row, pd.DataFrame) else row


def event_car(
    stock: pd.DataFrame,
    benchmark: pd.DataFrame,
    *,
    arrival: dt.date,
    horizon_sessions: int,
    exchange: str = DEFAULT_EXCHANGE,
) -> float | None:
    """Open(arrival) -> close(arrival + horizon) buy-and-hold, minus the benchmark's.

    ``None`` when any of the four prices is missing/non-positive or the stock's
    day-over-day close ratio inside the window leaves [0.55, 1.8] (split guard).
    """
    horizon = advance_trading_sessions(arrival, horizon_sessions, exchange)
    a, h = _row(stock, arrival), _row(stock, horizon)
    ba, bh = _row(benchmark, arrival), _row(benchmark, horizon)
    if a is None or h is None or ba is None or bh is None:
        return None
    if min(a["open"], h["close"], ba["open"], bh["close"]) <= 0:
        return None
    # NOTE: the slice skips sessions missing from the stock's own index, so a data gap
    # shows up as a jump between consecutive AVAILABLE rows and may trip the guard.
    # That is conservative (gap -> exclusion) and accepted for a survivor-only cache.
    window = stock.loc[pd.Timestamp(arrival) : pd.Timestamp(horizon), "close"]
    ratios = (window / window.shift(1)).dropna()
    if len(ratios) and not ((ratios >= SPLIT_RATIO_LO) & (ratios <= SPLIT_RATIO_HI)).all():
        return None
    return float(h["close"] / a["open"] - 1.0) - float(bh["close"] / ba["open"] - 1.0)


def match_controls(
    treated: pd.Series,
    pool: pd.DataFrame,
    *,
    k: int = CONTROLS_PER_EVENT,
    caliper_sd: float = CALIPER_SD,
    pool_sd: dict[str, float] | None = None,
    match_vars: tuple[str, ...] = MATCH_VARS,
) -> pd.DataFrame:
    """Nearest neighbours in standardized matching space, each variable within the caliper."""
    if pool.empty:
        return pool
    sd = pool_sd or {v: float(pool[v].std(ddof=0)) or 1.0 for v in match_vars}
    z = pd.DataFrame({v: (pool[v] - float(treated[v])) / (sd[v] or 1.0) for v in match_vars})
    within = (z.abs() <= caliper_sd).all(axis=1)
    dist = np.sqrt((z**2).sum(axis=1))
    cand = pool.loc[within].assign(_dist=dist[within]).sort_values("_dist", kind="stable")
    return cand.head(k).drop(columns="_dist").reset_index(drop=True)


def paired_difference_ci(
    d: np.ndarray, clusters: np.ndarray, *, n_boot: int = 9999, seed: int = 0
) -> dict:
    """Mean of per-event differences with a cluster (arrival-session) bootstrap CI."""
    d = np.asarray(d, dtype=float)
    clusters = np.asarray(clusters)
    uniq, inv = np.unique(clusters, return_inverse=True)
    sums = np.bincount(inv, weights=d)
    counts = np.bincount(inv)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(uniq), size=(n_boot, len(uniq)))
    means = sums[idx].sum(axis=1) / counts[idx].sum(axis=1)
    return {
        "mean": float(d.mean()),
        "n": len(d),
        "n_clusters": len(uniq),
        "ci90": (float(np.percentile(means, 5)), float(np.percentile(means, 95))),
        "ci95": (float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))),
        "boot_sd": float(means.std(ddof=1)),
    }


def planning_rule(
    *,
    mean: float,
    ci90_low: float,
    fee_round_trip: float = FEE_ROUND_TRIP,
    bound: float = PLANNING_BOUND,
) -> bool:
    """Build the forward lane iff the net point estimate is positive AND the net lower 90% bound clears ``bound``."""
    return (mean - fee_round_trip) > 0.0 and (ci90_low - fee_round_trip) > bound
