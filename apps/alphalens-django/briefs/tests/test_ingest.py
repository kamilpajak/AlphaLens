"""End-to-end ingest tests: tmp parquet directory → DB.

Uses ``@pytest.mark.django_db`` so each test gets a clean transactional
sandbox. ``tmp_path`` + ``pandas.DataFrame.to_parquet`` builds the input
fixture inline, no checked-in golden files.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
from pathlib import Path

import pandas as pd
import pytest
from django.core.management import call_command

from briefs.ingest.parquet import _EXPERT_COLUMNS, rebuild_from_parquet
from briefs.models import Brief, DayMeta


def _write_parquet(directory: Path, iso_date: str, rows: list[dict]) -> Path:
    """Write a brief parquet for one date into ``directory``."""
    path = directory / f"{iso_date}.parquet"
    pd.DataFrame(rows).to_parquet(path, index=False)
    return path


def _sample_rows() -> list[dict]:
    """Two rows that exercise required + a few optional columns."""
    return [
        {
            "ticker": "NVDA",
            "theme": "ai-infra",
            "company_name": "NVIDIA",
            "gates_passed": ["pe", "fcff"],
            "n_gates_passed": 2,
            "gate_verdict_json": '{"insider": {"passed": true, "threshold": 50000.0, "actual": 81000.0, "unit": "usd_net_90d"}}',
            "layer4_weighted_score": 12,
            "verified": True,
            "market_cap": 3_000_000_000_000.0,
            "also_in_themes": ["compute"],
            # Pipeline writes next_earnings_date as an ISO date STRING; ingest
            # must coerce it to the model DateField. AVGO omits it (null round-trip).
            "next_earnings_date": "2026-08-05",
            # Pipeline persists the trade setup as a json.dumps STRING of a dict.
            "brief_trade_setup": json.dumps(
                {"schema_version": "1.0.0", "status": "OK", "entry_tiers": [{"limit": 307.15}]}
            ),
        },
        {
            "ticker": "AVGO",
            "theme": "ai-infra",
            "company_name": "Broadcom",
            "gates_passed": ["pe"],
            "n_gates_passed": 1,
            "layer4_weighted_score": 8,
            "verified": False,
            "market_cap": 800_000_000_000.0,
            "also_in_themes": [],
        },
    ]


@pytest.mark.django_db
class TestRebuildSmoke:
    def test_first_run_creates_briefs_and_day_meta(self, tmp_path: Path):
        _write_parquet(tmp_path, "2026-05-22", _sample_rows())

        result = rebuild_from_parquet(briefs_dir=tmp_path)

        assert result.n_rebuilt == 1
        assert result.n_skipped == 0
        assert result.n_deleted == 0
        assert result.total_briefs == 2

        assert Brief.objects.count() == 2
        nvda = Brief.objects.get(ticker="NVDA")
        assert nvda.date == dt.date(2026, 5, 22)
        assert nvda.gates_passed == ["pe", "fcff"]
        assert nvda.verified is True
        # PR-4: structured gate reasons round-trip as a JSON string (TextField).
        assert json.loads(nvda.gate_verdict_json)["insider"]["actual"] == 81000.0
        # AVGO omits the column -> empty string default, not a crash.
        # next_earnings_date: ISO string in the parquet -> coerced DateField.
        assert nvda.next_earnings_date == dt.date(2026, 8, 5)
        # AVGO omits the column entirely -> null round-trip, not a crash.
        assert Brief.objects.get(ticker="AVGO").next_earnings_date is None

        meta = DayMeta.objects.get(date=dt.date(2026, 5, 22))
        assert meta.n_candidates == 2
        assert meta.n_themes == 1
        assert meta.top_theme == "ai-infra"
        assert meta.theme_counts == {"ai-infra": 2}

    def test_trade_setup_json_string_round_trips_to_dict(self, tmp_path: Path):
        _write_parquet(tmp_path, "2026-05-22", _sample_rows())
        rebuild_from_parquet(briefs_dir=tmp_path)

        nvda = Brief.objects.get(ticker="NVDA")
        assert isinstance(nvda.brief_trade_setup, dict)
        assert nvda.brief_trade_setup["status"] == "OK"
        assert nvda.brief_trade_setup["entry_tiers"][0]["limit"] == 307.15

        # AVGO omits the column entirely → NULL (older parquet / no setup persisted).
        avgo = Brief.objects.get(ticker="AVGO")
        assert avgo.brief_trade_setup is None

    def test_oneil_and_panel_assembled_into_blob(self, tmp_path: Path):
        # PR-8a: O'Neil's 8 columns + the 2 panel scalars are now ASSEMBLED into the
        # expert_assessments blob (PR-7 stamped them present-but-unread; this inverts
        # that). They ride under the "oneil" + "panel" blob keys, NOT model fields.
        rows = _sample_rows()
        rows[0].update(
            {
                "oneil_pct_off_52w_high": -3.0,
                "oneil_ma200_slope_pct_per_day": 0.05,
                "oneil_ma200_distance_pct": 8.0,
                "oneil_earnings_growth_yoy_pct": 20.0,
                "oneil_earnings_growth_near_zero_base": 0.0,
                "oneil_new_high_split_suspected": 1.0,
                "oneil_data_coverage": 1.0,
                "oneil_score": 72.0,
                "expert_spread": 47.0,
                "panel_config_version": "panel-v1-absdiff-2x",
            }
        )
        _write_parquet(tmp_path, "2026-05-22", rows)

        result = rebuild_from_parquet(briefs_dir=tmp_path)

        assert result.total_briefs == 2
        nvda = Brief.objects.get(ticker="NVDA")
        # Still NOT model attributes — the blob is the sole surface.
        assert not hasattr(nvda, "oneil_score")
        assert not hasattr(nvda, "expert_spread")
        ea = nvda.expert_assessments
        assert ea is not None
        oneil = ea["oneil"]
        assert oneil["oneil_score"] == 72.0
        assert oneil["oneil_pct_off_52w_high"] == -3.0
        # bool-as-float restored to a real tri-state bool (NOT the string "1.0").
        assert oneil["oneil_new_high_split_suspected"] is True
        assert oneil["oneil_earnings_growth_near_zero_base"] is False
        panel = ea["panel"]
        assert panel["expert_spread"] == 47.0
        assert panel["panel_config_version"] == "panel-v1-absdiff-2x"

    def test_null_spread_row_still_carries_panel_config_version(self, tmp_path: Path):
        # A row where the pipeline could not compute the spread (one expert absent)
        # still stamps panel_config_version; the blob's panel key holds null spread +
        # the config so the calibration corpus records which config would have applied.
        rows = _sample_rows()
        rows[0].update(
            {"expert_spread": float("nan"), "panel_config_version": "panel-v1-absdiff-2x"}
        )
        _write_parquet(tmp_path, "2026-05-22", rows)
        rebuild_from_parquet(briefs_dir=tmp_path)

        ea = Brief.objects.get(ticker="NVDA").expert_assessments
        assert ea is not None
        panel = ea["panel"]
        assert panel["expert_spread"] is None  # NaN finite-scrubbed to JSON null
        assert panel["panel_config_version"] == "panel-v1-absdiff-2x"

    def test_empty_directory_is_noop(self, tmp_path: Path):
        result = rebuild_from_parquet(briefs_dir=tmp_path)
        assert result.n_rebuilt == 0
        assert Brief.objects.count() == 0

    def test_missing_required_column_raises(self, tmp_path: Path):
        _write_parquet(tmp_path, "2026-05-22", [{"theme": "x", "layer4_weighted_score": 1}])
        with pytest.raises(ValueError, match="missing required columns"):
            rebuild_from_parquet(briefs_dir=tmp_path)


@pytest.mark.django_db
class TestMtimeGate:
    def test_second_run_skips_unchanged(self, tmp_path: Path):
        _write_parquet(tmp_path, "2026-05-22", _sample_rows())

        first = rebuild_from_parquet(briefs_dir=tmp_path)
        assert first.n_rebuilt == 1

        second = rebuild_from_parquet(briefs_dir=tmp_path)
        assert second.n_rebuilt == 0
        assert second.n_skipped == 1

    def test_mtime_bump_triggers_rebuild(self, tmp_path: Path):
        path = _write_parquet(tmp_path, "2026-05-22", _sample_rows())
        rebuild_from_parquet(briefs_dir=tmp_path)

        # Touch the file forward by 60s — Postgres + Python both round to micros,
        # so a whole second of skew is safely past _MTIME_EPS.
        future = path.stat().st_mtime + 60
        os.utime(path, (future, future))

        result = rebuild_from_parquet(briefs_dir=tmp_path)
        assert result.n_rebuilt == 1
        assert result.n_skipped == 0

    def test_force_ignores_mtime_gate(self, tmp_path: Path):
        _write_parquet(tmp_path, "2026-05-22", _sample_rows())
        rebuild_from_parquet(briefs_dir=tmp_path)

        result = rebuild_from_parquet(briefs_dir=tmp_path, force=True)
        assert result.n_rebuilt == 1
        assert result.n_skipped == 0


@pytest.mark.django_db
class TestOrphanDrop:
    def test_missing_parquet_prunes_only_with_opt_in(self, tmp_path: Path):
        path_a = _write_parquet(tmp_path, "2026-05-21", _sample_rows())
        _write_parquet(tmp_path, "2026-05-22", _sample_rows())
        rebuild_from_parquet(briefs_dir=tmp_path)
        assert Brief.objects.count() == 4
        assert DayMeta.objects.count() == 2

        path_a.unlink()
        result = rebuild_from_parquet(briefs_dir=tmp_path, prune_missing=True)

        assert result.n_deleted == 1
        assert result.deleted_dates == (dt.date(2026, 5, 21),)
        assert result.n_retained == 0
        assert Brief.objects.filter(date=dt.date(2026, 5, 21)).count() == 0
        assert not DayMeta.objects.filter(date=dt.date(2026, 5, 21)).exists()
        assert Brief.objects.filter(date=dt.date(2026, 5, 22)).count() == 2

    def test_missing_parquet_retained_by_default(self, tmp_path: Path):
        # PR-5 retention guard: a vanished parquet must NOT cascade-delete its
        # Brief rows by default (they are the EDGE-outcome join target).
        path_a = _write_parquet(tmp_path, "2026-05-21", _sample_rows())
        _write_parquet(tmp_path, "2026-05-22", _sample_rows())
        rebuild_from_parquet(briefs_dir=tmp_path)

        path_a.unlink()
        result = rebuild_from_parquet(briefs_dir=tmp_path)

        assert result.n_deleted == 0
        assert result.n_retained == 1
        assert result.retained_dates == (dt.date(2026, 5, 21),)
        # Rows + meta survive the missing parquet.
        assert Brief.objects.filter(date=dt.date(2026, 5, 21)).count() == 2
        assert DayMeta.objects.filter(date=dt.date(2026, 5, 21)).exists()


@pytest.mark.django_db
class TestSchemaTolerance:
    def test_missing_optional_columns_become_defaults(self, tmp_path: Path):
        # Old-format parquet: only required columns + one optional.
        _write_parquet(
            tmp_path,
            "2024-01-01",
            [{"ticker": "FOO", "theme": "legacy", "layer4_weighted_score": 5}],
        )
        rebuild_from_parquet(briefs_dir=tmp_path)
        foo = Brief.objects.get(ticker="FOO")
        assert foo.gates_passed == []
        assert foo.also_in_themes == []
        assert foo.verified is False
        assert foo.layer4_weighted_score == 5

    def test_non_iso_stem_is_skipped(self, tmp_path: Path):
        pd.DataFrame(_sample_rows()).to_parquet(tmp_path / "garbage.parquet", index=False)
        result = rebuild_from_parquet(briefs_dir=tmp_path)
        assert result.n_rebuilt == 0


@pytest.mark.django_db
class TestTemplateFactsRoundTrip:
    """L2 contract (test-strategy Phase 2): the typed template_facts seam.

    Producer: ``thematic.argumentation.orchestrator`` serialises the dict via
    ``json.dumps(..., sort_keys=True)`` into the parquet column
    ``brief_template_facts_json``. Consumer: ``ingest.parquet`` renames it to
    the model field ``brief_template_facts`` (a JSONField in
    ``_OBJECT_JSON_FIELDS``) and ``coerce_json_obj`` parses it back to a dict.
    The SPA renders the dict by iterating its keys, so a list/scalar would
    break the renderer — the positive control pins that non-dict JSON coerces
    to None, not a wrong shape. Failure class: seam-contract (field rename /
    JSON-interop corruption).
    """

    def test_template_facts_json_string_round_trips_to_dict(self, tmp_path: Path):
        facts = {
            "acquirer_name": "Example Corp",
            "target_name": "Target Inc",
            "deal_value_usd": "1500000000",
            "announced_date": "2026-05-20",
            "premium_pct": None,  # null value preserved inside the dict
        }
        _write_parquet(
            tmp_path,
            "2026-05-22",
            [
                {
                    "ticker": "EXMP",
                    "theme": "ma-activity",
                    "brief_template_id": "m_and_a_press_release",
                    # producer column name (aliased to brief_template_facts on ingest)
                    "brief_template_facts_json": json.dumps(facts, sort_keys=True),
                }
            ],
        )
        rebuild_from_parquet(briefs_dir=tmp_path)

        brief = Brief.objects.get(ticker="EXMP")
        assert isinstance(brief.brief_template_facts, dict)
        assert brief.brief_template_facts == facts
        assert brief.brief_template_facts["premium_pct"] is None
        assert brief.brief_template_id == "m_and_a_press_release"

    def test_non_dict_json_coerces_to_none(self, tmp_path: Path):
        # Positive control: a JSON array or scalar must NOT silently coerce to
        # a list/scalar (which the SPA dict-iterator can't render) — it must
        # become None. If this rotted, brief_template_facts would hold [1,2,3].
        _write_parquet(
            tmp_path,
            "2026-05-22",
            [
                {"ticker": "ARR", "theme": "t", "brief_template_facts_json": "[1, 2, 3]"},
                {"ticker": "SCA", "theme": "t", "brief_template_facts_json": "42"},
            ],
        )
        rebuild_from_parquet(briefs_dir=tmp_path)

        assert Brief.objects.get(ticker="ARR").brief_template_facts is None
        assert Brief.objects.get(ticker="SCA").brief_template_facts is None

    def test_missing_template_facts_column_is_null(self, tmp_path: Path):
        # Legacy parquet without the column → NULL facts, empty template id.
        _write_parquet(tmp_path, "2026-05-22", [{"ticker": "OLD", "theme": "legacy"}])
        rebuild_from_parquet(briefs_dir=tmp_path)

        old = Brief.objects.get(ticker="OLD")
        assert old.brief_template_facts is None
        assert old.brief_template_id == ""

    def test_brief_template_facts_is_in_object_json_fields(self):
        # Guard: a dict-shaped JSONField MUST be listed in _OBJECT_JSON_FIELDS,
        # else _coerce_for_field routes it through coerce_list_str and iterates
        # the dict's keys into a list[str] — the exact corruption this seam test
        # exists to prevent. (No django_db needed — pure config assertion.)
        from briefs.ingest.parquet import _OBJECT_JSON_FIELDS

        assert "brief_template_facts" in _OBJECT_JSON_FIELDS


@pytest.mark.django_db
class TestManagementCommand:
    def test_command_runs(self, tmp_path: Path):
        _write_parquet(tmp_path, "2026-05-22", _sample_rows())
        call_command("rebuild_briefs_cache", "--briefs-dir", str(tmp_path))
        assert Brief.objects.count() == 2


class TestDefaultBriefsDirEnvOverride:
    """Regression for the destructive container-vs-host path mismatch.

    The Django container runs as `django` (HOME=/home/django). The legacy
    ``DEFAULT_BRIEFS_DIR = Path.home() / .alphalens / thematic_briefs``
    resolved inside the container to ``/home/django/.alphalens/...`` which
    does not exist, so ``rebuild_briefs_cache --force`` deleted every date
    in the DB ("rebuilt=0 deleted=N"). The compose mount target is
    ``/var/lib/alphalens/thematic_briefs`` (see deploy/docker/django-prod/
    docker-compose.yaml). The env var ``ALPHALENS_BRIEFS_DIR`` lets compose
    push the right container-side path so the default matches the mount.
    """

    def test_env_var_overrides_default(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ALPHALENS_BRIEFS_DIR", str(tmp_path))
        # Re-import to pick up the env at module-init time.
        import importlib

        from briefs.ingest import parquet as parquet_mod

        importlib.reload(parquet_mod)
        try:
            assert parquet_mod.DEFAULT_BRIEFS_DIR == tmp_path
        finally:
            # Restore the original module-level default for other tests.
            monkeypatch.delenv("ALPHALENS_BRIEFS_DIR", raising=False)
            importlib.reload(parquet_mod)

    def test_falls_back_to_home_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("ALPHALENS_BRIEFS_DIR", raising=False)
        import importlib

        from briefs.ingest import parquet as parquet_mod

        importlib.reload(parquet_mod)
        try:
            assert parquet_mod.DEFAULT_BRIEFS_DIR == Path.home() / ".alphalens" / "thematic_briefs"
        finally:
            # Mirrors the override test — reload after monkeypatch teardown
            # so any subsequent test sees the real-env DEFAULT_BRIEFS_DIR.
            importlib.reload(parquet_mod)


_EXPECTED_BUFFETT_BLOB_COLUMNS = (
    "buffett_owner_earnings_yield_pct",
    "buffett_roic_latest",
    "buffett_roic_3y_avg",
    "buffett_margin_of_safety_pct",
    "buffett_data_coverage",
    "buffett_quality_score",
    "buffett_moat_type",
    "buffett_moat_trend",
    "buffett_management_candor",
    "buffett_understandable",
    "buffett_qualitative_rationale",
    "buffett_used_scuttlebutt",
    "buffett_qual_computed_at",
    "buffett_qual_config_version",
)

# Frozen mirrors of the pipeline tuples (Django cannot import the pipeline). Keep in
# lockstep with alphalens_pipeline.experts.oneil.quant_enrichment.ONEIL_COLUMNS and
# alphalens_pipeline.experts.disagreement.PANEL_COLUMNS — a divergence corrupts the
# blob assembly silently, so the drift-guard tests below are the only safety net.
_EXPECTED_ONEIL_BLOB_COLUMNS = (
    "oneil_pct_off_52w_high",
    "oneil_ma200_slope_pct_per_day",
    "oneil_ma200_distance_pct",
    "oneil_earnings_growth_yoy_pct",
    "oneil_earnings_growth_near_zero_base",
    "oneil_new_high_split_suspected",
    "oneil_data_coverage",
    "oneil_score",
    "oneil_rs_approx_pct",
)
_EXPECTED_PANEL_BLOB_COLUMNS = (
    "expert_spread",
    "panel_config_version",
)


@pytest.mark.django_db
class TestExpertAssessments:
    """PR-3: the expert_assessments JSONField is ASSEMBLED at ingest from the flat
    buffett_* columns, NaN/NaT/±inf-scrubbed, tri-state preserved."""

    @staticmethod
    def _buffett(ticker: str) -> dict:
        ea = Brief.objects.get(ticker=ticker).expert_assessments
        assert ea is not None
        return ea["buffett"]

    def test_blob_assembles_from_flat_parquet_columns(self, tmp_path: Path):
        rows = [
            {
                "ticker": "AAA",
                "theme": "t",
                "buffett_owner_earnings_yield_pct": 5.0,
                "buffett_roic_latest": 18.0,
                "buffett_moat_type": "brand",
                "buffett_understandable": True,
                "buffett_qualitative_rationale": "durable franchise",
                "buffett_used_scuttlebutt": True,
                "buffett_qual_computed_at": "2026-06-12T09:00:00+00:00",
                "buffett_qual_config_version": "buffett-pre-registry-v0",
            }
        ]
        _write_parquet(tmp_path, "2026-05-22", rows)
        rebuild_from_parquet(briefs_dir=tmp_path)

        # The blob is assembled from the flat PARQUET columns (PR-5b dropped the flat
        # MODEL fields, so the Brief no longer has buffett_* attributes — only the blob).
        blob = self._buffett("AAA")
        assert blob["buffett_owner_earnings_yield_pct"] == 5.0
        assert blob["buffett_moat_type"] == "brand"
        assert blob["buffett_understandable"] is True
        assert blob["buffett_qual_config_version"] == "buffett-pre-registry-v0"
        # The flat MODEL field is gone (PR-5b migration 0012): accessing it raises
        # AttributeError — explicit access asserts the drop, where `not hasattr`
        # would pass silently if `hasattr` itself swallowed an unrelated error.
        with pytest.raises(AttributeError):
            _ = Brief.objects.get(ticker="AAA").buffett_moat_type  # type: ignore[attr-defined]

    def test_non_finite_floats_become_json_null(self, tmp_path: Path):
        rows = [
            {
                "ticker": "AAA",
                "theme": "t",
                "buffett_owner_earnings_yield_pct": float("nan"),
                "buffett_roic_latest": 18.0,
            },
            {"ticker": "BBB", "theme": "t", "buffett_owner_earnings_yield_pct": float("inf")},
            {"ticker": "CCC", "theme": "t", "buffett_roic_latest": float("-inf")},
        ]
        _write_parquet(tmp_path, "2026-05-22", rows)
        rebuild_from_parquet(briefs_dir=tmp_path)

        for ticker in ("AAA", "BBB", "CCC"):
            # Reload from the DB (true write -> JSONField -> reload cycle).
            blob = self._buffett(ticker)
            # POSITIVE finiteness assertion over every numeric leaf (backend-agnostic;
            # also reddens on an Infinity leak, not just a substring scan).
            for value in blob.values():
                assert value is None or not isinstance(value, float) or math.isfinite(value)
            # And no NaN/Infinity token in the serialized JSON.
            serialized = json.dumps(Brief.objects.get(ticker=ticker).expert_assessments)
            for token in ("NaN", "NaT", "Infinity", "-Infinity"):
                assert token not in serialized
        assert self._buffett("AAA")["buffett_roic_latest"] == 18.0

    def test_tristate_understandable_preserved_in_blob(self, tmp_path: Path):
        rows = [
            {"ticker": "AAA", "theme": "t", "buffett_understandable": True},
            {"ticker": "BBB", "theme": "t", "buffett_understandable": False},
            # CCC has no understandable value; pandas unions the column across rows,
            # so CCC's buffett_understandable is present-as-NaN -> blob holds explicit
            # JSON null (tri-state), NOT False. (A blob that is wholly None only
            # happens when the parquet has NO buffett column at all — see
            # test_migration_0011_null_backfill.)
            {"ticker": "CCC", "theme": "t"},
        ]
        _write_parquet(tmp_path, "2026-05-22", rows)
        rebuild_from_parquet(briefs_dir=tmp_path)

        assert self._buffett("AAA")["buffett_understandable"] is True
        assert self._buffett("BBB")["buffett_understandable"] is False
        assert self._buffett("CCC")["buffett_understandable"] is None  # null, not False

    def test_config_version_rides_only_in_blob_no_flat_field(self, tmp_path: Path):
        rows = [
            {
                "ticker": "AAA",
                "theme": "t",
                "buffett_qual_config_version": "buffett-pre-registry-v0",
            }
        ]
        _write_parquet(tmp_path, "2026-05-22", rows)
        rebuild_from_parquet(briefs_dir=tmp_path)

        assert self._buffett("AAA")["buffett_qual_config_version"] == "buffett-pre-registry-v0"
        assert not hasattr(Brief.objects.get(ticker="AAA"), "buffett_qual_config_version")

    def test_expert_columns_match_frozen_buffett_tuple(self):
        # The ONLY cross-boundary drift guard (Django cannot import the pipeline).
        assert _EXPERT_COLUMNS["buffett"] == _EXPECTED_BUFFETT_BLOB_COLUMNS

    def test_expert_columns_match_frozen_oneil_tuple(self):
        # Drift guard: _EXPERT_COLUMNS["oneil"] must mirror the pipeline's
        # ONEIL_COLUMNS. If they diverge, the blob assembly silently drops/mistypes a
        # column. Keep _EXPECTED_ONEIL_BLOB_COLUMNS in lockstep with the registry.
        assert _EXPERT_COLUMNS["oneil"] == _EXPECTED_ONEIL_BLOB_COLUMNS

    def test_expert_columns_match_frozen_panel_tuple(self):
        # Drift guard: _EXPERT_COLUMNS["panel"] must mirror disagreement.PANEL_COLUMNS.
        assert _EXPERT_COLUMNS["panel"] == _EXPECTED_PANEL_BLOB_COLUMNS

    def test_oneil_panel_blob_keys_are_migration_free(self):
        # The whole justification for the blob-key path (vs flat Brief fields) is
        # that it needs NO migration — the JSONField already exists. Pin that adding
        # the oneil/panel keys did not introduce an un-applied model change.
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("makemigrations", "briefs", check=True, dry_run=True, stdout=out, stderr=out)

    def test_migration_0011_null_backfill(self, tmp_path: Path):
        # A brief whose parquet carries no buffett columns has expert_assessments
        # None (clean null over the new nullable JSONField).
        _write_parquet(tmp_path, "2026-05-22", [{"ticker": "AAA", "theme": "t"}])
        rebuild_from_parquet(briefs_dir=tmp_path)
        assert Brief.objects.get(ticker="AAA").expert_assessments is None


@pytest.mark.django_db
class TestAtrTiltFieldsIngest:
    """PR-2 contract guard: selection_score, atr_penalty, and scorer_config_version
    are picked up from matching parquet columns by the field-name-driven ingest path
    (same mechanism as insider_signal_version).  These tests exist so a future rename
    or type change in the model cannot silently drop or mis-coerce the columns."""

    def test_atr_fields_round_trip(self, tmp_path: Path):
        """All three ATR-tilt fields land on the Brief model with correct types."""
        _write_parquet(
            tmp_path,
            "2026-06-25",
            [
                {
                    "ticker": "NVDA",
                    "theme": "ai-infra",
                    "selection_score": 72.5,
                    "atr_penalty": 3.1,
                    "scorer_config_version": "atr-tilt-v1",
                }
            ],
        )
        rebuild_from_parquet(briefs_dir=tmp_path)
        brief = Brief.objects.get(ticker="NVDA")
        assert brief.selection_score == pytest.approx(72.5)
        assert brief.atr_penalty == pytest.approx(3.1)
        assert brief.scorer_config_version == "atr-tilt-v1"

    def test_missing_atr_columns_default_to_null_and_blank(self, tmp_path: Path):
        """A pre-atr-tilt parquet that omits these columns ingests without error:
        the two floats default to None and the version string defaults to ''."""
        _write_parquet(
            tmp_path,
            "2026-06-25",
            [{"ticker": "OLD", "theme": "legacy"}],
        )
        rebuild_from_parquet(briefs_dir=tmp_path)
        brief = Brief.objects.get(ticker="OLD")
        assert brief.selection_score is None
        assert brief.atr_penalty is None
        assert brief.scorer_config_version == ""

    def test_present_but_nan_atr_columns_coerce_to_null_and_blank(self, tmp_path: Path):
        """Defensive: the pipeline never emits NaN/None for these columns, but if a
        column is present-but-NaN (float) or present-but-None (string), the ingest
        finite-scrubs it (coerce_float -> None, CharField -> "") so no NaN ever
        reaches Postgres / the JSON wire. Mirrors the expert_spread NaN guard above."""
        _write_parquet(
            tmp_path,
            "2026-06-25",
            [
                {
                    "ticker": "NANROW",
                    "theme": "legacy",
                    "layer4_weighted_score": 2,
                    "selection_score": float("nan"),
                    "atr_penalty": float("nan"),
                    "scorer_config_version": None,
                }
            ],
        )
        rebuild_from_parquet(briefs_dir=tmp_path)
        brief = Brief.objects.get(ticker="NANROW")
        assert brief.selection_score is None
        assert brief.atr_penalty is None
        assert brief.scorer_config_version == ""


@pytest.mark.django_db
class TestInsiderSignalVersionIngest:
    """The insider-signal poolability key round-trips so the deferred
    Insider×EDGE calibration can partition old vs new signal semantics."""

    def test_signal_version_round_trips(self, tmp_path: Path):
        _write_parquet(
            tmp_path,
            "2026-06-17",
            [
                {
                    "ticker": "BAH",
                    "theme": "defense",
                    "insider_score_usd": 120000.0,
                    "insider_score_sector_percentile": 80.0,
                    "insider_signal_version": "insider-v2-buyonly-180d-withinbuyers",
                }
            ],
        )
        rebuild_from_parquet(briefs_dir=tmp_path)
        bah = Brief.objects.get(ticker="BAH")
        assert bah.insider_signal_version == "insider-v2-buyonly-180d-withinbuyers"

    def test_missing_version_defaults_to_blank(self, tmp_path: Path):
        # A pre-v2 parquet that omits the column ingests to "" (not a crash).
        _write_parquet(tmp_path, "2026-06-17", [{"ticker": "AAA", "theme": "t"}])
        rebuild_from_parquet(briefs_dir=tmp_path)
        assert Brief.objects.get(ticker="AAA").insider_signal_version == ""
