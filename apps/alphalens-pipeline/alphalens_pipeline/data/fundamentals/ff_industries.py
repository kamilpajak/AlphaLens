"""Fama-French 48-industry (FF-48) resolver — SIC-derivative cohort aggregator.

Why FF-48: SEC SIC's 4-digit codes split economically related companies
into siblings that should be ranked together (e.g., SIC 7371 / 7372 /
7373 / 7374 — all "computer services" but each its own peer cohort).
Bhojraj-Lee-Oler 2003 and Hrazdil-Scott 2013 show that academia-built
aggregations (GICS, FF-48) yield higher within-industry return
correlations and tighter valuation multiples than raw SIC. GICS is
S&P/MSCI IP and paid; FF-48 (Fama-French 1997 "Industry Costs of
Equity") is free and shipped from Ken French's data library.

Use here: a fallback cohort layer beyond
:func:`alphalens_pipeline.data.fundamentals.sic_index.iter_peers_fallback`'s
3-digit step. The fallback chain becomes::

    4-digit SIC → 3-digit SIC → FF-48 → thin

so DFIN-style cases (SIC 7380 with 5 raw peers) widen into the
"Business Services" bucket (~hundreds of peers) instead of falling to
"thin" and losing the percentile signal entirely. See issue #198.

Source of truth: a small parquet shipped at
``alphalens_pipeline/data/fundamentals/ff48_crosswalk.parquet`` with
columns ``sic_low / sic_high / ff48_id / ff48_short / ff48_name``.
Built offline from Ken French's ``Siccodes48.txt`` by
``scripts/build_ff48_index.py``. Refresh cadence: rebuilds with
``build_sic_index`` (monthly); FF-48 definitions themselves have been
stable since 1997.

Convention: any SIC NOT in any explicit range maps to industry 48
("Other / Almost Nothing"). Industry 48 also has explicit ranges in the
Ken French file (4950-4959 etc.) — those are absorbed naturally by the
range walk. The "if no range matches → 48" default mirrors academic
implementations.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pyarrow.parquet as pq

from alphalens_pipeline.data.fundamentals.sic_index import (
    _load_lookup_dicts,
    get_sic,
)

# Default artifact location: same convention as ``sic_index.parquet`` —
# package-internal so it ships in the Docker pipeline image.
# Test code monkey-patches this attribute.
_FF48_CROSSWALK_PATH = Path(__file__).parent / "ff48_crosswalk.parquet"

# Catch-all industry id per FF-48 convention (Fama-French 1997).
_FF48_OTHER_ID = 48


@lru_cache(maxsize=1)
def _load_ranges() -> list[tuple[int, int, int]]:
    """Read the crosswalk parquet once; return (sic_low, sic_high, ff48_id) tuples.

    Returns an empty list when the artifact is missing — see
    :func:`sic_to_ff48` for how callers should interpret this (an empty
    list disables the FF-48 mapping rather than collapsing every ticker
    into industry 48 Other).
    """
    if not _FF48_CROSSWALK_PATH.exists():
        return []
    table = pq.read_table(_FF48_CROSSWALK_PATH)
    lows = table.column("sic_low").to_pylist()
    highs = table.column("sic_high").to_pylist()
    ids = table.column("ff48_id").to_pylist()
    return list(zip(lows, highs, ids, strict=True))


@lru_cache(maxsize=1)
def _load_ff48_lookups() -> tuple[dict[int, str], dict[int, str]]:
    """Materialise ``(id → short_label, id → long_name)`` from the crosswalk."""
    if not _FF48_CROSSWALK_PATH.exists():
        return {}, {}
    table = pq.read_table(_FF48_CROSSWALK_PATH)
    ids = table.column("ff48_id").to_pylist()
    shorts = table.column("ff48_short").to_pylist()
    names = table.column("ff48_name").to_pylist()
    id_to_short: dict[int, str] = {}
    id_to_name: dict[int, str] = {}
    for fid, short, name in zip(ids, shorts, names, strict=True):
        # First-seen wins (the crosswalk repeats the (short, name) pair
        # across every SIC range that maps to the same industry).
        id_to_short.setdefault(int(fid), str(short))
        id_to_name.setdefault(int(fid), str(name))
    return id_to_short, id_to_name


@lru_cache(maxsize=1)
def _load_ff48_peers() -> dict[int, list[str]]:
    """Reverse-index ``ff48_id → [tickers]`` by walking the SIC index.

    Cache invariant mirrors ``sic_index._load_lookup_dicts``: the list
    values are shared process-wide, so ``iter_ff48_peers`` returns a
    defensive copy.
    """
    ticker_to_sic, _, _ = _load_lookup_dicts()
    out: dict[int, list[str]] = {}
    for ticker, sic in ticker_to_sic.items():
        ff48 = sic_to_ff48(sic)
        if ff48 is None:
            continue
        out.setdefault(ff48, []).append(ticker)
    return out


def sic_to_ff48(sic: int | None) -> int | None:
    """Map a 4-digit SEC SIC code to the Fama-French 48-industry id.

    ``None`` in → ``None`` out (no peer cohort available). Otherwise
    walks the shipped crosswalk ranges; the first match wins. If no
    explicit range matches, returns 48 (Other) per FF-48 convention —
    the SIC is real but uncategorised.

    The "match-first" order matters when the crosswalk has overlapping
    ranges (it doesn't in the stock Ken French file, but a manually
    edited fixture might): the first row in the parquet file wins,
    which is the order the build script emits.
    """
    if sic is None:
        return None
    ranges = _load_ranges()
    # Defensive: an empty range list means the crosswalk artifact was
    # missing or empty at process start. Returning ``_FF48_OTHER_ID`` in
    # that case would silently sweep every ticker into industry 48 and
    # make ``iter_ff48_peers(48)`` return the whole universe — a
    # meaningless cohort that the fallback resolver might then accept.
    # Return ``None`` so the caller can treat FF-48 as unavailable.
    if not ranges:
        return None
    for low, high, fid in ranges:
        if low <= sic <= high:
            return int(fid)
    return _FF48_OTHER_ID


def get_ff48(ticker: str) -> int | None:
    """Resolve ``ticker`` to its FF-48 industry id, or None when unknown.

    Case-insensitive on the ticker (delegates to
    :func:`alphalens_pipeline.data.fundamentals.sic_index.get_sic`).
    A ticker present in the SIC index with an unmapped SIC returns 48
    (Other); a ticker absent from the index returns ``None``.
    """
    sic = get_sic(ticker)
    if sic is None:
        return None
    return sic_to_ff48(sic)


def get_ff48_label(ff48: int) -> tuple[str, str] | None:
    """Return ``(short, long_name)`` for the FF-48 industry id, or None.

    ``short`` is the 5-char abbreviation Ken French uses in the data
    library (``BusSv``, ``Comps``, ``RlEst``); ``long_name`` is the
    human label (``Business Services``, ``Computers``, ``Real Estate``).
    Unknown ids return ``None`` — surface as "no FF-48 label available"
    rather than fabricating one.
    """
    id_to_short, id_to_name = _load_ff48_lookups()
    short = id_to_short.get(ff48)
    name = id_to_name.get(ff48)
    if short is None or name is None:
        return None
    return (short, name)


def iter_ff48_peers(ff48: int | None) -> list[str]:
    """Return every ticker in the SIC index whose SIC maps to ``ff48``.

    Empty list for unknown / None. Returns a fresh list each call — the
    underlying ``_load_ff48_peers`` dict is process-wide cached, so a
    caller that mutates the result would silently corrupt the cache for
    every subsequent caller. The ``list(...)`` copy at the boundary is
    the cheap defense (same invariant as ``iter_sic_peers``).
    """
    if ff48 is None:
        return []
    return list(_load_ff48_peers().get(ff48, []))


__all__ = [
    "get_ff48",
    "get_ff48_label",
    "iter_ff48_peers",
    "sic_to_ff48",
]
