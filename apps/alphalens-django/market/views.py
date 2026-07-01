"""``/v1/market/status`` — XNYS-state projection for the SPA session chip.

One GET endpoint, no database. Reads ``market.calendar`` (a thin wrapper
around the ``exchange_calendars`` library) to answer "does the venue trade
today, is it a half-day, is it open right now, when is the next open/close?"
— the data the SvelteKit per-exchange session chip needs.

``?as_of=YYYY-MM-DD`` pins the anchor for the day-level fields in
deterministic tests + operator debugging. Without it those fields default
to ``datetime.now(UTC).date()``; the intraday fields always use the real
current instant. The SPA never passes ``as_of``.
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
    """Read-only XNYS state snapshot. Ambient market-session telemetry for
    the SPA's per-exchange session chip — answers "is the venue open right
    now, when does it next open/close" so a reader can tell whether the
    prices in a brief are live or anchored to the last close. No order flow
    is involved (the paper-trade/broker chain was decommissioned, ADR 0012)."""

    @extend_schema(
        operation_id="market_status_retrieve",
        summary="Current market-status snapshot",
        description=(
            "Returns whether the requested venue (defaulting to XNYS) holds "
            "a session on the anchor date, whether that session is a "
            "half-day, whether the venue is in a regular session right now, "
            "and the UTC ISO 8601 timestamps of the next session open and "
            "next session close. The day-level fields (``is_trading_day``, "
            "``is_half_day``) are anchored on UTC today unless "
            "``?as_of=YYYY-MM-DD`` is supplied; the intraday fields "
            "(``is_open_now``, ``next_open_iso``, ``next_close_iso``) always "
            "reflect the current wall-clock instant, so a pre-open trading "
            "day reports today's open rather than the following session."
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
                    "is_open_now": serializers.BooleanField(),
                    "next_open_iso": serializers.DateTimeField(),
                    "next_close_iso": serializers.DateTimeField(),
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

        # Intraday fields reflect the real current instant regardless of
        # ``as_of`` — "is the venue open right now / when does it next
        # open/close" is a wall-clock question, while the day-level fields
        # above answer "what about the anchor date". ``next_open`` belongs
        # here (not with the day-level pair): keying it off ``now`` makes the
        # pre-open window on a trading day point at *today's* open, where a
        # day-anchored lookup wrongly skipped to the following session. The
        # SPA reads ``next_open_iso`` only while ``is_open_now`` is false and
        # ``next_close_iso`` only while it is true.
        now = dt.datetime.now(dt.UTC)
        is_open_now = market_calendar.is_session_open_at(now, exchange=exchange)
        next_open = market_calendar.next_session_open_utc(now, exchange=exchange)
        next_close = market_calendar.next_session_close_utc(now, exchange=exchange)

        # ``isoformat`` on a tz-aware datetime emits ``+00:00`` rather
        # than ``Z``; the SPA's ``new Date(...)`` parses both, but the
        # explicit offset reads more obviously to a human inspecting the
        # response.
        return Response(
            {
                "is_trading_day": is_trading,
                "is_half_day": is_half,
                "is_open_now": is_open_now,
                "next_open_iso": next_open.isoformat(),
                "next_close_iso": next_close.isoformat(),
                "exchange": exchange,
            }
        )
