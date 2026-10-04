"""The ``broker trades`` rules a mutation run found unpinned (#1701).

Each test names the memo rule it pins. A mutation run on the first version of
the builder changed each rule below without a failing test; the real-shape
tests use the LIVE fixture (``trades_fixture``), and ``synthetic_*`` helpers
build a shape LIVE never produced and say so.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
from typing import Any

from tests.brokers.automanager.test_broker_trades import (
    LULU_G2,
    QUBT,
    VST,
    VST_EXIT_TRADE,
    VST_STOP,
    _codes,
    _TradesCase,
)
from tests.brokers.automanager.trades_fixture import (
    FIXTURE_DIR,
    VENUE,
    FakeFillHistory,
    install_journals,
    trade,
    value,
    venue_without,
)

VST_UIC = 7300542
VST_STOP_PLACED_TS = 1790775418.9622772
VST_STOP_FILLED_TS = 1790863196.6686692
VST_FINAL_FILL = dt.datetime(2026, 10, 1, 13, 59, 21, 248000, tzinfo=dt.UTC)
UBER = "UBER:2026-09-08"
ALB_G2 = "ALB:2026-09-08-g2"
GME = "GME:2026-08-27"
SMG = "SMG:2026-08-19"
OLN = "OLN:2026-08-16"


def _is_vst_trailed(record: dict[str, Any]) -> bool:
    return record.get("kind") == "trailed" and record.get("uic") == VST_UIC


def _edit_vst_trailed(**changes: Any):
    def edit(journal: str, record: dict[str, Any]) -> dict[str, Any]:
        if _is_vst_trailed(record):
            return {**record, **{k: v(record) if callable(v) else v for k, v in changes.items()}}
        return record

    return edit


class RealOutcomeRules(_TradesCase):
    """§4.5 and §5 on the real LIVE picks, broker mode."""

    def setUp(self) -> None:
        super().setUp()
        install_journals(self.home)
        self.report = self.build()

    def test_uber_pnl_is_on_the_attributed_quantity_not_the_fill(self) -> None:
        # §4.5: the stop sold 8, the pick held 4; P&L is on the 4.
        outcome = trade(self.report, UBER)["outcome"]
        self.assertEqual(value(outcome["exit_qty"]), 4.0)
        self.assertAlmostEqual(value(outcome["pnl_cash"]), 4 * (67.99 - 73.105), places=6)

    def test_the_r_uses_spec_disaster_stop_even_with_initial_levels(self) -> None:
        # §5 / replay spec §5.1: GME's plan supplies exit.initial_levels.stop
        # 16.826, which must NOT replace spec.disaster_stop 16.8151.
        record = trade(self.report, GME)
        initial = record["plan"]["exit"]["initial_levels"]["stop"]
        declared = record["plan"]["spec"]["disaster_stop"]
        self.assertNotAlmostEqual(initial, declared, places=4)
        stop = record["outcome"]["denominator_stop"]
        self.assertEqual(value(stop), declared)
        self.assertTrue(stop["ref"].startswith("plan:spec.disaster_stop"), stop["ref"])
        self.assertAlmostEqual(value(record["outcome"]["r_multiple"]), 0.406672, places=6)

    def test_an_open_pick_reports_no_partial_fee_sum(self) -> None:
        # §5: fees of an open pick are null not_closed, not an entry-only sum.
        for key in ("RHI:2026-09-02", "NESR:2026-09-30"):
            outcome = trade(self.report, key)["outcome"]
            for name in ("commission", "exchange_fee", "fx_conversion"):
                with self.subTest(pick=key, fee=name):
                    self.assertIsNone(outcome["fees"][name]["value"])
                    self.assertEqual(outcome["fees"][name]["null_reason"], "not_closed")
            self.assertEqual(outcome["pnl_cash_acct"]["null_reason"], "not_closed")

    def test_holding_time_runs_from_the_first_entry_to_the_last_exit(self) -> None:
        # ALB g2: two entries, two exits (§5 holding_seconds).
        record = trade(self.report, ALB_G2)
        entries = [t["fill"]["venue_time"]["value"] for t in record["entries"] if t["fill"]]
        exits = [e["venue_time"]["value"] for e in record["exits"]]
        self.assertEqual((len(entries), len(exits)), (2, 2))
        self.assertAlmostEqual(value(record["outcome"]["holding_seconds"]), 347325.207, places=3)

    def test_synthetic_the_average_exit_is_weighted_by_attributed_quantity(self) -> None:
        # ALB g2's two stops (3 and 9 shares) both filled at 115.00; move the
        # 3-share one to 116.00 so a plain mean and a weighted mean differ.
        exits = trade(self.report, ALB_G2)["exits"]
        small = next(e["order_id"] for e in exits if value(e["attributed_qty"]) == 3.0)
        venue = venue_without()
        for row in venue["audit"]:
            if row["OrderId"] == small and row["Status"] == "FinalFill":
                row["AveragePrice"] = row["ExecutionPrice"] = 116.0
        record = trade(self.build(FakeFillHistory(venue), pick=ALB_G2), ALB_G2)
        self.assertAlmostEqual(
            value(record["outcome"]["avg_exit_price"]), (3 * 116.0 + 9 * 115.0) / 12, places=9
        )

    def test_entry_orders_are_not_closing_candidates(self) -> None:
        # §4.4: the full LIVE unattributed set; an entry Buy listed here would
        # mean the entry trigger was taken for a closing fill.
        listed = {
            (u["order_id"], value(u["unattributed_qty"]), u["reason"])
            for u in self.report.unattributed_fills
        }
        self.assertEqual(
            listed, {("5440923753", 4.0, "disaster_stop"), ("5446165594", 4.0, "manual_open")}
        )

    def test_smg_take_profit_is_tied_by_the_tranche_fired_line(self) -> None:
        (exit_,) = trade(self.report, SMG)["exits"]
        self.assertEqual(exit_["reason"], "take_profit")
        self.assertEqual(exit_["attribution"], "journal_tie")
        self.assertIn("keeper:tranche_fired sell_order_id=5436772593", exit_["reason_evidence"])

    def test_broker_mode_open_picks_do_not_carry_the_offline_warning(self) -> None:
        for key in ("RHI:2026-09-02", "NESR:2026-09-30"):
            with self.subTest(pick=key):
                record = trade(self.report, key)
                self.assertEqual(record["state"], "open")
                self.assertNotIn("exit_not_in_journal", _codes(record))


class OfflineReasons(_TradesCase):
    """§4.4 offline: reasons come from journal markers only."""

    def _vst_exit(self, **install: Any) -> dict[str, Any]:
        install_journals(self.home, **install)
        (exit_,) = trade(self.build(None, pick=VST), VST)["exits"]
        return exit_

    def test_tranche_fired_gives_take_profit(self) -> None:
        install_journals(self.home)
        report = self.build(None)
        for key in (SMG, OLN):
            with self.subTest(pick=key):
                (exit_,) = trade(report, key)["exits"]
                self.assertEqual(exit_["reason"], "take_profit")
                self.assertEqual(exit_["tp_label"], "TP1")

    def test_a_reanchored_marker_gives_reanchored_stop(self) -> None:
        def edit(journal: str, record: dict[str, Any]) -> dict[str, Any]:
            if _is_vst_trailed(record):
                return {
                    "kind": "reanchored",
                    "uic": VST_UIC,
                    "ts": record["ts"],
                    "stop_price": record["level"],
                }
            return record

        self.assertEqual(self._vst_exit(edit=edit)["reason"], "reanchored_stop")

    def test_a_marker_before_the_stop_was_placed_is_outside_the_window(self) -> None:
        exit_ = self._vst_exit(edit=_edit_vst_trailed(ts=VST_STOP_PLACED_TS - 1.0))
        self.assertIsNone(exit_["reason"])
        self.assertEqual(exit_["reason_null_reason"], "stop_amend_history_unavailable")

    def test_a_marker_after_the_stop_filled_is_outside_the_window(self) -> None:
        exit_ = self._vst_exit(edit=_edit_vst_trailed(ts=VST_STOP_FILLED_TS + 1.0))
        self.assertIsNone(exit_["reason"])

    def test_a_marker_on_another_uic_does_not_match(self) -> None:
        exit_ = self._vst_exit(edit=_edit_vst_trailed(uic=999))
        self.assertIsNone(exit_["reason"])

    def test_a_stop_fill_with_no_placement_line_has_no_window(self) -> None:
        exit_ = self._vst_exit(
            drop=lambda r: r.get("kind") == "stop_placed" and r.get("uic") == VST_UIC
        )
        self.assertIsNone(exit_["reason"])
        self.assertEqual(exit_["reason_null_reason"], "stop_amend_history_unavailable")
        self.assertIn("stop_placed_not_journaled", exit_["reason_evidence"])

    def test_a_retracted_tranche_plan_no_longer_ties_the_fired_line(self) -> None:
        # SMG's tranche_fired is tied to its pick by the governing
        # tranche_plan. A tranche_plan_retracted between them unties it, and
        # the take-profit reaches the pick by the uic rule instead.
        def is_smg_fired(record: dict[str, Any]) -> bool:
            return record.get("kind") == "tranche_fired" and record.get("uic") == 20237

        lines = (FIXTURE_DIR / "standalone_stops.jsonl").read_text(encoding="utf-8").splitlines()
        fired = [r for r in (json.loads(line) for line in lines if line.strip()) if is_smg_fired(r)]
        self.assertEqual(len(fired), 1)
        install_journals(
            self.home,
            drop=is_smg_fired,
            extra={"standalone_stops": [{"kind": "tranche_plan_retracted", "uic": 20237}, *fired]},
        )
        (exit_,) = trade(self.build(None, pick=SMG), SMG)["exits"]
        self.assertEqual(exit_["reason"], "take_profit")
        self.assertEqual(exit_["attribution"], "external_reference")
        self.assertNotEqual(exit_["attribution"], "journal_tie")


class OfflineStates(_TradesCase):
    """§4.6 offline."""

    def setUp(self) -> None:
        super().setUp()
        install_journals(self.home)
        self.report = self.build(None)

    def test_partial_journal_exits_on_an_old_pick_are_unresolved_not_closed(self) -> None:
        record = trade(self.report, ALB_G2)
        self.assertGreater(len(record["exits"]), 0)
        self.assertEqual(
            (record["state"], record["state_reason"]), ("unresolved", "compacted_before_snapshots")
        )

    def test_now_bracket_picks_are_unresolved_not_journaled(self) -> None:
        for key in (QUBT, "LAC:2026-08-11", "MP:2026-08-11"):
            with self.subTest(pick=key):
                record = trade(self.report, key)
                self.assertEqual(
                    (record["state"], record["state_reason"]), ("unresolved", "not_journaled")
                )
                self.assertEqual(record["outcome"]["r_multiple"]["null_reason"], "not_journaled")


def _synthetic_offline_pick(
    *,
    ticker: str = "ZZZ",
    day: str = "2026-10-02",
    side: str | None = None,
    disaster_stop: float = 9.0,
    entry_price: float = 10.0,
    exit_price: float | None = None,
    qty: float = 5.0,
    exit_qty: float | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """synthetic: one pick armed after the LIVE snapshot horizon, filled by a
    trail tier and (``exit_price`` given) closed by its own stop."""
    spec: dict[str, Any] = {"disaster_stop": disaster_stop, "entry_tiers": []}
    if side is not None:
        spec["side"] = side
    crid = f"{ticker}-{day}-entry-t0"
    intent = {
        "instrument": {"mic": "XNYS", "ticker": ticker},
        "meta": {"schema_version": "3", "source": "manual", "trade_date": day},
        "spec": spec,
    }
    extra: dict[str, list[dict[str, Any]]] = {
        "picks": [
            {
                "ticker": ticker,
                "date": day,
                "armed_ts": f"{day}T12:00:00+00:00",
                "status": "armed",
                "intent": intent,
            }
        ],
        "entry_trails": [
            {
                "kind": "watch_open",
                "crid": crid,
                "pick_key": f"{ticker}:{day}",
                "uic": 999,
                "qty": qty,
                "limit": entry_price,
                "tier_index": 0,
                "instrument_currency": "USD",
            },
            {
                "kind": "fired",
                "crid": crid,
                "order_id": "1",
                "avg_price": entry_price,
                "realized_qty": qty,
                "ts": f"{day}T14:00:00+00:00",
            },
        ],
    }
    if exit_price is not None:
        ref = f"{crid}-fire-stop-0"
        placed = dt.datetime.fromisoformat(f"{day}T14:00:05+00:00").timestamp()
        extra["standalone_stops"] = [
            {
                "kind": "stop_placed",
                "order_id": "2",
                "qty": qty,
                "ref": ref,
                "ts": placed,
                "uic": 999,
            },
            {
                "kind": "stop_filled",
                "order_id": "2",
                "qty": exit_qty if exit_qty is not None else qty,
                "avg_price": exit_price,
                "ref": ref,
                "ts": placed + 3600,
                "uic": 999,
                "partial": False,
            },
        ]
    return extra


class SyntheticSideAndRisk(_TradesCase):
    """§5 side sign and non_positive_risk; LIVE has no short and no pick
    entered at or beyond its stop."""

    def test_synthetic_short_pick_signs_pnl_and_risk(self) -> None:
        install_journals(
            self.home,
            extra=_synthetic_offline_pick(side="short", disaster_stop=13.0, exit_price=12.0),
        )
        record = trade(self.build(None, pick="ZZZ:2026-10-02"), "ZZZ:2026-10-02")
        outcome = record["outcome"]
        self.assertEqual(record["side"], "short")
        self.assertEqual(record["state"], "closed")
        self.assertAlmostEqual(value(outcome["pnl_cash"]), -10.0, places=9)
        self.assertAlmostEqual(value(outcome["risk_per_share"]), 3.0, places=9)
        self.assertAlmostEqual(value(outcome["r_multiple"]), -10.0 / 15.0, places=9)

    def test_synthetic_long_entered_below_its_stop_has_no_r(self) -> None:
        install_journals(
            self.home, extra=_synthetic_offline_pick(disaster_stop=10.5, exit_price=11.0)
        )
        outcome = trade(self.build(None, pick="ZZZ:2026-10-02"), "ZZZ:2026-10-02")["outcome"]
        self.assertAlmostEqual(value(outcome["pnl_cash"]), 5.0, places=9)
        for name in ("risk_per_share", "r_multiple"):
            with self.subTest(field=name):
                self.assertIsNone(outcome[name]["value"])
                self.assertEqual(outcome[name]["null_reason"], "non_positive_risk")

    def test_synthetic_long_entered_at_its_stop_has_no_r(self) -> None:
        install_journals(
            self.home, extra=_synthetic_offline_pick(disaster_stop=10.0, exit_price=11.0)
        )
        outcome = trade(self.build(None, pick="ZZZ:2026-10-02"), "ZZZ:2026-10-02")["outcome"]
        self.assertEqual(outcome["r_multiple"]["null_reason"], "non_positive_risk")

    def test_synthetic_one_share_short_of_the_entry_is_open(self) -> None:
        install_journals(self.home, extra=_synthetic_offline_pick(exit_price=11.0, exit_qty=4.0))
        record = trade(self.build(None, pick="ZZZ:2026-10-02"), "ZZZ:2026-10-02")
        self.assertEqual(record["state"], "open")

    def test_synthetic_with_no_snapshot_an_open_pick_is_unresolved(self) -> None:
        # §4.6: offline, "open" needs a snapshot horizon to stand behind.
        root = install_journals(self.home, extra=_synthetic_offline_pick())
        shutil.rmtree(root / "compaction_snapshots")
        report = self.build(None, pick="ZZZ:2026-10-02")
        self.assertIsNone(report.snapshot_horizon)
        record = trade(report, "ZZZ:2026-10-02")
        self.assertEqual(
            (record["state"], record["state_reason"]), ("unresolved", "compacted_before_snapshots")
        )


class SyntheticNeverFilledReasons(_TradesCase):
    """§4.6 never_filled reasons from the tier terminals."""

    def _two_tier_pick(self, kinds: tuple[str | None, str | None]) -> dict[str, Any]:
        extra = _synthetic_offline_pick()
        watch = extra["entry_trails"][0]
        extra["entry_trails"] = []
        for index, kind in enumerate(kinds):
            crid = f"ZZZ-2026-10-02-entry-t{index}"
            extra["entry_trails"].append({**watch, "crid": crid, "tier_index": index})
            if kind is not None:
                extra["entry_trails"].append({"kind": kind, "crid": crid})
        install_journals(self.home, extra=extra)
        return trade(self.build(None, pick="ZZZ:2026-10-02"), "ZZZ:2026-10-02")

    def test_one_tier_still_open_is_pending(self) -> None:
        record = self._two_tier_pick(("expired", None))
        self.assertEqual((record["state"], record["state_reason"]), ("never_filled", "pending"))

    def test_expired_then_cancelled_reads_the_last_tier(self) -> None:
        record = self._two_tier_pick(("expired", "cancelled"))
        self.assertEqual(record["state_reason"], "cancelled")

    def test_cancelled_then_expired_reads_the_last_tier(self) -> None:
        record = self._two_tier_pick(("cancelled", "expired"))
        self.assertEqual(record["state_reason"], "expired")


def _synthetic_two_picks(
    home: Any,
    *,
    second_entry: str = "2026-09-29T14:00:00.000000Z",
    closes: list[tuple[str, str | None, str, float]],
) -> FakeFillHistory:
    """synthetic: AAA and BBB on uic 555, 5 shares each, and closing Sells
    given as (order_id, reference, time, qty), in audit row order."""
    picks = []
    trails = []
    for ticker, day in (("AAA", "2026-09-28"), ("BBB", "2026-09-29")):
        intent = {
            "instrument": {"mic": "XNYS", "ticker": ticker},
            "meta": {"schema_version": "3", "source": "manual", "trade_date": day},
            "spec": {"disaster_stop": 9.0, "entry_tiers": []},
        }
        picks.append(
            {
                "ticker": ticker,
                "date": day,
                "armed_ts": f"{day}T12:00:00+00:00",
                "status": "armed",
                "intent": intent,
            }
        )
        trails.append(
            {
                "kind": "watch_open",
                "crid": f"{ticker}-{day}-entry-t0",
                "pick_key": f"{ticker}:{day}",
                "uic": 555,
                "qty": 5.0,
                "limit": 10.0,
                "tier_index": 0,
                "instrument_currency": "USD",
            }
        )
    install_journals(home, extra={"picks": picks, "entry_trails": trails})
    venue = venue_without()
    venue["instruments"]["555"] = VENUE["instruments"][str(VST_UIC)]

    def row(order_id: str, ref: str | None, side: str, when: str, qty: float, price: float):
        base = {
            "ActivityTime": when,
            "Amount": qty,
            "AssetType": "Stock",
            "AveragePrice": price,
            "BuySell": side,
            "ExecutionPrice": price,
            "FillAmount": qty,
            "FilledAmount": qty,
            "LogId": order_id,
            "OrderId": order_id,
            "OrderType": "Market",
            "PositionId": f"P-{order_id}",
            "Status": "FinalFill",
            "SubStatus": "Confirmed",
            "Uic": 555,
        }
        if ref:
            base["ExternalReference"] = ref
        return base

    venue["audit"] += [
        row("901", "AAA-2026-09-28-entry-t0-fire", "Buy", "2026-09-28T14:00:00.000000Z", 5, 10.0),
        row("902", "BBB-2026-09-29-entry-t0-fire", "Buy", second_entry, 5, 11.0),
        *(row(oid, ref, "Sell", when, qty, 12.0) for oid, ref, when, qty in closes),
    ]
    return FakeFillHistory(venue)


class SyntheticSharedUic(_TradesCase):
    """§4.4 / §4.5 on two picks that share a uic (LIVE has none)."""

    def test_a_take_profit_with_two_open_lots_is_ambiguous(self) -> None:
        broker = _synthetic_two_picks(
            self.home, closes=[("903", "u555-tp1-sell", "2026-09-30T14:00:00.000000Z", 5)]
        )
        report = self.build(broker)
        for key in ("AAA:2026-09-28", "BBB:2026-09-29"):
            with self.subTest(pick=key):
                record = trade(report, key)
                (exit_,) = record["exits"]
                self.assertIsNone(exit_["attributed_qty"]["value"])
                self.assertEqual(exit_["attributed_qty"]["null_reason"], "ambiguous_attribution")
                self.assertIn("ambiguous_attribution", _codes(record))
                self.assertEqual(record["state"], "open")

    def test_a_take_profit_whose_uic_differs_from_the_fill_is_unknown(self) -> None:
        broker = _synthetic_two_picks(
            self.home, closes=[("903", "u777-tp1-sell", "2026-09-30T14:00:00.000000Z", 5)]
        )
        aaa = trade(self.build(broker), "AAA:2026-09-28")
        (exit_,) = aaa["exits"]
        self.assertEqual(exit_["reason"], "unknown")
        self.assertIn("unclaimed_reference:u777-tp1-sell", exit_["reason_evidence"])

    def test_a_closing_fill_before_a_lot_opened_cannot_close_that_lot(self) -> None:
        # 903 sells 10 at 14:00 on 09-30, before BBB's entry at 15:00: only
        # AAA's 5 can be closed; the other 5 are listed, BBB keeps its lot.
        broker = _synthetic_two_picks(
            self.home,
            second_entry="2026-09-30T15:00:00.000000Z",
            closes=[("903", None, "2026-09-30T14:00:00.000000Z", 10)],
        )
        report = self.build(broker)
        self.assertEqual(trade(report, "BBB:2026-09-29")["exits"], [])
        self.assertEqual(trade(report, "BBB:2026-09-29")["state"], "open")
        self.assertEqual(trade(report, "AAA:2026-09-28")["state"], "closed")

    def test_closing_fills_are_taken_in_venue_time_order_not_row_order(self) -> None:
        # 904 (10-01) comes first in the audit rows, 903 (09-30 14:00, before
        # BBB's entry) second. In time order 903 closes AAA and 904 closes BBB.
        broker = _synthetic_two_picks(
            self.home,
            second_entry="2026-09-30T15:00:00.000000Z",
            closes=[
                ("904", None, "2026-10-01T14:00:00.000000Z", 5),
                ("903", None, "2026-09-30T14:00:00.000000Z", 5),
            ],
        )
        report = self.build(broker)
        self.assertEqual([e["order_id"] for e in trade(report, "AAA:2026-09-28")["exits"]], ["903"])
        self.assertEqual([e["order_id"] for e in trade(report, "BBB:2026-09-29")["exits"]], ["904"])

    def test_a_position_link_shared_by_two_picks_does_not_decide(self) -> None:
        broker = _synthetic_two_picks(
            self.home, closes=[("903", None, "2026-09-30T14:00:00.000000Z", 5)]
        )
        for row in broker.venue["audit"]:
            if row["OrderId"] in ("901", "902"):
                row["PositionId"] = "P-SHARED"
            if row["OrderId"] == "903":
                row["RelatedPositionId"] = "P-SHARED"
        aaa = trade(self.build(broker), "AAA:2026-09-28")
        self.assertEqual(aaa["exits"][0]["attribution"], "fifo_fallback")

    def test_synthetic_offline_stop_fill_with_two_open_picks_is_ambiguous(self) -> None:
        extra = _synthetic_offline_pick(ticker="AAA", day="2026-10-02")
        other = _synthetic_offline_pick(ticker="BBB", day="2026-10-02")
        for name, lines in other.items():
            extra[name] += lines
        extra["standalone_stops"] = [
            {
                "kind": "stop_filled",
                "order_id": "9",
                "qty": 5.0,
                "avg_price": 12.0,
                "ts": dt.datetime(2026, 10, 2, 16, tzinfo=dt.UTC).timestamp(),
                "uic": 999,
                "partial": False,
            }
        ]
        install_journals(self.home, extra=extra)
        report = self.build(None)
        for key in ("AAA:2026-10-02", "BBB:2026-10-02"):
            with self.subTest(pick=key):
                record = trade(report, key)
                self.assertEqual(
                    (record["state"], record["state_reason"]),
                    ("unresolved", "ambiguous_attribution"),
                )


class SyntheticBrokerPredicates(_TradesCase):
    """§4.4 broker-mode predicates with no LIVE shape."""

    def _vst(self, venue: dict[str, Any] | None = None, **install: Any) -> dict[str, Any]:
        install_journals(self.home, **install)
        return trade(self.build(FakeFillHistory(venue or venue_without()), pick=VST), VST)

    def test_a_trailed_marker_after_the_final_fill_is_outside_the_window(self) -> None:
        after = (VST_FINAL_FILL + dt.timedelta(seconds=1)).timestamp()
        (exit_,) = self._vst(edit=_edit_vst_trailed(ts=after))["exits"]
        self.assertEqual(exit_["reason"], "stop_moved_kind_unknown")

    def test_a_marker_one_and_a_half_ticks_away_does_not_match(self) -> None:
        def level(record: dict[str, Any]) -> float:
            return 138.835 if record["level"] == 138.824 else record["level"]

        (exit_,) = self._vst(edit=_edit_vst_trailed(level=level))["exits"]
        self.assertEqual(exit_["reason"], "stop_moved_kind_unknown")

    def test_a_marker_on_another_uic_does_not_match(self) -> None:
        (exit_,) = self._vst(edit=_edit_vst_trailed(uic=999))["exits"]
        self.assertEqual(exit_["reason"], "stop_moved_kind_unknown")

    def test_without_a_tick_no_marker_can_match(self) -> None:
        venue = venue_without()
        del venue["instruments"][str(VST_UIC)]
        (exit_,) = self._vst(venue)["exits"]
        self.assertEqual(exit_["reason"], "stop_moved_kind_unknown")

    def test_a_quantity_only_amend_after_a_move_is_not_another_move(self) -> None:
        venue = venue_without()
        last = [r for r in venue["audit"] if r["OrderId"] == VST_STOP and r["Status"] == "Changed"][
            -1
        ]
        venue["audit"].append(
            {**last, "ActivityTime": "2026-10-01T13:45:00.000000Z", "Amount": 5.0}
        )
        (exit_,) = self._vst(venue)["exits"]
        moves = [e for e in exit_["reason_evidence"] if e.startswith("audit:Changed")]
        self.assertEqual(len(moves), 4)

    def test_a_bracket_stop_reference_names_the_bracket_pick(self) -> None:
        crid = "bbbbbbbb-0000-0000-0000-000000000000"
        venue = venue_without()
        for row in venue["audit"]:
            if row["OrderId"] == VST_STOP:
                row["ExternalReference"] = f"{crid}-stop-0"
        submission = {
            "ticker": "VST",
            "trade_date": "2026-09-21",
            "brackets": [
                {"client_request_id": crid, "entry_order_id": "1", "exit_order_ids": [], "qty": 1}
            ],
        }
        record = self._vst(venue, extra={"submissions": [submission]})
        (exit_,) = record["exits"]
        self.assertEqual(exit_["attribution"], "external_reference")
        self.assertEqual(exit_["reason"], "trailed_stop")

    def test_a_limit_bracket_child_is_a_take_profit(self) -> None:
        venue = venue_without()
        for row in venue["audit"]:
            if row["OrderId"] == VST_STOP:
                row.pop("ExternalReference", None)
                row["OrderType"] = "Limit"
        submission = {
            "ticker": "VST",
            "trade_date": "2026-09-21",
            "brackets": [
                {
                    "client_request_id": "cccccccc-0000-0000-0000-000000000000",
                    "entry_order_id": "1",
                    "exit_order_ids": [VST_STOP],
                    "qty": 1,
                }
            ],
        }
        (exit_,) = self._vst(venue, extra={"submissions": [submission]})["exits"]
        self.assertEqual(exit_["attribution"], "journal_tie")
        self.assertEqual(exit_["reason"], "take_profit")

    def test_a_fill_before_the_first_entry_is_not_a_candidate(self) -> None:
        venue = venue_without()
        venue["audit"].append(
            {
                **next(
                    r
                    for r in venue["audit"]
                    if r["OrderId"] == VST_STOP and r["Status"] == "FinalFill"
                ),
                "OrderId": "777",
                "ExternalReference": None,
                "ActivityTime": "2026-09-30T13:00:00.000000Z",
            }
        )
        install_journals(self.home)
        report = self.build(FakeFillHistory(venue), pick=VST)
        self.assertNotIn("777", {u["order_id"] for u in report.unattributed_fills})
        self.assertNotIn("777", {e["order_id"] for e in trade(report, VST)["exits"]})


class SyntheticRejectedAttempts(_TradesCase):
    """§4.4: rejected manual attempts are evidence only on the same side and
    before the fill."""

    def _lulu_evidence(self, **changes: Any) -> list[str]:
        install_journals(self.home)
        venue = venue_without()
        for row in venue["audit"]:
            if row["OrderId"] == "5442081345":
                row.update(changes)
        (exit_,) = trade(self.build(FakeFillHistory(venue), pick=LULU_G2), LULU_G2)["exits"]
        return [e for e in exit_["reason_evidence"] if e.startswith("rejected:")]

    def test_the_real_rejected_sell_is_evidence(self) -> None:
        self.assertEqual(len(self._lulu_evidence()), 1)

    def test_a_rejected_order_on_the_other_side_is_not(self) -> None:
        self.assertEqual(self._lulu_evidence(BuySell="Buy"), [])

    def test_a_rejected_order_after_the_fill_is_not(self) -> None:
        self.assertEqual(self._lulu_evidence(ActivityTime="2026-09-11T18:00:00.000000Z"), [])


class SyntheticBookingsAndPeaks(_TradesCase):
    def test_a_dividend_booking_tied_to_the_trade_is_not_summed(self) -> None:
        install_journals(self.home)
        venue = venue_without()
        source = next(
            b
            for b in venue["bookings"]
            if b["RelatedTradeId"] == VST_EXIT_TRADE and b["BkAmountType"] == "Commission"
        )
        venue["bookings"].append(
            {**source, "BkAmountType": "Corporate Actions - Cash Dividends", "Amount": 5.0}
        )
        record = trade(self.build(FakeFillHistory(venue), pick=VST), VST)
        self.assertEqual(value(record["exits"][0]["fees"]["commission"]), -1.0)
        self.assertNotIn("booking_type_unmapped", _codes(record))

    def test_two_share_amount_rows_at_different_rates_give_the_weighted_rate(self) -> None:
        install_journals(self.home)
        venue = venue_without()
        share = next(
            b
            for b in venue["bookings"]
            if b["RelatedTradeId"] == VST_EXIT_TRADE and b["BkAmountType"] == "Share Amount"
        )
        base = next(t for t in venue["trades"] if t["TradeId"] == VST_EXIT_TRADE)
        venue["trades"].remove(base)
        venue["trades"] += [
            {**base, "TradeId": "T1", "Amount": -3.0},
            {**base, "TradeId": "T2", "Amount": -3.0},
        ]
        venue["bookings"] = [b for b in venue["bookings"] if b["RelatedTradeId"] != VST_EXIT_TRADE]
        venue["bookings"] += [
            {
                **share,
                "RelatedTradeId": "T1",
                "Amount": 400.0,
                "AmountAccountCurrency": 1600.0,
                "ConversionRate": 4.0,
            },
            {
                **share,
                "RelatedTradeId": "T2",
                "Amount": 400.0,
                "AmountAccountCurrency": 1200.0,
                "ConversionRate": 3.0,
            },
        ]
        (exit_,) = trade(self.build(FakeFillHistory(venue), pick=VST), VST)["exits"]
        rate = exit_["realized_fx"]["conversion_rate"]
        self.assertAlmostEqual(value(rate), 2800.0 / 800.0, places=9)
        self.assertEqual(rate["source"], "derived")

    def test_peaks_outside_the_holding_window_are_not_counted(self) -> None:
        peaks = [
            {
                "kind": "trailed",
                "uic": VST_UIC,
                "ts": VST_STOP_PLACED_TS - 86400,
                "level": 1.0,
                "peak": 999.0,
            },
            {
                "kind": "trailed",
                "uic": VST_UIC,
                "ts": (VST_FINAL_FILL + dt.timedelta(hours=1)).timestamp(),
                "level": 1.0,
                "peak": 999.0,
            },
        ]
        install_journals(self.home, extra={"standalone_stops": peaks})
        record = trade(self.build(pick=VST), VST)
        self.assertAlmostEqual(
            value(record["outcome"]["mfe_lower_bound"]), 141.3 - 135.11, places=6
        )
