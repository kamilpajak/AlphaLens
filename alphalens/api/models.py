"""Pydantic v2 response models for the API.

The ``Candidate`` model is generated from ``schema.CANDIDATE_COLUMNS`` so the
SQL schema, the parquet column list, and the API response stay aligned through
a single registry. ``from_row`` decodes list[str] fields from their JSON-text
representation in SQLite and normalises bool/datetime fields.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, create_model

from alphalens.api.schema import CANDIDATE_COLUMNS, Column


def _field_for(col: Column) -> tuple[type, Any]:
    """Map a Column to a (annotation, default) tuple for create_model."""
    py_type: type
    if col.py_kind == "str":
        py_type = str
    elif col.py_kind == "float":
        py_type = float
    elif col.py_kind == "int":
        py_type = int
    elif col.py_kind == "bool":
        py_type = bool
    elif col.py_kind == "list_str":
        py_type = list[str]
    elif col.py_kind == "datetime":
        py_type = str  # ISO 8601 string at the API boundary
    else:
        raise ValueError(f"unknown column kind: {col.py_kind}")
    return (py_type | None, None)


_candidate_fields: dict[str, tuple[type, Any]] = {
    # ``date`` is the second half of the (date, ticker) primary key; surfacing
    # it as a top-level field lets cross-day endpoints (theme history, ticker
    # history) be self-describing without an envelope per row.
    "date": (str, ...),
}
for col in CANDIDATE_COLUMNS:
    _candidate_fields[col.name] = _field_for(col)

# Generated dynamically so adding a column in schema.py propagates here.
Candidate: type[BaseModel] = create_model(  # type: ignore[call-overload]
    "Candidate",
    __config__=ConfigDict(extra="forbid"),
    **_candidate_fields,
)
Candidate.__doc__ = "One ranked candidate from a daily thematic brief."


def _decode_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return [value]
        if isinstance(decoded, list):
            return [str(v) for v in decoded]
        return [str(decoded)]
    return [str(value)]


def candidate_from_row(row: sqlite3.Row | Mapping[str, Any]) -> BaseModel:
    """Build a Candidate from a SQLite row, decoding JSON list columns."""
    mapping: Mapping[str, Any] = dict(row) if isinstance(row, sqlite3.Row) else row
    data: dict[str, Any] = {"date": mapping["date"]}
    for col in CANDIDATE_COLUMNS:
        raw = mapping.get(col.name)
        if col.py_kind == "list_str":
            data[col.name] = _decode_list(raw)
        elif col.py_kind == "bool":
            data[col.name] = bool(raw) if raw is not None else None
        else:
            data[col.name] = raw
    return Candidate(**data)


class DayMeta(BaseModel):
    """One row in the ``/v1/days`` index."""

    model_config = ConfigDict(extra="forbid")

    date: str = Field(description="ISO 8601 date (YYYY-MM-DD).")
    n_candidates: int
    n_themes: int
    top_theme: str | None = None


class DayBrief(BaseModel):
    """Full payload for one day — metadata + every ranked candidate."""

    model_config = ConfigDict(extra="forbid")

    date: str
    n_candidates: int
    n_themes: int
    top_theme: str | None = None
    theme_counts: dict[str, int]
    candidates: list[Candidate]  # type: ignore[valid-type]


class ThemeSummary(BaseModel):
    """One row in the ``/v1/themes`` aggregate."""

    model_config = ConfigDict(extra="forbid")

    theme: str
    n_candidates: int
    n_days: int
    first_seen: str
    last_seen: str


class TopTheme(BaseModel):
    model_config = ConfigDict(extra="forbid")

    theme: str
    n_days: int
    n_candidates: int


class Stats(BaseModel):
    """Aggregate stats served at ``/v1/stats``."""

    model_config = ConfigDict(extra="forbid")

    n_days: int
    n_candidates: int
    n_themes: int
    earliest_date: str | None = None
    latest_date: str | None = None
    last_rebuild_at: str | None = None
    top_themes: list[TopTheme]


class PageMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int
    limit: int
    offset: int


class Paginated[T](BaseModel):
    """Envelope for every list endpoint."""

    model_config = ConfigDict(extra="forbid")

    data: list[T]
    meta: PageMeta


# Concrete aliases for OpenAPI clarity (FastAPI prefers a named type per route).
PaginatedDayMeta = Annotated[
    Paginated[DayMeta],
    Field(description="Page of day-level metadata."),
]
PaginatedCandidate = Annotated[
    Paginated[Candidate],  # type: ignore[valid-type]
    Field(description="Page of brief candidates."),
]
PaginatedThemeSummary = Annotated[
    Paginated[ThemeSummary],
    Field(description="Page of theme summaries."),
]


class Health(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = "ok"


class Ready(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    db_path: str
    last_rebuild_at: str | None = None
    n_days: int
    n_candidates: int
