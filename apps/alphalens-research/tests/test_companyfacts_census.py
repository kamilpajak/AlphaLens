"""Census of the companyfacts parquet store (#1335, the half PR #1636 deferred).

PR #1636 gave the store a lifetime; its "Known issues" left the watching
undone: "Nothing watches the share of cached tickers past the gate, so the
2026-12-27 wave would still arrive unannounced if this fix regressed."

These tests pin the measurement that gauge rests on. Three things they are
deliberate about:

* **The age is the BINDING leg, not the union.** ``compute_fcff`` needs an OCF
  TTM *and* a capex TTM, and ``compute_ttm`` gates each separately. The store's
  own ``_data_age_days`` takes one max over the UNION of both chains, which
  reads "fresh" for an issuer whose OCF is current and whose capex stopped four
  quarters ago — while ``fcff_yield_pct`` is null anyway. Measured on a 500-CIK
  sample on 2026-11-03: 40 of the 142 CIKs the union measure calls fresh have a
  capex leg already past the gate. ``test_binding_leg_*`` is that case.
* **"The chain is absent" is its own bucket, not the worst band.** It is cause 2
  of the #1335 decomposition — financials, royalty trusts and miners that never
  file ``PaymentsToAcquirePropertyPlantAndEquipment`` — a structural property of
  the universe that no refetch can change. Folding it into ``past_gate`` would
  put a permanent floor under the number a reader watches for movement.
* **The thresholds are the production constants**, imported rather than
  retyped. A census that hardcodes 270 goes quietly wrong the day the gate moves.
"""

from __future__ import annotations

import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from alphalens_pipeline.data.fundamentals import concept_chains as chains
from alphalens_pipeline.data.fundamentals.companyfacts_parquet import SCHEMA
from alphalens_pipeline.data.fundamentals.ttm_aggregator import DEFAULT_TTM_MAX_STALENESS_DAYS
from alphalens_pipeline.data.store.companyfacts_census import (
    CROSSING_HORIZON_DAYS,
    WRITER_LIVENESS_DAYS,
    census_companyfacts_store,
    census_metrics,
)
from alphalens_pipeline.data.store.edgar_fundamentals import REFETCH_DATA_AGE_DAYS

TODAY = dt.date(2026, 11, 3)

OCF = chains.OPERATING_CASH_FLOW[0]
CAPEX = chains.CAPEX[0]


def _rows(*specs: tuple[str, dt.date]) -> pa.Table:
    """A companyfacts table holding one row per (concept, period_end) pair."""
    columns = {
        "taxonomy": ["us-gaap"] * len(specs),
        "concept": [concept for concept, _ in specs],
        "unit": ["USD"] * len(specs),
        "period_start": [end - dt.timedelta(days=90) for _, end in specs],
        "period_end": [end for _, end in specs],
        "val": [1.0] * len(specs),
        "accn": ["0000000000-26-000001"] * len(specs),
        "fy": [end.year for _, end in specs],
        "fp": ["Q1"] * len(specs),
        "form": ["10-Q"] * len(specs),
        "filed_date": [end + dt.timedelta(days=30) for _, end in specs],
        "frame": [None] * len(specs),
    }
    return pa.table(columns, schema=SCHEMA)


class _StoreFixture(unittest.TestCase):
    """A temporary store directory with one parquet per planted CIK."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self._next_cik = 1

    def _cik(self) -> str:
        cik = f"{self._next_cik:010d}"
        self._next_cik += 1
        return cik

    def plant(self, *specs: tuple[str, dt.date], mtime: dt.date = TODAY) -> Path:
        """Write one CIK's parquet from ``specs`` and stamp its mtime."""
        path = self.store / f"{self._cik()}.parquet"
        pq.write_table(_rows(*specs), path)
        stamp = dt.datetime.combine(mtime, dt.time(12, 0)).timestamp()
        os.utime(path, (stamp, stamp))
        return path

    def plant_both_legs(self, age_days: int, *, mtime: dt.date = TODAY) -> Path:
        """A CIK whose binding age is exactly ``age_days``: both legs at that age."""
        end = TODAY - dt.timedelta(days=age_days)
        return self.plant((OCF, end), (CAPEX, end), mtime=mtime)

    def census(self):
        return census_companyfacts_store(self.store, today=TODAY)


class TestAgeBands(_StoreFixture):
    """Which band a CIK lands in, on the binding FCFF leg."""

    def test_a_recent_filer_is_fresh(self) -> None:
        self.plant_both_legs(40)

        self.assertEqual(self.census().fresh, 1)

    def test_the_refetch_threshold_itself_is_still_fresh(self) -> None:
        # `_is_stale` returns False at `age <= REFETCH_DATA_AGE_DAYS`
        # (edgar_fundamentals.py), so the band boundary must agree with the
        # predicate that drives the refetch, not sit one day off it.
        self.plant_both_legs(REFETCH_DATA_AGE_DAYS)

        census = self.census()
        self.assertEqual((census.fresh, census.refetching), (1, 0))

    def test_one_day_past_the_refetch_threshold_is_refetching(self) -> None:
        self.plant_both_legs(REFETCH_DATA_AGE_DAYS + 1)

        census = self.census()
        self.assertEqual((census.fresh, census.refetching), (0, 1))

    def test_the_gate_itself_still_serves_a_ttm(self) -> None:
        # `compute_ttm` rejects on `last_end < asof - max_staleness_days`, which
        # is age > 270 strictly. At exactly 270 the columns still compute, so
        # this CIK is not yet dark.
        self.plant_both_legs(DEFAULT_TTM_MAX_STALENESS_DAYS)

        census = self.census()
        self.assertEqual((census.refetching, census.past_gate), (1, 0))

    def test_one_day_past_the_gate_is_dark(self) -> None:
        self.plant_both_legs(DEFAULT_TTM_MAX_STALENESS_DAYS + 1)

        census = self.census()
        self.assertEqual((census.refetching, census.past_gate), (0, 1))

    def test_the_bands_partition_the_readable_files(self) -> None:
        self.plant_both_legs(40)
        self.plant_both_legs(200)
        self.plant_both_legs(400)
        self.plant((OCF, TODAY - dt.timedelta(days=40)))

        census = self.census()
        self.assertEqual(
            census.fresh + census.refetching + census.past_gate + census.chain_absent,
            census.readable,
        )


class TestBindingLeg(_StoreFixture):
    """The age is the older of the two legs, because FCFF needs both."""

    def test_a_stale_capex_leg_darkens_a_current_ocf_leg(self) -> None:
        # The blind spot in the store's own union measure: max over the union
        # reads 40 days here and calls this issuer fresh, while `capex_ttm`
        # returns None and `fcff_yield_pct` is null regardless.
        self.plant(
            (OCF, TODAY - dt.timedelta(days=40)),
            (CAPEX, TODAY - dt.timedelta(days=400)),
        )

        self.assertEqual(self.census().past_gate, 1)

    def test_a_stale_ocf_leg_darkens_a_current_capex_leg(self) -> None:
        self.plant(
            (OCF, TODAY - dt.timedelta(days=400)),
            (CAPEX, TODAY - dt.timedelta(days=40)),
        )

        self.assertEqual(self.census().past_gate, 1)

    def test_both_legs_are_read_across_their_whole_chain(self) -> None:
        # Each leg is a CHAIN, not one concept. An issuer that migrated to the
        # continuing-operations variant of OCF still files the leg.
        self.plant(
            (chains.OPERATING_CASH_FLOW[1], TODAY - dt.timedelta(days=40)),
            (chains.CAPEX[1], TODAY - dt.timedelta(days=40)),
        )

        self.assertEqual(self.census().fresh, 1)


class TestChainAbsent(_StoreFixture):
    """A leg the issuer never files is its own bucket, not the worst band."""

    def test_no_capex_concept_is_chain_absent(self) -> None:
        self.plant((OCF, TODAY - dt.timedelta(days=40)))

        census = self.census()
        self.assertEqual((census.chain_absent, census.past_gate, census.fresh), (1, 0, 0))

    def test_no_operating_cash_flow_concept_is_chain_absent(self) -> None:
        self.plant((CAPEX, TODAY - dt.timedelta(days=40)))

        self.assertEqual(self.census().chain_absent, 1)

    def test_a_table_of_unrelated_concepts_is_chain_absent(self) -> None:
        self.plant(("Assets", TODAY - dt.timedelta(days=10)))

        self.assertEqual(self.census().chain_absent, 1)

    def test_a_leg_dated_only_in_the_future_cannot_serve(self) -> None:
        # `newest_period_end` drops future rows rather than clamping them, so a
        # lease term dated 2027 must not read as a fresh capex leg.
        self.plant(
            (OCF, TODAY - dt.timedelta(days=40)),
            (CAPEX, TODAY + dt.timedelta(days=400)),
        )

        self.assertEqual(self.census().chain_absent, 1)


class TestFileStates(_StoreFixture):
    """Readable, empty and unreadable are three different answers."""

    def test_an_empty_table_is_empty_not_a_band(self) -> None:
        # A valid SEC answer the store re-asks on REFETCH_EMPTY_INTERVAL_DAYS,
        # not a defect. It has no age, so it cannot sit in an age band.
        pq.write_table(_rows(), self.store / "0000000099.parquet")

        census = self.census()
        self.assertEqual((census.empty, census.readable), (1, 0))

    def test_a_corrupt_file_is_unreadable(self) -> None:
        (self.store / "0000000098.parquet").write_bytes(b"not a parquet file")

        census = self.census()
        self.assertEqual((census.unreadable, census.readable, census.empty), (1, 0, 0))

    def test_the_states_partition_the_store(self) -> None:
        self.plant_both_legs(40)
        pq.write_table(_rows(), self.store / "0000000099.parquet")
        (self.store / "0000000098.parquet").write_bytes(b"not a parquet file")

        census = self.census()
        self.assertEqual(
            census.readable + census.empty + census.unreadable,
            census.total_files,
        )
        self.assertEqual(census.total_files, 3)

    def test_an_interrupted_write_is_not_counted_at_all(self) -> None:
        # `_fetch_and_write` writes a `.parquet.tmp` sibling and renames it over
        # the target, so a SIGKILL mid-write leaves debris no reader ever opens.
        # Counting it as unreadable would report corruption that is not there.
        self.plant_both_legs(40)
        (self.store / "0000000097.parquet.tmp").write_bytes(b"torn write")

        census = self.census()
        self.assertEqual((census.total_files, census.unreadable), (1, 0))

    def test_a_missing_store_directory_reads_as_empty_rather_than_raising(self) -> None:
        # The census is best-effort observability; a store path that does not
        # exist is a real condition the gauges must be able to REPORT (it is
        # the Path.home()-in-Docker failure), not an exception that loses the
        # whole emit.
        census = census_companyfacts_store(self.store / "does-not-exist", today=TODAY)

        self.assertEqual(census.total_files, 0)


class TestCrossingHorizon(_StoreFixture):
    """The cliff a band count cannot see."""

    def test_a_file_crossing_inside_the_horizon_is_counted(self) -> None:
        self.plant_both_legs(DEFAULT_TTM_MAX_STALENESS_DAYS - CROSSING_HORIZON_DAYS + 1)

        self.assertEqual(self.census().crossing_gate_within_horizon, 1)

    def test_a_file_crossing_one_day_outside_the_horizon_is_not(self) -> None:
        self.plant_both_legs(DEFAULT_TTM_MAX_STALENESS_DAYS - CROSSING_HORIZON_DAYS)

        self.assertEqual(self.census().crossing_gate_within_horizon, 0)

    def test_a_file_already_past_the_gate_is_not_crossing(self) -> None:
        # It crossed; it is not about to. Counting it here would make the
        # warning series rise forever and never fall.
        self.plant_both_legs(DEFAULT_TTM_MAX_STALENESS_DAYS + 1)

        self.assertEqual(self.census().crossing_gate_within_horizon, 0)

    def test_the_horizon_counts_a_subset_of_the_refetch_band(self) -> None:
        # Every CIK crossing within the horizon is by construction inside the
        # refetch band, which is what makes the horizon a sharper read of the
        # same population rather than a second, contradictory one.
        self.plant_both_legs(DEFAULT_TTM_MAX_STALENESS_DAYS - 1)
        self.plant_both_legs(REFETCH_DATA_AGE_DAYS + 1)

        census = self.census()
        self.assertEqual(census.refetching, 2)
        self.assertEqual(census.crossing_gate_within_horizon, 1)


class TestWriterLiveness(_StoreFixture):
    """Whether anything is still writing the store, which the bands cannot say."""

    def test_a_recently_written_file_is_counted(self) -> None:
        self.plant_both_legs(40, mtime=TODAY - dt.timedelta(days=WRITER_LIVENESS_DAYS))

        self.assertEqual(self.census().refetched_recently, 1)

    def test_a_file_written_before_the_window_is_not(self) -> None:
        self.plant_both_legs(40, mtime=TODAY - dt.timedelta(days=WRITER_LIVENESS_DAYS + 1))

        self.assertEqual(self.census().refetched_recently, 0)

    def test_liveness_is_independent_of_the_age_bands(self) -> None:
        # The measure that would have gone to zero the day #1636 regressed,
        # months before any band moved: a frozen store keeps serving fresh data
        # for ~150 days before a single file reaches the refetch threshold.
        self.plant_both_legs(400, mtime=TODAY)

        census = self.census()
        self.assertEqual((census.past_gate, census.refetched_recently), (1, 1))

    def test_an_unreadable_file_still_counts_as_written(self) -> None:
        # It answers "did the writer run", not "is the data any good".
        (self.store / "0000000098.parquet").write_bytes(b"not a parquet file")

        self.assertEqual(self.census().refetched_recently, 1)


class TestCensusMetrics(_StoreFixture):
    """The gauge surface: names, labels, and every series on every run."""

    def test_every_label_value_is_emitted_even_when_the_bucket_is_zero(self) -> None:
        # Zero-init, the house convention. A band that vanishes from the file
        # when it empties makes `absent()` unusable and a drop indistinguishable
        # from a dead emitter.
        self.plant_both_legs(40)

        metrics = census_metrics(self.census())
        for band in ("fresh", "refetching", "past_gate", "chain_absent"):
            self.assertIn(f'alphalens_companyfacts_fcff_age_band{{band="{band}"}}', metrics)
        for state in ("readable", "empty", "unreadable"):
            self.assertIn(f'alphalens_companyfacts_cached_files{{state="{state}"}}', metrics)

    def test_the_band_gauge_carries_the_counts(self) -> None:
        self.plant_both_legs(40)
        self.plant_both_legs(400)

        metrics = census_metrics(self.census())
        self.assertEqual(metrics['alphalens_companyfacts_fcff_age_band{band="fresh"}'], 1)
        self.assertEqual(metrics['alphalens_companyfacts_fcff_age_band{band="past_gate"}'], 1)

    def test_the_horizon_label_states_the_window(self) -> None:
        # The threshold lives in a label, not in the metric name, so a second
        # horizon can be added later without renaming a series.
        self.plant_both_legs(DEFAULT_TTM_MAX_STALENESS_DAYS - 1)

        metrics = census_metrics(self.census())
        key = f'alphalens_companyfacts_crossing_gate_files{{within_days="{CROSSING_HORIZON_DAYS}"}}'
        self.assertEqual(metrics[key], 1)

    def test_the_liveness_label_states_its_own_window(self) -> None:
        self.plant_both_legs(40, mtime=TODAY)

        metrics = census_metrics(self.census())
        key = f'alphalens_companyfacts_refetched_files{{within_days="{WRITER_LIVENESS_DAYS}"}}'
        self.assertEqual(metrics[key], 1)

    def test_every_value_is_an_integer_count(self) -> None:
        # No ratios. A share cannot tell the numerator rising from the store
        # collapsing, and PromQL divides for free.
        self.plant_both_legs(40)

        for key, value in census_metrics(self.census()).items():
            self.assertIsInstance(value, int, key)


if __name__ == "__main__":
    unittest.main()
