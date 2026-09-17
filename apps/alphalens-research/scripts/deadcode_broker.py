"""Dead-code report for the broker and contract packages (#1471).

A report for a person to read, not a CI gate. Almost every vulture finding sits
at 60% confidence, so a gate would be noise. The report still must not read
"clean" while checking nothing, so it enforces four rules:

1. The scan covers ALL production code, and the scope filter only chooses what
   to print. A broker symbol used from research, a script or Django is used.
2. Vulture exits 3 whenever it finds anything, and a file it cannot parse only
   shows up on stderr. So any stderr output, or an exit status other than 0 or
   3, counts as a tool failure, never as clean.
3. A whitelist entry must still hide a scoped finding (checked with a second
   run without the whitelist), and it must carry a reason comment.
4. Vulture cannot see a whole module that nothing imports. An AST import graph
   over production code reports every scoped module with no production importer.

Usage::

    just deadcode
    .venv/bin/python apps/alphalens-research/scripts/deadcode_broker.py

Exit status: 0 clean, 1 something to read, 2 the tool itself failed.
"""

from __future__ import annotations

import ast
import importlib.util
import io
import re
import subprocess
import sys
import tokenize
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

EXIT_CLEAN = 0
EXIT_FINDINGS = 1
EXIT_TOOL_FAILED = 2

_VULTURE_OK = 0
_VULTURE_FOUND = 3

WHITELIST_NAME = "deadcode_broker_whitelist.py"
WHITELIST_RELPATH = f"apps/alphalens-research/scripts/{WHITELIST_NAME}"

# Every root that production code lives in. Tests and migrations are excluded
# below, so a symbol that only a test calls is reported.
PRODUCTION_ROOTS: tuple[str, ...] = (
    "apps/alphalens-broker-contract/broker_contract",
    "apps/alphalens-pipeline/alphalens_pipeline",
    "apps/alphalens-pipeline/alphalens_cli",
    "apps/alphalens-research/alphalens_research",
    "apps/alphalens-research/scripts",
    "apps/alphalens-feedback/alphalens_feedback",
    "apps/alphalens-django",
)

# What the report prints. Paths are repo-relative, exactly as vulture prints
# them when it runs from the repo root.
SCOPE_PREFIXES: tuple[str, ...] = (
    "apps/alphalens-broker-contract/broker_contract/",
    "apps/alphalens-pipeline/alphalens_pipeline/brokers/",
    "apps/alphalens-pipeline/alphalens_pipeline/paper/",
    "apps/alphalens-pipeline/alphalens_cli/commands/broker.py",
)

_EXCLUDE = "*/tests/*,*/migrations/*,*/test_*.py"
# The whitelist sits under a scanned root, so the run that must see without it
# excludes it. Only that run: vulture applies --exclude to an explicit path too,
# so the same pattern on the other run would drop the whitelist silently.
_EXCLUDE_WITHOUT_WHITELIST = f"{_EXCLUDE},*/{WHITELIST_NAME}"

# Typer registers commands and callbacks through decorators, so no call site exists.
_IGNORE_DECORATORS = "@*.command,@*.callback"

# Scoped modules that no production code imports, kept on purpose.
UNWIRED_ALLOWED: Mapping[str, str] = {
    "apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/service.py": (
        "the client-manager boundary; the acceptance suite drives the real loop through it"
    ),
}

_FINDING = re.compile(r"^(?P<path>[^:]+):\d+: unused \w+(?: \w+)? '(?P<name>[^']+)'")


@dataclass(frozen=True)
class VultureRun:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str]], VultureRun]


def vulture_available() -> bool:
    return importlib.util.find_spec("vulture") is not None


def subprocess_runner(root: Path) -> Runner:
    """Run vulture from ``root`` so it prints repo-relative paths."""

    def run(argv: Sequence[str]) -> VultureRun:
        completed = subprocess.run(
            [sys.executable, "-m", "vulture", *argv],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        return VultureRun(completed.returncode, completed.stdout, completed.stderr)

    return run


def _in_scope(path: str) -> bool:
    return path.startswith(SCOPE_PREFIXES)


def _scoped_findings(stdout: str) -> list[tuple[str, str]]:
    """(line, symbol name) for every finding inside the scope."""
    found = []
    for line in stdout.splitlines():
        match = _FINDING.match(line)
        if match and _in_scope(match["path"]):
            found.append((line, match["name"]))
    return found


def whitelist_entries(path: Path) -> dict[str, str]:
    """Whitelisted name -> the reason comment on its line ("" when missing)."""
    source = path.read_text()
    comments: dict[int, str] = {}
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            comments[token.start[0]] = token.string.lstrip("#").strip()
    entries: dict[str, str] = {}
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "_"
        ):
            entries[node.value.attr] = comments.get(node.lineno, "")
    return entries


def _module_name(path: Path) -> str:
    """Dotted import name: climb while the parent directory is a package."""
    parts = [] if path.name == "__init__.py" else [path.stem]
    directory = path.parent
    while (directory / "__init__.py").is_file():
        parts.insert(0, directory.name)
        directory = directory.parent
    return ".".join(parts)


def _imported_names(tree: ast.AST, module: str, *, is_package: bool) -> set[str]:
    """Every dotted name an import statement in ``tree`` can load."""
    package = module.split(".") if is_package else module.split(".")[:-1]
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - (node.level - 1)]
                if node.module:
                    base = [*base, *node.module.split(".")]
            else:
                base = (node.module or "").split(".")
            prefix = ".".join(part for part in base if part)
            names.add(prefix)
            names.update(f"{prefix}.{alias.name}" for alias in node.names)
    return names


def _production_files(root: Path, roots: Iterable[str]) -> list[Path]:
    files = []
    for relroot in roots:
        for path in sorted((root / relroot).rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            if "/tests/" in rel or "/migrations/" in rel or path.name.startswith("test_"):
                continue
            if path.name == WHITELIST_NAME:
                continue
            files.append(path)
    return files


def _unwired_modules(root: Path, roots: Iterable[str]) -> list[str]:
    files = _production_files(root, roots)
    importers: dict[str, set[str]] = {}
    for path in files:
        module = _module_name(path)
        try:
            tree = ast.parse(path.read_text())
        except (SyntaxError, UnicodeDecodeError):
            continue  # vulture reports it on stderr, which fails the run
        for name in _imported_names(tree, module, is_package=path.name == "__init__.py"):
            importers.setdefault(name, set()).add(module)
    unwired = []
    for path in files:
        rel = path.relative_to(root).as_posix()
        if path.name == "__init__.py" or not _in_scope(rel):
            continue
        module = _module_name(path)
        if not importers.get(module, set()) - {module}:
            unwired.append(rel)
    return unwired


def _tool_failure(run: VultureRun) -> bool:
    return run.returncode not in (_VULTURE_OK, _VULTURE_FOUND) or bool(run.stderr.strip())


def run_report(
    root: Path,
    runner: Runner,
    *,
    emit: Callable[[str], object] = print,
    production_roots: Sequence[str] = PRODUCTION_ROOTS,
    unwired_allowed: Mapping[str, str] = UNWIRED_ALLOWED,
) -> int:
    decorators = ["--ignore-decorators", _IGNORE_DECORATORS]
    # Paths first: vulture refuses a path that follows its options.
    with_whitelist = runner(
        [*production_roots, WHITELIST_RELPATH, "--exclude", _EXCLUDE, *decorators]
    )
    without_whitelist = runner(
        [*production_roots, "--exclude", _EXCLUDE_WITHOUT_WHITELIST, *decorators]
    )
    for run in (with_whitelist, without_whitelist):
        if _tool_failure(run):
            emit(f"vulture failed (exit {run.returncode}); nothing was checked:")
            emit(run.stderr.strip() or run.stdout.strip())
            return EXIT_TOOL_FAILED

    problems = False
    findings = _scoped_findings(with_whitelist.stdout)
    if findings:
        problems = True
        emit(f"Unused code in the broker scope ({len(findings)}):")
        for line, _ in findings:
            emit(f"  {line}")

    entries = whitelist_entries(root / WHITELIST_RELPATH)
    hidden = {name for _, name in _scoped_findings(without_whitelist.stdout)}
    stale = sorted(name for name in entries if name not in hidden)
    if stale:
        problems = True
        emit("Whitelist entries that hide no scoped finding (remove them):")
        for name in stale:
            emit(f"  {name}")
    unexplained = sorted(name for name, reason in entries.items() if not reason)
    if unexplained:
        problems = True
        emit("Whitelist entries without a reason comment:")
        for name in unexplained:
            emit(f"  {name}")

    unwired = _unwired_modules(root, production_roots)
    not_allowed = [rel for rel in unwired if rel not in unwired_allowed]
    if not_allowed:
        problems = True
        emit("Broker modules that no production code imports:")
        for rel in not_allowed:
            emit(f"  {rel}")
    now_wired = sorted(rel for rel in unwired_allowed if rel not in unwired)
    if now_wired:
        problems = True
        emit("Allowed unwired modules that are now imported or gone (remove the entry):")
        for rel in now_wired:
            emit(f"  {rel}")

    if not problems:
        emit("Broker scope clean.")
    return EXIT_FINDINGS if problems else EXIT_CLEAN


def main() -> int:
    root = Path(__file__).resolve().parents[3]
    return run_report(root, subprocess_runner(root))


if __name__ == "__main__":
    sys.exit(main())
