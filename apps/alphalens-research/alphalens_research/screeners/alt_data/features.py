"""v4 v2 alt-data 10-feature joiner.

Pre-registered as `alt_data_screener_v2_2026_04_30` per
docs/research/preregistration/params_alt_data_screener_v2_2026_04_30.json.

Builds a 10-feature frame per (ticker, asof) by joining six PIT-correct sources:

  1. Foster SUE w/ first-filed snapshot — `data/fundamentals/sue.py` (new in v4)
  2. EDGAR filing-date stream + HistoryStore — for PEAD (5d post-filing return)
  3. Polygon /stocks/v1/short-interest — `data/alt_data/polygon_short_interest.py` (new in v4)
  4. Shares outstanding via `data/alt_data/shares_outstanding.py` for % float
  5. Insider Form 4 cluster parquet (already wired) — diagnostic anchors
  6. HistoryStore (already wired) — for realized downside skew

PIT contracts per pre-reg:

- SUE: first-filed `<= asof` for residual std (perplexity Objection 3)
- PEAD: filing_date + 5 BD `<= asof` truncation (zen Objection 2A)
- Insider: F4 fix locked + filing-date gate
- Short interest: settlement_date + 8 BD `<= asof` (FINRA Rule 4560)
- Shares outstanding: filed_date `<= asof` per shares_outstanding module
- Filing density: filed `<= asof` count over trailing 252 BD

Time-decay multipliers (per zen Objection 1 secondary fix):

- SUE feature value = SUE × exp(-recency_days / 30)
- PEAD feature value = PEAD × exp(-recency_days / 30)
- Where recency_days = trading days since most recent eligible filing.

Cross-sectional ranks computed within each asof slice (NaN inputs propagate).
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Sequence
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# Canonical 10-feature ordering. MUST match `params_frozen.feature_whitelist`
# in `docs/research/preregistration/params_alt_data_screener_v2_2026_04_30.json`.
# Locked by `tests/test_alt_data_features.py::test_feature_names_match_pre_reg_v2`.
FEATURE_NAMES: tuple[str, ...] = (
    "earnings_sue_naive_4q_decayed",
    "earnings_pead_5d_post_decayed",
    "earnings_recency_days",
    "short_interest_pct_float_change_60d",
    "rank_short_interest_pct_float",
    "log1p_days_to_cover",
    "insider_log_count",
    "insider_log_dollar",
    "rank_realized_downside_skew_60d",
    "filing_density_4q",
)

_DECAY_TAU_DAYS = 30.0
_PEAD_LOOKBACK_DAYS = 90
_PEAD_POST_BD = 5
_FILING_DENSITY_MAX = 30
_FILING_DENSITY_LOOKBACK_BD = 252
_DOWNSIDE_SKEW_WINDOW = 60
_SHORT_INTEREST_CHANGE_DAYS = 60


# ---------------------------------------------------------------------------
# Per-feature primitives


def _decay_multiplier(recency_days: float) -> float:
    """exp(-recency_days / 30) — locked per pre-reg anti_overfit_constraints."""
    if recency_days < 0:
        return 1.0
    return float(math.exp(-recency_days / _DECAY_TAU_DAYS))


def _add_business_days(d: date, n: int) -> date:
    """Add ``n`` business days (Mon-Fri, no holiday calendar). Sufficient at the
    granularity of feature gating (5 BD windows tolerate ±1 day error).
    """
    cur = d
    added = 0
    while added < n:
        cur = cur + timedelta(days=1)
        if cur.weekday() < 5:
            added += 1
    return cur


def _close_at_or_before(closes: pd.Series, target: pd.Timestamp) -> float | None:
    sliced = closes.loc[:target]
    if sliced.empty:
        return None
    val = sliced.iloc[-1]
    return None if pd.isna(val) else float(val)


def _pead_5d_post(
    *,
    history_store,
    ticker: str,
    asof: date,
    most_recent_filing: date | None,
) -> float:
    """5-day post-filing cumulative return; 0.0 if no eligible filing.

    Eligible filing must satisfy:
      (filing + 5 BD) <= asof   AND   filing > asof - 90 days.

    Returns the [filing, filing+5BD] cumulative return on the ticker's close
    series. If history is missing for either anchor, returns 0.0.
    """
    if most_recent_filing is None:
        return 0.0
    if most_recent_filing <= asof - timedelta(days=_PEAD_LOOKBACK_DAYS):
        return 0.0
    five_bd_after = _add_business_days(most_recent_filing, _PEAD_POST_BD)
    if five_bd_after > asof:
        return 0.0
    df = history_store.truncate_to(ticker, asof)
    if df.empty or "close" not in df.columns:
        return 0.0
    closes = df["close"]
    base = _close_at_or_before(closes, pd.Timestamp(most_recent_filing))
    later = _close_at_or_before(closes, pd.Timestamp(five_bd_after))
    if base is None or later is None or base <= 0:
        return 0.0
    return float(later / base - 1.0)


def _realized_downside_skew_60d(returns: np.ndarray) -> float:
    """std(neg returns) / std(pos returns) over a 60-day window.

    Returns NaN when the input is too short, or when either side has zero
    variance (cannot form the ratio).
    """
    if len(returns) < _DOWNSIDE_SKEW_WINDOW:
        return float("nan")
    window = np.asarray(returns[-_DOWNSIDE_SKEW_WINDOW:], dtype=float)
    neg = window[window < 0]
    pos = window[window > 0]
    if len(neg) < 2 or len(pos) < 2:
        return float("nan")
    sigma_neg = float(np.std(neg, ddof=1))
    sigma_pos = float(np.std(pos, ddof=1))
    if sigma_pos <= 0:
        return float("nan")
    return sigma_neg / sigma_pos


def _filing_density_4q(filing_dates: Sequence[date], asof: date) -> int:
    """Count of distinct filing dates in [asof - 252 BD, asof], capped at 30."""
    # Conservative window: 252 trading days ~= 365 calendar days, tolerate
    # weekends. Use 365d window (filings on weekends don't exist anyway).
    lower = asof - timedelta(days=365)
    eligible = {d for d in filing_dates if lower < d <= asof}
    return min(len(eligible), _FILING_DENSITY_MAX)


def _ticker_daily_returns(
    history_store, ticker: str, asof: date, lookback_days: int = 252
) -> np.ndarray:
    """Daily returns on close[:asof] over the trailing `lookback_days`.

    Uses HistoryStore.truncate_to. Returns empty array when insufficient data.
    """
    df = history_store.truncate_to(ticker, asof)
    if df.empty or "close" not in df.columns:
        return np.array([])
    closes = df["close"].astype(float)
    if len(closes) < 2:
        return np.array([])
    rets = closes.pct_change().dropna().to_numpy()
    if lookback_days and len(rets) > lookback_days:
        rets = rets[-lookback_days:]
    return rets


def _short_interest_change_60d(
    *,
    polygon_si_client,
    shares_lookup: Callable[[str, date], int | None],
    ticker: str,
    asof: date,
) -> float:
    """(SI/float at most-recent eligible) - (same 60 calendar days earlier).

    NaN when either snapshot is unavailable.
    """
    df = polygon_si_client.fetch_ticker(ticker)
    if df.empty:
        return float("nan")

    # Build eligible mask: settlement_date + 8 BD <= asof
    eligible_mask = np.array([_add_business_days(ts.date(), 8) <= asof for ts in df.index])
    eligible = df[eligible_mask]
    if eligible.empty:
        return float("nan")
    current_settlement = eligible.index[-1].date()
    current_si = float(eligible.iloc[-1]["short_interest"])
    current_shares = shares_lookup(ticker, current_settlement)
    if current_shares is None or current_shares <= 0:
        return float("nan")
    current_pct = current_si / current_shares

    # Prior settlement at least 60 calendar days before current_settlement
    prior_target = current_settlement - timedelta(days=_SHORT_INTEREST_CHANGE_DAYS)
    prior = eligible.loc[: pd.Timestamp(prior_target)]
    if prior.empty:
        return float("nan")
    prior_settlement = prior.index[-1].date()
    prior_si = float(prior.iloc[-1]["short_interest"])
    prior_shares = shares_lookup(ticker, prior_settlement)
    if prior_shares is None or prior_shares <= 0:
        return float("nan")
    prior_pct = prior_si / prior_shares
    return current_pct - prior_pct


def _short_interest_pct_float_current(
    *,
    polygon_si_client,
    shares_lookup: Callable[[str, date], int | None],
    ticker: str,
    asof: date,
) -> float:
    """Most-recent eligible short_interest / shares_outstanding."""
    rec = polygon_si_client.features_as_of(ticker, asof)
    if rec is None:
        return float("nan")
    shares = shares_lookup(ticker, rec.settlement_date)
    if shares is None or shares <= 0:
        return float("nan")
    return rec.short_interest / shares


def _log1p_days_to_cover(
    *,
    polygon_si_client,
    ticker: str,
    asof: date,
) -> float:
    rec = polygon_si_client.features_as_of(ticker, asof)
    if rec is None:
        return float("nan")
    return float(np.log1p(max(rec.days_to_cover, 0.0)))


def _trading_days_since(filing: date, asof: date) -> float:
    """Approximate trading days elapsed between filing and asof (capped at 90)."""
    if filing > asof:
        return 0.0
    cal_days = (asof - filing).days
    # 252 BD / 365 cal ≈ 0.69
    bd = round(cal_days * 252 / 365)
    return float(min(bd, 90))


# ---------------------------------------------------------------------------
# Cross-sectional rank


def _cross_sectional_rank_descending(values: pd.Series) -> pd.Series:
    """Percentile rank where larger raw value → higher rank. NaN preserved."""
    if len(values.dropna()) <= 1:
        return values.where(values.isna(), 0.5)
    return values.rank(pct=True, na_option="keep")


# ---------------------------------------------------------------------------
# Per-ticker assembly


def _compute_per_ticker_features(
    ticker: str,
    asof: date,
    *,
    history_store,
    insider_scorer,
    sue_store,
    polygon_si_client,
    shares_lookup: Callable[[str, date], int | None],
    filings_lookup: Callable[[str, date], list[date]],
) -> dict[str, Any]:
    """Compute the per-(ticker, asof) features. Cross-sectional ranks are
    deferred to the slice-level pass.
    """
    # Filing-date stream for this ticker (most-recent-first or any order; we filter)
    filings = sorted(filings_lookup(ticker, asof), reverse=True)
    eligible_filings = [f for f in filings if _add_business_days(f, _PEAD_POST_BD) <= asof]
    most_recent_eligible = eligible_filings[0] if eligible_filings else None
    recency_days = _trading_days_since(most_recent_eligible, asof) if most_recent_eligible else 90.0
    decay = _decay_multiplier(recency_days)

    # SUE
    sue_raw = sue_store.sue(ticker, asof)
    sue_feat = (sue_raw * decay) if sue_raw is not None else 0.0

    # PEAD
    pead_raw = _pead_5d_post(
        history_store=history_store,
        ticker=ticker,
        asof=asof,
        most_recent_filing=most_recent_eligible,
    )
    pead_feat = pead_raw * decay

    # Short interest
    si_change = _short_interest_change_60d(
        polygon_si_client=polygon_si_client,
        shares_lookup=shares_lookup,
        ticker=ticker,
        asof=asof,
    )
    si_pct_float = _short_interest_pct_float_current(
        polygon_si_client=polygon_si_client,
        shares_lookup=shares_lookup,
        ticker=ticker,
        asof=asof,
    )
    log1p_dtc = _log1p_days_to_cover(
        polygon_si_client=polygon_si_client,
        ticker=ticker,
        asof=asof,
    )

    # Insider features (default 0 on None per scorer contract)
    insider_feat = insider_scorer.features_as_of(ticker, asof)
    if insider_feat is None:
        insider_log_count = 0.0
        insider_log_dollar = 0.0
    else:
        insider_log_count = float(np.log1p(max(insider_feat["insider_count"], 0.0)))
        insider_log_dollar = float(np.log1p(max(insider_feat["aggregate_dollar"], 0.0)))

    # Realized downside skew
    rets = _ticker_daily_returns(history_store, ticker, asof)
    skew_raw = _realized_downside_skew_60d(rets)

    # Filing density
    density = _filing_density_4q(filings, asof)

    return {
        "asof": asof,
        "ticker": ticker.upper(),
        "earnings_sue_naive_4q_decayed": float(sue_feat),
        "earnings_pead_5d_post_decayed": float(pead_feat),
        "earnings_recency_days": float(recency_days),
        "short_interest_pct_float_change_60d": float(si_change),
        "_si_pct_float_raw_for_rank": float(si_pct_float),
        "log1p_days_to_cover": float(log1p_dtc),
        "insider_log_count": insider_log_count,
        "insider_log_dollar": insider_log_dollar,
        "_downside_skew_raw_for_rank": float(skew_raw),
        "filing_density_4q": int(density),
    }


# ---------------------------------------------------------------------------
# Top-level builder


def build_feature_frame(
    *,
    history_store,
    insider_scorer,
    sue_store,
    polygon_si_client,
    shares_lookup: Callable[[str, date], int | None],
    filings_lookup: Callable[[str, date], list[date]],
    universe: Sequence[str],
    asof_dates: Sequence[date],
    benchmark: str = "SPY",
) -> pd.DataFrame:
    """PIT-correct 10-feature joiner across 6 sources.

    Returns long-format DataFrame with columns
    ``[asof, ticker, *FEATURE_NAMES]``. One row per (asof, ticker) where
    per-ticker compute yielded a result.
    """
    benchmark_up = benchmark.upper()
    universe_up = [t.upper() for t in universe if t.upper() != benchmark_up]
    n_asofs = len(asof_dates)
    n_tickers = len(universe_up)
    logger.info(
        "build_feature_frame: %d asofs x %d tickers = %d compute calls",
        n_asofs,
        n_tickers,
        n_asofs * n_tickers,
    )

    all_rows: list[pd.DataFrame] = []
    for asof_idx, asof in enumerate(asof_dates):
        rows = []
        for t in universe_up:
            row = _compute_per_ticker_features(
                ticker=t,
                asof=asof,
                history_store=history_store,
                insider_scorer=insider_scorer,
                sue_store=sue_store,
                polygon_si_client=polygon_si_client,
                shares_lookup=shares_lookup,
                filings_lookup=filings_lookup,
            )
            if row is not None:
                rows.append(row)
        if not rows:
            continue
        df_asof = pd.DataFrame(rows)

        # Cross-sectional ranks
        df_asof["rank_short_interest_pct_float"] = _cross_sectional_rank_descending(
            df_asof["_si_pct_float_raw_for_rank"]
        )
        df_asof["rank_realized_downside_skew_60d"] = _cross_sectional_rank_descending(
            df_asof["_downside_skew_raw_for_rank"]
        )
        df_asof = df_asof.drop(
            columns=["_si_pct_float_raw_for_rank", "_downside_skew_raw_for_rank"]
        )

        all_rows.append(df_asof)
        if (asof_idx + 1) % 10 == 0 or (asof_idx + 1) == n_asofs:
            logger.info(
                "build_feature_frame: %d/%d asofs (%.1f%%) — last asof=%s, rows accum=%d",
                asof_idx + 1,
                n_asofs,
                (asof_idx + 1) / n_asofs * 100,
                asof,
                sum(len(d) for d in all_rows),
            )

    if not all_rows:
        return _empty_frame()

    out = pd.concat(all_rows, ignore_index=True)
    cols = ["asof", "ticker", *FEATURE_NAMES]
    return out.reindex(columns=cols)


def _empty_frame() -> pd.DataFrame:
    cols = ["asof", "ticker", *FEATURE_NAMES]
    return pd.DataFrame(columns=cols)
