"""DRF endpoints for the feedback ledger.

Three endpoints (`/v1/feedback/*`) backed by the pipeline-side
``FeedbackStore``:

- POST   /v1/feedback/decisions          — create or upsert a row
- GET    /v1/feedback/decisions          — list by brief_date
- DELETE /v1/feedback/decisions/<uuid>   — idempotent undo
- GET    /v1/feedback/taxonomy           — exposes locked enums for SPA dropdowns

The view layer is intentionally a thin dispatch wrapper. All
cross-field validation rules live in ``Decision.__post_init__``
(pipeline package) so the SPA, the Telegram bot (deferred), and any
future CLI all hit the same invariants.

VIX market regime stamping uses ``regime.classify_vix`` with a
``None`` input until a server-side VIX cache lands (deferred to v2 per
locked memo Q6). The row is still persisted; ``market_regime_at_entry``
stamps as ``"unknown"`` so we never block a decision on a missing
regime stamp.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from django.conf import settings
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from alphalens_pipeline.feedback import regime
from alphalens_pipeline.feedback.store import (
    ACTIONS,
    DISMISS_TAXONOMY,
    Decision,
    DecisionValidationError,
    FeedbackStore,
)

from .serializers import (
    DecisionListResponseSerializer,
    DecisionRequestSerializer,
    DecisionResponseSerializer,
    TaxonomyResponseSerializer,
)


def _feedback_db_path() -> Path:
    """Resolve the SQLite path from settings, fail loud on misconfiguration.

    See ``feedback_pathhome_in_container_trap_2026_05_28`` — container
    HOME ≠ host HOME, so the path MUST be settable via env in prod. Dev
    falls back to ``~/.alphalens/feedback.db``.
    """
    return Path(settings.ALPHALENS_FEEDBACK_DB)


def _serialise_decision(d: Decision) -> dict:
    """Round-trip a Decision to a JSON-safe dict."""
    return {
        "id": d.id,
        "brief_date": d.brief_date.isoformat(),
        "ticker": d.ticker,
        "theme": d.theme,
        "surfaced_at": d.surfaced_at.isoformat(),
        "action": d.action,
        "action_at": d.action_at.isoformat(),
        "dismiss_category": d.dismiss_category,
        "dismiss_reason": d.dismiss_reason,
        "dismiss_note": d.dismiss_note,
        "confidence_subjective": d.confidence_subjective,
        "paper_trade_plan_id": d.paper_trade_plan_id,
        "position_size_usd": d.position_size_usd,
        "entry_price": d.entry_price,
        "market_regime_at_entry": d.market_regime_at_entry,
    }


class DecisionsView(APIView):
    """POST (create/upsert) + GET (list by brief_date) collection endpoint."""

    @extend_schema(request=DecisionRequestSerializer, responses=DecisionResponseSerializer)
    def post(self, request: Request) -> Response:
        serializer = DecisionRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # `unknown` until the server-side VIX cache lands (memo Q6 / v2).
        # Better to lose the regime stamp than block a user decision on
        # a network blip in the hot path.
        regime_label = regime.classify_vix(None)

        try:
            decision = Decision(
                brief_date=data["brief_date"],
                ticker=data["ticker"].upper(),
                theme=data["theme"],
                surfaced_at=data["surfaced_at"],
                action=data["action"],
                action_at=data.get("action_at") or dt.datetime.now(dt.UTC),
                dismiss_category=data.get("dismiss_category"),
                dismiss_reason=data.get("dismiss_reason"),
                dismiss_note=data.get("dismiss_note"),
                confidence_subjective=data.get("confidence_subjective"),
                paper_trade_plan_id=data.get("paper_trade_plan_id"),
                position_size_usd=data.get("position_size_usd"),
                entry_price=data.get("entry_price"),
                market_regime_at_entry=regime_label,
            )
        except DecisionValidationError as exc:
            raise ValidationError({"detail": str(exc)}) from exc

        with FeedbackStore.open(_feedback_db_path()) as fb:
            row_id, was_created = fb.insert(decision)
            stored = fb.get(row_id)

        # `stored` is non-None — we just inserted it; assertion keeps
        # pyright happy without leaking the case into the response.
        assert stored is not None  # noqa: S101 — type narrowing
        # 201 only on first creation; 200 on upsert update (zen pre-merge
        # finding #5). The SPA undo flow uses ``stored.id`` either way,
        # so the status code is purely a REST hint to API consumers.
        http_status = status.HTTP_201_CREATED if was_created else status.HTTP_200_OK
        return Response(_serialise_decision(stored), status=http_status)

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "brief_date",
                str,
                required=True,
                description="YYYY-MM-DD — brief date to filter by.",
            ),
        ],
        responses=DecisionListResponseSerializer,
    )
    def get(self, request: Request) -> Response:
        brief_date_str = request.query_params.get("brief_date")
        if not brief_date_str:
            raise ValidationError({"detail": "brief_date query parameter is required"})
        try:
            brief_date = dt.date.fromisoformat(brief_date_str)
        except ValueError as exc:
            raise ValidationError(
                {"detail": f"brief_date must be YYYY-MM-DD ({brief_date_str!r} did not parse)"}
            ) from exc

        with FeedbackStore.open(_feedback_db_path()) as fb:
            rows = fb.list_by_brief_date(brief_date)

        return Response({"data": [_serialise_decision(r) for r in rows]})


class DecisionDetailView(APIView):
    """DELETE single decision by uuid."""

    @extend_schema(responses={204: None})
    def delete(self, request: Request, decision_id: str) -> Response:
        # Idempotent: no 404 on unknown id. The SPA undo button may fire
        # twice across a slow network; we don't want the second click
        # to surface a confusing 404 toast to the user.
        with FeedbackStore.open(_feedback_db_path()) as fb:
            fb.delete(decision_id)
        return Response(status=status.HTTP_204_NO_CONTENT)


class TaxonomyView(APIView):
    """Static endpoint exposing the locked dismiss taxonomy + action enum.

    The SPA fetches this once on app boot to build the dropdowns
    without hard-coding the labels client-side. Pipeline + Django + SPA
    therefore share one source of truth (the taxonomy constant in
    ``alphalens_pipeline.feedback.store``).
    """

    @extend_schema(responses=TaxonomyResponseSerializer)
    def get(self, request: Request) -> Response:
        return Response(
            {
                "actions": list(ACTIONS),
                "categories": {k: list(v) for k, v in DISMISS_TAXONOMY.items()},
            }
        )
