from __future__ import annotations

import statistics

from .models import BriefCandidate, ComparisonResult, DiscoverCandidate


def _median_mcap(cands) -> float | None:
    vals = [c.mcap for c in cands if c.mcap is not None]
    return statistics.median(vals) if vals else None


def compare_candidates(
    discover: list[DiscoverCandidate],
    brief: list[BriefCandidate],
) -> ComparisonResult:
    d = {c.ticker for c in discover}
    b = {c.ticker for c in brief}
    return ComparisonResult(
        shared=sorted(d & b),
        perplexity_only=sorted(d - b),
        brief_only=sorted(b - d),
        discover_median_mcap=_median_mcap(discover),
        brief_median_mcap=_median_mcap(brief),
    )
