"""LIVE boot-assert (design memo §3 / ADR 0017 point 4) — the nine safety-rail
env vars a ``env=live`` instance must set explicitly, within bounds, before
it may boot.

The code defaults (``safety.py``: ``DEFAULT_MAX_OPEN=3``,
``DEFAULT_PORTFOLIO_GROSS_FRAC=1.0``, ``DEFAULT_DAILY_LOSS_LIMIT_R=3.0``) are
permissive — a LIVE unit missing one pin would silently trade 100% gross of
the real balance. ``assert_live_rails`` collects EVERY violation and reports
them together so an operator fixes the unit file once, not one restart per
missing pin.

Fully hermetic: every test scopes ``os.environ`` via ``mock.patch.dict`` and
none sets a plausible real account/credential value.
"""

from __future__ import annotations

import unittest
from unittest import mock

from alphalens_pipeline.brokers.automanager.entry_trails import ENTRY_TRAIL_BPS_MAX
from alphalens_pipeline.brokers.automanager.live_rails import (
    DAILY_LOSS_LIMIT_R_ENV,
    ENTRY_TRAIL_BPS_ENV,
    ENTRY_WATCH_MAX_PICKS_ENV,
    EXIT_POLICY_ENV,
    MAX_FEE_BPS_ENV,
    MAX_OPEN_ENV,
    PORTFOLIO_GROSS_FRAC_ENV,
    SIZING_EQUITY_ENV,
    SIZING_EQUITY_MODE_ENV,
    SIZING_MODE_CLAMPED,
    SIZING_MODE_DECLARED,
    assert_live_rails,
)
from broker_contract.contract import BrokerCapabilityError

# A fully in-bounds env — every test starts from this and mutates ONE var so
# failures are attributable to the var under test, never a sibling omission.
_VALID_ENV: dict[str, str] = {
    MAX_OPEN_ENV: "1",
    PORTFOLIO_GROSS_FRAC_ENV: "0.25",
    DAILY_LOSS_LIMIT_R_ENV: "1.0",
    SIZING_EQUITY_ENV: "10000",
    SIZING_EQUITY_MODE_ENV: "clamped",
    EXIT_POLICY_ENV: "trailing_atr",
    MAX_FEE_BPS_ENV: "100",
    ENTRY_TRAIL_BPS_ENV: "0",
    ENTRY_WATCH_MAX_PICKS_ENV: "2",
}

_ALL_RAIL_VARS = (
    MAX_OPEN_ENV,
    PORTFOLIO_GROSS_FRAC_ENV,
    DAILY_LOSS_LIMIT_R_ENV,
    SIZING_EQUITY_ENV,
    SIZING_EQUITY_MODE_ENV,
    EXIT_POLICY_ENV,
    MAX_FEE_BPS_ENV,
    ENTRY_TRAIL_BPS_ENV,
    ENTRY_WATCH_MAX_PICKS_ENV,
)


def _env_without(*names: str) -> dict[str, str]:
    return {k: v for k, v in _VALID_ENV.items() if k not in names}


class TestAllNineConstantsAreDistinctNames(unittest.TestCase):
    def test_env_var_names(self):
        self.assertEqual(MAX_OPEN_ENV, "ALPHALENS_BROKER_MAX_OPEN")
        self.assertEqual(PORTFOLIO_GROSS_FRAC_ENV, "ALPHALENS_BROKER_PORTFOLIO_GROSS_FRAC")
        self.assertEqual(DAILY_LOSS_LIMIT_R_ENV, "ALPHALENS_BROKER_DAILY_LOSS_LIMIT_R")
        self.assertEqual(SIZING_EQUITY_ENV, "ALPHALENS_BROKER_SIZING_EQUITY")
        self.assertEqual(SIZING_EQUITY_MODE_ENV, "ALPHALENS_BROKER_SIZING_EQUITY_MODE")
        self.assertEqual(EXIT_POLICY_ENV, "ALPHALENS_BROKER_EXIT_POLICY")
        self.assertEqual(MAX_FEE_BPS_ENV, "ALPHALENS_BROKER_MAX_FEE_BPS")
        self.assertEqual(ENTRY_TRAIL_BPS_ENV, "ALPHALENS_BROKER_ENTRY_TRAIL_BPS")
        self.assertEqual(ENTRY_WATCH_MAX_PICKS_ENV, "ALPHALENS_BROKER_ENTRY_WATCH_MAX_PICKS")
        self.assertEqual(len(set(_ALL_RAIL_VARS)), 9, "all nine env-var names must be distinct")


class TestValidEnvPasses(unittest.TestCase):
    def test_all_nine_set_in_bounds_passes(self):
        with mock.patch.dict("os.environ", _VALID_ENV, clear=True):
            assert_live_rails()  # must not raise

    def test_both_sizing_modes_pass(self):
        for mode in (SIZING_MODE_CLAMPED, SIZING_MODE_DECLARED):
            with self.subTest(sizing_mode=mode):
                env = dict(_VALID_ENV, **{SIZING_EQUITY_MODE_ENV: mode})
                with mock.patch.dict("os.environ", env, clear=True):
                    assert_live_rails()  # must not raise

    def test_bound_edges_pass(self):
        """The bounds are inclusive at the documented edges — EVERY bounded
        rail, so the name does not promise more coverage than it delivers."""
        edge_env = dict(_VALID_ENV)
        edge_env[MAX_OPEN_ENV] = "2"
        edge_env[PORTFOLIO_GROSS_FRAC_ENV] = "0.5"
        edge_env[DAILY_LOSS_LIMIT_R_ENV] = "2.0"
        edge_env[SIZING_EQUITY_ENV] = "15000"
        edge_env[MAX_FEE_BPS_ENV] = "1000"
        edge_env[ENTRY_WATCH_MAX_PICKS_ENV] = "2"
        with mock.patch.dict("os.environ", edge_env, clear=True):
            assert_live_rails()  # must not raise


class TestEachVarUnsetIsNamedInTheError(unittest.TestCase):
    def test_max_open_unset(self):
        with mock.patch.dict("os.environ", _env_without(MAX_OPEN_ENV), clear=True):
            with self.assertRaises(BrokerCapabilityError) as captured:
                assert_live_rails()
        self.assertIn(MAX_OPEN_ENV, str(captured.exception))

    def test_portfolio_gross_frac_unset(self):
        with mock.patch.dict("os.environ", _env_without(PORTFOLIO_GROSS_FRAC_ENV), clear=True):
            with self.assertRaises(BrokerCapabilityError) as captured:
                assert_live_rails()
        self.assertIn(PORTFOLIO_GROSS_FRAC_ENV, str(captured.exception))

    def test_daily_loss_limit_r_unset(self):
        with mock.patch.dict("os.environ", _env_without(DAILY_LOSS_LIMIT_R_ENV), clear=True):
            with self.assertRaises(BrokerCapabilityError) as captured:
                assert_live_rails()
        self.assertIn(DAILY_LOSS_LIMIT_R_ENV, str(captured.exception))

    def test_sizing_equity_unset(self):
        with mock.patch.dict("os.environ", _env_without(SIZING_EQUITY_ENV), clear=True):
            with self.assertRaises(BrokerCapabilityError) as captured:
                assert_live_rails()
        self.assertIn(SIZING_EQUITY_ENV, str(captured.exception))

    def test_sizing_equity_mode_unset(self):
        with mock.patch.dict("os.environ", _env_without(SIZING_EQUITY_MODE_ENV), clear=True):
            with self.assertRaises(BrokerCapabilityError) as captured:
                assert_live_rails()
        self.assertIn(SIZING_EQUITY_MODE_ENV, str(captured.exception))

    def test_exit_policy_unset(self):
        with mock.patch.dict("os.environ", _env_without(EXIT_POLICY_ENV), clear=True):
            with self.assertRaises(BrokerCapabilityError) as captured:
                assert_live_rails()
        self.assertIn(EXIT_POLICY_ENV, str(captured.exception))

    def test_entry_watch_max_picks_unset(self):
        with mock.patch.dict("os.environ", _env_without(ENTRY_WATCH_MAX_PICKS_ENV), clear=True):
            with self.assertRaises(BrokerCapabilityError) as captured:
                assert_live_rails()
        self.assertIn(ENTRY_WATCH_MAX_PICKS_ENV, str(captured.exception))

    def test_max_fee_bps_unset(self):
        with mock.patch.dict("os.environ", _env_without(MAX_FEE_BPS_ENV), clear=True):
            with self.assertRaises(BrokerCapabilityError) as captured:
                assert_live_rails()
        self.assertIn(MAX_FEE_BPS_ENV, str(captured.exception))

    def test_entry_trail_bps_unset(self):
        # Unlike the lenient runtime reader (entry_trails.entry_trail_bps,
        # unset -> feature OFF), the LIVE boot-assert requires an EXPLICIT
        # value — consistent with the seven existing pins.
        with mock.patch.dict("os.environ", _env_without(ENTRY_TRAIL_BPS_ENV), clear=True):
            with self.assertRaises(BrokerCapabilityError) as captured:
                assert_live_rails()
        self.assertIn(ENTRY_TRAIL_BPS_ENV, str(captured.exception))

    def test_entry_trail_bps_unset_message_names_explicit_zero(self):
        # The generic unset wording ("the code default is permissive") is
        # WRONG for this pin — the unset code default is OFF (safe) — and
        # could nudge an operator toward a nonzero value for the wrong
        # reason. The violation must say explicit 0 = trailing off.
        with mock.patch.dict("os.environ", _env_without(ENTRY_TRAIL_BPS_ENV), clear=True):
            with self.assertRaises(BrokerCapabilityError) as captured:
                assert_live_rails()
        violation = next(
            line for line in str(captured.exception).splitlines() if ENTRY_TRAIL_BPS_ENV in line
        )
        self.assertIn("explicit 0 = trailing off", violation)
        self.assertNotIn("permissive", violation)

    def test_entry_trail_bps_blank(self):
        env = dict(_VALID_ENV, **{ENTRY_TRAIL_BPS_ENV: "  "})
        with mock.patch.dict("os.environ", env, clear=True):
            with self.assertRaises(BrokerCapabilityError) as captured:
                assert_live_rails()
        self.assertIn(ENTRY_TRAIL_BPS_ENV, str(captured.exception))


class TestOutOfBoundsIsNamedInTheError(unittest.TestCase):
    def test_max_open_zero_rejected(self):
        env = dict(_VALID_ENV, **{MAX_OPEN_ENV: "0"})
        with mock.patch.dict("os.environ", env, clear=True):
            with self.assertRaises(BrokerCapabilityError) as captured:
                assert_live_rails()
        self.assertIn(MAX_OPEN_ENV, str(captured.exception))

    def test_max_open_above_cap_rejected(self):
        # Ceiling widened 2 -> 4 on 2026-09-04 (operator decision): the
        # WhatsApp flow arms 2+ manual picks a day on top of the two already
        # held, and MAX_OPEN=2 forced a disarm-to-arm trade every morning.
        # 5 is the first value above the new cap.
        env = dict(_VALID_ENV, **{MAX_OPEN_ENV: "5"})
        with mock.patch.dict("os.environ", env, clear=True):
            with self.assertRaises(BrokerCapabilityError) as captured:
                assert_live_rails()
        self.assertIn(MAX_OPEN_ENV, str(captured.exception))

    def test_max_open_at_the_widened_cap_passes(self):
        # 4 is the value the LIVE drop-in (20-exposure.conf) runs since
        # 2026-09-04; the cap is inclusive, so widening it must admit the
        # deployed value, and 3 sits inside the new range too.
        for raw in ("3", "4"):
            with self.subTest(raw=raw):
                env = dict(_VALID_ENV, **{MAX_OPEN_ENV: raw})
                with mock.patch.dict("os.environ", env, clear=True):
                    assert_live_rails()

    def test_entry_watch_max_picks_above_four_rejected(self):
        """#1189: the SIM soak runs this rail at the shared code ceiling, so the
        ceiling alone can no longer be what protects LIVE — the LIVE bound has
        to be its own assert. 10 was in bounds before this pin existed. The
        bound moved 2 -> 4 with MAX_OPEN on 2026-09-04 (one watch slot per
        position slot); 5 is the first value above it."""
        for raw in ("5", "10", "25"):
            with self.subTest(raw=raw):
                env = dict(_VALID_ENV, **{ENTRY_WATCH_MAX_PICKS_ENV: raw})
                with mock.patch.dict("os.environ", env, clear=True):
                    with self.assertRaises(BrokerCapabilityError) as captured:
                        assert_live_rails()
                self.assertIn(ENTRY_WATCH_MAX_PICKS_ENV, str(captured.exception))

    def test_entry_watch_max_picks_at_the_widened_cap_passes(self):
        # 4 is what 40-entry-trail.conf runs since 2026-09-04 (inclusive cap).
        for raw in ("3", "4"):
            with self.subTest(raw=raw):
                env = dict(_VALID_ENV, **{ENTRY_WATCH_MAX_PICKS_ENV: raw})
                with mock.patch.dict("os.environ", env, clear=True):
                    assert_live_rails()

    def test_entry_watch_max_picks_below_one_rejected(self):
        env = dict(_VALID_ENV, **{ENTRY_WATCH_MAX_PICKS_ENV: "0"})
        with mock.patch.dict("os.environ", env, clear=True):
            with self.assertRaises(BrokerCapabilityError) as captured:
                assert_live_rails()
        self.assertIn(ENTRY_WATCH_MAX_PICKS_ENV, str(captured.exception))

    def test_entry_watch_max_picks_non_integer_rejected(self):
        env = dict(_VALID_ENV, **{ENTRY_WATCH_MAX_PICKS_ENV: "many"})
        with mock.patch.dict("os.environ", env, clear=True):
            with self.assertRaises(BrokerCapabilityError) as captured:
                assert_live_rails()
        self.assertIn(ENTRY_WATCH_MAX_PICKS_ENV, str(captured.exception))

    def test_max_open_non_integer_rejected(self):
        env = dict(_VALID_ENV, **{MAX_OPEN_ENV: "one"})
        with mock.patch.dict("os.environ", env, clear=True):
            with self.assertRaises(BrokerCapabilityError) as captured:
                assert_live_rails()
        self.assertIn(MAX_OPEN_ENV, str(captured.exception))

    def test_portfolio_gross_frac_zero_rejected(self):
        """The bound is exclusive at zero — a 0 gross cap can never place."""
        env = dict(_VALID_ENV, **{PORTFOLIO_GROSS_FRAC_ENV: "0"})
        with mock.patch.dict("os.environ", env, clear=True):
            with self.assertRaises(BrokerCapabilityError) as captured:
                assert_live_rails()
        self.assertIn(PORTFOLIO_GROSS_FRAC_ENV, str(captured.exception))

    def test_portfolio_gross_frac_above_cap_rejected(self):
        env = dict(_VALID_ENV, **{PORTFOLIO_GROSS_FRAC_ENV: "1.0"})
        with mock.patch.dict("os.environ", env, clear=True):
            with self.assertRaises(BrokerCapabilityError) as captured:
                assert_live_rails()
        self.assertIn(PORTFOLIO_GROSS_FRAC_ENV, str(captured.exception))

    def test_daily_loss_limit_r_zero_rejected(self):
        env = dict(_VALID_ENV, **{DAILY_LOSS_LIMIT_R_ENV: "0"})
        with mock.patch.dict("os.environ", env, clear=True):
            with self.assertRaises(BrokerCapabilityError) as captured:
                assert_live_rails()
        self.assertIn(DAILY_LOSS_LIMIT_R_ENV, str(captured.exception))

    def test_daily_loss_limit_r_above_cap_rejected(self):
        env = dict(_VALID_ENV, **{DAILY_LOSS_LIMIT_R_ENV: "3.0"})
        with mock.patch.dict("os.environ", env, clear=True):
            with self.assertRaises(BrokerCapabilityError) as captured:
                assert_live_rails()
        self.assertIn(DAILY_LOSS_LIMIT_R_ENV, str(captured.exception))

    def test_sizing_equity_zero_rejected(self):
        env = dict(_VALID_ENV, **{SIZING_EQUITY_ENV: "0"})
        with mock.patch.dict("os.environ", env, clear=True):
            with self.assertRaises(BrokerCapabilityError) as captured:
                assert_live_rails()
        self.assertIn(SIZING_EQUITY_ENV, str(captured.exception))

    def test_sizing_equity_negative_rejected(self):
        env = dict(_VALID_ENV, **{SIZING_EQUITY_ENV: "-1000"})
        with mock.patch.dict("os.environ", env, clear=True):
            with self.assertRaises(BrokerCapabilityError) as captured:
                assert_live_rails()
        self.assertIn(SIZING_EQUITY_ENV, str(captured.exception))

    def test_max_fee_bps_zero_rejected(self):
        env = dict(_VALID_ENV, **{MAX_FEE_BPS_ENV: "0"})
        with mock.patch.dict("os.environ", env, clear=True):
            with self.assertRaises(BrokerCapabilityError) as captured:
                assert_live_rails()
        self.assertIn(MAX_FEE_BPS_ENV, str(captured.exception))

    def test_max_fee_bps_negative_rejected(self):
        env = dict(_VALID_ENV, **{MAX_FEE_BPS_ENV: "-50"})
        with mock.patch.dict("os.environ", env, clear=True):
            with self.assertRaises(BrokerCapabilityError) as captured:
                assert_live_rails()
        self.assertIn(MAX_FEE_BPS_ENV, str(captured.exception))

    def test_sizing_equity_above_cap_rejected(self):
        # The declared frame is the DIRECT multiplier on position size, and it
        # was the one rail the assert checked only for positivity: an operator
        # typo of 150000 for 15000 booted clean and traded ten times the
        # intended size (issue #1121). The cap is the value production already
        # runs, so this is inert today and any future widening is a code change
        # that leaves a trace — the regime _MAX_OPEN_UPPER has always had.
        env = dict(_VALID_ENV, **{SIZING_EQUITY_ENV: "150000"})
        with mock.patch.dict("os.environ", env, clear=True):
            with self.assertRaises(BrokerCapabilityError) as captured:
                assert_live_rails()
        self.assertIn(SIZING_EQUITY_ENV, str(captured.exception))

    def test_sizing_equity_at_the_deployed_frame_passes(self):
        # 15000 is what the LIVE unit runs (declared frame, 1% = 150). The cap
        # is inclusive, so bounding the rail must not refuse production.
        env = dict(_VALID_ENV, **{SIZING_EQUITY_ENV: "15000"})
        with mock.patch.dict("os.environ", env, clear=True):
            assert_live_rails()

    def test_max_fee_bps_above_cap_rejected(self):
        # Same hole, opposite direction: a looser fee floor admits trades the
        # cost gate should refuse. Production widened 100 -> 1000 after the
        # NVAX refusal at 1037 bps; past that, nothing bounded it at all.
        env = dict(_VALID_ENV, **{MAX_FEE_BPS_ENV: "5000"})
        with mock.patch.dict("os.environ", env, clear=True):
            with self.assertRaises(BrokerCapabilityError) as captured:
                assert_live_rails()
        self.assertIn(MAX_FEE_BPS_ENV, str(captured.exception))

    def test_max_fee_bps_at_the_deployed_floor_passes(self):
        env = dict(_VALID_ENV, **{MAX_FEE_BPS_ENV: "1000"})
        with mock.patch.dict("os.environ", env, clear=True):
            assert_live_rails()

    def test_non_finite_frame_and_fee_floor_are_still_refused(self):
        # The deleted _check_float_positive carried an explicit math.isfinite
        # guard, because `inf > 0` is True and every comparison against `nan` is
        # False. Moving these two rails onto the bounded checker must not lose
        # it: `inf` fails `<= hi`, and `nan` fails the whole chain. Pinned here
        # rather than assumed — this passed before the move and must after.
        for var in (SIZING_EQUITY_ENV, MAX_FEE_BPS_ENV):
            for raw in ("inf", "-inf", "nan"):
                with self.subTest(var=var, value=raw):
                    env = dict(_VALID_ENV, **{var: raw})
                    with mock.patch.dict("os.environ", env, clear=True):
                        with self.assertRaises(BrokerCapabilityError) as captured:
                            assert_live_rails()
                    self.assertIn(var, str(captured.exception))

    def test_unknown_sizing_mode_rejected_and_error_names_valid_values(self):
        env = dict(_VALID_ENV, **{SIZING_EQUITY_MODE_ENV: "snapshot"})
        with mock.patch.dict("os.environ", env, clear=True):
            with self.assertRaises(BrokerCapabilityError) as captured:
                assert_live_rails()
        message = str(captured.exception)
        self.assertIn(SIZING_EQUITY_MODE_ENV, message)
        self.assertIn(SIZING_MODE_CLAMPED, message)
        self.assertIn(SIZING_MODE_DECLARED, message)

    def test_entry_trail_bps_zero_and_bound_edge_pass(self):
        # "0" (feature OFF) and the memo §6 upper bound are both valid pins.
        for value in ("0", str(ENTRY_TRAIL_BPS_MAX)):
            with self.subTest(value=value):
                env = dict(_VALID_ENV, **{ENTRY_TRAIL_BPS_ENV: value})
                with mock.patch.dict("os.environ", env, clear=True):
                    assert_live_rails()  # must not raise

    def test_entry_trail_bps_out_of_bounds_or_malformed_rejected(self):
        # 151 is the first value past the memo §6 bound (150, NOT 300 — the
        # replay's edge is dead by d≈2-3%).
        for value in (str(ENTRY_TRAIL_BPS_MAX + 1), "-1", "abc"):
            with self.subTest(value=value):
                env = dict(_VALID_ENV, **{ENTRY_TRAIL_BPS_ENV: value})
                with mock.patch.dict("os.environ", env, clear=True):
                    with self.assertRaises(BrokerCapabilityError) as captured:
                        assert_live_rails()
                self.assertIn(ENTRY_TRAIL_BPS_ENV, str(captured.exception))

    def test_non_finite_positive_floats_rejected(self):
        # float("inf") > 0 and float("nan") <= 0 is False — a bare `<= 0`
        # check would let both BOOT a live daemon with an unbounded sizing
        # frame / fee floor. The rails must require finite values.
        for bad in ("inf", "nan", "-inf"):
            for var in (SIZING_EQUITY_ENV, MAX_FEE_BPS_ENV):
                with self.subTest(var=var, value=bad):
                    env = dict(_VALID_ENV, **{var: bad})
                    with mock.patch.dict("os.environ", env, clear=True):
                        with self.assertRaises(BrokerCapabilityError) as captured:
                            assert_live_rails()
                    self.assertIn(var, str(captured.exception))


class TestUnknownExitPolicyFailsAtBoot(unittest.TestCase):
    def test_unknown_exit_policy_rejected(self):
        env = dict(_VALID_ENV, **{EXIT_POLICY_ENV: "not_a_real_policy"})
        with mock.patch.dict("os.environ", env, clear=True):
            with self.assertRaises(BrokerCapabilityError) as captured:
                assert_live_rails()
        self.assertIn(EXIT_POLICY_ENV, str(captured.exception))

    def test_blank_exit_policy_rejected_as_unset(self):
        """An empty string must fail the explicit-set check, never silently
        fall back to the position-manager default (setup_static)."""
        env = dict(_VALID_ENV, **{EXIT_POLICY_ENV: "   "})
        with mock.patch.dict("os.environ", env, clear=True):
            with self.assertRaises(BrokerCapabilityError) as captured:
                assert_live_rails()
        self.assertIn(EXIT_POLICY_ENV, str(captured.exception))

    def test_known_policies_all_pass(self):
        for name in ("setup_static", "atr_bracket_1p5", "trailing_atr"):
            with self.subTest(exit_policy=name):
                env = dict(_VALID_ENV, **{EXIT_POLICY_ENV: name})
                with mock.patch.dict("os.environ", env, clear=True):
                    assert_live_rails()  # must not raise


class TestViolationsAreCollectedTogether(unittest.TestCase):
    """Every failing rail is named in ONE raise — an operator fixes the unit
    file once instead of one restart per missing pin."""

    def test_all_missing_names_every_rail(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(BrokerCapabilityError) as captured:
                assert_live_rails()
        message = str(captured.exception)
        for var in _ALL_RAIL_VARS:
            self.assertIn(var, message, f"{var} must be named in the collected error")

    def test_two_missing_both_named_one_valid_var_absent(self):
        env = _env_without(MAX_OPEN_ENV, MAX_FEE_BPS_ENV)
        with mock.patch.dict("os.environ", env, clear=True):
            with self.assertRaises(BrokerCapabilityError) as captured:
                assert_live_rails()
        message = str(captured.exception)
        self.assertIn(MAX_OPEN_ENV, message)
        self.assertIn(MAX_FEE_BPS_ENV, message)
        # The six still-valid vars must NOT be flagged.
        for var in (
            PORTFOLIO_GROSS_FRAC_ENV,
            DAILY_LOSS_LIMIT_R_ENV,
            SIZING_EQUITY_ENV,
            SIZING_EQUITY_MODE_ENV,
            EXIT_POLICY_ENV,
            ENTRY_TRAIL_BPS_ENV,
        ):
            self.assertNotIn(f"{var}:", message)


if __name__ == "__main__":
    unittest.main()
