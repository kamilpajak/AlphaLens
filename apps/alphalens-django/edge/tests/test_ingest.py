"""End-to-end ingest tests: tmp parquet store → DB.

Uses ``@pytest.mark.django_db``. ``tmp_path`` + ``pandas.DataFrame.to_parquet``
builds the population-ladder fixture inline, no checked-in golden files.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest
from django.core.management import call_command
from edge.ingest.parquet import rebuild_from_parquet
from edge.models import DayMetaLadderOutcome, LadderOutcome


def _write_parquet(directory: Path, iso_date: str, rows: list[dict]) -> Path:
    path = directory / f"{iso_date}.parquet"
    pd.DataFrame(rows).to_parquet(path, index=False)
    return path


def _terminal_row(ticker: str, *, excess: float | None) -> dict:
    return {
        "brief_date": dt.date(2026, 5, 27),
        "ticker": ticker,
        "plannable": True,
        "nonplannable_reason": None,
        "terminal": True,
        "matured_at": dt.date(2026, 6, 2),
        "ladder_classification": "TP_FULL",
        "blended_entry": 100.0,
        "realized_r": 1.5,
        "open_r": None,
        "mfe": 1.8,
        "mae": -0.2,
        "mfe_pct": 0.18,
        "mae_pct": -0.02,
        "forward_return": 0.06,
        "benchmark_window_return": 0.02 if excess is not None else None,
        "market_excess_return": excess,
        "sequence_str": "E1->TP1",
        "ambiguous_bars": 0,
        "ratchet_realized_r": 1.4,
        "holding_days_elapsed": 11,
        "entry_ttl_days": 7,
        "position_ttl_days": 42,
        "ladder_config_version": '{"order_ttl_days":7,"time_stop_days":42}',
        "suggested_gross_weight_pct": 0.04,
        "full_ladder_blended_entry": 100.0,
        "stop_distance_pct_full": 0.05,
        "implied_risk_pct_full": 0.002,
        "tiers_filled_count": 1.0,
        "realized_gross_weight_pct": 0.04,
        "stop_distance_pct": 0.05,
        "realized_risk_pct": 0.002,
        "realized_return_pct_of_book": 0.003,
        "open_return_pct_of_book": None,
    }


@pytest.mark.django_db
def test_ingest_writes_rows_and_daymeta(tmp_path: Path):
    _write_parquet(
        tmp_path,
        "2026-05-27",
        [_terminal_row("AMPL", excess=0.04), _terminal_row("RGTI", excess=-0.02)],
    )
    result = rebuild_from_parquet(tmp_path)

    assert result.n_rebuilt == 1
    assert result.total_rows == 2
    assert LadderOutcome.objects.count() == 2
    ampl = LadderOutcome.objects.get(ticker="AMPL")
    assert ampl.market_excess_return == pytest.approx(0.04)
    assert ampl.realized_r == pytest.approx(1.5)
    assert ampl.ladder_config_version == '{"order_ttl_days":7,"time_stop_days":42}'

    meta = DayMetaLadderOutcome.objects.get(brief_date=dt.date(2026, 5, 27))
    assert meta.n_rows == 2
    assert meta.n_terminal == 2
    assert meta.n_plannable == 2


@pytest.mark.django_db
def test_ingest_is_idempotent_upsert(tmp_path: Path):
    """A re-run on a CHANGED parquet replaces rows for the date (no duplication)."""
    _write_parquet(tmp_path, "2026-05-27", [_terminal_row("AMPL", excess=0.04)])
    rebuild_from_parquet(tmp_path, force=True)
    assert LadderOutcome.objects.filter(ticker="AMPL").count() == 1

    # Rewrite the date with two rows; force ignores the mtime gate.
    _write_parquet(
        tmp_path,
        "2026-05-27",
        [_terminal_row("AMPL", excess=0.05), _terminal_row("NEW", excess=0.01)],
    )
    rebuild_from_parquet(tmp_path, force=True)
    assert LadderOutcome.objects.count() == 2
    assert LadderOutcome.objects.get(ticker="AMPL").market_excess_return == pytest.approx(0.05)


@pytest.mark.django_db
def test_ingest_tolerates_old_parquet_without_size_or_benchmark_columns(tmp_path: Path):
    """An OLD parquet (21-col, pre-size, pre-benchmark) ingests with NULLs."""
    legacy_row = {
        "brief_date": dt.date(2026, 5, 24),
        "ticker": "OLDFMT",
        "plannable": True,
        "terminal": True,
        "matured_at": dt.date(2026, 6, 1),
        "ladder_classification": "SL_HIT",
        "realized_r": -1.0,
        "forward_return": -0.04,
        "holding_days_elapsed": 5,
    }
    _write_parquet(tmp_path, "2026-05-24", [legacy_row])
    rebuild_from_parquet(tmp_path)

    row = LadderOutcome.objects.get(ticker="OLDFMT")
    assert row.realized_r == pytest.approx(-1.0)
    assert row.market_excess_return is None  # absent column -> NULL
    assert row.suggested_gross_weight_pct is None  # absent size column -> NULL


@pytest.mark.django_db
def test_management_command_runs_with_guard(tmp_path: Path):
    """The command boots, runs the migration-skew guard, and ingests."""
    _write_parquet(tmp_path, "2026-05-27", [_terminal_row("AMPL", excess=0.04)])
    call_command("rebuild_ladder_outcomes_cache", "--store-dir", str(tmp_path), "--force")
    assert LadderOutcome.objects.filter(ticker="AMPL").count() == 1


@pytest.mark.django_db
def test_ingest_drops_dates_whose_parquet_disappeared(tmp_path: Path):
    _write_parquet(tmp_path, "2026-05-27", [_terminal_row("AMPL", excess=0.04)])
    rebuild_from_parquet(tmp_path)
    assert LadderOutcome.objects.count() == 1

    (tmp_path / "2026-05-27.parquet").unlink()
    result = rebuild_from_parquet(tmp_path)
    assert result.n_deleted == 1
    assert LadderOutcome.objects.count() == 0
    assert DayMetaLadderOutcome.objects.count() == 0


@pytest.mark.django_db
def test_ingest_tolerates_empty_parquet_from_zero_candidate_date(tmp_path: Path):
    # A 0-candidate brief date makes the population monitor write an EMPTY store
    # parquet — 0 rows, only the benchmark-excess columns (no brief_date/ticker).
    # The ingest must treat it as "no outcomes for this date", NOT crash the whole
    # rebuild (which would fail the thematic-build unit).
    path = tmp_path / "2026-05-27.parquet"
    pd.DataFrame({"benchmark_window_return": [], "market_excess_return": []}).to_parquet(
        path, index=False
    )
    result = rebuild_from_parquet(tmp_path)
    assert result.total_rows == 0
    assert LadderOutcome.objects.count() == 0
    meta = DayMetaLadderOutcome.objects.get(brief_date=dt.date(2026, 5, 27))
    assert meta.n_rows == 0


@pytest.mark.django_db
def test_ingest_still_raises_on_nonempty_parquet_missing_required_columns(tmp_path: Path):
    # A NON-empty parquet that lacks brief_date/ticker is a real schema break and
    # must still fail loudly (the empty-tolerance must not mask that).
    path = tmp_path / "2026-05-27.parquet"
    pd.DataFrame([{"realized_r": 1.0}]).to_parquet(path, index=False)
    with pytest.raises(ValueError, match="missing required columns"):
        rebuild_from_parquet(tmp_path)
