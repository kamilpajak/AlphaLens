"""Point-in-time fundamentals store backed by SimFin bulk CSV downloads.

Alternative to `HistoricalFundamentalsStore` (Alpha Vantage). SimFin's free
tier provides 5 years of quarterly financials for 5000+ US tickers via bulk
download — no per-request rate limiting, one API call loads the whole
dataset. Ideal for one-shot backtest preload where AV's 25 req/day free
tier would take ~3 weeks to populate.

Interface mirrors `HistoricalFundamentalsStore.features_as_of(ticker, date)`
so the adapter is swap-in.

Requires SIMFIN_API_KEY env var (free account from simfin.com). The `simfin`
Python package caches downloaded CSVs under the configured data directory
(default `~/.alphalens/simfin_cache/`), so only the first run pays the
~20-30 MB download cost.
"""

from __future__ import annotations

import logging
import os
from datetime import date
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)


# SimFin column names (quarterly dataset, US market). Column spellings are
# verbatim from the SimFin docs; keep in sync if the schema evolves.
_COL_CASH = "Cash, Cash Equivalents & Short Term Investments"
_COL_OCF = "Net Cash from Operating Activities"
_COL_NET_INCOME = "Net Income"
_COL_REVENUE = "Revenue"
_COL_CLOSE = "Close"
_COL_SHARES = "Shares Outstanding"


def _default_cache_dir() -> Path:
    return Path.home() / ".alphalens" / "simfin_cache"


class SimFinFundamentalsStore:
    """Preload SimFin quarterly data once; expose PIT lookups by (ticker, asof).

    Unlike the AV-based store this does NOT deep-copy a per-ticker bundle
    per call — it keeps the whole US dataset in three DataFrames indexed by
    Ticker + Report Date, and slices to the asof filter at query time.
    """

    def __init__(
        self,
        cache_dir: Path | None = None,
        api_key: str | None = None,
        with_prices: bool = False,
    ):
        """with_prices=True enables PIT P/S gate (needs ~435MB daily shareprices
        CSV cached locally). Default False — gate uses only runway / OCF /
        net_income components, which cover 54% of Layer 3 rejection reasons.
        """
        self.cache_dir = cache_dir or _default_cache_dir()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.api_key = api_key or os.environ.get("SIMFIN_API_KEY")
        if not self.api_key:
            raise RuntimeError(
                "SIMFIN_API_KEY not set. Register at simfin.com and add the "
                "key to .env, or pass api_key= explicitly."
            )
        self.with_prices = with_prices
        self._balance = None  # pd.DataFrame indexed by (Ticker, Report Date)
        self._cashflow = None
        self._income = None
        # Pre-split per-ticker price frames (key = upper ticker). Storing the
        # full 6.2M-row multi-index frame forces an O(n) slice at every lookup
        # (1226 days × 113 tickers = 138k slices → hours). Pre-splitting once
        # at preload drops this to O(1) dict lookup + per-series boolean mask.
        self._prices_by_ticker: dict[str, pd.DataFrame] | None = None
        self._preload_tickers: set[str] = set()

    def preload(self, tickers: list[str]) -> None:
        """Download/load the US quarterly datasets + daily prices.

        SimFin ships as 3-4 monolithic CSVs; per-ticker cost is O(index lookup),
        not O(API call), so the ticker list only controls which queries we
        expect to hit later (used for coverage validation, not fetching).
        """
        import simfin as sf

        sf.set_api_key(self.api_key)
        sf.set_data_dir(str(self.cache_dir))

        logger.info("Loading SimFin quarterly balance sheet (US)…")
        self._balance = sf.load_balance(variant="quarterly", market="us")
        logger.info("Loading SimFin quarterly cash flow (US)…")
        self._cashflow = sf.load_cashflow(variant="quarterly", market="us")
        logger.info("Loading SimFin quarterly income statement (US)…")
        self._income = sf.load_income(variant="quarterly", market="us")

        if self.with_prices:
            # ~435MB daily-shareprices CSV. Download speed varies by environment
            # (can be slow on throttled consumer broadband, fast on a VPS).
            # Once cached locally, simfin reads instantly.
            logger.info("Loading SimFin daily share prices (US)… [with_prices=True]")
            prices = sf.load_shareprices(variant="daily", market="us")
            # Pre-split by ticker to avoid O(n) slice at every features_as_of
            # call. Only keep columns we need — drop Open/High/Low/Volume/etc.
            # Group-iteration over a pre-sorted frame is ~1-2s for 5884 tickers.
            logger.info("Indexing share prices by ticker…")
            keep_cols = [c for c in (_COL_CLOSE, _COL_SHARES) if c in prices.columns]
            subset = prices[keep_cols]
            self._prices_by_ticker = {
                ticker.upper(): sub.droplevel("Ticker").sort_index()
                for ticker, sub in subset.groupby(level="Ticker", sort=False)
            }
            logger.info("  %d tickers indexed", len(self._prices_by_ticker))
        else:
            logger.info("Skipping shareprices load (with_prices=False) — ps_ratio will be None")

        self._preload_tickers = {t.upper() for t in tickers}
        covered = sum(
            1
            for t in self._preload_tickers
            if self._balance is not None and t in self._balance.index.get_level_values("Ticker")
        )
        logger.info(
            "SimFinFundamentalsStore preload: %d/%d requested tickers have balance sheet data",
            covered,
            len(self._preload_tickers),
        )
        # Mirror the AV store's failure-threshold abort — if <50% coverage the
        # backtest result would be dominated by tickers without gate data.
        if self._preload_tickers and covered / len(self._preload_tickers) < 0.5:
            raise RuntimeError(
                f"SimFin covered only {covered}/{len(self._preload_tickers)} tickers "
                f"(< 50% threshold). Check ticker symbols against SimFin universe."
            )

    def features_as_of(self, ticker: str, asof: date) -> dict | None:
        """PIT features for (ticker, asof). Filters SimFin reports to those
        published on or before asof, then computes the canonical feature dict.
        """
        import pandas as pd

        up = ticker.upper()
        if self._balance is None:
            return None

        # Slice each dataframe to (ticker, reports published by asof).
        asof_ts = pd.Timestamp(asof)

        def _slice(df):
            if df is None or up not in df.index.get_level_values("Ticker"):
                return None
            try:
                sub = df.loc[up]
            except KeyError:
                return None
            # SimFin frames are indexed by Report Date (fiscal quarter end); the
            # actual 10-Q/10-K filing lag is ~40-60 days. Filter by Publish Date
            # so the backtest only sees reports that were publicly available.
            publish = pd.to_datetime(sub["Publish Date"])
            mask = publish <= asof_ts
            filtered = sub[mask].sort_index()
            return filtered if not filtered.empty else None

        bs = _slice(self._balance)
        cf = _slice(self._cashflow)
        inc = _slice(self._income)

        features: dict = {
            "cash_runway_months": _runway_from_frames(bs, cf),
            "ps_ratio": _ps_ratio_pit(self._prices_by_ticker, inc, up, asof_ts),
            "net_income_ttm": _net_income_ttm_from_frame(inc),
            "consecutive_neg_ocf_quarters": _consecutive_neg_ocf_from_frame(cf),
        }
        return features


# ---- Helpers (pure functions, tested separately) ----------------------------


def _runway_from_frames(bs, cf) -> float | None:
    """Cash runway in months using trailing-4-quarter avg OCF."""
    if bs is None or cf is None or bs.empty or cf.empty:
        return None
    cash = bs[_COL_CASH].iloc[-1]
    if cash is None or cash != cash or cash <= 0:  # NaN check via != self
        return None
    recent_ocf = cf[_COL_OCF].tail(4).dropna()
    if recent_ocf.empty:
        return None
    avg_ocf = float(recent_ocf.mean())
    if avg_ocf >= 0:
        return None  # cash-flow positive
    return float(cash) / (-avg_ocf) * 3.0


def _net_income_ttm_from_frame(inc) -> float | None:
    if inc is None or inc.empty:
        return None
    last4 = inc[_COL_NET_INCOME].tail(4).dropna()
    if len(last4) < 4:
        return None
    return float(last4.sum())


def _consecutive_neg_ocf_from_frame(cf) -> int:
    if cf is None or cf.empty:
        return 0
    streak = 0
    # iterate newest → oldest
    for v in cf[_COL_OCF].iloc[::-1]:
        if v is None or v != v or v >= 0:
            break
        streak += 1
    return streak


def _ps_ratio_pit(prices_by_ticker, inc, ticker: str, asof_ts) -> float | None:
    """PIT P/S = market_cap(close × shares_outstanding) / revenue_ttm.

    `prices_by_ticker` is a pre-split dict `{ticker: DataFrame[Close, Shares]}`
    built once at preload — O(1) lookup here. Uses most recent row with
    Date ≤ asof. Returns None when prices not loaded, ticker unknown, or
    asof falls before the ticker's first available bar.
    """
    if prices_by_ticker is None or inc is None or inc.empty:
        return None
    ticker_prices = prices_by_ticker.get(ticker)
    if ticker_prices is None or ticker_prices.empty:
        return None
    # ticker_prices index is sorted ascending; use searchsorted for O(log n) asof.
    idx = ticker_prices.index.searchsorted(asof_ts, side="right") - 1
    if idx < 0:
        return None
    row = ticker_prices.iloc[idx]
    close = row.get(_COL_CLOSE)
    shares = row.get(_COL_SHARES)
    if close is None or shares is None or close != close or shares != shares:
        return None
    market_cap = float(close) * float(shares)
    if market_cap <= 0:
        return None

    rev_ttm_series = inc[_COL_REVENUE].tail(4).dropna()
    if len(rev_ttm_series) < 4:
        return None
    rev_ttm = float(rev_ttm_series.sum())
    if rev_ttm <= 0:
        return None
    return market_cap / rev_ttm
