"""Thematic-screening sector / peer resolver — thin adapter over SIC index.

Historically backed by SimFin's free bulk metadata
(``~/.alphalens/simfin_cache/{us-companies,industries}.csv``). PR #161
deleted the SimFin dependency but missed this consumer (issue #169), so
the module now delegates to :mod:`alphalens_pipeline.data.fundamentals.sic_index`
— SEC EDGAR SIC codes built into a shipped parquet artifact, no
external runtime cache required.

Public names are preserved (``get_industry_id`` / ``iter_industry_peers``
/ ``industry_label``) so the screener call sites in ``scorer.py`` and the
test mocks are unchanged. The "industry_id" int returned here is the
4-digit SEC SIC code (e.g., 3674 for Semiconductors), which differs from
SimFin's 6-digit IndustryId — opaque to callers, but tests that bake in
literal IDs have been updated accordingly.
"""

from __future__ import annotations

from alphalens_pipeline.data.fundamentals.sic_index import (
    get_sic as get_industry_id,
)
from alphalens_pipeline.data.fundamentals.sic_index import (
    iter_sic_peers as iter_industry_peers,
)
from alphalens_pipeline.data.fundamentals.sic_index import (
    iter_sic_peers_fallback as iter_industry_peers_fallback,
)
from alphalens_pipeline.data.fundamentals.sic_index import (
    sic_label as industry_label,
)

__all__ = [
    "get_industry_id",
    "industry_label",
    "iter_industry_peers",
    "iter_industry_peers_fallback",
]
