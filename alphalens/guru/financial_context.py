"""Build a compact financial context dict for GuruAgent LLM scoring.

Combines Polygon-backed fundamentals (overview + income/balance/cashflow) with
a 12-month yfinance price summary. Returns a plain dict that serializes
cleanly into a prompt. Polygon is point-in-time correct via
``period_of_report_date.lte=asof`` filter.

Migrated from Alpha Vantage 2026-04-25 — AV free tier (25 req/day) was
unworkable for 30+ ticker pilots. Polygon Stocks Starter ($29/mo) gives
unlimited calls and historical XBRL parsed financials.
"""

from __future__ import annotations

import os

import pandas as pd

from alphalens.guru.polygon_fundamentals import PolygonFundamentalsClient

# Module-level singleton client; tests can monkeypatch by patching
# ``alphalens.guru.financial_context._get_client``.
_CLIENT: PolygonFundamentalsClient | None = None


def _get_client() -> PolygonFundamentalsClient:
    global _CLIENT
    if _CLIENT is None:
        api_key = os.environ.get("POLYGON_API_KEY", "")
        if not api_key:
            raise RuntimeError("POLYGON_API_KEY not set in env (required for guru pipeline)")
        _CLIENT = PolygonFundamentalsClient(api_key=api_key)
    return _CLIENT


def _float_or_none(val) -> float | None:
    if val in (None, "", "None", "-"):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _compute_price_summary(price_series: pd.Series, asof: pd.Timestamp) -> dict:
    trailing = price_series[price_series.index <= asof].tail(252)
    if len(trailing) < 2:
        return {
            "latest_close": None,
            "trailing_12m_return": None,
            "max_drawdown_12m": None,
        }
    latest = float(trailing.iloc[-1])
    first = float(trailing.iloc[0])
    trailing_ret = latest / first - 1.0
    cummax = trailing.cummax()
    drawdown = (trailing - cummax) / cummax
    return {
        "latest_close": latest,
        "trailing_12m_return": float(trailing_ret),
        "max_drawdown_12m": float(drawdown.min()),
    }


def build_context(
    *,
    ticker: str,
    asof: pd.Timestamp,
    price_series: pd.Series,
    client: PolygonFundamentalsClient | None = None,
) -> dict | None:
    """Build per-ticker context dict for LLM scoring.

    Returns ``None`` if Polygon overview is unavailable (ticker delisted /
    not yet listed on asof / data gap). Caller should treat as "skip ticker".
    """
    cli = client or _get_client()
    overview = cli.get_overview(ticker)
    if not overview or not overview.get("Symbol"):
        return None

    financials = cli.fetch_financials(ticker=ticker, asof=asof, timeframe="annual", limit=3)
    latest_fy = financials[0] if financials else {}

    return {
        "ticker": ticker.upper(),
        "asof": asof.strftime("%Y-%m-%d"),
        "name": overview.get("Name"),
        "sector": overview.get("Sector"),
        "industry": overview.get("Industry"),
        "exchange": overview.get("Exchange"),
        "income": {
            "latest_fy_period": latest_fy.get("period_of_report_date"),
            "latest_fy_filing_date": latest_fy.get("filing_date"),
            "latest_fy_revenue": latest_fy.get("revenue"),
            "latest_fy_net_income": latest_fy.get("net_income"),
            "latest_fy_operating_income": latest_fy.get("operating_income"),
        },
        "balance": {
            "latest_fy_period": latest_fy.get("period_of_report_date"),
            "total_assets": latest_fy.get("total_assets"),
            "total_liabilities": latest_fy.get("total_liabilities"),
            "total_equity": latest_fy.get("total_equity"),
            "long_term_debt": latest_fy.get("long_term_debt"),
        },
        "cashflow": {
            "latest_fy_period": latest_fy.get("period_of_report_date"),
            "operating_cashflow": latest_fy.get("operating_cashflow"),
        },
        "history": [
            {
                "period": rec.get("period_of_report_date"),
                "revenue": rec.get("revenue"),
                "net_income": rec.get("net_income"),
            }
            for rec in financials[:3]
        ],
        "price_summary": _compute_price_summary(price_series, asof),
    }


def context_to_prompt(ctx: dict) -> str:
    """Render the context dict as compact key-value text for LLM consumption."""
    if ctx is None:
        return "NO DATA AVAILABLE"

    def _fmt_num(v, fmt="{:,.0f}"):
        return "n/a" if v is None else fmt.format(v)

    income = ctx.get("income", {})
    balance = ctx.get("balance", {})
    cashflow = ctx.get("cashflow", {})
    price = ctx.get("price_summary", {})
    history = ctx.get("history", []) or []

    # Compute simple ratios where possible (LLM does the rest)
    revenue = income.get("latest_fy_revenue")
    net_income = income.get("latest_fy_net_income")
    operating = income.get("latest_fy_operating_income")
    equity = balance.get("total_equity")
    assets = balance.get("total_assets")
    debt = balance.get("long_term_debt")
    op_cf = cashflow.get("operating_cashflow")

    profit_margin = (net_income / revenue) if (revenue and net_income) else None
    operating_margin = (operating / revenue) if (revenue and operating) else None
    roe = (net_income / equity) if (equity and net_income and equity > 0) else None
    debt_to_equity = (debt / equity) if (equity and debt and equity > 0) else None
    cf_to_ni = (op_cf / net_income) if (net_income and op_cf and net_income > 0) else None

    lines = [
        f"COMPANY: {ctx['ticker']} — {ctx.get('name') or 'Unknown'}",
        f"Sector: {ctx.get('sector') or 'Unknown'}   Exchange: {ctx.get('exchange') or 'Unknown'}",
        f"As-of evaluation: {ctx['asof']}",
        "",
        f"Latest annual report (period {income.get('latest_fy_period') or 'n/a'}, "
        f"filed {income.get('latest_fy_filing_date') or 'n/a'}):",
        f"  Revenue: ${_fmt_num(revenue)}",
        f"  Net income: ${_fmt_num(net_income)}",
        f"  Operating income: ${_fmt_num(operating)}",
        f"  Operating cashflow: ${_fmt_num(op_cf)}",
        "",
        "Balance sheet:",
        f"  Total assets: ${_fmt_num(assets)}",
        f"  Total equity: ${_fmt_num(equity)}",
        f"  Long-term debt: ${_fmt_num(debt)}",
        "",
        "Computed ratios:",
        f"  Profit margin: {_fmt_num(profit_margin, '{:.2%}')}",
        f"  Operating margin: {_fmt_num(operating_margin, '{:.2%}')}",
        f"  ROE (basic): {_fmt_num(roe, '{:.2%}')}",
        f"  Long-term D/E: {_fmt_num(debt_to_equity, '{:.2f}')}",
        f"  Operating CF / Net Income: {_fmt_num(cf_to_ni, '{:.2f}')}",
        "",
    ]

    if history:
        lines.append("Revenue / net income trend (last 3 fiscal years):")
        for rec in history:
            lines.append(
                f"  {rec.get('period') or 'n/a'}: "
                f"rev=${_fmt_num(rec.get('revenue'))}, "
                f"ni=${_fmt_num(rec.get('net_income'))}"
            )
        lines.append("")

    lines += [
        f"Price summary (trailing 12m as of {ctx['asof']}):",
        f"  Latest close: ${_fmt_num(price.get('latest_close'), '{:.2f}')}",
        f"  12m return: {_fmt_num(price.get('trailing_12m_return'), '{:.2%}')}",
        f"  Max drawdown: {_fmt_num(price.get('max_drawdown_12m'), '{:.2%}')}",
    ]
    return "\n".join(lines)
