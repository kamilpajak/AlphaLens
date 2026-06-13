"""Parquet cell → Python native value coercion.

Pure functions; no Django imports — so tests can exercise them in isolation
without ``django.setup()``. Each helper returns ``None`` for missing /
NaN-shaped input rather than letting NaN leak into the ORM (Django would
serialize ``float('nan')`` as the string ``"NaN"`` in JSONField, which then
fails to round-trip).
"""

from __future__ import annotations

import datetime as dt
import json
import math
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


def coerce_finite_float(value: Any) -> float | None:
    """Float coercion that also rejects ±inf, not just NaN/NaT.

    ``pd.isna`` does NOT catch ``np.inf``, so :func:`coerce_float` would let
    ``float('inf')`` through and Django's JSONField would emit the bare
    ``Infinity`` token (invalid JSON, the same leak class as ``NaN``). Used by the
    ``expert_assessments`` blob assembler so the persisted JSON can never hold a
    non-finite float. Kept separate from :func:`coerce_float` so the flat
    ``FloatField`` path (which expects raw passthrough) is unchanged.
    """
    out = coerce_float(value)
    if out is None or not math.isfinite(out):
        return None
    return out


def coerce_int(value: Any) -> int | None:
    if is_missing(value):
        return None
    return int(value)


def coerce_bool(value: Any) -> bool:
    if is_missing(value):
        return False
    return bool(value)


def coerce_optional_bool(value: Any) -> bool | None:
    """Nullable boolean: ``None`` for missing input (preserves the tri-state).

    Unlike :func:`coerce_bool` (which floors missing -> ``False``), this keeps
    ``None`` distinct from ``False`` — needed for a nullable ``BooleanField``
    such as ``buffett_understandable``, where "not assessed" (``None``) must not
    read as "not understandable" (``False``).
    """
    if is_missing(value):
        return None
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


def coerce_json_obj(value: Any) -> dict | None:
    """Object-shaped JSONField cell → ``dict`` (or ``None``).

    Accepts a dict (passthrough) or a JSON string (the pipeline persists
    ``brief_trade_setup`` via ``json.dumps``); missing / unparseable / non-object
    input → ``None``. Distinct from :func:`coerce_list_str`, which is for
    list-of-strings JSONFields (``also_in_themes``, ``gates_*``).
    """
    if is_missing(value):
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


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


# Per-column role classification for the expert_assessments blob assembler. Bool
# columns keep the None/True/False tri-state; float columns are non-finite-scrubbed
# (NaN/NaT/±inf -> None); everything else (enums / free text / datetime string /
# config_version) goes through coerce_str.
_EXPERT_BOOL_COLUMNS = frozenset(
    {
        "buffett_understandable",
        "buffett_used_scuttlebutt",
        # O'Neil audit flags (PR-8a): tri-state bool-as-float (0.0/1.0/NaN). MUST
        # stay here, not in _EXPERT_FLOAT_COLUMNS — coerce_optional_bool restores
        # None/True/False so a FALSE flag (0.0 -> False) does not fire the SPA badge
        # and an absent flag stays None. The else-branch coerce_str would persist
        # the JS-truthy strings "0.0"/"1.0" and break the strict ``=== true`` render.
        "oneil_new_high_split_suspected",
        "oneil_earnings_growth_near_zero_base",
    }
)
_EXPERT_FLOAT_COLUMNS = frozenset(
    {
        "buffett_owner_earnings_yield_pct",
        "buffett_roic_latest",
        "buffett_roic_3y_avg",
        "buffett_margin_of_safety_pct",
        "buffett_data_coverage",
        "buffett_quality_score",
        # O'Neil numeric readouts (PR-8a) — 6 floats (None -> JSON null).
        "oneil_pct_off_52w_high",
        "oneil_ma200_slope_pct_per_day",
        "oneil_ma200_distance_pct",
        "oneil_earnings_growth_yoy_pct",
        "oneil_data_coverage",
        "oneil_score",
        # Panel disagreement scalar (PR-8a) — finite-scrubbed; panel_config_version
        # (the sibling column) falls through to coerce_str.
        "expert_spread",
    }
)


def coerce_expert_blob(row: pd.Series, column_names: tuple[str, ...]) -> dict[str, Any] | None:
    """Assemble ONE expert's dict from a parquet row, non-finite-scrubbed + tri-state safe.

    Columns absent from ``row`` are skipped entirely (a sparse blob); a present
    column is coerced by role so the persisted JSON can never hold a ``NaN`` /
    ``NaT`` / ``Infinity`` token. Bool columns keep ``None`` distinct from
    ``False`` (tri-state); float columns are non-finite-scrubbed; everything else
    (enums / text / config_version) becomes ``str | None``. Returns the dict, or
    ``None`` when NO column from the set is present (so a row with no expert data
    yields a JSON null rather than ``{}``).

    INTENTIONAL: a row where the columns ARE present but every value resolves to
    null (a fully-unclassified name — in production the pipeline always stamps all
    columns, ``None`` where unavailable) yields a dict of all-null values, NOT
    ``None``. That is honest — the expert *was assembled* and found nothing,
    distinct from the expert being absent — and consumers treat an all-null
    assessment the same as a missing one. Only a parquet with NO expert column at
    all (pre-expert-era) produces a ``None`` blob.
    """
    out: dict[str, Any] = {}
    for col in column_names:
        if col not in row.index:
            continue
        raw = row[col]
        if col in _EXPERT_BOOL_COLUMNS:
            out[col] = coerce_optional_bool(raw)
        elif col in _EXPERT_FLOAT_COLUMNS:
            out[col] = coerce_finite_float(raw)
        else:
            out[col] = coerce_str(raw)
    return out or None
