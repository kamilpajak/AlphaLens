"""Run the ExecStopPost metrics hook as a subprocess and pin what it emits.

The hook writes ``alphalens_job_<job>.prom`` to ``ALPHALENS_TEXTFILE_DIR``, or
to ``$HOME/.alphalens/metrics`` when the variable is unset. That fallback is
what node_exporter never scrapes, and for two months it swallowed every
edge-mirror metric without a trace (#1366). The hook says so on stderr —
which a systemd unit forwards to its journal — whenever it falls back.

The hook tolerates a missing/unusable ``systemctl`` (``|| true``) and a
non-GNU ``date -d`` (duration falls back to 0), so it runs unchanged on macOS
and in CI. Nothing here asserts the duration: controlling the timestamp would
need a fake ``systemctl`` on ``PATH``, a pattern this repo does not have.

What IS asserted here is the value of every emitted line. A value node_exporter
cannot parse costs the WHOLE file — every series in it disappears — and the
hook used to interpolate ``$EXIT_STATUS`` raw, which systemd sets to the signal
NAME (``TERM``) on a signal kill.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
HOOK = REPO_ROOT / "deploy" / "systemd" / "bin" / "alphalens-emit-job-metrics"
FALLBACK_WARNING = "ALPHALENS_TEXTFILE_DIR unset"

# The value the hook emits when systemd gave it no usable numeric status.
# Deliberately OUTSIDE the 0-255 range a real wait status can occupy, so it can
# never be confused with an exit code a job actually returned, while still
# being >= 1 so `!= 0` alerting and the >= 1 dashboard threshold both fire.
NO_STATUS_SENTINEL = "256"

_SAMPLE_LINE = re.compile(r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)\{[^}]*\}\s+(?P<value>\S+)$")


def _run_hook(
    job: str,
    *,
    home: Path,
    textfile_dir: Path | None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        # No SERVICE_RESULT / EXIT_STATUS by default: the operator-by-hand shape.
    }
    if textfile_dir is not None:
        env["ALPHALENS_TEXTFILE_DIR"] = str(textfile_dir)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(HOOK), job],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _samples(body: str) -> dict[str, str]:
    """Every ``name{labels} value`` line, as ``{metric name: value}``."""
    out: dict[str, str] = {}
    for line in body.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        match = _SAMPLE_LINE.match(line.strip())
        if match:
            out[match.group("name")] = match.group("value")
    return out


def _every_line_is_well_formed(body: str) -> None:
    """Raise unless EVERY non-comment line is a sample with a float value.

    :func:`_samples` skips what it cannot match, so asserting over its output
    alone proves "every line that already looks like a sample carries a number"
    — not "node_exporter will accept this file". A line with no label braces, or
    with no value at all, would be dropped silently and the assertion would
    still pass. node_exporter rejects the WHOLE file on any bad sample, so the
    check has to walk the raw text.
    """
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _SAMPLE_LINE.match(line)
        if match is None:
            raise AssertionError(f"line is not a valid sample: {line!r}")
        try:
            float(match.group("value"))
        except ValueError as exc:
            raise AssertionError(f"value is not a float: {line!r}") from exc


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


class _HookCase(unittest.TestCase):
    """Shared fixture: one tmpdir, one routed textfile dir, one job name."""

    job = "probe-job"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.routed = Path(self._tmp.name) / "textfile"
        self.out = self.routed / f"alphalens_job_{self.job}.prom"

    def fire(self, **env: str) -> str:
        result = _run_hook(
            self.job, home=self.home, textfile_dir=self.routed, extra_env=env or None
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.out.read_text()


class TestExitCodeIsAlwaysANumber(_HookCase):
    """systemd sets ``$EXIT_STATUS`` to the SIGNAL NAME when it kills a unit.

    `systemd.exec(5)`: ``$EXIT_CODE`` is ``"exited"`` "and the signal name in
    all other cases". Interpolating that raw produced
    ``alphalens_job_last_exit_code{job="feedback-shadow-returns"} TERM`` on
    2026-09-13, node_exporter rejected the whole file
    (``node_textfile_scrape_error 1``), and `AlphalensJobFailed` — whose
    expression is ``alphalens_job_last_exit_code != 0`` — could not fire for a
    job killed on its timeout.
    """

    def test_signal_kill_emits_the_sentinel_not_the_signal_name(self) -> None:
        body = self.fire(SERVICE_RESULT="timeout", EXIT_CODE="killed", EXIT_STATUS="TERM")
        self.assertEqual(_samples(body)["alphalens_job_last_exit_code"], NO_STATUS_SENTINEL)
        self.assertNotIn("TERM", body)

    def test_a_numeric_status_is_passed_through_unchanged(self) -> None:
        body = self.fire(SERVICE_RESULT="exit-code", EXIT_CODE="exited", EXIT_STATUS="1")
        self.assertEqual(_samples(body)["alphalens_job_last_exit_code"], "1")

    def test_failure_with_no_status_at_all_is_not_reported_as_zero(self) -> None:
        """`systemd.exec(5)`: the variables "are only set if the service manager
        succeeded to start and identify the main process". Table 6 has rows
        (``protocol``, ``start-limit-hit``) where BOTH are unset. The old
        ``${EXIT_STATUS:-0}`` turned those into a parseable claim of success.
        """
        body = self.fire(SERVICE_RESULT="protocol")
        self.assertEqual(_samples(body)["alphalens_job_last_exit_code"], NO_STATUS_SENTINEL)

    def test_every_emitted_line_is_a_sample_node_exporter_would_accept(self) -> None:
        body = self.fire(SERVICE_RESULT="timeout", EXIT_CODE="killed", EXIT_STATUS="TERM")
        self.assertTrue(_samples(body), f"no samples parsed out of:\n{body}")
        _every_line_is_well_formed(body)

    def test_the_well_formed_check_can_actually_refute(self) -> None:
        """Positive control. Without it, the check above could rot into a
        no-op and nothing would notice — the failure mode it guards against is
        precisely a line that never reaches the assertion.
        """
        for bad in (
            'alphalens_job_last_exit_code{job="x"} TERM',  # the 2026-09-13 regression
            "alphalens_job_last_exit_code 123",  # no label braces
            'alphalens_job_last_exit_code{job="x"}',  # no value
            "garbage",
        ):
            with self.subTest(line=bad), self.assertRaises(AssertionError):
                _every_line_is_well_formed(bad)


class TestOperatorStopIsNotAFailure(_HookCase):
    """`systemd.exec(5)` Table 6, first row: ``"success" : "killed" : "HUP",
    "INT", "TERM", "PIPE"``. A plain ``systemctl stop`` of a running oneshot
    therefore reports SUCCESS together with ``EXIT_STATUS=TERM``. Branching on
    ``$EXIT_CODE`` alone would page a critical alert for that.
    """

    def test_success_killed_by_term_reports_zero_and_keeps_the_success_line(self) -> None:
        body = self.fire(SERVICE_RESULT="success", EXIT_CODE="killed", EXIT_STATUS="TERM")
        samples = _samples(body)
        self.assertEqual(samples["alphalens_job_last_exit_code"], "0")
        self.assertIn("alphalens_job_last_success_timestamp_seconds", samples)


class TestSignalIsExposedSeparately(_HookCase):
    """The signal is worth keeping, but not inside ``last_exit_code``: three
    ``code=exited, status=128`` runs appear in 30 days of the host journal, and
    four hooked units shell out to ``docker run``, whose client reports a killed
    container as ``code=exited, status=137``. A ``128+signum`` encoding would be
    indistinguishable from those.
    """

    def test_signal_number_is_its_own_gauge(self) -> None:
        body = self.fire(SERVICE_RESULT="timeout", EXIT_CODE="killed", EXIT_STATUS="TERM")
        self.assertEqual(_samples(body)["alphalens_job_last_signal"], "15")

    def test_the_signal_gauge_is_zero_when_no_signal_was_involved(self) -> None:
        """Emitted on EVERY run, zeros included. A series that disappears on
        healthy runs is indistinguishable from a stopped exporter — the same
        mistake as #1369."""
        body = self.fire(SERVICE_RESULT="exit-code", EXIT_CODE="exited", EXIT_STATUS="1")
        self.assertEqual(_samples(body)["alphalens_job_last_signal"], "0")


class TestLastSuccessSurvivesAFailedRun(_HookCase):
    """#1369: the hook rewrites the whole file and emitted
    ``last_success_timestamp_seconds`` only on success, so one failed run
    deleted the series — and ``AlphalensJobStale``
    (``time() - max(last_success{job}) > threshold``) cannot fire on an empty
    ``max()``. The staleness alarm was disarmed exactly while a job was broken.
    """

    def test_a_failed_run_carries_the_previous_success_forward_unchanged(self) -> None:
        first = self.fire(SERVICE_RESULT="success", EXIT_CODE="exited", EXIT_STATUS="0")
        t1 = _samples(first)["alphalens_job_last_success_timestamp_seconds"]

        second = self.fire(SERVICE_RESULT="exit-code", EXIT_CODE="exited", EXIT_STATUS="1")
        samples = _samples(second)
        self.assertEqual(samples["alphalens_job_last_exit_code"], "1")
        # Equality, not presence: re-emitting $NOW here would pass a
        # presence-only check while reintroducing the bug it claims to fix.
        self.assertEqual(samples["alphalens_job_last_success_timestamp_seconds"], t1)

    def test_a_first_ever_failure_emits_no_success_line_and_still_exits_zero(self) -> None:
        """The carry-forward reads the previous file. Under ``set -euo
        pipefail`` a bare ``grep`` with no match exits 1 and aborts the hook
        before ``mv`` — leaving a stale file, a stray ``.tmp`` and a failed
        ``ExecStopPost`` — in exactly the case this fix targets.
        """
        body = self.fire(SERVICE_RESULT="exit-code", EXIT_CODE="exited", EXIT_STATUS="1")
        self.assertNotIn("last_success", body)
        self.assertFalse(
            list(self.routed.glob("*.tmp")), "the atomic-write tempfile was left behind"
        )


if __name__ == "__main__":
    unittest.main()
