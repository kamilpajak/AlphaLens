"""One formatter for the trade alerts the daemon sends (#1621).

The entry-fill, stop-fill and take-profit alerts used to be three f-strings at
three call sites, and they drifted apart: the entry fill had no ticker, the
stop fill could not say WHICH stop filled, and the take-profit said ``sold``
for a SELL that was only submitted. The call sites now pass facts in a
``TradeEvent`` and ``render`` is the only place that writes text.

The mapping guard below is the part that answers "does a TradeIntent change
force a message redesign?": a new reaction-plan kind cannot reach a stop-fill
alert without a stop reason. It does NOT cover a new kind of exit EVENT that is
not a stop move (a manual or a time exit) — that still needs a new
``EventKind`` and a call site of its own.
"""

from __future__ import annotations

import unittest

from alphalens_pipeline.brokers.automanager import trade_alerts as ta
from alphalens_pipeline.brokers.automanager.trade_alerts import (
    EventKind,
    ExitReason,
    TradeEvent,
    render,
    stop_reason,
)
from broker_contract.trade_intent import codec


def _exit(**over: object) -> TradeEvent:
    fields: dict[str, object] = {
        "kind": EventKind.EXIT_FILLED,
        "ticker": "XYZ",
        "label": None,
        "qty": 3.0,
        "price": 10.0,
        "reason": ExitReason.PLAN_STOP,
        "level": None,
        "order_id": "1",
        "position_closed": True,
    }
    fields.update(over)
    return TradeEvent(**fields)  # type: ignore[arg-type]


class RenderEachKind(unittest.TestCase):
    def test_entry_fill_is_a_buy_with_ticker_label_and_price(self) -> None:
        event = TradeEvent(
            kind=EventKind.ENTRY_FILLED,
            ticker="SMMT",
            label="E1",
            qty=34.0,
            price=16.18,
            order_id="5446498379",
        )
        self.assertEqual(render(event), "BUY SMMT E1 34 @ 16.18 (order 5446498379)")

    def test_stop_fill_names_the_stop_and_the_outcome(self) -> None:
        self.assertEqual(
            render(_exit()), "SELL XYZ 3 @ 10.00 - plan stop - position closed (order 1)"
        )

    def test_submitted_take_profit_says_sent_not_sold(self) -> None:
        event = TradeEvent(
            kind=EventKind.EXIT_SUBMITTED,
            ticker="SMMT",
            label="TP1",
            qty=34.0,
            reason=ExitReason.TAKE_PROFIT,
            order_id="5447570154",
            position_closed=True,
        )
        self.assertEqual(
            render(event),
            "SELL SMMT TP1 34 - take-profit, market sell sent - position closed (order 5447570154)",
        )

    def test_an_exit_that_leaves_shares_says_the_position_is_still_open(self) -> None:
        self.assertEqual(
            render(_exit(position_closed=False)),
            "SELL XYZ 3 @ 10.00 - plan stop - position still open (order 1)",
        )

    def test_the_stop_level_follows_the_reason(self) -> None:
        text = render(_exit(reason=ExitReason.TRAILED_STOP, level=62.42304))
        self.assertIn("- trailed stop 62.42 -", text)


class RenderMissingFields(unittest.TestCase):
    """A field that is not known is left out. It is never printed as ``None``,
    and leaving it out never leaves a dangling separator behind."""

    def _assert_clean(self, text: str) -> None:
        self.assertNotIn("None", text)
        self.assertNotIn("  ", text)
        self.assertNotIn("- -", text)
        self.assertFalse(text.endswith(" -"), text)
        self.assertNotIn("- (", text)

    def test_no_price(self) -> None:
        text = render(_exit(price=None))
        self._assert_clean(text)
        self.assertNotIn("@", text)

    def test_no_order_id(self) -> None:
        text = render(_exit(order_id=None))
        self._assert_clean(text)
        self.assertNotIn("order", text)

    def test_no_reason(self) -> None:
        text = render(_exit(reason=None))
        self._assert_clean(text)
        self.assertEqual(text, "SELL XYZ 3 @ 10.00 - position closed (order 1)")

    def test_unknown_outcome(self) -> None:
        text = render(_exit(position_closed=None))
        self._assert_clean(text)
        self.assertEqual(text, "SELL XYZ 3 @ 10.00 - plan stop (order 1)")

    def test_nothing_optional(self) -> None:
        event = TradeEvent(kind=EventKind.EXIT_FILLED, ticker="XYZ", label=None, qty=3.0)
        self.assertEqual(render(event), "SELL XYZ 3")


class RenderPrices(unittest.TestCase):
    def test_price_at_or_above_one_has_two_decimals(self) -> None:
        self.assertIn("@ 39.10 ", render(_exit(price=39.1004)))
        self.assertIn("@ 1.00 ", render(_exit(price=1.0)))

    def test_price_below_one_has_four_decimals(self) -> None:
        self.assertIn("@ 0.4321 ", render(_exit(price=0.43214)))

    def test_fractional_quantity_is_not_rounded(self) -> None:
        self.assertIn(" 2.5 @", render(_exit(qty=2.5)))


class StopReasonMappingGuard(unittest.TestCase):
    """A reaction-plan kind added to the contract without a stop reason must
    make CI red. The kinds are read from the contract's own decode registry,
    never from a list written here."""

    def test_positive_control_the_registry_is_the_one_the_codec_decodes(self) -> None:
        kinds = set(codec._REACTION_BY_KIND)
        self.assertIn("trailing_stop", kinds)
        self.assertGreaterEqual(len(kinds), 3)

    def test_every_reaction_kind_declares_its_stop_marker(self) -> None:
        self.assertEqual(set(ta.STOP_MOVE_MARKER_BY_REACTION_KIND), set(codec._REACTION_BY_KIND))

    def test_every_stop_marker_has_a_reason(self) -> None:
        for kind, marker in ta.STOP_MOVE_MARKER_BY_REACTION_KIND.items():
            if marker is None:
                continue
            with self.subTest(kind=kind):
                self.assertIn(marker, ta.REASON_BY_STOP_MARKER)

    def test_every_reason_is_reachable_from_a_marker(self) -> None:
        # The other direction: a reason nobody maps to is dead vocabulary.
        markers = {m for m in ta.STOP_MOVE_MARKER_BY_REACTION_KIND.values() if m is not None}
        self.assertEqual(markers, set(ta.REASON_BY_STOP_MARKER))


class StopReason(unittest.TestCase):
    def test_no_marker_is_the_plan_stop(self) -> None:
        self.assertIs(stop_reason(None), ExitReason.PLAN_STOP)

    def test_known_markers(self) -> None:
        self.assertIs(stop_reason("trailed"), ExitReason.TRAILED_STOP)
        self.assertIs(stop_reason("reanchored"), ExitReason.REANCHORED_STOP)

    def test_unknown_marker_falls_back_to_a_generic_stop_and_warns(self) -> None:
        with self.assertLogs(ta.logger, level="WARNING") as logs:
            reason = stop_reason("something_new")
        self.assertIs(reason, ExitReason.STOP)
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(render(_exit(reason=reason)).split(" - ")[1], "stop")


class RealMessagesFromIssue1621(unittest.TestCase):
    """The five LIVE messages quoted in #1621, rebuilt from the values in their
    real journal lines (entry_trails.jsonl ``fired``, standalone_stops.jsonl
    ``stop_filled`` / ``trailed``, ``tranche_fired``)."""

    def test_smmt_entry(self) -> None:
        event = TradeEvent(
            kind=EventKind.ENTRY_FILLED,
            ticker="SMMT",
            label="E1",
            qty=34.0,
            price=16.18,
            order_id="5446498379",
        )
        self.assertEqual(render(event), "BUY SMMT E1 34 @ 16.18 (order 5446498379)")

    def test_ewtx_entry(self) -> None:
        event = TradeEvent(
            kind=EventKind.ENTRY_FILLED,
            ticker="EWTX",
            label="E1",
            qty=11.0,
            price=39.1004,
            order_id="5446900424",
        )
        self.assertEqual(render(event), "BUY EWTX E1 11 @ 39.10 (order 5446900424)")

    def test_uber_plan_stop(self) -> None:
        event = _exit(ticker="UBER", qty=8.0, price=67.99, order_id="5440923753")
        self.assertEqual(
            render(event), "SELL UBER 8 @ 67.99 - plan stop - position closed (order 5440923753)"
        )

    def test_smmt_take_profit(self) -> None:
        event = TradeEvent(
            kind=EventKind.EXIT_SUBMITTED,
            ticker="SMMT",
            label="TP1",
            qty=34.0,
            reason=ExitReason.TAKE_PROFIT,
            order_id="5447570154",
            position_closed=True,
        )
        self.assertEqual(
            render(event),
            "SELL SMMT TP1 34 - take-profit, market sell sent - position closed (order 5447570154)",
        )

    def test_asts_trailed_stop(self) -> None:
        event = _exit(
            ticker="ASTS",
            qty=7.0,
            price=62.37,
            reason=ExitReason.TRAILED_STOP,
            level=62.42304,
            order_id="5446399206",
        )
        self.assertEqual(
            render(event),
            "SELL ASTS 7 @ 62.37 - trailed stop 62.42 - position closed (order 5446399206)",
        )


if __name__ == "__main__":
    unittest.main()
