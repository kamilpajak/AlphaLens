"""Polygon-backed fundamentals client for GuruAgent pilot.

Replaces Alpha Vantage (free tier 25/day cap unworkable for 30+ ticker pilots).
Polygon Stocks Starter ($29/mo) gives unlimited calls + 5y history + structured
XBRL financials with ``period_of_report_date`` filter for clean PIT semantics.

Endpoints used:
- /vX/reference/financials  — annual/quarterly income/balance/cashflow
- /v3/reference/tickers/{T} — overview (sector, name, exchange)

Output schema matches what build_context expects, so financial_context.py needs
only the wrapper functions ``get_overview/get_income_statement/...`` swap.
"""

from __future__ import annotations

import time
from typing import Any

import pandas as pd
import requests

_BASE_URL = "https://api.polygon.io"


class PolygonFundamentalsError(RuntimeError):
    pass


class PolygonFundamentalsClient:
    """Thin wrapper for Polygon /vX/reference/financials and /v3/reference/tickers.

    Returns flattened dicts with normalized keys so callers don't have to dig
    through the nested ``financials.income_statement.revenues.value`` structure.
    """

    def __init__(
        self,
        *,
        api_key: str,
        session: requests.Session | None = None,
        sleep=time.sleep,
        rate_limit_per_sec: float = 5.0,
    ):
        if not api_key:
            raise ValueError("api_key required")
        self._api_key = api_key
        self._session = session or requests.Session()
        self._sleep = sleep
        self._min_interval_s = 1.0 / max(rate_limit_per_sec, 1.0)
        self._last_call_ts = 0.0

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_call_ts
        if elapsed < self._min_interval_s:
            self._sleep(self._min_interval_s - elapsed)
        self._last_call_ts = time.time()

    def _get(self, path: str, params: dict[str, Any]) -> dict:
        self._throttle()
        params = {**params, "apiKey": self._api_key}
        resp = self._session.get(f"{_BASE_URL}{path}", params=params, timeout=30)
        resp.raise_for_status()
        return resp.json() or {}

    def fetch_financials(
        self,
        *,
        ticker: str,
        asof: pd.Timestamp,
        timeframe: str = "annual",
        limit: int = 3,
    ) -> list[dict]:
        """Annual/quarterly financials for a ticker, filtered to reports
        whose period_of_report_date <= asof. Returns most recent first.

        Each record is a flat dict with: fiscal_period, period_of_report_date,
        filing_date, revenue, net_income, operating_income, total_assets,
        total_liabilities, total_equity, long_term_debt, operating_cashflow.
        """
        params = {
            "ticker": ticker.upper(),
            "timeframe": timeframe,
            "limit": limit,
            "order": "desc",
            "sort": "period_of_report_date",
            "period_of_report_date.lte": asof.strftime("%Y-%m-%d"),
        }
        payload = self._get("/vX/reference/financials", params)
        results = payload.get("results") or []
        return [_flatten_financial_record(r) for r in results]

    def get_overview(self, ticker: str) -> dict:
        """Company metadata via /v3/reference/tickers/{T}.

        Returns dict shaped like Alpha Vantage OVERVIEW so callers can swap
        with minimal changes (Symbol/Name/Industry/Sector keys).
        """
        payload = self._get(f"/v3/reference/tickers/{ticker.upper()}", {})
        results = payload.get("results") or {}
        if not results:
            return {}
        return {
            "Symbol": results.get("ticker", ticker.upper()),
            "Name": results.get("name", ""),
            "Sector": results.get("sic_description", "")[:60],
            "Industry": results.get("sic_description", ""),
            "Exchange": results.get("primary_exchange", ""),
            "Currency": results.get("currency_name", "USD").upper(),
        }


def _f(node: Any) -> float | None:
    if not isinstance(node, dict):
        return None
    val = node.get("value")
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _flatten_financial_record(record: dict) -> dict:
    fin = record.get("financials") or {}
    income = fin.get("income_statement") or {}
    balance = fin.get("balance_sheet") or {}
    cashflow = fin.get("cash_flow_statement") or {}

    return {
        "fiscal_period": record.get("fiscal_period"),
        "period_of_report_date": record.get("period_of_report_date"),
        "filing_date": record.get("filing_date"),
        # Income
        "revenue": _f(income.get("revenues")),
        "net_income": _f(income.get("net_income_loss")),
        "operating_income": _f(income.get("operating_income_loss")),
        # Balance
        "total_assets": _f(balance.get("assets")),
        "total_liabilities": _f(balance.get("liabilities")),
        "total_equity": _f(balance.get("equity")),
        "long_term_debt": _f(balance.get("long_term_debt")),
        # Cashflow
        "operating_cashflow": _f(cashflow.get("net_cash_flow_from_operating_activities")),
    }
