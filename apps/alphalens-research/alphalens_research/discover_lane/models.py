from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DiscoverCandidate:
    ticker: str
    company: str
    theme: str
    rationale: str
    citation_count: int
    citation_urls: list[str]
    source_event_title: str
    source_event_url: str
    mcap: float | None = None
    resolved: bool = False
    in_pipeline_universe: bool = False


@dataclass(frozen=True)
class BriefCandidate:
    ticker: str
    company: str
    theme: str
    source_event_title: str
    mcap: float | None


@dataclass(frozen=True)
class ComparisonResult:
    shared: list[str]
    perplexity_only: list[str]
    brief_only: list[str]
    discover_median_mcap: float | None
    brief_median_mcap: float | None


@dataclass(frozen=True)
class DateBlock:
    date: str
    discover: list[DiscoverCandidate]
    brief: list[BriefCandidate]
    comparison: ComparisonResult
