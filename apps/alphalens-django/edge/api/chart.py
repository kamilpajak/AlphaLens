"""Read-only DRF view for ``/v1/edge/chart/<brief_date>/<ticker>`` (PR-1).

Serves the PRE-COMPUTED ladder-chart payload (daily OHLC candles + entry/TP/stop
price lines + modeled fill/exit markers) for one ``(brief_date, ticker)`` so the
SvelteKit ``/edge`` page can draw a candlestick chart of the broker-free ladder
replay.

SLIM-IMAGE BOUNDARY (sacred): this module imports NOTHING from
``alphalens_pipeline``. The heavy compute (minute bars -> daily candles + marker
timestamping) is done pipeline-side by
``alphalens_pipeline.feedback.ladder_chart`` and persisted as the
``chart_payload_json`` string column on the ``LadderOutcome`` row. This view only
reads, parses, and serves that string — never recomputes.

Auth: inherits the project-wide ``IsAuthenticated`` default (no explicit
``permission_classes``), like the rest of ``/v1/edge/*``.
"""

from __future__ import annotations

import datetime as dt
import json
import logging

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from edge.api.serializers import ChartResponseSerializer
from edge.models import LadderOutcome

logger = logging.getLogger(__name__)

# The intrabar tie-break rule the replay applies (mirrors
# ``alphalens_pipeline.feedback.ladder_replay.TIE_BREAK_SL_FIRST``). Duplicated as
# a literal here because the slim image cannot import the pipeline; it is the
# fallback for the empty/NO_DATA payload so the response shape stays stable.
_INTRABAR_RULE = "sl_first"


def _date_from_path(raw: str | None) -> dt.date:
    """Parse a ``brief_date`` path segment as an ISO date or raise 404.

    Mirrors ``briefs.api.views._date_from_path``: a non-ISO segment in the URL
    means "no such resource" (404), not "bad request syntax" (400).
    """
    try:
        return dt.date.fromisoformat(str(raw))
    except (TypeError, ValueError) as exc:
        raise NotFound(f"no chart for date={raw}") from exc


def _empty_payload() -> dict:
    """The graceful NO_DATA payload for a row with a blank/unparseable column.

    Keeps the response shape identical to a real payload so the SPA can build one
    static type and branch on ``status`` (never a 500 on a missing projection).
    """
    return {
        "status": "NO_DATA",
        "bars": [],
        "price_lines": {"entry": None, "tp": [], "stop": None},
        "markers": [],
        "ambiguous_bars": 0,
        "intrabar_rule": _INTRABAR_RULE,
        "rth_only": True,
    }


def _parse_payload(raw: str | None) -> dict:
    """Parse the stored ``chart_payload_json`` string into a payload dict.

    A blank column (older row that predates the projection) or a non-dict / bad
    JSON value degrades to the NO_DATA payload rather than 500ing — the projection
    is best-effort and a missing one must read as "no chart", not an error.
    """
    if not raw:
        return _empty_payload()
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("edge-chart: unparseable chart_payload_json; serving NO_DATA")
        return _empty_payload()
    if not isinstance(parsed, dict):
        return _empty_payload()
    return parsed


class EdgeChartView(APIView):
    """``/v1/edge/chart/<brief_date>/<ticker>`` — the pre-computed chart payload."""

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "brief_date",
                OpenApiTypes.DATE,
                OpenApiParameter.PATH,
                description="Brief date (ISO YYYY-MM-DD).",
            ),
            OpenApiParameter(
                "ticker",
                OpenApiTypes.STR,
                OpenApiParameter.PATH,
                description="Candidate ticker (case-insensitive).",
            ),
        ],
        responses=ChartResponseSerializer,
    )
    def get(self, request: Request, brief_date: str, ticker: str) -> Response:
        parsed_date = _date_from_path(brief_date)
        # CompositePrimaryKey(brief_date, ticker) -> filter().first(), not get().
        outcome = (
            LadderOutcome.objects.filter(brief_date=parsed_date, ticker=ticker.upper())
            .only("brief_date", "ticker", "ladder_classification", "chart_payload_json")
            .first()
        )
        if outcome is None:
            raise NotFound(f"no chart for {ticker.upper()} on {parsed_date.isoformat()}")

        payload = _parse_payload(outcome.chart_payload_json)
        body = {
            "brief_date": outcome.brief_date,
            "ticker": outcome.ticker,
            "ladder_classification": outcome.ladder_classification,
            "status": payload.get("status", "NO_DATA"),
            "bars": payload.get("bars", []),
            "price_lines": payload.get("price_lines", {"entry": None, "tp": [], "stop": None}),
            "markers": payload.get("markers", []),
            "ambiguous_bars": payload.get("ambiguous_bars", 0),
            "intrabar_rule": payload.get("intrabar_rule", _INTRABAR_RULE),
            "rth_only": payload.get("rth_only", True),
        }
        return Response(ChartResponseSerializer(body).data)


__all__ = ["EdgeChartView"]
