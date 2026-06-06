"""True end-to-end seam test: pipeline population monitor -> parquet -> edge ingest.

The two sides of the population-ladder contract are otherwise pinned
INDEPENDENTLY: ``test_population_ladder_monitor.py`` (research suite) asserts the
monitor's OWN output columns, and ``test_ingest.py`` (this suite) builds its own
hand-rolled fixture frame and reads it back. A column DRIFT on the writer side
(the monitor renames / drops a column the edge reader needs) slips through both,
because the ingest is column-TOLERANT: it reads only the columns that exist as
ORM fields and coerces every absent one to NULL. So a renamed
``market_excess_return`` would just become silently NULL in production with no
test going red.

This test closes that seam by running BOTH real sides in one process (the CI
``django`` job installs the full workspace, so ``alphalens_pipeline`` is
importable here even though the slim PRODUCTION image deliberately is not):

1. Drive the REAL ``replay_population_ladders`` with SYNTHETIC bars (the ONLY
   mock — the vendor bar source) to write a REAL ``population_ladders/{date}``
   parquet via the real writer.
2. Run the REAL post-hoc enrichments the production CLI runs in order:
   ``enrich_store_with_size_fields`` (hermetic) then
   ``enrich_store_with_benchmark_excess`` (SPY bars injected — same one mock).
3. Run the REAL Django ``rebuild_from_parquet`` over that parquet.
4. Assert ``LadderOutcome`` + ``DayMetaLadderOutcome`` rows materialise with the
   monitor's actual values correctly mapped, and assert ``/v1/edge/summary``
   serves them.

The load-bearing assertion is :meth:`test_writer_columns_cover_every_reader_field`
— it pins that the REAL monitor parquet carries EVERY column the edge reader maps
into the ORM. Drop or rename a reader-needed column on the writer side and that
assertion goes red.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pandas as pd
import pytest
from django.db import models as django_models
from rest_framework.test import APIClient

from alphalens_pipeline.feedback.benchmark_excess import enrich_store_with_benchmark_excess
from alphalens_pipeline.feedback.population_ladder_monitor import (
    enrich_store_with_size_fields,
    replay_population_ladders,
)
from edge.ingest.parquet import rebuild_from_parquet
from edge.models import DayMetaLadderOutcome, LadderOutcome

UTC = dt.UTC

# A plannable OK setup (mirrors the research-suite fixture): single dip-buy entry
# at 100, single TP at 110, disaster stop at 95 -> R = 100 - 95 = 5.
_OK_SETUP = {
    "status": "OK",
    "schema_version": "1.0.0",
    "suggested_size_pct": 2.0,
    "disaster_stop": 95.0,
    "atr": 2.0,
    "order_ttl_days": 7,
    "entry_tiers": [{"limit": 100.0, "alloc_pct": 100.0}],
    "tp_tranches": [{"target": 110.0, "tranche_pct": 100.0}],
}

_BRIEF_DATE = dt.date(2026, 5, 1)
# "now" well past the 42-session hold so the position MATURES to a terminal row
# (a matured row carries realized_r + the size overlay, exercising the columns
# the edge dashboard actually reads).
_NOW = dt.datetime(2026, 7, 8, 7, 0, tzinfo=UTC)


def _write_brief(briefs_dir: Path, brief_date: dt.date, rows: list[dict]) -> None:
    frame_rows = [
        {
            "ticker": r["ticker"],
            "theme": r.get("theme", "ai"),
            "verified": r.get("verified", True),
            "brief_trade_setup": json.dumps(r["setup"]) if r["setup"] is not None else None,
        }
        for r in rows
    ]
    pd.DataFrame(frame_rows).to_parquet(briefs_dir / f"{brief_date.isoformat()}.parquet")


def _time_stop_bars(ticker, start, end):
    """Fill E1 at 100, then drift flat (never TP 110, never SL 95) for the whole
    hold -> a matured TIME_STOP terminal row with realized_r set."""
    minute = 60_000
    base = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    bars = []
    t = base
    while t < end_ms:
        bars.append({"t": t, "o": 100.0, "h": 100.5, "l": 99.5, "c": 100.0, "v": 1000.0})
        t += minute * 60  # hourly stride keeps the bar count modest
    return bars


def _spy_bars(ticker, start, end):
    """Synthetic SPY index path: +1% over the window (open 400 -> close 404)."""
    base = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    day = 86_400_000
    bars, t = [], base
    while t < end_ms:
        bars.append({"t": t, "o": 400.0, "h": 405.0, "l": 399.0, "c": 404.0, "v": 10_000.0})
        t += day
    return bars or [{"t": base, "o": 400.0, "h": 405.0, "l": 399.0, "c": 404.0, "v": 10_000.0}]


def _build_real_store(root: Path) -> Path:
    """Run the REAL monitor + REAL enrichments (vendor bars mocked) -> store dir.

    Mirrors the production CLI order: replay -> size enrichment -> benchmark
    excess. Returns the populated ``population_ladders`` store directory.
    """
    briefs_dir = root / "briefs"
    briefs_dir.mkdir()
    store_dir = root / "population_ladders"

    _write_brief(briefs_dir, _BRIEF_DATE, [{"ticker": "NVDA", "setup": _OK_SETUP, "theme": "ai"}])

    replay_population_ladders(
        briefs_dir,
        end_date=_NOW.date(),
        store_dir=store_dir,
        bar_fetch=_time_stop_bars,
        now=_NOW,
    )
    # Size overlay (hermetic — reads the brief, no vendor call).
    enrich_store_with_size_fields(store_dir, briefs_dir)
    # Benchmark excess (the ONLY other vendor touch — SPY bars injected).
    enrich_store_with_benchmark_excess(store_dir, bar_fetch=_spy_bars, now=_NOW)
    return store_dir


def _reader_payload_field_names() -> set[str]:
    """Every LadderOutcome column the ingest maps FROM a parquet column.

    Derived live from the ORM (so it tracks model edits) the SAME way the ingest's
    ``_payload_fields`` does: concrete fields, minus the synthetic composite pk and
    ``brief_date`` (assigned from the file stem, never a parquet column).
    """
    skip = {"pk", "brief_date"}
    return {
        f.name
        for f in LadderOutcome._meta.get_fields()
        if isinstance(f, django_models.Field) and f.name not in skip
    }


class TestPopulationMonitorToEdgeE2E:
    """Pipeline writer -> parquet -> Django reader, both sides REAL."""

    def test_writer_columns_cover_every_reader_field(self, tmp_path: Path):
        """SEAM CONTRACT: the REAL monitor parquet carries EVERY column the edge
        reader maps into the ORM.

        This is the drift trap. If the monitor renames or drops a column the edge
        reader needs (e.g. ``market_excess_return`` -> ``mkt_excess``), that column
        vanishes from the writer's output set while the reader still expects it,
        and this assertion goes red. The two independent per-side tests cannot
        catch that: the ingest silently coerces an absent column to NULL.
        """
        store_dir = _build_real_store(tmp_path)
        parquet = store_dir / f"{_BRIEF_DATE.isoformat()}.parquet"
        writer_columns = set(pd.read_parquet(parquet).columns)

        reader_fields = _reader_payload_field_names()
        missing = reader_fields - writer_columns
        assert not missing, (
            "monitor parquet is missing columns the edge reader needs (writer<->reader "
            f"schema drift): {sorted(missing)}. Writer columns: {sorted(writer_columns)}"
        )

    def test_market_excess_return_specifically_present(self, tmp_path: Path):
        """Belt-and-braces on the dashboard HEADLINE column.

        ``market_excess_return`` is the edge dashboard's headline metric and is
        written by a SEPARATE module (benchmark_excess) from the rest of the row.
        Pin it by name so a drift on that one column is unmistakable in the
        failure output, independent of the bulk-coverage assertion above.
        """
        store_dir = _build_real_store(tmp_path)
        parquet = store_dir / f"{_BRIEF_DATE.isoformat()}.parquet"
        columns = set(pd.read_parquet(parquet).columns)
        assert "market_excess_return" in columns
        assert "benchmark_window_return" in columns

    @pytest.mark.django_db
    def test_real_round_trip_materialises_outcome_rows(self, tmp_path: Path):
        """Full e2e: REAL monitor parquet -> REAL ingest -> ORM rows + DayMeta.

        Asserts the monitor's ACTUAL values land on the matching ORM columns (not
        just that columns exist), so a wrong-column MAPPING is caught too.
        """
        store_dir = _build_real_store(tmp_path)
        # Read the source-of-truth values straight off the monitor parquet.
        parquet = store_dir / f"{_BRIEF_DATE.isoformat()}.parquet"
        src = pd.read_parquet(parquet).set_index("ticker").loc["NVDA"]

        result = rebuild_from_parquet(store_dir)

        assert result.n_rebuilt == 1
        assert result.total_rows == 1
        assert LadderOutcome.objects.count() == 1

        row = LadderOutcome.objects.get(ticker="NVDA")
        assert row.brief_date == _BRIEF_DATE
        assert row.theme == "ai"
        assert row.plannable is True
        assert row.terminal is True
        # The matured sideways-drifter is a TIME_STOP with realized_r set.
        assert row.ladder_classification == "TIME_STOP"
        assert row.realized_r == pytest.approx(float(src["realized_r"]))
        assert row.forward_return == pytest.approx(float(src["forward_return"]))
        # Benchmark excess flowed through from the SPY enrichment.
        assert row.market_excess_return == pytest.approx(float(src["market_excess_return"]))
        assert row.benchmark_window_return == pytest.approx(float(src["benchmark_window_return"]))
        # Size overlay flowed through from the hermetic size enrichment.
        assert row.suggested_gross_weight_pct == pytest.approx(
            float(src["suggested_gross_weight_pct"])
        )

        meta = DayMetaLadderOutcome.objects.get(brief_date=_BRIEF_DATE)
        assert meta.n_rows == 1
        assert meta.n_terminal == 1
        assert meta.n_plannable == 1

    @pytest.mark.django_db
    def test_edge_summary_api_serves_the_ingested_population(self, tmp_path: Path):
        """The /v1/edge/summary endpoint reflects the e2e-ingested row."""
        store_dir = _build_real_store(tmp_path)
        rebuild_from_parquet(store_dir)

        client = APIClient()
        resp = client.get("/v1/edge/summary")
        assert resp.status_code == 200
        # One matured candidate in the population (below the N>=30 gate, so the
        # benchmark-excess aggregate stays "insufficient" by design — we assert the
        # population count, not the gated stat).
        body = resp.json()
        assert body["n_matured"] >= 1
