"""Pure DCF intrinsic-value modeling over the annual-statement series (Buffett PR-3, #503).

This is the **modeling** layer that assembles a Buffett-style discounted
cash-flow valuation from inputs that already exist on disk: the multi-year
:class:`~alphalens_pipeline.data.fundamentals.annual_aggregator.AnnualStatement`
series (companyfacts XBRL, PIT), a risk-free rate (FRED ``DGS10``), and a
current market value supplied by the caller. It is **not** a data layer — the
discount rate, equity-risk-premium, growth, and terminal-growth are modeling
assumptions, not fetched quantities, and every fetched number still comes from
an authoritative source (companyfacts / FRED / yfinance market cap), never an
LLM.

Design contract:

- **No I/O in the DCF math.** :func:`wacc`, :func:`discount_owner_earnings`,
  :func:`intrinsic_value_per_share`, :func:`margin_of_safety`, and
  :func:`intrinsic_value_from_statements` are pure functions over numbers /
  dataclasses. The only network-touching helper is :func:`risk_free_from_fred`,
  and it takes the FRED client by dependency injection so tests mock it.
- **Market value is passed in, never fetched here.** Historical (PIT) prices
  are a known limitation; the live caller supplies the current market cap from
  the yfinance snapshot via the store. The DCF module deliberately knows
  nothing about price retrieval.
- **Value convention.** :func:`discount_owner_earnings` returns the
  **enterprise value** (PV of the unlevered free-cash-flow proxy). Equity value
  is then ``enterprise_value + net_cash`` where ``net_cash = cash − total_debt``
  (so a net-debt company subtracts), and per-share value divides equity value by
  shares outstanding. ``margin_of_safety`` compares equity-level market value to
  equity-level intrinsic value.
- **base_fcf proxy.** :func:`intrinsic_value_from_statements` derives
  ``base_fcf`` from the latest fiscal year's ``ocf − capex`` (a free-cash-flow
  proxy). Once PR-2 (#502) lands its ``owner_earnings`` series, that figure can
  be substituted as ``base_fcf`` without changing this module's shape.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Protocol

import pandas as pd

from alphalens_pipeline.data.fundamentals.annual_aggregator import AnnualStatement

# Default equity-risk-premium assumption (long-run US equity premium over the
# risk-free rate). A modeling constant, not a fetched value.
DEFAULT_EQUITY_RISK_PREMIUM = 0.05

# FRED series for the 10-year US Treasury constant-maturity rate, reported in
# percent (e.g. 4.20 == 4.2%). Used as the risk-free proxy.
RISK_FREE_FRED_SERIES = "DGS10"


class FredSeriesClient(Protocol):
    """Structural type for any FRED client exposing ``fetch_series``.

    Matches :class:`alphalens_pipeline.data.macro.fred_client.FREDClient`. Kept
    as a Protocol so the helper can be injected with a mock in tests without a
    network call.
    """

    def fetch_series(self, series_id: str) -> pd.Series: ...


@dataclass(frozen=True)
class IntrinsicValue:
    """Result of a statements-driven DCF.

    ``enterprise_value`` is the PV of the projected free-cash-flow proxy.
    ``equity_value`` adds net cash (cash − total debt). ``base_fcf`` is the
    latest year's ``ocf − capex`` that fed the projection.
    ``intrinsic_value_per_share`` is ``None`` when shares outstanding is missing
    or non-positive.
    """

    enterprise_value: float
    equity_value: float
    base_fcf: float
    intrinsic_value_per_share: float | None


def wacc(
    *,
    risk_free: float,
    beta: float,
    equity_value: float,
    debt_value: float,
    cost_of_debt: float,
    tax_rate: float,
    equity_risk_premium: float = DEFAULT_EQUITY_RISK_PREMIUM,
) -> float:
    """Weighted-average cost of capital.

    Cost of equity via CAPM: ``risk_free + beta * equity_risk_premium``. Debt is
    after tax: ``cost_of_debt * (1 - tax_rate)``. Weights are the value shares of
    equity and debt in total capital (``equity_value + debt_value``).

    Raises ``ValueError`` when total capital is non-positive (no meaningful
    weights).
    """
    total_capital = equity_value + debt_value
    if total_capital <= 0:
        raise ValueError("total capital (equity_value + debt_value) must be positive")
    cost_of_equity = risk_free + beta * equity_risk_premium
    after_tax_cost_of_debt = cost_of_debt * (1 - tax_rate)
    weight_equity = equity_value / total_capital
    weight_debt = debt_value / total_capital
    return weight_equity * cost_of_equity + weight_debt * after_tax_cost_of_debt


def discount_owner_earnings(
    base_fcf: float,
    *,
    growth_rate: float,
    terminal_growth: float,
    discount_rate: float,
    years: int = 10,
) -> float:
    """Present value of a growing cash flow over an explicit horizon plus a
    Gordon-growth terminal value, discounted at ``discount_rate`` (the WACC).

    The explicit period grows ``base_fcf`` at ``growth_rate`` for ``years``
    periods; year ``t`` cash flow is ``base_fcf * (1 + growth_rate) ** t``,
    discounted by ``(1 + discount_rate) ** t``. The terminal value at the end of
    the explicit period is ``CF_years * (1 + terminal_growth) /
    (discount_rate - terminal_growth)`` (Gordon growth), discounted back by
    ``(1 + discount_rate) ** years``.

    Returns the **enterprise value** (the discounted value of the cash-flow
    proxy itself); net cash is layered on by the caller.

    Guard: a Gordon terminal value diverges unless the discount rate strictly
    exceeds the terminal growth rate. ``discount_rate <= terminal_growth``
    raises ``ValueError`` (documented behaviour — we raise rather than clamp so
    a mis-specified assumption surfaces loudly).
    """
    if years <= 0:
        raise ValueError("years must be positive")
    if discount_rate <= terminal_growth:
        raise ValueError("discount_rate must be strictly greater than terminal_growth")

    pv_explicit = 0.0
    cash_flow = base_fcf
    for t in range(1, years + 1):
        cash_flow = base_fcf * (1 + growth_rate) ** t
        pv_explicit += cash_flow / (1 + discount_rate) ** t

    terminal_cash_flow = cash_flow * (1 + terminal_growth)
    terminal_value = terminal_cash_flow / (discount_rate - terminal_growth)
    pv_terminal = terminal_value / (1 + discount_rate) ** years
    return pv_explicit + pv_terminal


def intrinsic_value_per_share(
    equity_value: float, shares_outstanding: float | None
) -> float | None:
    """Equity value divided by shares outstanding.

    Returns ``None`` when shares is missing or non-positive (cannot divide).
    """
    if shares_outstanding is None or shares_outstanding <= 0:
        return None
    return equity_value / shares_outstanding


def margin_of_safety(intrinsic_value: float, market_value: float) -> float:
    """Margin of safety = ``1 - market_value / intrinsic_value``.

    Sign convention: **positive** means the asset trades below intrinsic value
    (a discount, the Buffett margin of safety); **negative** means it trades
    above intrinsic value (overvalued). Compare like-for-like — both arguments
    at the equity level (equity intrinsic value vs market cap) or both per share.

    Raises ``ValueError`` when ``intrinsic_value`` is non-positive (the ratio is
    meaningless for a zero or negative intrinsic value).
    """
    if intrinsic_value <= 0:
        raise ValueError("intrinsic_value must be positive to compute margin of safety")
    return 1 - market_value / intrinsic_value


def intrinsic_value_from_statements(
    statements: Sequence[AnnualStatement],
    *,
    growth_rate: float,
    terminal_growth: float,
    discount_rate: float,
    years: int = 10,
) -> IntrinsicValue | None:
    """Assemble a DCF from the latest fiscal year of an annual-statement series.

    The series is expected newest-first (as returned by
    :func:`~alphalens_pipeline.data.fundamentals.annual_aggregator.annual_statements`);
    the first element is treated as the latest fiscal year.

    Steps:

    1. ``base_fcf = ocf - capex`` from the latest year (a free-cash-flow proxy;
       PR-2's ``owner_earnings`` can be substituted later).
    2. ``enterprise_value = discount_owner_earnings(base_fcf, ...)``.
    3. ``net_cash = cash_and_equivalents - (long_term_debt + short_term_debt)``
       from the latest year. Missing balance-sheet items are treated as zero
       (so a company reporting only cash, no debt, gets ``net_cash = cash``).
    4. ``equity_value = enterprise_value + net_cash``.
    5. per-share value when shares outstanding is present and positive.

    Fail-soft → ``None`` when the series is empty or the latest year is missing
    a required cash-flow input (``ocf`` or ``capex``); the caller treats ``None``
    as "DCF not computable for this ticker at this asof".
    """
    if not statements:
        return None
    latest = statements[0]
    if latest.ocf is None or latest.capex is None:
        return None

    base_fcf = latest.ocf - latest.capex
    enterprise_value = discount_owner_earnings(
        base_fcf,
        growth_rate=growth_rate,
        terminal_growth=terminal_growth,
        discount_rate=discount_rate,
        years=years,
    )

    cash = latest.cash_and_equivalents or 0.0
    long_term_debt = latest.long_term_debt or 0.0
    short_term_debt = latest.short_term_debt or 0.0
    net_cash = cash - (long_term_debt + short_term_debt)
    equity_value = enterprise_value + net_cash

    return IntrinsicValue(
        enterprise_value=enterprise_value,
        equity_value=equity_value,
        base_fcf=base_fcf,
        intrinsic_value_per_share=intrinsic_value_per_share(
            equity_value, latest.shares_outstanding
        ),
    )


def risk_free_from_fred(fred_client: FredSeriesClient, asof: date) -> float | None:
    """Risk-free rate (decimal) from FRED ``DGS10`` as of ``asof``.

    Fetches the 10-year Treasury constant-maturity series (reported in percent)
    via the injected client and returns the latest observation at or before
    ``asof``, converted to a decimal (4.20 → 0.042). Returns ``None`` when the
    client yields nothing or there is no observation at or before ``asof``.

    The FRED client is injected so unit tests pass a mock — this is the only
    function in the module that can touch the network.
    """
    try:
        series = fred_client.fetch_series(RISK_FREE_FRED_SERIES)
    except Exception:
        return None
    if series is None or series.empty:
        return None
    asof_ts = pd.Timestamp(asof)
    history = series.loc[series.index <= asof_ts]
    if history.empty:
        return None
    return float(history.iloc[-1]) / 100.0


__all__ = [
    "DEFAULT_EQUITY_RISK_PREMIUM",
    "RISK_FREE_FRED_SERIES",
    "FredSeriesClient",
    "IntrinsicValue",
    "discount_owner_earnings",
    "intrinsic_value_from_statements",
    "intrinsic_value_per_share",
    "margin_of_safety",
    "risk_free_from_fred",
    "wacc",
]
