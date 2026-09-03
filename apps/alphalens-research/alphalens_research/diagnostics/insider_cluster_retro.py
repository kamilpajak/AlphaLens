"""Insider-cluster retrospective helpers (pre-registered estimation stage).

Spec: ``docs/research/preregistration/params_insider_cluster_retro_2026_09.json``
and ``docs/research/insider_cluster_retro_design_2026_09_03.md`` (LOCKED).

Pure functions over pandas frames so every rule of the frozen spec is unit
tested in isolation: leg qualification, cluster detection (the signal is the
COMPLETION of the cluster — the filing of the second distinct insider), the
arrival rule keyed on the EDGAR acceptance time, the open-to-close event CAR
with the split guard, nearest-neighbour control matching, and the paired
difference with an arrival-session cluster bootstrap. The runner script only
orchestrates I/O and caching.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
from alphalens_pipeline.paper.calendar import (
    DEFAULT_EXCHANGE,
    advance_trading_sessions,
    session_on_or_after,
)

# Frozen spec constants (mirrored in the params JSON; a test pins parity).
LEG_MIN_USD = 10_000.0
CLUSTER_MIN_USD = 100_000.0
CLUSTER_MIN_USD_CHECK = 250_000.0
CLUSTER_WINDOW_SESSIONS = 2
CLUSTER_MIN_INSIDERS = 2
DEDUP_SESSIONS = 20
HORIZON_SESSIONS_PRIMARY = 19  # arrival + 19 = 20 sessions inclusive
HORIZON_SESSIONS_SECONDARY = 39
SPLIT_RATIO_LO, SPLIT_RATIO_HI = 0.55, 1.8
PRE_OPEN_CUTOFF_ET = dt.time(9, 0)
MATCH_VARS = ("ret_20d", "ret_6m", "vol_20d", "log_dv_20d")
CONTROLS_PER_EVENT = 5
CALIPER_SD = 1.5
FEE_ROUND_TRIP = 0.0066  # Saxo LIVE schedule: 0.08%/side + 0.25% FX each way on a fixed ticket
PLANNING_BOUND = -0.005  # lower 90% CI bound, net of fees


def qualifying_legs(form4: pd.DataFrame, *, leg_min_usd: float = LEG_MIN_USD) -> pd.DataFrame:
    """Open-market purchase legs by officers/directors, priced, >= ``leg_min_usd``.

    Ten-percent owners count only if they are also an officer or director.
    Amendments are dropped (the original filing already carried the leg).
    """
    df = form4
    mask = (
        (df["transaction_code"] == "P")
        & (df["acquired_disposed"] == "A")
        & (~df["is_amendment"].fillna(False).astype(bool))
        & (
            df["is_officer"].fillna(False).astype(bool)
            | df["is_director"].fillna(False).astype(bool)
        )
        & df["transaction_price_per_share"].notna()
        & df["transaction_shares"].notna()
    )
    out = df.loc[mask].copy()
    out["usd"] = out["transaction_shares"].astype(float) * out[
        "transaction_price_per_share"
    ].astype(float)
    out = out[out["usd"] >= leg_min_usd].copy()  # explicit copy after the filter
    out["filed_date"] = pd.to_datetime(out["filed_date"]).dt.date
    out["transaction_date"] = pd.to_datetime(out["transaction_date"]).dt.date
    return out.reset_index(drop=True)


def detect_clusters(
    legs: pd.DataFrame,
    *,
    window_sessions: int = CLUSTER_WINDOW_SESSIONS,
    min_insiders: int = CLUSTER_MIN_INSIDERS,
    min_usd: float = CLUSTER_MIN_USD,
    dedup_sessions: int = DEDUP_SESSIONS,
    exchange: str = DEFAULT_EXCHANGE,
) -> pd.DataFrame:
    """One row per cluster event: the first session at which the condition is observable.

    A cluster is >= ``min_insiders`` DISTINCT ``reporting_owner_cik`` whose legs
    were filed within ``window_sessions`` trading sessions of the anchor leg and
    whose USD sum is >= ``min_usd``. ``event_date`` is the ``filed_date`` of the
    leg that completes the insider count (the k-th distinct insider). One event
    per ticker per ``dedup_sessions``; the first wins.
    """
    if legs.empty:
        return pd.DataFrame(
            columns=[
                "ticker",
                "event_date",
                "first_leg_date",
                "n_insiders",
                "cluster_usd",
                "completing_accession",
            ]
        )
    per = (
        legs.groupby(["ticker", "reporting_owner_cik", "filed_date"], as_index=False)
        .agg(
            usd=("usd", "sum"),
            accession_number=("accession_number", "first"),
            transaction_date=("transaction_date", "min"),
        )
        .sort_values(["ticker", "filed_date"])
    )
    rows = []
    for ticker, grp in per.groupby("ticker", sort=False):
        g = grp.sort_values("filed_date").reset_index(drop=True)
        last_event: dt.date | None = None
        for i in range(len(g)):
            d0 = g.filed_date[i]
            hi = advance_trading_sessions(
                session_on_or_after(d0, exchange), window_sessions, exchange
            )
            w = g[(g.filed_date >= d0) & (g.filed_date <= hi)]
            if w.reporting_owner_cik.nunique() < min_insiders or w.usd.sum() < min_usd:
                continue
            firsts = w.groupby("reporting_owner_cik").filed_date.min().sort_values()
            ev_date = firsts.iloc[min_insiders - 1]
            # first-wins dedup: a completion inside [last_event, last_event + dedup_sessions]
            # (inclusive) is the same episode; the re-anchoring of the same cluster at its
            # later legs lands here too.
            if last_event is not None and ev_date <= advance_trading_sessions(
                session_on_or_after(last_event, exchange), dedup_sessions, exchange
            ):
                continue
            last_event = ev_date
            completing = w[w.filed_date == ev_date].iloc[0]
            rows.append(
                {
                    "ticker": ticker,
                    "event_date": ev_date,
                    "first_leg_date": d0,
                    "n_insiders": int(w.reporting_owner_cik.nunique()),
                    "cluster_usd": float(w.usd.sum()),
                    "completing_accession": completing.accession_number,
                    "completing_transaction_date": completing.transaction_date,
                }
            )
    return pd.DataFrame(rows)


def arrival_session(
    filed_date: dt.date, acceptance_et: dt.datetime | None, *, exchange: str = DEFAULT_EXCHANGE
) -> dt.date:
    """First session a follower can trade after the filing became public.

    Accepted before 09:00 ET -> the filing-date session's OPEN is obtainable;
    anything else (intraday, post-close, or unknown) -> next session OPEN.
    """
    base = session_on_or_after(filed_date, exchange)
    if (
        acceptance_et is not None
        and acceptance_et.date() == filed_date
        and acceptance_et.time() < PRE_OPEN_CUTOFF_ET
    ):
        return base
    return advance_trading_sessions(base, 1, exchange)


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
