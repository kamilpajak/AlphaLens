"""Enforce that SCORER_CONFIG (Docker-side) and LEAN_DEFAULTS (host-side) stay in sync.

The Lean algorithm runs inside the QuantConnect Docker container where the host
`alphalens` package is not importable, so scoring weights and feature windows
must be inlined in `lean_project/main.py`. This test guards against silent drift.

Parsing approach: `main.py` cannot be imported on the host (it pulls in
`AlgorithmImports`, a Docker-only symbol bag). We extract `SCORER_CONFIG` via
`ast.literal_eval` on the module source — faithful to the literal as written.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

HOST_ONLY_KEYS = frozenset(
    {
        "polygon_base_url",
        "polygon_rate_limit_per_min",
        "history_bootstrap_days",
    }
)
DOCKER_ONLY_KEYS: frozenset[str] = frozenset()


def _extract_dict_literal(source_path: Path, name: str) -> dict:
    """Return the literal value of a top-level `name = {...}` assignment."""
    tree = ast.parse(source_path.read_text(), filename=str(source_path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id == name:
                return ast.literal_eval(node.value)
    raise AssertionError(f"top-level dict literal {name!r} not found in {source_path}")


class TestLeanConfigParity(unittest.TestCase):
    """Docker-inlined SCORER_CONFIG must match host LEAN_DEFAULTS on shared keys."""

    @classmethod
    def setUpClass(cls):
        from alphalens.screeners.lean.config import LEAN_DEFAULTS

        cls.host_config = LEAN_DEFAULTS

        repo_root = Path(__file__).resolve().parent.parent
        docker_main = repo_root / "alphalens/screeners/lean/lean_project/main.py"
        cls.docker_config = _extract_dict_literal(docker_main, "SCORER_CONFIG")

    def test_shared_keys_have_identical_values(self):
        host_keys = set(self.host_config) - HOST_ONLY_KEYS
        docker_keys = set(self.docker_config) - DOCKER_ONLY_KEYS
        shared = host_keys & docker_keys

        mismatches = {
            key: (self.host_config[key], self.docker_config[key])
            for key in shared
            if self.host_config[key] != self.docker_config[key]
        }
        self.assertEqual(
            mismatches,
            {},
            f"value drift between LEAN_DEFAULTS and SCORER_CONFIG: {mismatches}",
        )

    def test_host_only_keys_match_allowlist(self):
        """Keys present only on host side must match HOST_ONLY_KEYS exactly.

        If a new host-only key appears (or an expected one disappears), this test
        fails — forcing an explicit decision: inline it in Docker too, or extend
        the allowlist with a justification.
        """
        actual_host_only = set(self.host_config) - set(self.docker_config)
        self.assertEqual(
            actual_host_only,
            set(HOST_ONLY_KEYS),
            "host-only key set drifted from allowlist",
        )

    def test_docker_only_keys_match_allowlist(self):
        actual_docker_only = set(self.docker_config) - set(self.host_config)
        self.assertEqual(
            actual_docker_only,
            set(DOCKER_ONLY_KEYS),
            "docker-only key set drifted from allowlist",
        )


if __name__ == "__main__":
    unittest.main()
