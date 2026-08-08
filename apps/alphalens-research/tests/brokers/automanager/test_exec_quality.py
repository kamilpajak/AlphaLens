"""Hermetic tests for the offline fill reconciler (build-seq 1b-ii).

``reconcile_fills`` joins the broker's ACTUAL fill (resolved by ``sell_order_id``
off the audit trail) to the decision-side ``tranche_fired`` telemetry line and
computes implementation shortfall. It is PURE — no I/O, no clock — so a crafted
journal + a fake resolver fully pin the record shape, the slippage math, the
sign convention, and the fill_status routing. ``write_exec_quality_parquet`` is
the thin IO wrapper (overwrite-rebuild); it round-trips and writes an
empty-but-typed parquet on empty input.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

import pandas as pd
from alphalens_pipeline.brokers.automanager.exec_quality import (
    EXEC_QUALITY_COLUMNS,
    EXEC_QUALITY_PARQUET,
    ExecQualityRecord,
    reconcile_fills,
    write_exec_quality_parquet,
)
from broker_contract.contract import InstrumentRef, OrderState, OrderStatus


def _tranche_fired_line(
    *,
    uic: int,
    tag: str,
    sell_order_id: str | None,
    decision_bid: float = 82.50,
    decision_ask: float = 82.60,
    decision_mid: float = 82.55,
    target_price: float = 83.00,
    qty: float = 2.0,
    event_time: str | None = "2026-07-20T18:00:00+00:00",
    source: str = "yfinance",
) -> dict[str, Any]:
    telemetry: dict[str, Any] = {
        "decision_bid": decision_bid,
        "decision_ask": decision_ask,
        "decision_mid": decision_mid,
        "spread_abs": decision_ask - decision_bid,
        "target_price": target_price,
        "qty": qty,
        "event_time": event_time,
        "source": source,
        "sell_order_id": sell_order_id,
    }
    return {"kind": "tranche_fired", "uic": uic, "tag": tag, "telemetry": telemetry}


def _order_state(
    order_id: str,
    status: OrderStatus,
    *,
    filled: float = 0.0,
    avg_fill_price: float | None = None,
) -> OrderState:
    return OrderState(
        order_id=order_id,
        status=status,
        instrument=None,
        filled_quantity=filled,
        raw_status="",
        avg_fill_price=avg_fill_price,
    )


class _FakeResolver:
    """A ``SupportsOrderResolution`` stub returning crafted OrderStates by id."""

    def __init__(self, by_id: dict[str, OrderState]) -> None:
        self._by_id = by_id
        self.calls: list[str] = []

    def resolve_order_outcome(self, order_id: str) -> OrderState:
        self.calls.append(order_id)
        return self._by_id[order_id]


class TestReconcileFills(unittest.TestCase):
    def test_filled_below_bid_is_adverse_positive_slippage(self):
        line = _tranche_fired_line(uic=111, tag="tp1", sell_order_id="S1", decision_bid=82.50)
        resolver = _FakeResolver(
            {"S1": _order_state("S1", OrderStatus.FILLED, filled=2.0, avg_fill_price=82.09)}
        )

        records = reconcile_fills([line], resolver)

        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec.uic, 111)
        self.assertEqual(rec.tag, "tp1")
        self.assertEqual(rec.sell_order_id, "S1")
        self.assertEqual(rec.fill_status, "filled")
        self.assertEqual(rec.fill_price, 82.09)
        self.assertEqual(rec.filled_qty, 2.0)
        self.assertEqual(rec.planned_qty, 2)
        self.assertEqual(rec.event_time, "2026-07-20T18:00:00+00:00")
        # Sign: sold BELOW the decision bid => positive (adverse) slippage.
        self.assertAlmostEqual(rec.slippage_abs, 82.50 - 82.09)
        self.assertAlmostEqual(rec.slippage_bps, (82.50 - 82.09) / 82.50 * 1e4)
        self.assertGreater(rec.slippage_abs, 0)

    def test_filled_above_bid_is_favorable_negative_slippage(self):
        line = _tranche_fired_line(uic=222, tag="tp2", sell_order_id="S2", decision_bid=82.50)
        resolver = _FakeResolver(
            {"S2": _order_state("S2", OrderStatus.FILLED, filled=3.0, avg_fill_price=82.90)}
        )

        rec = reconcile_fills([line], resolver)[0]

        self.assertEqual(rec.fill_status, "filled")
        self.assertAlmostEqual(rec.slippage_abs, 82.50 - 82.90)
        self.assertLess(rec.slippage_abs, 0)
        self.assertAlmostEqual(rec.slippage_bps, (82.50 - 82.90) / 82.50 * 1e4)

    def test_non_numeric_uic_line_is_skipped_not_crashed(self):
        # A malformed journal line (non-numeric uic) must be skipped, never
        # crash the offline tool — and must never reach the resolver.
        bad = _tranche_fired_line(uic=111, tag="tp1", sell_order_id="S-BAD")
        bad["uic"] = "not-a-number"
        good = _tranche_fired_line(uic=222, tag="tp1", sell_order_id="S-OK")
        resolver = _FakeResolver(
            {"S-OK": _order_state("S-OK", OrderStatus.FILLED, filled=2.0, avg_fill_price=82.0)}
        )

        records = reconcile_fills([bad, good], resolver)

        self.assertEqual([r.sell_order_id for r in records], ["S-OK"])
        self.assertEqual(resolver.calls, ["S-OK"])  # the bad line never resolved

    def test_filled_without_price_is_honest_none_slippage(self):
        line = _tranche_fired_line(uic=333, tag="tp1", sell_order_id="S3")
        resolver = _FakeResolver(
            {"S3": _order_state("S3", OrderStatus.FILLED, filled=1.0, avg_fill_price=None)}
        )

        rec = reconcile_fills([line], resolver)[0]

        self.assertEqual(rec.fill_status, "filled")
        self.assertIsNone(rec.fill_price)
        self.assertIsNone(rec.slippage_abs)
        self.assertIsNone(rec.slippage_bps)
        # filled_qty is still honestly captured.
        self.assertEqual(rec.filled_qty, 1.0)

    def test_unknown_status_is_unresolved_nulls(self):
        line = _tranche_fired_line(uic=444, tag="tp1", sell_order_id="S4")
        resolver = _FakeResolver({"S4": _order_state("S4", OrderStatus.UNKNOWN)})

        rec = reconcile_fills([line], resolver)[0]

        self.assertEqual(rec.fill_status, "unresolved")
        self.assertIsNone(rec.fill_price)
        self.assertIsNone(rec.filled_qty)
        self.assertIsNone(rec.slippage_abs)
        self.assertIsNone(rec.slippage_bps)

    def test_cancelled_status_is_pending_nulls(self):
        line = _tranche_fired_line(uic=555, tag="tp1", sell_order_id="S5")
        resolver = _FakeResolver({"S5": _order_state("S5", OrderStatus.CANCELLED)})

        rec = reconcile_fills([line], resolver)[0]

        self.assertEqual(rec.fill_status, "pending")
        self.assertIsNone(rec.fill_price)
        self.assertIsNone(rec.filled_qty)
        self.assertIsNone(rec.slippage_abs)

    def test_working_status_is_pending_nulls(self):
        line = _tranche_fired_line(uic=666, tag="tp1", sell_order_id="S6")
        resolver = _FakeResolver({"S6": _order_state("S6", OrderStatus.WORKING)})

        rec = reconcile_fills([line], resolver)[0]

        self.assertEqual(rec.fill_status, "pending")

    def test_line_without_sell_order_id_is_skipped(self):
        line = _tranche_fired_line(uic=777, tag="tp1", sell_order_id=None)
        resolver = _FakeResolver({})

        self.assertEqual(reconcile_fills([line], resolver), [])
        self.assertEqual(resolver.calls, [])

    def test_non_tranche_fired_lines_are_ignored(self):
        planned = {"kind": "planned", "uic": 1, "telemetry": {"sell_order_id": "X"}}
        bare = {"kind": "tranche_fired", "uic": 2, "tag": "t"}  # no telemetry
        resolver = _FakeResolver({})

        self.assertEqual(reconcile_fills([planned, bare], resolver), [])
        self.assertEqual(resolver.calls, [])

    def test_zero_decision_bid_guards_bps(self):
        line = _tranche_fired_line(uic=888, tag="tp1", sell_order_id="S8", decision_bid=0.0)
        resolver = _FakeResolver(
            {"S8": _order_state("S8", OrderStatus.FILLED, filled=1.0, avg_fill_price=10.0)}
        )

        rec = reconcile_fills([line], resolver)[0]

        # decision_bid == 0 -> bps guarded to None; abs is still the raw difference.
        self.assertIsNone(rec.slippage_bps)
        self.assertAlmostEqual(rec.slippage_abs, 0.0 - 10.0)

    def test_mixed_batch_routes_each_line(self):
        lines = [
            _tranche_fired_line(uic=1, tag="tp1", sell_order_id="F"),
            _tranche_fired_line(uic=2, tag="tp1", sell_order_id="U"),
            _tranche_fired_line(uic=3, tag="tp1", sell_order_id=None),
            _tranche_fired_line(uic=4, tag="tp1", sell_order_id="P"),
        ]
        resolver = _FakeResolver(
            {
                "F": _order_state("F", OrderStatus.FILLED, filled=2.0, avg_fill_price=80.0),
                "U": _order_state("U", OrderStatus.UNKNOWN),
                "P": _order_state("P", OrderStatus.REJECTED),
            }
        )

        records = reconcile_fills(lines, resolver)

        self.assertEqual([r.fill_status for r in records], ["filled", "unresolved", "pending"])
        self.assertEqual([r.uic for r in records], [1, 2, 4])

    def test_is_pure_deterministic(self):
        line = _tranche_fired_line(uic=1, tag="tp1", sell_order_id="S1")
        resolver = _FakeResolver(
            {"S1": _order_state("S1", OrderStatus.FILLED, filled=2.0, avg_fill_price=80.0)}
        )
        first = reconcile_fills([line], resolver)
        second = reconcile_fills([line], _FakeResolver(dict(resolver._by_id)))
        self.assertEqual(first, second)


class TestWriteExecQualityParquet(unittest.TestCase):
    def _record(self, **overrides: Any) -> ExecQualityRecord:
        base: dict[str, Any] = {
            "uic": 111,
            "tag": "tp1",
            "sell_order_id": "S1",
            "decision_bid": 82.50,
            "decision_mid": 82.55,
            "target_price": 83.00,
            "planned_qty": 2,
            "event_time": "2026-07-20T18:00:00+00:00",
            "fill_status": "filled",
            "fill_price": 82.09,
            "filled_qty": 2.0,
            "slippage_abs": 0.41,
            "slippage_bps": 49.7,
        }
        base.update(overrides)
        return ExecQualityRecord(**base)

    def test_round_trips_columns_and_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "nested" / "tranche_fills.parquet"
            returned = write_exec_quality_parquet([self._record()], out)

            self.assertEqual(returned, out)
            self.assertTrue(out.exists())
            df = pd.read_parquet(out)
            self.assertEqual(list(df.columns), list(EXEC_QUALITY_COLUMNS))
            self.assertEqual(len(df), 1)
            row = df.iloc[0]
            self.assertEqual(row["uic"], 111)
            self.assertEqual(row["sell_order_id"], "S1")
            self.assertEqual(row["fill_status"], "filled")
            self.assertAlmostEqual(row["fill_price"], 82.09)
            self.assertAlmostEqual(row["slippage_abs"], 0.41)

    def test_empty_input_writes_typed_empty_parquet(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "tranche_fills.parquet"
            write_exec_quality_parquet([], out)

            df = pd.read_parquet(out)
            self.assertEqual(list(df.columns), list(EXEC_QUALITY_COLUMNS))
            self.assertEqual(len(df), 0)

    def test_overwrite_rebuilds_from_given_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "tranche_fills.parquet"
            write_exec_quality_parquet([self._record(uic=1), self._record(uic=2)], out)
            write_exec_quality_parquet([self._record(uic=9)], out)

            df = pd.read_parquet(out)
            self.assertEqual(list(df["uic"]), [9])

    def test_null_price_row_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "tranche_fills.parquet"
            rec = self._record(
                fill_status="unresolved",
                fill_price=None,
                filled_qty=None,
                slippage_abs=None,
                slippage_bps=None,
            )
            write_exec_quality_parquet([rec], out)

            df = pd.read_parquet(out)
            self.assertTrue(pd.isna(df.iloc[0]["fill_price"]))
            self.assertTrue(pd.isna(df.iloc[0]["slippage_abs"]))

    def test_default_path_under_alphalens_home(self):
        self.assertEqual(EXEC_QUALITY_PARQUET.name, "tranche_fills.parquet")
        self.assertIn("exec_quality", EXEC_QUALITY_PARQUET.parts)
        self.assertIn(".alphalens", EXEC_QUALITY_PARQUET.parts)

    def test_empty_and_populated_parquets_share_identical_dtypes(self):
        # The schema must NOT vary by batch: an empty file, a fully-priced file,
        # and an all-null-price file must present identical dtypes so a
        # downstream reader unioning periodic snapshots never sees drift.
        with tempfile.TemporaryDirectory() as tmp:
            empty_out = Path(tmp) / "empty.parquet"
            full_out = Path(tmp) / "full.parquet"
            write_exec_quality_parquet([], empty_out)
            write_exec_quality_parquet(
                [
                    self._record(),
                    self._record(
                        uic=2,
                        fill_status="unresolved",
                        fill_price=None,
                        filled_qty=None,
                        slippage_abs=None,
                        slippage_bps=None,
                    ),
                ],
                full_out,
            )
            empty_df = pd.read_parquet(empty_out)
            full_df = pd.read_parquet(full_out)

            self.assertEqual(list(empty_df.dtypes), list(full_df.dtypes))
            # numeric columns are pinned numeric (the real drift the fix removes)
            self.assertEqual(str(full_df["uic"].dtype), "int64")
            self.assertEqual(str(full_df["slippage_bps"].dtype), "float64")
            self.assertEqual(str(empty_df["slippage_bps"].dtype), "float64")


class TestExecQualityRecordShape(unittest.TestCase):
    def test_is_frozen(self):
        rec = ExecQualityRecord(
            uic=1,
            tag="tp1",
            sell_order_id="S1",
            decision_bid=82.5,
            decision_mid=82.55,
            target_price=83.0,
            planned_qty=2,
            event_time=None,
            fill_status="filled",
            fill_price=82.0,
            filled_qty=2.0,
            slippage_abs=0.5,
            slippage_bps=60.6,
        )
        with self.assertRaises(AttributeError):
            rec.fill_price = 1.0  # type: ignore[misc]

    def test_instrument_ref_import_smoke(self):
        # Guard the contract import path stays valid (module-level dependency).
        self.assertTrue(hasattr(InstrumentRef, "__dataclass_fields__"))


if __name__ == "__main__":
    unittest.main()
