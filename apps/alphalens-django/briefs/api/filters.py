"""Query parameter parsers shared across viewsets."""

from __future__ import annotations

import datetime as dt

from rest_framework.exceptions import ValidationError


def parse_iso_date(value: str | None, field_name: str = "date") -> dt.date | None:
    """Parse ``YYYY-MM-DD`` or return None when absent.

    Rejects bad calendar values (``2026-02-30``) and bad shapes (``05/22/2026``).
    Raises DRF ``ValidationError`` so the framework converts it to a 400 with a
    consistent error envelope.
    """
    if value is None or value == "":
        return None
    try:
        return dt.date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            {field_name: f"must be a valid YYYY-MM-DD date, got {value!r}"}
        ) from exc


def parse_clamped_int(value: str | None, default: int, *, ge: int, le: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"expected an integer in [{ge}, {le}], got {value!r}") from exc
    return max(ge, min(le, parsed))


def get_paging(request, *, default_limit: int, max_limit: int) -> tuple[int, int]:
    """Read ``limit``/``offset`` query params with bounds enforcement."""
    limit = parse_clamped_int(
        request.query_params.get("limit"), default=default_limit, ge=1, le=max_limit
    )
    offset = parse_clamped_int(request.query_params.get("offset"), default=0, ge=0, le=10**9)
    return limit, offset
