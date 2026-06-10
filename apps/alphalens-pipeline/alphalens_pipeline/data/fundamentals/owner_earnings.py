"""Per-fiscal-year owner earnings + working-capital deltas (Buffett).

Owner earnings (Buffett's 1986 shareholder-letter definition) approximate the
cash an owner can extract from a business in a year without impairing its
competitive position:

    owner_earnings = net_income + D&A - maintenance_capex - ΔWC

This module is a pure transform over the multi-year :class:`AnnualStatement`
series produced by
:func:`alphalens_pipeline.data.fundamentals.annual_aggregator.annual_statements`
(ticket #501). It is **additive and unwired** — nothing in the thematic brief
pipeline consumes it; it exists as a building block for later valuation work.

Two deliberate approximations are documented in code so a reviewer can see the
limits:

1. **maintenance_capex ≈ min(capex, D&A).** XBRL exposes only total PP&E cash
   payments (``PaymentsToAcquirePropertyPlantAndEquipment``); it does NOT split
   maintenance from growth capex. Buffett's "maintenance capex" is a judgement
   call with no reported tag. ``min(capex, D&A)`` is the standard deterministic
   proxy: D&A is the accounting estimate of the capital consumed keeping assets
   in place, capped at the cash actually spent so a low-capex year is not
   over-charged. It is an APPROXIMATION, not the true figure.

2. **ΔWC = working_capital_t − working_capital_{t-1}**, where
   ``working_capital = accounts_receivable + inventory − accounts_payable``.
   A rising working capital balance (cash tied up in receivables / inventory)
   is a use of cash and so REDUCES owner earnings; a falling balance adds it
   back. The OLDEST year in the series has no prior, so its ΔWC — and therefore
   its owner_earnings — is ``None`` (fail-soft). Any required component being
   ``None`` for a given year propagates to ``owner_earnings = None`` for that
   year, but the record is still emitted so the series stays aligned with the
   input.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from alphalens_pipeline.data.fundamentals.annual_aggregator import AnnualStatement

# A ΔWC term is only valid between two CONSECUTIVE fiscal years. The #501
# series only emits years that carry duration data, so a missing / non-filed
# year (rare: delisting-relisting, a skipped filing) can leave a gap; without
# this guard the next-older entry would be used as the "prior" year and ΔWC
# would silently become a multi-year delta. A normal year-over-year gap is
# ~365 days; the window tolerates 52/53-week calendars and a fiscal-year-end
# shift of a few weeks while rejecting a whole skipped year (~730 days).
_PRIOR_FY_MIN_GAP_DAYS = 300
_PRIOR_FY_MAX_GAP_DAYS = 430


@dataclass(frozen=True)
class OwnerEarnings:
    """One fiscal year's owner-earnings figures, derived from #501 statements.

    ``working_capital`` is ``accounts_receivable + inventory −
    accounts_payable`` for the year, or ``None`` when any component is missing.
    ``working_capital_change`` is the year-over-year delta against the
    immediately older fiscal year, ``None`` for the oldest year (no prior) or
    when either year's working capital is ``None``. ``maintenance_capex`` is
    the ``min(capex, D&A)`` approximation, ``None`` when either input is
    missing. ``owner_earnings`` is ``None`` whenever any of net_income, D&A,
    maintenance_capex, or working_capital_change is ``None``.
    """

    fiscal_year_end: date
    fy: int
    owner_earnings: float | None
    maintenance_capex: float | None
    working_capital: float | None
    working_capital_change: float | None


def _working_capital(stmt: AnnualStatement) -> float | None:
    """``accounts_receivable + inventory − accounts_payable`` or None.

    Returns ``None`` if any of the three components is missing — a partial
    working-capital figure would silently bias the ΔWC term, so it is treated
    as undefined.
    """
    ar = stmt.accounts_receivable
    inv = stmt.inventory
    ap = stmt.accounts_payable
    if ar is None or inv is None or ap is None:
        return None
    return ar + inv - ap


def _is_prior_consecutive(prior: AnnualStatement, current: AnnualStatement) -> bool:
    """True when ``prior``'s fiscal-year end is ~1 year before ``current``'s.

    Guards the ΔWC term against a gap year in the series (which would turn a
    multi-year working-capital change into a mislabeled one-year delta).
    """
    gap_days = (current.fiscal_year_end - prior.fiscal_year_end).days
    return _PRIOR_FY_MIN_GAP_DAYS <= gap_days <= _PRIOR_FY_MAX_GAP_DAYS


def _maintenance_capex(stmt: AnnualStatement) -> float | None:
    """``min(capex, D&A)`` approximation, or None when either input is missing."""
    if stmt.capex is None or stmt.da is None:
        return None
    return min(stmt.capex, stmt.da)


def compute_owner_earnings(statements: list[AnnualStatement]) -> list[OwnerEarnings]:
    """Owner earnings per fiscal year, computed over the #501 annual series.

    ``statements`` is the newest-first series from
    :func:`annual_statements`. Each year is paired with the immediately older
    year (its successor in the list) to form ΔWC. The output preserves the
    newest-first order and has the same length as the input (one
    :class:`OwnerEarnings` per :class:`AnnualStatement`). The oldest year has
    no prior, so its ``working_capital_change`` and ``owner_earnings`` are
    ``None``.
    """
    out: list[OwnerEarnings] = []
    n = len(statements)
    for i, stmt in enumerate(statements):
        wc = _working_capital(stmt)
        # The prior fiscal year is the next (older) entry in the newest-first list.
        prior = statements[i + 1] if i + 1 < n else None
        prior_wc = _working_capital(prior) if prior is not None else None
        # Only difference CONSECUTIVE years — a gap (missing fiscal year) would
        # otherwise yield a multi-year ΔWC mislabeled as one year.
        consecutive = prior is not None and _is_prior_consecutive(prior, stmt)

        if wc is None or prior_wc is None or not consecutive:
            wc_change: float | None = None
        else:
            wc_change = wc - prior_wc

        maint = _maintenance_capex(stmt)

        if stmt.net_income is None or stmt.da is None or maint is None or wc_change is None:
            owner: float | None = None
        else:
            owner = stmt.net_income + stmt.da - maint - wc_change

        out.append(
            OwnerEarnings(
                fiscal_year_end=stmt.fiscal_year_end,
                fy=stmt.fy,
                owner_earnings=owner,
                maintenance_capex=maint,
                working_capital=wc,
                working_capital_change=wc_change,
            )
        )
    return out


__all__ = ["OwnerEarnings", "compute_owner_earnings"]
