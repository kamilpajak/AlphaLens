"""Parse ``EntityCommonStockSharesOutstanding`` history from SEC XBRL companyfacts.

Phase 2.5 foundation for PIT market-cap reconstruction: ``shares × close ≈ market cap``
where the ``shares`` side comes from this module and ``close`` from the yfinance cache.

SEC publishes XBRL companyfacts at ``data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json``
containing every concept the filer has ever reported. Shares outstanding lives under
either ``facts.us-gaap.EntityCommonStockSharesOutstanding`` (common case) or
``facts.dei.EntityCommonStockSharesOutstanding`` (some filers use only DEI). We try
us-gaap first and fall back to dei.

Each fact entry carries both ``end`` (balance-sheet period end) and ``filed`` (SEC
accept timestamp). **PIT filtering uses ``filed``** — filing date is when the info
became public. Using ``end`` would introduce look-ahead identical to the Layer 2b
SimFin Report-Date bug fixed in issue #18.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class SharesFact:
    cik: str
    end_date: date
    filed_date: date
    shares: int
    form_type: str
    accession: str


_CONCEPT = "EntityCommonStockSharesOutstanding"
_TAXONOMY_ORDER = ("us-gaap", "dei")


def parse_company_facts(payload: Mapping, cik: str) -> list[SharesFact]:
    """Extract shares-outstanding history from a companyfacts JSON payload.

    Malformed individual entries are silently dropped. Entirely missing
    concept (neither us-gaap nor dei taxonomy has it) returns ``[]``.
    """
    facts_root = payload.get("facts") or {}
    entries: list[dict] = []
    for taxonomy in _TAXONOMY_ORDER:
        concept = facts_root.get(taxonomy, {}).get(_CONCEPT)
        if concept:
            entries = concept.get("units", {}).get("shares", []) or []
            break

    out: list[SharesFact] = []
    for entry in entries:
        try:
            out.append(
                SharesFact(
                    cik=cik,
                    end_date=date.fromisoformat(entry["end"]),
                    filed_date=date.fromisoformat(entry["filed"]),
                    shares=int(entry["val"]),
                    form_type=entry.get("form", ""),
                    accession=entry.get("accn", ""),
                )
            )
        except (KeyError, ValueError, TypeError):
            continue
    return out


def latest_shares_as_of(facts: list[SharesFact], asof: date) -> int | None:
    """Latest share count where ``filed_date <= asof``. None when no fact qualifies."""
    eligible = [f for f in facts if f.filed_date <= asof]
    if not eligible:
        return None
    return max(eligible, key=lambda f: f.filed_date).shares
