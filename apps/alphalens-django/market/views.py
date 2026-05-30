"""``/v1/market/status`` — XNYS-state projection for the SPA banner.

One GET endpoint, no database. Reads ``market.calendar`` (a thin wrapper
around the ``exchange_calendars`` library) to answer "is the market open
right now, is it a half-day, when is the next open?" — the four data
points the SvelteKit ``MarketStatusBanner`` needs to render its
closed-market countdown.

``?as_of=YYYY-MM-DD`` pins the anchor for deterministic tests + operator
debugging. Without it the view defaults to ``datetime.now(UTC).date()``;
the SPA never passes ``as_of``.

Design memo: ``docs/research/paper_trading_non_trading_day_2026_05_29.md``
§5 (PR-C sequencing).
"""

from __future__ import annotations

import datetime as dt

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from market import calendar as market_calendar


def _parse_as_of(raw: str) -> dt.date:
    """Strict ``YYYY-MM-DD`` parser.

    ``dt.date.fromisoformat`` accepts plain ``YYYY-MM-DD`` strings
    on Python 3.11+; longer ISO forms (``YYYY-MM-DDTHH:MM:SS``) round-trip
    via ``datetime.fromisoformat`` instead. We deliberately reject the
    latter — the calendar lookup is date-granular, so accepting time
    components would silently drop information the operator might assume
    was honoured (e.g. "is XNYS open at 09:31 ET?" would still answer
    based on the date only).
    """
    if len(raw) != 10 or raw[4] != "-" or raw[7] != "-":
        raise ValueError("expected YYYY-MM-DD")
    return dt.date.fromisoformat(raw)


class MarketStatusView(APIView):
    """Read-only XNYS state snapshot. Read CLAUDE.md "Layer 1" — this is
    decision-support telemetry for the paper-trade submitter, exposed
    to the SPA so closed-market days surface a banner instead of users
    refreshing wondering why no orders are flowing."""

    @extend_schema(
        operation_id="market_status_retrieve",
        summary="Current market-status snapshot",
        description=(
            "Returns whether the requested venue (defaulting to XNYS) is "
            "currently in a regular session, whether today's session is a "
            "half-day, and the UTC ISO 8601 timestamp of the next session "
            "open. Anchored on UTC today unless ``?as_of=YYYY-MM-DD`` is "
            "supplied."
        ),
        parameters=[
            OpenApiParameter(
                name="as_of",
                type=OpenApiTypes.DATE,
                location=OpenApiParameter.QUERY,
                required=False,
                description=(
                    "ISO 8601 date (YYYY-MM-DD) to anchor the response on. "
                    "Defaults to today (UTC). Date-only — time components "
                    "are rejected with 400."
                ),
            ),
        ],
        responses={
            200: inline_serializer(
                name="MarketStatus",
                fields={
                    "is_trading_day": serializers.BooleanField(),
                    "is_half_day": serializers.BooleanField(),
                    "next_open_iso": serializers.DateTimeField(),
                    "exchange": serializers.CharField(),
                },
            ),
            400: OpenApiTypes.OBJECT,
        },
    )
    def get(self, request: Request) -> Response:
        raw_as_of = request.query_params.get("as_of")
        if raw_as_of is not None:
            try:
                anchor = _parse_as_of(raw_as_of)
            except ValueError:
                return Response(
                    {"detail": "as_of must be YYYY-MM-DD"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        else:
            anchor = dt.datetime.now(dt.UTC).date()

        exchange = market_calendar.DEFAULT_EXCHANGE
        is_trading = market_calendar.is_trading_day(anchor, exchange=exchange)
        is_half = market_calendar.is_half_day(anchor, exchange=exchange)
        next_open = market_calendar.next_trading_open_utc(anchor, exchange=exchange)

        # ``isoformat`` on a tz-aware datetime emits ``+00:00`` rather
        # than ``Z``; the SPA's ``new Date(...)`` parses both, but the
        # explicit offset reads more obviously to a human inspecting the
        # response.
        return Response(
            {
                "is_trading_day": is_trading,
                "is_half_day": is_half,
                "next_open_iso": next_open.isoformat(),
                "exchange": exchange,
            }
        )
