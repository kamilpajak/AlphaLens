"""DRF serializers — JSON shape for the read-only API.

`CandidateSerializer` is the ModelSerializer for ``Brief``. The four
denormalised ``*_str`` columns from the legacy SQLite cache
(`gates_passed_str`, `gates_failed_str`, `gates_unknown_str`,
`technicals_summary_str`) are intentionally absent: the canonical list[str]
fields are exposed, and the frontend formats them. See F1 model decision.

Other serializers (`DayMetaSerializer`, `DayBriefSerializer`, `ThemeSummarySerializer`,
`StatsSerializer`) are non-model: they shape aggregates or composed envelopes.
"""

from __future__ import annotations

from rest_framework import serializers

from briefs.models import Brief, DayMeta


# PR-5a reverses the PR-4 (#546) wire-split: the SPA card is now BLOB-DRIVEN — it
# reads the per-expert buffett values from expert_assessments instead of the flat
# buffett_* fields — so the always-visible card chip needs the blob IN the bulk
# list (a day brief / theme / ticker-history candidate). The PR-4 "redundant with
# the flat fields" rationale ends once the flat cols are dropped (PR-5b), so the
# blob must move back into the list. The blob is one small sparse dict per
# candidate (a few hundred bytes) for <100 candidates/day — negligible weight. The
# detail serializer is kept (now identical) for forward N-expert payload-split
# headroom. Epic #541, PR-5a (#547).
# Identical today (both just drop the composite pk); kept as two names so a future
# N-expert payload-split can trim the bulk list without re-touching the endpoints.
# test_api.test_blob_on_both_serializers pins that the two field sets stay EQUAL, so
# an accidental change to only one tuple reddens CI rather than silently diverging.
_LIST_EXCLUDE = ("pk",)
_DETAIL_EXCLUDE = ("pk",)


class CandidateSerializer(serializers.ModelSerializer):
    """One ranked candidate for a BULK list (day brief / theme / ticker history).

    Auto-exposes every Brief field except the composite pk — INCLUDING the
    ``expert_assessments`` blob, which the SPA card reads (PR-5a). The *_str legacy
    denormalisations live only in DRF method fields, not on the model, so they are
    not auto-exposed here.
    """

    class Meta:
        model = Brief
        exclude = _LIST_EXCLUDE


class CandidateDetailSerializer(serializers.ModelSerializer):
    """One candidate for the single-candidate DETAIL endpoint
    (``/v1/candidates/{date}/{ticker}``). Identical to :class:`CandidateSerializer`
    today; kept distinct so a future N-expert payload-split can trim the bulk list
    again without re-touching the endpoint wiring."""

    class Meta:
        model = Brief
        exclude = _DETAIL_EXCLUDE


class DayMetaSerializer(serializers.ModelSerializer):
    """Row in ``/v1/days``: per-day metadata only (no candidates)."""

    top_theme = serializers.SerializerMethodField()

    class Meta:
        model = DayMeta
        fields = ("date", "n_candidates", "n_themes", "top_theme")

    def get_top_theme(self, obj: DayMeta) -> str | None:
        """Empty-string DB default → None on the wire (parity with legacy API)."""
        return obj.top_theme or None


class EdgeBaseRateSerializer(serializers.Serializer):
    """Live edge base rate for ``meta.validation`` (see ``edge.api.base_rate``).

    Pool = plannable AND terminal AND finite realized_r over all dates. N-gated
    like /edge (stats null below 30 matured); degenerate math is null, never inf.
    """

    n_matured = serializers.IntegerField(
        help_text=(
            "Size of the realized-R pool (plannable, terminal, finite realized_r) "
            "— /edge's gross_realized_r_n, NOT its excess-keyed n_matured."
        )
    )
    mean_realized_r = serializers.FloatField(allow_null=True)
    payoff_ratio = serializers.FloatField(allow_null=True)
    breakeven_win_rate = serializers.FloatField(allow_null=True)
    as_of = serializers.DateField(
        allow_null=True,
        help_text="Max matured_at among contributing rows; null for an empty pool.",
    )


class DayValidationSerializer(serializers.Serializer):
    """Machine-generated honesty context (see ``briefs.api.day_validation``)."""

    selection_status = serializers.CharField()
    layer4_weighted_score_note = serializers.CharField()
    edge_base_rate = EdgeBaseRateSerializer()
    brief_dating = serializers.CharField()
    scorer_config_version = serializers.CharField(allow_null=True)


class DayMetaBlockSerializer(serializers.Serializer):
    """The additive top-level ``meta`` block on ``/v1/days/{date}``."""

    validation = DayValidationSerializer()


class DayBriefSerializer(serializers.Serializer):
    """Full payload for one day: meta + every ranked candidate."""

    date = serializers.DateField()
    n_candidates = serializers.IntegerField()
    n_themes = serializers.IntegerField()
    top_theme = serializers.CharField(allow_null=True)
    theme_counts = serializers.DictField(child=serializers.IntegerField())
    candidates = CandidateSerializer(many=True)
    meta = DayMetaBlockSerializer()


class ThemeSummarySerializer(serializers.Serializer):
    """Row in ``/v1/themes``: distinct theme + appearance counts."""

    theme = serializers.CharField()
    n_candidates = serializers.IntegerField()
    n_days = serializers.IntegerField()
    first_seen = serializers.DateField()
    last_seen = serializers.DateField()


class TopThemeSerializer(serializers.Serializer):
    theme = serializers.CharField()
    n_days = serializers.IntegerField()
    n_candidates = serializers.IntegerField()


class StatsSerializer(serializers.Serializer):
    """Top-line counters and most-frequent themes for ``/v1/stats``."""

    n_days = serializers.IntegerField()
    n_candidates = serializers.IntegerField()
    n_themes = serializers.IntegerField()
    earliest_date = serializers.DateField(allow_null=True)
    latest_date = serializers.DateField(allow_null=True)
    last_rebuild_at = serializers.DateTimeField(allow_null=True)
    top_themes = TopThemeSerializer(many=True)
