"""Point-in-time fundamentals store backed by SimFin bulk CSV downloads.

Alternative to `HistoricalFundamentalsStore` (Alpha Vantage). SimFin's free
tier provides 5 years of quarterly financials for 5000+ US tickers via bulk
download — no per-request rate limiting, one API call loads the whole
dataset. Ideal for one-shot backtest preload where AV's 25 req/day free
tier would take ~3 weeks to populate.

Interface mirrors `HistoricalFundamentalsStore.features_as_of(ticker, date)`
so the adapter is swap-in.

Requires SIMFIN_API_KEY env var (free account from simfin.com). The `simfin`
Python package caches downloaded CSVs under the configured data directory.
Resolution order: explicit `cache_dir=` kwarg > `SIMFIN_DATA_DIR` env var >
default `~/.alphalens/simfin_cache/`. The env var hook is used by the audit
orchestrator to redirect subprocesses to pod-local NVMe when running on
runpod (avoids MooseFS network-FS contention).
"""

from __future__ import annotations

import logging
import math
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
# Additional columns used by ev_fcff_features_as_of()
_COL_CAPEX_RAW = "Change in Fixed Assets & Intangibles"  # signed negative when capex positive
_COL_INTEREST = "Interest Expense, Net"
_COL_PRETAX = "Pretax Income (Loss)"
_COL_LT_DEBT = "Long Term Debt"
_COL_ST_DEBT = "Short Term Debt"
_COL_PUBLISH_DATE = "Publish Date"

# Effective tax rate floor/ceiling per ev_fcff_yield design memo §3
_TAX_RATE_FLOOR = 0.0
_TAX_RATE_CEILING = 0.35
# Minimum quarters required to compute a 5y FCF margin median — too few
# observations leaves the imputation dominated by noise. 8 quarters = 2 years
# of history is the published minimum-data threshold for cross-sectional
# fundamental quality factors (Asness QMJ 2018 footnote 9).
_MIN_QTRS_FCF_MARGIN_MEDIAN = 8
_LOOKBACK_QTRS_FCF_MARGIN = 20  # 5 years of quarters


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
        if cache_dir is not None:
            self.cache_dir = cache_dir
        else:
            env_dir = os.environ.get("SIMFIN_DATA_DIR")
            self.cache_dir = Path(env_dir) if env_dir else _default_cache_dir()
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

    def ev_fcff_features_as_of(self, ticker: str, asof: date) -> dict | None:
        """PIT snapshot for ``alphalens.screeners.ev_fcff_yield.scorer``.

        Returns the 11-field dict consumed by ``score_ev_fcff_yield``:

        - ``ocf_ttm``, ``capex_ttm`` (positive-signed), ``interest_expense_ttm``,
          ``tax_rate`` (clamped to [0, 0.35]), ``revenue_ttm``
        - ``fcf_margin_5y_median`` (rolling 20-quarter median of
          ``(OCF − Capex) / Revenue``, or ``None`` if < 8 quarters available)
        - ``price``, ``shares_outstanding``
        - ``long_term_debt``, ``short_term_debt``, ``cash_and_equivalents``

        Returns ``None`` if the ticker is unknown, has no fundamentals
        published on/before ``asof``, or lacks price data (when the store
        was constructed with ``with_prices=True`` — otherwise ``price`` /
        ``shares_outstanding`` are ``None`` and the scorer will drop).
        """
        up = ticker.upper()
        if self._balance is None or self._cashflow is None or self._income is None:
            return None
        asof_ts = pd.Timestamp(asof)

        def _slice(df: pd.DataFrame | None) -> pd.DataFrame | None:
            if df is None or up not in df.index.get_level_values("Ticker"):
                return None
            try:
                sub = df.loc[up]
            except KeyError:
                return None
            publish = pd.to_datetime(sub[_COL_PUBLISH_DATE])
            mask = publish <= asof_ts
            filtered = sub[mask].sort_index()
            return filtered if not filtered.empty else None

        bs = _slice(self._balance)
        cf = _slice(self._cashflow)
        inc = _slice(self._income)
        if bs is None or cf is None or inc is None:
            return None

        # TTM = sum of last 4 quarterly values (sorted by Report Date ascending).
        ocf_ttm = _ttm_sum(cf, _COL_OCF)
        capex_raw_ttm = _ttm_sum(cf, _COL_CAPEX_RAW)
        # SimFin signs Change in Fixed Assets & Intangibles negative when the
        # firm INVESTS in fixed assets — i.e. capex is reported as negative.
        # Flip the sign so capex_ttm is positive for an investing firm.
        capex_ttm = -capex_raw_ttm if capex_raw_ttm is not None else None
        interest_ttm = _ttm_sum(inc, _COL_INTEREST)
        revenue_ttm = _ttm_sum(inc, _COL_REVENUE)
        net_income_ttm = _ttm_sum(inc, _COL_NET_INCOME)

        # Most-recent Publish Date across the 3 statement frames — surfaces
        # the freshness of the underlying filing so downstream consumers can
        # detect stale fundamentals (per zen review 2026-05-17).
        publish_dates = []
        for frame in (bs, cf, inc):
            if frame is not None and _COL_PUBLISH_DATE in frame.columns:
                frame_max = pd.to_datetime(frame[_COL_PUBLISH_DATE]).max()
                if pd.notna(frame_max):
                    publish_dates.append(frame_max)
        latest_publish = max(publish_dates) if publish_dates else None
        publish_date_str = (
            latest_publish.strftime("%Y-%m-%d") if latest_publish is not None else None
        )

        # Effective tax rate from latest available quarterly Pretax / NetIncome.
        # Formula: τ = 1 − NetIncome / Pretax, clamped to [0, 0.35].
        pretax_latest = _latest_value(inc, _COL_PRETAX)
        ni_latest = _latest_value(inc, _COL_NET_INCOME)
        tax_rate = _effective_tax_rate(pretax_latest, ni_latest)

        ltd_latest = _latest_value(bs, _COL_LT_DEBT)
        std_latest = _latest_value(bs, _COL_ST_DEBT)
        cash_latest = _latest_value(bs, _COL_CASH)

        # 5y rolling FCF margin median (last 20 quarters), used for imputation
        # when current FCFF is non-positive (per design memo §4).
        fcf_margin_5y = _fcf_margin_median(cf, inc)

        # Latest price + shares from prices_by_ticker (None if with_prices=False).
        price: float | None = None
        shares: float | None = None
        if self._prices_by_ticker is not None:
            ticker_prices = self._prices_by_ticker.get(up)
            if ticker_prices is not None and not ticker_prices.empty:
                idx = ticker_prices.index.searchsorted(asof_ts, side="right") - 1
                if idx >= 0:
                    row = ticker_prices.iloc[idx]
                    raw_close = row.get(_COL_CLOSE)
                    raw_shares = row.get(_COL_SHARES)
                    if raw_close is not None and not math.isnan(raw_close):
                        price = float(raw_close)
                    if raw_shares is not None and not math.isnan(raw_shares):
                        shares = float(raw_shares)

        return {
            "ocf_ttm": ocf_ttm,
            "capex_ttm": capex_ttm,
            "interest_expense_ttm": interest_ttm,
            "tax_rate": tax_rate,
            "revenue_ttm": revenue_ttm,
            "fcf_margin_5y_median": fcf_margin_5y,
            "price": price,
            "shares_outstanding": shares,
            "long_term_debt": ltd_latest,
            "short_term_debt": std_latest,
            "cash_and_equivalents": cash_latest,
            "net_income_ttm": net_income_ttm,
            "publish_date_str": publish_date_str,
        }


# ---- Helpers (pure functions, tested separately) ----------------------------


def _runway_from_frames(bs, cf) -> float | None:
    """Cash runway in months using trailing-4-quarter avg OCF."""
    if bs is None or cf is None or bs.empty or cf.empty:
        return None
    cash = bs[_COL_CASH].iloc[-1]
    if cash is None or math.isnan(cash) or cash <= 0:
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
        if v is None or math.isnan(v) or v >= 0:
            break
        streak += 1
    return streak


# PIT data lookup has 8 distinct missing-data paths; each warrants a None return.
def _ps_ratio_pit(prices_by_ticker, inc, ticker: str, asof_ts) -> float | None:  # noqa: PLR0911
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
    if close is None or shares is None or math.isnan(close) or math.isnan(shares):
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


# ---- Helpers for ev_fcff_features_as_of() ----------------------------------


def _ttm_sum(df: pd.DataFrame | None, column: str) -> float | None:
    """Sum the last 4 quarterly values of ``column`` in a per-ticker frame.

    Returns ``None`` when df is empty, the column is absent, or fewer than 4
    non-NaN observations are available — TTM requires exactly 4 trailing
    quarters for proper annualization.
    """
    if df is None or df.empty or column not in df.columns:
        return None
    last4 = df[column].tail(4).dropna()
    if len(last4) < 4:
        return None
    return float(last4.sum())


def _latest_value(df: pd.DataFrame | None, column: str) -> float | None:
    """Most recent non-NaN value in ``column`` of a per-ticker frame."""
    if df is None or df.empty or column not in df.columns:
        return None
    series = df[column].dropna()
    if series.empty:
        return None
    return float(series.iloc[-1])


def _effective_tax_rate(pretax: float | None, net_income: float | None) -> float:
    """``1 − NI / Pretax`` clamped to ``[_TAX_RATE_FLOOR, _TAX_RATE_CEILING]``.

    Returns ``_TAX_RATE_CEILING`` floor of 0.21 (federal US corporate rate
    2018+) when pretax is non-positive or net income exceeds pretax (negative
    effective tax, which can happen with NOL carryforwards but breaks the
    formula). The 0.21 default is the most conservative non-zero rate for
    unlevered FCF computation — overstates tax shield slightly vs zero-tax
    which would give too much capex addback.
    """
    if pretax is None or net_income is None:
        return 0.21
    if pretax <= 0 or net_income > pretax:
        return 0.21
    raw = 1.0 - net_income / pretax
    if not math.isfinite(raw):
        return 0.21
    return max(_TAX_RATE_FLOOR, min(_TAX_RATE_CEILING, raw))


def _fcf_margin_median(cf: pd.DataFrame | None, inc: pd.DataFrame | None) -> float | None:
    """Rolling 20-quarter median of ``(OCF − Capex) / Revenue``.

    Returns ``None`` if fewer than ``_MIN_QTRS_FCF_MARGIN_MEDIAN`` non-NaN
    margins can be computed. The denominator (Revenue) must be positive to
    contribute to the margin sample — zero/negative-revenue quarters are
    skipped, not zeroed.
    """
    if cf is None or inc is None or cf.empty or inc.empty:
        return None
    # Join on Report Date to avoid sign / scale mismatch from independent slices.
    ocf = cf[_COL_OCF] if _COL_OCF in cf.columns else None
    capex_raw = cf[_COL_CAPEX_RAW] if _COL_CAPEX_RAW in cf.columns else None
    revenue = inc[_COL_REVENUE] if _COL_REVENUE in inc.columns else None
    if ocf is None or capex_raw is None or revenue is None:
        return None
    joined = pd.concat({"ocf": ocf, "capex_raw": capex_raw, "revenue": revenue}, axis=1).dropna()
    if joined.empty:
        return None
    # Quarterly FCF = OCF + capex_raw (raw is negative for invest direction).
    fcf_quarterly = joined["ocf"] + joined["capex_raw"]
    margin = fcf_quarterly / joined["revenue"]
    margin = margin[joined["revenue"] > 0]
    margin = margin.tail(_LOOKBACK_QTRS_FCF_MARGIN).dropna()
    if len(margin) < _MIN_QTRS_FCF_MARGIN_MEDIAN:
        return None
    return float(margin.median())
