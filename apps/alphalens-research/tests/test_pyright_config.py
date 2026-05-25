"""Pin pyrightconfig.json integrity.

This is a config-only test: it does not run pyright. The CI step runs the
checker; this guard prevents the config file from silently rotting (invalid
JSON, dropped strict-mode envs, accidentally widened typeCheckingMode).

Why these specific invariants: the strict execution environments on
backtest/attribution/scorers are the load-bearing part of the config.
Drop one of them and we silently lose type guarantees on the surfaces
where wrong types corrupt verdicts.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = REPO_ROOT / "pyrightconfig.json"

STRICT_ROOTS = {
    "apps/alphalens-research/alphalens_research/backtest",
    "apps/alphalens-research/alphalens_research/attribution",
    # Scorers strict at file granularity — opportunistic_form4.py is
    # SHA256-locked by experiment_insider_pc_compound._COMPONENT_LOCKED_HASHES,
    # so it cannot carry the pragma overrides the rest of the dir uses.
    "apps/alphalens-pipeline/alphalens_pipeline/scorers/_common.py",
    "apps/alphalens-pipeline/alphalens_pipeline/scorers/cohen_malloy_classifier.py",
    "apps/alphalens-pipeline/alphalens_pipeline/scorers/fcff_yield.py",
}


def _load_config() -> dict:
    # pyrightconfig.json is JSONC (pyright supports // line comments); strip
    # them before handing to stdlib json which is strict-JSON only.
    raw = CONFIG_PATH.read_text(encoding="utf-8")
    clean = "\n".join(line for line in raw.splitlines() if not line.lstrip().startswith("//"))
    return json.loads(clean)


class TestPyrightConfig(unittest.TestCase):
    def test_config_file_exists(self):
        self.assertTrue(CONFIG_PATH.is_file(), f"missing {CONFIG_PATH}")

    def test_config_parses_as_json(self):
        _load_config()

    def test_global_mode_is_basic(self):
        cfg = _load_config()
        self.assertEqual(cfg.get("typeCheckingMode"), "basic")

    def test_python_version_pinned(self):
        cfg = _load_config()
        self.assertEqual(cfg.get("pythonVersion"), "3.13")

    def test_strict_directories_listed(self):
        # Pyright only applies strict mode to dirs listed in the top-level
        # `strict` array. executionEnvironments does NOT carry strictness —
        # it only overrides extraPaths/pythonVersion. Catching that regression
        # is the whole reason this test exists.
        cfg = _load_config()
        strict_dirs = set(cfg.get("strict", []))
        for expected in STRICT_ROOTS:
            self.assertIn(
                expected,
                strict_dirs,
                f"strict directory missing from top-level 'strict' array: {expected}",
            )

    def test_includes_cover_all_three_apps(self):
        cfg = _load_config()
        include = cfg.get("include", [])
        joined = " ".join(include)
        self.assertIn("alphalens_pipeline", joined)
        self.assertIn("alphalens_research", joined)
        self.assertIn("alphalens-django", joined)


if __name__ == "__main__":
    unittest.main()
