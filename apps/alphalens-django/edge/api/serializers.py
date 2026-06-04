"""DRF serializers for the ``/v1/edge/*`` read-only API.

``LadderOutcomeSerializer`` is the ModelSerializer for the per-candidate rows.
The summary serializers shape the N-gated, benchmark-relative aggregate built by
``edge.api.summary.build_edge_summary`` — they intentionally allow the gated
"insufficient" shape (means absent, ``status='insufficient'`` + ``n_matured`` +
``threshold``) so the frontend can branch on ``status`` without a 2nd request.
"""

from __future__ import annotations

from rest_framework import serializers

from edge.models import LadderOutcome


class LadderOutcomeSerializer(serializers.ModelSerializer):
    """One population-ladder outcome row (per candidate)."""

    class Meta:
        model = LadderOutcome
        exclude = ("pk",)


class EdgeOutcomeRowSerializer(serializers.Serializer):
    """The lean per-candidate shape for ``/v1/edge/outcomes`` (memo §5 table)."""

    ticker = serializers.CharField()
    brief_date = serializers.DateField()
    theme = serializers.CharField(allow_null=True, required=False)
    ladder_classification = serializers.CharField(allow_blank=True)
    terminal = serializers.BooleanField()
    realized_r = serializers.FloatField(allow_null=True)
    open_r = serializers.FloatField(allow_null=True)
    market_excess_return = serializers.FloatField(allow_null=True)
    forward_return = serializers.FloatField(allow_null=True)
    benchmark_window_return = serializers.FloatField(allow_null=True)
    holding_days_elapsed = serializers.IntegerField(allow_null=True)
    realized_return_pct_of_book = serializers.FloatField(allow_null=True)


class _QuantilesSerializer(serializers.Serializer):
    p10 = serializers.FloatField(allow_null=True)
    p50 = serializers.FloatField(allow_null=True)
    p90 = serializers.FloatField(allow_null=True)


class EdgePanelSerializer(serializers.Serializer):
    """The EDGE panel — gated. ``status='insufficient'`` nulls the stat fields.

    The key SHAPE is stable (every field always present); the N-gate hides the
    numbers by setting them to ``null`` rather than dropping the keys, so the
    frontend can build a single static type and branch on ``status``.
    """

    status = serializers.ChoiceField(choices=["insufficient", "early", "ok"])
    n_matured = serializers.IntegerField()
    threshold = serializers.IntegerField()
    # null when status == 'insufficient'.
    market_excess_mean = serializers.FloatField(allow_null=True)
    market_excess_median = serializers.FloatField(allow_null=True)
    market_excess_quantiles = _QuantilesSerializer()
    gross_realized_r_mean = serializers.FloatField(allow_null=True)
    gross_realized_r_median = serializers.FloatField(allow_null=True)
    gross_realized_r_n = serializers.IntegerField()
    holding_days_n = serializers.IntegerField()
    holding_days_p50 = serializers.FloatField(allow_null=True)
    holding_days_p95 = serializers.FloatField(allow_null=True)
    gross_of_cost = serializers.BooleanField()
    regime_stratified = serializers.BooleanField()


class PortfolioPanelSerializer(serializers.Serializer):
    """The PORTFOLIO (size-weighted) panel — gated, same N-gate as EDGE."""

    status = serializers.ChoiceField(choices=["insufficient", "early", "ok"])
    n_matured = serializers.IntegerField()
    threshold = serializers.IntegerField()
    total_realized_contribution_pct_of_book = serializers.FloatField(allow_null=True)
    size_weighted_realized_r = serializers.FloatField(allow_null=True)
    mean_realized_risk_pct = serializers.FloatField(allow_null=True)
    mean_tiers_filled_count = serializers.FloatField(allow_null=True)
    gross_of_cost = serializers.BooleanField()


class DeploymentPanelSerializer(serializers.Serializer):
    """The DEPLOYMENT panel — N-INDEPENDENT, always populated."""

    n_terminal = serializers.IntegerField()
    n_filled = serializers.IntegerField()
    n_no_fill = serializers.IntegerField()
    fill_rate = serializers.FloatField(allow_null=True)
    no_fill_rate = serializers.FloatField(allow_null=True)
    mean_tiers_filled_count = serializers.FloatField(allow_null=True)


class OpenPositionsSerializer(serializers.Serializer):
    """Ongoing positions as a DESCRIPTIVE distribution (never a scalar mean)."""

    n_open = serializers.IntegerField()
    near_tp = serializers.IntegerField()
    near_sl = serializers.IntegerField()
    note = serializers.CharField()


class EdgeSummarySerializer(serializers.Serializer):
    """``/v1/edge/summary`` — the full N-gated, benchmark-relative aggregate."""

    n_brief = serializers.IntegerField()
    n_plannable = serializers.IntegerField()
    n_terminal = serializers.IntegerField()
    n_matured = serializers.IntegerField()
    n_gate_threshold = serializers.IntegerField()
    benchmark = serializers.CharField()
    metric_note = serializers.CharField()
    edge = EdgePanelSerializer()
    portfolio = PortfolioPanelSerializer()
    deployment = DeploymentPanelSerializer()
    open_positions = OpenPositionsSerializer()
