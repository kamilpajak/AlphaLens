"""Build a daily short-term reversal (STR) factor a la Jegadeesh (1990).

Construction:
  At each trading day t, rank universe by prior 21-trading-day return
  (close_t / close_{t-21} - 1). Form decile portfolios.
  STR_{t+1} = mean_return(bottom_decile) - mean_return(top_decile),
  using day-(t+1) close-to-close returns.

Bottom decile = biggest losers over prior month → buying them (long).
Top decile = biggest winners → shorting them.
Net STR captures the 1-month reversal premium.

Universe: all tickers in the cached price database (~/.alphalens/prices/).
This is intentionally non-PIT — STR is meant as a *factor* (return premium
proxy), parallel to Ken French factors. It is not used as a trading signal.

Output: ~/.alphalens/factors/str_daily.csv with columns [date, STR].
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from alphalens.alt_data.yfinance_cache import load_cached_histories

logger = logging.getLogger(__name__)

_PRICES_DIR = Path.home() / ".alphalens" / "prices"
_OUT_PATH = Path.home() / ".alphalens" / "factors" / "str_daily.csv"


def build_returns_panel(prices_dir: Path) -> pd.DataFrame:
    """Wide DataFrame: rows=date, cols=ticker, values=daily return (close-to-close)."""
    parquets = sorted(prices_dir.glob("*.parquet"))
    if not parquets:
        raise FileNotFoundError(f"no parquet price files in {prices_dir}")
    logger.info("loading %d ticker parquets", len(parquets))
    tickers = [p.stem for p in parquets]
    histories = load_cached_histories(tickers, prices_dir)

    closes = {}
    for ticker, df in histories.items():
        if df is None or df.empty:
            continue
        s = df["close"].copy()
        s.index = pd.DatetimeIndex(s.index).tz_localize(None)
        closes[ticker] = s
    if not closes:
        raise RuntimeError("no usable close data")
    panel = pd.DataFrame(closes)
    panel = panel.sort_index()
    returns = panel.pct_change()
    logger.info("returns panel: %d rows × %d tickers", len(returns), len(returns.columns))
    return returns


def compute_str_factor(
    returns: pd.DataFrame, formation_days: int = 21, min_obs: int = 100
) -> pd.Series:
    """Daily STR factor.

    For each date t with sufficient prior history:
      1. compute prior-21d cumulative return per ticker (using formation window ending at t-1)
      2. rank surviving tickers cross-sectionally; form deciles
      3. STR_t = mean(return_t for bottom_decile_at_t-1) - mean(return_t for top_decile_at_t-1)

    The formation window ends at t-1 to avoid look-ahead: portfolio composition
    is set BEFORE the day t return is realized.
    """
    log1p = np.log1p(returns.fillna(0.0))
    cumulative = log1p.rolling(window=formation_days, min_periods=formation_days).sum()
    formation = np.expm1(cumulative)
    prior_formation = formation.shift(1)

    str_series: list[float] = []
    str_dates: list[pd.Timestamp] = []
    for ts in returns.index[formation_days + 1 :]:
        formation_row = prior_formation.loc[ts].dropna()
        if len(formation_row) < min_obs:
            continue
        deciles = pd.qcut(formation_row, 10, labels=False, duplicates="drop")
        if deciles.isna().all():
            continue
        bottom_tickers = formation_row.index[deciles == 0]
        top_tickers = formation_row.index[deciles == deciles.max()]
        ret_today = returns.loc[ts]
        bottom_ret = ret_today.reindex(bottom_tickers).dropna().mean()
        top_ret = ret_today.reindex(top_tickers).dropna().mean()
        if pd.isna(bottom_ret) or pd.isna(top_ret):
            continue
        str_series.append(float(bottom_ret - top_ret))
        str_dates.append(ts)

    return pd.Series(str_series, index=pd.DatetimeIndex(str_dates), name="STR")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--formation-days", type=int, default=21)
    ap.add_argument("--min-obs", type=int, default=100)
    ap.add_argument("--start", type=date.fromisoformat, default=date(2010, 1, 1))
    ap.add_argument("--out", type=Path, default=_OUT_PATH)
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    returns = build_returns_panel(_PRICES_DIR)
    returns = returns.loc[returns.index >= pd.Timestamp(args.start)]
    logger.info("computing STR factor with formation=%dd", args.formation_days)
    str_factor = compute_str_factor(
        returns, formation_days=args.formation_days, min_obs=args.min_obs
    )
    logger.info(
        "STR series: %d obs, mean=%.3fbps/d, ann=%.2f%%, std=%.3f%%, sharpe=%.2f",
        len(str_factor),
        float(str_factor.mean() * 10_000),
        float(str_factor.mean() * 252 * 100),
        float(str_factor.std() * 100),
        float(str_factor.mean() / str_factor.std() * np.sqrt(252)) if str_factor.std() > 0 else 0.0,
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    str_factor.to_csv(args.out, header=True, index_label="date")
    logger.info("wrote → %s", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
