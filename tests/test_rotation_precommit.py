import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_CONFIG = """core_weights: {SPY: 0.60, QQQ: 0.30, IWM: 0.10}
max_tilt: 0.10
rebalance_stride: 63
etf_spread_bps: {SPY: 1.0, QQQ: 2.0, IWM: 3.0}
rules: []
gates: {rolling_sharpe_min: 0.30, carhart_oos_t_min: 1.50}
"""


class TestCountConfigCommits(unittest.TestCase):
    def test_counts_commits_touching_config_path(self):
        from alphalens.archive.rotation.precommit import count_config_commits

        # `git log --oneline configs/rotation.yaml` returns 3 lines
        completed = MagicMock(
            returncode=0,
            stdout="abc 1\ndef 2\nghi 3\n",
            stderr="",
        )
        with patch(
            "alphalens.archive.rotation.precommit.subprocess.run", return_value=completed
        ) as mock_run:
            n = count_config_commits(Path("configs/rotation.yaml"))

        self.assertEqual(n, 3)
        args = mock_run.call_args.args[0]
        self.assertIn("log", args)
        self.assertIn("configs/rotation.yaml", args)

    def test_zero_commits_when_no_git_history(self):
        from alphalens.archive.rotation.precommit import count_config_commits

        completed = MagicMock(returncode=0, stdout="", stderr="")
        with patch("alphalens.archive.rotation.precommit.subprocess.run", return_value=completed):
            self.assertEqual(count_config_commits(Path("configs/x.yaml")), 0)


class TestRecordRun(unittest.TestCase):
    def test_appends_json_entry_to_runlog(self):
        from alphalens.archive.rotation.config import ConfigFingerprint
        from alphalens.archive.rotation.precommit import record_run

        with tempfile.TemporaryDirectory() as tmp:
            runlog = Path(tmp) / "runlog.jsonl"
            fp = ConfigFingerprint(
                config_path="/tmp/rotation.yaml",
                content_sha256="a" * 64,
                git_sha="b" * 40,
            )

            record_run(
                runlog_path=runlog,
                fingerprint=fp,
                split="is",
                start="2009-01-01",
                end="2020-12-31",
                n_rebalances=47,
                sharpe_net=1.25,
                notes="baseline IS",
            )
            record_run(
                runlog_path=runlog,
                fingerprint=fp,
                split="oos",
                start="2021-01-01",
                end="2026-04-22",
                n_rebalances=20,
                sharpe_net=0.75,
                notes=None,
            )

            lines = runlog.read_text().splitlines()

        self.assertEqual(len(lines), 2)
        entry = json.loads(lines[0])
        self.assertEqual(entry["split"], "is")
        self.assertEqual(entry["git_sha"], "b" * 40)
        self.assertAlmostEqual(entry["sharpe_net"], 1.25)
        self.assertEqual(entry["n_rebalances"], 47)
        self.assertIn("timestamp", entry)


class TestCheckOOSDiscipline(unittest.TestCase):
    def test_ok_when_no_config_commits_since_is_baseline(self):
        from alphalens.archive.rotation.precommit import check_oos_discipline

        # 1 commit total, matches IS baseline SHA → 0 changes since IS
        completed = MagicMock(returncode=0, stdout="abc 1\n", stderr="")
        with patch("alphalens.archive.rotation.precommit.subprocess.run", return_value=completed):
            status = check_oos_discipline(
                config_path=Path("cfg.yaml"),
                is_baseline_sha="abc",
            )

        self.assertEqual(status.commits_since_is, 0)
        self.assertTrue(status.clean)

    def test_flags_when_config_changed_after_is(self):
        from alphalens.archive.rotation.precommit import check_oos_discipline

        # 3 commits; IS baseline at oldest (abc) → 2 commits since IS
        completed = MagicMock(
            returncode=0,
            stdout="newest 3\nmid 2\nabc 1\n",
            stderr="",
        )
        with patch("alphalens.archive.rotation.precommit.subprocess.run", return_value=completed):
            status = check_oos_discipline(
                config_path=Path("cfg.yaml"),
                is_baseline_sha="abc",
            )

        self.assertEqual(status.commits_since_is, 2)
        self.assertFalse(status.clean)
        self.assertIn("2", status.message)

    def test_true_n_tests_accumulates(self):
        """Bonferroni n_tests = commits_since_is + 2 (H1 + H2 baseline)."""
        from alphalens.archive.rotation.precommit import check_oos_discipline

        completed = MagicMock(
            returncode=0,
            stdout="d\nc\nb\nabc\n",
            stderr="",
        )
        with patch("alphalens.archive.rotation.precommit.subprocess.run", return_value=completed):
            status = check_oos_discipline(
                config_path=Path("cfg.yaml"),
                is_baseline_sha="abc",
                baseline_n_tests=2,
            )

        # 3 commits since abc + 2 baseline = 5
        self.assertEqual(status.true_n_tests, 5)


if __name__ == "__main__":
    unittest.main()
