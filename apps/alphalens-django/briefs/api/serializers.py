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


# The heavy expert-panel blob (expert_assessments — a per-expert dict of ~14
# values each) is detail-endpoint-only: it would bloat the bulk candidate lists
# (a day brief / theme / ticker-history can carry dozens of candidates × the blob,
# all redundant with the flat buffett_* fields that still ship). The list
# serializer drops it; the detail serializer keeps it. The split is enforced by a
# serializer-field test (not advisory). Epic #541, PR-4 (#546).
_LIST_EXCLUDE = ("pk", "expert_assessments")
_DETAIL_EXCLUDE = ("pk",)


class CandidateSerializer(serializers.ModelSerializer):
    """One ranked candidate for a BULK list (day brief / theme / ticker history).

    Auto-exposes every Brief field except the composite pk AND the heavy
    ``expert_assessments`` blob (see :class:`CandidateDetailSerializer`). The *_str
    legacy denormalisations live only in DRF method fields, not on the model, so
    they are not auto-exposed here.
    """

    class Meta:
        model = Brief
        exclude = _LIST_EXCLUDE


class CandidateDetailSerializer(serializers.ModelSerializer):
    """One candidate for the single-candidate DETAIL endpoint
    (``/v1/candidates/{date}/{ticker}``) — identical to
    :class:`CandidateSerializer` plus the full ``expert_assessments`` blob, fetched
    on demand (e.g. for a card's deep-read drawer) rather than in every bulk list."""

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
