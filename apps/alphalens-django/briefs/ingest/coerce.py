"""Parquet cell → Python native value coercion.

Pure functions; no Django imports — so tests can exercise them in isolation
without ``django.setup()``. Each helper returns ``None`` for missing /
NaN-shaped input rather than letting NaN leak into the ORM (Django would
serialize ``float('nan')`` as the string ``"NaN"`` in JSONField, which then
fails to round-trip).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pandas as pd


def is_missing(value: Any) -> bool:
    """True for None, NaN, NaT scalars from parquet.

    Uses ``pd.isna`` which handles all of None / float NaN / pandas NaT in one
    call. ``math.isnan`` would reject NaT (not a float); the ``x != x`` idiom
    also works but trips Sonar S1764, so route everything through pandas.
    """
    if value is None:
        return True
    try:
        result = pd.isna(value)
    except (TypeError, ValueError):
        return False
    # pd.isna returns a bool for scalars and an ndarray for array-likes; we
    # only treat scalar NaN/NaT as missing.
    return bool(result) if isinstance(result, bool) else False


def coerce_str(value: Any) -> str | None:
    if is_missing(value):
        return None
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return str(value)


def coerce_float(value: Any) -> float | None:
    if is_missing(value):
        return None
    return float(value)


def coerce_int(value: Any) -> int | None:
    if is_missing(value):
        return None
    return int(value)


def coerce_bool(value: Any) -> bool:
    if is_missing(value):
        return False
    return bool(value)


def coerce_list_str(value: Any) -> list[str]:
    """numpy arrays, pandas object cells, scalar strings, lists — all → list[str]."""
    if is_missing(value):
        return []
    if hasattr(value, "tolist"):
        value = value.tolist()
    if isinstance(value, str):
        return [value]
    try:
        return [str(v) for v in value]
    except TypeError:
        return [str(value)]


def coerce_date(value: Any) -> dt.date | None:
    """ISO date string or pandas Timestamp → ``date``; bad input → None."""
    if is_missing(value):
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if hasattr(value, "to_pydatetime"):
        return value.to_pydatetime().date()
    text = str(value).strip()
    if not text:
        return None
    try:
        return dt.date.fromisoformat(text[:10])
    except ValueError:
        return None


def coerce_datetime(value: Any) -> dt.datetime | None:
    """ISO datetime string or pandas Timestamp → tz-aware datetime; bad → None."""
    if is_missing(value):
        return None
    if isinstance(value, dt.datetime):
        return _ensure_tz(value)
    if hasattr(value, "to_pydatetime"):
        return _ensure_tz(value.to_pydatetime())
    text = str(value).strip()
    if not text:
        return None
    try:
        return _ensure_tz(dt.datetime.fromisoformat(text))
    except ValueError:
        return None


def _ensure_tz(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value
