"""Envelope-shaped pagination — matches the legacy ``{data, meta}`` contract.

DRF's default ``LimitOffsetPagination`` emits ``{count, next, previous, results}``.
The frontend (and existing OpenAPI consumers) expect
``{data: [...], meta: {total, limit, offset}}``, so we override the response
shape while keeping the underlying paging behaviour.
"""

from __future__ import annotations

from rest_framework.pagination import LimitOffsetPagination
from rest_framework.response import Response

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


class EnvelopePagination(LimitOffsetPagination):
    default_limit = DEFAULT_LIMIT
    max_limit = MAX_LIMIT

    def get_paginated_response(self, data) -> Response:
        return Response(
            {
                "data": data,
                "meta": {
                    "total": self.count,
                    "limit": self.limit,
                    "offset": self.offset,
                },
            }
        )

    def get_paginated_response_schema(self, schema):
        """drf-spectacular consumes this to render the OpenAPI list shape."""
        return {
            "type": "object",
            "required": ["data", "meta"],
            "properties": {
                "data": schema,
                "meta": {
                    "type": "object",
                    "required": ["total", "limit", "offset"],
                    "properties": {
                        "total": {"type": "integer", "example": 123},
                        "limit": {"type": "integer", "example": DEFAULT_LIMIT},
                        "offset": {"type": "integer", "example": 0},
                    },
                },
            },
        }


def envelope(data: list, *, total: int, limit: int, offset: int) -> dict:
    """For aggregate endpoints that paginate manually (themes, ticker history)."""
    return {"data": data, "meta": {"total": total, "limit": limit, "offset": offset}}
