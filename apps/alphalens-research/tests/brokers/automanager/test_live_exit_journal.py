from __future__ import annotations

import datetime as dt
import unittest
from unittest import mock

from alphalens_pipeline.brokers.automanager import control_loop as cl
from alphalens_pipeline.brokers.automanager.live_exit_engine import (
    TrancheExit,
    _fire_telemetry,
    fold_fired_tranches,
    mark_tranche_fired,
)
from broker_contract.price_feed import PricePoint


class TestFoldFiredTranches(unittest.TestCase):
    def test_folds_lines_into_per_uic_tag_sets(self):
        lines = [
            {"kind": "tranche_fired", "uic": 486, "tag": "tp1"},
            {"kind": "tranche_fired", "uic": 486, "tag": "tp2"},
            {"kind": "tranche_fired", "uic": 999, "tag": "tp1"},
            {"kind": "oco_placed", "uic": 486},  # ignored
            {"kind": "tranche_fired", "uic": 486},  # malformed (no tag) ignored
        ]
        out = fold_fired_tranches(lines)
        self.assertEqual(out[486], frozenset({"tp1", "tp2"}))
        self.assertEqual(out[999], frozenset({"tp1"}))

    def test_still_folds_a_line_carrying_a_telemetry_key(self):
        # (test e) The nested telemetry payload must never disturb idempotency:
        # kind/uic/tag stay top-level, so a line carrying an extra "telemetry"
        # key folds exactly like a bare one.
        lines = [
            {
                "kind": "tranche_fired",
                "uic": 486,
                "tag": "tp1",
                "telemetry": {"decision_bid": 16.5, "source": "saxo-live-l1"},
            },
        ]
        out = fold_fired_tranches(lines)
        self.assertEqual(out[486], frozenset({"tp1"}))


class TestMarkTrancheFired(unittest.TestCase):
    def _capture(self):
        records: list[dict] = []
        return records, mock.patch.object(
            cl, "_append_standalone_stop_journal", side_effect=records.append
        )

    def test_with_telemetry_nests_it_under_a_telemetry_key(self):
        # (test a)
        records, patch = self._capture()
        telemetry = {"decision_bid": 16.5, "decision_ask": 16.7, "source": "saxo-live-l1"}
        with patch:
            mark_tranche_fired(486, "tp1", telemetry=telemetry)
        self.assertEqual(
            records,
            [{"kind": "tranche_fired", "uic": 486, "tag": "tp1", "telemetry": telemetry}],
        )

    def test_without_telemetry_writes_the_bare_three_key_line(self):
        # (test b) backward-compat: the historical shape is byte-identical.
        records, patch = self._capture()
        with patch:
            mark_tranche_fired(486, "tp1")
        self.assertEqual(records, [{"kind": "tranche_fired", "uic": 486, "tag": "tp1"}])

    def test_top_level_keys_are_coerced_even_with_telemetry(self):
        records, patch = self._capture()
        with patch:
            mark_tranche_fired("486", "tp1", telemetry={"decision_bid": 1.0})
        self.assertEqual(records[0]["uic"], 486)
        self.assertIsInstance(records[0]["uic"], int)
        self.assertEqual(records[0]["tag"], "tp1")


class TestFireTelemetry(unittest.TestCase):
    def _point(self, event_time):
        return PricePoint(
            uic=486,
            bid=16.5,
            ask=16.7,
            event_time=event_time,
            received_at=dt.datetime(2026, 8, 8, tzinfo=dt.UTC),
            source="saxo-live-l1",
        )

    def test_maps_a_pricepoint_and_tranche_to_the_exact_dict(self):
        # (test d)
        et = dt.datetime(2026, 8, 8, 13, 30, tzinfo=dt.UTC)
        point = self._point(et)
        ex = TrancheExit(tag="tp1", qty=50, target_price=16.0)
        self.assertEqual(
            _fire_telemetry(point, ex, sell_order_id="mkt-2"),
            {
                "decision_bid": 16.5,
                "decision_ask": 16.7,
                "decision_mid": 16.6,
                "spread_abs": point.ask - point.bid,
                "target_price": 16.0,
                "qty": 50,
                "event_time": et.isoformat(),
                "source": "saxo-live-l1",
                "sell_order_id": "mkt-2",
            },
        )

    def test_none_event_time_maps_to_none(self):
        # (test d, edge) an unpublished provider tick stays honestly None.
        point = self._point(None)
        ex = TrancheExit(tag="tp1", qty=50, target_price=16.0)
        self.assertIsNone(_fire_telemetry(point, ex, sell_order_id="mkt-2")["event_time"])

    def test_sell_order_id_is_verbatim_alongside_the_other_eight_fields(self):
        # (test c) sell_order_id is the 9th field; the other 8 stay untouched.
        point = self._point(dt.datetime(2026, 8, 8, 13, 30, tzinfo=dt.UTC))
        ex = TrancheExit(tag="tp1", qty=50, target_price=16.0)
        out = _fire_telemetry(point, ex, sell_order_id="mkt-7")
        self.assertEqual(out["sell_order_id"], "mkt-7")
        for field in (
            "decision_bid",
            "decision_ask",
            "decision_mid",
            "spread_abs",
            "target_price",
            "qty",
            "event_time",
            "source",
        ):
            self.assertIn(field, out)


if __name__ == "__main__":
    unittest.main()
