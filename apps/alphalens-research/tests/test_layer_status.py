"""Enforce that every layer/screener package declares an explicit lifecycle status.

Status markers (`__status__`, optional `__closed_date__`, `__closed_reason__`)
let a reader see at-a-glance which warstwa is live, archived, or research-only
without grepping CLAUDE.md and memory. The test is also the gate that prevents
adding a new layer silently.

Discovery is automatic (no hand-maintained tuple to rot): every ``__init__.py``
under the research layer roots (:data:`LAYER_ROOTS`) MUST declare ``__status__``,
except the small :data:`NAMESPACE_ONLY_ALLOWLIST` of genuine namespace packages.
Any other package across the workspace that already declares ``__status__`` is
also picked up and validated, so a status marker can never quietly fall out of
coverage. Adding a CLOSED/ARCHIVED package without a 7-gate evidence map, or a
new screener without a status, fails this test the moment the package lands.
"""

from __future__ import annotations

import importlib
import unittest
from pathlib import Path

VALID_STATUSES = {"ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"}

REQUIRED_EVIDENCE_KEYS = frozenset(
    {
        "carhart_4f_hac",
        "sanity_checks_4gate",
        "walk_forward_oos",
        "multiple_testing_correction",
        "cost_drag",
        "bootstrap_ci",
        "survivorship_pit",
    }
)
EVIDENCE_PREFIXES = ("N/A: ", "UNTESTED: ")
REPO_ROOT = Path(__file__).resolve().parents[3]

# Top-level package name -> on-disk source root. Used both to rglob the layer
# roots and to translate any discovered __init__.py path back into a dotted
# module name for importlib.
PACKAGE_ROOTS: dict[str, Path] = {
    "alphalens_research": REPO_ROOT / "apps" / "alphalens-research" / "alphalens_research",
    "alphalens_pipeline": REPO_ROOT / "apps" / "alphalens-pipeline" / "alphalens_pipeline",
}

# Research-side layer roots that MUST carry a status on every package under them.
# (Pipeline-side packages are still validated when they declare __status__, but
# their status is not *required* here — those layers live on the infra side of
# the ADR 0011 split and are documented in CLAUDE.md, not gated as warstwy.)
LAYER_ROOTS = (
    PACKAGE_ROOTS["alphalens_research"] / "screeners",
    PACKAGE_ROOTS["alphalens_research"] / "gates",
    PACKAGE_ROOTS["alphalens_research"] / "backtest",
    PACKAGE_ROOTS["alphalens_research"] / "overlays",
    PACKAGE_ROOTS["alphalens_research"] / "attribution",
    PACKAGE_ROOTS["alphalens_research"] / "preaudit",
    PACKAGE_ROOTS["alphalens_research"] / "diagnostics",
    PACKAGE_ROOTS["alphalens_research"] / "retrospective_audit",
)

# Genuine namespace-only packages under the layer roots: they hold sub-packages
# but no layer of their own, so they legitimately carry no __status__. Kept as a
# tiny explicit allowlist so that a real layer cannot hide here by accident.
NAMESPACE_ONLY_ALLOWLIST = frozenset(
    {
        "alphalens_research.screeners",
    }
)


def _module_name(init_path: Path) -> str | None:
    """Translate an ``__init__.py`` path into its dotted package name.

    Returns None if the file is not under one of the known package roots.
    """
    for top, root in PACKAGE_ROOTS.items():
        try:
            rel = init_path.parent.relative_to(root)
        except ValueError:
            continue
        parts = (top, *rel.parts)
        return ".".join(parts)
    return None


def _declares_status(init_path: Path) -> bool:
    """True if the package assigns ``__status__`` (not just mentions it in prose).

    Cheap static check on source text so we don't have to import every package
    just to discover the universe; ``importlib`` is used afterwards for the
    declared set.
    """
    text = init_path.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("__status__") and ("=" in stripped):
            # Guard against a type-annotation-only line with no assignment.
            head = stripped.split("=", 1)[0]
            if "__status__" in head:
                return True
    return False


def _discover_packages() -> tuple[list[str], list[str]]:
    """Return (required, discovered_with_status).

    ``required`` — every package under LAYER_ROOTS, minus the namespace
    allowlist; each MUST declare __status__ (the missing ones are exactly what
    ``test_each_layer_declares_status`` reports).

    ``discovered_with_status`` — every package anywhere under PACKAGE_ROOTS that
    actually assigns __status__. This is the set the value / reason / evidence
    checks iterate, so a status marker can never escape validation.
    """
    required: set[str] = set()
    for root in LAYER_ROOTS:
        for init_path in root.rglob("__init__.py"):
            name = _module_name(init_path)
            if name is None or name in NAMESPACE_ONLY_ALLOWLIST:
                continue
            required.add(name)

    with_status: set[str] = set()
    for root in PACKAGE_ROOTS.values():
        for init_path in root.rglob("__init__.py"):
            if not _declares_status(init_path):
                continue
            name = _module_name(init_path)
            if name is not None:
                with_status.add(name)

    return sorted(required), sorted(with_status)


REQUIRED_PACKAGES, PACKAGES_WITH_STATUS = _discover_packages()


class TestLayerStatus(unittest.TestCase):
    def test_discovery_is_non_empty(self):
        """Positive control: the rglob must actually find packages.

        If LAYER_ROOTS rotted (renamed dir, moved tree), the discovery would
        return empty and every iterate-and-assert test below would pass
        vacuously. Pin a floor so that failure mode is loud.
        """
        self.assertGreater(
            len(REQUIRED_PACKAGES), 10, f"layer discovery looks broken: {REQUIRED_PACKAGES}"
        )
        self.assertGreater(
            len(PACKAGES_WITH_STATUS),
            len(REQUIRED_PACKAGES),
            "expected more status-declaring packages than required layer roots "
            "(pipeline-side layers also declare __status__)",
        )

    def test_each_layer_declares_status(self):
        missing = []
        for pkg in REQUIRED_PACKAGES:
            module = importlib.import_module(pkg)
            if not hasattr(module, "__status__"):
                missing.append(pkg)
        self.assertEqual(missing, [], f"packages missing __status__: {missing}")

    def test_status_value_in_allowlist(self):
        invalid = []
        for pkg in PACKAGES_WITH_STATUS:
            module = importlib.import_module(pkg)
            status = getattr(module, "__status__", None)
            if status not in VALID_STATUSES:
                invalid.append((pkg, status))
        self.assertEqual(invalid, [], f"invalid __status__ values: {invalid}")

    def test_closed_layers_have_reason(self):
        """CLOSED and ARCHIVED layers should explain why — easy for future reader to act on."""
        missing_reason = []
        for pkg in PACKAGES_WITH_STATUS:
            module = importlib.import_module(pkg)
            status = getattr(module, "__status__", None)
            if status in {"CLOSED", "ARCHIVED"} and not getattr(module, "__closed_reason__", None):
                missing_reason.append(pkg)
        self.assertEqual(
            missing_reason,
            [],
            f"CLOSED/ARCHIVED packages missing __closed_reason__: {missing_reason}",
        )

    def test_closed_layers_have_evidence(self):
        """CLOSED/ARCHIVED layers must publish a structured 7-gate evidence map.

        See docs/research/kill_verdict_checklist.md. Each value must be one of:
          - a path ending in .md that resolves under repo root, OR
          - a string starting with "N/A: " (gate doesn't apply) with non-empty
            justification, OR
          - a string starting with "UNTESTED: " (gate applies but consciously
            not run) with non-empty justification.
        """
        errors: list[str] = []
        for pkg in PACKAGES_WITH_STATUS:
            module = importlib.import_module(pkg)
            status = getattr(module, "__status__", None)
            if status not in {"CLOSED", "ARCHIVED"}:
                continue

            evidence = getattr(module, "__closed_evidence__", None)
            if evidence is None:
                errors.append(f"{pkg}: missing __closed_evidence__")
                continue
            if not isinstance(evidence, dict):
                errors.append(
                    f"{pkg}: __closed_evidence__ must be a dict, got {type(evidence).__name__}"
                )
                continue

            keys = set(evidence.keys())
            if keys != REQUIRED_EVIDENCE_KEYS:
                missing = REQUIRED_EVIDENCE_KEYS - keys
                extra = keys - REQUIRED_EVIDENCE_KEYS
                errors.append(
                    f"{pkg}: key mismatch — missing={sorted(missing)} extra={sorted(extra)}"
                )
                continue

            for key, value in evidence.items():
                if not isinstance(value, str) or not value:
                    errors.append(f"{pkg}.{key}: value must be a non-empty string")
                    continue
                if value.startswith(EVIDENCE_PREFIXES):
                    prefix = next(p for p in EVIDENCE_PREFIXES if value.startswith(p))
                    justification = value[len(prefix) :].strip()
                    if len(justification) < 3:
                        errors.append(f"{pkg}.{key}: '{prefix}' marker missing justification")
                    continue
                if not value.endswith(".md"):
                    errors.append(
                        f"{pkg}.{key}: value must be a .md path or start with one of {EVIDENCE_PREFIXES}; got {value!r}"
                    )
                    continue
                if not (REPO_ROOT / value).is_file():
                    errors.append(
                        f"{pkg}.{key}: evidence path does not point to an existing file: {value}"
                    )

        self.assertEqual(errors, [], "evidence violations:\n  " + "\n  ".join(errors))


if __name__ == "__main__":
    unittest.main()
