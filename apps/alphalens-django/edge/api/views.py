"""Read-only DRF views for the ``/v1/edge/*`` market-behavior edge dashboard.

* ``GET /v1/edge/summary?window=N`` — :class:`EdgeSummaryView`. The N-gated,
  benchmark-relative aggregate (memo §3). ``window`` (calendar days back from the
  latest brief date) bounds the population; default = all.
* ``GET /v1/edge/outcomes?window=N&status=terminal|ongoing`` —
  :class:`EdgeOutcomesView`. The per-candidate rows behind the aggregate.

Both are auth_cf-gated like the rest of the API (the project-wide
``IsAuthenticated`` default). TELEMETRY / EXPLORATORY only — no re-weight action,
no arbitrary slicing surface (memo §3.6: only the overall population is exposed
live; by-theme / by-gate slicing stays offline).
"""

from __future__ import annotations

import datetime as dt

from briefs.models import Brief
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from edge.api.serializers import EdgeOutcomeRowSerializer, EdgeSummarySerializer
from edge.api.summary import build_edge_summary
from edge.models import LadderOutcome

# Hard cap on window so a caller cannot ask for an unbounded scan. The monitor
# lookback is ~75 calendar days; 400 covers it with generous margin.
_MAX_WINDOW_DAYS = 400
# Per-page cap for the outcomes listing.
_OUTCOMES_LIMIT = 500

_LADDER_FIELD_NAMES = [f.name for f in LadderOutcome._meta.get_fields() if hasattr(f, "attname")]


def _parse_window(request: Request) -> int | None:
    """Parse the ``window`` query param (calendar days back). None = all dates."""
    raw = request.query_params.get("window")
    if raw is None or raw == "":
        return None
    try:
        n = int(raw)
    except ValueError:
        return None
    if n <= 0:
        return None
    return min(n, _MAX_WINDOW_DAYS)


def _window_floor(window: int | None) -> dt.date | None:
    """The earliest brief_date to include for ``window`` days back from latest.

    Anchored to the latest brief_date in the cache (NOT today) so the window is
    stable regardless of when the API is hit relative to the nightly rebuild.
    """
    if window is None:
        return None
    latest = (
        LadderOutcome.objects.order_by("-brief_date").values_list("brief_date", flat=True).first()
    )
    if latest is None:
        return None
    return latest - dt.timedelta(days=window)


class EdgeSummaryView(APIView):
    """``/v1/edge/summary`` — the N-gated, benchmark-relative aggregate."""

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "window",
                OpenApiTypes.INT,
                description="Calendar days back from the latest brief date (default: all).",
            ),
        ],
        responses=EdgeSummarySerializer,
    )
    def get(self, request: Request) -> Response:
        window = _parse_window(request)
        qs = LadderOutcome.objects.all()
        floor = _window_floor(window)
        if floor is not None:
            qs = qs.filter(brief_date__gte=floor)

        rows = list(qs.values(*_LADDER_FIELD_NAMES))
        payload = build_edge_summary(rows)
        return Response(EdgeSummarySerializer(payload).data)


class EdgeOutcomesView(APIView):
    """``/v1/edge/outcomes`` — per-candidate rows (theme joined from the brief)."""

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "window",
                OpenApiTypes.INT,
                description="Calendar days back from the latest brief date (default: all).",
            ),
            OpenApiParameter(
                "status",
                OpenApiTypes.STR,
                description="Filter: 'terminal' or 'ongoing' (default: all plannable).",
            ),
        ],
        responses=EdgeOutcomeRowSerializer(many=True),
    )
    def get(self, request: Request) -> Response:
        window = _parse_window(request)
        qs = LadderOutcome.objects.filter(plannable=True)
        floor = _window_floor(window)
        if floor is not None:
            qs = qs.filter(brief_date__gte=floor)

        status_filter = (request.query_params.get("status") or "").lower()
        if status_filter == "terminal":
            qs = qs.filter(terminal=True)
        elif status_filter == "ongoing":
            qs = qs.filter(terminal=False)

        qs = qs.order_by("-brief_date", "ticker")[:_OUTCOMES_LIMIT]
        outcomes = list(qs)

        themes = self._theme_lookup(outcomes)
        rows = [
            {
                "ticker": o.ticker,
                "brief_date": o.brief_date,
                "theme": themes.get((o.brief_date, o.ticker)),
                "ladder_classification": o.ladder_classification,
                "terminal": o.terminal,
                "realized_r": o.realized_r,
                "open_r": o.open_r,
                "market_excess_return": o.market_excess_return,
                "forward_return": o.forward_return,
                "benchmark_window_return": o.benchmark_window_return,
                "holding_days_elapsed": o.holding_days_elapsed,
                "realized_return_pct_of_book": o.realized_return_pct_of_book,
            }
            for o in outcomes
        ]
        return Response({"data": EdgeOutcomeRowSerializer(rows, many=True).data})

    @staticmethod
    def _theme_lookup(
        outcomes: list[LadderOutcome],
    ) -> dict[tuple[dt.date, str], str]:
        """Join ``theme`` from the Brief cache by (date, ticker).

        The population-ladder parquet has no ``theme`` column — it is recoverable
        from the brief for the same (brief_date, ticker). A missing brief leaves
        the theme None (older / uncached date); never 500s.
        """
        if not outcomes:
            return {}
        dates = {o.brief_date for o in outcomes}
        tickers = {o.ticker for o in outcomes}
        brief_rows = Brief.objects.filter(date__in=dates, ticker__in=tickers).values(
            "date", "ticker", "theme"
        )
        return {(b["date"], b["ticker"]): b["theme"] for b in brief_rows}


__all__ = ["EdgeOutcomesView", "EdgeSummaryView"]
