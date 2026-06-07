"""Static lint of the Form-4 daily-incremental systemd service + timer.

Mirrors ``test_deploy_systemd_units.py::TestShadowReturnsUnit``: pins the
directives the cron-health + parity tests cannot infer — oneshot + working dir,
fail-loud EnvironmentFile (no leading dash), the ExecStart script + ``--lookback-days``
flag, the emit-hook job token (``form4-incremental``), and the timer's daily
02:30 UTC slot + ``Persistent=true`` + ``[Install]`` section.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SYSTEMD_DIR = REPO_ROOT / "deploy" / "systemd"
SERVICE_PATH = SYSTEMD_DIR / "alphalens-form4-incremental.service"
TIMER_PATH = SYSTEMD_DIR / "alphalens-form4-incremental.timer"


class TestForm4IncrementalService(unittest.TestCase):
    def setUp(self) -> None:
        self.text = SERVICE_PATH.read_text()

    def test_service_is_oneshot_with_working_dir(self) -> None:
        self.assertIn("Type=oneshot", self.text)
        self.assertIn("WorkingDirectory=%h/AlphaLens", self.text)

    def test_service_loads_etc_alphalens_env_fail_loud(self) -> None:
        # No leading dash: a typoed / missing /etc/alphalens/env must surface
        # as a unit failure (a missing SEC_EDGAR_USER_AGENT would otherwise
        # silently fall through to the canonical default UA).
        self.assertRegex(
            self.text,
            re.compile(r"^EnvironmentFile=/etc/alphalens/env\s*$", re.MULTILINE),
            "Must load /etc/alphalens/env without a leading dash (fail loud).",
        )

    def test_service_execstart_runs_incremental_script_with_lookback(self) -> None:
        # Tolerate `\`-continuations at each gap (the directive spans 3 lines).
        cont = r"(?:\s*\\\s*\n\s*|\s+)"
        self.assertRegex(
            self.text,
            re.compile(
                r"^ExecStart=%h/AlphaLens/\.venv/bin/python"
                + cont
                + r"apps/alphalens-research/scripts/run_form4_daily_incremental\.py"
                + cont
                + r"--lookback-days\s+3\b",
                re.MULTILINE,
            ),
            "ExecStart must run run_form4_daily_incremental.py with --lookback-days 3.",
        )

    def test_service_wires_emit_hook_with_own_job_name(self) -> None:
        self.assertRegex(
            self.text,
            re.compile(
                r"^ExecStopPost=%h/AlphaLens/deploy/systemd/bin/"
                r"alphalens-emit-job-metrics\s+form4-incremental\s*$",
                re.MULTILINE,
            ),
            "ExecStopPost must wire alphalens-emit-job-metrics form4-incremental.",
        )


class TestForm4IncrementalTimer(unittest.TestCase):
    def setUp(self) -> None:
        self.text = TIMER_PATH.read_text()

    def test_timer_fires_daily_at_0230_utc_persistent(self) -> None:
        self.assertRegex(
            self.text,
            re.compile(r"^OnCalendar=\*-\*-\* 02:30:00 UTC\s*$", re.MULTILINE),
            "Timer must fire daily at 02:30 UTC (staggered off AV 00:05 + thematic HH:30).",
        )
        self.assertRegex(self.text, re.compile(r"^Persistent=true\s*$", re.MULTILINE))

    def test_timer_carries_install_section(self) -> None:
        self.assertRegex(self.text, re.compile(r"^\[Install\]\s*$", re.MULTILINE))
        self.assertRegex(self.text, re.compile(r"^WantedBy=timers\.target\s*$", re.MULTILINE))


if __name__ == "__main__":
    unittest.main()
