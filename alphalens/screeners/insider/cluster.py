"""Cluster detection: identify Kelley-Tetlock cluster-buy events.

Pure function over a collection of ``Form4Record``. A cluster is present
when ≥N distinct reporting owners each placed an eligible open-market
purchase within a trailing window of ``asof``. Eligibility:

- Transaction occurred in ``[asof - window_days, asof]`` inclusive.
- Transaction is not made under a 10b5-1 plan that was adopted ≥
  ``plan_age_threshold_days`` before ``asof`` (old plans = mechanical
  execution; design doc §6 excludes them).
- 10b5-1 plan with unparseable adoption date → conservative exclude.

Input records should already be code-P + officer/director filtered by
M2b ``filter_eligible`` — the cluster module does not re-apply those
checks.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from alphalens.alt_data.form4_records import Form4Record
from alphalens.alt_data.plan_10b5_1 import extract_10b5_1_adoption, plan_age_days


@dataclass(frozen=True)
class ClusterMetrics:
    insider_count: int
    aggregate_dollar: Decimal
    records: tuple[Form4Record, ...]


def detect_cluster(
    records: list[Form4Record],
    asof: date,
    *,
    window_days: int = 30,
    min_distinct_insiders: int = 3,
    plan_age_threshold_days: int = 90,
) -> ClusterMetrics | None:
    window_start = asof - timedelta(days=window_days)

    eligible: list[Form4Record] = []
    for r in records:
        if r.transaction_date < window_start or r.transaction_date > asof:
            continue
        if _should_exclude_for_10b5_1(r, asof, plan_age_threshold_days):
            continue
        eligible.append(r)

    distinct_insiders = {r.reporting_owner_cik for r in eligible}
    if len(distinct_insiders) < min_distinct_insiders:
        return None

    aggregate = sum(
        (
            r.transaction_shares * r.transaction_price_per_share
            for r in eligible
            if r.transaction_price_per_share is not None
        ),
        start=Decimal("0"),
    )
    return ClusterMetrics(
        insider_count=len(distinct_insiders),
        aggregate_dollar=aggregate,
        records=tuple(eligible),
    )


def _should_exclude_for_10b5_1(
    record: Form4Record,
    asof: date,
    threshold_days: int,
) -> bool:
    for _, text in record.footnotes:
        has_plan, adopted = extract_10b5_1_adoption(text)
        if not has_plan:
            continue
        age = plan_age_days(adoption_date=adopted, asof=asof)
        if age is None or age >= threshold_days:
            return True
    return False
