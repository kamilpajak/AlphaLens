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
    matured_at = serializers.DateField(allow_null=True, required=False)
    theme = serializers.CharField(allow_null=True, required=False)
    scorer_config_version = serializers.CharField(allow_null=True, required=False)
    ladder_classification = serializers.CharField(allow_blank=True)
    # TP levels touched vs tranches actually sold: captured < touched flags that
    # TP_FULL / the chart's green arrows overstate capture (partial entry fill).
    # Nullable for rows that predate the columns.
    captured_tp_count = serializers.IntegerField(allow_null=True, required=False)
    touched_tp_count = serializers.IntegerField(allow_null=True, required=False)
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
    hit_rate = serializers.FloatField(allow_null=True)
    gross_realized_r_mean = serializers.FloatField(allow_null=True)
    gross_realized_r_median = serializers.FloatField(allow_null=True)
    gross_realized_r_n = serializers.IntegerField()
    holding_days_n = serializers.IntegerField()
    holding_days_p50 = serializers.FloatField(allow_null=True)
    holding_days_p95 = serializers.FloatField(allow_null=True)
    gross_of_cost = serializers.BooleanField()
    regime_stratified = serializers.BooleanField()


class PortfolioPanelSerializer(serializers.Serializer):
    """The PORTFOLIO (per-name size) panel — gated, same N-gate as EDGE.

    Reports per-name risk geometry only; the shared-book aggregates were removed
    (no single capital book exists for this tool, ADR 0012).
    """

    status = serializers.ChoiceField(choices=["insufficient", "early", "ok"])
    n_matured = serializers.IntegerField()
    threshold = serializers.IntegerField()
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


class ChartBarSerializer(serializers.Serializer):
    """One daily OHLC candle (folded from the cached minute bars, RTH-only).

    ``time`` is an ISO date string (``YYYY-MM-DD``) so it maps 1:1 to a
    Lightweight-Charts daily bar time.
    """

    time = serializers.CharField()
    open = serializers.FloatField()
    high = serializers.FloatField()
    low = serializers.FloatField()
    close = serializers.FloatField()
    volume = serializers.FloatField()


class ChartMarkerSerializer(serializers.Serializer):
    """One modeled fill / exit marker, snapped to a daily bar ``time``.

    ``kind`` is the chart vocabulary (``ENTRY`` / ``TP`` / ``SL`` / ``TIME_STOP``);
    ``ambiguous`` carries the SL-first intrabar flag (a bar that touched both a TP
    high and the SL low, resolved SL-first).
    """

    time = serializers.CharField()
    kind = serializers.CharField()
    level_id = serializers.CharField()
    price = serializers.FloatField(allow_null=True)
    label = serializers.CharField()
    ambiguous = serializers.BooleanField()


class ChartPriceLinesSerializer(serializers.Serializer):
    """The resting ladder levels drawn as horizontal price lines.

    ``tp`` is the ordered list of take-profit targets. TIME_STOP is NOT a price
    line — it is an exit event drawn only as a marker.
    """

    entry = serializers.FloatField(allow_null=True)
    tp = serializers.ListField(child=serializers.FloatField())
    stop = serializers.FloatField(allow_null=True)


class ChartResponseSerializer(serializers.Serializer):
    """``/v1/edge/chart/<brief_date>/<ticker>`` — the pre-computed chart payload.

    The SHAPE is stable across ``status``: a NO_DATA / NO_STRUCTURE payload carries
    empty ``bars`` / ``markers`` and null/empty ``price_lines`` so the SPA branches
    on ``status`` without a 2nd request. ``rth_only`` is always true in PR-1
    (RTH-only daily candles; intraday is a later PR).
    """

    brief_date = serializers.DateField()
    ticker = serializers.CharField()
    ladder_classification = serializers.CharField(allow_blank=True)
    # Lifecycle fields so the SPA can style Open vs Closed + a "Day N" label
    # without a 2nd request. ``terminal`` is the closed/ongoing flag; the rest are
    # null while a position is still open (or has no fill).
    terminal = serializers.BooleanField()
    holding_days_elapsed = serializers.IntegerField(allow_null=True)
    open_r = serializers.FloatField(allow_null=True)
    realized_r = serializers.FloatField(allow_null=True)
    status = serializers.ChoiceField(choices=["OK", "NO_DATA", "NO_STRUCTURE"])
    bars = ChartBarSerializer(many=True)
    price_lines = ChartPriceLinesSerializer()
    markers = ChartMarkerSerializer(many=True)
    ambiguous_bars = serializers.IntegerField()
    intrabar_rule = serializers.CharField()
    rth_only = serializers.BooleanField()


class WhatIfLensSerializer(serializers.Serializer):
    """One break-even lens's gated R aggregate (keyed by lens_id in the parent map).

    ``mean_r`` / ``median_r`` are null below the N-gate; ``n`` survives so the UI can
    show coverage.
    """

    n = serializers.IntegerField()
    mean_r = serializers.FloatField(allow_null=True)
    median_r = serializers.FloatField(allow_null=True)
    # Same-cohort realized baseline for "vs realized" — the realized R of exactly
    # this lens's contributing rows (not the panel-wide gross mean). Null below the
    # gate; ``realized_r_baseline_n`` can be < ``n`` (never-filled NO_FILL rows carry
    # a counterfactual value but no realized outcome).
    realized_r_baseline = serializers.FloatField(allow_null=True)
    realized_r_baseline_n = serializers.IntegerField()
    # Paired per-row direction counts over the baseline cohort — lens R strictly
    # above the row's realized R is "helped", strictly below is "harmed", ties feed
    # neither (so n_helped + n_harmed <= realized_r_baseline_n). Nulled below the
    # N-gate like the means (they reveal the effect's direction).
    n_helped = serializers.IntegerField(allow_null=True)
    n_harmed = serializers.IntegerField(allow_null=True)
    # Provenance ref (design-memo section) for a lens whose parameters were fixed
    # BEFORE registration; null for in-sample-tuned lenses. Mirrored from the
    # pipeline registry in ``summary._LENS_PREREGISTERED_REF``.
    preregistered_ref = serializers.CharField(allow_null=True)


class WhatIfPanelSerializer(serializers.Serializer):
    """The WHAT-IF panel — display-only, IN-SAMPLE counterfactual exit-stop lenses.

    Gated like EDGE. The realized headline is never touched by this block. The lens
    registry (labels + ``in_sample``/``validated`` status) lives client-side, so the
    ``lenses`` map is keyed by ``lens_id`` only.
    """

    status = serializers.ChoiceField(choices=["insufficient", "early", "ok"])
    n_matured = serializers.IntegerField()
    threshold = serializers.IntegerField()
    in_sample = serializers.BooleanField()
    note = serializers.CharField()
    lenses = serializers.DictField(child=WhatIfLensSerializer())


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
    whatif = WhatIfPanelSerializer()
    deployment = DeploymentPanelSerializer()
    open_positions = OpenPositionsSerializer()


class EdgeExcessPointSerializer(serializers.Serializer):
    """One per-trade point in the SPY-excess scatter."""

    date = serializers.CharField()
    excess = serializers.FloatField()
    ticker = serializers.CharField(allow_blank=True)
    holding_days = serializers.IntegerField(allow_null=True)
    episode_repeat = serializers.BooleanField()


class EdgeExcessTrendSerializer(serializers.Serializer):
    """One trailing-window smoothed value in the SPY-excess trend line."""

    date = serializers.CharField()
    mean = serializers.FloatField()
    lo = serializers.FloatField()
    hi = serializers.FloatField()


class EdgeExcessTelemetrySerializer(serializers.Serializer):
    """``/v1/edge/excess-telemetry`` — per-trade SPY-excess scatter + gated trend."""

    benchmark = serializers.CharField()
    status = serializers.ChoiceField(choices=["accumulating", "ok"])
    gate_threshold = serializers.IntegerField()
    n_total = serializers.IntegerField()
    n_effective = serializers.IntegerField()
    median_holding_days = serializers.FloatField(allow_null=True)
    smoother_window = serializers.IntegerField()
    metric_note = serializers.CharField()
    benchmark_note = serializers.CharField()
    points = EdgeExcessPointSerializer(many=True)
    trend = EdgeExcessTrendSerializer(many=True)
