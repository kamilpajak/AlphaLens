"""Canonical SEC EDGAR fundamentals store for AlphaLens.

Returns the 16-field features dict that the thematic scorers consume,
backed by SEC XBRL companyfacts parquets at
``~/.alphalens/companyfacts_parquet/{CIK}.parquet``. Missing CIKs are
fetched on demand via :class:`alphalens_pipeline.data.alt_data.sec_edgar_client.SecEdgarClient`
(throttled to SEC's 10 req/s polite limit, retry/backoff included).

The 16-field parity contract was originally defined by the now-deleted
SimFin store; downstream scorers in
``alphalens_pipeline/thematic/screening/{fcff_signal,valuation_signal,magic_formula}.py``
work via single-line import swap. Validation gate evidence:
``docs/research/edgar_fundamentals_validation_2026_05_19.md``.

Implementation notes callers do NOT need to handle:

- EDGAR ``CapEx`` is reported with positive sign (cash outflow magnitude),
  not negated as in SimFin. No client-side sign flip needed — but the
  parity contract preserves SimFin's positive convention, so callers see
  the same sign either way.
- ``tax_rate`` is derived from ``IncomeTaxExpenseBenefit / PreTaxIncome``
  and clamped to ``[0, 0.35]`` (SimFin parity); defaults to 0.21 when the
  components are missing.
- ``long_term_debt`` and ``short_term_debt`` use a debt-free fallback to
  ``0.0`` when the issuer has filed at least one balance sheet but never
  a debt row — fixes the gap that broke EV/EBITDA for MANH-class tickers.
- ``shares_outstanding`` follows a 3-tier chain (issue #172 Bug 1):
  1. ``dei:EntityCommonStockSharesOutstanding`` — modern primary (cover-
     page disclosure, often fresher than the balance-sheet tag).
  2. ``us-gaap:CommonStockSharesOutstanding`` — legacy fallback.
  Both XBRL tiers apply a 180-day staleness gate (issuers like C3.ai
  populated us-gaap once at IPO and never refreshed it).
  3. yfinance ``Ticker.get_shares_full`` / ``fast_info.shares`` —
     external fallback when both XBRL chains are missing or stale.
"""

from __future__ import annotations

import logging
import math
import os
from datetime import date
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from alphalens_pipeline.data.alt_data.sec_edgar_client import SecEdgarClient
from alphalens_pipeline.data.fundamentals import concept_chains as chains
from alphalens_pipeline.data.fundamentals.annual_aggregator import (
    AnnualStatement,
    annual_statements,
)
from alphalens_pipeline.data.fundamentals.capital_allocation import (
    CapitalAllocation,
    compute_buyback_proxy,
)
from alphalens_pipeline.data.fundamentals.companyfacts_parquet import (
    CompanyfactsParquetReader,
    companyfacts_json_to_parquet_table,
)
from alphalens_pipeline.data.fundamentals.edgar_companyfacts import _pit_filter
from alphalens_pipeline.data.fundamentals.owner_earnings import (
    OwnerEarnings,
    compute_owner_earnings,
)
from alphalens_pipeline.data.fundamentals.ttm_aggregator import (
    _arrow_table_to_entries,
    compute_ttm,
    fcf_margin_rolling_median,
    has_any_concept,
    latest_instant,
)

logger = logging.getLogger(__name__)

DEFAULT_PARQUET_DIR = Path.home() / ".alphalens" / "companyfacts_parquet"
DEFAULT_USER_AGENT = "AlphaLens-fundamentals pajakkamil@gmail.com"
USER_AGENT_ENV = "SEC_EDGAR_USER_AGENT"

# SimFin parity: clamp tax_rate to this range and default when missing.
_TAX_RATE_MIN = 0.0
_TAX_RATE_MAX = 0.35
_TAX_RATE_DEFAULT = 0.21

# Shares-outstanding XBRL freshness window. Issuers file 10-Q every ~90
# days; 180 days covers one missed quarter plus filing lag. See
# `docs/research/edgar_fundamentals_data_quality_2026_05_20.md` and
# Perplexity research persisted under `~/.claude/projects/.../tool-results/`.
SHARES_MAX_AGE_DAYS = 180


class EdgarFundamentalsStore:
    """PIT fundamentals store backed by SEC XBRL companyfacts.

    Canonical fundamentals source for AlphaLens. The 16-field dict
    returned by :meth:`ev_fcff_features_as_of` is the parity contract that
    thematic scorers in
    ``alphalens_pipeline/thematic/screening/{fcff_signal,valuation_signal,magic_formula}.py``
    consume.

    Parameters
    ----------
    cache_dir
        Local parquet cache root. Defaults to ``~/.alphalens/companyfacts_parquet/``.
    with_prices
        Reserved for future price-source integration. EDGAR has no quotes —
        the thematic pipeline pairs EDGAR shares with yfinance closes via
        :mod:`alphalens_pipeline.thematic.verification.mcap_filter`. Today this flag
        only controls whether ``price`` and ``shares_outstanding`` appear in
        the returned dict; when ``False``, ``price`` is ``None`` and
        ``shares_outstanding`` is sourced from EDGAR regardless.
    sec_client
        Injectable :class:`SecEdgarClient`; defaults to one instantiated
        from the ``SEC_EDGAR_USER_AGENT`` env var (or a sensible default).
        Tests pass a stub.
    """

    def __init__(
        self,
        cache_dir: Path | str | None = None,
        *,
        with_prices: bool = False,
        sec_client: SecEdgarClient | None = None,
    ) -> None:
        self._dir = Path(cache_dir) if cache_dir is not None else DEFAULT_PARQUET_DIR
        self._dir.mkdir(parents=True, exist_ok=True)
        self._with_prices = with_prices
        self._sec_client = sec_client or self._default_sec_client()
        self._reader = CompanyfactsParquetReader(self._dir)
        # ticker (upper) -> 10-digit CIK; populated by preload() + on-demand.
        self._ticker_to_cik: dict[str, str] = {}
        self._loaded_tickers: bool = False
        # ticker (upper) -> latest close from yfinance batch in preload().
        self._prices: dict[str, float] = {}
        # ticker (upper) -> shares from yfinance 3rd-tier fallback.
        self._shares_cache: dict[str, float | None] = {}

    @staticmethod
    def _default_sec_client() -> SecEdgarClient:
        ua = os.environ.get(USER_AGENT_ENV) or DEFAULT_USER_AGENT
        return SecEdgarClient(user_agent=ua)

    # --- ticker → CIK resolution ------------------------------------------

    def _load_ticker_map(self) -> None:
        if self._loaded_tickers:
            return
        try:
            payload = self._sec_client.fetch_company_tickers()
        except Exception as exc:  # network / parse fail — caller can still hit cached parquets
            logger.warning("SEC company_tickers.json fetch failed: %s", exc)
            payload = {}
        for entry in payload.values():
            if not isinstance(entry, dict):
                continue
            t = entry.get("ticker")
            cik = entry.get("cik_str")
            if t and cik is not None:
                self._ticker_to_cik[str(t).upper()] = str(cik).zfill(10)
        self._loaded_tickers = True

    def _cik_for(self, ticker: str) -> str | None:
        self._load_ticker_map()
        return self._ticker_to_cik.get(ticker.upper())

    # --- universe + preload -----------------------------------------------

    def universe(self) -> list[str]:
        """Tickers whose CIK has a parquet on disk.

        Replaces direct ``._income.index.get_level_values('Ticker')`` access
        in ``scripts/experiment_ev_fcff_yield.py`` — exposes the queryable
        ticker universe without leaking internals.
        """
        self._load_ticker_map()
        cik_to_ticker = {cik: ticker for ticker, cik in self._ticker_to_cik.items()}
        available: list[str] = []
        for path in self._dir.glob("*.parquet"):
            cik = path.stem
            ticker = cik_to_ticker.get(cik)
            if ticker is not None:
                available.append(ticker)
        return sorted(set(available))

    def preload(self, tickers: list[str]) -> None:
        """Fetch + cache companyfacts parquets for any ticker missing locally.

        Idempotent — skips tickers whose parquet already exists. First call
        with a cold cache pays ~12s per 100 missing tickers (SEC throttle
        10 req/s); subsequent calls are free.
        """
        self._load_ticker_map()
        missing_ciks: list[tuple[str, str]] = []
        for ticker in tickers:
            cik = self._cik_for(ticker)
            if cik is None:
                logger.warning("ticker %s unresolved (no CIK from SEC), skipping", ticker)
                continue
            if not (self._dir / f"{cik}.parquet").exists():
                missing_ciks.append((ticker, cik))
        if missing_ciks:
            logger.info("preload: fetching %d missing companyfacts from SEC", len(missing_ciks))
            for ticker, cik in missing_ciks:
                try:
                    facts = self._sec_client.fetch_company_facts(cik)
                except Exception as exc:
                    logger.warning("companyfacts fetch failed for %s/%s: %s", ticker, cik, exc)
                    continue
                table = companyfacts_json_to_parquet_table(facts)
                pq.write_table(table, self._dir / f"{cik}.parquet")
        # Batch-fetch prices for all tickers in one yfinance round-trip even
        # when no companyfacts are missing — otherwise warm-cache runs would
        # never populate prices and fall through to per-ticker fast_info.
        if self._with_prices and tickers:
            self._batch_fetch_prices(tickers)

    # --- the parity contract: 16-field features dict ---------------------

    def ev_fcff_features_as_of(self, ticker: str, asof: date) -> dict[str, Any] | None:
        """Return the 16-field features dict, or None when no CIK / no data.

        Field-by-field parity with the now-deleted SimFin store's
        ``ev_fcff_features_as_of`` so the downstream scorers consume the
        same shape regardless of the migration history.
        """
        cik = self._cik_for(ticker)
        if cik is None:
            return None
        # Trigger on-demand fetch if the parquet is missing.
        if not (self._dir / f"{cik}.parquet").exists():
            self.preload([ticker])
        # Re-check; if still missing the fetch failed.
        if self._reader.get_cik_table(cik) is None:
            return None

        # Duration concepts (TTM, Compustat formula).
        revenue_ttm = compute_ttm(self._reader, cik, chains.REVENUE, asof)
        operating_income_ttm = compute_ttm(self._reader, cik, chains.OPERATING_INCOME, asof)
        ocf_ttm = compute_ttm(self._reader, cik, chains.OPERATING_CASH_FLOW, asof)
        capex_ttm = compute_ttm(self._reader, cik, chains.CAPEX, asof)
        net_income_ttm = compute_ttm(self._reader, cik, chains.NET_INCOME, asof)

        # D&A: try the single-tag chain first, fall back to summing
        # components when neither single tag is present.
        da_ttm = compute_ttm(self._reader, cik, chains.DEPRECIATION_AMORTISATION, asof)
        if da_ttm is None:
            components = [
                compute_ttm(self._reader, cik, (c,), asof)
                for c in chains.DEPRECIATION_AMORTISATION_COMPONENTS
            ]
            present = [c for c in components if c is not None]
            if present:
                da_ttm = sum(present)

        # Interest expense: None when issuer doesn't break it out (often
        # debt-free); SimFin parity expects None in that case.
        interest_expense_ttm = compute_ttm(self._reader, cik, chains.INTEREST_EXPENSE, asof)

        # Tax rate: clamp + default per SimFin parity.
        tax_expense = compute_ttm(self._reader, cik, chains.INCOME_TAX_EXPENSE, asof)
        pretax_income = compute_ttm(self._reader, cik, chains.PRETAX_INCOME, asof)
        tax_rate = self._derive_tax_rate(tax_expense, pretax_income)

        # Instant concepts (balance sheet).
        cash_and_equivalents = latest_instant(self._reader, cik, chains.CASH, asof)
        total_equity = latest_instant(self._reader, cik, chains.EQUITY, asof)

        # Debt with debt-free fallback (MANH gap fix).
        long_term_debt = latest_instant(self._reader, cik, chains.LONG_TERM_DEBT, asof)
        short_term_debt = latest_instant(self._reader, cik, chains.SHORT_TERM_DEBT, asof)
        if (
            long_term_debt is None
            and short_term_debt is None
            and has_any_concept(self._reader, cik, chains.BALANCE_SHEET_MARKERS, asof)
        ):
            # Issuer files balance sheets but never reports debt rows.
            # Treat as structurally zero rather than missing.
            long_term_debt = 0.0
            short_term_debt = 0.0

        # Shares outstanding — 3-tier chain per issue #172 Bug 1:
        # (1) dei (modern primary, cover-page disclosure)
        # (2) us-gaap (legacy fallback)
        # Both XBRL tiers apply a 180-day age gate to defend against
        # issuers (e.g. C3.ai) whose tag was populated once at IPO and
        # never updated. (3) yfinance fallback when both XBRL tiers miss.
        shares_outstanding = latest_instant(
            self._reader,
            cik,
            chains.SHARES_OUTSTANDING_DEI,
            asof,
            taxonomy="dei",
            unit="shares",
            max_age_days=SHARES_MAX_AGE_DAYS,
        )
        if shares_outstanding is None:
            shares_outstanding = latest_instant(
                self._reader,
                cik,
                chains.SHARES_OUTSTANDING_US_GAAP,
                asof,
                unit="shares",
                max_age_days=SHARES_MAX_AGE_DAYS,
            )
        if shares_outstanding is None:
            shares_outstanding = self._fetch_shares_yf(ticker, asof)

        # Price: not in EDGAR. When ``with_prices=True`` we pull a current
        # close from yfinance. Snapshot, not PIT — acceptable for live
        # thematic briefs (asof ≈ today); historical replay would need a
        # different price source. SimFin parity reserves the field.
        price = self._fetch_price(ticker, asof) if self._with_prices else None

        # FCF margin 5y median: rolling 20-quarter window. Returns None when
        # fewer than 8 quarter-aligned data points are visible at asof — too
        # thin for a stable median. ~90% of S&P 500-class issuers clear the
        # bar (probe 2026-05-20). When None, downstream impute_fcff /
        # _effective_fcf_margin gracefully fall back to the spot TTM margin.
        # We pass the already-derived TTM tax_rate as the per-quarter proxy:
        # per-quarter tax rates are too noisy and a TTM-stable rate yields a
        # consistent FCFF tax shield across the 20-quarter window.
        fcf_margin_5y_median = fcf_margin_rolling_median(self._reader, cik, asof, tax_rate=tax_rate)

        publish_date_str = self._latest_publish_date(cik, asof)

        return {
            "ocf_ttm": ocf_ttm,
            "capex_ttm": capex_ttm,
            "interest_expense_ttm": interest_expense_ttm,
            "tax_rate": tax_rate,
            "revenue_ttm": revenue_ttm,
            "fcf_margin_5y_median": fcf_margin_5y_median,
            "price": price,
            "shares_outstanding": shares_outstanding,
            "long_term_debt": long_term_debt,
            "short_term_debt": short_term_debt,
            "cash_and_equivalents": cash_and_equivalents,
            "net_income_ttm": net_income_ttm,
            "publish_date_str": publish_date_str,
            "operating_income_ttm": operating_income_ttm,
            "total_equity": total_equity,
            "da_ttm": da_ttm,
        }

    def annual_series_as_of(
        self, ticker: str, asof: date, *, max_years: int = 10
    ) -> list[AnnualStatement]:
        """Multi-year annual (FY) statement series, PIT-correct at ``asof``.

        Unlike :meth:`ev_fcff_features_as_of` (a single TTM-at-asof
        snapshot), this returns the full per-fiscal-year history — newest
        first, capped to ``max_years`` — for margin / capital-intensity
        trend analysis and DCF history. Empty list when the ticker has no
        CIK or no companyfacts on disk; triggers an on-demand fetch when the
        parquet is missing, mirroring :meth:`ev_fcff_features_as_of`.
        """
        cik = self._cik_for(ticker)
        if cik is None:
            return []
        if not (self._dir / f"{cik}.parquet").exists():
            self.preload([ticker])
        return annual_statements(self._reader, cik, asof, max_years=max_years)

    def owner_earnings_as_of(
        self, ticker: str, asof: date, *, max_years: int = 10
    ) -> list[OwnerEarnings]:
        """Per-fiscal-year owner earnings + working-capital deltas, PIT at ``asof``.

        Delegates to
        :func:`alphalens_pipeline.data.fundamentals.owner_earnings.compute_owner_earnings`
        over the :meth:`annual_series_as_of` series — newest first, capped to
        ``max_years``. The oldest year has no prior fiscal year, so its
        ``working_capital_change`` (and hence ``owner_earnings``) is ``None``.
        ``maintenance_capex`` is the ``min(capex, D&A)`` approximation; see the
        owner-earnings module docstring. Additive and unwired — not consumed by
        the thematic brief pipeline. Empty list when the ticker has no CIK or no
        companyfacts on disk.
        """
        return compute_owner_earnings(self.annual_series_as_of(ticker, asof, max_years=max_years))

    def capital_allocation_as_of(
        self, ticker: str, asof: date, *, max_years: int = 10
    ) -> list[CapitalAllocation]:
        """Per-fiscal-year buyback proxy (Δ shares outstanding YoY), PIT at ``asof``.

        Delegates to
        :func:`alphalens_pipeline.data.fundamentals.capital_allocation.compute_buyback_proxy`
        over the :meth:`annual_series_as_of` series — newest first, capped to
        ``max_years``. ``net_buyback`` reports only the SIGN of the share-count
        change (a fall = net buyback, a rise = net issuance / dilution), not the
        dollar amount; see the capital-allocation module docstring. The oldest
        year has no prior fiscal year, so its change fields are ``None``.
        Additive and unwired — not consumed by the thematic brief pipeline.
        Empty list when the ticker has no CIK or no companyfacts on disk.
        """
        return compute_buyback_proxy(self.annual_series_as_of(ticker, asof, max_years=max_years))

    # --- internals --------------------------------------------------------

    @staticmethod
    def _derive_tax_rate(tax_expense: float | None, pretax_income: float | None) -> float:
        if tax_expense is None or pretax_income is None or pretax_income == 0:
            return _TAX_RATE_DEFAULT
        try:
            rate = tax_expense / pretax_income
        except ZeroDivisionError:
            return _TAX_RATE_DEFAULT
        if math.isnan(rate):
            return _TAX_RATE_DEFAULT
        return max(_TAX_RATE_MIN, min(_TAX_RATE_MAX, rate))

    def _batch_fetch_prices(self, tickers: list[str]) -> None:
        """Populate self._prices from a single yfinance batch download.

        Cuts N sequential HTTPS round-trips (one per ticker) down to one.
        Misses (delisted, weird tickers, partial yfinance returns) fall
        through to the per-ticker path in :meth:`_fetch_price`.
        """
        try:
            import pandas as pd
            import yfinance as yf
        except ImportError:
            logger.warning("yfinance unavailable; prices empty")
            return
        try:
            df = yf.download(tickers, period="5d", progress=False, auto_adjust=False)
        except Exception as exc:
            logger.warning("yfinance batch download failed: %s", exc)
            return
        if df is None or df.empty or "Close" not in df.columns:
            return
        closes = df["Close"].ffill().iloc[-1]
        if isinstance(closes, pd.Series):
            for t, val in closes.items():
                if pd.notna(val):
                    self._prices[str(t).upper()] = float(val)
        # Single-ticker batch returns a scalar.
        elif pd.notna(closes):
            self._prices[tickers[0].upper()] = float(closes)

    def _fetch_price(self, ticker: str, asof: date) -> float | None:
        """Latest close from yfinance.

        Snapshot (most recent close on or before today), not PIT.
        Acceptable for live thematic briefs where asof ≈ today; the
        future paradigm-13 audit replay (which IS PIT-sensitive) is out
        of scope of this store and routes through a separate price loader.
        """
        cached = self._prices.get(ticker.upper())
        if cached is not None:
            return cached
        try:
            import yfinance as yf
        except ImportError:
            logger.warning("yfinance unavailable; price=None for %s", ticker)
            return None
        try:
            info = yf.Ticker(ticker).fast_info
            price = info.last_price
        except Exception as exc:
            logger.warning("yfinance fast_info failed for %s: %s", ticker, exc)
            return None
        if price is None or math.isnan(price):
            return None
        result = float(price)
        self._prices[ticker.upper()] = result
        return result

    def _fetch_shares_yf(self, ticker: str, asof: date) -> float | None:
        """Third-tier shares fallback — yfinance ``get_shares_full`` / ``fast_info.shares``.

        Mirrors the PIT shares pattern in
        :mod:`alphalens_pipeline.thematic.verification.mcap_filter` so live brief
        cohorts (asof ≈ today) and historical replays use the same
        external source. ``get_shares_full`` is the only yfinance API that
        exposes a dated series; ``fast_info.shares`` is a snapshot used
        when the series returns nothing. Results are cached per ticker for
        the lifetime of the store so a single brief doesn't double-fetch.
        """
        key = ticker.upper()
        if key in self._shares_cache:
            return self._shares_cache[key]
        try:
            import pandas as pd
            import yfinance as yf
        except ImportError:
            logger.warning("yfinance unavailable; shares fallback skipped for %s", ticker)
            self._shares_cache[key] = None
            return None
        try:
            tk = yf.Ticker(ticker)
            asof_ts = pd.Timestamp(asof)
            try:
                series = tk.get_shares_full(
                    start=(asof_ts - pd.Timedelta(days=400)).date().isoformat(),
                    end=(asof_ts + pd.Timedelta(days=1)).date().isoformat(),
                )
            except Exception:
                series = None
            if series is not None and len(series) > 0:
                series.index = pd.to_datetime(series.index).tz_localize(None)
                pit = series[series.index <= asof_ts]
                if not pit.empty:
                    val = float(pit.iloc[-1])
                    self._shares_cache[key] = val
                    return val
            fallback = getattr(tk.fast_info, "shares", None)
            val = float(fallback) if fallback else None
        except Exception as exc:
            # Transient failures (rate limit, DNS, malformed response) do
            # NOT pollute the cache — a retry within the same store
            # lifetime should be able to succeed (zen finding #3 on PR
            # #174). Only definitive ``None`` results from a clean call
            # are cached.
            logger.warning("yfinance shares fallback failed for %s: %s", ticker, exc)
            return None
        self._shares_cache[key] = val
        return val

    def _latest_publish_date(self, cik: str, asof: date) -> str | None:
        """ISO date of the most recent visible filing across the core concepts.

        Mirrors SimFin's ``publish_date_str`` semantic — used by
        ``valuation_signal`` to render ``financials_age_days`` in briefs.
        """
        table = self._reader.get_cik_table(cik)
        if table is None:
            return None
        latest: str | None = None
        # Sample a handful of high-cardinality concepts likely to span the
        # filing history; iterating all concepts would be wasteful and the
        # result is the same — they all share the same set of filings.
        for chain in (
            chains.NET_INCOME,
            chains.REVENUE,
            chains.CASH,
            chains.EQUITY,
        ):
            for concept in chain:
                entries = _arrow_table_to_entries(table, concept)
                visible = _pit_filter(entries, asof)
                for e in visible:
                    if latest is None or e.filed > latest:
                        latest = e.filed
        return latest


__all__ = ["EdgarFundamentalsStore"]
