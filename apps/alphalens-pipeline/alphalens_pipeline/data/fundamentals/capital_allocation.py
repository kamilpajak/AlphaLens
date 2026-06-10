"""Per-fiscal-year capital-allocation completeness — buyback proxy (Buffett).

A net buyback is a key Buffett capital-allocation signal: management retiring
its own shares (when the price is below intrinsic value) returns capital to
continuing owners by raising each share's claim on the business. The exact cash
spent on repurchases sits in the financing-activities cash-flow statement, but a
deterministic and PIT-clean proxy is the **year-over-year change in shares
outstanding** over the #501 annual series: a falling share count signals net
buybacks, a rising one signals net issuance / dilution.

This module is a pure transform over the multi-year :class:`AnnualStatement`
series produced by
:func:`alphalens_pipeline.data.fundamentals.annual_aggregator.annual_statements`
(ticket #501). It is **additive and unwired** — nothing in the thematic brief
pipeline consumes it; it exists as a building block for later valuation /
capital-allocation work, mirroring
:mod:`alphalens_pipeline.data.fundamentals.owner_earnings`.

The proxy is approximate: a share count can fall from a reverse split or rise
from an acquisition paid in stock, neither of which is a discretionary capital
return / raise. ``net_buyback`` reports only the SIGN of the share-count change,
not the dollar amount or the management intent — it is a screening signal, not
the audited repurchase figure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from alphalens_pipeline.data.fundamentals.annual_aggregator import AnnualStatement

# A share-count delta is only valid between two CONSECUTIVE fiscal years. The
# #501 series only emits years that carry duration data, so a missing / non-filed
# year (rare: delisting-relisting, a skipped filing) can leave a gap; without
# this guard the next-older entry would be used as the "prior" year and the
# change would silently become a multi-year delta. A normal year-over-year gap
# is ~365 days; the window tolerates 52/53-week calendars and a fiscal-year-end
# shift of a few weeks while rejecting a whole skipped year (~730 days). These
# are local copies of the owner-earnings constants on purpose — a 2-line
# duplication keeps the two pure modules independent (no private cross-import).
_PRIOR_FY_MIN_GAP_DAYS = 300
_PRIOR_FY_MAX_GAP_DAYS = 430


@dataclass(frozen=True)
class CapitalAllocation:
    """One fiscal year's buyback-proxy figures, derived from #501 statements.

    ``shares_change`` is the year-over-year delta in ``shares_outstanding``
    against the immediately older fiscal year (current − prior).
    ``shares_change_pct`` is that delta as a fraction of the prior year's count.
    ``net_buyback`` is ``True`` when ``shares_change_pct < 0`` — a FALL in the
    share count, the buyback signal — and ``False`` on a flat or rising count
    (net issuance / dilution).

    The three change fields are ``None`` (not computable) for the oldest year
    (no prior), for a non-consecutive year (gap-year guard), when either year's
    ``shares_outstanding`` is missing, or when the prior year's count is
    non-positive (no meaningful denominator). The record is still emitted in all
    those cases so the series stays aligned with the input.
    """

    fiscal_year_end: date
    fy: int
    shares_outstanding: float | None
    shares_change: float | None
    shares_change_pct: float | None
    net_buyback: bool | None


def _is_prior_consecutive(prior: AnnualStatement, current: AnnualStatement) -> bool:
    """True when ``prior``'s fiscal-year end is ~1 year before ``current``'s.

    Guards the share-count delta against a gap year in the series (which would
    turn a multi-year change into a mislabeled one-year delta).
    """
    gap_days = (current.fiscal_year_end - prior.fiscal_year_end).days
    return _PRIOR_FY_MIN_GAP_DAYS <= gap_days <= _PRIOR_FY_MAX_GAP_DAYS


def compute_buyback_proxy(statements: list[AnnualStatement]) -> list[CapitalAllocation]:
    """Year-over-year share-count buyback proxy over the #501 annual series.

    ``statements`` is the newest-first series from
    :func:`annual_statements`. Each year is paired with the immediately older
    year (its successor in the list) to form the share-count change. The output
    preserves the newest-first order and has the same length as the input (one
    :class:`CapitalAllocation` per :class:`AnnualStatement`). The oldest year
    has no prior, so its change fields are ``None``.
    """
    out: list[CapitalAllocation] = []
    n = len(statements)
    for i, stmt in enumerate(statements):
        shares = stmt.shares_outstanding
        # The prior fiscal year is the next (older) entry in the newest-first list.
        prior = statements[i + 1] if i + 1 < n else None
        prior_shares = prior.shares_outstanding if prior is not None else None
        # Only difference CONSECUTIVE years — a gap (missing fiscal year) would
        # otherwise yield a multi-year delta mislabeled as one year.
        consecutive = prior is not None and _is_prior_consecutive(prior, stmt)

        if shares is None or prior_shares is None or not consecutive or prior_shares <= 0:
            change: float | None = None
            change_pct: float | None = None
            net_buyback: bool | None = None
        else:
            change = shares - prior_shares
            change_pct = change / prior_shares
            net_buyback = change_pct < 0

        out.append(
            CapitalAllocation(
                fiscal_year_end=stmt.fiscal_year_end,
                fy=stmt.fy,
                shares_outstanding=shares,
                shares_change=change,
                shares_change_pct=change_pct,
                net_buyback=net_buyback,
            )
        )
    return out


__all__ = ["CapitalAllocation", "compute_buyback_proxy"]
