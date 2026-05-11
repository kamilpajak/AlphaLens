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

from alphalens.preaudit.profiles import (
    INSIDER_PC_COMPOUND_PROFILE,
    SMOKE_PROFILES,
    SmokeProfile,
)
from alphalens_cli.commands.audit import _SCRIPTS


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
