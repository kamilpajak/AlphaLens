"""Enforce module-direction rules across the AlphaLens workspace.

Two tiers of rules:

1. Intra-research: the ADR 0007 layer DAG (Layer 2 screener → 3 engine →
   4 overlay → 5 attribution) is one-way. backtest must stay screener- and
   attribution-agnostic; screeners must not reach forward into backtest /
   overlays / attribution; overlays must not import attribution; attribution
   (terminal) must not import screeners; gates must not import backtest /
   attribution.

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
        "exemptions": set(),
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
    # ADR 0007 layer DAG (Layer 2 -> 3 -> 4 -> 5). A screener ranks @ time t and
    # is consumed by the engine; it must not reach forward into the engine,
    # overlay, or attribution that sit downstream of it. Three separate rules so
    # a single forbidden_prefix stays exact (a shared prefix would not cover all
    # three sibling packages).
    {
        "name": "screeners must not import backtest (Layer 2 -> 3 is one-way)",
        "from_pkg": "alphalens_research.screeners",
        "forbidden_prefix": "alphalens_research.backtest.",
        "exemptions": set(),
    },
    {
        "name": "screeners must not import overlays (Layer 2 sits upstream of Layer 4)",
        "from_pkg": "alphalens_research.screeners",
        "forbidden_prefix": "alphalens_research.overlays.",
        "exemptions": set(),
    },
    {
        "name": "screeners must not import attribution (Layer 2 sits upstream of Layer 5)",
        "from_pkg": "alphalens_research.screeners",
        "forbidden_prefix": "alphalens_research.attribution.",
        "exemptions": set(),
    },
    {
        # Layer 4 (overlay) resizes portfolio exposure on realised vol; Layer 5
        # (attribution) consumes the overlaid returns. The overlay reaching into
        # attribution would invert the L4 -> L5 direction.
        "name": "overlays must not import attribution (Layer 4 -> 5 is one-way)",
        "from_pkg": "alphalens_research.overlays",
        "forbidden_prefix": "alphalens_research.attribution.",
        "exemptions": set(),
    },
    {
        # Attribution is the terminal consumer (Layer 5). It reads BacktestReport
        # returns, never the screener that produced the picks — that would make
        # the verdict layer depend on a specific Layer 2 implementation.
        "name": "attribution must not import screeners (Layer 5 is terminal)",
        "from_pkg": "alphalens_research.attribution",
        "forbidden_prefix": "alphalens_research.screeners.",
        "exemptions": set(),
    },
    # Layer 2 selection-gate wraps a Scorer and modifies WHICH tickers deploy. It
    # sits between the screener (Layer 2) and the engine (Layer 3); it must not
    # reach forward into the engine or the attribution that sit downstream.
    {
        "name": "gates must not import backtest (gate feeds the engine, not vice versa)",
        "from_pkg": "alphalens_research.gates",
        "forbidden_prefix": "alphalens_research.backtest.",
        "exemptions": set(),
    },
    {
        "name": "gates must not import attribution (gate sits upstream of Layer 5)",
        "from_pkg": "alphalens_research.gates",
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
        """A documented exemption must stay tied to a real violation.

        Two ways an exemption can rot:
          1. The exempted file is deleted — the entry now points at nothing.
          2. The exempted file no longer contains the forbidden import — the
             entry silently widens the allowlist for a smell that is gone.

        Both fail loudly here so a stale exemption can't mask a future
        re-introduction of the same forbidden import.
        """
        for rule in RULES:
            for rel in rule["exemptions"]:
                path = WORKSPACE_ROOT / rel
                self.assertTrue(
                    path.exists(),
                    f"exemption refers to missing file: {rel}",
                )
                modules = list(_iter_imports(path, include_function_scope=True))
                self.assertTrue(
                    any(m.startswith(rule["forbidden_prefix"]) for m in modules),
                    f"dead exemption: {rel} no longer imports "
                    f"{rule['forbidden_prefix']!r} — remove it from rule "
                    f"{rule['name']!r}",
                )


if __name__ == "__main__":
    unittest.main()
