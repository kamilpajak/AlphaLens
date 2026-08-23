"""Pin the promtool lint gate: CI job and the local recipe must not drift.

``deploy/monitoring/prometheus/rules/alphalens.yaml`` is loaded by a Prometheus
that only ever sees a hand-synced copy on the VPS, so a malformed PromQL
expression is not caught by anything downstream: the rules file is copied,
Prometheus is HUP-ed, and it REFUSES THE WHOLE FILE — all 35
``alphalens-cron-health`` rules stop evaluating at once. Nothing pages, because
the thing that would page is what just stopped. The blast radius is every job
staleness alert in the project, invisible until a real outage goes unreported.

``test_monitoring_alerts.py`` cannot catch this. It parses the file as YAML and
asserts on structure and substrings; ``expr`` is just a string to it. Only
``promtool check rules`` parses PromQL.

Two things therefore have to stay true, and this module pins both:

1. The CI job and the ``just lint-rules`` recipe run the SAME promtool version.
   A local green that CI cannot reproduce (or vice versa) is worse than no
   local recipe at all.
2. That version matches the Prometheus actually running on the VPS. Linting
   with a NEWER parser than the one that loads the file can accept syntax the
   live server rejects — the exact failure this gate exists to prevent, merely
   moved later. The expected version is a constant here because the VPS
   Prometheus is not machine-readable from the repo; when the server is
   upgraded, this test is the reminder to bump both call sites.

Positive control: a fabricated version mismatch must be detected, so the parity
check cannot rot into a no-op.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
JUSTFILE = REPO_ROOT / "justfile"
RULES = REPO_ROOT / "deploy" / "monitoring" / "prometheus" / "rules" / "alphalens.yaml"

# Prometheus running on the production VPS, read from its own buildinfo
# endpoint on 2026-07-31. Bump BOTH call sites when the server is upgraded.
EXPECTED_PROM_VERSION = "v3.3.1"

_IMAGE_RE = re.compile(r"prom/prometheus:(v[\d.]+)")


def _versions_in(path: Path) -> list[str]:
    return _IMAGE_RE.findall(path.read_text())


class TestPromtoolLintParity(unittest.TestCase):
    def test_ci_workflow_lints_the_rules_file(self) -> None:
        text = CI_WORKFLOW.read_text()
        self.assertIn(
            "promtool",
            text,
            "ci.yml no longer runs promtool — PromQL syntax is then unchecked before merge.",
        )
        self.assertIn(
            "deploy/monitoring/prometheus/rules",
            text,
            "the promtool CI step must lint the repo rules directory.",
        )

    def test_justfile_exposes_the_same_lint_locally(self) -> None:
        self.assertIn(
            "lint-rules",
            JUSTFILE.read_text(),
            "`just lint-rules` is how the gate is reproduced locally before pushing.",
        )

    def test_both_call_sites_pin_the_production_server_version(self) -> None:
        # Asserting each found version equals the constant also makes the two
        # call sites equal to each other, so a separate CI-vs-justfile parity
        # test would be redundant. The existence check is NOT redundant: with
        # no version found the equality loop below would pass vacuously, which
        # is exactly what an accidentally deleted pin looks like.
        #
        # Pinning to the SERVER version, not the newest, is deliberate: a newer
        # parser can accept expressions the live 3.3.1 rejects, which would let
        # the very failure this gate exists to prevent through to production.
        for path in (CI_WORKFLOW, JUSTFILE):
            found = _versions_in(path)
            self.assertTrue(
                found,
                f"no pinned prom/prometheus:<version> found in {path.name} — the "
                f"lint either vanished or went unpinned.",
            )
            for version in found:
                self.assertEqual(
                    version,
                    EXPECTED_PROM_VERSION,
                    f"{path.name} pins promtool {version} but the VPS Prometheus runs "
                    f"{EXPECTED_PROM_VERSION}. If the server was upgraded, bump "
                    f"EXPECTED_PROM_VERSION here and both call sites together.",
                )

    def test_version_mismatch_would_be_detected(self) -> None:
        # Positive control for the parity assertion above: prove the regex and
        # the comparison actually discriminate, so this file cannot silently
        # degrade into asserting nothing.
        fake_ci = "image: prom/prometheus:v3.3.1"
        fake_just = "image: prom/prometheus:v9.9.9"
        self.assertNotEqual(
            set(_IMAGE_RE.findall(fake_ci)),
            set(_IMAGE_RE.findall(fake_just)),
            "the version regex no longer discriminates — parity check is a no-op.",
        )

    def test_ci_and_justfile_evaluate_both_promtool_fixtures(self) -> None:
        # ci.yml hard-codes the fixture paths (rearm design memo §7.10): a new
        # fixture file that is not named in the promtool-test step is silently
        # never executed while CI stays green. Both call sites must run BOTH
        # fixtures — the 1h-grid thematic one and the 1m-grid broker-stream one
        # (a `for: 20m` vs `for: 10m` case is indistinguishable on a 1h grid,
        # which is why a second file exists at all).
        fixtures = ("alphalens_test.yaml", "alphalens_broker_test.yaml")
        rules_dir = RULES.parent
        for fixture in fixtures:
            self.assertTrue(
                (rules_dir / fixture).is_file(),
                f"promtool fixture {fixture} is missing from {rules_dir}.",
            )
        for path in (CI_WORKFLOW, JUSTFILE):
            text = path.read_text()
            for fixture in fixtures:
                self.assertIn(
                    fixture,
                    text,
                    f"{path.name} does not run promtool against {fixture} — "
                    "its cases are silently never executed.",
                )

    def test_rules_header_does_not_overstate_unit_test_coverage(self) -> None:
        # The header used to list what the unit tests check in a way that read
        # as full validation. Anyone trusting that list would not run promtool.
        header = RULES.read_text()[:2000]
        self.assertIn(
            "promtool",
            header,
            "the rules file header must point at the promtool gate, since the "
            "unit tests do not parse PromQL.",
        )
