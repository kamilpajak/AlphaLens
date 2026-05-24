"""Ticker / industry / sector resolver backed by SEC's SIC codes.

Replaces the former SimFin bulk-metadata loader at
:mod:`alphalens_pipeline.thematic.screening.sector_peers` after PR #161 removed the
``simfin>=1.0.2`` dependency but missed this independent SimFin consumer
(issue #169).

Source of truth: a small parquet shipped at
``alphalens_pipeline/data/fundamentals/sic_index.parquet`` with columns
``ticker / cik / sic / sic_description``. Rebuilt offline by
``scripts/build_sic_index.py`` (walks the SP1500 PIT YAMLs + delisted
overlay, fetches each CIK's top-level ``sic``/``sicDescription`` via the
canonical :class:`alphalens_pipeline.data.alt_data.sec_edgar_client.SecEdgarClient`,
writes the parquet). Refresh cadence: manual, monthly — SIC reassignments
are rare.

Cohort-width note: SIC's 4-digit taxonomy is broader than SimFin's
6-digit hierarchical IndustryId. The legacy "Quantum Computing"
sub-industry (4 tickers) is absorbed into "Semiconductors & Related
Devices" (~100 tickers). Sector-percentile signals consumed by
``scorer.py`` widen accordingly. Acceptable trade-off for the
single-vendor (EDGAR) unblock; theme-conditional cohort refinement is
deferred.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

# Default artifact location: package-internal so it ships in the Docker
# pipeline image (`deploy/docker/Dockerfile.pipeline` `COPY`s `alphalens_pipeline/`).
# Test code monkey-patches this module attribute.
_SIC_INDEX_PATH = Path(__file__).parent / "sic_index.parquet"


# SIC Division ranges per SEC's published Standard Industrial Classification
# manual. Each tuple is (low_inclusive, high_inclusive, division_name).
# Source: https://www.sec.gov/info/edgar/siccodes.htm
_SIC_DIVISION_RANGES: tuple[tuple[int, int, str], ...] = (
    (100, 999, "Agriculture, Forestry and Fishing"),
    (1000, 1499, "Mining"),
    (1500, 1799, "Construction"),
    (2000, 3999, "Manufacturing"),
    (4000, 4999, "Transportation, Communications, Electric, Gas and Sanitary services"),
    (5000, 5199, "Wholesale Trade"),
    (5200, 5999, "Retail Trade"),
    (6000, 6799, "Finance, Insurance and Real Estate"),
    (7000, 8999, "Services"),
    (9100, 9729, "Public Administration"),
)


def _division_name(sic: int) -> str | None:
    """Return the SEC division name for a 4-digit SIC code, or None if unmapped."""
    for low, high, name in _SIC_DIVISION_RANGES:
        if low <= sic <= high:
            return name
    return None


@lru_cache(maxsize=1)
def _load_index() -> pa.Table | None:
    """Read the SIC index parquet once per process; None when missing."""
    if not _SIC_INDEX_PATH.exists():
        return None
    return pq.read_table(_SIC_INDEX_PATH)


DEFAULT_MIN_COHORT = 8


@lru_cache(maxsize=1)
def _load_lookup_dicts() -> tuple[dict[str, int], dict[int, list[str]], dict[int, str]]:
    """Materialise the parquet into three dicts keyed for O(1) lookup.

    Returns ``(ticker_to_sic, sic_to_peers, sic_to_description)``. Empty
    dicts when the parquet artifact is missing. Built once per process
    (memoised); tests clear the cache between cases.

    Cache mutation invariant: the dicts returned here are shared across
    every caller in the process. Any module-public accessor that exposes
    a MUTABLE value (currently only ``iter_sic_peers`` on the
    ``list[str]`` values of ``sic_to_peers``) MUST return a defensive
    copy at its boundary — otherwise a downstream ``.append`` /
    ``.sort`` / ``.pop`` silently corrupts the global cache for the
    remainder of the process. If the schema ever evolves (e.g.
    ``sic_to_description`` switching from ``str`` to ``list[str]``),
    extend the same discipline to the new accessor — and add a
    regression test in the shape of
    ``test_returned_list_is_defensive_copy``.
    """
    table = _load_index()
    if table is None:
        return {}, {}, {}
    tickers = table.column("ticker").to_pylist()
    sics = table.column("sic").to_pylist()
    descriptions = table.column("sic_description").to_pylist()
    ticker_to_sic: dict[str, int] = {}
    sic_to_peers: dict[int, list[str]] = {}
    sic_to_description: dict[int, str] = {}
    for ticker, sic, description in zip(tickers, sics, descriptions, strict=True):
        if sic is None:
            continue
        ticker_to_sic[ticker] = int(sic)
        sic_to_peers.setdefault(int(sic), []).append(ticker)
        # First description wins; tickers sharing a SIC share an EDGAR
        # description by construction (filers carry the same sicDescription
        # for the same code), so the "first wins" policy is stable.
        sic_to_description.setdefault(int(sic), description or "")
    return ticker_to_sic, sic_to_peers, sic_to_description


def get_sic(ticker: str) -> int | None:
    """Return the 4-digit SEC SIC code for ``ticker``, or None if unmapped.

    Case-insensitive on the ticker. Missing ticker, missing index file, or a
    ticker with a null SIC all resolve to None — the caller's contract
    treats None as "no peer cohort available" and skips the percentile
    signal for that candidate.
    """
    if not ticker:
        return None
    ticker_to_sic, _, _ = _load_lookup_dicts()
    return ticker_to_sic.get(ticker.upper())


def iter_sic_peers(sic: int | None) -> list[str]:
    """Return all tickers sharing ``sic``. Empty list for unknown / None.

    Membership is computed from the shipped parquet artifact, so the peer
    set reflects whichever ticker universe the index was built from. New
    IPOs that were absent at build time will not appear as peers until
    the next ``scripts/build_sic_index.py`` refresh.

    Returns a fresh list each call — the underlying ``sic_to_peers`` dict
    is built once per process via ``@lru_cache`` and reused, so a caller
    that mutates the returned list (``.append``, ``.sort``, ``.pop``)
    would silently corrupt the cache for every subsequent caller. The
    ``list(...)`` copy at the boundary is the cheap defense.
    """
    if sic is None:
        return []
    _, sic_to_peers, _ = _load_lookup_dicts()
    return list(sic_to_peers.get(sic, []))


@lru_cache(maxsize=1)
def _load_sic3_peers() -> dict[int, list[str]]:
    """Per-3-digit-SIC peer lists, deduped, used by the fallback resolver.

    Bhojraj-Lee-Oler 2003 finds 3-digit aggregations modestly improve
    within-cohort homogeneity over 4-digit (different sub-segments of the
    same broad industry still trade on the same fundamentals), but
    2-digit pools become too heterogeneous (SIC 73 lumps software with
    temp staffing and commercial printing). The fallback resolver
    therefore tops out at 3-digit.
    """
    _, sic_to_peers, _ = _load_lookup_dicts()
    out: dict[int, list[str]] = {}
    for sic, tickers in sic_to_peers.items():
        prefix = sic // 10
        # `setdefault` once per prefix avoids re-fetching the list per ticker.
        bucket = out.setdefault(prefix, [])
        bucket.extend(tickers)
    # Dedup while preserving the first-seen order — important when a peer
    # ranking downstream depends on stable iteration.
    for prefix, tickers in out.items():
        seen: set[str] = set()
        deduped: list[str] = []
        for t in tickers:
            if t not in seen:
                seen.add(t)
                deduped.append(t)
        out[prefix] = deduped
    return out


def iter_peers_fallback(
    sic: int | None,
    *,
    min_cohort: int = DEFAULT_MIN_COHORT,
    peer_filter: Callable[[list[str]], list[str]] | None = None,
) -> tuple[list[str], str]:
    """Resolve peers for ``sic`` with a 3-step fallback chain.

    Tries: exact 4-digit cohort → 3-digit prefix (e.g. 7372 → 737,
    gathering 7370..7379) → Fama-French 48-industry bucket → thin.

    The FF-48 step (issue #198) widens past the 3-digit limit using a
    SIC-derivative aggregation built in academia (Fama-French 1997
    "Industry Costs of Equity"). FF-48's coarser buckets are
    empirically more economically coherent than SIC at the same width
    (Bhojraj-Lee-Oler 2003, Hrazdil-Scott 2013), so the DFIN-style case
    (SIC 7380 → only 2-3 raw peers) widens into "Business Services"
    (~150-300 peers covering SIC 7370-7399) instead of collapsing to
    "thin".

    ``peer_filter`` is an optional callback that drops peers the caller
    considers non-comparable (typically the mcap / penny-stock floor in
    :func:`alphalens_pipeline.thematic.screening._common.filter_peers_by_mcap_price`).
    The filter is applied BEFORE the ``min_cohort`` check at every level
    of the chain so a raw cohort that clears the floor but is mostly
    warrants / shells / penny stocks correctly falls through to the next
    level rather than rendering a "sic4" / "sic3" / "ff48" badge over an
    effective sub-floor cohort (Gemini 3 Pro review on PR #215).

    Returns ``(peers, level)`` where ``level`` is one of:
    - ``"sic4"`` — exact 4-digit cohort met ``min_cohort`` AFTER filter
    - ``"sic3"`` — 3-digit prefix cohort met ``min_cohort`` AFTER filter
    - ``"ff48"`` — Fama-French 48 industry cohort met ``min_cohort``
      AFTER filter
    - ``"thin"`` — no level met the floor; caller should treat as "no
      percentile available" and surface the thin-cohort badge instead of
      a colored signal bar.
    """
    if sic is None:
        return [], "thin"
    sic4 = iter_sic_peers(sic)
    if peer_filter is not None:
        sic4 = peer_filter(sic4)
    if len(sic4) >= min_cohort:
        return sic4, "sic4"
    sic3 = list(_load_sic3_peers().get(sic // 10, []))
    if peer_filter is not None:
        sic3 = peer_filter(sic3)
    if len(sic3) >= min_cohort:
        return sic3, "sic3"
    # Lazy import to break the ff_industries → sic_index circular dep —
    # ff_industries imports get_sic + _load_lookup_dicts from this module.
    from alphalens_pipeline.data.fundamentals import ff_industries

    ff48_id = ff_industries.sic_to_ff48(sic)
    ff48 = ff_industries.iter_ff48_peers(ff48_id)
    if peer_filter is not None:
        ff48 = peer_filter(ff48)
    if len(ff48) >= min_cohort:
        return ff48, "ff48"
    return [], "thin"


def sic_label(sic: int | None) -> tuple[str | None, str | None]:
    """Return ``(industry_name, sector_name)`` for ``sic``.

    ``industry_name`` is the EDGAR-reported ``sicDescription`` (the
    fine-grained 4-digit-code human label). ``sector_name`` is the SEC
    SIC Division name (coarser, 10 buckets). ``(None, None)`` when the
    SIC is unknown to the index.
    """
    if sic is None:
        return (None, None)
    _, _, sic_to_description = _load_lookup_dicts()
    description = sic_to_description.get(sic)
    if description is None:
        return (None, None)
    return (description, _division_name(sic))


__all__ = [
    "DEFAULT_MIN_COHORT",
    "get_sic",
    "iter_peers_fallback",
    "iter_sic_peers",
    "sic_label",
]
