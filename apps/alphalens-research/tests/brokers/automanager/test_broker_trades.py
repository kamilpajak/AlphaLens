"""The ``broker trades`` builder: one record per pick (#1701).

Built on the LIVE journals and venue rows of 2026-10-03 (``trades_fixture``).
The golden case is VST:2026-09-21, whose expected values the design memo
derives by hand from the same rows (§9 "Golden case"). The other real shapes
(UBER, QUBT, LULU g2, AMBA, GME, EWTX, RHI, ALB g2) pin the rules of §4.4 and
§4.5 on what actually happened; ``synthetic_*`` tests edit a copy for a shape
LIVE never produced, and say so.

The discrimination checks at the end of the golden case are the ones the memo
requires to be repeated whenever the reason predicate changes.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import unittest
from typing import Any

from alphalens_pipeline.brokers.automanager import trade_alerts
from alphalens_pipeline.brokers.automanager.trades import (
    EXIT_REASON_BY_ALERT_REASON,
    EXIT_REASONS,
    NULL_REASONS,
    WARNING_CODES,
    TradesFilters,
    build_trades,
    format_time,
)

from tests.brokers.automanager.home_isolation import IsolatedHomeTestCase
from tests.brokers.automanager.trades_fixture import (
    NOW,
    SIM_RHI_DIR,
    VENUE,
    FakeFillHistory,
    install_journals,
    trade,
    value,
    venue_without,
)

VST = "VST:2026-09-21"
VST_ENTRY = "5448021994"
VST_STOP = "5448023092"
VST_EXIT_TRADE = "6885451891"


def _codes(record: dict[str, Any]) -> list[str]:
    return [w["code"] for w in record["warnings"]]


class _TradesCase(IsolatedHomeTestCase):
    env = "live"

    def build(
        self,
        broker: Any = "default",
        *,
        pick: str | None = None,
        **filters: Any,
    ):
        if broker == "default":
            broker = FakeFillHistory()
        return build_trades(
            self.env,
            broker=broker,
            filters=TradesFilters(pick=pick, limit=None, **filters),
            now=NOW,
        )


class GoldenVst(_TradesCase):
    """VST:2026-09-21 from the real lines and rows (memo §9 golden table)."""

    def setUp(self) -> None:
        super().setUp()
        install_journals(self.home)
        self.report = self.build(pick=VST)
        self.record = trade(self.report, VST)

    def test_the_entry(self) -> None:
        (tier,) = self.record["entries"]
        fill = tier["fill"]
        self.assertEqual(tier["path"], "trail_watch")
        self.assertEqual(tier["terminal"], "filled")
        self.assertEqual(tier["trigger_order_id"], VST_ENTRY)
        self.assertEqual(value(fill["qty"]), 6.0)
        self.assertEqual(value(fill["price"]), 135.11)
        self.assertEqual(fill["price"]["source"], "venue.audit")
        self.assertEqual(value(fill["venue_time"]), "2026-09-30T13:36:35.916Z")
        self.assertEqual(value(fill["detected_at"]), "2026-09-30T13:36:43.825Z")
        self.assertEqual(value(tier["armed_at"]), "2026-09-30T13:35:50.877Z")
        self.assertEqual(value(tier["planned_limit"]), 135.61)
        self.assertEqual(value(tier["planned_qty"]), 6.0)

    def test_the_placed_stop_is_the_audit_placed_price(self) -> None:
        self.assertEqual(value(self.record["placed_stop"]), 125.85)
        self.assertEqual(self.record["placed_stop"]["source"], "venue.audit")

    def test_the_exit(self) -> None:
        (exit_,) = self.record["exits"]
        self.assertEqual(value(exit_["qty"]), 6.0)
        self.assertEqual(value(exit_["price"]), 138.66)
        self.assertEqual(value(exit_["venue_time"]), "2026-10-01T13:59:21.248Z")
        self.assertEqual(exit_["reason"], "trailed_stop")
        self.assertEqual(value(exit_["stop_level_at_fill"]), 138.82)
        self.assertEqual(exit_["attribution"], "external_reference")
        self.assertEqual(value(exit_["attributed_qty"]), 6.0)
        self.assertIn("audit:Changed 138.82 @2026-10-01T13:40:17.127Z", exit_["reason_evidence"])
        self.assertIn("keeper:trailed 138.824", exit_["reason_evidence"])

    def test_the_outcome(self) -> None:
        outcome = self.record["outcome"]
        self.assertEqual(self.record["state"], "closed")
        self.assertAlmostEqual(value(outcome["pnl_cash"]), 21.30, places=6)
        self.assertAlmostEqual(value(outcome["pnl_pct_of_spent"]), 2.6275, places=4)
        self.assertEqual(value(outcome["denominator"]), 125.85)
        self.assertAlmostEqual(value(outcome["risk_per_share"]), 9.26, places=6)
        self.assertAlmostEqual(value(outcome["r_multiple"]), 0.3834, places=4)
        self.assertAlmostEqual(value(outcome["holding_seconds"]), 87765.332, places=3)
        self.assertEqual(outcome["pnl_cash"]["unit"], "USD")

    def test_the_fees_per_fill_and_for_the_pick(self) -> None:
        entry_fees = self.record["entries"][0]["fill"]["fees"]
        exit_fees = self.record["exits"][0]["fees"]
        self.assertEqual(value(entry_fees["commission"]), -1.0)
        self.assertEqual(value(entry_fees["exchange_fee"]), 0.0)
        self.assertAlmostEqual(value(entry_fees["fx_conversion"]), -7.79, places=6)
        self.assertEqual(value(exit_fees["commission"]), -1.0)
        self.assertEqual(value(exit_fees["exchange_fee"]), -0.02)
        self.assertAlmostEqual(value(exit_fees["fx_conversion"]), -8.05, places=6)
        self.assertEqual(entry_fees["commission"]["unit"], "USD")
        self.assertEqual(entry_fees["fx_conversion"]["unit"], "PLN")
        fees = self.record["outcome"]["fees"]
        self.assertAlmostEqual(value(fees["commission"]), -2.0, places=6)
        self.assertAlmostEqual(value(fees["exchange_fee"]), -0.02, places=6)
        self.assertAlmostEqual(value(fees["fx_conversion"]), -15.84, places=6)

    def test_the_realized_fx_and_the_account_currency_outcome(self) -> None:
        entry_fx = self.record["entries"][0]["fill"]["realized_fx"]
        exit_fx = self.record["exits"][0]["realized_fx"]
        self.assertEqual(value(entry_fx["conversion_rate"]), 3.85078295)
        self.assertEqual(value(entry_fx["share_amount_acct"]), -3121.68)
        self.assertEqual(value(exit_fx["conversion_rate"]), 3.86282872)
        self.assertEqual(value(exit_fx["share_amount_acct"]), 3213.72)
        outcome = self.record["outcome"]
        self.assertAlmostEqual(value(outcome["notional_spent_acct"]), 3121.68, places=6)
        self.assertAlmostEqual(value(outcome["pnl_cash_acct"]), 92.04, places=6)
        self.assertEqual(outcome["pnl_cash_acct"]["unit"], "PLN")

    def test_the_mfe_lower_bound_is_the_highest_trailed_peak(self) -> None:
        self.assertAlmostEqual(
            value(self.record["outcome"]["mfe_lower_bound"]), 141.3 - 135.11, places=6
        )

    def test_the_plan_and_the_lifecycle(self) -> None:
        self.assertEqual(self.record["pick_status"], "armed")
        self.assertEqual(self.record["plan_schema_version"], "3")
        self.assertEqual(self.record["plan_size_shape"], "by_amount")
        self.assertEqual(self.record["plan_source"], "manual")
        self.assertEqual(self.record["side"], "long")
        self.assertEqual(value(self.record["plan_armed_at"]), "2026-09-21T16:12:44.000Z")
        self.assertEqual(self.record["plan"]["intent_id"], "VST:2026-09-21:manual")
        self.assertEqual(value(self.record["instrument"]["uic"]), 7300542)
        self.assertEqual(
            value(self.record["sizing_fx"]["source"]), "saxo-fxspot-uic-47-mid-inverted"
        )

    def test_no_warning_on_the_golden_case(self) -> None:
        self.assertEqual(self.record["warnings"], [])

    def test_the_whole_record_renders_as_strict_json(self) -> None:
        json.dumps(self.report.body(), allow_nan=False)


class DiscriminationCheck(_TradesCase):
    """Memo §9: repeat whenever the reason predicate changes."""

    @staticmethod
    def _is_trailed(record: dict[str, Any]) -> bool:
        return record.get("kind") == "trailed" and record.get("uic") == 7300542

    def test_without_the_trailed_lines_it_is_a_move_of_unknown_kind_never_disaster(self) -> None:
        install_journals(self.home, drop=self._is_trailed)
        (exit_,) = trade(self.build(pick=VST), VST)["exits"]
        self.assertEqual(exit_["reason"], "stop_moved_kind_unknown")

    def test_without_the_moves_too_it_is_the_disaster_stop_whatever_the_fill(self) -> None:
        install_journals(self.home, drop=self._is_trailed)
        venue = venue_without(rows=lambda r: r["OrderId"] == VST_STOP and r["Status"] == "Changed")
        (exit_,) = trade(self.build(FakeFillHistory(venue), pick=VST), VST)["exits"]
        self.assertEqual(exit_["reason"], "disaster_stop")
        self.assertEqual(value(exit_["stop_level_at_fill"]), 125.85)

    def test_offline_without_the_trailed_lines_the_reason_is_null_not_a_guess(self) -> None:
        install_journals(self.home, drop=self._is_trailed)
        (exit_,) = trade(self.build(None, pick=VST), VST)["exits"]
        self.assertIsNone(exit_["reason"])
        self.assertEqual(exit_["reason_null_reason"], "stop_amend_history_unavailable")

    def test_offline_with_the_trailed_lines_it_is_trailed(self) -> None:
        install_journals(self.home)
        (exit_,) = trade(self.build(None, pick=VST), VST)["exits"]
        self.assertEqual(exit_["reason"], "trailed_stop")


class OfflineMode(_TradesCase):
    def setUp(self) -> None:
        super().setUp()
        install_journals(self.home)
        self.report = self.build(None)

    def test_venue_fields_are_null_offline_and_journal_values_say_so(self) -> None:
        record = trade(self.report, VST)
        fill = record["entries"][0]["fill"]
        self.assertEqual(fill["fees"]["commission"]["null_reason"], "offline")
        self.assertEqual(fill["price"]["source"], "keeper.entry_watch")
        self.assertEqual(value(fill["price"]), 135.11)
        self.assertEqual(record["entries"][0]["armed_at"]["null_reason"], "offline")
        self.assertEqual(record["state"], "closed")
        self.assertEqual(self.report.mode, "offline")
        self.assertEqual(self.report.sources["venue.audit"]["status"], "offline")

    def test_offline_holding_time_names_the_keeper_source(self) -> None:
        holding = trade(self.report, VST)["outcome"]["holding_seconds"]
        self.assertTrue(holding["source"].startswith("keeper."), holding)

    def test_picks_older_than_the_horizon_with_lost_exits_are_unresolved(self) -> None:
        # AMBA's take-profit and GME's stop fill were compacted before the
        # first snapshot (memo §10).
        for key in ("AMBA:2026-09-04", "GME:2026-08-27"):
            with self.subTest(pick=key):
                record = trade(self.report, key)
                self.assertEqual(record["state"], "unresolved")
                self.assertEqual(record["state_reason"], "compacted_before_snapshots")
                self.assertEqual(
                    record["outcome"]["pnl_cash"]["null_reason"], "compacted_before_snapshots"
                )

    def test_the_horizon_is_the_earliest_snapshot(self) -> None:
        self.assertEqual(format_time(self.report.snapshot_horizon), "2026-10-01T20:10:03.000Z")


class SyntheticNewerOpenPickOffline(_TradesCase):
    """synthetic: a pick armed after the snapshot horizon with no stop fill."""

    def test_it_is_open_with_the_neutral_warning(self) -> None:
        intent = {
            "instrument": {"mic": "XNYS", "ticker": "ZZZ"},
            "meta": {"schema_version": "3", "source": "manual", "trade_date": "2026-10-02"},
            "spec": {"disaster_stop": 9.0, "entry_tiers": []},
        }
        install_journals(
            self.home,
            extra={
                "picks": [
                    {
                        "ticker": "ZZZ",
                        "date": "2026-10-02",
                        "armed_ts": "2026-10-02T12:00:00+00:00",
                        "status": "armed",
                        "intent": intent,
                    }
                ],
                "entry_trails": [
                    {
                        "kind": "watch_open",
                        "crid": "ZZZ-2026-10-02-entry-t0",
                        "pick_key": "ZZZ:2026-10-02",
                        "uic": 999,
                        "qty": 5.0,
                        "limit": 10.0,
                        "tier_index": 0,
                        "instrument_currency": "USD",
                    },
                    {
                        "kind": "fired",
                        "crid": "ZZZ-2026-10-02-entry-t0",
                        "order_id": "1",
                        "avg_price": 10.0,
                        "realized_qty": 5.0,
                        "ts": "2026-10-02T14:00:00+00:00",
                    },
                ],
            },
        )
        record = trade(self.build(None, pick="ZZZ:2026-10-02"), "ZZZ:2026-10-02")
        self.assertEqual(record["state"], "open")
        self.assertIn("exit_not_in_journal", _codes(record))
        self.assertEqual(record["outcome"]["pnl_cash"]["null_reason"], "not_closed")
        self.assertEqual(value(record["outcome"]["entry_qty"]), 5.0)


class RealShapes(_TradesCase):
    """The rules of §4.4 and §4.5 on what LIVE actually did."""

    def setUp(self) -> None:
        super().setUp()
        install_journals(self.home)
        self.report = self.build()

    def test_uber_four_shares_to_the_pick_the_rest_listed(self) -> None:
        record = trade(self.report, "UBER:2026-09-08")
        (exit_,) = record["exits"]
        self.assertEqual(value(exit_["attributed_qty"]), 4.0)
        self.assertEqual(value(exit_["qty"]), 8.0)
        self.assertEqual(exit_["reason"], "disaster_stop")
        self.assertEqual(record["state"], "closed")
        self.assertIn("exit_qty_exceeds_pick", _codes(record))
        self.assertIn("booking_prorated", _codes(record))
        on_uber = [
            u
            for u in self.report.unattributed_fills
            if u["order_id"] in ("5440923753", "5446165594")
        ]
        by_order = {u["order_id"]: u for u in on_uber}
        self.assertEqual(value(by_order["5440923753"]["unattributed_qty"]), 4.0)
        self.assertEqual(by_order["5440923753"]["reason"], "disaster_stop")
        self.assertEqual(by_order["5446165594"]["reason"], "manual_open")
        self.assertEqual(value(by_order["5446165594"]["price"]), 69.55)
        # The exit's bookings are this pick's by 4/8.
        fees = record["outcome"]["fees"]
        exit_commission = value(exit_["fees"]["commission"])
        entry_commission = sum(value(t["fill"]["fees"]["commission"]) for t in record["entries"])
        self.assertAlmostEqual(
            value(fees["commission"]), entry_commission + exit_commission * 4 / 8, places=6
        )

    def test_qubt_closes_by_the_position_link(self) -> None:
        record = trade(self.report, "QUBT:2026-09-03")
        self.assertEqual(record["plan_size_shape"], "by_percent")
        self.assertEqual(record["plan_schema_version"], "2")
        bracket = [t for t in record["entries"] if t["path"] == "now_bracket"]
        self.assertEqual(len(bracket), 1)
        self.assertEqual(value(bracket[0]["fill"]["qty"]), 33.0)
        (exit_,) = record["exits"]
        self.assertEqual(exit_["reason"], "manual_close")
        self.assertEqual(exit_["attribution"], "position_link")
        self.assertEqual(exit_["related_position_id"], "7732832861")
        self.assertEqual(record["state"], "closed")
        # Percent-sized: the R still has its stop; nothing size-derived is guessed.
        self.assertEqual(value(record["outcome"]["denominator"]), 6.5)

    def test_lulu_g2_reaches_the_pick_by_fifo_and_shows_the_rejected_link(self) -> None:
        record = trade(self.report, "LULU:2026-09-08-g2")
        (exit_,) = record["exits"]
        self.assertEqual(exit_["reason"], "manual_close")
        self.assertEqual(exit_["attribution"], "fifo_fallback")
        self.assertIn("no_external_reference", exit_["reason_evidence"])
        self.assertIn("rejected:5442081345 RelatedPositionId=7741085955", exit_["reason_evidence"])
        self.assertIn("attribution_fifo_assumed", _codes(record))
        self.assertEqual(record["pick_status"], "disarmed")
        self.assertIsNotNone(record["plan"], "a disarmed pick keeps the plan of its armed line")

    def test_amba_take_profit_is_owned_by_the_uic_rule(self) -> None:
        record = trade(self.report, "AMBA:2026-09-04")
        (exit_,) = record["exits"]
        self.assertEqual(exit_["reason"], "take_profit")
        self.assertEqual(exit_["tp_label"], "TP1")
        self.assertIn("uic_rule", exit_["reason_evidence"])

    def test_gme_legacy_stop_lines_still_resolve_through_the_reference(self) -> None:
        record = trade(self.report, "GME:2026-08-27")
        (exit_,) = record["exits"]
        self.assertNotEqual(exit_["reason"], "unknown")
        self.assertEqual(exit_["attribution"], "external_reference")
        self.assertEqual(record["plan_source"], "absent")

    def test_ewtx_moved_stop_whose_markers_are_lost(self) -> None:
        (exit_,) = trade(self.report, "EWTX:2026-09-25")["exits"]
        self.assertEqual(exit_["reason"], "stop_moved_kind_unknown")
        self.assertNotEqual(exit_["reason"], "disaster_stop")

    def test_rhi_is_open_and_its_stop_comes_from_the_audit(self) -> None:
        record = trade(self.report, "RHI:2026-09-02")
        self.assertEqual(record["state"], "open")
        self.assertEqual(value(record["outcome"]["entry_qty"]), 3.0)
        self.assertEqual(record["outcome"]["pnl_cash"]["null_reason"], "not_closed")
        self.assertEqual(value(record["placed_stop"]), 30.39)

    def test_alb_g2_two_tiers_two_stops(self) -> None:
        record = trade(self.report, "ALB:2026-09-08-g2")
        self.assertEqual(record["state"], "closed")
        self.assertAlmostEqual(
            value(record["outcome"]["avg_entry_price"]), (3 * 123.4333 + 9 * 119.13) / 12, places=6
        )
        self.assertEqual(sorted(value(e["attributed_qty"]) for e in record["exits"]), [3.0, 9.0])
        self.assertEqual({e["reason"] for e in record["exits"]}, {"disaster_stop"})

    def test_lifecycle_states(self) -> None:
        cases = {
            "NVAX:2026-08-10": ("never_filled", "refused"),
            "QBTS:2026-09-21": ("never_filled", "disarmed"),
        }
        for key, (state, reason) in cases.items():
            with self.subTest(pick=key):
                record = trade(self.report, key)
                self.assertEqual((record["state"], record["state_reason"]), (state, reason))
                self.assertEqual(record["outcome"]["pnl_cash"]["null_reason"], "never_filled")

    def test_the_later_of_two_armed_lines_is_the_plan(self) -> None:
        record = trade(self.report, "LAC:2026-08-11")
        self.assertEqual(value(record["plan_armed_at"]), "2026-08-12T19:26:59.000Z")
        self.assertEqual(record["pick_status"], "refused")

    def test_a_probe_order_on_a_uic_no_pick_trades_is_not_listed(self) -> None:
        orders = {u["order_id"] for u in self.report.unattributed_fills}
        self.assertNotIn("5432611806", orders)  # torb-live-buy-486

    def test_the_audit_window_opens_a_day_before_the_oldest_armed_pick(self) -> None:
        broker = FakeFillHistory()
        self.build(broker)
        ((since, until),) = broker.calls
        self.assertEqual(until, NOW)
        self.assertEqual(since, dt.datetime(2026, 8, 10, 8, 29, 33, tzinfo=dt.UTC))


class SyntheticOwnership(_TradesCase):
    """synthetic: reference shapes LIVE has not produced on a pick exit."""

    def _with_exit_ref(self, ref: str | None, *, order_type: str | None = None) -> dict[str, Any]:
        install_journals(self.home)
        venue = venue_without()
        for row in venue["audit"]:
            if row["OrderId"] == VST_STOP:
                if ref is None:
                    row.pop("ExternalReference", None)
                else:
                    row["ExternalReference"] = ref
                if order_type and row["Status"] == "Placed":
                    row["OrderType"] = order_type
        return trade(self.build(FakeFillHistory(venue), pick=VST), VST)

    def test_a_reference_the_tp_label_would_only_uppercase_is_not_a_take_profit(self) -> None:
        (exit_,) = self._with_exit_ref("foo-bar")["exits"]
        self.assertEqual(exit_["reason"], "unknown")
        self.assertIn("unclaimed_reference:foo-bar", exit_["reason_evidence"])

    def test_oco_tp_and_stop_legs(self) -> None:
        (tp,) = self._with_exit_ref("VST-2026-09-21-entry-t0-fire-tp")["exits"]
        self.assertEqual(tp["reason"], "take_profit")
        self.tearDown_home()
        (stop,) = self._with_exit_ref("VST-2026-09-21-entry-t0-fire-stop")["exits"]
        self.assertEqual(stop["reason"], "trailed_stop")

    def tearDown_home(self) -> None:
        import shutil

        shutil.rmtree(self.home / ".alphalens", ignore_errors=True)

    def test_a_bracket_child_is_owned_through_exit_order_ids(self) -> None:
        install_journals(
            self.home,
            extra={
                "submissions": [
                    {
                        "ticker": "VST",
                        "trade_date": "2026-09-21",
                        "brackets": [
                            {
                                "client_request_id": "aaaaaaaa-0000-0000-0000-000000000000",
                                "entry_order_id": "1",
                                "exit_order_ids": [VST_STOP],
                                "qty": 1,
                            }
                        ],
                    }
                ]
            },
        )
        venue = venue_without()
        for row in venue["audit"]:
            if row["OrderId"] == VST_STOP:
                row.pop("ExternalReference", None)
        record = trade(self.build(FakeFillHistory(venue), pick=VST), VST)
        exit_ = record["exits"][0]
        self.assertEqual(exit_["attribution"], "journal_tie")
        self.assertEqual(exit_["reason"], "trailed_stop")

    def test_a_disaster_stop_three_ticks_below_its_level_is_still_the_disaster_stop(self) -> None:
        install_journals(self.home)
        venue = venue_without(rows=lambda r: r["OrderId"] == VST_STOP and r["Status"] == "Changed")
        for row in venue["audit"]:
            if row["OrderId"] == VST_STOP and row["Status"] == "FinalFill":
                row["AveragePrice"] = row["ExecutionPrice"] = 125.82
        (exit_,) = trade(self.build(FakeFillHistory(venue), pick=VST), VST)["exits"]
        self.assertEqual(exit_["reason"], "disaster_stop")
        self.assertEqual(value(exit_["price"]), 125.82)

    def test_a_trailed_line_of_another_stop_generation_does_not_match(self) -> None:
        # Move the trailed lines to before this stop order was placed.
        def edit(journal: str, record: dict[str, Any]) -> dict[str, Any]:
            if record.get("kind") == "trailed" and record.get("uic") == 7300542:
                return {**record, "ts": record["ts"] - 3 * 86400}
            return record

        install_journals(self.home, edit=edit)
        (exit_,) = trade(self.build(pick=VST), VST)["exits"]
        self.assertEqual(exit_["reason"], "stop_moved_kind_unknown")

    def test_a_reanchored_marker_gives_reanchored_stop(self) -> None:
        def edit(journal: str, record: dict[str, Any]) -> dict[str, Any]:
            if record.get("kind") == "trailed" and record.get("uic") == 7300542:
                return {
                    "kind": "reanchored",
                    "uic": 7300542,
                    "ts": record["ts"],
                    "stop_price": record["level"],
                }
            return record

        install_journals(self.home, edit=edit)
        (exit_,) = trade(self.build(pick=VST), VST)["exits"]
        self.assertEqual(exit_["reason"], "reanchored_stop")


def _two_picks_one_uic(home: Any, *, related: str | None) -> FakeFillHistory:
    """synthetic: two picks on uic 555 and one unreferenced closing fill."""
    picks = []
    trails = []
    for ticker, day, armed, _entry_time in (
        ("AAA", "2026-09-28", "2026-09-28T12:00:00+00:00", "2026-09-28T14:00:00.000000Z"),
        ("BBB", "2026-09-29", "2026-09-29T12:00:00+00:00", "2026-09-29T14:00:00.000000Z"),
    ):
        intent = {
            "instrument": {"mic": "XNYS", "ticker": ticker},
            "meta": {"schema_version": "3", "source": "manual", "trade_date": day},
            "spec": {"disaster_stop": 9.0, "entry_tiers": []},
        }
        picks.append(
            {"ticker": ticker, "date": day, "armed_ts": armed, "status": "armed", "intent": intent}
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
    venue["instruments"]["555"] = VENUE["instruments"]["7300542"]

    def row(
        order_id: str, ref: str | None, side: str, when: str, price: float, pos: str, **extra: Any
    ):
        base = {
            "ActivityTime": when,
            "Amount": 5.0,
            "AssetType": "Stock",
            "AveragePrice": price,
            "BuySell": side,
            "ExecutionPrice": price,
            "FillAmount": 5.0,
            "FilledAmount": 5.0,
            "LogId": str(abs(hash(order_id)) % 10**9),
            "OrderId": order_id,
            "OrderType": "Market",
            "PositionId": pos,
            "Status": "FinalFill",
            "SubStatus": "Confirmed",
            "Uic": 555,
            **extra,
        }
        if ref:
            base["ExternalReference"] = ref
        return base

    venue["audit"] += [
        row(
            "901", "AAA-2026-09-28-entry-t0-fire", "Buy", "2026-09-28T14:00:00.000000Z", 10.0, "P-A"
        ),
        row(
            "902", "BBB-2026-09-29-entry-t0-fire", "Buy", "2026-09-29T14:00:00.000000Z", 11.0, "P-B"
        ),
        row(
            "903",
            None,
            "Sell",
            "2026-09-30T14:00:00.000000Z",
            12.0,
            "P-C",
            **({"RelatedPositionId": related} if related else {}),
        ),
    ]
    return FakeFillHistory(venue)


class SyntheticTwoPicksOneUic(_TradesCase):
    def test_a_related_position_link_decides(self) -> None:
        broker = _two_picks_one_uic(self.home, related="P-B")
        report = self.build(broker, ticker=None)
        bbb = trade(report, "BBB:2026-09-29")
        aaa = trade(report, "AAA:2026-09-28")
        self.assertEqual(bbb["exits"][0]["attribution"], "position_link")
        self.assertEqual(bbb["state"], "closed")
        self.assertEqual(aaa["state"], "open")

    def test_without_the_link_fifo_gives_it_to_the_oldest_lot_and_says_so(self) -> None:
        broker = _two_picks_one_uic(self.home, related=None)
        report = self.build(broker)
        aaa = trade(report, "AAA:2026-09-28")
        self.assertEqual(aaa["exits"][0]["attribution"], "fifo_fallback")
        self.assertIn("attribution_fifo_assumed", _codes(aaa))
        self.assertEqual(trade(report, "BBB:2026-09-29")["exits"], [])

    def test_a_link_that_matches_no_pick_is_evidence_then_fifo(self) -> None:
        broker = _two_picks_one_uic(self.home, related="P-UNKNOWN")
        aaa = trade(self.build(broker), "AAA:2026-09-28")
        self.assertIn("related_position_unmatched:P-UNKNOWN", aaa["exits"][0]["reason_evidence"])

    def test_equal_looking_position_ids_are_never_compared(self) -> None:
        # The closing fill's own PositionId equals a lot's: that is not a link.
        broker = _two_picks_one_uic(self.home, related=None)
        for row in broker.venue["audit"]:
            if row["OrderId"] == "903":
                row["PositionId"] = "P-B"
        aaa = trade(self.build(broker), "AAA:2026-09-28")
        self.assertEqual(aaa["exits"][0]["attribution"], "fifo_fallback")


class ReportsAndBookings(_TradesCase):
    def test_an_audit_fill_with_no_report_row_keeps_its_audit_values(self) -> None:
        install_journals(self.home)
        broker = FakeFillHistory(venue_without(trades={VST_EXIT_TRADE}))
        record = trade(self.build(broker, pick=VST), VST)
        (exit_,) = record["exits"]
        self.assertEqual(value(exit_["price"]), 138.66)
        self.assertEqual(value(exit_["qty"]), 6.0)
        self.assertEqual(exit_["executions"], [])
        self.assertEqual(exit_["fees"]["commission"]["null_reason"], "report_row_missing")
        self.assertEqual(
            exit_["realized_fx"]["conversion_rate"]["null_reason"], "report_row_missing"
        )
        self.assertIn("report_lags_audit", _codes(record))
        self.assertEqual(record["state"], "closed")
        self.assertEqual(record["outcome"]["pnl_cash_acct"]["null_reason"], "fx_rate_not_realized")

    def test_an_unknown_booking_type_is_reported(self) -> None:
        install_journals(self.home)
        venue = venue_without()
        venue["bookings"].append(
            {
                **next(b for b in venue["bookings"] if b["RelatedTradeId"] == VST_EXIT_TRADE),
                "BkAmountType": "Brand New Fee",
                "Amount": -5.0,
            }
        )
        record = trade(self.build(FakeFillHistory(venue), pick=VST), VST)
        self.assertIn("booking_type_unmapped", _codes(record))
        # Not summed into any fee.
        self.assertEqual(value(record["exits"][0]["fees"]["commission"]), -1.0)

    def test_on_sim_the_reports_are_skipped_and_said_so(self) -> None:
        install_journals(self.home)
        report = self.build(FakeFillHistory(sim=True), pick=VST)
        record = trade(report, VST)
        self.assertEqual(report.sources["venue.trades_report"]["status"], "skipped")
        self.assertEqual(report.sources["venue.trades_report"]["reason"], "sim_reports_unusable")
        self.assertEqual(
            record["exits"][0]["fees"]["commission"]["null_reason"], "sim_reports_unusable"
        )
        self.assertEqual(record["exits"][0]["executions"], [])

    def test_synthetic_multi_execution_fill_uses_the_average_and_both_trades(self) -> None:
        install_journals(self.home)
        venue = venue_without()
        for row in venue["audit"]:
            if row["OrderId"] == VST_STOP and row["Status"] == "FinalFill":
                row["ExecutionPrice"] = 138.60
                row["AveragePrice"] = 138.66
        base = next(t for t in venue["trades"] if t["TradeId"] == VST_EXIT_TRADE)
        venue["trades"].remove(base)
        venue["trades"] += [
            {**base, "TradeId": "T1", "Amount": -3.0, "Price": 138.60},
            {**base, "TradeId": "T2", "Amount": -3.0, "Price": 138.72},
        ]
        commission = next(
            b
            for b in venue["bookings"]
            if b["RelatedTradeId"] == VST_EXIT_TRADE and b["BkAmountType"] == "Commission"
        )
        venue["bookings"] = [b for b in venue["bookings"] if b["RelatedTradeId"] != VST_EXIT_TRADE]
        venue["bookings"] += [
            {**commission, "RelatedTradeId": "T1"},
            {**commission, "RelatedTradeId": "T2"},
        ]
        (exit_,) = trade(self.build(FakeFillHistory(venue), pick=VST), VST)["exits"]
        self.assertEqual(value(exit_["price"]), 138.66)
        self.assertEqual([e["trade_id"] for e in exit_["executions"]], ["T1", "T2"])
        self.assertEqual(value(exit_["fees"]["commission"]), -2.0)

    def test_a_time_gap_within_a_millisecond_passes_and_beyond_a_second_warns(self) -> None:
        install_journals(self.home)
        self.assertNotIn("audit_report_disagree", _codes(trade(self.build(pick=VST), VST)))
        venue = venue_without()
        for t in venue["trades"]:
            if t["TradeId"] == VST_EXIT_TRADE:
                t["TradeExecutionTime"] = "2026-10-01T13:59:23.500000Z"
        record = trade(self.build(FakeFillHistory(venue), pick=VST), VST)
        self.assertIn("audit_report_disagree", _codes(record))


class CrossChecksAndEdges(_TradesCase):
    def test_journal_audit_disagree(self) -> None:
        def edit(journal: str, record: dict[str, Any]) -> dict[str, Any]:
            if record.get("kind") == "fired" and record.get("crid") == "VST-2026-09-21-entry-t0":
                return {**record, "avg_price": 135.50}
            return record

        install_journals(self.home, edit=edit)
        record = trade(self.build(pick=VST), VST)
        self.assertIn("journal_audit_disagree", _codes(record))
        self.assertEqual(value(record["entries"][0]["fill"]["price"]), 135.11)

    def test_entry_not_in_journal(self) -> None:
        install_journals(
            self.home, drop=lambda r: str(r.get("crid", "")).startswith("VST-2026-09-21")
        )
        record = trade(self.build(pick=VST), VST)
        self.assertIn("entry_not_in_journal", _codes(record))
        self.assertEqual(value(record["entries"][0]["fill"]["qty"]), 6.0)
        self.assertEqual(record["state"], "closed")

    def test_a_non_finite_journal_value_is_null_non_finite_and_renders_strictly(self) -> None:
        def edit(journal: str, record: dict[str, Any]) -> dict[str, Any]:
            if record.get("kind") == "fired" and record.get("crid") == "VST-2026-09-21-entry-t0":
                return {**record, "avg_price": float("inf")}
            return record

        install_journals(self.home, edit=edit)
        report = self.build(None, pick=VST)
        record = trade(report, VST)
        self.assertEqual(record["entries"][0]["fill"]["price"]["null_reason"], "non_finite")
        json.dumps(report.body(), allow_nan=False)

    def test_a_collapsed_duplicate_line_is_flagged_offline(self) -> None:
        line = {
            "kind": "cancelled",
            "crid": "VST-2026-09-21-entry-t0",
            "note": "synthetic duplicate",
        }
        install_journals(self.home, extra={"entry_trails": [line, line]})
        self.assertIn("fold_order_uncertain", _codes(trade(self.build(None, pick=VST), VST)))
        # Broker mode takes the terminal from the audit, unaffected.
        broker_record = trade(self.build(pick=VST), VST)
        self.assertEqual(broker_record["entries"][0]["terminal"], "filled")
        self.assertNotIn("fold_order_uncertain", _codes(broker_record))

    def test_touch_time_is_read(self) -> None:
        install_journals(self.home)
        record = trade(self.build(pick="NESR:2026-09-30"), "NESR:2026-09-30")
        self.assertEqual(value(record["entries"][0]["touched_at"]), "2026-10-01T15:00:36.070Z")


class LifecycleAndPlanShapes(_TradesCase):
    """Memo §9 tests 18 and 22 on the real LIVE picks."""

    def setUp(self) -> None:
        super().setUp()
        install_journals(self.home)
        self.report = self.build()

    def test_gme_is_a_schema_1_document_with_no_source(self) -> None:
        record = trade(self.report, "GME:2026-08-27")
        self.assertEqual(record["plan_schema_version"], "1")
        self.assertEqual(record["plan_source"], "absent")
        self.assertEqual(record["plan_size_shape"], "by_percent")

    def test_a_disarmed_pick_that_filled_keeps_the_plan_of_its_last_armed_line(self) -> None:
        # GME: armed, refused, armed again, filled, then disarmed by the
        # sibling retire. The plan is the SECOND armed line, not the disarm.
        record = trade(self.report, "GME:2026-08-27")
        self.assertEqual(record["pick_status"], "disarmed")
        self.assertEqual(value(record["pick_status_at"]), "2026-08-31T21:14:41.000Z")
        self.assertTrue(record["pick_status_reason"].startswith("sibling retire"))
        self.assertEqual(value(record["plan_armed_at"]), "2026-08-28T16:42:17.000Z")
        self.assertIsNotNone(record["plan"])
        self.assertEqual(record["state"], "closed")

    def test_never_filled_reasons(self) -> None:
        cases = {
            "NVAX:2026-08-10": "refused",
            "QBTS:2026-09-21": "disarmed",
            "CIEN:2026-09-04": "expired",
            "BE:2026-09-30": "pending",
        }
        for key, reason in cases.items():
            with self.subTest(pick=key):
                record = trade(self.report, key)
                self.assertEqual(
                    (record["state"], record["state_reason"]), ("never_filled", reason)
                )


_RFC3339_MS_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
_LOOKS_LIKE_A_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T")


def _timestamps(node: Any, path: str = "") -> list[tuple[str, str]]:
    """Every string in a rendered body that carries a time of day. The plan is
    skipped: it is the armed document, copied verbatim."""
    found: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for key, child in node.items():
            if key != "plan":
                found += _timestamps(child, f"{path}.{key}")
    elif isinstance(node, list):
        for i, child in enumerate(node):
            found += _timestamps(child, f"{path}[{i}]")
    elif isinstance(node, str) and _LOOKS_LIKE_A_TIME.match(node):
        found.append((path, node))
    return found


class TimeFormat(_TradesCase):
    """Memo §3.1 / test 26: epoch floats, ISO strings with offsets and
    ``touch_ts`` all come out as RFC 3339 UTC with milliseconds and ``Z``."""

    def test_every_timestamp_is_rfc3339_utc_with_milliseconds(self) -> None:
        install_journals(self.home)
        for mode, broker in (("broker", "default"), ("offline", None)):
            body = self.build(broker).body()
            stamps = _timestamps(body)
            self.assertGreater(len(stamps), 100, mode)
            for path, stamp in stamps:
                with self.subTest(mode=mode, path=path):
                    self.assertRegex(stamp, _RFC3339_MS_Z)


class FiltersAndCounts(_TradesCase):
    def setUp(self) -> None:
        super().setUp()
        install_journals(self.home)

    def test_counts_all_is_before_the_state_filter_and_selected_after(self) -> None:
        everything = self.build()
        closed = self.build(state="closed")
        self.assertEqual(closed.counts["all"], everything.counts["all"])
        self.assertEqual(closed.counts["selected"]["total"], everything.counts["all"]["closed"])
        self.assertTrue(all(r["state"] == "closed" for r in closed.trades))

    def test_limit_truncates_and_says_so(self) -> None:
        report = build_trades(
            "live", broker=FakeFillHistory(), filters=TradesFilters(limit=1), now=NOW
        )
        self.assertTrue(report.truncated)
        self.assertEqual(len(report.trades), 1)
        self.assertGreater(report.counts["selected"]["total"], 1)

    def test_newest_trade_date_first(self) -> None:
        dates = [r["trade_date"] for r in self.build().trades]
        self.assertEqual(dates, sorted(dates, reverse=True))

    def test_since_and_ticker(self) -> None:
        report = self.build(since=dt.date(2026, 9, 21), ticker="vst")
        self.assertEqual([r["pick_key"] for r in report.trades], [VST])


QUBT = "QUBT:2026-09-03"


class BracketStopIsThePlacedStop(_TradesCase):
    """QUBT's stop carries the bracket form ``<uuid>-stop-0`` (order 5439823194,
    placed @6.5), not the ``<crid>-entry-tN-stop-N`` form."""

    def setUp(self) -> None:
        super().setUp()
        install_journals(self.home)

    def test_broker_mode_reads_the_bracket_stop_from_the_audit(self) -> None:
        placed = trade(self.build(pick=QUBT), QUBT)["placed_stop"]
        self.assertEqual(value(placed), 6.5)
        self.assertEqual(placed["source"], "venue.audit")
        self.assertEqual(placed["ref"], "order:5439823194")

    def test_offline_reads_the_bracket_stop_from_the_stop_journal(self) -> None:
        placed = trade(self.build(None, pick=QUBT), QUBT)["placed_stop"]
        self.assertEqual(value(placed), None)
        # The stop_placed line of this generation has no stop_price; the
        # journal still names the bracket's stop, so the reason is not a
        # missing audit row.
        self.assertEqual(placed["source"], "keeper.stop_journal")
        self.assertEqual(placed["ref"], "line:standalone_stops:stop_placed")


SIM_RHI = "RHI:2026-09-03"
SIM_RHI_STOP = "5040004072"


class SimRhiBracketStopOffline(_TradesCase):
    """SIM RHI:2026-09-03, offline: a now-bracket tier t0 (701, fill not
    journaled) and a trail tier t1 (1095, filled), closed by ONE stop
    ``d1b6f68d-...-stop-0`` that sold 1796. The reference names the pick's own
    bracket, so the stop is the pick's; the pick cannot be ``closed`` while
    the bracket tier's fill is unknown."""

    env = "sim"

    def setUp(self) -> None:
        super().setUp()
        install_journals(self.home, env="sim", source_dir=SIM_RHI_DIR)
        self.report = self.build(None, pick=SIM_RHI)
        self.record = trade(self.report, SIM_RHI)

    def test_the_stop_is_owned_through_its_bracket_reference_not_by_fifo(self) -> None:
        (exit_,) = self.record["exits"]
        self.assertEqual(exit_["order_id"], SIM_RHI_STOP)
        self.assertEqual(exit_["attribution"], "external_reference")
        self.assertEqual(value(exit_["attributed_qty"]), 1095.0)
        self.assertNotIn("attribution_fifo_assumed", _codes(self.record))
        self.assertIn("exit_qty_exceeds_pick", _codes(self.record))

    def test_the_pick_is_unresolved_while_its_bracket_fill_is_not_journaled(self) -> None:
        self.assertEqual(self.record["state"], "unresolved")
        self.assertEqual(self.record["state_reason"], "not_journaled")
        self.assertEqual(self.record["outcome"]["r_multiple"]["null_reason"], "not_journaled")

    def test_the_surplus_is_listed_with_the_reason_its_reason_is_null(self) -> None:
        (surplus,) = [u for u in self.report.unattributed_fills if u["order_id"] == SIM_RHI_STOP]
        self.assertEqual(value(surplus["unattributed_qty"]), 701.0)
        self.assertIsNone(surplus["reason"])
        self.assertEqual(surplus["reason_null_reason"], "stop_amend_history_unavailable")


class PublishedVocabularies(unittest.TestCase):
    def test_every_alert_reason_has_a_row(self) -> None:
        self.assertEqual(
            set(EXIT_REASON_BY_ALERT_REASON), {member.name for member in trade_alerts.ExitReason}
        )
        self.assertTrue(set(EXIT_REASON_BY_ALERT_REASON.values()) <= set(EXIT_REASONS))

    def test_no_value_looks_like_an_order_write(self) -> None:
        import re

        for vocabulary in (NULL_REASONS, WARNING_CODES, EXIT_REASONS):
            for item in vocabulary:
                self.assertIsNone(re.match(r"^(place_|amend_)", item), item)


if __name__ == "__main__":
    unittest.main()
