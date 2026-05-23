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
    "apps/alphalens-pipeline/alphalens_pipeline/scorers",
}


def _load_config() -> dict:
    raw = CONFIG_PATH.read_text(encoding="utf-8")
    return json.loads(raw)


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

    def test_strict_execution_environments_present(self):
        cfg = _load_config()
        envs = cfg.get("executionEnvironments", [])
        roots = {env.get("root") for env in envs}
        for expected in STRICT_ROOTS:
            self.assertIn(
                expected,
                roots,
                f"strict execution environment missing for {expected}",
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
