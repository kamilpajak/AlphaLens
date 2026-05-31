"""DRF serializers for the feedback endpoints.

These deliberately stop at type coercion (date/datetime parsing,
field-level type validation). Cross-field invariants (action enum,
dismiss pair-integrity, position-size only for live_traded) are
authoritative in ``alphalens_pipeline.feedback.store.Decision`` and
re-raised as DRF ``ValidationError`` by the view so the rules live in
one place and the SPA + Telegram bot eventually share the same error
surface.
"""

from __future__ import annotations

from rest_framework import serializers


class DecisionRequestSerializer(serializers.Serializer):
    """Body schema for POST /v1/feedback/decisions. All optional fields
    default to None so the pipeline-side Decision dataclass enforces the
    rules (e.g. dismiss_category required when action=dismissed)."""

    brief_date = serializers.DateField()
    ticker = serializers.CharField(max_length=10)
    theme = serializers.CharField(max_length=200)
    surfaced_at = serializers.DateTimeField()
    action = serializers.CharField(max_length=20)
    action_at = serializers.DateTimeField(required=False, allow_null=True)
    dismiss_category = serializers.CharField(required=False, allow_null=True, max_length=30)
    dismiss_reason = serializers.CharField(required=False, allow_null=True, max_length=30)
    # max_length mirrors the SPA's <input maxlength="200"> so an oversized
    # note is rejected server-side instead of silently persisted (zen
    # pre-merge: SPA/server cap symmetry).
    dismiss_note = serializers.CharField(
        required=False, allow_null=True, allow_blank=True, max_length=200
    )
    confidence_subjective = serializers.IntegerField(required=False, allow_null=True)
    paper_trade_plan_id = serializers.CharField(required=False, allow_null=True, max_length=64)
    position_size_usd = serializers.FloatField(required=False, allow_null=True)
    entry_price = serializers.FloatField(required=False, allow_null=True)


class DecisionResponseSerializer(serializers.Serializer):
    """Response shape for a persisted decision row.

    Mirrors ``views._serialise_decision`` output so drf-spectacular can
    generate accurate OpenAPI schemas for the SPA + downstream clients.
    """

    id = serializers.CharField()
    brief_date = serializers.DateField()
    ticker = serializers.CharField()
    theme = serializers.CharField()
    surfaced_at = serializers.DateTimeField()
    action = serializers.CharField()
    action_at = serializers.DateTimeField()
    dismiss_category = serializers.CharField(allow_null=True)
    dismiss_reason = serializers.CharField(allow_null=True)
    dismiss_note = serializers.CharField(allow_null=True)
    confidence_subjective = serializers.IntegerField(allow_null=True)
    paper_trade_plan_id = serializers.CharField(allow_null=True)
    position_size_usd = serializers.FloatField(allow_null=True)
    entry_price = serializers.FloatField(allow_null=True)
    market_regime_at_entry = serializers.CharField(allow_null=True)


class DecisionListResponseSerializer(serializers.Serializer):
    """Envelope for GET /v1/feedback/decisions."""

    data = DecisionResponseSerializer(many=True)


class TaxonomyResponseSerializer(serializers.Serializer):
    """Shape of GET /v1/feedback/taxonomy — exposed to SPA dropdowns."""

    actions = serializers.ListField(child=serializers.CharField())
    categories = serializers.DictField(child=serializers.ListField(child=serializers.CharField()))
