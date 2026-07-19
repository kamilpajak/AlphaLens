"""Mutation-hardening tests for paper/sizing.py (cosmic-ray survivors).

Each test pins one operator/constant mutation the behavioural suite in
test_sizing.py / test_sizing_fx.py missed (440 mutants, 61 survived at 86.1%).
Docstrings name the mutation each test kills. See
docs/research/mutation_testing_targets_2026_07_18.md.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import unittest

from alphalens_pipeline.paper.constants import EXPECTED_AVG_HOLD_DAYS, STEADY_STATE_GROSS_FRAC
from alphalens_pipeline.paper.fx import FxConversion
from alphalens_pipeline.paper.sizing import (
    TierPlan,
    TpTranchePlan,
    TradeSetupNotPlannableError,
    compute_daily_scale_factor,
    compute_setup_plan,
    setup_plan_gross_guard_limit,
    validate_trade_setup,
)

_ASOF = dt.datetime(2026, 7, 18, 12, 0, tzinfo=dt.UTC)


def _setup(
    *,
    suggested_size_pct: float = 5.0,
    disaster_stop: float = 90.0,
    status: str = "OK",
    schema_version: str = "1.0.0",
    order_ttl_days=10,
    entry_tiers=None,
    tp_tranches=None,
) -> dict:
    if entry_tiers is None:
        entry_tiers = [{"limit": 100.0, "alloc_pct": 100.0, "tag": "t0"}]
    setup: dict = {
        "schema_version": schema_version,
        "status": status,
        "disaster_stop": disaster_stop,
        "suggested_size_pct": suggested_size_pct,
        "entry_tiers": entry_tiers,
    }
    if order_ttl_days is not None:
        setup["order_ttl_days"] = order_ttl_days
    if tp_tranches is not None:
        setup["tp_tranches"] = tp_tranches
    return setup


def _fx(
    *, rate: float = 4.34, buffer_pct: float = 1.0, account: str = "EUR", instrument: str = "PLN"
):
    return FxConversion(
        account_currency=account,
        instrument_currency=instrument,
        rate=rate,
        sizing_buffer_pct=buffer_pct,
        source="saxo-fxspot-uic-1343-mid",
        price_type="Tradable",
        bid=None,
        ask=None,
        asof=_ASOF,
    )


class TierPlanFrozenTests(unittest.TestCase):
    def test_tierplan_is_frozen(self):
        # Kills @dataclass(frozen=True)->frozen=False on TierPlan (L51).
        with self.assertRaises(dataclasses.FrozenInstanceError):
            TierPlan(0, 100.0, 5, 50.0, "t").qty = 9

    def test_tptrancheplan_is_frozen(self):
        # Kills @dataclass(frozen=True)->frozen=False on TpTranchePlan (L62).
        with self.assertRaises(dataclasses.FrozenInstanceError):
            TpTranchePlan(0, 110.0, 50.0, 1.0, "t").target_price = 9.0


class ValidateTradeSetupHardeningTests(unittest.TestCase):
    def test_status_lexically_after_ok_is_rejected(self):
        # `status != "OK"` must reject a status that sorts AFTER "OK"; a
        # `!=`->`<` mutant reads "REJECTED" < "OK" as False and admits it.
        with self.assertRaises(TradeSetupNotPlannableError):
            validate_trade_setup(_setup(status="REJECTED"))

    def test_status_matched_by_value_not_identity(self):
        # A non-interned "OK" is == but not `is` the source literal; a
        # `!=`->`is not` mutant would reject a perfectly valid setup (L137).
        ok = "".join(["O", "K"])
        self.assertEqual(validate_trade_setup(_setup(status=ok)), 5.0)

    def test_negative_suggested_size_is_rejected(self):
        # `suggested_size_pct <= 0`: a negative value must be rejected; an
        # `<=`->`==` mutant only rejects exactly 0 (L150).
        with self.assertRaises(TradeSetupNotPlannableError):
            validate_trade_setup(_setup(suggested_size_pct=-5.0))

    def test_fractional_disaster_stop_is_accepted(self):
        # `disaster_stop <= 0`: a small positive stop (0.5) is valid; a
        # NumberReplacer `<= 1` mutant would reject it (L154).
        self.assertEqual(validate_trade_setup(_setup(disaster_stop=0.5)), 5.0)

    def test_zero_disaster_stop_is_rejected(self):
        # A zero stop must be rejected; `<= -1` / `< 0` mutants admit it (L154).
        with self.assertRaises(TradeSetupNotPlannableError):
            validate_trade_setup(_setup(disaster_stop=0.0))

    def test_negative_disaster_stop_is_rejected(self):
        # A negative stop must be rejected; an `<=`->`==` mutant admits it (L154).
        with self.assertRaises(TradeSetupNotPlannableError):
            validate_trade_setup(_setup(disaster_stop=-5.0))

    def test_tier_missing_limit_key_is_unusable(self):
        # A tier with no "limit" key defaults to 0 and is dropped; a
        # NumberReplacer on the `.get("limit", 0)` default (->1) would count
        # it as usable and let an all-unusable setup pass (L170).
        with self.assertRaises(TradeSetupNotPlannableError):
            validate_trade_setup(_setup(entry_tiers=[{"alloc_pct": 100.0}]))

    def test_tier_zero_limit_is_unusable(self):
        # A tier with limit == 0 is dropped; a NumberReplacer on the `or 0`
        # fallback (->1) would count it as usable (L170).
        with self.assertRaises(TradeSetupNotPlannableError):
            validate_trade_setup(_setup(entry_tiers=[{"limit": 0.0, "alloc_pct": 100.0}]))


class ComputeDailyScaleFactorHardeningTests(unittest.TestCase):
    def test_extra_positional_arg_is_rejected(self):
        # steady_state_gross_frac is keyword-only (the `*` marker); a `*`->`/`
        # mutant would accept it positionally (L181).
        with self.assertRaises(TypeError):
            compute_daily_scale_factor([1.0], 1000.0, 0.5)  # type: ignore[misc]

    def test_nonpositive_equity_short_circuits_to_one(self):
        # `paper_equity <= 0` returns 1.0 BEFORE any arithmetic — a real guard
        # against dividing by a non-positive budget. With equity=-0.5 the
        # `<= 0` guard must fire; `==0`, `<= -1`, and `or`->`and` mutants each
        # fall through and compute a (negative) factor instead (L207).
        self.assertEqual(compute_daily_scale_factor([-50.0], -0.5), 1.0)

    def test_small_positive_equity_still_scales_below_one(self):
        # A tiny but positive equity must NOT short-circuit; a NumberReplacer
        # `<= 1` mutant would return 1.0 for equity in (0, 1] (L207).
        self.assertLess(compute_daily_scale_factor([50.0], 0.5), 1.0)

    def test_zero_aggregate_returns_one_avoiding_division(self):
        # A zero aggregate (all-zero pcts) returns 1.0, guarding the
        # `daily_target / aggregate` division. `<`/`<= -1` mutants fall through
        # to a ZeroDivisionError; the returned-constant 1.0->2.0/0.0 mutants
        # change the value (L210/L211).
        self.assertEqual(compute_daily_scale_factor([0.0], 1000.0), 1.0)

    def test_negative_aggregate_returns_one(self):
        # A negative aggregate (with positive equity) also short-circuits to
        # 1.0; an `<= 0`->`== 0` mutant would fall through to a negative factor
        # (L210).
        self.assertEqual(compute_daily_scale_factor([-50.0], 1000.0), 1.0)

    def test_small_aggregate_still_scales(self):
        # An aggregate in (0, 1] must still be scaled, not short-circuited; a
        # NumberReplacer `<= 1` mutant would return 1.0 (L210).
        self.assertLess(compute_daily_scale_factor([50.0], 1.0), 1.0)

    def test_scale_factor_matches_formula(self):
        # Pin min(1.0, daily_target / aggregate) exactly.
        pcts, equity = [50.0, 30.0], 100_000.0
        aggregate = sum(s / 100.0 * equity for s in pcts)
        daily_target = STEADY_STATE_GROSS_FRAC * equity / EXPECTED_AVG_HOLD_DAYS
        expected = min(1.0, daily_target / aggregate)
        self.assertAlmostEqual(compute_daily_scale_factor(pcts, equity), expected, places=12)


class ComputeSetupPlanHardeningTests(unittest.TestCase):
    def _plan(self, **kw):
        return compute_setup_plan(
            brief_trade_setup=_setup(**kw), paper_equity=100_000.0, scale_factor=1.0
        )

    def test_fractional_limit_tier_is_kept(self):
        # A sub-dollar limit (0.5) is a valid tier; an `<= 0`->`<= 1` mutant
        # would drop it and raise "no usable entry tiers" (L277).
        plan = self._plan(entry_tiers=[{"limit": 0.5, "alloc_pct": 100.0}])
        self.assertEqual(len(plan.entry_tiers), 1)

    def test_negative_limit_tier_is_skipped_but_loop_continues(self):
        # A negative-limit tier is skipped (continue), and the following good
        # tier is still processed. Kills `<= 0`->`== 0` (mutant keeps the -5
        # tier -> 2 entries) and continue->break (mutant stops -> 0 entries ->
        # raises) (L277/L280).
        plan = self._plan(
            entry_tiers=[{"limit": -5.0, "alloc_pct": 50.0}, {"limit": 10.0, "alloc_pct": 50.0}]
        )
        self.assertEqual(len(plan.entry_tiers), 1)
        self.assertEqual(plan.entry_tiers[0].limit_price, 10.0)

    def test_missing_alloc_pct_defaults_to_zero(self):
        # A tier with no alloc_pct sizes to 0; a NumberReplacer on the default
        # (->1.0/-1.0) would allocate it a non-zero share (L281).
        plan = self._plan(entry_tiers=[{"limit": 100.0}])
        self.assertEqual(plan.entry_tiers[0].alloc_pct, 0.0)
        self.assertEqual(plan.entry_tiers[0].qty, 0)

    def test_negative_alloc_qty_floored_at_zero(self):
        # A negative alloc_pct yields a negative notional; qty must clamp at 0,
        # not go negative. An `max(0, ...)`->`max(-1, ...)` mutant returns -1
        # (L283).
        plan = self._plan(entry_tiers=[{"limit": 100.0, "alloc_pct": -100.0}])
        self.assertEqual(plan.entry_tiers[0].qty, 0)

    def test_order_ttl_missing_defaults_to_zero(self):
        # A missing/falsy order_ttl_days becomes the 0 sentinel; `or -1`/`or 1`
        # mutants change the sentinel (L313).
        plan = self._plan(order_ttl_days=None)
        self.assertEqual(plan.order_ttl_days, 0)

    def test_order_ttl_present_is_preserved(self):
        # A real order_ttl_days must pass through; an `or`->`and` mutant zeroes
        # it (5 and 0 == 0) (L313).
        plan = self._plan(order_ttl_days=7)
        self.assertEqual(plan.order_ttl_days, 7)


class TpTrancheHardeningTests(unittest.TestCase):
    def _plan(self, tp_tranches):
        return compute_setup_plan(
            brief_trade_setup=_setup(tp_tranches=tp_tranches),
            paper_equity=100_000.0,
            scale_factor=1.0,
        )

    def test_fractional_target_tranche_is_kept(self):
        # A sub-dollar target is valid; an `<= 0`->`<= 1` mutant drops it (L300).
        plan = self._plan([{"target": 0.5, "tranche_pct": 100.0}])
        self.assertEqual(len(plan.tp_tranches), 1)

    def test_zero_target_tranche_is_skipped_but_loop_continues(self):
        # A zero-target tranche is skipped (continue) and the following good
        # tranche is still recorded. Kills `<= 0`->`< 0`/`<= -1` (mutant keeps
        # the 0 target) and continue->break (L300/L301).
        plan = self._plan(
            [{"target": 0.0, "tranche_pct": 50.0}, {"target": 110.0, "tranche_pct": 50.0}]
        )
        self.assertEqual(len(plan.tp_tranches), 1)
        self.assertEqual(plan.tp_tranches[0].target_price, 110.0)

    def test_negative_target_tranche_is_excluded(self):
        # A negative target must be dropped; an `<= 0`->`== 0` mutant keeps it (L300).
        plan = self._plan(
            [{"target": -5.0, "tranche_pct": 50.0}, {"target": 110.0, "tranche_pct": 50.0}]
        )
        self.assertEqual(len(plan.tp_tranches), 1)

    def test_missing_tranche_defaults_are_zero(self):
        # A tranche with no tranche_pct / r_multiple defaults both to 0.0;
        # NumberReplacers on those defaults (->1.0/-1.0) change them (L306/L307).
        plan = self._plan([{"target": 110.0}])
        self.assertEqual(plan.tp_tranches[0].tranche_pct, 0.0)
        self.assertEqual(plan.tp_tranches[0].r_multiple, 0.0)


class FxGuardHardeningTests(unittest.TestCase):
    def _plan(self, fx):
        return compute_setup_plan(
            brief_trade_setup=_setup(entry_tiers=[{"limit": 100.0, "alloc_pct": 100.0}]),
            paper_equity=10_000.0,
            scale_factor=1.0,
            fx=fx,
        )

    def test_cross_currency_account_after_instrument_is_accepted(self):
        # The same-currency guard is `==`, not `>=`: a valid cross-currency
        # pair whose account ccy sorts AFTER the instrument ccy ("PLN" > "EUR")
        # must be accepted; a `==`->`>=` mutant rejects it (L249).
        plan = self._plan(_fx(account="PLN", instrument="EUR", rate=0.23))
        self.assertGreaterEqual(len(plan.entry_tiers), 1)

    def test_same_currency_matched_by_value_not_identity(self):
        # Same-currency FxConversion must be rejected even when the two ccy
        # strings are equal-value but distinct objects; a `==`->`is` mutant
        # would admit it (L249).
        same = "".join(["US", "D"])
        with self.assertRaises(TradeSetupNotPlannableError):
            self._plan(_fx(account="USD", instrument=same, rate=1.1))

    def test_rate_below_one_is_accepted(self):
        # A valid rate < 1 (e.g. PLN->EUR ~0.23) must be accepted; a
        # NumberReplacer `<= 1` mutant rejects it (L254).
        plan = self._plan(_fx(account="PLN", instrument="EUR", rate=0.9))
        self.assertGreaterEqual(len(plan.entry_tiers), 1)

    def test_negative_rate_is_rejected(self):
        # A non-positive rate must be rejected; an `<=`->`==` mutant admits a
        # negative rate (L254).
        with self.assertRaises(TradeSetupNotPlannableError):
            self._plan(_fx(rate=-1.0))

    def test_fx_sizing_notional_drives_qty_exactly(self):
        # Pin the one FX line: sizing_notional = total_notional * rate *
        # (1 - buffer/100), floored per tier. A big buffer (50%) and a $1 limit
        # make the floor cross an integer boundary, so a NumberReplacer on the
        # `/ 100.0` divisor (L272) shifts the qty (a small buffer floors away).
        plan = compute_setup_plan(
            brief_trade_setup=_setup(entry_tiers=[{"limit": 1.0, "alloc_pct": 100.0}]),
            paper_equity=10_000.0,
            scale_factor=1.0,
            fx=_fx(rate=2.0, buffer_pct=50.0),
        )
        total_notional = 5.0 / 100.0 * 10_000.0  # final_size_pct=5, equity=10k -> 500
        sizing_notional = total_notional * 2.0 * (1.0 - 50.0 / 100.0)  # 500
        expected_qty = int(sizing_notional // 1.0)  # alloc 100%, limit 1.0 -> 500
        self.assertEqual(plan.entry_tiers[0].qty, expected_qty)


class GrossGuardHardeningTests(unittest.TestCase):
    def test_gross_guard_frac_is_keyword_only(self):
        # gross_safety_frac is keyword-only (the `*` marker); a `*`->`/` mutant
        # would accept it positionally (L341).
        plan = compute_setup_plan(
            brief_trade_setup=_setup(), paper_equity=10_000.0, scale_factor=1.0
        )
        with self.assertRaises(TypeError):
            setup_plan_gross_guard_limit(plan, 0.5)  # type: ignore[misc]


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
