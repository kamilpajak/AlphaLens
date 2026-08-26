"""Pin that every uv workspace member sits inside every repo-wide Python gate.

Why this exists (issue #1140):
  ``apps/alphalens-broker-contract`` holds the exit-geometry policies, the
  sizing contract, the FX conversion object and the cost constants — the money
  arithmetic of the execution layer — and neither gate ever looked at it:

    * ``pyrightconfig.json`` listed pipeline / research / django in ``include``.
    * ``.github/workflows/ci.yml`` named the same two paths in ``ruff check``,
      ``ruff format --check`` and ``bandit -r``.

  "pyright is green" and "ruff is clean" were true statements that said nothing
  about the package. ``apps/alphalens-feedback`` had the same hole.

  The guard that was supposed to catch this — the old
  ``test_pyright_config.py::test_includes_cover_all_three_apps`` — hard-coded
  the number three and matched by substring, so a fourth or fifth member could
  never trip it. That is the rot this file replaces: the expectation is now
  DERIVED from ``[tool.uv.workspace] members`` in the root ``pyproject.toml``,
  so adding a member without adding it to the gates fails here automatically.

Scope decisions (why this is parity, not equality):
  - The invariant is one-directional: member ⇒ inside the gate. A gate path
    that is not a workspace member is fine (``apps/alphalens-django/briefs`` is
    one such entry, and pyright deliberately includes the Django apps
    individually rather than the whole member directory).
  - A gate is checked against the UNION of its invocations across the whole
    workflow, because the Django app is linted in its own CI job with its own
    ``uv sync``. Requiring one command line to name every member would force a
    layout the workflow does not have.
  - Coverage and SonarCloud have the same blind spot on broker-contract and are
    deliberately NOT asserted here — widening either moves the coverage
    denominator and so the quality gate, which needs its own measurement. They
    are tracked separately.

Positive control:
  ``test_positive_control_ghost_member_would_fail`` drives the same pure helper
  with a fabricated member that is in no gate and asserts it comes back as
  missing — so the parity check can never rot into a no-op that passes because
  it compares two empty sets.
"""

from __future__ import annotations

import json
import re
import tomllib
import unittest
from collections.abc import Iterable
from pathlib import Path

# tests/<name>.py -> apps/alphalens-research/tests; repo root is 3 up.
REPO_ROOT = Path(__file__).resolve().parents[3]
PYPROJECT = REPO_ROOT / "pyproject.toml"
PYRIGHT_CONFIG = REPO_ROOT / "pyrightconfig.json"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
SONAR_PROPERTIES = REPO_ROOT / "sonar-project.properties"

# A fabricated member used only by the positive control. Kept module-level so
# the "is this name actually free?" assertion and the control read the same one.
GHOST_MEMBER = "apps/alphalens-ghost"

# Each gate's command as authored in ci.yml. ``[^\n]*`` stops at the end of the
# `run:` line — every one of these is a single-line command today, and a future
# multi-line rewrite must show up here as a parse failure rather than as a
# silently shorter path list (see test_every_gate_parses_to_a_nonempty_path_list).
_GATE_COMMAND_RE = {
    "ruff check": re.compile(r"uv run ruff check ([^\n]*)"),
    "ruff format --check": re.compile(r"uv run ruff format --check ([^\n]*)"),
    "bandit -r": re.compile(r"uv run bandit -r ([^\n]*)"),
}


def _workspace_members() -> set[str]:
    """Every uv workspace member path, exactly as the root pyproject declares it.

    This is the truth set: a member is a Python package the repo builds and
    ships, so a gate that does not cover it is a gate with a hole.
    """
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    members = data["tool"]["uv"]["workspace"]["members"]
    return {m.rstrip("/") for m in members}


def _pyright_include() -> list[str]:
    """The ``include`` array from pyrightconfig.json.

    The file is JSONC (pyright allows ``//`` line comments); stdlib json is
    strict, so drop comment lines first — same approach as test_pyright_config.
    """
    raw = PYRIGHT_CONFIG.read_text(encoding="utf-8")
    clean = "\n".join(line for line in raw.splitlines() if not line.lstrip().startswith("//"))
    return list(json.loads(clean).get("include", []))


def _ci_gate_paths(gate: str) -> list[str]:
    """Union of the ``apps/...`` paths a gate is pointed at across ci.yml.

    Union, not per-invocation: the Django app is linted in its own CI job, so
    the same gate legitimately appears on more than one command line.
    """
    pattern = _GATE_COMMAND_RE[gate]
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    matches = pattern.findall(text)
    if not matches:
        raise AssertionError(
            f"Could not find a `uv run {gate} ...` invocation in "
            f"{CI_WORKFLOW.relative_to(REPO_ROOT)}. The gate's step changed shape — "
            f"update _GATE_COMMAND_RE in this test."
        )
    return [token for line in matches for token in line.split() if token.startswith("apps/")]


def _sonar_sources() -> list[str]:
    """The ``sonar.sources`` path list from sonar-project.properties.

    A member outside this list is invisible to SonarCloud entirely — no bugs,
    no smells, no hotspots, no coverage display — which was the one claim of
    issue #1142 that survived being executed (the coverage.xml and diff-cover
    claims did not: the CI coverage run is repo-root-cwd and unscoped, so it
    already measures every executed first-party file).
    """
    for line in SONAR_PROPERTIES.read_text(encoding="utf-8").splitlines():
        if line.startswith("sonar.sources="):
            return [tok.strip() for tok in line.split("=", 1)[1].split(",") if tok.strip()]
    raise AssertionError(
        f"Could not find a `sonar.sources=` line in {SONAR_PROPERTIES.name} — "
        "the property moved or was renamed; update _sonar_sources in this test."
    )


def _members_missing_from(members: Iterable[str], gate_paths: Iterable[str]) -> set[str]:
    """Pure helper: members not covered by any of ``gate_paths``.

    A member counts as covered when a gate path IS the member directory or sits
    underneath it — ``apps/alphalens-django/briefs`` covers
    ``apps/alphalens-django``. Trailing slashes are stripped because the CI
    command lines carry them and the config entries do not.
    """
    normalized = [p.rstrip("/") for p in gate_paths]
    return {
        member
        for member in members
        if not any(p == member or p.startswith(f"{member}/") for p in normalized)
    }


class TestCiGateWorkspaceParity(unittest.TestCase):
    """Every workspace member must be inside the type gate and the lint gates."""

    def test_workspace_members_parse_to_a_nonempty_set(self) -> None:
        # An empty truth set would make every parity assertion below pass
        # vacuously, which is exactly the failure mode this file exists to end.
        members = _workspace_members()
        self.assertGreater(
            len(members),
            0,
            f"Parsed no [tool.uv.workspace] members from {PYPROJECT.name} — layout changed.",
        )

    def test_every_gate_parses_to_a_nonempty_path_list(self) -> None:
        # Guards the regexes themselves: a stale pattern that matches nothing
        # raises, and one that matches a shorter line would show up as an empty
        # path list rather than as a parity failure naming the right cause.
        for gate in _GATE_COMMAND_RE:
            with self.subTest(gate=gate):
                self.assertGreater(len(_ci_gate_paths(gate)), 0)

    def test_every_member_is_inside_the_pyright_include(self) -> None:
        members = _workspace_members()
        include = _pyright_include()
        missing = _members_missing_from(members, include)
        self.assertEqual(
            missing,
            set(),
            f"Workspace member(s) outside the pyright type gate: {sorted(missing)}. "
            f"Add them to `include` in pyrightconfig.json — and to every "
            f"executionEnvironments extraPaths, or pyright will still fail to "
            f"resolve them at their consumers. Current include: {include}.",
        )

    def test_every_member_is_inside_every_ci_lint_gate(self) -> None:
        members = _workspace_members()
        for gate in _GATE_COMMAND_RE:
            with self.subTest(gate=gate):
                paths = _ci_gate_paths(gate)
                missing = _members_missing_from(members, paths)
                self.assertEqual(
                    missing,
                    set(),
                    f"Workspace member(s) outside `uv run {gate}`: {sorted(missing)}. "
                    f"Add them to that step in .github/workflows/ci.yml. "
                    f"Current paths: {paths}.",
                )

    def test_sonar_sources_parse_to_a_nonempty_list(self) -> None:
        # Same vacuity guard as the CI gates: a moved/renamed property must
        # fail loudly here, never make the parity below vacuously pass.
        self.assertGreater(len(_sonar_sources()), 0)

    def test_every_member_is_inside_sonar_sources(self) -> None:
        members = _workspace_members()
        sources = _sonar_sources()
        missing = _members_missing_from(members, sources)
        self.assertEqual(
            missing,
            set(),
            f"Workspace member(s) invisible to SonarCloud: {sorted(missing)}. "
            f"Add them to `sonar.sources` in sonar-project.properties — a member "
            f"outside it gets no analysis and no coverage display at all. "
            f"Current sources: {sources}.",
        )

    def test_positive_control_ghost_member_would_fail(self) -> None:
        # MANDATORY positive control: a member no gate names MUST come back as
        # missing. If this ever passes, the helper has rotted into a no-op and
        # every assertion above is decoration.
        include = _pyright_include()
        self.assertNotIn(
            GHOST_MEMBER,
            [p.rstrip("/") for p in include],
            f"Fixture invalid: a real package is literally at {GHOST_MEMBER}.",
        )
        for gate in _GATE_COMMAND_RE:
            with self.subTest(gate=gate):
                missing = _members_missing_from({GHOST_MEMBER}, _ci_gate_paths(gate))
                self.assertEqual(
                    missing,
                    {GHOST_MEMBER},
                    "Positive control failed: the parity helper did not flag a "
                    "fabricated member missing from the gate.",
                )
        self.assertEqual(_members_missing_from({GHOST_MEMBER}, include), {GHOST_MEMBER})
        self.assertEqual(_members_missing_from({GHOST_MEMBER}, _sonar_sources()), {GHOST_MEMBER})


if __name__ == "__main__":
    unittest.main()
