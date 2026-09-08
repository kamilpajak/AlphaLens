"""Run the ExecStopPost metrics hook as a subprocess and pin its routing.

The hook writes ``alphalens_job_<job>.prom`` to ``ALPHALENS_TEXTFILE_DIR``, or
to ``$HOME/.alphalens/metrics`` when the variable is unset. That fallback is
what node_exporter never scrapes, and for two months it swallowed every
edge-mirror metric without a trace (#1366). The hook now says so on stderr —
which a systemd unit forwards to its journal — whenever it falls back.

The hook tolerates a missing/unusable ``systemctl`` (``|| true``) and a
non-GNU ``date -d`` (duration falls back to 0), so it runs unchanged on macOS
and in CI. Nothing here asserts the duration.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
HOOK = REPO_ROOT / "deploy" / "systemd" / "bin" / "alphalens-emit-job-metrics"
FALLBACK_WARNING = "ALPHALENS_TEXTFILE_DIR unset"


def _run_hook(
    job: str, *, home: Path, textfile_dir: Path | None
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        # No SERVICE_RESULT / EXIT_STATUS: the operator-by-hand shape.
    }
    if textfile_dir is not None:
        env["ALPHALENS_TEXTFILE_DIR"] = str(textfile_dir)
    return subprocess.run(
        ["bash", str(HOOK), job],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


class TestEmitJobMetricsHookRouting(unittest.TestCase):
    def test_unset_textfile_dir_falls_back_loudly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            result = _run_hook("probe-job", home=home, textfile_dir=None)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(FALLBACK_WARNING, result.stderr)
            self.assertTrue(
                (home / ".alphalens" / "metrics" / "alphalens_job_probe-job.prom").is_file()
            )

    def test_routed_textfile_dir_is_used_and_silent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            routed = Path(tmp) / "textfile"
            result = _run_hook("probe-job", home=home, textfile_dir=routed)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn(FALLBACK_WARNING, result.stderr)
            out = routed / "alphalens_job_probe-job.prom"
            self.assertTrue(out.is_file())
            self.assertFalse((home / ".alphalens").exists())
            body = out.read_text()
            self.assertIn('alphalens_job_last_run_timestamp_seconds{job="probe-job"}', body)
            # No systemd env -> SERVICE_RESULT "unknown" -> no success line.
            self.assertNotIn("last_success", body)


if __name__ == "__main__":
    unittest.main()
