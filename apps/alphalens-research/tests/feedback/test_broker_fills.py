# apps/alphalens-research/tests/feedback/test_broker_fills.py
"""Contract tests for the broker-fills-v1 loader/validator.

Plain ``unittest.TestCase`` ONLY — CI runs ``unittest discover`` and silently
skips pytest-style classes (PR #806 incident).
"""

import datetime as dt
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from alphalens_pipeline.feedback import broker_fills as bf

_SOURCE_TOKEN = '{"broker":"ibkr-paper","schema":1,"source":"thesis-jsonl+llm-outcomes-jsonl"}'


def _make_row(**overrides) -> dict:
    """One fully-populated broker-fills-v1 row; override any field per test."""
    base = {
        "schema_version": bf.SCHEMA_VERSION,
        "fills_source_version": _SOURCE_TOKEN,
        "export_run_ts_utc": pd.Timestamp("2026-07-17T06:00:00Z"),
        "trade_id_hash": "a" * 64,
        "ticker": "NVDA",
        "market": "US",
        "side": "BUY",
        "strategy": "pilot",
        "scanner_sources": ["alphalens"],
        "source_claims": ["ALPHALENS_FILTERED"],
        "provenance_cohort": bf.PROVENANCE_POST_C1612,
        "fill_ts_utc": pd.Timestamp("2026-07-15T14:31:00Z"),
        "close_ts_utc": pd.Timestamp("2026-07-16T18:00:00Z"),
        "holding_seconds": 98_940,
        "close_reason": "TAKE_PROFIT",
        "entry_price": 100.0,
        "close_price": 108.0,
        "close_price_is_trigger": True,
        "stop_loss_pct": 4.0,
        "take_profit_pct": 8.0,
        "realized_r": 1.9,
        "pnl_pct_of_notional": 7.6,
        "pnl_pct_basis": "FILL_NOTIONAL",
        "commission_pct_of_notional": 0.05,
        "commission_is_modeled": True,
        "entry_fill_vs_thesis_spot_bps": 3.2,
        "joined_streams": "BOTH",
        "record_error": None,
    }
    base.update(overrides)
    return base


class _SnapshotDirCase(unittest.TestCase):
    """Shared temp-dir fixture: write snapshots, load through the public API."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.fills_dir = Path(self._tmp.name)

    def write_snapshot(
        self, rows: list[dict], name: str = "broker-fills-20260717T060000Z.parquet"
    ) -> Path:
        path = self.fills_dir / name
        pd.DataFrame(rows).to_parquet(path)
        return path


class TestSchemaConstants(unittest.TestCase):
    def test_column_list_pins_all_28_contract_columns(self):
        self.assertEqual(len(bf.BROKER_FILLS_V1_COLUMNS), 28)
        self.assertEqual(bf.BROKER_FILLS_V1_COLUMNS[0], "schema_version")
        self.assertEqual(bf.BROKER_FILLS_V1_COLUMNS[-1], "record_error")

    def test_required_columns_subset_of_schema(self):
        self.assertTrue(set(bf.BROKER_FILLS_V1_COLUMNS) >= bf.REQUIRED_COLUMNS)
        self.assertIn("trade_id_hash", bf.REQUIRED_COLUMNS)
        self.assertIn("provenance_cohort", bf.REQUIRED_COLUMNS)

    def test_forbidden_set_anti_rot(self):
        # Positive control: the tripwire can never silently rot to empty or
        # start rejecting a legitimate contract column.
        self.assertGreaterEqual(len(bf.FORBIDDEN_COLUMNS), 10)
        self.assertEqual(bf.FORBIDDEN_COLUMNS & set(bf.BROKER_FILLS_V1_COLUMNS), set())
        for name in ("qty", "quantity", "notional_usd", "account", "order_id"):
            self.assertIn(name, bf.FORBIDDEN_COLUMNS)


class TestRoundTrip(_SnapshotDirCase):
    def test_synthetic_parquet_round_trips_through_loader(self):
        rows = [
            _make_row(),
            _make_row(
                trade_id_hash="b" * 64,
                ticker="INTC",
                side="SELL",
                close_reason="STOP_LOSS",
                realized_r=-1.02,
            ),
        ]
        self.write_snapshot(rows)

        df = bf.load_broker_fills(self.fills_dir)

        self.assertEqual(list(df.columns), list(bf.BROKER_FILLS_V1_COLUMNS))
        self.assertEqual(len(df), 2)
        self.assertEqual(set(df["ticker"]), {"NVDA", "INTC"})
        self.assertEqual(set(df["schema_version"]), {bf.SCHEMA_VERSION})
        self.assertEqual(list(df["scanner_sources"].iloc[0]), ["alphalens"])
        self.assertAlmostEqual(float(df.loc[df["ticker"] == "INTC", "realized_r"].iloc[0]), -1.02)

    def test_lexically_latest_snapshot_wins(self):
        self.write_snapshot(
            [_make_row(ticker="OLD1")], name="broker-fills-20260701T060000Z.parquet"
        )
        self.write_snapshot(
            [_make_row(ticker="NEW1")], name="broker-fills-20260717T060000Z.parquet"
        )

        df = bf.load_broker_fills(self.fills_dir)

        self.assertEqual(list(df["ticker"]), ["NEW1"])


class TestSchemaVersionGate(_SnapshotDirCase):
    def test_unknown_schema_version_rejected_loudly(self):
        self.write_snapshot([_make_row(schema_version="broker-fills-v2")])
        with self.assertRaisesRegex(bf.BrokerFillsContractError, "broker-fills-v2"):
            bf.load_broker_fills(self.fills_dir)

    def test_null_schema_version_rejected(self):
        self.write_snapshot([_make_row(schema_version=None)])
        with self.assertRaisesRegex(bf.BrokerFillsContractError, "schema_version"):
            bf.load_broker_fills(self.fills_dir)


class TestPrivacyInvariant(_SnapshotDirCase):
    def test_forbidden_columns_rejected_positive_control(self):
        # A mis-built export carrying private data MUST fail validation — this
        # is the anti-rot positive control for the tripwire.
        for forbidden in ("qty", "notional_usd", "account", "realized_pnl", "order_id"):
            with self.subTest(column=forbidden):
                row = _make_row()
                row[forbidden] = 123.0
                self.write_snapshot([row])
                with self.assertRaisesRegex(bf.BrokerFillsContractError, "forbidden"):
                    bf.load_broker_fills(self.fills_dir)

    def test_forbidden_check_is_case_insensitive(self):
        row = _make_row()
        row["Quantity"] = 10
        self.write_snapshot([row])
        with self.assertRaisesRegex(bf.BrokerFillsContractError, "forbidden"):
            bf.load_broker_fills(self.fills_dir)

    def test_unknown_column_under_v1_rejected(self):
        # Defense in depth: a column the tripwire doesn't know by name is still
        # a contract breach under v1 (column add requires a schema bump).
        row = _make_row()
        row["ledger_cash_balance"] = 1.0
        self.write_snapshot([row])
        with self.assertRaisesRegex(bf.BrokerFillsContractError, "unknown column"):
            bf.load_broker_fills(self.fills_dir)


class TestProvenanceCohort(_SnapshotDirCase):
    def test_null_cohort_outcomes_only_normalizes_to_no_entry_record(self):
        self.write_snapshot(
            [
                _make_row(
                    provenance_cohort=None,
                    joined_streams="OUTCOMES_ONLY",
                    strategy=None,
                    scanner_sources=None,
                    source_claims=None,
                )
            ]
        )
        df = bf.load_broker_fills(self.fills_dir)
        self.assertEqual(df["provenance_cohort"].iloc[0], bf.PROVENANCE_NO_ENTRY_RECORD)

    def test_null_cohort_with_provenance_keys_normalizes_to_post_c1612(self):
        self.write_snapshot(
            [_make_row(provenance_cohort=None, scanner_sources=[], source_claims=[])]
        )
        df = bf.load_broker_fills(self.fills_dir)
        # Empty list is a VALUE (post-C1612 "genuinely no sources"), not null.
        self.assertEqual(df["provenance_cohort"].iloc[0], bf.PROVENANCE_POST_C1612)

    def test_null_cohort_entry_without_keys_normalizes_to_pre_c1612(self):
        self.write_snapshot(
            [_make_row(provenance_cohort=None, scanner_sources=None, source_claims=None)]
        )
        df = bf.load_broker_fills(self.fills_dir)
        self.assertEqual(df["provenance_cohort"].iloc[0], bf.PROVENANCE_PRE_C1612)

    def test_unknown_cohort_value_rejected(self):
        self.write_snapshot([_make_row(provenance_cohort="C9999_WHAT")])
        with self.assertRaisesRegex(bf.BrokerFillsContractError, "provenance_cohort"):
            bf.load_broker_fills(self.fills_dir)


class TestStructuralGuards(_SnapshotDirCase):
    def test_duplicate_trade_id_hash_rejected(self):
        self.write_snapshot([_make_row(), _make_row(ticker="INTC")])  # same hash
        with self.assertRaisesRegex(bf.BrokerFillsContractError, "duplicate trade_id_hash"):
            bf.load_broker_fills(self.fills_dir)

    def test_missing_required_column_rejected(self):
        row = _make_row()
        del row["close_reason"]
        self.write_snapshot([row])
        with self.assertRaisesRegex(bf.BrokerFillsContractError, "close_reason"):
            bf.load_broker_fills(self.fills_dir)

    def test_missing_nullable_column_backfilled_to_none(self):
        row = _make_row()
        del row["record_error"]
        del row["entry_fill_vs_thesis_spot_bps"]
        self.write_snapshot([row])
        df = bf.load_broker_fills(self.fills_dir)
        self.assertEqual(list(df.columns), list(bf.BROKER_FILLS_V1_COLUMNS))
        self.assertIsNone(df["record_error"].iloc[0])


class TestEmptyDirBehavior(_SnapshotDirCase):
    def test_empty_dir_returns_empty_pinned_frame(self):
        df = bf.load_broker_fills(self.fills_dir)
        self.assertEqual(len(df), 0)
        self.assertEqual(list(df.columns), list(bf.BROKER_FILLS_V1_COLUMNS))

    def test_missing_dir_returns_empty_pinned_frame(self):
        df = bf.load_broker_fills(self.fills_dir / "nope")
        self.assertEqual(len(df), 0)
        self.assertEqual(list(df.columns), list(bf.BROKER_FILLS_V1_COLUMNS))

    def test_non_matching_files_ignored(self):
        (self.fills_dir / "notes.txt").write_text("not a snapshot")
        self.assertIsNone(bf.latest_snapshot_path(self.fills_dir))


class TestCalibrationJoinKeys(_SnapshotDirCase):
    def test_weekday_fill_maps_to_same_session(self):
        self.write_snapshot([_make_row(fill_ts_utc=pd.Timestamp("2026-07-15T14:31:00Z"))])
        df = bf.load_broker_fills(self.fills_dir)
        keys = bf.calibration_join_keys(df)
        self.assertEqual(keys["arrival_session"].iloc[0], dt.date(2026, 7, 15))
        self.assertEqual(keys["ticker"].iloc[0], "NVDA")

    def test_weekend_fill_rolls_to_next_session(self):
        # 2026-07-11 is a Saturday; next XNYS session is Monday 2026-07-13.
        self.write_snapshot([_make_row(fill_ts_utc=pd.Timestamp("2026-07-11T15:00:00Z"))])
        df = bf.load_broker_fills(self.fills_dir)
        keys = bf.calibration_join_keys(df)
        self.assertEqual(keys["arrival_session"].iloc[0], dt.date(2026, 7, 13))

    def test_null_fill_ts_yields_none_not_dropped(self):
        self.write_snapshot(
            [
                _make_row(fill_ts_utc=None),
                _make_row(trade_id_hash="b" * 64, ticker="INTC"),
            ]
        )
        df = bf.load_broker_fills(self.fills_dir)
        keys = bf.calibration_join_keys(df)
        self.assertEqual(len(keys), 2)
        self.assertIsNone(keys["arrival_session"].iloc[0])
        self.assertEqual(keys["arrival_session"].iloc[1], dt.date(2026, 7, 15))


class TestNoAnalysisSurface(unittest.TestCase):
    def test_module_exposes_no_ab_statistics(self):
        # The Cluster #22 look is pre-registered with a HARD N>=30-per-arm
        # floor; the loader module must not grow a way to compute it early.
        public = set(bf.__all__)
        for banned in ("mannwhitney", "median", "ab_test", "compare_arms", "first_look"):
            self.assertFalse(
                any(banned in name.lower() for name in public),
                f"analysis-shaped symbol matching {banned!r} in __all__",
            )


if __name__ == "__main__":
    unittest.main()
