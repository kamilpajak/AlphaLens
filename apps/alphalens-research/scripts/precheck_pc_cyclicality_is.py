"""Pre-screen #2 — P/C abnormal cyclicality on IS 2014-2017.

Per zen Z4 + perplexity P-INDEPENDENCE adversarial reviews 2026-05-10:
even with cross-sectional independence (insider × P/C ρ ≈ 0), portfolio
RETURNS could be correlated under regime shifts. Compound's
counter-cyclical exposure depends on whether P/C cyclicality matches or
opposes insider_form4 (which is EXTREME counter-cyclical per PR #88).

This precheck builds a top-decile EW long-only P/C portfolio at 21d stride
on IS 2014-2017, computes daily continuous returns, and classifies vol-
regime cyclicality via signal_vol_regime.classify_cyclicality. Result is
informational for the compound design memo §7 (Open Risks), NOT a kill
gate (compound is Layer 1 fusion, NOT Layer 4 overlay).
"""

from __future__ import annotations

import logging
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from alphalens_pipeline.data.alt_data.pit_universe_loader import load_universe_union  # noqa: E402
from alphalens_pipeline.data.alt_data.yfinance_cache import load_cached_histories  # noqa: E402
from alphalens_research.attribution.signal_vol_regime import (  # noqa: E402
    aggregate_returns_by_regime,
    assign_vol_regime_quintiles,
    classify_cyclicality,
)
from alphalens_research.screeners.options_volume.features import build_feature_frame  # noqa: E402
from alphalens_research.screeners.options_volume.pc_abnormal_volume import (
    score_pc_abnormal_residual,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("precheck-cyc")

_PRICES_DIR = Path.home() / ".alphalens" / "prices"
_SMD_DIR = Path.home() / ".alphalens" / "ivolatility_smd"

_TOP_DECILE_PCT = 0.10
_REBALANCE_STRIDE_DAYS = 21


def _smd_loader(ticker: str) -> pd.DataFrame | None:
    p = _SMD_DIR / f"{ticker.upper()}.parquet"
    if not p.exists():
        return None
    df = pd.read_parquet(p)
    if "tradeDate" in df.columns:
        df["date"] = pd.to_datetime(df["tradeDate"])
    return df


def _monthly_asofs(start: date, end: date, *, day_of_month: int = 21) -> list[pd.Timestamp]:
    asofs = []
    cur = pd.Timestamp(start.year, start.month, day_of_month)
    while cur.date() <= end:
        while cur.weekday() > 4:
            cur += pd.Timedelta(days=1)
        if cur.date() <= end:
            asofs.append(cur)
        if cur.month == 12:
            cur = pd.Timestamp(cur.year + 1, 1, day_of_month)
        else:
            cur = pd.Timestamp(cur.year, cur.month + 1, day_of_month)
    return asofs


def _build_top_decile_portfolio_daily_returns(
    scores_panel: pd.DataFrame,
    histories: dict[str, pd.DataFrame],
    asofs: list[pd.Timestamp],
    top_decile_pct: float = _TOP_DECILE_PCT,
) -> pd.Series:
    """Construct top-decile EW long-only portfolio + return daily series.

    For each asof t, pick top-decile of non-NaN scored tickers by score;
    hold equally until t+stride. Daily portfolio return = mean of held
    tickers' daily returns (skipna).
    """
    daily_returns_per_ticker = {}
    for ticker, hist in histories.items():
        if hist is None or hist.empty or "close" not in hist.columns:
            continue
        ret = hist["close"].pct_change()
        daily_returns_per_ticker[ticker] = ret

    sorted_asofs = sorted(asofs)
    portfolio_returns = []

    for i, asof in enumerate(sorted_asofs):
        scores_at_asof = scores_panel[scores_panel["asof"] == asof.normalize()]
        if scores_at_asof.empty:
            continue
        valid = scores_at_asof.dropna(subset=["score"])
        if valid.empty:
            continue
        n_decile = max(1, int(len(valid) * top_decile_pct))
        top = valid.nlargest(n_decile, "score")["ticker"].tolist()
        held_tickers = [t for t in top if t in daily_returns_per_ticker]
        if not held_tickers:
            continue

        # Hold until next asof (or end of window)
        next_asof = sorted_asofs[i + 1] if i + 1 < len(sorted_asofs) else None
        # End of holding period
        end_idx = (
            next_asof if next_asof is not None else asof + pd.Timedelta(days=_REBALANCE_STRIDE_DAYS)
        )

        # Per-day mean return across held tickers
        rets_in_period = pd.DataFrame({t: daily_returns_per_ticker[t] for t in held_tickers})
        rets_in_period = rets_in_period[
            (rets_in_period.index > asof) & (rets_in_period.index <= end_idx)
        ]
        if rets_in_period.empty:
            continue
        portfolio_daily = rets_in_period.mean(axis=1, skipna=True)
        portfolio_returns.append(portfolio_daily)

    if not portfolio_returns:
        return pd.Series(dtype=float, name="portfolio_daily")
    series = pd.concat(portfolio_returns).sort_index()
    series = series[~series.index.duplicated(keep="first")]
    return series.rename("portfolio_daily")


def main() -> int:
    is_start = date(2014, 1, 1)
    is_end = date(2017, 12, 31)

    logger.info("=" * 60)
    logger.info("Pre-screen #2 (P/C cyclicality, IS): %s .. %s", is_start, is_end)
    logger.info("=" * 60)

    universe = load_universe_union(is_start, is_end)
    logger.info("PIT universe: %d tickers", len(universe))

    histories = load_cached_histories(universe, _PRICES_DIR)
    histories = {t: h for t, h in histories.items() if h is not None and not h.empty}
    logger.info("OHLCV histories: %d", len(histories))

    asofs = _monthly_asofs(is_start, is_end)
    logger.info("Monthly asofs: %d", len(asofs))

    logger.info("Building P/C feature frame ...")
    pc_features = build_feature_frame(
        smd_loader=_smd_loader,
        universe=list(histories.keys()),
        asof_dates=[a.strftime("%Y-%m-%d") for a in asofs],
    )
    logger.info("P/C features: %d rows", len(pc_features))
    if pc_features.empty:
        logger.error("Empty P/C features — abort")
        return 1

    pc_scores = score_pc_abnormal_residual(pc_features)
    pc_features = pc_features.assign(score=pc_scores).dropna(subset=["score"])
    pc_features["asof"] = pd.to_datetime(pc_features["asof"]).dt.normalize()
    pc_panel = pc_features[["asof", "ticker", "score"]].copy()
    logger.info("P/C scores: %d non-NaN over %d asofs", len(pc_panel), pc_panel["asof"].nunique())

    logger.info("Building top-decile EW portfolio + daily continuous returns ...")
    portfolio_returns = _build_top_decile_portfolio_daily_returns(
        pc_panel, histories, asofs, top_decile_pct=_TOP_DECILE_PCT
    )
    logger.info(
        "P/C portfolio daily returns: n=%d, mean=%.4f%%/d, std=%.4f%%/d",
        len(portfolio_returns),
        portfolio_returns.mean() * 100,
        portfolio_returns.std() * 100,
    )

    if len(portfolio_returns) < 100:
        logger.error(
            "Too few daily returns (%d) for cyclicality classification", len(portfolio_returns)
        )
        return 2

    # Load IWM 60d realized vol (same exogenous regime variable as PR #88)
    logger.info("Loading IWM benchmark for vol regime ...")
    iwm = yf.download(
        "IWM", start="2013-09-01", end="2018-01-15", progress=False, auto_adjust=False
    )
    if isinstance(iwm.columns, pd.MultiIndex):
        iwm.columns = [c[0] for c in iwm.columns]
    iwm_ret = iwm["Adj Close"].pct_change()
    iwm_vol_60d = iwm_ret.rolling(60).std() * np.sqrt(252)

    # Align to portfolio dates
    aligned = pd.DataFrame({"portfolio": portfolio_returns, "vol60": iwm_vol_60d}).dropna()
    logger.info("Aligned obs: %d", len(aligned))

    quintiles = assign_vol_regime_quintiles(aligned["vol60"], n_quintiles=5)
    summary = aggregate_returns_by_regime(aligned["portfolio"], quintiles, periods_per_year=252)

    logger.info("=" * 60)
    logger.info("PER-QUINTILE SUMMARY (P/C abnormal portfolio, IS 2014-2017)")
    logger.info("=" * 60)
    logger.info(
        f"{'Q':5} {'count':>8} {'mean/d':>10} {'std/d':>10} {'sharpe_ann':>12} {'ann_ret':>10}"
    )
    for q in ["Q1", "Q2", "Q3", "Q4", "Q5"]:
        logger.info(
            f"{q:5} {summary.quintile_counts[q]:>8} "
            f"{summary.quintile_means[q] * 100:>9.4f}% "
            f"{summary.quintile_stds[q] * 100:>9.4f}% "
            f"{summary.quintile_sharpes[q]:>+12.3f} "
            f"{summary.quintile_means[q] * 252 * 100:>+9.2f}%"
        )

    verdict = classify_cyclicality(summary)
    logger.info("=" * 60)
    logger.info("VERDICT")
    logger.info("=" * 60)
    logger.info("Sign pattern   : %s", verdict.sign_pattern)
    logger.info("R_mean         : %+.3f", verdict.r_mean)
    logger.info("R_sharpe       : %+.3f", verdict.r_sharpe)
    logger.info("Classification : %s", verdict.classification)
    logger.info("Proceed (info) : %s", verdict.proceed)
    logger.info("Rationale      : %s", verdict.rationale)
    logger.info("")
    logger.info(
        "Compound risk note: insider_form4 base is EXTREME counter-cyclical (PR #88). "
        "If P/C is also counter-cyclical, compound RETURNS may be highly correlated "
        "even with cross-sectional ρ≈0 (per perplexity P-INDEPENDENCE). "
        "If P/C is calm-period-concentrated, compound RETURNS may be uncorrelated."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
