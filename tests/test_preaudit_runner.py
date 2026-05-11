"""Unit tests for run_smoke — subprocess invocation + exit-code parsing.

Mocks ``subprocess.run`` so the tests never spawn the real experiment
script. The integration test (test_preaudit_integration.py) covers the
real-data end-to-end path.

Critical safety property exercised here: the runner MUST inject an
ephemeral ``--out`` path so a smoke invocation can never clobber a
concurrently-running audit's output artifact (zen 2026-05-11 catch).
"""

from __future__ import annotations

import subprocess
import unittest
from datetime import date
from unittest import mock

from alphalens.preaudit.profiles import (
    SMOKE_PROFILES,
    DataDep,
    SmokeProfile,
    SmokeStatus,
)
from alphalens.preaudit.runner import run_smoke


def _completed(returncode: int, stderr: str = "", stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["dummy"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _completed_writing_file(returncode: int, content: str = '{"smoke": "synth"}', stderr: str = ""):
    """subprocess.run side_effect that writes synthetic content to --out.

    Models a real experiment subprocess that successfully wrote its
    output artifact. Required for happy-path tests after the
    "exit=0 + non-empty output" validation gate (zen 2026-05-11 HIGH).
    """

    def _se(argv, *args, **kwargs):
        from pathlib import Path

        out_path = Path(argv[argv.index("--out") + 1])
        out_path.write_text(content)
        return subprocess.CompletedProcess(
            args=argv, returncode=returncode, stdout="", stderr=stderr
        )

    return _se


class TestRunSmokeUnknownStrategy(unittest.TestCase):
    def test_unknown_strategy_returns_unknown_status(self):
        result = run_smoke("__not_a_real_strategy__")
        self.assertEqual(result.status, SmokeStatus.UNKNOWN_STRATEGY)

    def test_strategy_in_scripts_but_no_profile_returns_no_profile(self):
        # tri_factor exists in _SCRIPTS but has no SmokeProfile today.
        result = run_smoke("tri_factor")
        self.assertEqual(result.status, SmokeStatus.NO_PROFILE)


class TestRunSmokeInvocation(unittest.TestCase):
    """Verify the subprocess invocation argv is shaped correctly."""

    def test_subprocess_called_with_python_and_script_path(self):
        with mock.patch(
            "alphalens.preaudit.runner.subprocess.run", return_value=_completed(0)
        ) as m:
            run_smoke("insider_pc_compound")
        argv = m.call_args.args[0]
        self.assertTrue(any("python" in a or "python3" in a for a in argv[:2]), argv)
        self.assertTrue(any("experiment_insider_pc_compound.py" in a for a in argv), argv)

    def test_subprocess_argv_includes_smoke_window(self):
        profile = SMOKE_PROFILES["insider_pc_compound"]
        with mock.patch(
            "alphalens.preaudit.runner.subprocess.run", return_value=_completed(0)
        ) as m:
            run_smoke("insider_pc_compound")
        argv = m.call_args.args[0]
        self.assertIn(profile.smoke_window[0].isoformat(), argv)
        self.assertIn(profile.smoke_window[1].isoformat(), argv)

    def test_subprocess_argv_includes_profile_extra_args(self):
        profile = SMOKE_PROFILES["insider_pc_compound"]
        with mock.patch(
            "alphalens.preaudit.runner.subprocess.run", return_value=_completed(0)
        ) as m:
            run_smoke("insider_pc_compound")
        argv = m.call_args.args[0]
        for arg in profile.extra_args:
            self.assertIn(arg, argv, f"missing {arg!r} in {argv}")

    def test_subprocess_argv_injects_ephemeral_out_path(self):
        """CRITICAL: --out path MUST be an ephemeral tempfile.

        Without this, smoke could silently overwrite a concurrent audit's
        output JSON. ``tempfile.mkstemp`` guarantees a unique path inside
        the system temp dir, with mode 0600.
        """
        import os
        import tempfile

        with mock.patch(
            "alphalens.preaudit.runner.subprocess.run", return_value=_completed(0)
        ) as m:
            run_smoke("insider_pc_compound")
        argv = m.call_args.args[0]
        self.assertIn("--out", argv)
        out_path = argv[argv.index("--out") + 1]
        self.assertTrue(
            os.path.basename(out_path).startswith("preaudit_smoke_"),
            f"unexpected ephemeral filename: {out_path}",
        )
        self.assertTrue(out_path.endswith(".json"), out_path)
        # Must live inside the OS temp dir (cross-platform), not in repo.
        self.assertTrue(
            out_path.startswith(tempfile.gettempdir()),
            f"ephemeral path {out_path} outside system temp {tempfile.gettempdir()}",
        )
        self.assertNotIn("docs/research", out_path)
        self.assertNotIn("/workspace/AlphaLens/docs", out_path)

    def test_two_invocations_get_distinct_out_paths(self):
        """tempfile.mkstemp guarantees distinct paths across parallel smokes."""
        seen: list[str] = []
        with mock.patch(
            "alphalens.preaudit.runner.subprocess.run", return_value=_completed(0)
        ) as m:
            run_smoke("insider_pc_compound")
            run_smoke("insider_pc_compound")
        argv1 = m.call_args_list[0].args[0]
        argv2 = m.call_args_list[1].args[0]
        out1 = argv1[argv1.index("--out") + 1]
        out2 = argv2[argv2.index("--out") + 1]
        seen.append(out1)
        seen.append(out2)
        self.assertNotEqual(out1, out2)


class TestRunSmokeOutcome(unittest.TestCase):
    def test_zero_exit_with_nonempty_output_returns_pass(self):
        with mock.patch(
            "alphalens.preaudit.runner.subprocess.run",
            side_effect=_completed_writing_file(0),
        ):
            result = run_smoke("insider_pc_compound")
        self.assertEqual(result.status, SmokeStatus.PASS)
        self.assertEqual(result.exit_code, 0)

    def test_zero_exit_with_empty_output_returns_fail(self):
        """Zen 2026-05-11 HIGH catch: an experiment that exits 0 but
        silently writes nothing should NOT pass smoke. mkstemp seeds
        an empty file; subprocess mock leaves it untouched."""
        with mock.patch("alphalens.preaudit.runner.subprocess.run", return_value=_completed(0)):
            result = run_smoke("insider_pc_compound")
        self.assertEqual(result.status, SmokeStatus.FAIL)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("did not write", result.detail)

    def test_nonzero_exit_returns_fail_with_stderr_tail(self):
        stderr = "traceback line 1\nProcessError: data missing"
        with mock.patch(
            "alphalens.preaudit.runner.subprocess.run", return_value=_completed(8, stderr=stderr)
        ):
            result = run_smoke("insider_pc_compound")
        self.assertEqual(result.status, SmokeStatus.FAIL)
        self.assertEqual(result.exit_code, 8)
        self.assertIn("ProcessError", result.detail)

    def test_timeout_returns_timeout_status(self):
        with mock.patch(
            "alphalens.preaudit.runner.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="dummy", timeout=300),
        ):
            result = run_smoke("insider_pc_compound")
        self.assertEqual(result.status, SmokeStatus.TIMEOUT)


class TestRunSmokeCleansUpEphemeralOut(unittest.TestCase):
    """The ephemeral --out file should not survive after run_smoke returns.

    Even if the subprocess fails partway through, the runner cleans up
    in `finally` so /tmp doesn't accumulate orphan preaudit_smoke_* files.
    """

    def test_ephemeral_out_cleaned_after_subprocess(self):
        captured_path: dict[str, str] = {}

        def fake_run(argv, *args, **kwargs):
            # Touch the file so cleanup has something to remove.
            from pathlib import Path

            out_path = Path(argv[argv.index("--out") + 1])
            out_path.write_text("synthetic-smoke-output")
            captured_path["path"] = str(out_path)
            return _completed(0)

        with mock.patch("alphalens.preaudit.runner.subprocess.run", side_effect=fake_run):
            run_smoke("insider_pc_compound")

        from pathlib import Path

        self.assertFalse(
            Path(captured_path["path"]).exists(),
            f"ephemeral out file not cleaned: {captured_path['path']}",
        )

    def test_cleanup_runs_even_on_subprocess_failure(self):
        captured_path: dict[str, str] = {}

        def fake_run(argv, *args, **kwargs):
            from pathlib import Path

            out_path = Path(argv[argv.index("--out") + 1])
            out_path.write_text("partial-output")
            captured_path["path"] = str(out_path)
            return _completed(1, stderr="boom")

        with mock.patch("alphalens.preaudit.runner.subprocess.run", side_effect=fake_run):
            run_smoke("insider_pc_compound")

        from pathlib import Path

        self.assertFalse(Path(captured_path["path"]).exists())


class TestRunSmokeStrategyWithoutHashGuard(unittest.TestCase):
    """A profile without `has_component_hash_guard=True` should still run."""

    def setUp(self):
        self.dummy_profile = SmokeProfile(
            strategy="tri_factor",  # exists in _SCRIPTS, no hash guard
            smoke_window=(date(2019, 1, 1), date(2019, 1, 31)),
            extra_args=("--quick",),
            data_deps=(
                DataDep(
                    name="prices",
                    check_type=__import__(
                        "alphalens.preaudit.profiles", fromlist=["CheckType"]
                    ).CheckType.EXISTS_NONEMPTY,
                ),
            ),
            has_component_hash_guard=False,
        )

    def test_runner_calls_subprocess_when_profile_provided_explicitly(self):
        with mock.patch(
            "alphalens.preaudit.runner.subprocess.run",
            side_effect=_completed_writing_file(0),
        ) as m:
            result = run_smoke("tri_factor", profile=self.dummy_profile)
        self.assertEqual(result.status, SmokeStatus.PASS)
        self.assertEqual(m.call_count, 1)


if __name__ == "__main__":
    unittest.main()
