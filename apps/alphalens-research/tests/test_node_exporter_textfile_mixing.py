"""What node_exporter actually SCRAPES from a directory the job-metrics hook
writes into — run against the real exporter, not a reading of its source.

Opt-in (needs Docker):

    NODE_EXPORTER_DOCKER_TEST=1 .venv/bin/python -m unittest \
        tests.test_node_exporter_textfile_mixing -v

The CI research job runs it with the flag set (pinned by
``test_promtool_lint_parity.py``).

Why it exists (#1461): the plan to drop ``# HELP`` / ``# TYPE`` from the hook
was argued from ``collector/textfile.go`` alone — "a family without a help line
is never compared, so old HELP-bearing files and new helpless ones coexist".
The collector does pass both through, but the client_golang REGISTRY behind
it then refuses every sample whose help text or type differs from the first
family it saw under that name (``error gathering metrics ... has help ... but
should have ...``), and ``node_textfile_scrape_error`` stays 0. A mixed
directory therefore silently loses whichever style sorts second, per name.
That is the observation that made the rollout a one-shot migration (strip the
comment lines from every existing file in the same step as the hook deploy)
instead of "nothing to do", and this test keeps it from being re-argued.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

from tests.test_emit_job_metrics_hook import _run_hook

_ENABLED = os.environ.get("NODE_EXPORTER_DOCKER_TEST") == "1"
# The VPS runs prom/node-exporter:latest, which was 1.9.1 on 2026-09-15
# (node_exporter_build_info). Pin the version the claims were measured on.
NODE_EXPORTER_IMAGE = "prom/node-exporter:v1.9.1"
_READY_TIMEOUT_S = 20.0

# Exactly what the pre-#1461 hook wrote: all five families, each with its
# HELP/TYPE pair. A stale file on the VPS looks like this until its job runs.
OLD_STYLE_FILE = (
    "# HELP alphalens_job_last_run_timestamp_seconds Unix time of the last invocation (success or failure)\n"
    "# TYPE alphalens_job_last_run_timestamp_seconds gauge\n"
    'alphalens_job_last_run_timestamp_seconds{{job="{job}"}} 1700000000\n'
    "# HELP alphalens_job_last_duration_seconds Wall-clock seconds for the last invocation\n"
    "# TYPE alphalens_job_last_duration_seconds gauge\n"
    'alphalens_job_last_duration_seconds{{job="{job}"}} 12\n'
    "# HELP alphalens_job_last_exit_code Exit status of the last invocation (0 = success, 256 = systemd reported no numeric status)\n"
    "# TYPE alphalens_job_last_exit_code gauge\n"
    'alphalens_job_last_exit_code{{job="{job}"}} 0\n'
    "# HELP alphalens_job_last_signal Signal number that terminated the last invocation (0 = not signalled)\n"
    "# TYPE alphalens_job_last_signal gauge\n"
    'alphalens_job_last_signal{{job="{job}"}} 0\n'
    "# HELP alphalens_job_last_success_timestamp_seconds Unix time of the last successful invocation\n"
    "# TYPE alphalens_job_last_success_timestamp_seconds gauge\n"
    'alphalens_job_last_success_timestamp_seconds{{job="{job}"}} 1700000000\n'
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _scrape(textfile_dir: Path) -> tuple[str, str]:
    """Start the exporter on ``textfile_dir`` (textfile collector only), return
    (``/metrics`` body, container log)."""
    port = _free_port()
    container = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-d",
            "-p",
            f"127.0.0.1:{port}:9100",
            "-v",
            f"{textfile_dir}:/tf:ro",
            NODE_EXPORTER_IMAGE,
            "--collector.disable-defaults",
            "--collector.textfile",
            "--collector.textfile.directory=/tf",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    ).stdout.strip()
    try:
        deadline = time.monotonic() + _READY_TIMEOUT_S
        body = ""
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=2) as resp:
                    body = resp.read().decode()
                break
            except OSError:
                time.sleep(0.25)
        if not body:
            raise AssertionError("node_exporter did not answer /metrics in time")
        log = subprocess.run(
            ["docker", "logs", container], capture_output=True, text=True, timeout=30, check=False
        )
        return body, log.stdout + log.stderr
    finally:
        subprocess.run(["docker", "stop", container], capture_output=True, timeout=60, check=False)


def _sample_lines(body: str, job: str) -> list[str]:
    return [
        line
        for line in body.splitlines()
        if line.startswith("alphalens_job_") and f'job="{job}"' in line
    ]


@unittest.skipUnless(_ENABLED, "set NODE_EXPORTER_DOCKER_TEST=1 (needs Docker) to run")
@unittest.skipUnless(shutil.which("docker"), "docker not on PATH")
class TestWhatTheExporterScrapesFromHookOutput(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.textfile_dir = Path(self._tmp.name) / "textfile"
        self.textfile_dir.mkdir()

    def _hook(self, job: str) -> None:
        result = _run_hook(
            job,
            home=self.home,
            textfile_dir=self.textfile_dir,
            extra_env={"SERVICE_RESULT": "success", "EXIT_CODE": "exited", "EXIT_STATUS": "0"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_two_files_written_by_the_hook_are_both_scraped(self) -> None:
        """The steady state after the migration: helpless files only. Both
        jobs' samples come through, no gather error, scrape_error 0."""
        self._hook("aaa-first")
        self._hook("bbb-second")
        body, log = _scrape(self.textfile_dir)
        self.assertEqual(len(_sample_lines(body, "aaa-first")), 5, body)
        self.assertEqual(len(_sample_lines(body, "bbb-second")), 5, body)
        self.assertIn("node_textfile_scrape_error 0", body)
        self.assertNotIn("error gathering metrics", log)

    def test_a_help_bearing_file_and_a_hook_file_cannot_share_a_name(self) -> None:
        """The mixed state a partial rollout would produce. Whichever style
        sorts SECOND loses every sample under the shared names, the exporter
        logs a gather error, and scrape_error stays 0 — so neither
        AlphalensTextfileScrapeError nor the per-job family rule would see it;
        only AlphalensJobMetricMissing would, one job at a time. This is why
        the deploy strips the comment lines from every existing file in the
        same step as the hook change.
        """
        for old_job, hook_job, loser in (
            ("aaa-old", "bbb-hook", "bbb-hook"),
            ("bbb-old", "aaa-hook", "bbb-old"),
        ):
            with self.subTest(order=f"{old_job} then {hook_job}"):
                for stale in self.textfile_dir.glob("*.prom"):
                    stale.unlink()
                (self.textfile_dir / f"alphalens_job_{old_job}.prom").write_text(
                    OLD_STYLE_FILE.format(job=old_job)
                )
                self._hook(hook_job)
                body, log = _scrape(self.textfile_dir)
                winner = hook_job if loser == old_job else old_job
                self.assertEqual(len(_sample_lines(body, winner)), 5, body)
                self.assertEqual(_sample_lines(body, loser), [], body)
                self.assertIn("error gathering metrics", log)
                self.assertIn("node_textfile_scrape_error 0", body)


if __name__ == "__main__":
    unittest.main()
