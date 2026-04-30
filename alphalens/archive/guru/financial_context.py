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

from alphalens.archive.guru.polygon_fundamentals import PolygonFundamentalsClient

# Module-level singleton client; tests can monkeypatch by patching
# ``alphalens.archive.guru.financial_context._get_client``.
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


def _fmt_num(v, fmt: str = "{:,.0f}") -> str:
    return "n/a" if v is None else fmt.format(v)


def _safe_ratio(num, denom, *, denom_positive: bool = False) -> float | None:
    """Return num/denom when both are truthy; ``denom_positive=True`` also requires denom > 0."""
    if not (num and denom):
        return None
    if denom_positive and denom <= 0:
        return None
    return num / denom


def _compute_ratios(income: dict, balance: dict, cashflow: dict) -> dict[str, float | None]:
    revenue = income.get("latest_fy_revenue")
    net_income = income.get("latest_fy_net_income")
    operating = income.get("latest_fy_operating_income")
    equity = balance.get("total_equity")
    debt = balance.get("long_term_debt")
    op_cf = cashflow.get("operating_cashflow")
    return {
        "profit_margin": _safe_ratio(net_income, revenue),
        "operating_margin": _safe_ratio(operating, revenue),
        "roe": _safe_ratio(net_income, equity, denom_positive=True),
        "debt_to_equity": _safe_ratio(debt, equity, denom_positive=True),
        "cf_to_ni": _safe_ratio(op_cf, net_income, denom_positive=True),
    }


def _format_history_lines(history: list[dict]) -> list[str]:
    if not history:
        return []
    lines = ["Revenue / net income trend (last 3 fiscal years):"]
    for rec in history:
        lines.append(
            f"  {rec.get('period') or 'n/a'}: "
            f"rev=${_fmt_num(rec.get('revenue'))}, "
            f"ni=${_fmt_num(rec.get('net_income'))}"
        )
    lines.append("")
    return lines


def context_to_prompt(ctx: dict) -> str:
    """Render the context dict as compact key-value text for LLM consumption."""
    if ctx is None:
        return "NO DATA AVAILABLE"

    income = ctx.get("income", {})
    balance = ctx.get("balance", {})
    cashflow = ctx.get("cashflow", {})
    price = ctx.get("price_summary", {})
    history = ctx.get("history", []) or []
    ratios = _compute_ratios(income, balance, cashflow)

    lines = [
        f"COMPANY: {ctx['ticker']} — {ctx.get('name') or 'Unknown'}",
        f"Sector: {ctx.get('sector') or 'Unknown'}   Exchange: {ctx.get('exchange') or 'Unknown'}",
        f"As-of evaluation: {ctx['asof']}",
        "",
        f"Latest annual report (period {income.get('latest_fy_period') or 'n/a'}, "
        f"filed {income.get('latest_fy_filing_date') or 'n/a'}):",
        f"  Revenue: ${_fmt_num(income.get('latest_fy_revenue'))}",
        f"  Net income: ${_fmt_num(income.get('latest_fy_net_income'))}",
        f"  Operating income: ${_fmt_num(income.get('latest_fy_operating_income'))}",
        f"  Operating cashflow: ${_fmt_num(cashflow.get('operating_cashflow'))}",
        "",
        "Balance sheet:",
        f"  Total assets: ${_fmt_num(balance.get('total_assets'))}",
        f"  Total equity: ${_fmt_num(balance.get('total_equity'))}",
        f"  Long-term debt: ${_fmt_num(balance.get('long_term_debt'))}",
        "",
        "Computed ratios:",
        f"  Profit margin: {_fmt_num(ratios['profit_margin'], '{:.2%}')}",
        f"  Operating margin: {_fmt_num(ratios['operating_margin'], '{:.2%}')}",
        f"  ROE (basic): {_fmt_num(ratios['roe'], '{:.2%}')}",
        f"  Long-term D/E: {_fmt_num(ratios['debt_to_equity'], '{:.2f}')}",
        f"  Operating CF / Net Income: {_fmt_num(ratios['cf_to_ni'], '{:.2f}')}",
        "",
    ]
    lines += _format_history_lines(history)
    lines += [
        f"Price summary (trailing 12m as of {ctx['asof']}):",
        f"  Latest close: ${_fmt_num(price.get('latest_close'), '{:.2f}')}",
        f"  12m return: {_fmt_num(price.get('trailing_12m_return'), '{:.2%}')}",
        f"  Max drawdown: {_fmt_num(price.get('max_drawdown_12m'), '{:.2%}')}",
    ]
    return "\n".join(lines)
