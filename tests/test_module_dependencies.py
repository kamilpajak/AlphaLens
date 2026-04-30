"""Enforce module-direction rules between alphalens packages.

The screener-agnostic design of `alphalens.backtest.*` only holds if backtest
modules do not pull in concrete screener implementations. Adapters belong with
the screener (e.g. `alphalens/screeners/themed/backtest_adapter.py`), not
inside the harness. This test parses every backtest source file with `ast` and
checks both top-level and lazy (function-scope) imports against forbidden
prefixes.

Adding a justified exception requires updating the EXEMPTIONS allowlist below
with a one-line reason — making the trade-off explicit and reviewable.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

RULES = (
    {
        "name": "backtest must stay screener-agnostic",
        "from_pkg": "alphalens.backtest",
        "forbidden_prefix": "alphalens.screeners.",
        "exemptions": {
            # Layer 2c (Lean) ARCHIVED + Layer 2b (themed) CLOSED — historical
            # validation replays recorded picks against the only available OHLCV
            # loader. Imports are flagged RESEARCH-ONLY in source.
            "alphalens/backtest/historical_validation.py",
        },
    },
    {
        # ADR 0007 + Phase 4 reorg: Layer 3 (engine) produces BacktestReport;
        # Layer 5 (attribution) consumes it. The reverse direction (engine
        # importing attribution metrics, factor regressions, verdict gates)
        # would create a cycle where the engine self-attributes its own output.
        "name": "engine must stay attribution-agnostic (BacktestReport flows L3 -> L5, not back)",
        "from_pkg": "alphalens.backtest",
        "forbidden_prefix": "alphalens.attribution.",
        "exemptions": set(),
    },
)


def _iter_imports(path: Path):
    """Yield every (module, file_relpath) for `from <module> import ...` in `path`."""
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            yield node.module


def _python_files(pkg_dir: Path):
    return sorted(p for p in pkg_dir.rglob("*.py") if p.name != "__pycache__")


class TestModuleDependencies(unittest.TestCase):
    def test_rules(self):
        violations: list[tuple[str, str, str]] = []
        for rule in RULES:
            pkg_dir = REPO_ROOT / rule["from_pkg"].replace(".", "/")
            for path in _python_files(pkg_dir):
                rel = str(path.relative_to(REPO_ROOT))
                if rel in rule["exemptions"]:
                    continue
                for module in _iter_imports(path):
                    if module.startswith(rule["forbidden_prefix"]):
                        violations.append((rule["name"], rel, module))

        self.assertEqual(
            violations,
            [],
            "module dependency violations:\n  "
            + "\n  ".join(f"[{r}] {f}: {m}" for r, f, m in violations),
        )

    def test_exemptions_still_exist(self):
        """If an exempted file is removed, the exemption entry must go too."""
        for rule in RULES:
            for rel in rule["exemptions"]:
                self.assertTrue(
                    (REPO_ROOT / rel).exists(),
                    f"exemption refers to missing file: {rel}",
                )


if __name__ == "__main__":
    unittest.main()
