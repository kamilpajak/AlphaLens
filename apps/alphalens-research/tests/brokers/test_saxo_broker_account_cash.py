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
from alphalens_pipeline.brokers.saxo.broker import SaxoBroker

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

    def get_client_info(self) -> dict[str, Any]:
        return {"ClientKey": "CK-1"}

    def get_accounts(self) -> dict[str, Any]:
        return {"Data": [{"AccountKey": "AK-1"}]}

    def get_trades_report(self, client_key: str, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("trades", kwargs))
        return list(self.trades)

    def get_bookings_report(self, client_key: str, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("bookings", kwargs))
        return list(self.bookings)

    def get_balances(self, client_key: str, account_key: str | None = None) -> dict[str, Any]:
        self.calls.append(("balances", {"account_key": account_key}))
        return dict(self.balance)


class TheCapability(unittest.TestCase):
    def test_the_broker_offers_the_capability(self) -> None:
        broker = SaxoBroker(_FakeClient())  # type: ignore[arg-type]
        self.assertIsInstance(broker, SupportsAccountCash)


if __name__ == "__main__":
    unittest.main()
