"""Distress-credit scorer adapter — bottom-quintile Merton-PD selection.

Engine adapter contract: ``adapter(histories, config) -> DataFrame`` with
``ticker`` and ``score`` columns. Score = -PD so engine ``top_n`` picks
lowest-PD names (safest equity = long leg of distress puzzle).

Adapter zeroes scores outside the bottom quintile by PD so that engine's
top_n selection effectively gives the bottom quintile. Top distress
quintile is excluded entirely (defensive prior locked at 20% per
literature standard).
"""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd

from alphalens_research.screeners.distress_credit.features import (
    InMemoryLiabilitiesStore,
    InMemoryShareCountStore,
    LiabilitiesStoreProtocol,
    ShareCountStoreProtocol,
)
from alphalens_research.screeners.distress_credit.merton import (
    merton_d2,
    merton_pd,
    realised_vol_60d,
)

__all__ = [
    "InMemoryLiabilitiesStore",
    "InMemoryShareCountStore",
    "distress_credit_adapter",
]

_MIN_BARS_REQUIRED = 65
_DEFAULT_QUINTILE_PCT = 0.20
_DEFAULT_TOP_DISTRESS_EXCLUDE_PCT = 0.20
_DEFAULT_HORIZON_YEARS = 1.0


def _is_eligible(
    ticker: str,
    df: pd.DataFrame,
    benchmark: str | None,
    liab_store: LiabilitiesStoreProtocol | None,
    share_store: ShareCountStoreProtocol | None,
) -> bool:
    """Cheap pre-checks before computing sigma + Merton."""
    if ticker == benchmark or df is None or len(df) < _MIN_BARS_REQUIRED:
        return False
    return not (liab_store is None or share_store is None)


def _score_one_ticker(
    ticker: str,
    df: pd.DataFrame,
    *,
    asof: pd.Timestamp,
    benchmark: str | None,
    liab_store: LiabilitiesStoreProtocol | None,
    share_store: ShareCountStoreProtocol | None,
    rf: float,
    horizon_years: float,
) -> dict | None:
    """Compute (mcap, liab, sigma, PD) for one ticker; None if any step fails."""
    if not _is_eligible(ticker, df, benchmark, liab_store, share_store):
        return None
    closes = df["close"].to_numpy(dtype=float)
    sigma = realised_vol_60d(closes) if closes[-1] > 0 else None
    if sigma is None:
        return None
    liab = liab_store.get(ticker, asof)
    shares = share_store.get(ticker, asof)
    if liab is None or shares is None or liab <= 0 or shares <= 0:
        return None
    equity_mcap = float(closes[-1]) * float(shares)
    d2 = merton_d2(
        equity_mcap=equity_mcap,
        total_liabilities=float(liab),
        sigma_equity=sigma,
        rf=rf,
        horizon_years=horizon_years,
    )
    if d2 is None:
        return None
    return {
        "ticker": ticker,
        "pd": float(merton_pd(d2)),
        "d2": float(d2),
        "equity_mcap": equity_mcap,
        "total_liabilities": float(liab),
        "sigma_60d": sigma,
    }


def _resolve_rf(rf_series: pd.Series | None, asof: pd.Timestamp) -> float:
    """Latest DGS1 observation <= asof, defaulting to 0.04 if unavailable."""
    if rf_series is None or len(rf_series) == 0:
        return 0.04
    history = rf_series.loc[rf_series.index <= asof]
    if len(history) == 0:
        return 0.04
    val = float(history.iloc[-1])
    if not np.isfinite(val):
        return 0.04
    return val


def distress_credit_adapter(
    histories: Mapping[str, pd.DataFrame],
    config: Mapping | None = None,
) -> pd.DataFrame:
    """Score = -PD (Merton naive KMV) on bottom-quintile-only universe.

    Required config:
      asof: pd.Timestamp
      liabilities_store: LiabilitiesStoreProtocol
      shares_store: ShareCountStoreProtocol
      rf_series: pd.Series of DGS1 (decimal)
      benchmark: ticker to skip
      _quintile_pct: 0.20 default
      _top_distress_exclude_pct: 0.20 default
    """
    cfg = dict(config or {})
    asof = cfg.get("asof")
    if asof is None:
        raise ValueError("distress_credit_adapter requires config['asof']")
    benchmark = cfg.get("benchmark")
    liab_store = cfg.get("liabilities_store")
    share_store = cfg.get("shares_store")
    rf_series = cfg.get("rf_series")
    quintile_pct = float(cfg.get("_quintile_pct", _DEFAULT_QUINTILE_PCT))
    top_distress_pct = float(
        cfg.get("_top_distress_exclude_pct", _DEFAULT_TOP_DISTRESS_EXCLUDE_PCT)
    )
    horizon_years = float(cfg.get("_horizon_years", _DEFAULT_HORIZON_YEARS))

    rf = _resolve_rf(rf_series, asof)

    rows = [
        scored
        for ticker, df in histories.items()
        if (
            scored := _score_one_ticker(
                ticker,
                df,
                asof=asof,
                benchmark=benchmark,
                liab_store=liab_store,
                share_store=share_store,
                rf=rf,
                horizon_years=horizon_years,
            )
        )
        is not None
    ]
    if not rows:
        return pd.DataFrame(columns=["ticker", "score", "pd"])

    df = pd.DataFrame(rows).sort_values("pd", ascending=True).reset_index(drop=True)

    # Exclude top distress quintile (highest PDs)
    n_total = len(df)
    n_exclude_top = int(n_total * top_distress_pct)
    if n_exclude_top > 0:
        df = df.iloc[: n_total - n_exclude_top].reset_index(drop=True)

    # Keep first 20% of ORIGINAL universe size (not 20% of post-exclusion size).
    # Equal-weighting downstream means bottom quintile gets exactly 20% capital.
    n_keep = max(1, int(n_total * quintile_pct))
    df = df.iloc[:n_keep].reset_index(drop=True)

    df["score"] = -df["pd"]
    return df


distress_credit_adapter.MIN_BARS_REQUIRED = _MIN_BARS_REQUIRED
