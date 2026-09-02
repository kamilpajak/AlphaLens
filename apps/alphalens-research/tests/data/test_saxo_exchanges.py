"""Pins for the shared Saxo venue reference data (``data/alt_data/saxo_exchanges.py``):

- ``MIC_TO_SAXO_EXCHANGE_ID`` covers XASE (NYSE American) so AMEX listings
  (live-verified UUUU:xase, uic 549463) stop being unresolvable.
- ``SAXO_TICKER_ALIASES`` is well-formed and carries the LAC -> LAC_NEW
  positive control, so the map cannot rot empty silently.
- ``US_MIC_PROBE_ORDER`` (the shared placement-side + day-1-gate probe order)
  only names venues the MIC map can actually resolve.
"""

from __future__ import annotations

import unittest

from alphalens_pipeline.data.alt_data.saxo_exchanges import (
    MIC_TO_SAXO_EXCHANGE_ID,
    SAXO_TICKER_ALIASES,
    US_MIC_PROBE_ORDER,
    alias_expected_for,
)


class TestMicToSaxoExchangeId(unittest.TestCase):
    def test_xase_maps_to_amex(self) -> None:
        """NYSE American — live-verified UUUU:xase / uic 549463 (2026-08-12)."""
        self.assertEqual(MIC_TO_SAXO_EXCHANGE_ID["XASE"], "AMEX")

    def test_xams_maps_to_ams(self) -> None:
        """Euronext Amsterdam cash equities — live-verified against SIM
        /ref/v1/exchanges (ExchangeId "AMS", Mic XAMS, NL) and by resolving
        ASML:xams / uic 1636 / EUR (2026-09-02). Map entry ONLY (#1238 PR 6):
        XAMS stays out of every probe order and out of arm-manual's
        SUPPORTED_MICS until its own validation arc."""
        self.assertEqual(MIC_TO_SAXO_EXCHANGE_ID["XAMS"], "AMS")


class TestSaxoTickerAliases(unittest.TestCase):
    def test_map_is_well_formed(self) -> None:
        """Every entry maps a non-empty UPPER market ticker to a DIFFERENT
        non-empty UPPER Saxo symbol root plus a positive uic pin — a
        same-as-key, empty, or unpinned entry would be a silent no-op or a
        wrong-instrument hazard (the pin is what makes a stale alias fail
        closed)."""
        for market_ticker, (saxo_symbol, expected_uic) in SAXO_TICKER_ALIASES.items():
            with self.subTest(market_ticker=market_ticker):
                self.assertTrue(market_ticker)
                self.assertTrue(saxo_symbol)
                self.assertEqual(market_ticker, market_ticker.upper())
                self.assertEqual(saxo_symbol, saxo_symbol.upper())
                self.assertNotEqual(market_ticker, saxo_symbol)
                self.assertIsInstance(expected_uic, int)
                self.assertGreater(expected_uic, 0)

    def test_positive_control_lac_maps_to_lac_new_with_uic_pin(self) -> None:
        """LAC -> (LAC_NEW, 38022146) (live-verified 2026-08-12) — a positive
        control so the alias map cannot rot to empty silently."""
        self.assertEqual(SAXO_TICKER_ALIASES["LAC"], ("LAC_NEW", 38022146))

    def test_accessor_upper_cases_and_returns_the_pinned_pair(self) -> None:
        self.assertEqual(alias_expected_for("lac"), ("LAC_NEW", 38022146))
        self.assertIsNone(alias_expected_for("MP"))


class TestUsMicProbeOrder(unittest.TestCase):
    def test_probe_order_is_xnys_xnas_xase(self) -> None:
        self.assertEqual(US_MIC_PROBE_ORDER, ("XNYS", "XNAS", "XASE"))

    def test_every_probed_mic_is_resolvable(self) -> None:
        """A probe-order MIC missing from the venue map would make every
        probe of that venue a guaranteed miss."""
        for mic in US_MIC_PROBE_ORDER:
            with self.subTest(mic=mic):
                self.assertIn(mic, MIC_TO_SAXO_EXCHANGE_ID)


if __name__ == "__main__":
    unittest.main()
