"""The adapter reports what the venue said; the pipeline decides what it means.

The `fx.py` split, applied to quantity: `SaxoBroker._quantity_rules_from_details`
reads the vendor dict VERBATIM with honest `None` for anything absent, and
`execution.build_quantity_lattice` is where an absence becomes a refusal.

The fields have been arriving all along — `get_instrument_details` is called at
eight placement sites — and were dropped on the floor. Verified 2026-08-25:
`MinimumTradeSize`, `IncrementSize`, `LotSize`, `AmountDecimals`,
`MinimumOrderValue` and `FractionalOrderEnabled` appeared nowhere in source.
"""

from __future__ import annotations

import unittest

from alphalens_pipeline.brokers.execution import build_quantity_lattice
from alphalens_pipeline.brokers.saxo.broker import SaxoBroker
from broker_contract.quantity import InstrumentQuantityRules
from broker_contract.sizing import TradeSetupNotPlannableError

# The shape live-probed on our own cohort (CDR, and the US names): whole shares.
_SAXO_WHOLE_SHARE_DETAILS = {
    "Uic": 23474,
    "MinimumTradeSize": 1,
    "IncrementSize": 1,
    "LotSize": 1,
    "AmountDecimals": 0,
    "MinimumOrderValue": 0,
    "FractionalOrderEnabled": False,
    "CurrencyCode": "USD",
}


class TestAdapterReportsVerbatim(unittest.TestCase):
    def test_the_live_probed_shape_is_read_field_for_field(self) -> None:
        rules = SaxoBroker._quantity_rules_from_details(_SAXO_WHOLE_SHARE_DETAILS)
        self.assertEqual(rules.quantity_step, 1.0)
        self.assertEqual(rules.min_quantity, 1.0)
        self.assertEqual(rules.quantity_precision, 0)
        self.assertEqual(rules.round_lot, 1.0)
        self.assertIs(rules.fractional_enabled, False)
        self.assertEqual(rules.currency, "USD")
        self.assertIn("saxo", rules.source)

    def test_an_absent_field_is_reported_as_None_never_defaulted(self) -> None:
        # The whole point. Substituting 1 for "the vendor did not say" is the
        # present bug wearing a nicer name — the caller could no longer tell a
        # whole-share venue from an unanswered question.
        rules = SaxoBroker._quantity_rules_from_details({"Uic": 1})
        self.assertIsNone(rules.quantity_step)
        self.assertIsNone(rules.min_quantity)
        self.assertIsNone(rules.quantity_precision)
        self.assertIsNone(rules.fractional_enabled)

    def test_a_fractional_venue_is_read_as_one(self) -> None:
        rules = SaxoBroker._quantity_rules_from_details(
            {
                "Uic": 9,
                "IncrementSize": 0.001,
                "MinimumTradeSize": 0.001,
                "AmountDecimals": 3,
                "FractionalOrderEnabled": True,
            }
        )
        self.assertEqual(rules.quantity_step, 0.001)
        self.assertIs(rules.fractional_enabled, True)

    def test_hostile_values_do_not_raise_in_the_adapter(self) -> None:
        # The adapter reports; it does not judge. A garbage value becomes an
        # honest None here and is refused by policy one layer up, where the
        # refusal can carry a reason.
        rules = SaxoBroker._quantity_rules_from_details(
            {
                "Uic": 2,
                "IncrementSize": "not-a-number",
                "MinimumTradeSize": None,
                "AmountDecimals": [],
                "LotSize": float("nan"),
            }
        )
        self.assertIsNone(rules.quantity_step)
        self.assertIsNone(rules.min_quantity)
        self.assertIsNone(rules.quantity_precision)
        self.assertIsNone(rules.round_lot)


class TestPolicyTurnsAbsenceIntoARefusal(unittest.TestCase):
    def test_the_live_probed_shape_builds_the_whole_share_lattice(self) -> None:
        lattice = build_quantity_lattice(
            SaxoBroker._quantity_rules_from_details(_SAXO_WHOLE_SHARE_DETAILS)
        )
        self.assertEqual(lattice.step, 1.0)
        self.assertEqual(lattice.min_qty, 1.0)
        self.assertEqual(lattice.precision, 0)
        # Half a step is 0.5 — the number QTY_PRECISION has always been. The
        # migration is behaviour-preserving on Saxo by derivation, not luck.
        self.assertEqual(lattice.step / 2.0, 0.5)

    def test_a_missing_step_is_refused_not_guessed(self) -> None:
        with self.assertRaises(TradeSetupNotPlannableError):
            build_quantity_lattice(InstrumentQuantityRules(broker_instrument_id="1"))

    def test_a_non_positive_step_is_refused(self) -> None:
        with self.assertRaises(TradeSetupNotPlannableError):
            build_quantity_lattice(
                InstrumentQuantityRules(broker_instrument_id="1", quantity_step=0.0)
            )

    def test_a_step_the_precision_cannot_express_is_refused(self) -> None:
        with self.assertRaises(TradeSetupNotPlannableError):
            build_quantity_lattice(
                InstrumentQuantityRules(
                    broker_instrument_id="1",
                    quantity_step=0.001,
                    quantity_precision=2,
                    min_quantity=0.001,
                )
            )

    def test_a_missing_minimum_falls_back_to_one_step_not_to_zero(self) -> None:
        # A venue that states a step but no minimum can still trade one step.
        lattice = build_quantity_lattice(
            InstrumentQuantityRules(
                broker_instrument_id="1", quantity_step=0.5, quantity_precision=1
            )
        )
        self.assertEqual(lattice.min_qty, 0.5)

    def test_round_lot_is_carried_but_never_becomes_the_step(self) -> None:
        # A US equity may have a 100-share round lot and still accept odd lots.
        # Treating it as a mandatory step would refuse every ordinary order.
        lattice = build_quantity_lattice(
            SaxoBroker._quantity_rules_from_details({**_SAXO_WHOLE_SHARE_DETAILS, "LotSize": 100})
        )
        self.assertEqual(lattice.step, 1.0)
        self.assertEqual(lattice.round_lot, 100.0)


if __name__ == "__main__":
    unittest.main()
