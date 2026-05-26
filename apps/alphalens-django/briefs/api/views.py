"""Read-only DRF viewsets for the ``/v1/*`` API surface.

The viewset boundaries mirror URL prefixes:

* ``/v1/days``           — :class:`DayViewSet`
* ``/v1/themes``         — :class:`ThemeViewSet`
* ``/v1/candidates``     — :class:`CandidateViewSet`
* ``/v1/tickers``        — :class:`TickerViewSet`
* ``/v1/stats``          — :class:`StatsView`

Pagination is the envelope shape ``{data, meta}`` (see ``pagination.py``); list
endpoints inherit it automatically when they return a queryset. Aggregate
endpoints (themes, stats, ticker history) build the envelope explicitly via
``envelope(...)`` because they don't fit the ListModelMixin contract.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from django.db.models import Count, Max, Min, QuerySet
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from briefs.api.filters import get_paging, parse_iso_date
from briefs.api.pagination import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    EnvelopePagination,
    envelope,
)
from briefs.api.serializers import (
    CandidateSerializer,
    DayBriefSerializer,
    DayMetaSerializer,
    StatsSerializer,
    ThemeSummarySerializer,
)
from briefs.models import Brief, DayMeta

DATE_RANGE_PARAMS = [
    OpenApiParameter("from", OpenApiTypes.DATE, description="Inclusive ISO date lower bound."),
    OpenApiParameter("to", OpenApiTypes.DATE, description="Inclusive ISO date upper bound."),
]

# Path parameter declarations for drf-spectacular — without these the
# generator can't introspect the type of a custom ``lookup_field`` on a
# ``viewsets.ViewSet`` and falls back to "string" with a warning. Apply
# via ``@extend_schema_view`` on the class so every action picks them up.
DATE_PATH = OpenApiParameter(
    "date",
    OpenApiTypes.DATE,
    location=OpenApiParameter.PATH,
    description="ISO 8601 date (YYYY-MM-DD).",
)
THEME_PATH = OpenApiParameter(
    "theme",
    OpenApiTypes.STR,
    location=OpenApiParameter.PATH,
    description="Exact theme name.",
)
TICKER_PATH = OpenApiParameter(
    "ticker",
    OpenApiTypes.STR,
    location=OpenApiParameter.PATH,
    description="Upper-case ticker symbol.",
)

# Reject URL segments containing slashes or dots so drf-spectacular doesn't
# fall back to "string" type on custom ``lookup_field`` paths.
_PATH_SEGMENT_NO_DOT = r"[^/.]+"


def _date_from_path(pk: str | None, *, what: str) -> dt.date:
    """Parse a path PK as an ISO date or raise 404.

    Distinct from query-param parsing: a non-ISO segment in the URL means
    "no such resource" from the client's perspective (404), not "bad
    request syntax" (400). Query-param ``from``/``to`` keeps the 400 path
    because that's a real request-shape error.
    """
    try:
        parsed = parse_iso_date(pk, field_name="date")
    except ValidationError as exc:
        raise NotFound(f"no {what} for date={pk}") from exc
    if parsed is None:
        raise NotFound(f"no {what} for date={pk}")
    return parsed


def _apply_date_range(qs: QuerySet, request: Request, field: str = "date") -> QuerySet:
    d_from = parse_iso_date(request.query_params.get("from"), field_name="from")
    d_to = parse_iso_date(request.query_params.get("to"), field_name="to")
    if d_from is not None:
        qs = qs.filter(**{f"{field}__gte": d_from})
    if d_to is not None:
        qs = qs.filter(**{f"{field}__lte": d_to})
    return qs


@extend_schema_view(
    retrieve=extend_schema(parameters=[DATE_PATH]),
    candidates=extend_schema(parameters=[DATE_PATH]),
)
class DayViewSet(viewsets.ViewSet):
    """``/v1/days`` — index + per-day envelope + per-day candidates."""

    pagination_class = EnvelopePagination
    # OpenAPI parity with legacy FastAPI: path parameter is named ``date``,
    # not the DRF default ``id``.
    lookup_field = "date"
    lookup_url_kwarg = "date"
    lookup_value_regex = _PATH_SEGMENT_NO_DOT

    @extend_schema(parameters=DATE_RANGE_PARAMS, responses=DayMetaSerializer(many=True))
    def list(self, request: Request) -> Response:
        qs = _apply_date_range(DayMeta.objects.all(), request).order_by("-date")
        paginator = EnvelopePagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(DayMetaSerializer(page, many=True).data)

    @extend_schema(responses=DayBriefSerializer)
    def retrieve(self, request: Request, date: str | None = None) -> Response:
        asof = _date_from_path(date, what="brief")
        meta = DayMeta.objects.filter(date=asof).first()
        if meta is None:
            raise NotFound(f"no brief for date={asof.isoformat()}")

        candidates = Brief.objects.filter(date=asof).order_by("-layer4_weighted_score", "ticker")
        payload = {
            "date": meta.date,
            "n_candidates": meta.n_candidates,
            "n_themes": meta.n_themes,
            "top_theme": meta.top_theme or None,
            "theme_counts": meta.theme_counts,
            "candidates": CandidateSerializer(candidates, many=True).data,
        }
        return Response(DayBriefSerializer(payload).data)

    @extend_schema(
        parameters=[
            OpenApiParameter("theme", str, description="Exact theme match."),
            OpenApiParameter("min_score", int, description="Layer-4 score floor."),
        ],
        responses=CandidateSerializer(many=True),
    )
    @action(detail=True, methods=["get"], url_path="candidates")
    def candidates(self, request: Request, date: str | None = None) -> Response:
        asof = _date_from_path(date, what="brief")
        if not DayMeta.objects.filter(date=asof).exists():
            raise NotFound(f"no brief for date={asof.isoformat()}")

        qs = Brief.objects.filter(date=asof)
        theme = request.query_params.get("theme")
        if theme:
            qs = qs.filter(theme=theme)
        min_score = request.query_params.get("min_score")
        if min_score is not None:
            try:
                qs = qs.filter(layer4_weighted_score__gte=int(min_score))
            except ValueError as exc:
                raise NotFound(f"invalid min_score: {min_score!r}") from exc
        qs = qs.order_by("-layer4_weighted_score", "ticker")

        paginator = EnvelopePagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(CandidateSerializer(page, many=True).data)


@extend_schema_view(candidates=extend_schema(parameters=[THEME_PATH]))
class ThemeViewSet(viewsets.ViewSet):
    """``/v1/themes`` — distinct-theme aggregation + per-theme candidates."""

    pagination_class = EnvelopePagination
    lookup_field = "theme"
    lookup_url_kwarg = "theme"
    lookup_value_regex = _PATH_SEGMENT_NO_DOT

    @extend_schema(parameters=DATE_RANGE_PARAMS, responses=ThemeSummarySerializer(many=True))
    def list(self, request: Request) -> Response:
        qs = _apply_date_range(Brief.objects.all(), request)
        agg = (
            qs.values("theme")
            .annotate(
                n_candidates=Count("ticker"),
                n_days=Count("date", distinct=True),
                first_seen=Min("date"),
                last_seen=Max("date"),
            )
            .order_by("-n_candidates", "theme")
        )

        limit, offset = get_paging(request, default_limit=DEFAULT_LIMIT, max_limit=MAX_LIMIT)
        total = agg.count()
        page = list(agg[offset : offset + limit])
        return Response(
            envelope(
                ThemeSummarySerializer(page, many=True).data,
                total=total,
                limit=limit,
                offset=offset,
            )
        )

    @extend_schema(parameters=DATE_RANGE_PARAMS, responses=CandidateSerializer(many=True))
    @action(detail=True, methods=["get"], url_path="candidates")
    def candidates(self, request: Request, theme: str | None = None) -> Response:
        qs = _apply_date_range(Brief.objects.filter(theme=theme), request)
        qs = qs.order_by("-date", "-layer4_weighted_score", "ticker")

        paginator = EnvelopePagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(CandidateSerializer(page, many=True).data)


class CandidateViewSet(viewsets.ViewSet):
    """``/v1/candidates/{date}/{ticker}`` — single candidate fetch.

    The detail PK is supplied as a compound URL segment, not a single field.
    The router pattern in ``urls.py`` is custom for this reason.
    """

    @extend_schema(responses=CandidateSerializer)
    def retrieve_compound(self, request: Request, date: str, ticker: str) -> Response:
        parsed = _date_from_path(date, what="candidate")
        brief = Brief.objects.filter(date=parsed, ticker=ticker.upper()).first()
        if brief is None:
            raise NotFound(f"no candidate for date={parsed.isoformat()} ticker={ticker.upper()}")
        return Response(CandidateSerializer(brief).data)


@extend_schema_view(history=extend_schema(parameters=[TICKER_PATH]))
class TickerViewSet(viewsets.ViewSet):
    """``/v1/tickers/{ticker}/history`` — every brief appearance for one ticker."""

    pagination_class = EnvelopePagination
    lookup_field = "ticker"
    lookup_url_kwarg = "ticker"
    lookup_value_regex = _PATH_SEGMENT_NO_DOT

    @extend_schema(parameters=DATE_RANGE_PARAMS, responses=CandidateSerializer(many=True))
    @action(detail=True, methods=["get"], url_path="history")
    def history(self, request: Request, ticker: str | None = None) -> Response:
        ticker = (ticker or "").upper()
        qs = _apply_date_range(Brief.objects.filter(ticker=ticker), request)
        qs = qs.order_by("-date")

        paginator = EnvelopePagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(CandidateSerializer(page, many=True).data)


class StatsView(APIView):
    """``/v1/stats`` — top-line counters + N most-frequent themes."""

    @extend_schema(
        parameters=[OpenApiParameter("top_n", int, description="Top themes to include (1-50).")],
        responses=StatsSerializer,
    )
    def get(self, request: Request) -> Response:
        try:
            top_n = max(1, min(50, int(request.query_params.get("top_n", 10))))
        except ValueError:
            top_n = 10

        day_agg = DayMeta.objects.aggregate(
            n_days=Count("date"),
            earliest_date=Min("date"),
            latest_date=Max("date"),
            last_rebuild_at=Max("rebuilt_at"),
        )
        n_candidates = Brief.objects.count()
        n_themes = Brief.objects.values("theme").distinct().count()

        top_rows = list(
            Brief.objects.values("theme")
            .annotate(n_candidates=Count("ticker"), n_days=Count("date", distinct=True))
            .order_by("-n_candidates", "theme")[:top_n]
        )

        payload: dict[str, Any] = {
            "n_days": day_agg["n_days"] or 0,
            "n_candidates": n_candidates,
            "n_themes": n_themes,
            "earliest_date": day_agg["earliest_date"],
            "latest_date": day_agg["latest_date"],
            "last_rebuild_at": day_agg["last_rebuild_at"],
            "top_themes": top_rows,
        }
        return Response(StatsSerializer(payload).data, status=status.HTTP_200_OK)


__all__ = [
    "CandidateViewSet",
    "DayViewSet",
    "StatsView",
    "ThemeViewSet",
    "TickerViewSet",
]


# Silence unused-import warning — `dt` is exported for future range helpers.
_ = dt
