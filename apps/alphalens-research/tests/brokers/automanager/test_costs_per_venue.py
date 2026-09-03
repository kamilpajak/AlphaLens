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
    XAMS_FEE_CARD,
    XETR_FEE_CARD,
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

    def test_xetr_card_is_the_saxo_pl_classic_schedule(self) -> None:
        # Saxo PL Classic, read 2026-09-03 (home.saxo): Xetra 0.08% min EUR 3.
        self.assertEqual(XETR_FEE_CARD.commission_rate, 0.0008)
        self.assertEqual(XETR_FEE_CARD.min_commission, 3.0)

    def test_xams_card_is_the_saxo_pl_classic_schedule(self) -> None:
        # Euronext 0.08% min EUR 2 — a DIFFERENT minimum than Xetra, which is
        # why the lookup re-keys on MIC (#1271): one EUR card cannot price
        # both venues.
        self.assertEqual(XAMS_FEE_CARD.commission_rate, 0.0008)
        self.assertEqual(XAMS_FEE_CARD.min_commission, 2.0)

    def test_fee_card_lookup_by_mic_covers_the_whole_venue_map(self) -> None:
        for mic, card in (
            ("XNYS", US_FEE_CARD),
            ("XNAS", US_FEE_CARD),
            ("XASE", US_FEE_CARD),
            ("XWAR", WSE_FEE_CARD),
            ("XETR", XETR_FEE_CARD),
            ("XAMS", XAMS_FEE_CARD),
        ):
            with self.subTest(mic=mic):
                self.assertIs(fee_card_for(None, exchange_mic=mic), card)

    def test_mic_discriminates_the_two_eur_venues(self) -> None:
        # The whole point of the re-key: EUR alone cannot tell Xetra (min 3)
        # from Euronext Amsterdam (min 2).
        self.assertIs(fee_card_for("EUR", exchange_mic="XETR"), XETR_FEE_CARD)
        self.assertIs(fee_card_for("EUR", exchange_mic="XAMS"), XAMS_FEE_CARD)

    def test_eur_without_mic_falls_back_to_the_xetr_card(self) -> None:
        # Legacy/unstamped records: the Xetra card's HIGHER minimum (EUR 3 vs
        # Euronext's EUR 2) can only over-refuse, never under-charge.
        self.assertIs(fee_card_for("EUR"), XETR_FEE_CARD)

    def test_unknown_mic_falls_back_to_the_currency_key(self) -> None:
        self.assertIs(fee_card_for("USD", exchange_mic="XTKS"), US_FEE_CARD)
        self.assertIsNone(fee_card_for("CHF", exchange_mic="XSWX"))

    def test_mic_wins_over_a_mismatched_currency_stamp(self) -> None:
        # Both stamps come from ONE resolved instrument, so a disagreement is
        # corrupt data; the venue schedule (MIC) wins, and reading its minimum
        # in the notional's currency can only OVER-state the fee (conservative).
        self.assertIs(fee_card_for("USD", exchange_mic="XWAR"), WSE_FEE_CARD)


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

    def test_xetr_card_charges_the_eur_minimum_and_rate(self) -> None:
        # 1000 EUR notional: ad-valorem 0.8 EUR < min 3 EUR -> 2*3 EUR round
        # trip = 60 bps; no FX leg on a EUR account.
        self.assertAlmostEqual(
            round_trip_fee_bps(1_000.0, fx_applies=False, card=XETR_FEE_CARD), 60.0
        )

    def test_xetr_ad_valorem_dominates_large_notionals(self) -> None:
        # 100k EUR: ad-valorem 80 EUR > min 3 EUR -> 2*80/100k = 16 bps.
        self.assertAlmostEqual(
            round_trip_fee_bps(100_000.0, fx_applies=False, card=XETR_FEE_CARD), 16.0
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

    def test_stamped_mic_discriminates_the_eur_venues(self) -> None:
        xetr = cost_gate_facts(
            instrument_currency="EUR", sizing_currency="EUR", exchange_mic="XETR"
        )
        xams = cost_gate_facts(
            instrument_currency="EUR", sizing_currency="EUR", exchange_mic="XAMS"
        )
        self.assertIs(xetr.card, XETR_FEE_CARD)
        self.assertIs(xams.card, XAMS_FEE_CARD)
        self.assertFalse(xetr.fx_applies)

    def test_unstamped_mic_keeps_the_currency_fallback(self) -> None:
        facts = cost_gate_facts(instrument_currency="PLN", sizing_currency="PLN")
        self.assertIs(facts.card, WSE_FEE_CARD)

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
