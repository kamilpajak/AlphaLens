"""LIVE boot-assert (design memo §3 / ADR 0017 point 4) — the six safety-rail
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

from alphalens_pipeline.brokers.automanager.live_rails import (
    DAILY_LOSS_LIMIT_R_ENV,
    EXIT_POLICY_ENV,
    MAX_FEE_BPS_ENV,
    MAX_OPEN_ENV,
    PORTFOLIO_GROSS_FRAC_ENV,
    SIZING_EQUITY_ENV,
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
    EXIT_POLICY_ENV: "trailing_atr",
    MAX_FEE_BPS_ENV: "100",
}

_ALL_RAIL_VARS = (
    MAX_OPEN_ENV,
    PORTFOLIO_GROSS_FRAC_ENV,
    DAILY_LOSS_LIMIT_R_ENV,
    SIZING_EQUITY_ENV,
    EXIT_POLICY_ENV,
    MAX_FEE_BPS_ENV,
)


def _env_without(*names: str) -> dict[str, str]:
    return {k: v for k, v in _VALID_ENV.items() if k not in names}


class TestAllSixConstantsAreDistinctNames(unittest.TestCase):
    def test_env_var_names(self):
        self.assertEqual(MAX_OPEN_ENV, "ALPHALENS_BROKER_MAX_OPEN")
        self.assertEqual(PORTFOLIO_GROSS_FRAC_ENV, "ALPHALENS_BROKER_PORTFOLIO_GROSS_FRAC")
        self.assertEqual(DAILY_LOSS_LIMIT_R_ENV, "ALPHALENS_BROKER_DAILY_LOSS_LIMIT_R")
        self.assertEqual(SIZING_EQUITY_ENV, "ALPHALENS_BROKER_SIZING_EQUITY")
        self.assertEqual(EXIT_POLICY_ENV, "ALPHALENS_BROKER_EXIT_POLICY")
        self.assertEqual(MAX_FEE_BPS_ENV, "ALPHALENS_BROKER_MAX_FEE_BPS")
        self.assertEqual(len(set(_ALL_RAIL_VARS)), 6, "all six env-var names must be distinct")


class TestValidEnvPasses(unittest.TestCase):
    def test_all_six_set_in_bounds_passes(self):
        with mock.patch.dict("os.environ", _VALID_ENV, clear=True):
            assert_live_rails()  # must not raise

    def test_bound_edges_pass(self):
        """The bounds are inclusive at the documented edges."""
        edge_env = dict(_VALID_ENV)
        edge_env[MAX_OPEN_ENV] = "2"
        edge_env[PORTFOLIO_GROSS_FRAC_ENV] = "0.5"
        edge_env[DAILY_LOSS_LIMIT_R_ENV] = "2.0"
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

    def test_exit_policy_unset(self):
        with mock.patch.dict("os.environ", _env_without(EXIT_POLICY_ENV), clear=True):
            with self.assertRaises(BrokerCapabilityError) as captured:
                assert_live_rails()
        self.assertIn(EXIT_POLICY_ENV, str(captured.exception))

    def test_max_fee_bps_unset(self):
        with mock.patch.dict("os.environ", _env_without(MAX_FEE_BPS_ENV), clear=True):
            with self.assertRaises(BrokerCapabilityError) as captured:
                assert_live_rails()
        self.assertIn(MAX_FEE_BPS_ENV, str(captured.exception))


class TestOutOfBoundsIsNamedInTheError(unittest.TestCase):
    def test_max_open_zero_rejected(self):
        env = dict(_VALID_ENV, **{MAX_OPEN_ENV: "0"})
        with mock.patch.dict("os.environ", env, clear=True):
            with self.assertRaises(BrokerCapabilityError) as captured:
                assert_live_rails()
        self.assertIn(MAX_OPEN_ENV, str(captured.exception))

    def test_max_open_above_cap_rejected(self):
        env = dict(_VALID_ENV, **{MAX_OPEN_ENV: "3"})
        with mock.patch.dict("os.environ", env, clear=True):
            with self.assertRaises(BrokerCapabilityError) as captured:
                assert_live_rails()
        self.assertIn(MAX_OPEN_ENV, str(captured.exception))

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

    def test_all_six_missing_names_all_six(self):
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
        # The four still-valid vars must NOT be flagged.
        for var in (
            PORTFOLIO_GROSS_FRAC_ENV,
            DAILY_LOSS_LIMIT_R_ENV,
            SIZING_EQUITY_ENV,
            EXIT_POLICY_ENV,
        ):
            self.assertNotIn(f"{var}:", message)


if __name__ == "__main__":
    unittest.main()
