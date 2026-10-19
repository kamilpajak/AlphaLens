"""The Saxo adapter's account-cash capability (#1689, memo Phase 2).

``list_account_cash`` is the cash book's read: the SAME two report endpoints
``list_fill_history`` uses, read UNFILTERED, plus the account's own balance.
The fill-history capability cannot serve as the cash book — it keeps only
bookings whose ``related_trade_id`` is in the trades it kept
(``saxo/broker.py:1707``), which on the captured window drops 5 of 104 rows
worth 24 004.05 PLN (memo section 1.4).

Rows come from the LIVE account on 2026-10-03, the fixture
``automanager/fixtures/trades/live_2026_10_03/venue.json`` (104 bookings rows,
41 trades rows), and the balance body from the LIVE probes of 2026-10-05
(memo section 1.6 P1) and 2026-10-08 (memo section 1.7 P5).
"""

from __future__ import annotations

import datetime as dt
import json
import unittest
from pathlib import Path
from typing import Any

from alphalens_pipeline.brokers.account_cash import SupportsAccountCash
from alphalens_pipeline.brokers.fill_history import ReadWindow
from alphalens_pipeline.brokers.saxo.broker import SaxoBroker
from alphalens_pipeline.brokers.saxo.client import SaxoRateLimitError
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
_LIVE_BASE = "https://gateway.saxobank.com/" + "openapi"

# The balance body: memo section 1.6 P1 (LIVE 2026-10-05) for the five
# position fields, and memo section 1.7 P5 (LIVE 2026-10-08, 23:46:52 UTC,
# three trades in flight) for ``TransactionsNotBooked`` -- the only state in
# which that field is non-zero, and the one P1's quiet window could not show.
_BALANCE_BODY: dict[str, Any] = {
    "Currency": "PLN",
    "CashBalance": 20738.68,
    "NonMarginPositionsValue": 3422.56,
    "TotalValue": 24161.24,
    "TransactionsNotBooked": -4319.07,
    "CalculationReliability": "Ok",
}

_SINCE = dt.date(2026, 8, 1)
_UNTIL = dt.date(2026, 10, 3)


class _FakeClient:
    """Answers the reads ``list_account_cash`` makes, and records them."""

    def __init__(self, *, base_url: str = _LIVE_BASE) -> None:
        self.base_url = base_url
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.bookings: list[dict[str, Any]] = [dict(r) for r in _VENUE["bookings"]]
        self.trades: list[dict[str, Any]] = [dict(r) for r in _VENUE["trades"]]
        self.balance: dict[str, Any] = dict(_BALANCE_BODY)
        self.rate_limited = False

    def get_client_info(self) -> dict[str, Any]:
        return {"ClientKey": "CK-1"}

    def get_accounts(self) -> dict[str, Any]:
        return {"Data": [{"AccountKey": "AK-1"}]}

    def get_trades_report(self, client_key: str, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("trades", kwargs))
        return list(self.trades)

    def get_bookings_report(self, client_key: str, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("bookings", kwargs))
        if self.rate_limited:
            raise SaxoRateLimitError("429")
        return list(self.bookings)

    def get_balances(self, client_key: str, account_key: str | None = None) -> dict[str, Any]:
        self.calls.append(("balances", {"account_key": account_key}))
        return dict(self.balance)


class TheCapability(unittest.TestCase):
    def test_the_broker_offers_the_capability(self) -> None:
        broker = SaxoBroker(_FakeClient())  # type: ignore[arg-type]
        self.assertIsInstance(broker, SupportsAccountCash)


class TheRead(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _FakeClient()
        self.broker = SaxoBroker(self.client)  # type: ignore[arg-type]

    def test_every_booking_row_comes_back_unfiltered(self) -> None:
        """All 104 rows, including the 5 the fill-history filter drops.

        ``list_fill_history`` keeps only bookings whose ``related_trade_id`` is
        in the trades it kept (``saxo/broker.py:1707``). The five ids below are
        exactly the rows that filter removes: three deposits, whose
        ``RelatedTradeId`` is a non-trade value, and the two corporate-action
        rows, whose ``RelatedTradeId`` is ``"0"``."""
        read = self.broker.list_account_cash(_SINCE, _UNTIL)
        assert read.events is not None
        self.assertEqual(len(read.events), 104)
        ids = {event.bk_amount_id for event in read.events}
        for dropped in (
            "58226097441",
            "58612863938",
            "59102022834",
            "60187389516",
            "60187389517",
        ):
            with self.subTest(bk_amount_id=dropped):
                self.assertIn(dropped, ids)

    def test_the_window_asked_for_is_the_window_published(self) -> None:
        """Dates in, dates out, no pad.

        ``list_fill_history`` pads the report window by one day on both ends
        because its caller passes datetimes and the 2026-10-03 probe could not
        tell whether the report date was UTC or account-local. Memo section 1.7
        P9 settled that: ``Date`` is the venue-stamped trade date. So the
        window asked for IS the window read, and a sweep of ``2026-08-01`` is
        not silently two days wider."""
        read = self.broker.list_account_cash(_SINCE, _UNTIL)
        kwargs = dict(self.client.calls)["bookings"]
        self.assertEqual(kwargs["from_date"], "2026-08-01")
        self.assertEqual(kwargs["to_date"], "2026-10-03")
        self.assertEqual(kwargs["account_key"], "AK-1")
        self.assertEqual(read.events_window, ReadWindow("2026-08-01", "2026-10-03", 104))

    def test_the_trades_window_is_published_too(self) -> None:
        read = self.broker.list_account_cash(_SINCE, _UNTIL)
        kwargs = dict(self.client.calls)["trades"]
        self.assertEqual(kwargs["from_date"], "2026-08-01")
        self.assertEqual(kwargs["to_date"], "2026-10-03")
        self.assertEqual(read.trades_window, ReadWindow("2026-08-01", "2026-10-03", 41))

    def test_a_vendor_error_leaves_as_a_contract_error(self) -> None:
        """The network calls sit INSIDE ``_translate_saxo_errors()``.

        Outside it a raw ``SaxoError`` escapes the adapter boundary and the CLI
        classifies it as the base ``broker_failed`` instead of the retryable
        rate-limit code."""
        self.client.rate_limited = True
        with self.assertRaises(BrokerRateLimitError):
            self.broker.list_account_cash(_SINCE, _UNTIL)


if __name__ == "__main__":
    unittest.main()
