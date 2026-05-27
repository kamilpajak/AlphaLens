"""End-to-end ingest tests: tmp parquet directory → DB.

Uses ``@pytest.mark.django_db`` so each test gets a clean transactional
sandbox. ``tmp_path`` + ``pandas.DataFrame.to_parquet`` builds the input
fixture inline, no checked-in golden files.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import pandas as pd
import pytest
from django.core.management import call_command

from briefs.ingest.parquet import rebuild_from_parquet
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
            "layer4_weighted_score": 12,
            "verified": True,
            "market_cap": 3_000_000_000_000.0,
            "also_in_themes": ["compute"],
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
    def test_deleted_parquet_removes_briefs_and_meta(self, tmp_path: Path):
        path_a = _write_parquet(tmp_path, "2026-05-21", _sample_rows())
        _write_parquet(tmp_path, "2026-05-22", _sample_rows())
        rebuild_from_parquet(briefs_dir=tmp_path)
        assert Brief.objects.count() == 4
        assert DayMeta.objects.count() == 2

        path_a.unlink()
        result = rebuild_from_parquet(briefs_dir=tmp_path)

        assert result.n_deleted == 1
        assert result.deleted_dates == (dt.date(2026, 5, 21),)
        assert Brief.objects.filter(date=dt.date(2026, 5, 21)).count() == 0
        assert not DayMeta.objects.filter(date=dt.date(2026, 5, 21)).exists()
        assert Brief.objects.filter(date=dt.date(2026, 5, 22)).count() == 2


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
class TestManagementCommand:
    def test_command_runs(self, tmp_path: Path):
        _write_parquet(tmp_path, "2026-05-22", _sample_rows())
        call_command("rebuild_briefs_cache", "--briefs-dir", str(tmp_path))
        assert Brief.objects.count() == 2
