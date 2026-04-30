"""Alpha Vantage fundamentals fetcher + feature extraction.

Calls the four AV endpoints (OVERVIEW, BALANCE_SHEET, CASH_FLOW,
INCOME_STATEMENT) directly via stdlib `urllib.request` and applies a PIT
filter on `fiscalDateEnding` to drop reports ending after `curr_date`.

`extract_features` produces the canonical feature dict consumed by
`alphalens.data.fundamentals.gate`:
  - cash_runway_months: cash / abs(quarterly OCF) x 3
  - ps_ratio: from OVERVIEW.PriceToSalesRatioTTM
  - net_income_ttm: sum of last 4 quarterly netIncome values
  - consecutive_neg_ocf_quarters: count from most recent quarter backwards
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen

logger = logging.getLogger(__name__)

_AV_BASE_URL = "https://www.alphavantage.co/query"


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


def _make_av_request(function_name: str, symbol: str) -> dict:
    """Call the Alpha Vantage REST API and return parsed JSON.

    Returns {} on rate-limit responses, network errors, or non-JSON payloads
    so partial data still flows through to extract_features.
    """
    api_key = os.getenv("ALPHA_VANTAGE_API_KEY")
    if not api_key:
        raise ValueError("ALPHA_VANTAGE_API_KEY environment variable is not set.")

    params = {"function": function_name, "symbol": symbol, "apikey": api_key}
    url = f"{_AV_BASE_URL}?{urlencode(params)}"
    with urlopen(url, timeout=30) as resp:
        body = resp.read().decode("utf-8")

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return {}

    if isinstance(data, dict):
        if "Information" in data:
            info = str(data["Information"]).lower()
            if "rate limit" in info or "api key" in info:
                logger.warning("Alpha Vantage rate-limited on %s/%s", function_name, symbol)
                return {}
        if "Error Message" in data:
            logger.warning(
                "Alpha Vantage error on %s/%s: %s", function_name, symbol, data["Error Message"]
            )
            return {}

    return data if isinstance(data, dict) else {}


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

    Individual endpoint failures return {} so partial data still flows through
    — extract_features defends against missing sections.
    """

    def _safe(fn):
        try:
            return fn(ticker, curr_date=curr_date)
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
