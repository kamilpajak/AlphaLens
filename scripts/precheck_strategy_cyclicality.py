"""Multi-strategy cyclicality verification.

Tests the "phase-stability screening selects FOR counter-cyclicality" hypothesis
by classifying past strategies' portfolio cyclicality. Runs each strategy's
scorer on a common window (2018-2023, matches insider_form4 OOS where
counter-cyclical baseline was first established in PR #88), builds top-decile
EW long-only portfolio at 21d stride, computes daily continuous returns vs
IWM 60d realized vol, classifies cyclicality.

Usage:
    .venv/bin/python scripts/precheck_strategy_cyclicality.py --strategy mom_lowvol
    .venv/bin/python scripts/precheck_strategy_cyclicality.py --strategy v9d_residual
    .venv/bin/python scripts/precheck_strategy_cyclicality.py --strategy distress_credit

Output: VERDICT = EXTREME counter-cyclical / counter-cyclical / orthogonal /
calm-period concentrated / EXTREME calm-period / INCONCLUSIVE.

NOT a production audit driver. Single-shot verification for hypothesis
testing. Window 2018-2023 is BURNT for the strategies being tested (already
audited under their own pre-regs); no further hypothesis spent here.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from alphalens.attribution.signal_vol_regime import (  # noqa: E402
    aggregate_returns_by_regime,
    assign_vol_regime_quintiles,
    classify_cyclicality,
)
from alphalens.backtest.top_decile_portfolio import (  # noqa: E402
    monthly_asof_calendar,
    top_decile_portfolio_daily_returns,
)
from alphalens.data.alt_data.pit_universe_loader import load_universe_union  # noqa: E402
from alphalens.data.alt_data.yfinance_cache import load_cached_histories  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("cyc")

_PRICES_DIR = Path.home() / ".alphalens" / "prices"
_SMD_DIR = Path.home() / ".alphalens" / "ivolatility_smd"


def _smd_loader(ticker: str) -> pd.DataFrame | None:
    p = _SMD_DIR / f"{ticker.upper()}.parquet"
    if not p.exists():
        return None
    df = pd.read_parquet(p)
    if "tradeDate" in df.columns:
        df["date"] = pd.to_datetime(df["tradeDate"])
    return df


# --- Strategy-specific scoring functions ---


def _score_mom_lowvol(
    histories: dict[str, pd.DataFrame], asofs: list[pd.Timestamp]
) -> pd.DataFrame:
    """Run mom+lowvol adapter per asof. Returns long panel [asof, ticker, score]."""
    from alphalens.screeners.momentum_lowvol import momentum_lowvol_adapter

    rows = []
    for asof_ts in asofs:
        cfg = {"asof": asof_ts, "_adv_min_usd": 2_000_000.0, "_vol_weight": 1.0}
        df = momentum_lowvol_adapter(histories, cfg)
        if df.empty:
            continue
        df["asof"] = asof_ts.normalize()
        rows.append(df[["asof", "ticker", "score"]])
    if not rows:
        return pd.DataFrame(columns=["asof", "ticker", "score"])
    return pd.concat(rows, ignore_index=True)


def _score_v9d_residual(
    histories: dict[str, pd.DataFrame], asofs: list[pd.Timestamp]
) -> pd.DataFrame:
    """Run v9D cross-sectional residual scorer using iVol features."""
    from alphalens.screeners.options_implied.cross_sectional_residual import (
        score_cross_sectional_residual,
    )
    from alphalens.screeners.options_implied.features import build_feature_frame

    features = build_feature_frame(
        smd_loader=_smd_loader,
        universe=list(histories.keys()),
        asof_dates=[a.strftime("%Y-%m-%d") for a in asofs],
    )
    if features.empty:
        return pd.DataFrame(columns=["asof", "ticker", "score"])
    scores = score_cross_sectional_residual(features)
    out = features.assign(score=scores).dropna(subset=["score"])
    out["asof"] = pd.to_datetime(out["asof"]).dt.normalize()
    return out[["asof", "ticker", "score"]].copy()


def _score_distress_credit(
    histories: dict[str, pd.DataFrame], asofs: list[pd.Timestamp]
) -> pd.DataFrame:
    """Run distress_credit (Merton PD) scorer using production stores."""
    from alphalens.screeners.distress_credit.features import make_production_stores
    from alphalens.screeners.distress_credit.scorer import distress_credit_adapter

    liab_store, share_store = make_production_stores()
    rows = []
    for asof_ts in asofs:
        cfg = {
            "asof": asof_ts,
            "_liab_store": liab_store,
            "_share_store": share_store,
        }
        try:
            df = distress_credit_adapter(histories, cfg)
        except Exception as e:
            logger.debug("distress_credit asof %s failed: %s", asof_ts.date(), e)
            continue
        if df is None or df.empty:
            continue
        df["asof"] = asof_ts.normalize()
        rows.append(df[["asof", "ticker", "score"]])
    if not rows:
        return pd.DataFrame(columns=["asof", "ticker", "score"])
    return pd.concat(rows, ignore_index=True)


_STRATEGIES = {
    "mom_lowvol": _score_mom_lowvol,
    "v9d_residual": _score_v9d_residual,
    "distress_credit": _score_distress_credit,
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", choices=list(_STRATEGIES), required=True)
    parser.add_argument("--start", default="2018-01-01")
    parser.add_argument("--end", default="2023-12-31")
    args = parser.parse_args()

    is_start = pd.to_datetime(args.start).date()
    is_end = pd.to_datetime(args.end).date()

    logger.info("=" * 60)
    logger.info("Strategy cyclicality verification: %s | %s .. %s", args.strategy, is_start, is_end)
    logger.info("=" * 60)

    universe = load_universe_union(is_start, is_end)
    logger.info("PIT universe: %d tickers", len(universe))

    histories = load_cached_histories(universe, _PRICES_DIR)
    histories = {t: h for t, h in histories.items() if h is not None and not h.empty}
    logger.info("OHLCV histories: %d", len(histories))

    asofs = monthly_asof_calendar(is_start, is_end)
    logger.info("Monthly asofs: %d", len(asofs))

    logger.info("Running %s scorer per asof ...", args.strategy)
    scores_panel = _STRATEGIES[args.strategy](histories, asofs)
    if scores_panel.empty:
        logger.error("Empty scores panel — abort")
        return 1
    logger.info(
        "Scores: %d non-NaN rows over %d asofs",
        len(scores_panel),
        scores_panel["asof"].nunique(),
    )

    logger.info("Building top-decile EW portfolio daily returns ...")
    portfolio_returns = top_decile_portfolio_daily_returns(scores_panel, histories, asofs)
    logger.info(
        "Portfolio daily returns: n=%d, mean=%.4f%%/d, std=%.4f%%/d",
        len(portfolio_returns),
        portfolio_returns.mean() * 100,
        portfolio_returns.std() * 100,
    )

    if len(portfolio_returns) < 100:
        logger.error("Too few daily returns (%d)", len(portfolio_returns))
        return 2

    logger.info("Loading IWM benchmark ...")
    iwm = yf.download(
        "IWM",
        start=(pd.Timestamp(is_start) - pd.Timedelta(days=120)).strftime("%Y-%m-%d"),
        end=(pd.Timestamp(is_end) + pd.Timedelta(days=15)).strftime("%Y-%m-%d"),
        progress=False,
        auto_adjust=False,
    )
    if isinstance(iwm.columns, pd.MultiIndex):
        iwm.columns = [c[0] for c in iwm.columns]
    iwm_ret = iwm["Adj Close"].pct_change()
    iwm_vol_60d = iwm_ret.rolling(60).std() * np.sqrt(252)

    aligned = pd.DataFrame({"portfolio": portfolio_returns, "vol60": iwm_vol_60d}).dropna()
    logger.info("Aligned obs: %d", len(aligned))

    quintiles = assign_vol_regime_quintiles(aligned["vol60"], n_quintiles=5)
    summary = aggregate_returns_by_regime(aligned["portfolio"], quintiles, periods_per_year=252)

    logger.info("=" * 60)
    logger.info("PER-QUINTILE SUMMARY (%s, %s..%s)", args.strategy, is_start, is_end)
    logger.info("=" * 60)
    logger.info(f"{'Q':5} {'count':>8} {'mean/d':>10} {'sharpe_ann':>12} {'ann_ret':>10}")
    for q in ["Q1", "Q2", "Q3", "Q4", "Q5"]:
        logger.info(
            f"{q:5} {summary.quintile_counts[q]:>8} "
            f"{summary.quintile_means[q] * 100:>9.4f}% "
            f"{summary.quintile_sharpes[q]:>+12.3f} "
            f"{summary.quintile_means[q] * 252 * 100:>+9.2f}%"
        )

    verdict = classify_cyclicality(summary)
    logger.info("=" * 60)
    logger.info(
        "VERDICT: %s | proceed=%s | R_mean=%+.3f | sign=%s",
        verdict.classification,
        verdict.proceed,
        verdict.R_mean,
        verdict.sign_pattern,
    )
    logger.info("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
