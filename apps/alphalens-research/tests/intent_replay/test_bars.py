"""Unit tests for ``intent_replay/bars.py`` — the price-input contract of the
replay (spec sections 4, 4.5 and 5.4).

Every refusal here is a :class:`BarsError` carrying a ``Failure`` whose ``code``
is one of the engine-owned bar codes and whose ``details["reason"]`` names the
rule, mirroring ``broker_contract.trade_intent.validate``.
"""

from __future__ import annotations

import math
import unittest

from intent_replay.bars import (
    BARS_EMPTY_CODE,
    BARS_INVALID_CODE,
    BARS_REASONS,
    BARS_UNORDERED_CODE,
    WINDOW_TOO_SHORT_CODE,
    Bar,
    BarsError,
    _refuse,
    check_window_covers,
    parse_bars,
    validate_sequence,
)

WALK_START = 1_758_547_800_000  # 2026-09-22 13:30 UTC, an arbitrary session open


def _bar(t: int, price: float = 100.0) -> Bar:
    return Bar(t=t, open=price, high=price + 1.0, low=price - 1.0, close=price)


def _bars(*ts: int) -> tuple[Bar, ...]:
    return tuple(_bar(t) for t in ts)


def _refusal(exc: BarsError) -> tuple[str, str | None]:
    """The (code, reason) pair a client branches on. Every bar refusal is final."""
    failure = exc.failure
    assert failure.retryable is False, "a refused bar sequence is never retryable"
    return failure.code, failure.details.get("reason")


class SequenceOrderingTest(unittest.TestCase):
    def test_bar_sequence_refuses_unordered(self) -> None:
        cases = {
            "decreasing in the middle": (_bars(1, 2, 5, 3, 6), 3),
            "descending throughout": (_bars(5, 4, 3, 2, 1), 1),
        }
        for label, (bars, index) in cases.items():
            with self.subTest(label):
                with self.assertRaises(BarsError) as ctx:
                    validate_sequence(bars)
                self.assertEqual(_refusal(ctx.exception), (BARS_UNORDERED_CODE, "decreasing"))
                self.assertEqual(ctx.exception.failure.details["index"], index)

    def test_bar_sequence_refuses_duplicate_timestamps(self) -> None:
        with self.assertRaises(BarsError) as ctx:
            validate_sequence(_bars(1, 2, 2, 3))
        self.assertEqual(_refusal(ctx.exception), (BARS_UNORDERED_CODE, "duplicate"))
        self.assertEqual(ctx.exception.failure.details["index"], 2)

    def test_bar_sequence_refuses_empty(self) -> None:
        with self.assertRaises(BarsError) as ctx:
            validate_sequence(())
        self.assertEqual(_refusal(ctx.exception), (BARS_EMPTY_CODE, None))

    def test_an_ordered_sequence_is_returned_unchanged(self) -> None:
        for label, bars in {"three bars": _bars(1, 2, 3), "a single bar": _bars(1)}.items():
            with self.subTest(label):
                self.assertEqual(validate_sequence(list(bars)), bars)


class BarValueTest(unittest.TestCase):
    def test_bar_rejects_non_finite_prices(self) -> None:
        cases = {
            "nan high": ({"high": math.nan}, "high"),
            "inf low": ({"low": math.inf}, "low"),
            "negative inf close": ({"close": -math.inf}, "close"),
        }
        for label, (override, field) in cases.items():
            with self.subTest(label):
                prices = {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, **override}
                with self.assertRaises(BarsError) as ctx:
                    Bar(t=1, **prices)
                self.assertEqual(_refusal(ctx.exception), (BARS_INVALID_CODE, "numeric_not_finite"))
                self.assertEqual(ctx.exception.failure.details["field"], field)


class WindowCoverageTest(unittest.TestCase):
    def test_window_too_short_when_bars_end_before_walk_start(self) -> None:
        with self.assertRaises(BarsError) as ctx:
            check_window_covers(_bars(WALK_START - 2, WALK_START - 1), WALK_START)
        self.assertEqual(_refusal(ctx.exception), (WINDOW_TOO_SHORT_CODE, "ends_before_walk_start"))

    def test_window_too_short_when_bars_begin_after_walk_start(self) -> None:
        with self.assertRaises(BarsError) as ctx:
            check_window_covers(_bars(WALK_START + 1, WALK_START + 2), WALK_START)
        self.assertEqual(
            _refusal(ctx.exception), (WINDOW_TOO_SHORT_CODE, "begins_after_walk_start")
        )

    def test_window_covering_walk_start_is_accepted(self) -> None:
        cases = {
            "strictly inside": _bars(WALK_START - 1, WALK_START + 1),
            "first bar at walk_start": _bars(WALK_START, WALK_START + 1),
            "last bar at walk_start": _bars(WALK_START - 1, WALK_START),
            "single bar at walk_start": _bars(WALK_START),
        }
        for label, bars in cases.items():
            with self.subTest(label):
                self.assertIsNone(check_window_covers(bars, WALK_START))

    def test_window_check_refuses_empty(self) -> None:
        with self.assertRaises(BarsError) as ctx:
            check_window_covers((), WALK_START)
        self.assertEqual(_refusal(ctx.exception), (BARS_EMPTY_CODE, None))


class ReasonVocabularyTest(unittest.TestCase):
    def test_an_unregistered_reason_cannot_ship(self) -> None:
        # The vocabulary is closed at the raise site, not only in a test of the
        # constant: a refusal built with a reason outside BARS_REASONS is a
        # programming error, surfaced as ValueError rather than published.
        with self.assertRaises(ValueError):
            _refuse(BARS_UNORDERED_CODE, "made up", reason="not_a_registered_reason")

    def test_reason_vocabulary_is_closed(self) -> None:
        # A constant, deliberately: the mirror of INTENT_INVALID_REASONS, which the
        # raise path asserts against so an unregistered reason cannot ship.
        self.assertEqual(
            set(BARS_REASONS),
            {
                "decreasing",
                "duplicate",
                "numeric_not_finite",
                "ends_before_walk_start",
                "begins_after_walk_start",
                "not_a_list",
                "wrong_type",
                "missing_key",
                "unknown_key",
            },
        )


class ParseBarsTest(unittest.TestCase):
    """The published bar shape: ONE JSON array of {t, open, high, low, close}
    objects (spec section 5.4). The ENGINE owns the shape; the CLI only reads the
    file, so a file that is not JSON at all is the CLI's ``bars_malformed``.

    Every refusal here widens ``bars_invalid`` rather than adding a code: all
    four new reasons say "this is not a usable bar", which is the mode that code
    already names."""

    def _ok(self, t: int) -> dict[str, object]:
        return {"t": t, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5}

    def test_a_list_of_objects_becomes_bars_in_order(self) -> None:
        bars = parse_bars([self._ok(1), self._ok(2)])
        self.assertEqual([bar.t for bar in bars], [1, 2])
        self.assertEqual(bars[0], Bar(t=1, open=100.0, high=101.0, low=99.0, close=100.5))

    def test_an_empty_list_parses_and_is_the_sequence_check_s_refusal(self) -> None:
        # Not refused here: ``bars_empty`` is validate_sequence's, and answering
        # it twice would make the caller's order of checks change the code.
        self.assertEqual(parse_bars([]), ())

    def test_an_integer_price_is_accepted(self) -> None:
        bars = parse_bars([{"t": 1, "open": 100, "high": 101, "low": 99, "close": 100}])
        self.assertEqual(bars[0].open, 100.0)

    def test_a_document_that_is_not_a_list_is_refused(self) -> None:
        for data in ({"t": 1}, "[]", 7, None):
            with self.subTest(repr(data)):
                with self.assertRaises(BarsError) as caught:
                    parse_bars(data)
                self.assertEqual(_refusal(caught.exception), (BARS_INVALID_CODE, "not_a_list"))

    def test_a_bar_that_is_not_an_object_is_refused_with_its_index(self) -> None:
        with self.assertRaises(BarsError) as caught:
            parse_bars([self._ok(1), [1, 2, 3]])
        self.assertEqual(_refusal(caught.exception), (BARS_INVALID_CODE, "wrong_type"))
        self.assertEqual(caught.exception.failure.details["index"], 1)

    def test_a_missing_key_is_refused_and_named(self) -> None:
        bar = self._ok(1)
        del bar["low"]
        with self.assertRaises(BarsError) as caught:
            parse_bars([bar])
        self.assertEqual(_refusal(caught.exception), (BARS_INVALID_CODE, "missing_key"))
        self.assertEqual(caught.exception.failure.details["keys"], ["low"])

    def test_a_key_the_bar_does_not_model_is_refused(self) -> None:
        # ``hgih`` silently ignored would be a bar its author did not send - the
        # argument config_invalid.unknown_key already carries.
        bar = self._ok(1)
        bar["hgih"] = 101.0
        with self.assertRaises(BarsError) as caught:
            parse_bars([bar])
        self.assertEqual(_refusal(caught.exception), (BARS_INVALID_CODE, "unknown_key"))
        self.assertEqual(caught.exception.failure.details["keys"], ["hgih"])

    def test_a_non_integer_t_is_refused(self) -> None:
        bar = self._ok(1)
        bar["t"] = 1.5
        with self.assertRaises(BarsError) as caught:
            parse_bars([bar])
        self.assertEqual(_refusal(caught.exception), (BARS_INVALID_CODE, "wrong_type"))
        self.assertEqual(caught.exception.failure.details["field"], "t")

    def test_a_boolean_is_not_a_number(self) -> None:
        # ``bool`` is an ``int`` subclass, so a bare isinstance check would let
        # ``true`` through as 1.
        for field in ("t", "open"):
            with self.subTest(field):
                bar = self._ok(1)
                bar[field] = True
                with self.assertRaises(BarsError) as caught:
                    parse_bars([bar])
                self.assertEqual(_refusal(caught.exception), (BARS_INVALID_CODE, "wrong_type"))

    def test_a_string_price_is_refused(self) -> None:
        bar = self._ok(1)
        bar["high"] = "101.0"
        with self.assertRaises(BarsError) as caught:
            parse_bars([bar])
        self.assertEqual(_refusal(caught.exception), (BARS_INVALID_CODE, "wrong_type"))

    def test_a_non_finite_price_is_still_the_value_type_s_refusal(self) -> None:
        bar = self._ok(1)
        bar["low"] = math.nan
        with self.assertRaises(BarsError) as caught:
            parse_bars([bar])
        self.assertEqual(_refusal(caught.exception), (BARS_INVALID_CODE, "numeric_not_finite"))
