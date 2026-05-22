"""Enforce that every layer/screener package declares an explicit lifecycle status.

Status markers (`__status__`, optional `__closed_date__`, `__closed_reason__`)
let a reader see at-a-glance which warstwa is live, archived, or research-only
without grepping CLAUDE.md and memory. The test is also the gate that prevents
adding a new layer silently — it must be added to LAYERS_WITH_STATUS below.
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
REPO_ROOT = Path(__file__).resolve().parent.parent

LAYERS_WITH_STATUS = (
    "alphalens.data.alt_data",
    "alphalens.data.store",
    "alphalens.data.universes",
    "alphalens.backtest",
    "alphalens.attribution",
    "alphalens.diagnostics",
    "alphalens.data.fundamentals",
    "alphalens.literature_review",
    "alphalens.data.macro",
    "alphalens.gates",
    "alphalens.overlays",
    "alphalens.paper_trade",
    "alphalens.screeners.alt_data",
    "alphalens.screeners.compound_insider_pc",
    "alphalens.screeners.distress_credit",
    "alphalens.screeners.ev_fcff_yield",
    "alphalens.screeners.event_drift",
    "alphalens.screeners.insider_activity",
    "alphalens.screeners.momentum_lowvol",
    "alphalens.screeners.multi_source_two_stage",
    "alphalens.screeners.options_implied",
    "alphalens.screeners.options_volume",
    "alphalens.screeners.prescreener",
    "alphalens.thematic",
    "alphalens.watchdog",
)


class TestLayerStatus(unittest.TestCase):
    def test_each_layer_declares_status(self):
        missing = []
        for pkg in LAYERS_WITH_STATUS:
            module = importlib.import_module(pkg)
            if not hasattr(module, "__status__"):
                missing.append(pkg)
        self.assertEqual(missing, [], f"packages missing __status__: {missing}")

    def test_status_value_in_allowlist(self):
        invalid = []
        for pkg in LAYERS_WITH_STATUS:
            module = importlib.import_module(pkg)
            status = getattr(module, "__status__", None)
            if status not in VALID_STATUSES:
                invalid.append((pkg, status))
        self.assertEqual(invalid, [], f"invalid __status__ values: {invalid}")

    def test_closed_layers_have_reason(self):
        """CLOSED and ARCHIVED layers should explain why — easy for future reader to act on."""
        missing_reason = []
        for pkg in LAYERS_WITH_STATUS:
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
        for pkg in LAYERS_WITH_STATUS:
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
