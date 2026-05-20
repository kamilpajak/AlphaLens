"""Alpha Vantage fundamentals fetcher + feature extraction.

Calls the four AV endpoints (OVERVIEW, BALANCE_SHEET, CASH_FLOW,
INCOME_STATEMENT) via the canonical
:class:`alphalens.data.alt_data.alphavantage_client.AlphaVantageClient`
and applies a PIT filter on ``fiscalDateEnding`` to drop reports ending
after ``curr_date``.

``extract_features`` produces the canonical feature dict consumed by
``alphalens.data.fundamentals.gate``:
  - cash_runway_months: cash / abs(quarterly OCF) x 3
  - ps_ratio: from OVERVIEW.PriceToSalesRatioTTM
  - net_income_ttm: sum of last 4 quarterly netIncome values
  - consecutive_neg_ocf_quarters: count from most recent quarter backwards
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any
from urllib.error import URLError

from alphalens.data.alt_data.alphavantage_client import (
    AlphaVantageClient,
    AVRateLimitError,
    AVSchemaError,
    get_default_av_client,
)

logger = logging.getLogger(__name__)

# Back-compat re-export: external callers used to import this from here.
# Today there are none in the repo, but keeping the alias makes
# `from alphalens.data.fundamentals.fetcher import AlphaVantageRateLimitError`
# stay equivalent to the canonical name should an out-of-tree script depend
# on it.
AlphaVantageRateLimitError = AVRateLimitError


def _filter_reports_by_date(result: Any, curr_date: str | None) -> Any:
    """Drop annualReports/quarterlyReports entries with fiscalDateEnding > curr_date.

    Prevents look-ahead bias by removing fiscal periods that end after the
    simulation's current date. Returns the input unchanged if no curr_date
    or the result is not a dict.
    """
    if not curr_date or not isinstance(result, dict):
        return result
    for key in ("annualReports", "quarterlyReports"):
        if key in result:
            result[key] = [r for r in result[key] if r.get("fiscalDateEnding", "") <= curr_date]
    return result


def _make_av_request(
    function_name: str,
    symbol: str,
    *,
    client: AlphaVantageClient | None = None,
) -> dict:
    """Call Alpha Vantage via the canonical client and return parsed JSON.

    Returns ``{}`` on AV-reported application errors (invalid ticker, malformed
    payload) so partial bundles still flow through ``fetch_ticker_bundle``.
    Raises :class:`AVRateLimitError` on quota / api-key / premium signals so
    the caller can abort a batch instead of polluting it with null features.
    Network errors propagate as ``urllib.error.URLError`` / ``HTTPError``.

    ``client`` is optional — when omitted, the process-wide default client
    (lazy-initialized from ``ALPHA_VANTAGE_API_KEY``) is used.
    """
    av = client or get_default_av_client()
    try:
        return av.query(function_name, symbol=symbol)
    except AVSchemaError as exc:
        # Soft-fail: extract_features defends against missing sections, and
        # an invalid-ticker / malformed payload should not abort the whole
        # bundle. Rate-limit / network errors still propagate.
        logger.warning("Alpha Vantage error on %s/%s: %s", function_name, symbol, exc)
        return {}


def _av_overview(ticker: str, curr_date: str | None = None) -> Mapping:
    return _make_av_request("OVERVIEW", ticker)


def _av_balance_sheet(ticker: str, curr_date: str | None = None) -> Mapping:
    return _filter_reports_by_date(_make_av_request("BALANCE_SHEET", ticker), curr_date)


def _av_cashflow(ticker: str, curr_date: str | None = None) -> Mapping:
    return _filter_reports_by_date(_make_av_request("CASH_FLOW", ticker), curr_date)


def _av_income_statement(ticker: str, curr_date: str | None = None) -> Mapping:
    return _filter_reports_by_date(_make_av_request("INCOME_STATEMENT", ticker), curr_date)


def fetch_ticker_bundle(ticker: str, curr_date: str | None = None) -> dict:
    """Fetch all four AV endpoints for a ticker and bundle them.

    Individual application-level endpoint failures return {} so partial data
    still flows through — extract_features defends against missing sections.
    Rate-limit signals (AlphaVantageRateLimitError) propagate to the caller
    so a batch can be aborted instead of polluted with null features.
    """

    def _safe(fn):
        try:
            return fn(ticker, curr_date=curr_date)
        except AlphaVantageRateLimitError:
            raise
        except URLError as exc:
            logger.warning("AV network error for %s: %s", ticker, exc)
            return {}
        except Exception as exc:
            logger.warning("AV fetch failed for %s: %s", ticker, exc)
            return {}

    return {
        "overview": _safe(_av_overview),
        "balance_sheet": _safe(_av_balance_sheet),
        "cash_flow": _safe(_av_cashflow),
        "income_statement": _safe(_av_income_statement),
    }


def _to_float(x: Any) -> float | None:
    """AV returns strings; 'None' / '' / missing -> None. Returns float or None."""
    if x is None:
        return None
    if isinstance(x, str):
        s = x.strip()
        if not s or s.lower() == "none":
            return None
        try:
            return float(s)
        except ValueError:
            return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _consecutive_neg_ocf(cash_flow: Mapping) -> int:
    """Count consecutive negative operatingCashflow quarters from the most
    recent report backwards. Streak breaks on first non-negative value."""
    reports = cash_flow.get("quarterlyReports") or []
    streak = 0
    for r in reports:  # AV returns most-recent first
        ocf = _to_float(r.get("operatingCashflow"))
        if ocf is None or ocf >= 0:
            break
        streak += 1
    return streak


def _cash_runway_months(balance_sheet: Mapping, cash_flow: Mapping) -> float | None:
    """Monthly runway = cash_and_short_term_investments / |TTM avg quarterly OCF| x 3.

    Uses trailing-4-quarter average OCF to smooth one-time items (legal
    settlements, inventory builds, tax windfalls) that otherwise risk
    false-positive hard rejects. Returns None if data is insufficient or
    the company is cash-flow positive over the trailing window.
    """
    bs_reports = balance_sheet.get("quarterlyReports") or []
    cf_reports = cash_flow.get("quarterlyReports") or []
    if not bs_reports or not cf_reports:
        return None

    cash = _to_float(bs_reports[0].get("cashAndShortTermInvestments"))
    if cash is None or cash <= 0:
        return None

    ocf_values = [_to_float(r.get("operatingCashflow")) for r in cf_reports[:4]]
    ocf_values = [v for v in ocf_values if v is not None]
    if not ocf_values:
        return None

    avg_ocf = sum(ocf_values) / len(ocf_values)
    if avg_ocf >= 0:
        return None  # Cash-flow positive over TTM — no runway concern

    burn_per_quarter = -avg_ocf
    quarters = cash / burn_per_quarter
    return quarters * 3.0


def _net_income_ttm(overview: Mapping, income_statement: Mapping) -> float | None:
    """Prefer OVERVIEW.NetIncomeTTM (authoritative). Fall back to sum of last 4
    quarterly netIncome if missing."""
    ni_ttm = _to_float(overview.get("NetIncomeTTM"))
    if ni_ttm is not None:
        return ni_ttm

    reports = income_statement.get("quarterlyReports") or []
    values = [_to_float(r.get("netIncome")) for r in reports[:4]]
    values = [v for v in values if v is not None]
    if len(values) < 4:
        return None
    return sum(values)


def extract_features(bundle: Mapping) -> dict:
    """Normalize an AV bundle to the canonical feature dict consumed by gate.py."""
    overview = bundle.get("overview") or {}
    balance_sheet = bundle.get("balance_sheet") or {}
    cash_flow = bundle.get("cash_flow") or {}
    income_statement = bundle.get("income_statement") or {}

    return {
        "cash_runway_months": _cash_runway_months(balance_sheet, cash_flow),
        "ps_ratio": _to_float(overview.get("PriceToSalesRatioTTM")),
        "net_income_ttm": _net_income_ttm(overview, income_statement),
        "consecutive_neg_ocf_quarters": _consecutive_neg_ocf(cash_flow),
    }
