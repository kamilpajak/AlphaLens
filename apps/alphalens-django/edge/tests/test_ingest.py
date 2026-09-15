"""End-to-end ingest tests: tmp parquet store → DB.

Uses ``@pytest.mark.django_db``. ``tmp_path`` + ``pandas.DataFrame.to_parquet``
builds the population-ladder fixture inline, no checked-in golden files.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import os
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
        "grid_realized_r_json": '{"single_tp_first":1.0,"single_tp_last":-1.0,"no_tp_ride":-1.0}',
        "realized_r_full_fill": 1.3,
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
    assert json.loads(ampl.grid_realized_r_json)["single_tp_first"] == 1.0
    assert ampl.realized_r_full_fill == pytest.approx(1.3)

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


@pytest.mark.django_db
def test_ingest_persists_scorer_config_version(tmp_path: Path):
    """A parquet WITH scorer_config_version flows through to the DB row."""
    row = _terminal_row("SCVT", excess=0.03)
    row["scorer_config_version"] = "scorer-v1-test"
    _write_parquet(tmp_path, "2026-05-27", [row])
    rebuild_from_parquet(tmp_path)

    outcome = LadderOutcome.objects.get(ticker="SCVT")
    assert outcome.scorer_config_version == "scorer-v1-test"


@pytest.mark.django_db
def test_ingest_scorer_config_version_defaults_to_empty_when_column_absent(tmp_path: Path):
    """A parquet WITHOUT scorer_config_version ingests with the field defaulting to ''."""
    row = _terminal_row("OLDROW", excess=0.01)
    # Explicitly ensure the column is absent (not just None).
    row.pop("scorer_config_version", None)
    _write_parquet(tmp_path, "2026-05-27", [row])
    rebuild_from_parquet(tmp_path)

    outcome = LadderOutcome.objects.get(ticker="OLDROW")
    assert outcome.scorer_config_version == ""


@pytest.mark.django_db
def test_ingest_persists_source_and_event_overlap(tmp_path: Path):
    """Event-lane provenance (epic #1293) flows through to the DB row."""
    row = _terminal_row("LUCK", excess=0.03)
    row["source"] = "insider_cluster"
    row["event_overlap"] = False
    other = _terminal_row("QUBT", excess=0.01)
    other["source"] = "thematic"
    other["event_overlap"] = True
    _write_parquet(tmp_path, "2026-05-27", [row, other])
    rebuild_from_parquet(tmp_path)

    assert LadderOutcome.objects.get(ticker="LUCK").source == "insider_cluster"
    assert LadderOutcome.objects.get(ticker="LUCK").event_overlap is False
    assert LadderOutcome.objects.get(ticker="QUBT").source == "thematic"
    assert LadderOutcome.objects.get(ticker="QUBT").event_overlap is True


@pytest.mark.django_db
def test_ingest_source_defaults_to_empty_and_overlap_false_when_absent(tmp_path: Path):
    """A parquet WITHOUT the lane columns ingests as '' / False (read as thematic)."""
    row = _terminal_row("OLDLANE", excess=0.01)
    row.pop("source", None)
    row.pop("event_overlap", None)
    _write_parquet(tmp_path, "2026-05-27", [row])
    rebuild_from_parquet(tmp_path)

    outcome = LadderOutcome.objects.get(ticker="OLDLANE")
    assert outcome.source == ""
    assert outcome.event_overlap is False
    assert LadderOutcome.objects.thematic().count() == 1


@pytest.mark.django_db
def test_ingest_persists_sector_excess_columns(tmp_path: Path):
    """A parquet WITH the sector-excess columns (PR-2b) flows through to the DB.

    The sector-relative outcome is measured against the candidate's OWN SPDR
    sector ETF over the SAME window as market-excess — a different series from the
    SPY-derived market_state label (memo §4.2, D4 resolution).
    """
    row = _terminal_row("NVDA", excess=0.05)
    row["sector_etf_ticker"] = "XLK"
    row["sector_etf_window_return"] = 0.03
    row["sector_excess_return"] = 0.03  # forward_return 0.06 − 0.03
    row["outcome_benchmark_version"] = "sector-etf-v1-sic2-spdr-v1"
    _write_parquet(tmp_path, "2026-05-27", [row])
    rebuild_from_parquet(tmp_path)

    outcome = LadderOutcome.objects.get(ticker="NVDA")
    assert outcome.sector_etf_ticker == "XLK"
    assert outcome.sector_etf_window_return == pytest.approx(0.03)
    assert outcome.sector_excess_return == pytest.approx(0.03)
    assert outcome.outcome_benchmark_version == "sector-etf-v1-sic2-spdr-v1"


@pytest.mark.django_db
def test_ingest_sector_excess_defaults_when_columns_absent(tmp_path: Path):
    """A parquet WITHOUT the sector-excess columns ingests floats NULL, tokens ''."""
    row = _terminal_row("OLDROW", excess=0.01)
    for col in (
        "sector_etf_ticker",
        "sector_etf_window_return",
        "sector_excess_return",
        "outcome_benchmark_version",
    ):
        row.pop(col, None)
    _write_parquet(tmp_path, "2026-05-27", [row])
    rebuild_from_parquet(tmp_path)

    outcome = LadderOutcome.objects.get(ticker="OLDROW")
    assert outcome.sector_etf_ticker == ""
    assert outcome.sector_etf_window_return is None
    assert outcome.sector_excess_return is None
    assert outcome.outcome_benchmark_version == ""


@pytest.mark.django_db
def test_ingest_round_trips_split_invalidated_with_null_realized_r(tmp_path: Path):
    """A SPLIT_INVALIDATED terminal row (implausible-guard redesign, #1090)
    round-trips through the ingest untouched.

    The replay window crossed a corporate action, so the row is terminal with
    ``realized_r`` NULL (the NO_FILL null convention). The parquet also carries
    the guard provenance columns (``guard_disposition`` /
    ``guard_config_version``), which are parquet-only telemetry — the ingest
    must drop them silently, not crash on the unknown columns.
    """
    row = _terminal_row("MQ", excess=None)
    row["ladder_classification"] = "SPLIT_INVALIDATED"
    row["realized_r"] = None
    row["open_r"] = None
    row["realized_return_pct_of_book"] = None
    row["guard_disposition"] = "split_invalidated"
    row["guard_config_version"] = "2026-08-23"
    _write_parquet(tmp_path, "2026-05-27", [row])
    rebuild_from_parquet(tmp_path)

    outcome = LadderOutcome.objects.get(ticker="MQ")
    assert outcome.ladder_classification == "SPLIT_INVALIDATED"
    assert outcome.terminal is True
    assert outcome.realized_r is None
    assert outcome.open_r is None

    meta = DayMetaLadderOutcome.objects.get(brief_date=dt.date(2026, 5, 27))
    assert meta.n_terminal == 1


def _write_watermark(store_dir: Path, completed_at: float) -> None:
    (store_dir / ".ingest_watermark.json").write_text(json.dumps({"completed_at": completed_at}))


@pytest.mark.django_db
def test_ingest_skips_parquet_newer_than_watermark_and_preserves_prior_data(tmp_path: Path):
    # A settled first run: parquet WITH benchmark + watermark strictly newer than it.
    _write_parquet(tmp_path, "2026-07-17", [_terminal_row("BIO", excess=0.05)])
    p = tmp_path / "2026-07-17.parquet"
    _write_watermark(tmp_path, p.stat().st_mtime + 10.0)
    r1 = rebuild_from_parquet(tmp_path)
    assert dt.date(2026, 7, 17) in r1.rebuilt_dates

    # Simulate a MID-RUN rewrite: rewrite the CONTENT to the half-state the race
    # captures (fresh terminal, benchmark NOT yet computed -> None), then bump mtime
    # past the unchanged watermark. Rewriting content (not just os.utime) is what
    # makes the assertion falsifiable: if the gate failed, the None would overwrite.
    _write_parquet(tmp_path, "2026-07-17", [_terminal_row("BIO", excess=None)])
    newer = p.stat().st_mtime + 100.0
    os.utime(p, (newer, newer))
    r2 = rebuild_from_parquet(tmp_path)
    assert dt.date(2026, 7, 17) in r2.unsettled_dates
    assert dt.date(2026, 7, 17) not in r2.rebuilt_dates
    # DB still holds the prior COMPLETE row — the mid-run None was NOT ingested.
    row = LadderOutcome.objects.get(brief_date="2026-07-17", ticker="BIO")
    assert row.market_excess_return == pytest.approx(0.05)


@pytest.mark.django_db
def test_force_bypasses_the_settled_gate(tmp_path: Path):
    # An operator hand-fixed a date's parquet (mtime now ahead of a stale watermark);
    # --force must rebuild it anyway, per its documented "ignore the gate" contract.
    _write_parquet(tmp_path, "2026-07-17", [_terminal_row("BIO", excess=0.05)])
    p = tmp_path / "2026-07-17.parquet"
    _write_watermark(tmp_path, p.stat().st_mtime - 10.0)  # parquet is "unsettled"
    result = rebuild_from_parquet(tmp_path, force=True)
    assert dt.date(2026, 7, 17) in result.rebuilt_dates
    assert dt.date(2026, 7, 17) not in result.unsettled_dates


@pytest.mark.django_db
def test_mtime_equal_to_watermark_is_settled(tmp_path: Path):
    # Boundary: mtime == watermark counts as settled (strict `>` is unsettled only).
    _write_parquet(tmp_path, "2026-07-17", [_terminal_row("BIO", excess=0.05)])
    p = tmp_path / "2026-07-17.parquet"
    _write_watermark(tmp_path, p.stat().st_mtime)  # exactly equal
    result = rebuild_from_parquet(tmp_path)
    assert dt.date(2026, 7, 17) in result.rebuilt_dates
    assert dt.date(2026, 7, 17) not in result.unsettled_dates


@pytest.mark.django_db
def test_ingest_reingests_after_watermark_advances(tmp_path: Path):
    _write_parquet(tmp_path, "2026-07-17", [_terminal_row("BIO", excess=None)])
    p = tmp_path / "2026-07-17.parquet"
    _write_watermark(tmp_path, p.stat().st_mtime - 10.0)  # parquet is "unsettled"
    r1 = rebuild_from_parquet(tmp_path)
    assert dt.date(2026, 7, 17) in r1.unsettled_dates

    # Run completes: rewrite parquet WITH benchmark, advance watermark past it.
    _write_parquet(tmp_path, "2026-07-17", [_terminal_row("BIO", excess=0.05)])
    _write_watermark(tmp_path, (tmp_path / "2026-07-17.parquet").stat().st_mtime + 10.0)
    r2 = rebuild_from_parquet(tmp_path)
    assert dt.date(2026, 7, 17) in r2.rebuilt_dates
    assert LadderOutcome.objects.get(
        brief_date="2026-07-17", ticker="BIO"
    ).market_excess_return == pytest.approx(0.05)


@pytest.mark.django_db
def test_ingest_without_watermark_falls_back_to_mtime_gate(tmp_path: Path):
    # No sentinel present → existing behaviour (mtime gate only), no regression.
    _write_parquet(tmp_path, "2026-07-17", [_terminal_row("BIO", excess=0.05)])
    result = rebuild_from_parquet(tmp_path)  # no watermark file written
    assert dt.date(2026, 7, 17) in result.rebuilt_dates


@pytest.mark.django_db
def test_ingest_tolerates_malformed_watermark(tmp_path: Path):
    _write_parquet(tmp_path, "2026-07-17", [_terminal_row("BIO", excess=0.05)])
    (tmp_path / ".ingest_watermark.json").write_text("{not json")
    result = rebuild_from_parquet(tmp_path)  # malformed → fallback, still ingests
    assert dt.date(2026, 7, 17) in result.rebuilt_dates


@pytest.mark.django_db
def test_result_carries_the_watermark_it_read(tmp_path: Path):
    _write_parquet(tmp_path, "2026-07-17", [_terminal_row("BIO", excess=0.05)])
    settled_at = (tmp_path / "2026-07-17.parquet").stat().st_mtime + 10.0
    _write_watermark(tmp_path, settled_at)

    assert rebuild_from_parquet(tmp_path).watermark == pytest.approx(settled_at)

    (tmp_path / ".ingest_watermark.json").unlink()
    assert rebuild_from_parquet(tmp_path).watermark is None


# --- the gauges the mirror publishes (#1436) ---------------------------------
#
# AlphalensEdgeStale used to read the mirror's last-success clock, which
# advances on an all-unsettled run (exit 0). The command now publishes what it
# saw — the watermark it read, the refused-date count and the newest brief date
# in the mirror's per-date ledger — so the rules can read the DATA's age.

_METRICS_FILE = "alphalens_domain_edge-mirror.prom"
_WATERMARK_GAUGE = "alphalens_edge_mirror_watermark_timestamp_seconds"
_UNSETTLED_GAUGE = "alphalens_edge_mirror_unsettled_dates"
_NEWEST_GAUGE = "alphalens_edge_mirror_newest_brief_date_timestamp_seconds"


def _midnight_utc(iso_date: str) -> float:
    return dt.datetime.combine(
        dt.date.fromisoformat(iso_date), dt.time(), tzinfo=dt.UTC
    ).timestamp()


def _read_gauges(metrics_dir: Path) -> dict[str, float]:
    text = (metrics_dir / _METRICS_FILE).read_text()
    return {name: float(value) for name, value in (line.split(" ") for line in text.splitlines())}


def _run_mirror(store: Path) -> str:
    out = io.StringIO()
    call_command("rebuild_ladder_outcomes_cache", "--store-dir", str(store), stdout=out)
    return out.getvalue()


@pytest.fixture
def mirror_dirs(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    store = tmp_path / "store"
    metrics = tmp_path / "metrics"
    store.mkdir()
    metrics.mkdir()
    monkeypatch.setenv("ALPHALENS_TEXTFILE_DIR", str(metrics))
    return store, metrics


@pytest.mark.django_db
def test_command_publishes_the_refusal_and_the_frozen_newest_date(mirror_dirs):
    # The 2026-09-13 shape. A settled ingest of D first ...
    store, metrics = mirror_dirs
    first = _write_parquet(store, "2026-09-11", [_terminal_row("BIO", excess=0.05)])
    # The +10 s offset is what makes a gauge that reported wall-clock time
    # (instead of the watermark it read) fail the abs=1.0 comparison below.
    settled_at = first.stat().st_mtime + 10.0
    _write_watermark(store, settled_at)
    _run_mirror(store)
    gauges = _read_gauges(metrics)
    assert gauges[_UNSETTLED_GAUGE] == 0
    assert gauges[_WATERMARK_GAUGE] == pytest.approx(settled_at, abs=1.0)
    assert gauges[_NEWEST_GAUGE] == _midnight_utc("2026-09-11")

    # ... then the killed nightly: D rewritten and D+1 created, both newer than
    # the watermark it never advanced.
    _write_parquet(store, "2026-09-11", [_terminal_row("BIO", excess=None)])
    second = _write_parquet(store, "2026-09-12", [_terminal_row("ACME", excess=0.01)])
    for path in (first, second):
        os.utime(path, (settled_at + 100.0, settled_at + 100.0))

    summary = _run_mirror(store)

    assert "rebuilt=0 skipped=0 deleted=0 unsettled=2 total_rows=0" in summary
    gauges = _read_gauges(metrics)
    assert gauges[_UNSETTLED_GAUGE] == 2
    assert gauges[_WATERMARK_GAUGE] == pytest.approx(settled_at, abs=1.0)
    # The newest date is what Postgres HOLDS, not what the store offers: the
    # refused 2026-09-12 must not move the gauge.
    assert gauges[_NEWEST_GAUGE] == _midnight_utc("2026-09-11")


@pytest.mark.django_db
def test_command_publishes_the_ingested_newest_date_after_a_settled_run(mirror_dirs):
    store, metrics = mirror_dirs
    _write_parquet(store, "2026-09-11", [_terminal_row("BIO", excess=0.05)])
    newest = _write_parquet(store, "2026-09-12", [_terminal_row("ACME", excess=0.01)])
    _write_watermark(store, newest.stat().st_mtime + 10.0)

    summary = _run_mirror(store)

    assert "rebuilt=2 skipped=0 deleted=0 unsettled=0 total_rows=2" in summary
    assert _read_gauges(metrics)[_NEWEST_GAUGE] == _midnight_utc("2026-09-12")


@pytest.mark.django_db
def test_command_counts_a_zero_candidate_day_as_the_newest_date(mirror_dirs):
    # A 0-candidate brief date is an EMPTY parquet: ingested as a ledger row
    # (DayMetaLadderOutcome) with no outcome rows. The store advanced, so the
    # gauge must say so — reading max(brief_date) off LadderOutcome would not.
    store, metrics = mirror_dirs
    _write_parquet(store, "2026-09-11", [_terminal_row("BIO", excess=0.05)])
    empty = store / "2026-09-12.parquet"
    pd.DataFrame({"benchmark_window_return": [], "market_excess_return": []}).to_parquet(
        empty, index=False
    )
    _write_watermark(store, empty.stat().st_mtime + 10.0)

    summary = _run_mirror(store)

    assert "rebuilt=2" in summary
    assert LadderOutcome.objects.filter(brief_date="2026-09-12").count() == 0
    assert _read_gauges(metrics)[_NEWEST_GAUGE] == _midnight_utc("2026-09-12")


@pytest.mark.django_db
def test_unsettled_is_counted_before_the_mtime_gate(tmp_path: Path):
    # An already-mirrored date whose parquet is UNCHANGED, with the watermark
    # moved below its mtime (a restore, or a nightly that never stamped): the
    # date must be reported unsettled, not skipped. A gate that checked the
    # mtime first would call it skipped, and AlphalensEdgeMirrorRefusing reads
    # the unsettled count.
    path = _write_parquet(tmp_path, "2026-09-11", [_terminal_row("BIO", excess=0.05)])
    mtime = path.stat().st_mtime
    _write_watermark(tmp_path, mtime + 10.0)
    first = rebuild_from_parquet(tmp_path)
    assert dt.date(2026, 9, 11) in first.rebuilt_dates

    _write_watermark(tmp_path, mtime - 10.0)
    second = rebuild_from_parquet(tmp_path)

    assert second.unsettled_dates == (dt.date(2026, 9, 11),)
    assert second.skipped_dates == ()


@pytest.mark.django_db
def test_command_without_a_watermark_publishes_zero_and_still_ingests(mirror_dirs):
    store, metrics = mirror_dirs
    _write_parquet(store, "2026-09-11", [_terminal_row("BIO", excess=0.05)])

    summary = _run_mirror(store)

    assert "rebuilt=1" in summary
    assert _read_gauges(metrics)[_WATERMARK_GAUGE] == 0


@pytest.mark.django_db
def test_command_on_an_empty_store_publishes_zero_newest_date(mirror_dirs):
    store, metrics = mirror_dirs

    _run_mirror(store)

    gauges = _read_gauges(metrics)
    assert gauges[_NEWEST_GAUGE] == 0
    assert gauges[_UNSETTLED_GAUGE] == 0


@pytest.mark.django_db
def test_command_without_a_textfile_dir_still_ingests(tmp_path: Path, monkeypatch):
    # The compose file without the mount (or a local run): the ingest is the
    # job; the missing metric channel is a warning, not a failure.
    monkeypatch.delenv("ALPHALENS_TEXTFILE_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    store = tmp_path / "store"
    store.mkdir()
    _write_parquet(store, "2026-09-11", [_terminal_row("BIO", excess=0.05)])

    summary = _run_mirror(store)

    assert summary == "rebuilt=1 skipped=0 deleted=0 unsettled=0 total_rows=1\n"
    assert LadderOutcome.objects.filter(ticker="BIO").count() == 1
    assert not list(tmp_path.rglob("*.prom"))
