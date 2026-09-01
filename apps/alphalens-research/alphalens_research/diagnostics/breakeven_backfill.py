"""Pure logic to backfill the break-even what-if column onto historical rows.

The population monitor stamps ``breakeven_realized_r_json`` only on a fresh minute
resolve, so rows it froze BEFORE the column existed (already-terminal decisions)
carry no value. This module fills those gaps from the retained bars WITHOUT ever
overwriting a value the monitor already stamped. It is display-only (the headline
``realized_r`` is never touched). Thin I/O wrapper: ``scripts/backfill_breakeven_whatif.py``.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable
from typing import Any

import pandas as pd
from alphalens_pipeline.paper.calendar import (
    advance_trading_sessions,
    session_on_or_after,
    session_open_utc,
)

_COLUMN = "breakeven_realized_r_json"

# Sentinel for "this row cannot be computed at all" (no setup / no bars) in the
# per-key merge below — distinct from an HONEST null (a lens that resolved to
# no-value, which the monitor also stamps forward as null).
UNRESOLVABLE = object()


def _is_empty(value: Any) -> bool:
    """A cell counts as 'no what-if yet' when it is None / NaN / an empty string."""
    if value is None or value == "":
        return True
    return isinstance(value, float) and pd.isna(value)


def rows_needing_backfill(df: pd.DataFrame) -> list[int]:
    """Positional indices of plannable rows that lack the break-even what-if column.

    A missing column (an old parquet) means every plannable row needs it. Rows the
    monitor already stamped (non-empty) are excluded, so an existing value is never
    a candidate for overwrite.
    """
    has_col = _COLUMN in df.columns
    has_plannable = "plannable" in df.columns
    out: list[int] = []
    for i in range(len(df)):
        row = df.iloc[i]
        if has_plannable and not bool(row["plannable"]):
            continue
        if not has_col or _is_empty(row[_COLUMN]):
            out.append(i)
    return out


def apply_backfill(
    df: pd.DataFrame,
    compute: Callable[[Any], str | None],
) -> tuple[pd.DataFrame, int]:
    """Fill the break-even column on rows that lack it, using ``compute(row)``.

    ``compute`` returns the ``json.dumps`` of ``{lens_id: realized_r}`` for a row, or
    ``None`` when the row cannot be resolved (no setup / no retained bars) — in which
    case the row is left untouched. Never overwrites an existing value. Returns a NEW
    frame (the input is not mutated) plus the count of rows actually filled.
    """
    out = df.copy()
    if _COLUMN not in out.columns:
        out[_COLUMN] = None
    values: list[Any] = out[_COLUMN].tolist()
    filled = 0
    for i in rows_needing_backfill(out):
        value = compute(out.iloc[i])
        if value is not None:
            values[i] = value
            filled += 1
    out[_COLUMN] = values
    return out, filled


def _parse_map(value: Any) -> dict[str, Any] | None:
    """Parse a cell into its lens map, or ``None`` for empty/malformed cells."""
    if _is_empty(value):
        return None
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def rows_missing_lens_key(df: pd.DataFrame, lens_id: str) -> list[int]:
    """Positional indices of plannable rows whose NON-EMPTY lens map lacks ``lens_id``.

    Per-key complement of :func:`rows_needing_backfill` (issue #1232): that helper
    treats any non-empty cell as done, so it can never add a NEW lens to a frozen
    map. This one selects exactly the rows whose map exists but misses the key —
    or carries it as null (an accidental cutoff-less run stamps null, which stays
    recoverable). Empty cells stay the old helper's scope; an absent column yields
    no candidates (there is nothing to merge into).
    """
    if _COLUMN not in df.columns:
        return []
    has_plannable = "plannable" in df.columns
    out: list[int] = []
    for i in range(len(df)):
        row = df.iloc[i]
        if has_plannable and not bool(row["plannable"]):
            continue
        parsed = _parse_map(row[_COLUMN])
        if parsed is None:
            continue
        if lens_id not in parsed or parsed[lens_id] is None:
            out.append(i)
    return out


def apply_lens_key_backfill(
    df: pd.DataFrame,
    lens_id: str,
    compute: Callable[[Any], Any],
) -> tuple[pd.DataFrame, int]:
    """Merge ONLY ``lens_id`` into each candidate row's existing lens map.

    ``compute(row)`` returns the lens's realized R for the row: a float, ``None``
    for an honest no-value (stamped as null, mirroring the monitor's forward
    stamp), or :data:`UNRESOLVABLE` (no setup / no bars — the row is left
    untouched). Every pre-existing key keeps its stamped value verbatim — this
    function NEVER recomputes another lens (the full-grid ``update()`` path would
    silently replace e.g. a 52w-ceiling-capped ``atr_bracket_1p5`` stamp with an
    uncapped recompute). Returns a NEW frame plus the count of rows whose map
    actually CHANGED, so a rerun is a countable no-op.
    """
    out = df.copy()
    values: list[Any] = out[_COLUMN].tolist() if _COLUMN in out.columns else []
    filled = 0
    for i in rows_missing_lens_key(out, lens_id):
        parsed = _parse_map(values[i])
        if parsed is None:  # pragma: no cover — candidates always parse
            continue
        value = compute(out.iloc[i])
        if value is UNRESOLVABLE:
            continue
        if lens_id in parsed and parsed[lens_id] == value:
            continue  # null -> null (or identical recovery): nothing changed
        merged = dict(parsed)
        merged[lens_id] = value
        values[i] = json.dumps(merged)
        filled += 1
    if _COLUMN in out.columns:
        out[_COLUMN] = values
    return out, filled


def entry_ttl_cutoff_ms(brief_date: dt.date, entry_ttl_days: int, exchange: str = "XNYS") -> int:
    """The row's production entry-TTL cutoff in epoch-ms, EXACTLY as the monitor
    derives it (``_engine_cutoffs(...)[5]``): the session-OPEN of the session
    ``entry_ttl_days`` TRADING days after the arrival session. ``entry_ttl_days``
    must come from the ROW's stamped ``entry_ttl_days`` (the TTL actually applied
    when it froze), never from today's default constant.
    """
    arrival = session_on_or_after(brief_date, exchange)
    expiry = advance_trading_sessions(arrival, int(entry_ttl_days), exchange)
    return int(session_open_utc(expiry, exchange).timestamp() * 1000)
