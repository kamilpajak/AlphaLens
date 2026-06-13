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


class CandidateSerializer(serializers.ModelSerializer):
    """One ranked candidate from a daily thematic brief."""

    class Meta:
        model = Brief
        # Auto-expose every Brief field except the composite pk via `exclude`, so a
        # new model field appears in the wire payload without editing a hand-kept
        # list (no drifting count to maintain). The *_str legacy denormalisations
        # live only in the DRF method fields below, not on the model, so they are
        # not auto-exposed here.
        exclude = ("pk",)


class DayMetaSerializer(serializers.ModelSerializer):
    """Row in ``/v1/days``: per-day metadata only (no candidates)."""

    top_theme = serializers.SerializerMethodField()

    class Meta:
        model = DayMeta
        fields = ("date", "n_candidates", "n_themes", "top_theme")

    def get_top_theme(self, obj: DayMeta) -> str | None:
        """Empty-string DB default → None on the wire (parity with legacy API)."""
        return obj.top_theme or None


class DayBriefSerializer(serializers.Serializer):
    """Full payload for one day: meta + every ranked candidate."""

    date = serializers.DateField()
    n_candidates = serializers.IntegerField()
    n_themes = serializers.IntegerField()
    top_theme = serializers.CharField(allow_null=True)
    theme_counts = serializers.DictField(child=serializers.IntegerField())
    candidates = CandidateSerializer(many=True)


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
