"""The Saxo adapter's fill-history capability (#1701 §4.7).

Pins what ``list_fill_history`` reads and how it maps the vendor rows, with
rows taken from the LIVE account on 2026-10-03 (VST:2026-09-21, redacted):

- the audit read sends ``FromDateTime`` / ``ToDateTime`` and filters on our
  side by uic only later (the endpoint ignores ``Uic``);
- the report window is padded by one day on both ends, in dates, and the rows
  are then kept by ``TradeExecutionTime`` (trades) and by trade id (bookings);
- on SIM the report endpoints are never called;
- the tick size comes from one instrument-details GET per uic.
"""

from __future__ import annotations

import datetime as dt
import json
import unittest
from pathlib import Path
from typing import Any

from alphalens_pipeline.brokers.fill_history import (
    CostBooking,
    Execution,
    FillHistory,
    OrderActivity,
    SupportsFillHistory,
    parse_utc,
)
from alphalens_pipeline.brokers.saxo.broker import SaxoBroker
from alphalens_pipeline.brokers.saxo.client import SIM_BASE_URL, SaxoRateLimitError
from broker_contract.contract import BrokerRateLimitError

_VENUE = json.loads(
    (
        Path(__file__).parent
        / "automanager"
        / "fixtures"
        / "trades"
        / "live_2026_10_03"
        / "venue.json"
    ).read_text(encoding="utf-8")
)
_VST_ORDERS = {"5448021994", "5448023092"}
_VST_TRADES = {"6883674691", "6885451891"}
_LIVE_BASE = "https://gateway.saxobank.com/" + "openapi"


class _FakeClient:
    """Answers the reads ``list_fill_history`` makes, and records them."""

    def __init__(self, *, base_url: str = _LIVE_BASE) -> None:
        self.base_url = base_url
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.audit = [r for r in _VENUE["audit"] if r["OrderId"] in _VST_ORDERS]
        self.trades = [r for r in _VENUE["trades"] if r["TradeId"] in _VST_TRADES]
        self.bookings = [r for r in _VENUE["bookings"] if r["RelatedTradeId"] in _VST_TRADES]
        self.rate_limited = False

    def get_client_info(self) -> dict[str, Any]:
        return {"ClientKey": "CK-1"}

    def get_accounts(self) -> dict[str, Any]:
        return {"Data": [{"AccountKey": "AK-1"}]}

    def get_order_activities(self, client_key: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("audit", kwargs))
        if self.rate_limited:
            raise SaxoRateLimitError("429")
        return {"Data": list(self.audit)}

    def get_trades_report(self, client_key: str, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("trades", kwargs))
        return list(self.trades)

    def get_bookings_report(self, client_key: str, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("bookings", kwargs))
        return list(self.bookings)

    def get_instrument_details(self, uic: Any, asset_type: str = "Stock") -> dict[str, Any]:
        self.calls.append(("details", {"uic": uic}))
        return _VENUE["instruments"][str(uic)]


_SINCE = dt.datetime(2026, 9, 20, 16, 12, 44, tzinfo=dt.UTC)
_UNTIL = dt.datetime(2026, 10, 3, 15, 30, tzinfo=dt.UTC)


class ParseUtc(unittest.TestCase):
    def test_every_shape_the_sources_use_becomes_aware_utc(self) -> None:
        expected = dt.datetime(2026, 9, 30, 13, 36, 35, 916000, tzinfo=dt.UTC)
        for value in (
            "2026-09-30T13:36:35.916000Z",
            "2026-09-30T13:36:35.9160000Z",
            "2026-09-30T15:36:35.916+02:00",
            "2026-09-30T13:36:35.916",
            expected.timestamp(),
        ):
            with self.subTest(value=value):
                self.assertEqual(parse_utc(value), expected)

    def test_what_is_not_a_time_is_none(self) -> None:
        for value in (None, True, "soon", float("nan"), float("inf"), [], {}):
            with self.subTest(value=value):
                self.assertIsNone(parse_utc(value))


class ListFillHistory(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _FakeClient()
        self.broker = SaxoBroker(self.client)  # type: ignore[arg-type]

    def test_the_broker_offers_the_capability(self) -> None:
        self.assertIsInstance(self.broker, SupportsFillHistory)

    def test_the_audit_read_names_both_ends_of_the_window(self) -> None:
        self.broker.list_fill_history(_SINCE, _UNTIL)
        audit_kwargs = dict(self.client.calls)["audit"]
        self.assertEqual(audit_kwargs["entry_type"], "All")
        self.assertEqual(audit_kwargs["from_datetime"], "2026-09-20T16:12:44Z")
        self.assertEqual(audit_kwargs["to_datetime"], "2026-10-03T15:30:00Z")

    def test_the_report_window_is_padded_by_one_day_on_both_ends(self) -> None:
        history = self.broker.list_fill_history(_SINCE, _UNTIL)
        for name in ("trades", "bookings"):
            with self.subTest(report=name):
                kwargs = dict(self.client.calls)[name]
                self.assertEqual(kwargs["from_date"], "2026-09-19")
                self.assertEqual(kwargs["to_date"], "2026-10-04")
                self.assertEqual(kwargs["account_key"], "AK-1")
        assert history.trades_window is not None
        self.assertEqual(
            (history.trades_window.start, history.trades_window.end), ("2026-09-19", "2026-10-04")
        )

    def test_the_rows_are_mapped_as_sent(self) -> None:
        history = self.broker.list_fill_history(_SINCE, _UNTIL)
        fill = next(
            a for a in history.activities if a.order_id == "5448023092" and a.status == "FinalFill"
        )
        self.assertEqual(
            fill,
            OrderActivity(
                order_id="5448023092",
                external_reference="VST-2026-09-21-entry-t0-fire-stop-0",
                uic=7300542,
                buy_sell="Sell",
                order_type="Market",
                status="FinalFill",
                sub_status="Confirmed",
                price=None,
                activity_time=dt.datetime(2026, 10, 1, 13, 59, 21, 248000, tzinfo=dt.UTC),
                amount=6.0,
                filled_amount=6.0,
                execution_price=138.66,
                average_price=138.66,
                position_id="7764452789",
                related_position_id=None,
                log_id=4166532524,
            ),
        )
        assert history.executions is not None and history.bookings is not None
        self.assertIn(
            Execution(
                trade_id="6885451891",
                order_id="5448023092",
                execution_time=dt.datetime(2026, 10, 1, 13, 59, 21, 247000, tzinfo=dt.UTC),
                price=138.66,
                amount=-6.0,
                to_open_or_close="ToClose",
                trade_date="2026-10-01",
            ),
            history.executions,
        )
        share = next(
            b
            for b in history.bookings
            if b.related_trade_id == "6883674691" and b.bk_amount_type == "Share Amount"
        )
        self.assertEqual(
            share,
            CostBooking(
                related_trade_id="6883674691",
                bk_amount_type="Share Amount",
                amount=-810.66,
                currency="USD",
                amount_account_currency=-3121.68,
                account_currency="PLN",
                conversion_rate=3.85078295,
                conversion_cost_account_currency=-7.78999999999996,
                date="2026-09-30",
            ),
        )

    def test_report_rows_outside_the_window_are_dropped_by_execution_time(self) -> None:
        # The padded date window can return a trade executed before `since`.
        self.client.trades.append(
            {**self.client.trades[0], "TradeId": "1", "TradeExecutionTime": "2026-09-19T15:00:00Z"}
        )
        self.client.bookings.append({**self.client.bookings[0], "RelatedTradeId": "1"})
        history = self.broker.list_fill_history(_SINCE, _UNTIL)
        assert history.executions is not None and history.bookings is not None
        self.assertEqual({e.trade_id for e in history.executions}, _VST_TRADES)
        self.assertEqual({b.related_trade_id for b in history.bookings}, _VST_TRADES)

    def test_on_sim_the_report_endpoints_are_never_called(self) -> None:
        client = _FakeClient(base_url=SIM_BASE_URL)
        history = SaxoBroker(client).list_fill_history(_SINCE, _UNTIL)  # type: ignore[arg-type]
        self.assertEqual([name for name, _ in client.calls], ["audit"])
        self.assertIsNone(history.executions)
        self.assertIsNone(history.bookings)
        self.assertEqual(history.reports_skipped_reason, "sim_reports_unusable")

    def test_a_vendor_error_leaves_as_a_contract_error(self) -> None:
        self.client.rate_limited = True
        with self.assertRaises(BrokerRateLimitError):
            self.broker.list_fill_history(_SINCE, _UNTIL)

    def test_it_returns_a_fill_history(self) -> None:
        self.assertIsInstance(self.broker.list_fill_history(_SINCE, _UNTIL), FillHistory)


class TickSize(unittest.TestCase):
    def test_one_details_read_per_uic(self) -> None:
        client = _FakeClient()
        broker = SaxoBroker(client)  # type: ignore[arg-type]
        self.assertEqual(broker.tick_size(7300542, 138.82), 0.01)
        self.assertEqual(broker.tick_size(7300542, 0.5), 0.0001)
        self.assertEqual([name for name, _ in client.calls], ["details"])


if __name__ == "__main__":
    unittest.main()
