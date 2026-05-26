"""Enforce module-direction rules across the AlphaLens workspace.

Two tiers of rules:

1. Intra-research: backtest must stay screener-agnostic + attribution-agnostic
   so the Layer 3 (engine) → Layer 5 (attribution) DAG holds.

2. Cross-tier (split PR2): ``alphalens_pipeline`` must not import from
   ``alphalens_research`` — the pipeline tier is downstream-free
   infrastructure. The single exemption is the CLI (``alphalens_cli``),
   which orchestrates both tiers via lazy imports inside command bodies
   (the CLI files live in pipeline-side but route into research via
   function-scope imports — see commands/audit.py, preaudit.py,
   preregister.py).

Adding a justified exception requires updating the EXEMPTIONS allowlist
below with a one-line reason — making the trade-off explicit and reviewable.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

# Workspace root = repo top dir (two levels above this test file:
# tests/foo.py → tests/ → apps/alphalens-research/ → apps/ → repo)
WORKSPACE_ROOT = Path(__file__).resolve().parents[3]

# Map from top-level python package name to its workspace member dir.
PACKAGE_DIRS: dict[str, Path] = {
    "alphalens_pipeline": WORKSPACE_ROOT / "apps" / "alphalens-pipeline" / "alphalens_pipeline",
    "alphalens_research": WORKSPACE_ROOT / "apps" / "alphalens-research" / "alphalens_research",
    "alphalens_cli": WORKSPACE_ROOT / "apps" / "alphalens-pipeline" / "alphalens_cli",
}

RULES = (
    {
        "name": "backtest must stay screener-agnostic",
        "from_pkg": "alphalens_research.backtest",
        "forbidden_prefix": "alphalens_research.screeners.",
        "exemptions": {
            # Layer 2c (Lean) ARCHIVED + Layer 2b (themed) CLOSED — historical
            # validation replays recorded picks against the only available OHLCV
            # loader. Imports are flagged RESEARCH-ONLY in source.
            "apps/alphalens-research/alphalens_research/backtest/historical_validation.py",
        },
    },
    {
        # ADR 0007 + Phase 4 reorg: Layer 3 (engine) produces BacktestReport;
        # Layer 5 (attribution) consumes it. The reverse direction (engine
        # importing attribution metrics, factor regressions, verdict gates)
        # would create a cycle where the engine self-attributes its own output.
        "name": "engine must stay attribution-agnostic (BacktestReport flows L3 -> L5, not back)",
        "from_pkg": "alphalens_research.backtest",
        "forbidden_prefix": "alphalens_research.attribution.",
        "exemptions": set(),
    },
    {
        # Workspace split (PR2): the pipeline tier hosts live infrastructure
        # (data, core, scorers, edgar_detector, thematic, literature_scanner) and
        # must remain downstream-free. The research tier consumes pipeline,
        # never the reverse. Direct top-level imports from alphalens_pipeline
        # to alphalens_research would create a workspace-level dependency cycle.
        "name": "alphalens_pipeline must not import from alphalens_research",
        "from_pkg": "alphalens_pipeline",
        "forbidden_prefix": "alphalens_research.",
        "exemptions": set(),
    },
)


def _iter_imports(path: Path, *, include_function_scope: bool):
    """Yield every imported module name in ``path``.

    Covers both ``import X`` and ``from X import Y`` shapes (using ``ast.Import``
    + ``ast.ImportFrom`` respectively). Walks into all non-function nodes so
    forbidden imports nested in ``if TYPE_CHECKING:`` / ``try`` / ``with`` blocks
    are caught — the rule should hold module-import-time regardless of
    surrounding control flow.

    If ``include_function_scope`` is False, skip imports inside function /
    method / lambda bodies (the documented lazy-CLI pattern). Otherwise all
    imports, including lazy ones, are emitted.
    """
    tree = ast.parse(path.read_text(), filename=str(path))

    class _ImportCollector(ast.NodeVisitor):
        def __init__(self) -> None:
            self.modules: list[str] = []

        def visit_Import(self, node: ast.Import) -> None:
            for alias in node.names:
                if alias.name:
                    self.modules.append(alias.name)
            self.generic_visit(node)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            if node.module:
                self.modules.append(node.module)
            self.generic_visit(node)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            if include_function_scope:
                self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            if include_function_scope:
                self.generic_visit(node)

        def visit_Lambda(self, node: ast.Lambda) -> None:
            # Lambdas can't contain import statements; no-op for symmetry.
            return

    collector = _ImportCollector()
    collector.visit(tree)
    yield from collector.modules


def _python_files(pkg_dir: Path):
    return sorted(p for p in pkg_dir.rglob("*.py") if p.name != "__pycache__")


def _resolve_pkg_dir(from_pkg: str) -> Path:
    """Map ``alphalens_research.backtest`` → its on-disk directory."""
    parts = from_pkg.split(".")
    base = PACKAGE_DIRS.get(parts[0])
    if base is None:
        raise KeyError(f"unknown top-level package in rule: {parts[0]}")
    return base.joinpath(*parts[1:]) if len(parts) > 1 else base


class TestModuleDependencies(unittest.TestCase):
    def test_rules(self):
        violations: list[tuple[str, str, str]] = []
        for rule in RULES:
            pkg_dir = _resolve_pkg_dir(rule["from_pkg"])
            # Cross-tier pipeline rule: skip function-scope imports because the
            # CLI is allowed to lazy-import the research tier inside command
            # bodies (see module docstring).
            top_level_only = rule["from_pkg"] == "alphalens_pipeline"
            for path in _python_files(pkg_dir):
                rel = str(path.relative_to(WORKSPACE_ROOT))
                if rel in rule["exemptions"]:
                    continue
                for module in _iter_imports(path, include_function_scope=not top_level_only):
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
                    (WORKSPACE_ROOT / rel).exists(),
                    f"exemption refers to missing file: {rel}",
                )


if __name__ == "__main__":
    unittest.main()
