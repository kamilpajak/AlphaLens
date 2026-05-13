"""Unit tests for SIMFIN_DATA_DIR env var fallback in SimFinFundamentalsStore.

Issue #105 H2: the audit orchestrator passes `extra_env={"SIMFIN_DATA_DIR": ...}`
to subprocesses so they can resolve their cache to the pod-local NVMe path.
Before this fix the store ignored the env var and always fell back to
~/.alphalens/simfin_cache, defeating the orchestrator plumbing.

Resolution order asserted here:
    explicit cache_dir kwarg > SIMFIN_DATA_DIR env > _default_cache_dir()
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class TestSimFinDataDirEnv(unittest.TestCase):
    def test_env_var_used_when_no_explicit_cache_dir(self):
        from alphalens.data.store.simfin import SimFinFundamentalsStore

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "from_env"
            with patch.dict(
                os.environ,
                {"SIMFIN_API_KEY": "testkey", "SIMFIN_DATA_DIR": str(target)},
            ):
                store = SimFinFundamentalsStore()
            self.assertEqual(store.cache_dir, target)
            self.assertTrue(target.is_dir())

    def test_explicit_cache_dir_overrides_env(self):
        from alphalens.data.store.simfin import SimFinFundamentalsStore

        with tempfile.TemporaryDirectory() as tmp:
            explicit = Path(tmp) / "explicit"
            from_env = Path(tmp) / "from_env_ignored"
            with patch.dict(
                os.environ,
                {"SIMFIN_API_KEY": "testkey", "SIMFIN_DATA_DIR": str(from_env)},
            ):
                store = SimFinFundamentalsStore(cache_dir=explicit)
            self.assertEqual(store.cache_dir, explicit)
            self.assertTrue(explicit.is_dir())
            self.assertFalse(from_env.exists())

    def test_falls_back_to_default_when_neither_set(self):
        from alphalens.data.store import simfin as simfin_mod
        from alphalens.data.store.simfin import SimFinFundamentalsStore

        with tempfile.TemporaryDirectory() as tmp:
            fake_default = Path(tmp) / "fake_default"
            env = {k: v for k, v in os.environ.items() if k != "SIMFIN_DATA_DIR"}
            env["SIMFIN_API_KEY"] = "testkey"
            with (
                patch.dict(os.environ, env, clear=True),
                patch.object(simfin_mod, "_default_cache_dir", return_value=fake_default),
            ):
                store = SimFinFundamentalsStore()
            self.assertEqual(store.cache_dir, fake_default)
            self.assertTrue(fake_default.is_dir())


if __name__ == "__main__":
    unittest.main()
