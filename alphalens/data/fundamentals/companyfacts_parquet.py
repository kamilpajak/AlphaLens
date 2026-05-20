"""Long-format Parquet representation of SEC EDGAR companyfacts.

Replaces the dict-of-parsed-JSON cache pattern of FosterSUEStore /
SloanAccrualsStore / AnnouncementDateProvider where each store independently
held parsed JSON in memory at 90 GB peak RSS for the S&P 1500 universe (3x
duplication + Python dict overhead). Long-format Arrow Tables compress ~6x
on disk via dictionary encoding and load ~30x cheaper than parsed JSON dicts.

Layout: ``~/.alphalens/companyfacts_parquet/{cik}.parquet``, one file per CIK
mirroring the JSON layout 1:1 to simplify migration. Stores access via
``CompanyfactsParquetReader.get_cik_table(cik)`` and filter in-memory via
``pyarrow.compute.equal`` -- vectorized, no per-store JSON dict materialization.

Schema (long-format):

  taxonomy       string   (us-gaap | dei)
  concept        string   (e.g. EarningsPerShareBasic)
  unit           string   (USD | USD/shares | shares | ...)
  period_start   date32   nullable (instants have no start)
  period_end     date32
  val            float64
  accn           string
  fy             int32    nullable (some SEC entries use synthetic frame codes >32767)
  fp             string   nullable (Q1/Q2/Q3/Q4/FY/H1/H2/CY)
  form           string   (10-K/10-Q/8-K/...)
  filed_date     date32
  frame          string   nullable (e.g. CY2023Q2)
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

logger = logging.getLogger(__name__)


SCHEMA = pa.schema(
    [
        pa.field("taxonomy", pa.string(), nullable=False),
        pa.field("concept", pa.string(), nullable=False),
        pa.field("unit", pa.string(), nullable=False),
        pa.field("period_start", pa.date32(), nullable=True),
        pa.field("period_end", pa.date32(), nullable=False),
        pa.field("val", pa.float64(), nullable=False),
        pa.field("accn", pa.string(), nullable=False),
        pa.field("fy", pa.int32(), nullable=True),
        pa.field("fp", pa.string(), nullable=True),
        pa.field("form", pa.string(), nullable=False),
        pa.field("filed_date", pa.date32(), nullable=False),
        pa.field("frame", pa.string(), nullable=True),
    ]
)


def _parse_iso(value: str | None) -> date | None:
    if value is None:
        return None
    return date.fromisoformat(value)


def _safe_int(value: Any) -> int | None:
    """Defensive cast for SEC ``fy`` field — None for missing or non-numeric."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _iter_unit_entries(facts_block: dict) -> Any:
    """Yield (taxonomy, concept, unit, entry) tuples for each valid record."""
    for taxonomy, concepts in facts_block.items():
        if not isinstance(concepts, dict):
            continue
        for concept, concept_block in concepts.items():
            if not isinstance(concept_block, dict):
                continue
            units_block = concept_block.get("units")
            if not isinstance(units_block, dict):
                continue
            for unit, entries in units_block.items():
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if isinstance(entry, dict):
                        yield taxonomy, concept, unit, entry


def _append_entry_row(
    columns: dict[str, list],
    taxonomy: str,
    concept: str,
    unit: str,
    entry: dict,
) -> None:
    columns["taxonomy"].append(taxonomy)
    columns["concept"].append(concept)
    columns["unit"].append(unit)
    columns["period_start"].append(_parse_iso(entry.get("start")))
    columns["period_end"].append(_parse_iso(entry.get("end")))
    columns["val"].append(float(entry["val"]))
    columns["accn"].append(entry.get("accn") or "")
    columns["fy"].append(_safe_int(entry.get("fy")))
    columns["fp"].append(entry.get("fp"))
    columns["form"].append(entry.get("form") or "")
    columns["filed_date"].append(_parse_iso(entry["filed"]))
    columns["frame"].append(entry.get("frame"))


def companyfacts_json_to_parquet_table(facts: dict[str, Any]) -> pa.Table:
    """Convert a parsed SEC companyfacts JSON dict into a long-format Arrow Table.

    Walks ``facts["facts"][taxonomy][concept]["units"][unit]`` and emits one
    row per entry. Entries missing any required field (end / filed / val /
    accn / form) are skipped silently -- SEC bulk dumps occasionally contain
    truncated records that should not propagate downstream.
    """
    columns: dict[str, list] = {field.name: [] for field in SCHEMA}

    facts_block = facts.get("facts", {})
    if not isinstance(facts_block, dict):
        return pa.Table.from_pydict(columns, schema=SCHEMA)

    for taxonomy, concept, unit, entry in _iter_unit_entries(facts_block):
        if entry.get("end") is None or entry.get("filed") is None or entry.get("val") is None:
            continue
        _append_entry_row(columns, taxonomy, concept, unit, entry)

    return pa.Table.from_pydict(columns, schema=SCHEMA)


# ---------------------------------------------------------------------------
# Reader: per-CIK Arrow Table accessor with bounded FIFO cache.
#
# Stores call ``get_cik_table(cik)`` once per CIK and filter the resulting
# table in-memory via vectorized pyarrow.compute (no per-store JSON dict
# materialization). The cache holds whole Arrow Tables -- compressed in
# memory via dictionary-encoded string columns -- and FIFO-evicts the
# oldest entries when capacity is hit. FIFO (rather than LRU) is fine for
# the warm-pass-once access pattern of all current consumers; switch to
# LRU only if a future caller revisits CIKs out of insertion order.


def _evict_to_capacity(cache: dict, max_size: int) -> int:
    """FIFO eviction: drop oldest entries until ``len(cache) <= max_size``."""
    max_size = max(max_size, 0)
    evicted = 0
    while len(cache) > max_size:
        cache.pop(next(iter(cache)))
        evicted += 1
    return evicted


class CompanyfactsParquetReader:
    """Read per-CIK long-format Arrow Tables from a parquet directory.

    Layout: ``{root}/{cik}.parquet`` (one file per CIK, matching SEC bulk-dump
    JSON layout 1:1). Missing CIK files return ``None``; the negative result
    is cached so repeated lookups do not re-stat the filesystem.

    Cache capacity defaults to 2500, which covers the full S&P 1500 PIT
    universe AND Russell 2000 (~2000 CIKs) compound experiments with
    margin while keeping the in-memory footprint bounded (Arrow Tables
    are dictionary-encoded; ~5 KB / CIK typical -> ~10-15 MB total at
    full capacity). Override the default upward only when a single
    experiment touches > 2500 unique CIKs.
    """

    def __init__(self, parquet_dir: Path, *, cache_capacity: int = 2500) -> None:
        self._dir = Path(parquet_dir)
        self._cache: dict[str, pa.Table | None] = {}
        self._cache_capacity = cache_capacity

    def get_cik_table(self, cik: str) -> pa.Table | None:
        if cik in self._cache:
            return self._cache[cik]
        table = self._read_table_from_disk(cik)
        self._cache[cik] = table
        _evict_to_capacity(self._cache, self._cache_capacity)
        return table

    def _read_table_from_disk(self, cik: str) -> pa.Table | None:
        path = self._dir / f"{cik}.parquet"
        if not path.exists():
            return None
        try:
            return pq.read_table(path)
        except (OSError, pa.ArrowInvalid) as exc:
            logger.warning("Failed to read parquet for CIK %s: %s", cik, exc)
            return None


def filter_concept(
    table: pa.Table,
    taxonomy: str,
    concept: str,
    unit: str | None = None,
) -> pa.Table:
    """Return rows where ``taxonomy`` and ``concept`` (and optionally ``unit``) match.

    Vectorized via ``pyarrow.compute.equal`` -- no Python-level row iteration.
    Returns an empty Arrow Table (with the canonical schema) when nothing
    matches; never returns ``None``.
    """
    mask = pc.and_(
        pc.equal(table["taxonomy"], taxonomy),
        pc.equal(table["concept"], concept),
    )
    if unit is not None:
        mask = pc.and_(mask, pc.equal(table["unit"], unit))
    return table.filter(mask)
