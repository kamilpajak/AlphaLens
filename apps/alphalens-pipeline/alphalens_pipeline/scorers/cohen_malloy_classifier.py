"""Cohen-Malloy-Pomorski 2012 routine vs opportunistic insider classifier.

Faithful replication of the paper's classification rule per p. 1786 Section III.A:

  Routine = trade in same calendar month for at least 3 CONSECUTIVE prior years.
  Opportunistic = everyone else WITH sufficient history.
  Eligibility = >=1 trade in EACH of the 3 preceding calendar years.
  Lookback = 3 years (NOT 5).

Classification is performed at the START of each calendar year Y, locked for
that year, and re-evaluated annually using the rolling window [Y-3, Y).
A trade IN year Y itself does NOT count toward eligibility (paper: "based on
past history of trades... and then look to see how they trade from that point
onwards").
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from enum import StrEnum

LOOKBACK_YEARS = 3


class CohenMalloyLabel(StrEnum):
    """Three-way classification; UNCLASSIFIED records are dropped from signal."""

    ROUTINE = "routine"
    OPPORTUNISTIC = "opportunistic"
    UNCLASSIFIED = "unclassified"


def classify_from_transaction_dates(
    transaction_dates: Iterable[date],
    *,
    classification_year: int,
) -> CohenMalloyLabel:
    """Classify a single insider given their transaction history.

    Parameters
    ----------
    transaction_dates
        Iterable of ``datetime.date`` values representing all known Form-4
        transactions by the insider. Dates outside the 3-year lookback
        window (and dates in or after ``classification_year``) are ignored.
    classification_year
        The calendar year for which the classification applies. The lookback
        window is [classification_year - 3, classification_year).

    Returns
    -------
    CohenMalloyLabel
        ``ROUTINE`` if there exists any calendar month M such that the insider
        traded in month M in EACH of the 3 preceding years. ``OPPORTUNISTIC``
        if the insider has >=1 trade in each of the 3 preceding years but no
        such consistent same-month pattern. ``UNCLASSIFIED`` if any of the 3
        preceding years has zero trades (insufficient history).
    """
    window_start_year = classification_year - LOOKBACK_YEARS  # inclusive
    window_end_year = classification_year  # exclusive

    # Restrict to in-window transactions only — pre-window and current-year
    # trades both excluded per paper definition.
    in_window = [d for d in transaction_dates if window_start_year <= d.year < window_end_year]

    # Eligibility: must have >=1 trade in EACH of the 3 preceding years.
    years_with_trades = {d.year for d in in_window}
    required_years = set(range(window_start_year, window_end_year))
    if not required_years.issubset(years_with_trades):
        return CohenMalloyLabel.UNCLASSIFIED

    # Routine check: does there exist a calendar month M (1..12) present in
    # ALL 3 preceding years?
    months_per_year: dict[int, set[int]] = {y: set() for y in required_years}
    for d in in_window:
        months_per_year[d.year].add(d.month)
    common_months = set.intersection(*months_per_year.values())
    if common_months:
        return CohenMalloyLabel.ROUTINE
    return CohenMalloyLabel.OPPORTUNISTIC
