"""Per-venue fee cards + the currency-fact derivation for the #1112 cost gates
(#1238 PR 3).

The Saxo PL Classic schedule differs per venue: US equities pay 0.08% with a
USD 1 per-fill minimum, WSE (GPW) equities pay 0.12% with a PLN 10 per-fill
minimum, and both minimums are denominated in the INSTRUMENT currency — the
notional the fee equation prices is already instrument-currency, so no
conversion enters the model. ``cost_gate_facts`` turns the journaled currency
stamps into the facts the exit-side gates could not see before (#1112 gates
used the conservative ``COST_GATE_*`` constants); unknown stamps keep exactly
those conservative constants (over-refuse, never under-charge).
"""

from __future__ import annotations

import unittest

from alphalens_pipeline.brokers.automanager.costs import (
    COMMISSION_RATE,
    MIN_COMMISSION_USD,
    US_FEE_CARD,
    WSE_FEE_CARD,
    CostGateFacts,
    cost_gate_facts,
    fee_card_for,
    min_profitable_exit_price,
    round_trip_fee_bps,
)


class TestFeeCards(unittest.TestCase):
    def test_us_card_mirrors_the_module_constants(self) -> None:
        self.assertEqual(US_FEE_CARD.commission_rate, COMMISSION_RATE)
        self.assertEqual(US_FEE_CARD.min_commission, MIN_COMMISSION_USD)

    def test_wse_card_is_the_saxo_pl_classic_schedule(self) -> None:
        # Saxo PL Classic, read 2026-09-02 (home.saxo/pl-pl): 0.12% min 10 PLN.
        self.assertEqual(WSE_FEE_CARD.commission_rate, 0.0012)
        self.assertEqual(WSE_FEE_CARD.min_commission, 10.0)

    def test_fee_card_lookup_by_instrument_currency(self) -> None:
        self.assertIs(fee_card_for("USD"), US_FEE_CARD)
        self.assertIs(fee_card_for("PLN"), WSE_FEE_CARD)
        self.assertIsNone(fee_card_for("CHF"))
        self.assertIsNone(fee_card_for(None))
        self.assertIsNone(fee_card_for(""))


class TestRoundTripFeeBpsPerCard(unittest.TestCase):
    def test_default_card_is_byte_identical_to_the_old_us_model(self) -> None:
        self.assertEqual(
            round_trip_fee_bps(10_000.0, fx_applies=True),
            round_trip_fee_bps(10_000.0, fx_applies=True, card=US_FEE_CARD),
        )

    def test_wse_card_charges_the_pln_minimum_and_rate(self) -> None:
        # 1000 PLN notional: ad-valorem 1.2 PLN < min 10 PLN -> 2*10 PLN round
        # trip = 200 bps; no FX leg on a PLN account.
        self.assertAlmostEqual(
            round_trip_fee_bps(1_000.0, fx_applies=False, card=WSE_FEE_CARD), 200.0
        )

    def test_wse_ad_valorem_dominates_large_notionals(self) -> None:
        # 100k PLN: ad-valorem 120 PLN > min 10 PLN -> 2*120/100k = 24 bps.
        self.assertAlmostEqual(
            round_trip_fee_bps(100_000.0, fx_applies=False, card=WSE_FEE_CARD), 24.0
        )


class TestCostGateFacts(unittest.TestCase):
    def test_pln_instrument_on_pln_account_drops_fx_and_uses_the_wse_card(self) -> None:
        facts = cost_gate_facts(instrument_currency="PLN", sizing_currency="PLN")
        self.assertFalse(facts.fx_applies)
        self.assertTrue(facts.min_commission_applies)
        self.assertIs(facts.card, WSE_FEE_CARD)

    def test_usd_instrument_on_pln_account_keeps_the_fx_leg(self) -> None:
        facts = cost_gate_facts(instrument_currency="USD", sizing_currency="PLN")
        self.assertTrue(facts.fx_applies)
        self.assertIs(facts.card, US_FEE_CARD)

    def test_unknown_instrument_currency_is_conservative_legacy(self) -> None:
        for facts in (
            cost_gate_facts(instrument_currency=None, sizing_currency="PLN"),
            cost_gate_facts(instrument_currency="CHF", sizing_currency="PLN"),
            cost_gate_facts(instrument_currency="USD", sizing_currency=None),
        ):
            self.assertEqual(facts, CostGateFacts.legacy())

    def test_legacy_facts_reproduce_the_old_constants(self) -> None:
        legacy = CostGateFacts.legacy()
        self.assertTrue(legacy.fx_applies)
        self.assertTrue(legacy.min_commission_applies)
        self.assertIs(legacy.card, US_FEE_CARD)


class TestMinProfitableExitPricePerFacts(unittest.TestCase):
    def test_default_facts_are_byte_identical_to_the_old_bar(self) -> None:
        self.assertEqual(
            min_profitable_exit_price(entry_price=60.0, qty=10.0),
            min_profitable_exit_price(entry_price=60.0, qty=10.0, facts=CostGateFacts.legacy()),
        )

    def test_pln_on_pln_bar_is_lower_at_realistic_gpw_size(self) -> None:
        # CDR-scale position (~9240 PLN): the WSE ad-valorem (0.12%) clears
        # the 10 PLN minimum and there is no FX round trip, so the exit bar
        # sits BELOW the conservative legacy bar (US minimum semantics + FX).
        pln_facts = cost_gate_facts(instrument_currency="PLN", sizing_currency="PLN")
        legacy = min_profitable_exit_price(entry_price=231.0, qty=40.0)
        pln = min_profitable_exit_price(entry_price=231.0, qty=40.0, facts=pln_facts)
        self.assertIsNotNone(legacy)
        self.assertIsNotNone(pln)
        self.assertLess(pln, legacy)

    def test_pln_minimum_dominates_small_notionals_raising_the_bar(self) -> None:
        # 600 PLN notional: 2 x 10 PLN minimum = ~333 bps round trip — MORE
        # than the legacy bar even after dropping FX. The stricter bar is the
        # honest one; the fee really is that punishing on a tiny GPW position.
        pln_facts = cost_gate_facts(instrument_currency="PLN", sizing_currency="PLN")
        legacy = min_profitable_exit_price(entry_price=60.0, qty=10.0)
        pln = min_profitable_exit_price(entry_price=60.0, qty=10.0, facts=pln_facts)
        self.assertIsNotNone(legacy)
        self.assertIsNotNone(pln)
        self.assertGreater(pln, legacy)


if __name__ == "__main__":
    unittest.main()
