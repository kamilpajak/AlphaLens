"""v9D scoring pipeline extracted for single-asof paper-trade use.

Mirrors ``scripts/experiment_v9_cross_sectional_residual.py`` helpers
(`_pit_union`, `_smd_loader`, scoring + decile selection) but parameterized
for a *single* as-of date — paper-trade's weekly cron picks one day at a
time, not a multi-month grid. The scorer module itself
(`alphalens.screeners.options_implied.cross_sectional_residual`) is
unchanged; this is just adapter code.

Public entry points:
- ``score_top_decile(asof, ...)`` — runs full pipeline for one asof,
  returns top-decile ticker list + scores + universe size
- ``compute_realized_return(holdings, prior_asof, current_asof, ...)`` —
  computes equal-weighted gross return over the holdings between two
  asof dates (used by the weekly cron to mark prior-week P&L)

Both functions accept an injectable ``smd_loader`` for testability.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from alphalens.data.alt_data.ivolatility_smd_cache import load_cached_smd
from alphalens.screeners.options_implied import (
    build_feature_frame,
    score_cross_sectional_residual,
)
from alphalens.screeners.options_implied.target import forward_raw_return

logger = logging.getLogger(__name__)

DEFAULT_PIT_DIR = Path.home() / ".alphalens" / "pit_universe"
DEFAULT_SMD_CACHE_DIR = Path.home() / ".alphalens" / "ivolatility_smd"
DEFAULT_ETFS: tuple[str, ...] = ("SPY", "QQQ", "IWM", "MDY", "EFA", "EEM", "TLT", "GLD")
DEFAULT_DECILE_PCT = 0.1
DEFAULT_ADV_MIN_USD = 2_000_000.0


SmdLoaderFn = Callable[[str], pd.DataFrame | None]


@dataclass(frozen=True)
class ScoringResult:
    asof: date
    universe_size: int
    n_scored: int
    coverage_pct: float
    top_decile_tickers: list[str]
    top_decile_scores: dict[str, float]
    decile_size: int


def pit_union(
    *,
    start_year: int = 2018,
    pit_dir: Path | None = None,
    extra_etfs: tuple[str, ...] = DEFAULT_ETFS,
) -> list[str]:
    """Union of all PIT-universe yaml snapshots from ``start_year`` onward.

    Same logic as ``experiment_v9_cross_sectional_residual._pit_union``;
    centralized here so paper-trade and audit scripts share the
    universe-construction definition."""
    pit_dir = pit_dir or DEFAULT_PIT_DIR
    union: set[str] = set()
    for p in sorted(pit_dir.glob("*.yaml")):
        try:
            snap_year = int(p.stem.split("-")[0])
        except ValueError:
            continue
        if snap_year < start_year:
            continue
        data = yaml.safe_load(p.read_text()) or {}
        for t in data.get("tickers", []):
            union.add(str(t).upper())
    union |= set(extra_etfs)
    return sorted(union)


def make_smd_loader(cache_dir: Path | None = None) -> SmdLoaderFn:
    """Return a memoized SMD loader closure backed by the local cache."""
    cache_dir = cache_dir or DEFAULT_SMD_CACHE_DIR
    cache: dict[str, pd.DataFrame | None] = {}

    def _load(ticker: str) -> pd.DataFrame | None:
        key = ticker.upper()
        if key not in cache:
            cache[key] = load_cached_smd(key, cache_dir)
        return cache[key]

    return _load


def score_top_decile(
    asof: date,
    *,
    universe: list[str] | None = None,
    smd_loader: SmdLoaderFn | None = None,
    decile_pct: float = DEFAULT_DECILE_PCT,
    adv_min_dollar: float = DEFAULT_ADV_MIN_USD,
) -> ScoringResult:
    """Run v9D pipeline for a single asof; return top-decile holdings.

    Defaults wire to the standard cache locations (``~/.alphalens/...``).
    Tests can pass an injectable ``smd_loader`` and an explicit ``universe``
    to avoid filesystem dependencies.
    """
    if universe is None:
        universe = pit_union()
    if smd_loader is None:
        smd_loader = make_smd_loader()

    features = build_feature_frame(
        smd_loader=smd_loader,
        universe=universe,
        asof_dates=[asof.isoformat()],
        adv_min_dollar=adv_min_dollar,
    )

    if features.empty:
        return ScoringResult(
            asof=asof,
            universe_size=len(universe),
            n_scored=0,
            coverage_pct=0.0,
            top_decile_tickers=[],
            top_decile_scores={},
            decile_size=0,
        )

    scores = score_cross_sectional_residual(features)
    n_scored = int(scores.notna().sum())
    coverage = n_scored / max(1, len(features))

    scored = features.assign(_score=scores).dropna(subset=["_score"])
    decile_size = max(1, round(len(scored) * decile_pct))
    if len(scored) < 2 * decile_size:
        return ScoringResult(
            asof=asof,
            universe_size=len(universe),
            n_scored=n_scored,
            coverage_pct=coverage,
            top_decile_tickers=[],
            top_decile_scores={},
            decile_size=decile_size,
        )

    ranked = scored.sort_values("_score", ascending=False).head(decile_size)
    tickers = ranked["ticker"].tolist()
    scores_dict = {str(r["ticker"]): float(r["_score"]) for _, r in ranked.iterrows()}

    return ScoringResult(
        asof=asof,
        universe_size=len(universe),
        n_scored=n_scored,
        coverage_pct=coverage,
        top_decile_tickers=tickers,
        top_decile_scores=scores_dict,
        decile_size=decile_size,
    )


def compute_realized_return(
    holdings: list[str],
    prior_asof: date,
    *,
    holding_period_days: int = 5,
    smd_loader: SmdLoaderFn | None = None,
    delisting_events: dict | None = None,
) -> tuple[float, int]:
    """Equal-weighted gross return over ``holdings`` from ``prior_asof`` to
    ``prior_asof + holding_period_days`` trading days.

    Uses ``forward_raw_return`` (same primitive as v9D experiment) which
    handles delisting events via the optional ``delisting_events`` index.
    Returns ``(mean_return, n_realized)`` where ``n_realized`` is the
    number of holdings whose forward return resolved to non-NaN.
    """
    if smd_loader is None:
        smd_loader = make_smd_loader()
    if not holdings:
        return float("nan"), 0

    rets: list[float] = []
    for t in holdings:
        r = forward_raw_return(
            smd_loader,
            t,
            prior_asof,
            holding_period=holding_period_days,
            delisting_events=delisting_events,
        )
        rets.append(np.nan if r is None else float(r))

    arr = np.asarray(rets, dtype=float)
    if np.all(np.isnan(arr)):
        return float("nan"), 0
    return float(np.nanmean(arr)), int(np.sum(~np.isnan(arr)))


def latest_trading_asof(
    *, today: date | None = None, smd_loader: SmdLoaderFn | None = None
) -> date | None:
    """Return the most recent calendar date in the MDY benchmark cache that
    has a non-NaN ivp30 row, on or before ``today`` (default: today's date).

    Used by the weekly cron to pick a deterministic asof — same selection
    rule the v9D experiment used for its calendar."""
    if smd_loader is None:
        smd_loader = make_smd_loader()
    if today is None:
        today = date.today()
    df = smd_loader("MDY")
    if df is None or df.empty:
        return None
    if "ivp30" in df.columns:
        df = df.loc[df["ivp30"].notna()]
    if df.empty:
        return None
    df = df.sort_values("tradeDate")
    dates = pd.to_datetime(df["tradeDate"]).dt.date.tolist()
    valid = [d for d in dates if d <= today]
    return max(valid) if valid else None


def benchmark_return(
    prior_asof: date,
    *,
    holding_period_days: int = 5,
    benchmark: str = "MDY",
    smd_loader: SmdLoaderFn | None = None,
) -> float:
    """Return the benchmark's forward holding-period return over the same
    window as ``compute_realized_return``."""
    if smd_loader is None:
        smd_loader = make_smd_loader()
    r = forward_raw_return(smd_loader, benchmark, prior_asof, holding_period=holding_period_days)
    return float("nan") if r is None else float(r)


def incremental_refresh_smd(
    tickers: list[str],
    *,
    target_end: date,
    cache_dir: Path | None = None,
    fetcher: Callable[[str, date, date], pd.DataFrame | None] | None = None,
    sleep_between: float = 0.2,
) -> dict[str, int]:
    """Append missing-date rows to existing per-ticker SMD parquets.

    Unlike ``download_and_cache`` (which skips already-cached tickers
    entirely), this function reads each ticker's existing parquet, finds
    the latest ``tradeDate``, and fetches only ``(latest, target_end]`` if
    the latest is older than ``target_end``. Missing tickers (no parquet
    yet) are skipped — use ``download_and_cache`` for bulk seed.

    Returns a dict with counts: {"refreshed": N, "skipped_uptodate": N,
    "skipped_missing": N, "errors": N}.
    """
    cache_dir = cache_dir or DEFAULT_SMD_CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)

    if fetcher is None:
        from alphalens.data.alt_data.ivolatility_smd_cache import (
            _robust_smd_fetcher,
        )

        fetcher = _robust_smd_fetcher

    counts = {"refreshed": 0, "skipped_uptodate": 0, "skipped_missing": 0, "errors": 0}

    for ticker in tickers:
        path = cache_dir / f"{ticker.upper()}.parquet"
        if not path.exists():
            counts["skipped_missing"] += 1
            continue
        try:
            existing = pd.read_parquet(path)
        except Exception as exc:
            logger.warning("[%s] parquet read failed: %s", ticker, exc)
            counts["errors"] += 1
            continue
        if existing.empty or "tradeDate" not in existing.columns:
            counts["errors"] += 1
            continue
        max_date_str = pd.to_datetime(existing["tradeDate"]).max().date()
        if max_date_str >= target_end:
            counts["skipped_uptodate"] += 1
            continue
        try:
            from datetime import timedelta

            new_start = max_date_str + timedelta(days=1)
            new_df = fetcher(ticker, new_start, target_end)
        except Exception as exc:
            logger.warning("[%s] fetch failed: %s", ticker, exc)
            counts["errors"] += 1
            continue
        if new_df is None or (isinstance(new_df, pd.DataFrame) and new_df.empty):
            counts["skipped_uptodate"] += 1  # vendor reports no new rows
            continue
        try:
            from alphalens.data.alt_data.ivolatility_smd_cache import (
                _coerce_mixed_object_columns,
            )

            new_df = _coerce_mixed_object_columns(new_df)
            combined = pd.concat([existing, new_df], ignore_index=True)
            combined = combined.drop_duplicates(subset=["tradeDate"], keep="last")
            combined = combined.sort_values("tradeDate").reset_index(drop=True)
            combined.to_parquet(path)
            counts["refreshed"] += 1
        except Exception as exc:
            logger.warning("[%s] write failed: %s", ticker, exc)
            counts["errors"] += 1
        if sleep_between > 0:
            import time

            time.sleep(sleep_between)

    return counts


def backfill_smd_history(
    tickers: list[str],
    *,
    target_start: date,
    cache_dir: Path | None = None,
    fetcher: Callable[[str, date, date], pd.DataFrame | None] | None = None,
    sleep_between: float = 0.2,
) -> dict[str, int]:
    """Prepend pre-existing-min-date rows to per-ticker SMD parquets.

    Inverse of ``incremental_refresh_smd``. Reads each ticker's existing
    parquet, finds the earliest ``tradeDate``, and fetches
    ``[target_start, earliest)`` if ``earliest > target_start``. Vendor's
    range-mode call returns whatever rows it has (empty-DataFrame for no
    coverage). Combined frame is dedup'd by ``tradeDate`` and sorted.

    Missing tickers (no parquet) are skipped — bulk seed must run first via
    ``download_and_cache``. This function is for *historical extension* of
    a working cache, not initial population.

    Returns a counts dict: {"backfilled": N, "skipped_already_covered": N,
    "skipped_no_coverage": N, "skipped_missing": N, "errors": N}.
    """
    cache_dir = cache_dir or DEFAULT_SMD_CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)

    if fetcher is None:
        from alphalens.data.alt_data.ivolatility_smd_cache import (
            _robust_smd_fetcher,
        )

        fetcher = _robust_smd_fetcher

    counts = {
        "backfilled": 0,
        "skipped_already_covered": 0,
        "skipped_no_coverage": 0,
        "skipped_missing": 0,
        "errors": 0,
    }

    for ticker in tickers:
        path = cache_dir / f"{ticker.upper()}.parquet"
        if not path.exists():
            counts["skipped_missing"] += 1
            continue
        try:
            existing = pd.read_parquet(path)
        except Exception as exc:
            logger.warning("[%s] parquet read failed: %s", ticker, exc)
            counts["errors"] += 1
            continue
        if existing.empty or "tradeDate" not in existing.columns:
            counts["errors"] += 1
            continue
        min_date = pd.to_datetime(existing["tradeDate"]).min().date()
        if min_date <= target_start:
            counts["skipped_already_covered"] += 1
            continue
        try:
            from datetime import timedelta

            new_end = min_date - timedelta(days=1)
            new_df = fetcher(ticker, target_start, new_end)
        except Exception as exc:
            logger.warning("[%s] backfill fetch failed: %s", ticker, exc)
            counts["errors"] += 1
            continue
        if new_df is None or (isinstance(new_df, pd.DataFrame) and new_df.empty):
            counts["skipped_no_coverage"] += 1
            continue
        try:
            from alphalens.data.alt_data.ivolatility_smd_cache import (
                _coerce_mixed_object_columns,
            )

            new_df = _coerce_mixed_object_columns(new_df)
            combined = pd.concat([new_df, existing], ignore_index=True)
            combined = combined.drop_duplicates(subset=["tradeDate"], keep="last")
            combined = combined.sort_values("tradeDate").reset_index(drop=True)
            combined.to_parquet(path)
            counts["backfilled"] += 1
        except Exception as exc:
            logger.warning("[%s] backfill write failed: %s", ticker, exc)
            counts["errors"] += 1
        if sleep_between > 0:
            import time

            time.sleep(sleep_between)

    return counts
