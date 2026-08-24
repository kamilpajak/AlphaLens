"""The read-only driver behind the #1113 instrument.

Builds a tiny population-ladder store, its briefs and its cached minute bars in a
temp directory, then asserts the emitted envelope: the denominator is surfaced,
the structurally-empty cell says so, and no verdict word appears anywhere. The
comparison this instrument feeds is pre-registered in issue #1115; a driver that
shipped a verdict would pre-empt it.
"""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd
from alphalens_pipeline.paper.calendar import session_on_or_after, session_open_utc
from alphalens_research.diagnostics import fill_partition as fp
from scripts.measure_fill_partition import (
    EXCHANGE,
    PAYLOAD_SCHEMA,
    build_report,
    collect_opportunities,
    main,
    report_payload,
)

BRIEF_DATE = dt.date(2026, 8, 3)
GENERATED_AT = "2026-08-25T00:00:00+00:00"

# Words a #1113 payload must never contain: the verdict belongs to #1115.
FORBIDDEN_TOKENS = ("verdict", "better", "worse", "passes", "decision", "recommend", "conclusion")

TIERS = ((100.0, 20.0), (97.0, 30.0), (95.0, 50.0))
DISASTER_STOP = 90.0


def _trade_setup() -> dict:
    return {
        "status": "OK",
        "suggested_size_pct": 1.0,
        "disaster_stop": DISASTER_STOP,
        "order_ttl_days": 7,
        "entry_tiers": [
            {"limit": limit, "alloc_pct": alloc, "tag": f"E{i + 1}"}
            for i, (limit, alloc) in enumerate(TIERS)
        ],
        "tp_tranches": [{"target": 130.0, "tranche_pct": 100.0, "tag": "tp1"}],
        "atr": 2.0,
    }


def _write_brief(briefs_dir: Path, tickers: list[str]) -> None:
    briefs_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        [
            {
                "ticker": t,
                "theme": "test",
                "verified": True,
                "brief_trade_setup": json.dumps(_trade_setup()),
                "scorer_config_version": "test",
            }
            for t in tickers
        ]
    ).to_parquet(briefs_dir / f"{BRIEF_DATE.isoformat()}.parquet")


def _store_row(ticker: str, **over) -> dict:
    row = {
        "brief_date": BRIEF_DATE,
        "ticker": ticker,
        "plannable": True,
        "terminal": True,
        "ladder_classification": "TP_FULL",
        "realized_r": 1.2,
        "mae_pct": -0.03,
        "stop_distance_pct": 0.10,
        "holding_days_elapsed": 5,
        "market_excess_return": 0.01,
        "sequence_str": "E1->TP1",
    }
    row.update(over)
    return row


def _write_store(store_dir: Path, rows: list[dict]) -> None:
    store_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(store_dir / f"{BRIEF_DATE.isoformat()}.parquet")


def _write_bars(store_dir: Path, ticker: str, lows: list[float]) -> None:
    arrival = session_on_or_after(BRIEF_DATE, EXCHANGE)
    open_ms = int(session_open_utc(arrival, EXCHANGE).timestamp() * 1000)
    bars = [
        {
            "t": open_ms + i * 60_000,
            "o": low + 0.5,
            "h": low + 1.0,
            "l": low,
            "c": low + 0.5,
            "v": 1000.0,
        }
        for i, low in enumerate(lows)
    ]
    bars_dir = store_dir / "bars"
    bars_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(bars).to_parquet(bars_dir / f"{ticker.upper()}_{arrival.isoformat()}.parquet")


class _Fixture:
    def __init__(self, tmp: Path) -> None:
        self.store = tmp / "population_ladders"
        self.briefs = tmp / "thematic_briefs"


class MeasureFillPartitionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.fx = _Fixture(Path(self._tmp.name))

    def _collect(self, arm: str = fp.OVERSHOOT_ARM_MEASURED):
        return collect_opportunities(
            store_dir=self.fx.store,
            briefs_dir=self.fx.briefs,
            fill_model=fp.FILL_MODEL_TOUCH,
            overshoot_arm=arm,
        )

    def _payload(self, arm: str = fp.OVERSHOOT_ARM_MEASURED) -> dict:
        opportunities, coverage = self._collect(arm)
        report = build_report(opportunities, fill_model=fp.FILL_MODEL_TOUCH, overshoot_arm=arm)
        return report_payload(report, coverage=coverage, generated_at=GENERATED_AT)


class TestDriverEnvelope(MeasureFillPartitionTestCase):
    def setUp(self) -> None:
        super().setUp()
        _write_brief(self.fx.briefs, ["AAA", "BBB", "CCC"])
        _write_store(
            self.fx.store,
            [
                _store_row("AAA"),  # E1 only
                _store_row("BBB", ladder_classification="NO_FILL", realized_r=None),
                _store_row("CCC", plannable=False, terminal=False, ladder_classification=None),
            ],
        )
        _write_bars(self.fx.store, "AAA", [99.0, 98.0])
        _write_bars(self.fx.store, "BBB", [101.0, 102.0])
        _write_bars(self.fx.store, "CCC", [99.0])

    def test_the_envelope_surfaces_the_opportunity_denominator(self) -> None:
        payload = self._payload()
        self.assertEqual(payload["schema"], PAYLOAD_SCHEMA)
        self.assertEqual(payload["denominator"]["n_store_rows"], 3)
        self.assertEqual(payload["denominator"]["n_opportunities"], 2)
        self.assertEqual(payload["denominator"]["excluded"][fp.EXCLUDE_NOT_PLANNABLE], 1)
        self.assertEqual(
            payload["denominator"]["n_store_rows"],
            payload["denominator"]["n_opportunities"]
            + sum(payload["denominator"]["excluded"].values()),
        )

    def test_the_never_filled_pick_is_in_the_report(self) -> None:
        payload = self._payload()
        cells = {p["partition"]: p for p in payload["partitions"]}
        self.assertEqual(cells[fp.PARTITION_UNFILLED]["n"], 1)
        self.assertEqual(cells[fp.PARTITION_FIRST_ONLY]["n"], 1)
        self.assertIsNone(cells[fp.PARTITION_UNFILLED]["realised_return_mean"])
        self.assertAlmostEqual(cells[fp.PARTITION_UNFILLED]["forgone_return_mean"], 0.01)

    def test_the_deep_only_cell_is_zero_and_says_it_is_unreachable_offline(self) -> None:
        cells = {p["partition"]: p for p in self._payload()["partitions"]}
        self.assertEqual(cells[fp.PARTITION_DEEP_ONLY]["n"], 0)
        self.assertTrue(cells[fp.PARTITION_DEEP_ONLY]["offline_unreachable"])
        self.assertFalse(cells[fp.PARTITION_FIRST_ONLY]["offline_unreachable"])

    def test_the_envelope_stamps_the_fill_model_and_the_overshoot_arm(self) -> None:
        payload = self._payload()
        self.assertEqual(payload["inputs"]["fill_model"], fp.FILL_MODEL_TOUCH)
        self.assertEqual(payload["inputs"]["overshoot_arm"], fp.OVERSHOOT_ARM_MEASURED)
        self.assertAlmostEqual(payload["inputs"]["overshoot_bps"], fp.ENTRY_TRAIL_OVERSHOOT_BPS)

    def test_the_envelope_carries_no_verdict_word(self) -> None:
        blob = json.dumps(self._payload()).lower()
        for token in FORBIDDEN_TOKENS:
            with self.subTest(token=token):
                self.assertNotIn(token, blob)

    def test_the_envelope_is_json_serialisable_as_it_stands(self) -> None:
        json.dumps(self._payload())  # would raise on a dataclass or a date

    def test_the_conditional_fill_records_are_emitted(self) -> None:
        records = self._payload()["conditional_fills"]
        self.assertEqual(
            [(r["given_tier"], r["then_tier"]) for r in records], [("E1", "E2"), ("E2", "E3")]
        )
        e1 = records[0]
        self.assertEqual(e1["n_given"], 1)
        self.assertEqual(e1["n_then"], 0)
        self.assertAlmostEqual(e1["rate"], 0.0)


class TestDriverCoverage(MeasureFillPartitionTestCase):
    def test_a_row_without_a_cached_bar_path_is_excluded_not_dropped(self) -> None:
        _write_brief(self.fx.briefs, ["AAA"])
        _write_store(self.fx.store, [_store_row("AAA")])
        # deliberately no bars written
        payload = self._payload()
        self.assertEqual(payload["denominator"]["n_store_rows"], 1)
        self.assertEqual(payload["denominator"]["excluded"][fp.EXCLUDE_NO_REPLAY], 1)
        self.assertEqual(payload["coverage"]["rows_without_cached_bars"], 1)

    def test_a_row_whose_brief_is_missing_is_excluded_not_dropped(self) -> None:
        _write_store(self.fx.store, [_store_row("AAA")])  # no brief parquet at all
        payload = self._payload()
        self.assertEqual(payload["denominator"]["n_store_rows"], 1)
        self.assertEqual(payload["denominator"]["excluded"][fp.EXCLUDE_NO_REPLAY], 1)
        self.assertEqual(payload["coverage"]["dates_without_a_brief"], 1)

    def test_a_row_whose_brief_carries_no_entry_tiers_is_excluded_not_dropped(self) -> None:
        self.fx.briefs.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(
            [{"ticker": "AAA", "theme": "t", "verified": True, "brief_trade_setup": None}]
        ).to_parquet(self.fx.briefs / f"{BRIEF_DATE.isoformat()}.parquet")
        _write_store(self.fx.store, [_store_row("AAA")])
        _write_bars(self.fx.store, "AAA", [99.0])
        payload = self._payload()
        self.assertEqual(payload["denominator"]["excluded"][fp.EXCLUDE_NO_REPLAY], 1)
        self.assertEqual(payload["coverage"]["rows_without_entry_tiers"], 1)

    def test_an_empty_store_produces_an_empty_but_well_formed_envelope(self) -> None:
        self.fx.store.mkdir(parents=True, exist_ok=True)
        payload = self._payload()
        self.assertEqual(payload["denominator"]["n_store_rows"], 0)
        self.assertEqual(len(payload["partitions"]), len(fp.PARTITIONS))


class TestDriverIsOfflineOnly(MeasureFillPartitionTestCase):
    def test_the_driver_makes_no_polygon_call(self) -> None:
        _write_brief(self.fx.briefs, ["AAA"])
        _write_store(self.fx.store, [_store_row("AAA")])
        _write_bars(self.fx.store, "AAA", [99.0, 96.0])
        with mock.patch(
            "alphalens_pipeline.data.alt_data.polygon_client.get_default_polygon_client",
            side_effect=AssertionError("the instrument must not fetch"),
        ):
            payload = self._payload()
        self.assertEqual(payload["denominator"]["n_opportunities"], 1)


class TestDeeperTierTimingComesFromTheBars(MeasureFillPartitionTestCase):
    def test_a_deeper_tier_filling_on_a_later_bar_is_recorded_as_later(self) -> None:
        _write_brief(self.fx.briefs, ["AAA"])
        _write_store(self.fx.store, [_store_row("AAA", sequence_str="E1->E2->TP1")])
        _write_bars(self.fx.store, "AAA", [99.0, 96.0])
        e1 = self._payload()["conditional_fills"][0]
        self.assertEqual(e1["n_then"], 1)
        self.assertEqual(e1["n_then_later"], 1)
        self.assertEqual(e1["n_then_same_bar"], 0)

    def test_a_deeper_tier_filling_in_the_same_bar_is_recorded_as_same_bar(self) -> None:
        _write_brief(self.fx.briefs, ["AAA"])
        _write_store(self.fx.store, [_store_row("AAA", sequence_str="E1->E2->TP1")])
        _write_bars(self.fx.store, "AAA", [96.0, 96.0])
        e1 = self._payload()["conditional_fills"][0]
        self.assertEqual(e1["n_then_same_bar"], 1)
        self.assertEqual(e1["n_then_later"], 0)

    def test_the_through_model_refuses_a_tier_the_bar_only_touched(self) -> None:
        _write_brief(self.fx.briefs, ["AAA"])
        _write_store(self.fx.store, [_store_row("AAA")])
        _write_bars(self.fx.store, "AAA", [100.0])  # low EXACTLY at E1
        touch, _ = collect_opportunities(
            store_dir=self.fx.store,
            briefs_dir=self.fx.briefs,
            fill_model=fp.FILL_MODEL_TOUCH,
            overshoot_arm=fp.OVERSHOOT_ARM_MEASURED,
        )
        through, _ = collect_opportunities(
            store_dir=self.fx.store,
            briefs_dir=self.fx.briefs,
            fill_model=fp.FILL_MODEL_THROUGH,
            overshoot_arm=fp.OVERSHOOT_ARM_MEASURED,
        )
        self.assertEqual(touch[0].filled_tiers, ("E1",))
        self.assertEqual(through[0].filled_tiers, ())


class TestDriverCli(MeasureFillPartitionTestCase):
    def test_the_json_mode_prints_exactly_one_json_value(self) -> None:
        _write_brief(self.fx.briefs, ["AAA"])
        _write_store(self.fx.store, [_store_row("AAA")])
        _write_bars(self.fx.store, "AAA", [99.0])
        with mock.patch("sys.stdout") as out:
            code = main(
                [
                    "--store",
                    str(self.fx.store),
                    "--briefs",
                    str(self.fx.briefs),
                    "--json",
                ]
            )
        self.assertEqual(code, 0)
        printed = "".join(c.args[0] for c in out.write.call_args_list)
        payload = json.loads(printed)
        self.assertEqual(payload["schema"], PAYLOAD_SCHEMA)

    def test_an_unknown_overshoot_arm_is_a_usage_error_not_a_traceback(self) -> None:
        with self.assertRaises(SystemExit):
            main(["--store", str(self.fx.store), "--overshoot-arm", "hopeful"])


if __name__ == "__main__":
    unittest.main()
