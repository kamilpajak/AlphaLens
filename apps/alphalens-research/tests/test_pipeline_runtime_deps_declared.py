"""Pin every third-party import in ``alphalens_pipeline`` + ``alphalens_cli``
to a package that's actually resolvable inside the pipeline Docker image.

Why this exists:
  ``uv sync --frozen --no-dev --package alphalens-pipeline`` in the
  pipeline Docker image installs ONLY the runtime-dep closure of
  ``alphalens-pipeline``. Anything that happens to be available in the
  workspace venv via a sibling package — typically
  ``alphalens-research`` dev deps OR the Django app pulling
  ``drf-spectacular`` — gets stripped at the Docker layer.

  The pattern bit us in 2026-05-31 when ``jsonschema`` (used by
  ``thematic.extraction.templates.yaml_schema``) was only available
  workspace-wide via ``drf-spectacular``. Local tests + dev CLI both
  passed; the freshly-rebuilt pipeline image crashed at
  ``alphalens --help`` because the CLI eagerly registers the
  ``templates`` subapp at startup. See
  ``feedback_pipeline_image_manual_rebuild_after_merge``.

  ``uv export --no-dev --package alphalens-pipeline`` is the source of
  truth — it returns the exact set of distributions ``uv sync`` would
  install in the slim Docker image. Reading that closure catches both
  the "direct dep forgotten" class and the deeper "transitive supplier
  flipped" class.

Scope (excluded from the import scan):
  - Standard-library modules (json, datetime, pathlib, …)
  - First-party packages (``alphalens_pipeline``, ``alphalens_cli``,
    ``alphalens_research`` for the documented lazy-CLI import exception)
  - Optional / fallback imports inside ``try``/``except ImportError`` —
    contract there is "absence is handled at runtime"
"""

from __future__ import annotations

import ast
import functools
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
PIPELINE_DIR = REPO_ROOT / "apps" / "alphalens-pipeline"
PIPELINE_PYPROJECT = PIPELINE_DIR / "pyproject.toml"
SCANNED_DIRS = (
    PIPELINE_DIR / "alphalens_pipeline",
    PIPELINE_DIR / "alphalens_cli",
)

# Module names that ship with CPython itself. ast.walk yields the
# top-level package (``json`` for ``json.dumps``), so this stdlib check
# only needs the top-level names. ``sys.stdlib_module_names`` is the
# authoritative source as of 3.10.
STDLIB_MODULES: frozenset[str] = frozenset(sys.stdlib_module_names)

# First-party workspace packages — never come from PyPI.
# ``alphalens_research`` appears here because the documented exception
# in CLAUDE.md ("Lazy CLI imports") allows pipeline CLI command bodies
# to import from research — those imports are reachable in dev but not
# inside the pipeline Docker image. They never execute at CLI startup,
# so an absent module is fine for the cron path; treat as first-party.
# ``alphalens_feedback`` is a DIFFERENT case: it is a declared workspace
# dependency of alphalens-pipeline (see its pyproject) that the pipeline
# image installs for real — outcome_join/shadow_return import it at module
# top level. It is skipped here because it comes from the workspace, not
# PyPI; its actual presence inside the image is proven by the image-smoke
# CI job (build + import), not by this closure check.
FIRST_PARTY_PREFIXES: tuple[str, ...] = (
    "alphalens_pipeline",
    "alphalens_cli",
    "alphalens_research",
    "alphalens_feedback",
)

# Distribution name → top-level import-module names. Most are identical
# after lower-case + s/-/_/ (e.g. ``json-repair`` → ``json_repair``),
# but PyPI distribution names and importable top-level modules diverge
# often enough that we keep an explicit override table for the cases
# that don't follow the heuristic.
DIST_TO_MODULE_OVERRIDES: dict[str, set[str]] = {
    "pyyaml": {"yaml"},
    "python-dotenv": {"dotenv"},
    "beautifulsoup4": {"bs4"},
    "phase-robust-backtesting": {"phase_robust_backtesting"},
    "exchange-calendars": {"exchange_calendars"},
    "json-repair": {"json_repair"},
    "jsonschema-specifications": {"jsonschema_specifications"},
}


@functools.lru_cache(maxsize=1)
def _resolved_pipeline_modules() -> set[str]:
    """Return the top-level import modules reachable in the pipeline image.

    Runs ``uv export --no-dev --package alphalens-pipeline``, which
    matches the exact ``uv sync`` invocation in
    ``deploy/docker/Dockerfile.pipeline``. The output lists every
    distribution + version in the runtime closure; we strip versions
    and map to top-level module names via DIST_TO_MODULE_OVERRIDES /
    the s/-/_/ fallback.

    Cached for test-run lifetime — the subprocess takes 200-400ms.
    """
    try:
        result = subprocess.run(
            [
                "uv",
                "export",
                "--no-dev",
                "--package",
                "alphalens-pipeline",
                "--format",
                "requirements-txt",
            ],
            check=True,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise unittest.SkipTest(
            f"uv export failed ({exc}); cannot verify pipeline runtime closure"
        ) from exc

    modules: set[str] = set()
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        # Skip blank lines, comments, hash continuations, and the
        # workspace package itself.
        if not line or line.startswith(("#", "--", "-e ")):
            continue
        # ``pandas==2.3.0 \`` form; split on the first space or ``==``.
        token = line.split(" ", 1)[0].split("==", 1)[0].strip()
        if not token:
            continue
        dist = token.lower()
        if dist in DIST_TO_MODULE_OVERRIDES:
            modules.update(DIST_TO_MODULE_OVERRIDES[dist])
        else:
            modules.add(dist.replace("-", "_"))

    # Parser sanity check (zen pre-merge MEDIUM, PR #326 follow-up):
    # ``uv export --format requirements-txt`` is not a formally
    # documented stable interface. A future uv minor bump could change
    # line-continuation or header formatting in a way that silently
    # drops entries — which would turn this test into a false-negative
    # gate (it'd pass even after re-introducing the jsonschema bug).
    # These three modules are unconditionally required by the pipeline
    # (pandas + typer + requests are top-of-file imports in dozens of
    # files); if any is missing the parser is broken, not the deps.
    canary_modules = {"pandas", "typer", "requests"}
    missing_canary = canary_modules - modules
    if missing_canary:
        raise unittest.SkipTest(
            f"uv export parser sanity check failed — missing {sorted(missing_canary)} "
            "from resolved closure. Likely cause: `uv export --format "
            "requirements-txt` output format changed and the parser needs "
            "an update. Refusing to run the enforcement check on a stale "
            "closure (would produce false negatives)."
        )
    return modules


_DEFERRED_PARENT_TYPES: tuple[type, ...] = (
    ast.Try,
    # Function-body imports are the documented "lazy CLI" exception in
    # CLAUDE.md (`apps/alphalens-pipeline/alphalens_cli/commands/{audit,
    # preaudit,preregister}.py` import phase-robust-backtesting +
    # alphalens_research inside command bodies). They never execute at
    # startup so absence in the slim Docker image is benign.
    ast.FunctionDef,
    ast.AsyncFunctionDef,
)


def _is_type_checking_guard(parent: ast.AST) -> bool:
    """True if ``parent`` is an ``if TYPE_CHECKING:`` block.

    ``from typing import TYPE_CHECKING`` is False at runtime, so any
    import nested inside ``if TYPE_CHECKING:`` never executes — static
    analysers parse the block but Python skips it. Common pattern for
    forward type hints; absent in the slim Docker image is fine.
    Caught by zen pre-merge review of PR #326 (MEDIUM).
    """
    if not isinstance(parent, ast.If):
        return False
    test = parent.test
    if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
        return True
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


def _is_deferred_import(node: ast.AST, tree: ast.AST) -> bool:
    """True if ``node`` sits inside a deferred-execution parent block.

    Three patterns all mean "absence is handled at runtime":
      - try/except — catches the ImportError
      - function body — only fires when the function is called; the
        CLI cron path never calls audit / preaudit / preregister
      - ``if TYPE_CHECKING:`` — block is False at runtime, body skipped
    """
    for parent in ast.walk(tree):
        if isinstance(parent, _DEFERRED_PARENT_TYPES) or _is_type_checking_guard(parent):
            for child in ast.walk(parent):
                if child is node:
                    return True
    return False


def _walk_imports(py_file: Path):
    """Yield (top-level-module-name, import-node, parsed-tree) per import."""
    try:
        source = py_file.read_text()
    except (OSError, UnicodeDecodeError):
        return
    try:
        tree = ast.parse(source, filename=str(py_file))
    except SyntaxError:
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.split(".")[0], node, tree
        elif isinstance(node, ast.ImportFrom):
            # Relative imports (``from .foo import bar``) carry level > 0.
            if node.level and node.level > 0:
                continue
            if not node.module:
                continue
            yield node.module.split(".")[0], node, tree


class TestEveryPipelineImportInDockerImage(unittest.TestCase):
    """Every top-level import in pipeline + CLI must trace to a package
    reachable in the pipeline Docker image's runtime closure."""

    def test_no_module_missing_from_resolved_closure(self):
        resolved = _resolved_pipeline_modules()
        missing: dict[str, list[str]] = {}
        for py_dir in SCANNED_DIRS:
            for py_file in py_dir.rglob("*.py"):
                rel = py_file.relative_to(REPO_ROOT)
                for module, node, tree in _walk_imports(py_file):
                    if module in STDLIB_MODULES:
                        continue
                    if any(module.startswith(p) for p in FIRST_PARTY_PREFIXES):
                        continue
                    if module in resolved:
                        continue
                    if _is_deferred_import(node, tree):
                        continue
                    missing.setdefault(module, []).append(f"{rel}:{node.lineno}")
        if missing:
            lines = [
                "Found imports not reachable inside the pipeline Docker image "
                "(uv export --no-dev --package alphalens-pipeline):"
            ]
            for module, sites in sorted(missing.items()):
                lines.append(
                    f"  - {module}: {sites[0]}"
                    + (f" (+{len(sites) - 1} more)" if len(sites) > 1 else "")
                )
            lines.append(
                "\nDeclare the missing distribution in "
                "apps/alphalens-pipeline/pyproject.toml [project.dependencies], "
                "OR add a mapping in DIST_TO_MODULE_OVERRIDES at the top of "
                "this test file if the PyPI dist name doesn't match the "
                "import name, OR wrap the import in a try/except ImportError "
                "block if the dep is genuinely optional."
            )
            self.fail("\n".join(lines))


class TestJsonschemaSpecificallyDeclared(unittest.TestCase):
    """Pin the specific regression that prompted this test.

    Even if the general test gets a future false-negative tweak, the
    jsonschema declaration must stay because the CLI's eager templates
    registration crashes without it.
    """

    def test_jsonschema_in_pipeline_pyproject(self):
        data = tomllib.loads(PIPELINE_PYPROJECT.read_text())
        deps = data.get("project", {}).get("dependencies", []) or []
        names = [d.split(">=")[0].split("==")[0].split("[")[0].strip().lower() for d in deps]
        self.assertIn(
            "jsonschema",
            names,
            "jsonschema MUST be a runtime dep of alphalens-pipeline — "
            "yaml_schema.py imports it at module load + the CLI eagerly "
            "registers the templates subapp at startup.",
        )


class TestTransitiveOnlyDistsDeclaredDirectly(unittest.TestCase):
    """Pin used-but-only-transitively-supplied dists as DIRECT deps.

    ``beautifulsoup4`` (imported as ``bs4`` in thematic.verification.
    tenk_grep) rides in via ``yfinance``; ``numpy`` rides in via
    ``pandas``/``scipy``. Both are imported directly by pipeline code, so
    a future yfinance / pandas bump that drops them would break
    ``alphalens thematic verify`` (bs4) or the scorers + schemas (numpy)
    at runtime on the VPS while the general closure test stays green
    (the dist is still IN the closure, just no longer ours to keep).
    Declaring them directly makes the dependency explicit and pins the
    floor. Same class CLAUDE.md already records as fixed for
    httpx / pyyaml.
    """

    def _declared_dist_names(self) -> list[str]:
        data = tomllib.loads(PIPELINE_PYPROJECT.read_text())
        deps = data.get("project", {}).get("dependencies", []) or []
        return [d.split(">=")[0].split("==")[0].split("[")[0].strip().lower() for d in deps]

    def test_beautifulsoup4_in_pipeline_pyproject(self):
        self.assertIn(
            "beautifulsoup4",
            self._declared_dist_names(),
            "beautifulsoup4 MUST be a direct runtime dep — tenk_grep.py "
            "imports `from bs4 import BeautifulSoup` and it only rides in "
            "transitively via yfinance.",
        )

    def test_numpy_in_pipeline_pyproject(self):
        self.assertIn(
            "numpy",
            self._declared_dist_names(),
            "numpy MUST be a direct runtime dep — it is imported in ~9 "
            "pipeline modules (scorers, data.schemas, thematic.screening) "
            "and only rides in transitively via pandas/scipy.",
        )


class TestDeferredImportExemptions(unittest.TestCase):
    """Unit-test the AST-walk filter against synthetic snippets.

    Pins the exemption matrix so a future refactor of the filter (e.g.
    adding more parent types) preserves the documented contract.
    """

    def _walk_and_classify(self, src: str) -> list[tuple[str, bool]]:
        tree = ast.parse(src)
        out: list[tuple[str, bool]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.name.split(".")[0]
                    out.append((name, _is_deferred_import(node, tree)))
            elif (
                isinstance(node, ast.ImportFrom)
                and node.module
                and not (node.level and node.level > 0)
            ):
                name = node.module.split(".")[0]
                out.append((name, _is_deferred_import(node, tree)))
        return out

    def test_top_level_import_not_deferred(self):
        out = self._walk_and_classify("import pandas")
        self.assertEqual(out, [("pandas", False)])

    def test_function_body_import_is_deferred(self):
        src = "def f():\n    import pandas\n"
        self.assertEqual(self._walk_and_classify(src), [("pandas", True)])

    def test_try_block_import_is_deferred(self):
        src = "try:\n    import pandas\nexcept ImportError:\n    pandas = None\n"
        self.assertEqual(self._walk_and_classify(src), [("pandas", True)])

    def test_type_checking_block_import_is_deferred(self):
        src = "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import pandas\n"
        out = self._walk_and_classify(src)
        # First entry is the unconditional ``from typing import …`` —
        # NOT deferred. Second entry is the guarded pandas import —
        # MUST be deferred (False at runtime).
        self.assertEqual(out, [("typing", False), ("pandas", True)])

    def test_typing_dot_type_checking_also_deferred(self):
        # Variant: ``if typing.TYPE_CHECKING:`` (qualified attribute
        # access instead of bare name). Same semantics, same skip.
        src = "import typing\nif typing.TYPE_CHECKING:\n    import pandas\n"
        out = self._walk_and_classify(src)
        self.assertEqual(out, [("typing", False), ("pandas", True)])


if __name__ == "__main__":
    unittest.main()
