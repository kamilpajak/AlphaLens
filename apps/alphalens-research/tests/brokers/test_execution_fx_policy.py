"""FX-leg policy tests for ``brokers/execution.py`` (design memo
``docs/research/saxo_fx_leg_gpw_design_2026_07_18.md`` §4.3).

Covers :func:`build_fx_conversion` (the policy-acceptance seam between the
adapter's verbatim ``FxRateQuote`` and sizing's ``FxConversion`` — injected
clock for staleness, refuse-to-size on every bad-quote class, NEVER a 1.0
fallback) and the operator-locked policy constant values.
"""

from __future__ import annotations

import datetime as dt
import unittest

from alphalens_pipeline.brokers import execution
from alphalens_pipeline.brokers.execution import build_fx_conversion
from broker_contract.fx import FxRateQuote
from broker_contract.sizing import TradeSetupNotPlannableError

_ASOF = dt.datetime(2026, 7, 18, 10, 0, 0, tzinfo=dt.UTC)
_TRADABLE = "Tradable"
_EURPLN_SOURCE = "saxo-fxspot-uic-1343-mid"


def _quote(**overrides: object) -> FxRateQuote:
    fields: dict = {
        "base_currency": "EUR",
        "quote_currency": "PLN",
        "mid": 4.34,
        "bid": 4.3331,
        "ask": 4.3469,
        "price_type_bid": _TRADABLE,
        "price_type_ask": _TRADABLE,
        "market_state": "Open",
        "source": _EURPLN_SOURCE,
        "asof": _ASOF,
    }
    fields.update(overrides)
    return FxRateQuote(**fields)


class TestBuildFxConversionHappyPath(unittest.TestCase):
    def test_tradable_quote_freezes_the_conversion_verbatim(self):
        conversion = build_fx_conversion(_quote(), now=_ASOF)

        self.assertEqual(conversion.account_currency, "EUR")
        self.assertEqual(conversion.instrument_currency, "PLN")
        self.assertEqual(conversion.rate, 4.34, "Mid is the ONLY sizing rate")
        self.assertEqual(conversion.bid, 4.3331)
        self.assertEqual(conversion.ask, 4.3469)
        self.assertEqual(conversion.price_type, _TRADABLE)
        self.assertEqual(conversion.source, _EURPLN_SOURCE)
        self.assertEqual(conversion.asof, _ASOF)

    def test_buffer_is_stamped_from_the_execution_policy(self):
        conversion = build_fx_conversion(_quote(), now=_ASOF)
        self.assertEqual(conversion.sizing_buffer_pct, execution._FX_SIZING_BUFFER_PCT)

    def test_indicative_is_accepted(self):
        conversion = build_fx_conversion(
            _quote(price_type_bid="Indicative", price_type_ask="Indicative"), now=_ASOF
        )
        self.assertEqual(conversion.price_type, "Indicative")


class TestBuildFxConversionRefusals(unittest.TestCase):
    """Every refusal is TradeSetupNotPlannableError — no order, no fallback."""

    def test_old_indicative_refused(self):
        # The documented weekend/no-market state (LastUpdated is NOT a
        # data-age signal — freshness is judged from PriceType).
        with self.assertRaises(TradeSetupNotPlannableError) as ctx:
            build_fx_conversion(
                _quote(price_type_bid="OldIndicative", price_type_ask="OldIndicative"),
                now=_ASOF,
            )
        self.assertIn("OldIndicative", str(ctx.exception))

    def test_no_access_refused(self):
        with self.assertRaises(TradeSetupNotPlannableError):
            build_fx_conversion(
                _quote(price_type_bid="NoAccess", price_type_ask="NoAccess"), now=_ASOF
            )

    def test_absent_price_type_refused(self):
        with self.assertRaises(TradeSetupNotPlannableError):
            build_fx_conversion(_quote(price_type_bid=None, price_type_ask=None), now=_ASOF)

    def test_one_bad_side_refuses_even_when_the_other_is_tradable(self):
        with self.assertRaises(TradeSetupNotPlannableError):
            build_fx_conversion(_quote(price_type_ask="OldIndicative"), now=_ASOF)

    def test_missing_mid_refused_never_fabricated_from_bid_ask(self):
        with self.assertRaises(TradeSetupNotPlannableError) as ctx:
            build_fx_conversion(_quote(mid=None), now=_ASOF)
        self.assertIn("Mid", str(ctx.exception))

    def test_non_positive_mid_refused(self):
        with self.assertRaises(TradeSetupNotPlannableError):
            build_fx_conversion(_quote(mid=0.0), now=_ASOF)

    def test_same_currency_pair_refused(self):
        # Same-currency sizing is a strict no-op (fx=None) — a rate for
        # EUR->EUR means the orchestration took a wrong turn.
        with self.assertRaises(TradeSetupNotPlannableError):
            build_fx_conversion(_quote(quote_currency="EUR"), now=_ASOF)

    def test_stale_quote_refused_fresh_quote_accepted(self):
        # Injected clock: one second past the max age refuses, one second
        # under it passes (the belt constant, not LastUpdated).
        max_age = execution._FX_RATE_MAX_AGE_S
        stale_now = _ASOF + dt.timedelta(seconds=max_age + 1)
        fresh_now = _ASOF + dt.timedelta(seconds=max_age - 1)

        with self.assertRaises(TradeSetupNotPlannableError) as ctx:
            build_fx_conversion(_quote(), now=stale_now)
        self.assertIn("stale", str(ctx.exception))
        self.assertIsNotNone(build_fx_conversion(_quote(), now=fresh_now))


class TestFxPolicyConstantValues(unittest.TestCase):
    """Operator-locked values (memo §7 decisions) — a change must be deliberate."""

    def test_locked_values(self):
        self.assertEqual(execution._MISSING_FX_RATE_POLICY, "reject")
        self.assertEqual(execution._FX_RATE_MAX_AGE_S, 300)
        self.assertEqual(execution._FX_ACCEPTED_PRICE_TYPES, ("Tradable", "Indicative"))
        self.assertEqual(execution._FX_RATE_SOURCE, "saxo-fxspot-infoprice-mid")
        self.assertEqual(execution._FX_CONVERSION_POINT, "notional-before-qty")
        self.assertEqual(execution._FX_PRECHECK_RATE_DIVERGENCE_MAX_PCT, 2.0)
        self.assertEqual(execution._FX_SIZING_BUFFER_PCT, 1.0)


if __name__ == "__main__":
    unittest.main()
