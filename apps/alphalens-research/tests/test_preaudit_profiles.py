"""Registry sanity tests for SMOKE_PROFILES.

Guards against three classes of regression:

1. Ghost profile — a profile keyed by a strategy that no longer
   exists in :data:`alphalens_cli.commands.audit._SCRIPTS`.
2. Profile drift — the locked smoke args (e.g. ``--skip-precheck``,
   ``--universe-size-cap``) silently removed.
3. Hash-guard claim mismatch — a profile declares
   ``has_component_hash_guard=True`` but the corresponding experiment
   script doesn't actually define ``_verify_component_hashes``.
"""

from __future__ import annotations

import unittest
from datetime import date

from alphalens_cli.commands.audit import _SCRIPTS
from alphalens_research.preaudit.profiles import (
    INSIDER_PC_COMPOUND_PROFILE,
    PEAD_PSS_V2_PROFILE,
    SMOKE_PROFILE_EXEMPT,
    SMOKE_PROFILES,
    SmokeProfile,
)


class TestSmokeProfileRegistry(unittest.TestCase):
    def test_registry_is_nonempty(self):
        self.assertGreater(len(SMOKE_PROFILES), 0)

    def test_every_profile_key_resolves_in_audit_scripts(self):
        unknown = sorted(set(SMOKE_PROFILES) - set(_SCRIPTS))
        self.assertEqual(
            unknown,
            [],
            f"ghost profiles (not in audit._SCRIPTS): {unknown}",
        )

    def test_profile_strategy_matches_dict_key(self):
        for key, profile in SMOKE_PROFILES.items():
            self.assertEqual(
                key,
                profile.strategy,
                f"dict key {key!r} != profile.strategy {profile.strategy!r}",
            )

    def test_every_audit_script_has_profile_or_is_exempt(self):
        """Inverse drift guard (zen 2026-05-11): when a new strategy
        lands in `audit._SCRIPTS`, this test fails until either a
        `SmokeProfile` is added OR the strategy is explicitly listed in
        `SMOKE_PROFILE_EXEMPT`. Forces the decision at PR review time,
        not at audit-launch time.
        """
        unregistered = set(_SCRIPTS) - set(SMOKE_PROFILES) - SMOKE_PROFILE_EXEMPT
        self.assertEqual(
            unregistered,
            set(),
            f"strategies in _SCRIPTS without a SmokeProfile and not in "
            f"SMOKE_PROFILE_EXEMPT: {sorted(unregistered)}. "
            f"Add a SmokeProfile to SMOKE_PROFILES, or document the "
            f"exemption in SMOKE_PROFILE_EXEMPT in alphalens_research/preaudit/profiles.py.",
        )

    def test_no_overlap_between_profiles_and_exempt(self):
        overlap = set(SMOKE_PROFILES) & SMOKE_PROFILE_EXEMPT
        self.assertEqual(
            overlap,
            set(),
            f"strategies both registered and exempt (contradictory): {sorted(overlap)}",
        )


class TestInsiderPcCompoundProfileLock(unittest.TestCase):
    """Lock the smoke profile for insider_pc_compound against silent edit."""

    profile: SmokeProfile = INSIDER_PC_COMPOUND_PROFILE

    def test_strategy_name(self):
        self.assertEqual(self.profile.strategy, "insider_pc_compound")

    def test_smoke_window_is_2019_q1(self):
        self.assertEqual(self.profile.smoke_window, (date(2019, 1, 1), date(2019, 3, 31)))

    def test_extra_args_include_skip_precheck(self):
        # Pod doesn't carry pre-2018 iVol coverage; --skip-precheck is the
        # documented runpod pattern (per memo §3.5 + PR #96).
        self.assertIn("--skip-precheck", self.profile.extra_args)

    def test_extra_args_include_universe_size_cap_at_least_300(self):
        # cap < 100 risks empty-asof FALSE-GREEN; cap=300 matches golden
        # master fixture.
        args = list(self.profile.extra_args)
        self.assertIn("--universe-size-cap", args)
        idx = args.index("--universe-size-cap")
        cap = int(args[idx + 1])
        self.assertGreaterEqual(
            cap,
            300,
            f"cap {cap} < 300 risks silent empty-asof FALSE-GREEN",
        )

    def test_has_component_hash_guard_true(self):
        # insider_pc_compound is the only strategy today with
        # _verify_component_hashes inside its experiment script.
        self.assertTrue(self.profile.has_component_hash_guard)

    def test_data_deps_cover_known_runtime_paths(self):
        names = {d.name for d in self.profile.data_deps}
        # These match _PRICES_DIR / _FORM4_PARQUET_DEFAULT / _SMD_DIR
        # constants in scripts/experiment_insider_pc_compound.py.
        self.assertIn("form4_parquet", names)
        self.assertIn("ivolatility_smd", names)
        self.assertIn("prices", names)


class TestPeadPssV2ProfileLock(unittest.TestCase):
    """Lock the smoke profile for paradigm-14 PEAD v2 against silent edit.

    Pre-reg ledger: pead_v5_pss_2026_05_13 under event_drift_search_2026_05_03.
    """

    profile: SmokeProfile = PEAD_PSS_V2_PROFILE

    def test_strategy_name(self):
        self.assertEqual(self.profile.strategy, "pead_pss_v2_2026_05_13")

    def test_smoke_window_is_2018_q1(self):
        # A3-validated anchor events (AAPL/JPM/UNH/CAT/RSG) all fall in
        # 2018-Q1; the smoke window aligns with the IS-phase warm window.
        self.assertEqual(self.profile.smoke_window, (date(2018, 1, 1), date(2018, 3, 31)))

    def test_extra_args_include_skip_precheck(self):
        self.assertIn("--skip-precheck", self.profile.extra_args)

    def test_extra_args_include_universe_size_cap(self):
        args = list(self.profile.extra_args)
        self.assertIn("--universe-size-cap", args)
        idx = args.index("--universe-size-cap")
        cap = int(args[idx + 1])
        # cap=200 keeps wall < 5 min on warm AV cache.
        self.assertGreaterEqual(cap, 100)

    def test_extra_args_include_daily_rebalance_stride(self):
        # PEAD is daily-rebalance by construction (B2 contract); stride=1
        # is the documented orchestrator-compat value.
        args = list(self.profile.extra_args)
        self.assertIn("--rebalance-stride", args)
        idx = args.index("--rebalance-stride")
        self.assertEqual(int(args[idx + 1]), 1)

    def test_data_deps_include_av_cache_and_prices_and_factors(self):
        names = {d.name for d in self.profile.data_deps}
        # AV EARNINGS cache (per-ticker JSON), yfinance OHLCV, FF5+UMD factors.
        self.assertIn("av_cache", names)
        self.assertIn("prices", names)
        self.assertIn("factors", names)

    def test_has_component_hash_guard_false(self):
        # PEAD scaffold has no component-hash invariants (single scorer,
        # no compound-source mismatch surface like insider_pc_compound).
        self.assertFalse(self.profile.has_component_hash_guard)


class TestHashGuardClaimsAreHonest(unittest.TestCase):
    """If a profile says has_component_hash_guard=True, the experiment
    script MUST actually define `_verify_component_hashes`."""

    def test_hash_guard_function_exists_for_flagged_profiles(self):
        for profile in SMOKE_PROFILES.values():
            if not profile.has_component_hash_guard:
                continue
            module_path = _SCRIPTS[profile.strategy]
            source = module_path.read_text(encoding="utf-8")
            self.assertIn(
                "_verify_component_hashes",
                source,
                f"{profile.strategy!r} claims hash guard but "
                f"{module_path.name} has no _verify_component_hashes",
            )


if __name__ == "__main__":
    unittest.main()
