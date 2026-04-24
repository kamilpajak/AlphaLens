"""Extract 10b5-1 plan adoption date from Form 4 footnote text.

Design doc §6 + R6: a cluster-buy candidate must be excluded when the
trade was executed under a 10b5-1 plan adopted more than 90 days before
the transaction (pre-planned, mechanical, low information). Adoption
dates live in Form 4 footnote free text. Post-April 2023 SEC amendments
made the wording more structured; pre-2023 filings use varied phrasings.

This module deliberately biases toward recall: if we detect a 10b5-1 plan
reference but cannot parse the adoption date, the caller conservatively
treats the trade as "<90 days old" (i.e. excluded). Precision target is
≥98% on a stratified benchmark, recall ≥95% (see R6 thresholds).
"""

from __future__ import annotations

import re
from datetime import date

_PLAN_REF_RE = re.compile(r"10\s*b\s*5\s*-\s*1", re.IGNORECASE)

# Month name → index lookup; intentionally tolerant of common abbreviations.
_MONTHS: dict[str, int] = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

_MONTH_PATTERN = "|".join(sorted(_MONTHS, key=len, reverse=True))

# Ordered by specificity. First successful match wins at each text position.
_DATE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b"), "iso"),
    (re.compile(rf"\b({_MONTH_PATTERN})\.?\s+(\d{{1,2}}),?\s+(\d{{4}})\b", re.IGNORECASE), "spelled"),
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b"), "us_numeric"),
)


def _parse_match(kind: str, groups: tuple[str, ...]) -> date | None:
    try:
        if kind == "iso":
            return date(int(groups[0]), int(groups[1]), int(groups[2]))
        if kind == "spelled":
            month = _MONTHS[groups[0].lower().rstrip(".")]
            return date(int(groups[2]), month, int(groups[1]))
        if kind == "us_numeric":
            year = int(groups[2])
            if year < 100:
                year += 2000
            return date(year, int(groups[0]), int(groups[1]))
    except (ValueError, KeyError):
        return None
    return None


def _earliest_date(text: str) -> date | None:
    """Return the earliest parseable date in the text, scanned left-to-right."""
    for pattern, kind in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            parsed = _parse_match(kind, match.groups())
            if parsed is not None:
                return parsed
    return None


def extract_10b5_1_adoption(footnote_text: str) -> tuple[bool, date | None]:
    """Parse a footnote for 10b5-1 plan reference and adoption date.

    Returns ``(has_plan_reference, adoption_date_or_none)``. When the
    footnote mentions a 10b5-1 plan but no parseable date is present, the
    tuple is ``(True, None)`` and the caller must conservatively exclude.
    """
    if not _PLAN_REF_RE.search(footnote_text):
        return False, None
    # Prefer the date nearest the plan reference (earliest date typically
    # corresponds to the adoption; subsequent dates like amendments follow).
    adopted = _earliest_date(footnote_text)
    return True, adopted


def plan_age_days(*, adoption_date: date | None, asof: date) -> int | None:
    """Return (asof - adoption_date).days, or None when adoption_date is unknown."""
    if adoption_date is None:
        return None
    return (asof - adoption_date).days
