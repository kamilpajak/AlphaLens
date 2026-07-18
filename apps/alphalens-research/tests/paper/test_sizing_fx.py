"""FX-leg sizing tests (``paper/sizing.py`` + ``paper/fx.py``).

Pins the FX-leg design memo §4.2 math: the conversion happens ONCE, between
the account-currency notional and the per-tier qty division; prices are
NEVER converted; the same-currency ``fx=None`` path is byte-exact vs the
pre-FX-leg output; the gross guard compares in ONE currency through the
plan's OWN rate (no buffer on the ceiling).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import math
import unittest

from alphalens_pipeline.paper.constants import GROSS_SAFETY_FRAC
from alphalens_pipeline.paper.fx import FxConversion, FxRateQuote
from alphalens_pipeline.paper.sizing import (
    TradeSetupNotPlannableError,
    compute_setup_plan,
    setup_plan_gross_guard_limit,
    setup_plan_gross_notional,
)

_ASOF = dt.datetime(2026, 7, 18, 10, 0, 0, tzinfo=dt.UTC)
_EQUITY_EUR = 1_000_000.0


def _fx(
    *,
    rate: float = 4.34,
    buffer_pct: float = 1.0,
    account: str = "EUR",
    instrument: str = "PLN",
) -> FxConversion:
    return FxConversion(
        account_currency=account,
        instrument_currency=instrument,
        rate=rate,
        sizing_buffer_pct=buffer_pct,
        source="saxo-fxspot-uic-1343-mid",
        price_type="Tradable",
        bid=4.3331,
        ask=4.3469,
        asof=_ASOF,
    )


def _setup(*, suggested_size_pct: float = 6.0, entry_tiers: list | None = None) -> dict:
    if entry_tiers is None:
        entry_tiers = [
            {"limit": 100.0, "alloc_pct": 50.0, "atr_distance": 0.0, "tag": "t0"},
            {"limit": 95.0, "alloc_pct": 30.0, "atr_distance": 1.0, "tag": "t1"},
            {"limit": 90.0, "alloc_pct": 20.0, "atr_distance": 2.0, "tag": "t2"},
        ]
    return {
        "schema_version": "1.1.0",
        "status": "OK",
        "asof_close": 100.0,
        "atr": 1.5,
        "disaster_stop": 85.0,
        "suggested_size_pct": suggested_size_pct,
        "order_ttl_days": 5,
        "entry_tiers": entry_tiers,
        "tp_tranches": [{"target": 110.0, "tranche_pct": 100.0, "r_multiple": 1.0, "tag": "tp0"}],
    }


class TestSameCurrencyNoOpByteExact(unittest.TestCase):
    """fx=None must preserve today's US sizing byte-exact (memo §4.3 item 1)."""

    def test_fx_none_plan_is_field_identical_to_the_implicit_default(self):
        setup = _setup()
        implicit = compute_setup_plan(
            brief_trade_setup=setup, paper_equity=_EQUITY_EUR, scale_factor=0.05
        )
        explicit = compute_setup_plan(
            brief_trade_setup=setup, paper_equity=_EQUITY_EUR, scale_factor=0.05, fx=None
        )
        self.assertEqual(dataclasses.asdict(implicit), dataclasses.asdict(explicit))

    def test_fx_none_golden_numbers_unchanged(self):
        # The pre-FX-leg golden: 6% x 0.05 -> 0.3% -> EUR 3000; qty floors
        # 15 / 9 / 6 on the 100/95/90 tiers at 50/30/20 alloc.
        plan = compute_setup_plan(
            brief_trade_setup=_setup(), paper_equity=_EQUITY_EUR, scale_factor=0.05
        )
        self.assertAlmostEqual(plan.total_notional, 3_000.0)
        self.assertEqual([t.qty for t in plan.entry_tiers], [15, 9, 6])
        self.assertIsNone(plan.fx)

    def test_sizing_notional_is_identity_when_fx_is_none(self):
        plan = compute_setup_plan(
            brief_trade_setup=_setup(), paper_equity=_EQUITY_EUR, scale_factor=0.05
        )
        self.assertEqual(plan.sizing_notional, plan.total_notional)


class TestCrossCurrencyConversion(unittest.TestCase):
    def test_conversion_applied_between_notional_and_qty_division(self):
        # EUR 60,000 (6% x 1.0) x 4.34 x 0.99 = PLN 257,796; tier qtys floor
        # against INSTRUMENT-currency limits.
        plan = compute_setup_plan(
            brief_trade_setup=_setup(), paper_equity=_EQUITY_EUR, scale_factor=1.0, fx=_fx()
        )
        expected_sizing = 60_000.0 * 4.34 * 0.99
        self.assertAlmostEqual(plan.sizing_notional, expected_sizing, places=6)
        expected_qtys = [
            math.floor(expected_sizing * alloc / 100.0 / limit)
            for limit, alloc in ((100.0, 50.0), (95.0, 30.0), (90.0, 20.0))
        ]
        self.assertEqual([t.qty for t in plan.entry_tiers], expected_qtys)

    def test_total_notional_stays_account_currency(self):
        plan = compute_setup_plan(
            brief_trade_setup=_setup(), paper_equity=_EQUITY_EUR, scale_factor=1.0, fx=_fx()
        )
        self.assertAlmostEqual(plan.total_notional, 60_000.0, msg="account-ccy, unconverted")

    def test_prices_are_never_converted(self):
        plan = compute_setup_plan(
            brief_trade_setup=_setup(), paper_equity=_EQUITY_EUR, scale_factor=1.0, fx=_fx()
        )
        self.assertEqual([t.limit_price for t in plan.entry_tiers], [100.0, 95.0, 90.0])
        self.assertEqual(plan.disaster_stop, 85.0)
        self.assertEqual(plan.tp_tranches[0].target_price, 110.0)

    def test_zero_buffer_is_the_pure_rate(self):
        plan = compute_setup_plan(
            brief_trade_setup=_setup(),
            paper_equity=_EQUITY_EUR,
            scale_factor=1.0,
            fx=_fx(buffer_pct=0.0),
        )
        self.assertAlmostEqual(plan.sizing_notional, 60_000.0 * 4.34, places=6)

    def test_buffer_shrinks_qty_at_the_floor(self):
        # One tier, limit exactly at the unbuffered notional: buffered sizing
        # floors to 0 shares while unbuffered would buy 1 — the haircut acts
        # BEFORE the qty floor.
        tiers = [{"limit": 43_400.0, "alloc_pct": 100.0, "atr_distance": 0.0, "tag": "t0"}]
        setup = _setup(suggested_size_pct=1.0, entry_tiers=tiers)
        buffered = compute_setup_plan(
            brief_trade_setup=setup, paper_equity=_EQUITY_EUR, scale_factor=1.0, fx=_fx()
        )
        unbuffered = compute_setup_plan(
            brief_trade_setup=setup,
            paper_equity=_EQUITY_EUR,
            scale_factor=1.0,
            fx=_fx(buffer_pct=0.0),
        )
        self.assertEqual(buffered.entry_tiers[0].qty, 0)
        self.assertEqual(unbuffered.entry_tiers[0].qty, 1)

    def test_fx_object_is_carried_on_the_plan(self):
        fx = _fx()
        plan = compute_setup_plan(
            brief_trade_setup=_setup(), paper_equity=_EQUITY_EUR, scale_factor=1.0, fx=fx
        )
        self.assertIs(plan.fx, fx)


class TestFxRefusals(unittest.TestCase):
    def test_non_positive_rate_refused(self):
        with self.assertRaises(TradeSetupNotPlannableError):
            compute_setup_plan(
                brief_trade_setup=_setup(),
                paper_equity=_EQUITY_EUR,
                scale_factor=1.0,
                fx=_fx(rate=0.0),
            )

    def test_same_currency_fx_conversion_refused(self):
        # Same-currency must pass fx=None (strict no-op) — a EUR->EUR
        # "conversion" is a caller bug, not a 1.0 rate.
        with self.assertRaises(TradeSetupNotPlannableError):
            compute_setup_plan(
                brief_trade_setup=_setup(),
                paper_equity=_EQUITY_EUR,
                scale_factor=1.0,
                fx=_fx(instrument="EUR"),
            )


class TestGrossGuardSingleCurrency(unittest.TestCase):
    def test_same_currency_limit_is_frac_times_equity(self):
        plan = compute_setup_plan(
            brief_trade_setup=_setup(), paper_equity=_EQUITY_EUR, scale_factor=1.0
        )
        self.assertAlmostEqual(setup_plan_gross_guard_limit(plan), GROSS_SAFETY_FRAC * _EQUITY_EUR)

    def test_cross_currency_limit_converts_equity_through_the_plan_rate(self):
        # SAME rate object as sizing, NO buffer on the ceiling.
        plan = compute_setup_plan(
            brief_trade_setup=_setup(), paper_equity=_EQUITY_EUR, scale_factor=1.0, fx=_fx()
        )
        self.assertAlmostEqual(
            setup_plan_gross_guard_limit(plan), GROSS_SAFETY_FRAC * _EQUITY_EUR * 4.34
        )

    def test_gross_and_limit_share_the_instrument_currency(self):
        # Both sides of the compare are instrument-ccy: a sane plan's gross
        # sits far below the converted ceiling.
        plan = compute_setup_plan(
            brief_trade_setup=_setup(), paper_equity=_EQUITY_EUR, scale_factor=1.0, fx=_fx()
        )
        self.assertLess(setup_plan_gross_notional(plan), setup_plan_gross_guard_limit(plan))


class TestFxRateQuotePriceType(unittest.TestCase):
    def test_symmetric_sides_collapse_to_one_token(self):
        quote = FxRateQuote(
            base_currency="EUR",
            quote_currency="PLN",
            mid=4.34,
            bid=None,
            ask=None,
            price_type_bid="Tradable",
            price_type_ask="Tradable",
            market_state=None,
            source="s",
            asof=_ASOF,
        )
        self.assertEqual(quote.price_type, "Tradable")

    def test_asymmetric_sides_are_joined_verbatim(self):
        quote = FxRateQuote(
            base_currency="EUR",
            quote_currency="PLN",
            mid=4.34,
            bid=None,
            ask=None,
            price_type_bid="Tradable",
            price_type_ask="OldIndicative",
            market_state=None,
            source="s",
            asof=_ASOF,
        )
        self.assertEqual(quote.price_type, "Tradable/OldIndicative")


if __name__ == "__main__":
    unittest.main()
