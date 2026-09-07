"""Pin the Alertmanager Telegram render gate: CI job and local recipe must not drift.

``deploy/monitoring/alertmanager/config.yaml`` + ``telegram.tmpl`` are loaded by an
Alertmanager that only ever sees a hand-copied pair on the VPS. Two things can
go wrong there that no unit test in ``test_monitoring_alerts.py`` can see:

1. The config or template fails to LOAD (bad YAML key, a template that does
   not parse) — Alertmanager keeps the previous config on SIGHUP and the
   operator learns nothing until the next page is missing.
2. The template RENDERS text Telegram rejects. #1345: ``parse_mode: Markdown``
   turned every bare ``_`` in a description into an unterminated italic and
   Telegram answered "can't parse entities" — 7191 failed notify attempts,
   no page. Only a real render through the same Go html/template path the
   notifier uses (``amtool template render --template.type=html``) shows what
   Telegram will receive.

So CI runs ``amtool check-config`` and ``amtool template render`` against a
fixture whose annotations carry every character that bit (``_ < > & * `` and
backticks), and diffs the output against ``render_expected.txt``. This module
pins that both the CI step and the ``just lint-alertmanager`` recipe exist, run
the html render type (a ``text`` render would hide the escaping), diff against
the committed expectation, and use the SAME Alertmanager image version as the
VPS — a newer template engine could accept what the live one rejects.

Positive control: a fabricated version mismatch must be detected.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
JUSTFILE = REPO_ROOT / "justfile"
AM_DIR = REPO_ROOT / "deploy" / "monitoring" / "alertmanager"

# Alertmanager running on the production VPS (`alertmanager --version`, read
# 2026-09-06). Bump BOTH call sites when the container is upgraded.
EXPECTED_AM_VERSION = "v0.28.1"

_IMAGE_RE = re.compile(r"prom/alertmanager:(v[\d.]+)")
_CALL_SITES = ("ci.yml", "justfile")


def _versions_in(path: Path) -> list[str]:
    return _IMAGE_RE.findall(path.read_text())


class TestAmtoolRenderParity(unittest.TestCase):
    def test_fixture_and_expectation_exist(self) -> None:
        for name in ("render_fixture.json", "render_expected.txt", "telegram.tmpl"):
            self.assertTrue((AM_DIR / name).is_file(), f"{name} missing from {AM_DIR}.")

    def test_fixture_carries_every_character_that_broke_delivery(self) -> None:
        # A fixture without the offending characters renders clean under any
        # parse mode and proves nothing.
        fixture = (AM_DIR / "render_fixture.json").read_text()
        for char in ("_", "<", ">", "&", "*", "`"):
            self.assertIn(char, fixture, f"render_fixture.json must contain {char!r}.")

    def test_expectation_shows_html_escaping(self) -> None:
        expected = (AM_DIR / "render_expected.txt").read_text()
        for entity in ("&lt;", "&gt;", "&amp;"):
            self.assertIn(entity, expected, "expected rendering must show html/template escaping.")
        self.assertIn("node_exporter", expected, "a bare underscore must survive untouched.")

    def test_both_call_sites_run_check_config_and_html_render(self) -> None:
        for path in (CI_WORKFLOW, JUSTFILE):
            text = path.read_text()
            self.assertIn("amtool", text, f"{path.name} does not run amtool at all.")
            self.assertIn("check-config", text, f"{path.name} must lint the Alertmanager config.")
            self.assertIn(
                "template render", text, f"{path.name} must render the Telegram template."
            )
            self.assertIn(
                "--template.type=html",
                text,
                f"{path.name} renders as text — the html/template escaping that #1345 "
                "depends on is then invisible.",
            )
            self.assertIn("render_fixture.json", text, f"{path.name} must render the fixture.")
            self.assertIn(
                "render_expected.txt",
                text,
                f"{path.name} must diff the render against the committed expectation.",
            )

    def test_both_call_sites_pin_the_production_server_version(self) -> None:
        for path in (CI_WORKFLOW, JUSTFILE):
            found = _versions_in(path)
            self.assertTrue(
                found,
                f"no pinned prom/alertmanager:<version> found in {path.name} — the "
                "render gate either vanished or went unpinned.",
            )
            for version in found:
                self.assertEqual(
                    version,
                    EXPECTED_AM_VERSION,
                    f"{path.name} pins amtool {version} but the VPS Alertmanager runs "
                    f"{EXPECTED_AM_VERSION}. If the container was upgraded, bump "
                    "EXPECTED_AM_VERSION here and both call sites together.",
                )

    def test_version_mismatch_would_be_detected(self) -> None:
        fake_ci = "image: prom/alertmanager:v0.28.1"
        fake_just = "image: prom/alertmanager:v9.9.9"
        self.assertNotEqual(
            set(_IMAGE_RE.findall(fake_ci)),
            set(_IMAGE_RE.findall(fake_just)),
            "the version regex no longer discriminates — parity check is a no-op.",
        )
