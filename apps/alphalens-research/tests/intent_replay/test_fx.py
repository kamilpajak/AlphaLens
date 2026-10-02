"""The stated FX facts, and the one relation the daemon can adjudicate (#1592).

The replay may not import the daemon, so the conversion is a REWRITE of
``broker_contract.sizing``'s one FX line rather than a call to it: that function
also floors to whole shares, which section 5 keeps as a recorded scope cut.
Calling the contract here, in a test, is an ORACLE at test time and not a
production import -- the same stance ``test_cost_gate`` takes toward the
daemon's fee card.
"""

from __future__ import annotations

import datetime as dt
import math
import random
import unittest

from broker_contract.fx import FxConversion
from broker_contract.sizing import SetupPlan
from intent_replay.fx import Fx

ACCOUNT, INSTRUMENT = "PLN", "USD"
RATE, BUFFER_PCT = 1.0 / 3.7, 1.0


def _cross(**overrides: object) -> Fx:
    fields: dict[str, object] = {
        "account_currency": ACCOUNT,
        "instrument_currency": INSTRUMENT,
        "rate": RATE,
        "round_trip_cost_rate": 0.005,
        "sizing_buffer_pct": BUFFER_PCT,
    }
    fields.update(overrides)
    return Fx(**fields)  # type: ignore[arg-type]


def _same(**overrides: object) -> Fx:
    fields: dict[str, object] = {
        "account_currency": ACCOUNT,
        "instrument_currency": ACCOUNT,
        "rate": None,
        "round_trip_cost_rate": None,
        "sizing_buffer_pct": None,
    }
    fields.update(overrides)
    return Fx(**fields)  # type: ignore[arg-type]


class AppliesIsDerivedFromTheTwoCodesTest(unittest.TestCase):
    """The flag a caller used to state, and could state wrongly (section 5.2.1)."""

    def test_two_different_codes_mean_a_conversion_applies(self) -> None:
        self.assertTrue(_cross().applies)

    def test_the_same_code_twice_means_it_does_not(self) -> None:
        self.assertFalse(_same().applies)


class TheSameCurrencyArmReturnsItsArgumentTest(unittest.TestCase):
    """Why every pre-#1592 number is reproduced exactly.

    The arm RETURNS the argument; it does not multiply by one. That is what
    makes a same-currency run the preimage every retired literal is pinned
    against, so the two tests below are the load-bearing ones in this module.
    """

    ADVERSARIAL = (1500.0, 0.1, 1e300, 5e-324, 2.0**53 + 1.0, math.pi)

    def test_the_sizing_notional_is_the_total_for_every_shape_of_float(self) -> None:
        for total in self.ADVERSARIAL:
            with self.subTest(total=total):
                self.assertEqual(_same().sizing_notional(total), total)

    def test_a_buffer_stated_anyway_does_not_reach_the_same_currency_arm(self) -> None:
        # The config refuses a stated buffer on a same-currency run, so this
        # cannot arrive from the wire. It is asserted here because a leak would
        # move every published number on the path that exists to be unmoved.
        leaking = _same(sizing_buffer_pct=25.0)
        self.assertEqual(leaking.sizing_notional(1500.0), 1500.0)

    def test_to_shares_is_the_identity_even_with_a_rate_stated(self) -> None:
        self.assertEqual(_same(rate=3.7).to_shares(13.5), 13.5)

    def test_an_account_amount_is_already_an_instrument_amount(self) -> None:
        self.assertEqual(_same(rate=3.7).in_instrument_currency(1500.0), 1500.0)


class TheCrossCurrencyArmTest(unittest.TestCase):
    def test_the_buffer_comes_off_the_total_before_anything_splits_it(self) -> None:
        # One per cent of 1500 is exact in binary, which is why the fixtures
        # downstream can state a derivation and still assert a literal.
        self.assertEqual(_cross().sizing_notional(1500.0), 1485.0)

    def test_the_rate_turns_an_account_quantity_into_shares(self) -> None:
        self.assertEqual(_cross(rate=0.5).to_shares(20.0), 10.0)

    def test_the_rate_also_prices_an_account_amount_in_the_instruments_currency(self) -> None:
        # The same multiplication under a second name, because the two have
        # different DIMENSIONS: one answers "how many shares", the other
        # "how much money in the other currency". A call site that said
        # ``to_shares`` for a cash figure would read as a share count.
        self.assertEqual(_cross(rate=0.5).in_instrument_currency(900.0), 450.0)

    def test_the_two_names_are_one_arithmetic(self) -> None:
        # Stated rather than left to a reader: if these ever diverge, one of the
        # two conversions has grown a rule the other lacks, and the result's
        # derived notional would stop agreeing with the gate's.
        fx = _cross(rate=1.08)
        for value in (0.0, 1.0, 1500.0, 1e300, 5e-324):
            with self.subTest(value):
                self.assertEqual(fx.to_shares(value), fx.in_instrument_currency(value))

    def test_the_pair_unit_spells_the_direction_rather_than_implying_it(self) -> None:
        # `USD/PLN` reads as PLN per USD under market convention -- the inverse
        # of what section 5.2.1 means -- so the token says the direction in
        # words. This string IS the direction check: config compares it.
        self.assertEqual(_cross().pair_unit(), "USD_per_PLN")


class TheDaemonAdjudicatesTheConversionTest(unittest.TestCase):
    """The relation the contract can settle, and the bound it settles it to.

    Bit-identity is NOT available. The daemon computes
    ``total x rate x (1 - b/100)`` in the instrument currency; the replay
    computes ``total x (1 - b/100)`` in the account currency and converts
    later, at the cost gate. Float multiplication is not associative, so the
    two reach the same quantity by different routes. The bound below is
    measured, not chosen.
    """

    DRAWS = 200_000
    SEED = 20261002
    TOTAL_RANGE = (1e2, 1e6)
    RATE_RANGE = (1e-3, 1e3)
    BUFFER_RANGE = (0.0, 99.9)
    ULP_BOUND = 2.0

    def _daemon_notional(self, total: float, rate: float, buffer_pct: float) -> float:
        conversion = FxConversion(
            account_currency=ACCOUNT,
            instrument_currency=INSTRUMENT,
            rate=rate,
            sizing_buffer_pct=buffer_pct,
            source="test-oracle",
            price_type=None,
            bid=None,
            ask=None,
            asof=dt.datetime(2026, 10, 2, tzinfo=dt.UTC),
        )
        plan = SetupPlan(
            total_notional=total,
            disaster_stop=1.0,
            order_ttl_days=1,
            entry_tiers=(),
            tp_tranches=(),
            fx=conversion,
        )
        return plan.sizing_notional

    def _gaps(self) -> list[float]:
        rnd = random.Random(self.SEED)
        gaps = []
        for _ in range(self.DRAWS):
            total = rnd.uniform(*self.TOTAL_RANGE)
            rate = rnd.uniform(*self.RATE_RANGE)
            buffer_pct = rnd.uniform(*self.BUFFER_RANGE)
            fx = _cross(rate=rate, sizing_buffer_pct=buffer_pct)
            mine = fx.sizing_notional(total) * rate
            theirs = self._daemon_notional(total, rate, buffer_pct)
            gaps.append(abs(mine - theirs) / math.ulp(theirs))
        return gaps

    def test_the_replay_reaches_the_daemons_notional_to_within_two_ulps(self) -> None:
        self.assertLessEqual(
            max(self._gaps()),
            self.ULP_BOUND,
            msg=f"{self.DRAWS} draws, seed {self.SEED}, total {self.TOTAL_RANGE}, "
            f"rate {self.RATE_RANGE}, buffer {self.BUFFER_RANGE}",
        )

    def test_the_bound_is_not_vacuous_because_the_routes_really_do_differ(self) -> None:
        # Without this the assertion above would pass on a stub returning the
        # daemon's own value, and would claim a tolerance nothing needs.
        gaps = self._gaps()
        self.assertGreater(len([gap for gap in gaps if gap > 0.0]) / len(gaps), 0.20)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
