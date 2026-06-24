from __future__ import annotations

import dataclasses

from .models import DiscoverCandidate


def enrich_candidates(
    candidates: list[DiscoverCandidate],
    *,
    yf_client,
    universe: set[str],
) -> list[DiscoverCandidate]:
    out: list[DiscoverCandidate] = []
    seen: set[str] = set()
    for c in candidates:
        if c.ticker in seen:
            continue
        seen.add(c.ticker)
        mcap = yf_client.market_cap(c.ticker)
        out.append(
            dataclasses.replace(
                c,
                mcap=mcap,
                resolved=mcap is not None,
                in_pipeline_universe=c.ticker in universe,
            )
        )
    return out
