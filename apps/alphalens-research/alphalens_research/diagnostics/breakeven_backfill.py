"""Pure logic to backfill the break-even what-if column onto historical rows.

The population monitor stamps ``breakeven_realized_r_json`` only on a fresh minute
resolve, so rows it froze BEFORE the column existed (already-terminal decisions)
carry no value. This module fills those gaps from the retained bars WITHOUT ever
overwriting a value the monitor already stamped. It is display-only (the headline
``realized_r`` is never touched). Thin I/O wrapper: ``scripts/backfill_breakeven_whatif.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pandas as pd

_COLUMN = "breakeven_realized_r_json"


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
