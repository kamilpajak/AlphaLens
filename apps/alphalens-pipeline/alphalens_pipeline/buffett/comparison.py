"""Buffett Mode-A comparison assembler — the brief-delta quantitative lens (#511).

For a thematic brief date this module assembles, per candidate ticker, the
Buffett quantitative metrics the brief does NOT already carry: owner-earnings
yield, a DCF margin of safety, multi-year ROIC and operating-margin trend, the
net-buyback proxy, and dividend yield. The brief already exposes ``roic_pct``,
``fcff_yield_pct``, ``valuation_pe/ps/ev_ebitda`` and ``market_cap``; this lens
only ADDS the delta, so an operator reading both side by side gets the Buffett
view without duplicating what is already in the brief.

Design contract:

- **Pure-ish + fully injectable.** :func:`compute_panel` reads fundamentals from
  the injected ``store`` and reaches the network only through two injected
  callables — ``mcap_fn`` (market cap) and ``dividends_fn`` (dividend history).
  Unit tests pass fakes so no network is touched. Every external call is wrapped
  so a single failure yields ``None`` for that one field and NEVER raises; a
  thematic basket is full of small / recent names whose fundamentals are mostly
  missing, and that patchy coverage is itself informative (the Buffett "too
  hard" pile) — it is surfaced honestly via ``data_coverage`` and never
  fabricated.

- **DCF assumptions are deliberately conservative screening proxies, not a
  precise valuation.** The intrinsic value capitalises the LATEST owner-earnings
  figure with NO growth at a fixed 10% hurdle rate over a 10-year horizon
  (:data:`DEFAULT_GROWTH` / :data:`DEFAULT_TERMINAL_GROWTH` /
  :data:`DEFAULT_HURDLE_RATE`). Owner earnings is a LEVERED proxy (it starts
  from net income, which is post-interest), so adding net cash on top double-
  counts the capital structure for leveraged firms — the #503 levered-proxy
  caveat carries over. These assumptions are exposed as module constants on
  purpose: making them visible is the whole point of the lens. Do NOT treat the
  per-share intrinsic value as a theoretically clean valuation.

Mode B (an independent universe screener) is out of scope — see
``docs/research/buffett_thematic_comparison_2026_06_10.md``.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeVar

import pandas as pd

from alphalens_pipeline.data.fundamentals.annual_aggregator import AnnualStatement
from alphalens_pipeline.data.fundamentals.capital_allocation import CapitalAllocation
from alphalens_pipeline.data.fundamentals.owner_earnings import OwnerEarnings
from alphalens_pipeline.data.fundamentals.valuation_dcf import (
    discount_owner_earnings,
    margin_of_safety,
)
from alphalens_pipeline.paper.brief_loader import load_brief
from alphalens_pipeline.thematic.screening import magic_formula

logger = logging.getLogger(__name__)

# --- fixed DCF / screening assumptions (visible + overridable on purpose) -----

# Owner-earnings growth over the explicit DCF horizon. Zero by design — a
# no-growth capitalisation is the conservative Buffett screening stance (do not
# pay for growth you have to assume). Overridable so a caller can sensitivity-test.
DEFAULT_GROWTH = 0.0

# Terminal (perpetuity) growth in the Gordon tail. Zero, same conservative reason.
DEFAULT_TERMINAL_GROWTH = 0.0

# Discount rate / hurdle. A flat 10% required return — the classic Buffett
# screening hurdle — NOT a per-name WACC. Using one rate across the basket keeps
# the lens a comparison screen, not a precise per-company valuation.
DEFAULT_HURDLE_RATE = 0.10

# Explicit DCF horizon (years) before the Gordon terminal value.
DEFAULT_DCF_YEARS = 10

# How many newest non-None years feed the 3-year ROIC / op-margin averages.
_TREND_AVG_YEARS = 3

# Trailing window (days) over which dividends are summed for the yield.
_DIVIDEND_WINDOW_DAYS = 365

# The 6 Buffett-delta fields that ``data_coverage`` scores. Each contributes
# 1/6 when it resolves to a non-None value; a basket full of small / recent
# names will resolve few of them, and that is the honest signal.
_COVERAGE_FIELDS = (
    "owner_earnings_yield_pct",
    "roic_latest",
    "margin_of_safety_pct",
    "op_margin_latest",
    "net_buyback",
    "dividend_yield_pct",
)

# Default runtime root holding the daily thematic brief parquets.
_DEFAULT_BRIEFS_DIR = Path.home() / ".alphalens" / "thematic_briefs"


class _FundamentalsStore(Protocol):
    """Structural type for the subset of ``EdgarFundamentalsStore`` used here.

    Kept as a Protocol so unit tests inject a lightweight fake exposing only the
    four accessor methods without constructing the real store (which would fetch
    from SEC / yfinance).
    """

    def ev_fcff_features_as_of(self, ticker: str, asof: dt.date) -> dict | None: ...

    def annual_series_as_of(
        self, ticker: str, asof: dt.date, *, max_years: int = 10
    ) -> list[AnnualStatement]: ...

    def owner_earnings_as_of(
        self, ticker: str, asof: dt.date, *, max_years: int = 10
    ) -> list[OwnerEarnings]: ...

    def capital_allocation_as_of(
        self, ticker: str, asof: dt.date, *, max_years: int = 10
    ) -> list[CapitalAllocation]: ...


# Injectable callable types. ``mcap_fn`` mirrors
# ``thematic.verification.mcap_filter.fetch_mcap``; ``dividends_fn`` mirrors
# ``YFinanceClient.dividends``.
McapFn = Callable[..., float | None]
DividendsFn = Callable[..., pd.Series]


@dataclass(frozen=True)
class BuffettPanel:
    """One candidate's Buffett-delta metrics for the comparison table.

    Every quantitative field is ``Optional`` — a missing fundamental yields
    ``None`` (never a fabricated zero). ``data_coverage`` is the fraction of the
    six :data:`_COVERAGE_FIELDS` that resolved (0..1), surfaced so a reader sees
    at a glance how complete the Buffett view is for this name.
    """

    ticker: str
    theme: str
    market_cap: float | None
    owner_earnings_latest: float | None
    owner_earnings_yield_pct: float | None
    roic_latest: float | None
    roic_3y_avg: float | None
    op_margin_latest: float | None
    op_margin_3y_avg: float | None
    intrinsic_value_per_share: float | None
    margin_of_safety_pct: float | None
    buyback_pct: float | None
    net_buyback: bool | None
    dividend_yield_pct: float | None
    data_coverage: float


def _latest_owner_earnings(series: Sequence[OwnerEarnings]) -> float | None:
    """The newest non-None ``owner_earnings`` value in a newest-first series."""
    for entry in series:
        if entry.owner_earnings is not None:
            return entry.owner_earnings
    return None


def _roic_for_year(stmt: AnnualStatement) -> float | None:
    """Per-year ROIC = ``100 * operating_income / (ltd + std + equity - cash)``.

    Mirrors :func:`magic_formula.compute_roic` but over a single FY statement's
    fields instead of the TTM feature dict. ``None`` when any input is missing or
    invested capital is non-positive.
    """
    oi = stmt.operating_income
    ltd = stmt.long_term_debt
    std = stmt.short_term_debt
    equity = stmt.total_equity
    cash = stmt.cash_and_equivalents
    if oi is None or ltd is None or std is None or equity is None or cash is None:
        return None
    invested = ltd + std + equity - cash
    if invested <= 0:
        return None
    return 100.0 * oi / invested


def _op_margin_for_year(stmt: AnnualStatement) -> float | None:
    """Per-year operating margin = ``100 * operating_income / revenue``."""
    oi = stmt.operating_income
    rev = stmt.revenue
    if oi is None or rev is None or rev <= 0:
        return None
    return 100.0 * oi / rev


def _avg_of_newest(values: Sequence[float | None], *, limit: int) -> float | None:
    """Mean of up to ``limit`` newest non-None values, or ``None`` when none.

    ``values`` is newest-first; the first ``limit`` non-None entries are
    averaged. Returns ``None`` if every entry is ``None``.
    """
    available: list[float] = []
    for v in values:
        if v is None:
            continue
        available.append(v)
        if len(available) >= limit:
            break
    if not available:
        return None
    return sum(available) / len(available)


def _dividend_yield_pct(
    dividends: pd.Series, *, price: float | None, asof: dt.date
) -> float | None:
    """``100 * (sum of dividends in the trailing 365d) / price``, or ``None``.

    ``None`` when there is no price or no in-window dividend. The window is
    ``(asof - 365d, asof]`` (exclusive lower bound, inclusive upper). yfinance's
    ``.dividends`` are split-adjusted; pairing them with a split-adjusted live
    price keeps the per-share basis consistent (see ``YFinanceClient.dividends``).
    """
    if price is None or price <= 0 or dividends is None or dividends.empty:
        return None
    asof_ts = pd.Timestamp(asof)
    window_start = asof_ts - pd.Timedelta(days=_DIVIDEND_WINDOW_DAYS)
    in_window = dividends[(dividends.index > window_start) & (dividends.index <= asof_ts)]
    if in_window.empty:
        return None
    total = float(in_window.sum())
    if total <= 0:
        return None
    return 100.0 * total / price


def _intrinsic_value_per_share(
    *,
    owner_earnings_latest: float | None,
    latest_statement: AnnualStatement | None,
) -> float | None:
    """DCF per-share intrinsic value over the latest owner-earnings figure.

    Capitalises ``owner_earnings_latest`` with no growth at the fixed hurdle
    rate, then bridges enterprise value to equity via ``net_cash = cash -
    (long_term_debt + short_term_debt)`` from the latest annual statement and
    divides by shares. ``None`` when owner earnings is missing / non-positive,
    there is no latest statement, or shares is missing / non-positive. Never
    raises (the Gordon guard cannot trip: hurdle 10% > terminal growth 0%).

    Share-basis note: the divisor is the LATEST ANNUAL statement's
    ``shares_outstanding``, but the downstream margin-of-safety compares the
    resulting per-share value against the current snapshot ``price`` (features
    dict). If the share count moved materially since the fiscal-year close
    (large buyback / issuance), the two are on slightly different bases — an
    accepted approximation for a screening lens, not a precise valuation.
    """
    if owner_earnings_latest is None or owner_earnings_latest <= 0:
        return None
    if latest_statement is None:
        return None
    shares = latest_statement.shares_outstanding
    if shares is None or shares <= 0:
        return None
    enterprise_value = discount_owner_earnings(
        owner_earnings_latest,
        growth_rate=DEFAULT_GROWTH,
        terminal_growth=DEFAULT_TERMINAL_GROWTH,
        discount_rate=DEFAULT_HURDLE_RATE,
        years=DEFAULT_DCF_YEARS,
    )
    cash = latest_statement.cash_and_equivalents or 0.0
    ltd = latest_statement.long_term_debt or 0.0
    std = latest_statement.short_term_debt or 0.0
    net_cash = cash - (ltd + std)
    equity_value = enterprise_value + net_cash
    return equity_value / shares


def _margin_of_safety_pct(*, per_share: float | None, price: float | None) -> float | None:
    """``100 * margin_of_safety(per_share, price)`` guarded to never raise.

    ``None`` unless both a positive per-share intrinsic value and a price are
    present. The underlying :func:`margin_of_safety` raises on a non-positive
    intrinsic value, so the positivity guard is what keeps this safe.
    """
    if per_share is None or per_share <= 0 or price is None:
        return None
    try:
        return 100.0 * margin_of_safety(per_share, price)
    except ValueError:
        # Defensive: the per_share > 0 guard already prevents the documented
        # raise, but a future signature change must still fail soft here.
        return None


def compute_panel(
    ticker: str,
    theme: str,
    asof: dt.date,
    *,
    store: _FundamentalsStore,
    mcap_fn: McapFn,
    dividends_fn: DividendsFn,
) -> BuffettPanel:
    """Assemble the Buffett-delta :class:`BuffettPanel` for one candidate.

    Reads fundamentals from ``store`` and the two injected network callables.
    Every external lookup is wrapped so a single failure becomes ``None`` for
    that field and never aborts the panel — the patchy-coverage path is the
    common case for small thematic names and is reported via ``data_coverage``.
    """
    market_cap = _safe(lambda: mcap_fn(ticker, asof=asof), what=f"mcap({ticker})")
    features = _safe(
        lambda: store.ev_fcff_features_as_of(ticker, asof),
        what=f"features({ticker})",
    )
    annual = (
        _safe(
            lambda: store.annual_series_as_of(ticker, asof),
            what=f"annual({ticker})",
        )
        or []
    )
    owner_series = (
        _safe(
            lambda: store.owner_earnings_as_of(ticker, asof),
            what=f"owner_earnings({ticker})",
        )
        or []
    )
    capital_series = (
        _safe(
            lambda: store.capital_allocation_as_of(ticker, asof),
            what=f"capital_allocation({ticker})",
        )
        or []
    )

    price = features.get("price") if isinstance(features, dict) else None
    latest_statement = annual[0] if annual else None

    owner_earnings_latest = _latest_owner_earnings(owner_series)
    owner_earnings_yield_pct = (
        100.0 * owner_earnings_latest / market_cap
        if owner_earnings_latest is not None
        and owner_earnings_latest > 0
        and market_cap is not None
        and market_cap > 0
        else None
    )

    roic_latest = magic_formula.compute_roic(features) if isinstance(features, dict) else None
    roic_by_year = [_roic_for_year(s) for s in annual]
    roic_3y_avg = _avg_of_newest(roic_by_year, limit=_TREND_AVG_YEARS)

    op_margin_by_year = [_op_margin_for_year(s) for s in annual]
    op_margin_latest = op_margin_by_year[0] if op_margin_by_year else None
    op_margin_3y_avg = _avg_of_newest(op_margin_by_year, limit=_TREND_AVG_YEARS)

    intrinsic_value_per_share = _intrinsic_value_per_share(
        owner_earnings_latest=owner_earnings_latest,
        latest_statement=latest_statement,
    )
    margin_of_safety_pct = _margin_of_safety_pct(per_share=intrinsic_value_per_share, price=price)

    latest_capital = capital_series[0] if capital_series else None
    net_buyback = latest_capital.net_buyback if latest_capital is not None else None
    buyback_pct = (
        latest_capital.shares_change_pct * 100.0
        if latest_capital is not None and latest_capital.shares_change_pct is not None
        else None
    )

    dividends = _safe(lambda: dividends_fn(ticker, asof=asof), what=f"dividends({ticker})")
    dividend_yield_pct = (
        _dividend_yield_pct(dividends, price=price, asof=asof) if dividends is not None else None
    )

    resolved = {
        "owner_earnings_yield_pct": owner_earnings_yield_pct,
        "roic_latest": roic_latest,
        "margin_of_safety_pct": margin_of_safety_pct,
        "op_margin_latest": op_margin_latest,
        "net_buyback": net_buyback,
        "dividend_yield_pct": dividend_yield_pct,
    }
    data_coverage = sum(1 for field in _COVERAGE_FIELDS if resolved[field] is not None) / len(
        _COVERAGE_FIELDS
    )

    return BuffettPanel(
        ticker=ticker,
        theme=theme,
        market_cap=market_cap,
        owner_earnings_latest=owner_earnings_latest,
        owner_earnings_yield_pct=owner_earnings_yield_pct,
        roic_latest=roic_latest,
        roic_3y_avg=roic_3y_avg,
        op_margin_latest=op_margin_latest,
        op_margin_3y_avg=op_margin_3y_avg,
        intrinsic_value_per_share=intrinsic_value_per_share,
        margin_of_safety_pct=margin_of_safety_pct,
        buyback_pct=buyback_pct,
        net_buyback=net_buyback,
        dividend_yield_pct=dividend_yield_pct,
        data_coverage=data_coverage,
    )


def build_comparison(
    brief_date: dt.date,
    *,
    briefs_dir: Path | None = None,
    store: _FundamentalsStore,
    mcap_fn: McapFn,
    dividends_fn: DividendsFn,
) -> list[BuffettPanel]:
    """Load the brief for ``brief_date`` and compute one panel per candidate.

    Preserves the brief's candidate order. ``store`` / ``mcap_fn`` /
    ``dividends_fn`` are injected so the assembler stays testable without the
    network; the CLI wires the real ``EdgarFundamentalsStore`` + ``fetch_mcap``
    + the yfinance client's ``dividends``.
    """
    resolved_dir = briefs_dir if briefs_dir is not None else _DEFAULT_BRIEFS_DIR
    candidates = load_brief(brief_date, resolved_dir)
    return [
        compute_panel(
            candidate.ticker,
            candidate.theme,
            brief_date,
            store=store,
            mcap_fn=mcap_fn,
            dividends_fn=dividends_fn,
        )
        for candidate in candidates
    ]


_T = TypeVar("_T")


def _safe(fn: Callable[[], _T], *, what: str) -> _T | None:
    """Call ``fn`` returning its result, or ``None`` (logged) on any exception.

    Keeps a single bad ticker / vendor hiccup from aborting the whole panel —
    the Buffett lens prefers an honest ``None`` (counted against
    ``data_coverage``) over a crash. Generic in the callable's return type so a
    static checker keeps the per-field types through the wrapper.
    """
    try:
        return fn()
    except Exception as exc:
        logger.warning("buffett lens: %s failed: %s", what, exc)
        return None


__all__ = [
    "DEFAULT_DCF_YEARS",
    "DEFAULT_GROWTH",
    "DEFAULT_HURDLE_RATE",
    "DEFAULT_TERMINAL_GROWTH",
    "BuffettPanel",
    "build_comparison",
    "compute_panel",
]
