import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock


def _valid_payload(status="success"):
    return {
        "status": status,
        "timestamp": "2026-04-18T21:00:00+00:00",
        "version": "1.0",
        "total_scored": 1,
        "universe_size": 10,
        "rankings": [
            {
                "ticker": "AAPL",
                "rank": 1,
                "score": 0.9,
                "roc5": 0.01,
                "roc20": 0.05,
                "roc60": 0.12,
                "volume_surprise": 2.0,
                "trend_strength": 1.0,
                "breakout": True,
                "near_high": 0.98,
                "last_close": 200.0,
                "avg_dollar_volume": 500_000_000.0,
            }
        ],
    }


def _build_config(tmp: Path):
    from alphalens.archive.screeners.lean.runner import LeanRunConfig

    (tmp / "project").mkdir()
    (tmp / "data").mkdir()
    (tmp / "results").mkdir()
    (tmp / "logs").mkdir()
    return LeanRunConfig(
        project_dir=tmp / "project",
        data_dir=tmp / "data",
        results_dir=tmp / "results",
        logs_dir=tmp / "logs",
        image="fake/lean",
        timeout_sec=10,
    )


class TestBuildDockerArgs(unittest.TestCase):
    def test_includes_all_volume_mounts(self):
        from alphalens.archive.screeners.lean.runner import LeanDockerRunner

        with tempfile.TemporaryDirectory() as tmp:
            cfg = _build_config(Path(tmp))
            args = LeanDockerRunner(cfg, subprocess_runner=MagicMock()).build_docker_args()

        self.assertIn("docker", args)
        self.assertIn("run", args)
        self.assertIn("--rm", args)
        # Volume mounts present for every directory.
        v_args = [args[i + 1] for i, a in enumerate(args) if a == "-v"]
        self.assertTrue(any(str(cfg.project_dir) in v for v in v_args))
        self.assertTrue(any(str(cfg.data_dir) in v for v in v_args))
        self.assertTrue(any(str(cfg.results_dir) in v for v in v_args))
        self.assertTrue(any(str(cfg.logs_dir) in v for v in v_args))
        self.assertIn("fake/lean", args)

    def test_passes_algo_location_and_data_folder(self):
        from alphalens.archive.screeners.lean.runner import LeanDockerRunner

        with tempfile.TemporaryDirectory() as tmp:
            cfg = _build_config(Path(tmp))
            args = LeanDockerRunner(cfg, subprocess_runner=MagicMock()).build_docker_args()

        self.assertIn("--algorithm-location", args)
        self.assertIn("/Project/main.py", args)
        self.assertIn("--data-folder", args)
        self.assertIn("/Data", args)
        self.assertIn("--algorithm-language", args)
        self.assertIn("Python", args)
        self.assertIn("--algorithm-type-name", args)
        self.assertIn("LeanBatchScreener", args)

    def test_extra_env_included(self):
        from alphalens.archive.screeners.lean.runner import LeanDockerRunner, LeanRunConfig

        with tempfile.TemporaryDirectory() as tmp:
            base = _build_config(Path(tmp))
            cfg = LeanRunConfig(
                project_dir=base.project_dir,
                data_dir=base.data_dir,
                results_dir=base.results_dir,
                logs_dir=base.logs_dir,
                image=base.image,
                extra_env={"POLYGON_API_KEY": "secret"},
            )
            args = LeanDockerRunner(cfg, subprocess_runner=MagicMock()).build_docker_args()

        self.assertIn("-e", args)
        self.assertIn("POLYGON_API_KEY=secret", args)


class TestRun(unittest.TestCase):
    def _fake_runner(self, returncode=0, stdout="ok", stderr="", write_payload=None):
        """Return a callable that pretends to invoke docker and writes JSON side-effect."""

        def runner(cmd, timeout):
            if write_payload is not None:
                # Infer /Results mount target from args.
                for i, arg in enumerate(cmd):
                    if arg == "-v" and ":/Results" in cmd[i + 1]:
                        host_path = cmd[i + 1].split(":")[0]
                        (Path(host_path) / "candidates.json").write_text(json.dumps(write_payload))
                        break
            proc = MagicMock()
            proc.returncode = returncode
            proc.stdout = stdout
            proc.stderr = stderr
            return proc

        return runner

    def test_success_returns_lean_output(self):
        from alphalens.archive.screeners.lean.runner import LeanDockerRunner

        with tempfile.TemporaryDirectory() as tmp:
            cfg = _build_config(Path(tmp))
            fake = self._fake_runner(write_payload=_valid_payload())
            runner = LeanDockerRunner(cfg, subprocess_runner=fake)

            output = runner.run()

        self.assertEqual(output.status, "success")
        self.assertEqual(len(output.rankings), 1)

    def test_nonzero_exit_raises(self):
        from alphalens.archive.screeners.lean.runner import LeanDockerRunner, LeanRunError

        with tempfile.TemporaryDirectory() as tmp:
            cfg = _build_config(Path(tmp))
            fake = self._fake_runner(returncode=1, stderr="crash")
            runner = LeanDockerRunner(cfg, subprocess_runner=fake)

            with self.assertRaises(LeanRunError):
                runner.run()

    def test_missing_output_raises(self):
        from alphalens.archive.screeners.lean.runner import LeanDockerRunner, LeanRunError

        with tempfile.TemporaryDirectory() as tmp:
            cfg = _build_config(Path(tmp))
            fake = self._fake_runner(returncode=0, write_payload=None)  # no JSON
            runner = LeanDockerRunner(cfg, subprocess_runner=fake)

            with self.assertRaises(LeanRunError):
                runner.run()

    def test_timeout_raises_lean_run_error(self):
        from alphalens.archive.screeners.lean.runner import LeanDockerRunner, LeanRunError

        def raising_runner(cmd, timeout):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

        with tempfile.TemporaryDirectory() as tmp:
            cfg = _build_config(Path(tmp))
            runner = LeanDockerRunner(cfg, subprocess_runner=raising_runner)

            with self.assertRaises(LeanRunError):
                runner.run()

    def test_status_error_payload_raises(self):
        from alphalens.archive.screeners.lean.runner import LeanDockerRunner, LeanRunError

        with tempfile.TemporaryDirectory() as tmp:
            cfg = _build_config(Path(tmp))
            fake = self._fake_runner(
                write_payload={**_valid_payload(), "status": "error", "rankings": []}
            )
            runner = LeanDockerRunner(cfg, subprocess_runner=fake)

            with self.assertRaises(LeanRunError):
                runner.run()

    def test_stale_results_removed_before_run(self):
        from alphalens.archive.screeners.lean.runner import LeanDockerRunner, LeanRunError

        with tempfile.TemporaryDirectory() as tmp:
            cfg = _build_config(Path(tmp))
            stale = cfg.results_dir / "candidates.json"
            stale.write_text(json.dumps(_valid_payload()))  # leftover from last run

            # Runner fails; without cleanup it would falsely see the stale file.
            def fake(cmd, timeout):
                proc = MagicMock()
                proc.returncode = 1
                proc.stdout = ""
                proc.stderr = "crash"
                return proc

            with self.assertRaises(LeanRunError):
                LeanDockerRunner(cfg, subprocess_runner=fake).run()

    def test_persists_logs_for_postmortem(self):
        from alphalens.archive.screeners.lean.runner import LeanDockerRunner

        with tempfile.TemporaryDirectory() as tmp:
            cfg = _build_config(Path(tmp))
            fake = self._fake_runner(
                stdout="hello",
                stderr="some warning",
                write_payload=_valid_payload(),
            )
            LeanDockerRunner(cfg, subprocess_runner=fake).run()

            logs = list(cfg.logs_dir.iterdir())
            self.assertTrue(any(p.name.endswith("_stdout.log") for p in logs))
            self.assertTrue(any(p.name.endswith("_stderr.log") for p in logs))


class TestDockerAvailable(unittest.TestCase):
    def test_true_when_docker_returns_zero(self):
        from alphalens.archive.screeners.lean.runner import docker_available

        fake = MagicMock()
        fake.returncode = 0
        self.assertTrue(docker_available(lambda cmd, t: fake))

    def test_false_when_binary_missing(self):
        from alphalens.archive.screeners.lean.runner import docker_available

        def raise_fnf(cmd, t):
            raise FileNotFoundError("docker not installed")

        self.assertFalse(docker_available(raise_fnf))

    def test_false_on_nonzero_exit(self):
        from alphalens.archive.screeners.lean.runner import docker_available

        fake = MagicMock()
        fake.returncode = 127
        self.assertFalse(docker_available(lambda cmd, t: fake))


class TestDefaultRunConfig(unittest.TestCase):
    def test_builds_from_alphalens_defaults(self):
        from alphalens.archive.screeners.lean.config import (
            DATA_DIR,
            LEAN_DOCKER_IMAGE,
            LEAN_PROJECT_DIR,
            LOGS_DIR,
            RESULTS_DIR,
        )
        from alphalens.archive.screeners.lean.runner import default_run_config

        cfg = default_run_config()

        self.assertEqual(cfg.project_dir, LEAN_PROJECT_DIR)
        self.assertEqual(cfg.data_dir, DATA_DIR)
        self.assertEqual(cfg.results_dir, RESULTS_DIR)
        self.assertEqual(cfg.logs_dir, LOGS_DIR)
        self.assertEqual(cfg.image, LEAN_DOCKER_IMAGE)


if __name__ == "__main__":
    unittest.main()
