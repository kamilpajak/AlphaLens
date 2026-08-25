"""The wire boundary VERIFIES a quantity. It never adjusts one.

Prices may be adjusted at the wire — `_quantize_price` snaps to the tick,
because a sub-25-bps price move is economically inert. A share is not. An
off-lattice quantity arriving at the wire is an upstream defect, and silently
rounding it would be the same class of mistake this workstream exists to
remove: two quantizers that can disagree.

So this is deliberately asymmetric with the price path, and the asymmetry is
the point.

NO-OP TODAY. Saxo reports `IncrementSize 1` for our cohort, and everything
upstream still emits whole shares, so every real placement passes unchanged.
The value is that P5 cannot silently regress past it.
"""

from __future__ import annotations

import unittest

from alphalens_pipeline.brokers.saxo.broker import SaxoBroker
from broker_contract.contract import OrderRejectedError

_WHOLE_SHARE = {
    "Uic": 23474,
    "MinimumTradeSize": 1,
    "IncrementSize": 1,
    "AmountDecimals": 0,
    "CurrencyCode": "USD",
}
_FRACTIONAL = {
    "Uic": 9,
    "MinimumTradeSize": 0.001,
    "IncrementSize": 0.001,
    "AmountDecimals": 3,
    "CurrencyCode": "USD",
}


class TestWireVerificationIsANoOpToday(unittest.TestCase):
    def test_every_whole_share_quantity_passes_unchanged(self) -> None:
        for qty in (1, 2, 6, 27, 46, 100, 1_000):
            with self.subTest(qty=qty):
                self.assertEqual(SaxoBroker._verify_quantity(qty, _WHOLE_SHARE, label="qty"), qty)

    def test_a_float_valued_whole_share_passes(self) -> None:
        # Owned quantities arrive as floats off the wire even though they are
        # whole numbers; 46.0 must not be treated as suspicious.
        self.assertEqual(SaxoBroker._verify_quantity(46.0, _WHOLE_SHARE, label="qty"), 46.0)


class TestWireVerificationRefuses(unittest.TestCase):
    def test_an_off_lattice_quantity_is_refused_not_rounded(self) -> None:
        # THE asymmetry with the price path. 0.669 shares on a whole-share
        # venue is an upstream defect; rounding it here would hide the defect
        # and put a phantom share on the wire.
        with self.assertRaises(OrderRejectedError):
            SaxoBroker._verify_quantity(0.669, _WHOLE_SHARE, label="qty")

    def test_below_the_venue_minimum_is_refused(self) -> None:
        details = {**_FRACTIONAL, "MinimumTradeSize": 0.5}
        with self.assertRaises(OrderRejectedError):
            SaxoBroker._verify_quantity(0.1, details, label="qty")

    def test_non_finite_and_non_positive_are_refused(self) -> None:
        for bad in (0, -1, float("nan"), float("inf")):
            with self.subTest(bad=bad), self.assertRaises(OrderRejectedError):
                SaxoBroker._verify_quantity(bad, _WHOLE_SHARE, label="qty")

    def test_the_refusal_names_the_label_and_the_quantity(self) -> None:
        try:
            SaxoBroker._verify_quantity(0.669, _WHOLE_SHARE, label="new_qty")
        except OrderRejectedError as exc:
            self.assertIn("new_qty", str(exc))
            self.assertIn("0.669", str(exc))
        else:  # pragma: no cover - the assertion above must fire
            self.fail("expected a refusal")


class TestWireVerificationDegradesWhenTheVenueSaysNothing(unittest.TestCase):
    def test_an_unusable_details_payload_passes_the_quantity_through(self) -> None:
        # P4 must not change behaviour. A venue that reports no lattice cannot
        # be verified against one, and refusing here would break placement on
        # a path that works today. The refusal for "no rules" belongs upstream,
        # at sizing time, where it can be a pick-level decision rather than a
        # dead order.
        for details in ({}, {"Uic": 1}, {"Uic": 1, "IncrementSize": "nonsense"}):
            with self.subTest(details=details):
                self.assertEqual(SaxoBroker._verify_quantity(7, details, label="qty"), 7)

    def test_a_venue_that_contradicts_itself_is_refused_not_degraded(self) -> None:
        # "The venue said nothing" and "the venue's own numbers disagree" are
        # different failures and must not share a catch. Absence degrades — a
        # working placement path must not die because a payload is thin.
        # Contradiction is a refusal: a 0.001 step at two decimal places is not
        # a lattice, and passing the quantity through would silently disable
        # wire verification on exactly the payload that needed it most.
        contradictory = {
            "Uic": 1,
            "IncrementSize": 0.001,
            "AmountDecimals": 2,
            "MinimumTradeSize": 0.001,
        }
        with self.assertRaises(OrderRejectedError):
            SaxoBroker._verify_quantity(0.665, contradictory, label="qty")

    def test_a_fractional_venue_accepts_its_own_fractions(self) -> None:
        self.assertEqual(SaxoBroker._verify_quantity(0.669, _FRACTIONAL, label="qty"), 0.669)
        with self.assertRaises(OrderRejectedError):
            SaxoBroker._verify_quantity(0.6695, _FRACTIONAL, label="qty")


if __name__ == "__main__":
    unittest.main()
