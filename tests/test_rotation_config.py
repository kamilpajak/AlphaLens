import unittest
from unittest.mock import MagicMock, patch


class TestCaptureGitSha(unittest.TestCase):
    def test_returns_40_char_hex_from_rev_parse(self):
        from alphalens.rotation.config import capture_git_sha

        sha = "a" * 40
        completed = MagicMock(returncode=0, stdout=f"{sha}\n", stderr="")
        porcelain = MagicMock(returncode=0, stdout="", stderr="")

        with patch(
            "alphalens.rotation.config.subprocess.run",
            side_effect=[porcelain, completed],
        ):
            result = capture_git_sha()

        self.assertEqual(result, sha)
        self.assertEqual(len(result), 40)

    def test_raises_dirty_repo_error_when_porcelain_nonempty(self):
        from alphalens.rotation.config import DirtyRepoError, capture_git_sha

        porcelain = MagicMock(
            returncode=0, stdout=" M configs/tactical_rotation_v1.yaml\n", stderr=""
        )

        with (
            patch("alphalens.rotation.config.subprocess.run", return_value=porcelain),
            self.assertRaises(DirtyRepoError),
        ):
            capture_git_sha()

    def test_allow_dirty_bypasses_check(self):
        from alphalens.rotation.config import capture_git_sha

        sha = "b" * 40
        completed = MagicMock(returncode=0, stdout=f"{sha}\n", stderr="")

        with patch("alphalens.rotation.config.subprocess.run", return_value=completed) as mock_run:
            result = capture_git_sha(allow_dirty=True)

        self.assertEqual(result, sha)
        self.assertEqual(mock_run.call_count, 1)
        self.assertEqual(mock_run.call_args.args[0], ["git", "rev-parse", "HEAD"])


_VALID_CONFIG = """
core_weights:
  SPY: 0.60
  QQQ: 0.30
  IWM: 0.10
max_tilt: 0.10
rebalance_stride: 63
etf_spread_bps:
  SPY: 1.0
  QQQ: 2.0
  IWM: 3.0
rules:
  - name: yield_steep
    signal: yield_curve_slope
    operator: gt
    threshold: 100
    tilt: {QQQ: 0.05, SPY: -0.05}
  - name: vix_elevated
    signal: vix_decile
    operator: gt
    threshold: 0.75
    tilt: {SPY: 0.05, QQQ: -0.05}
gates:
  rolling_sharpe_min: 0.30
  carhart_oos_t_min: 1.50
"""


def _write_config(tmpdir, text: str):
    import pathlib

    path = pathlib.Path(tmpdir) / "rotation.yaml"
    path.write_text(text)
    return path


class TestLoadConfig(unittest.TestCase):
    def test_loads_valid_yaml_into_rotation_config(self):
        import tempfile

        from alphalens.rotation.config import RotationConfig, load_config

        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(tmp, _VALID_CONFIG)
            cfg = load_config(path)

        self.assertIsInstance(cfg, RotationConfig)
        self.assertEqual(cfg.core_weights, {"SPY": 0.60, "QQQ": 0.30, "IWM": 0.10})
        self.assertEqual(cfg.rebalance_stride, 63)
        self.assertEqual(cfg.max_tilt, 0.10)
        self.assertEqual(len(cfg.rules), 2)
        self.assertEqual(cfg.rules[0].name, "yield_steep")
        self.assertEqual(cfg.rules[0].tilt, {"QQQ": 0.05, "SPY": -0.05})

    def test_rejects_core_weights_not_summing_to_one(self):
        import tempfile

        from alphalens.rotation.config import ConfigError, load_config

        bad = _VALID_CONFIG.replace("SPY: 0.60", "SPY: 0.50")
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(tmp, bad)
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_rejects_more_than_five_rules(self):
        import tempfile

        from alphalens.rotation.config import ConfigError, load_config

        extra_rules = "\n".join(
            f"  - name: rule_{i}\n"
            f"    signal: yield_curve_slope\n"
            f"    operator: gt\n"
            f"    threshold: 0\n"
            f"    tilt: {{QQQ: 0.01, SPY: -0.01}}"
            for i in range(6)
        )
        text = _VALID_CONFIG.replace(
            "rules:\n"
            "  - name: yield_steep\n"
            "    signal: yield_curve_slope\n"
            "    operator: gt\n"
            "    threshold: 100\n"
            "    tilt: {QQQ: 0.05, SPY: -0.05}\n"
            "  - name: vix_elevated\n"
            "    signal: vix_decile\n"
            "    operator: gt\n"
            "    threshold: 0.75\n"
            "    tilt: {SPY: 0.05, QQQ: -0.05}\n",
            f"rules:\n{extra_rules}\n",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(tmp, text)
            with self.assertRaises(ConfigError) as ctx:
                load_config(path)
        self.assertIn("rules", str(ctx.exception).lower())

    def test_rejects_unknown_ticker_in_core(self):
        import tempfile

        from alphalens.rotation.config import ConfigError, load_config

        bad = _VALID_CONFIG.replace("SPY: 0.60", "MSFT: 0.60")
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(tmp, bad)
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_rejects_rule_tilt_with_unknown_ticker(self):
        import tempfile

        from alphalens.rotation.config import ConfigError, load_config

        bad = _VALID_CONFIG.replace("QQQ: 0.05, SPY: -0.05", "TSLA: 0.05, SPY: -0.05")
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(tmp, bad)
            with self.assertRaises(ConfigError):
                load_config(path)


class TestConfigFingerprint(unittest.TestCase):
    def test_fingerprint_has_git_sha_and_content_sha(self):
        import tempfile

        from alphalens.rotation.config import (
            ConfigFingerprint,
            compute_fingerprint,
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(tmp, _VALID_CONFIG)
            sha = "c" * 40
            completed = MagicMock(returncode=0, stdout=f"{sha}\n", stderr="")
            with patch("alphalens.rotation.config.subprocess.run", return_value=completed):
                fp = compute_fingerprint(path, allow_dirty=True)

        self.assertIsInstance(fp, ConfigFingerprint)
        self.assertEqual(fp.git_sha, sha)
        self.assertEqual(len(fp.content_sha256), 64)  # hex
        self.assertEqual(fp.config_path, str(path))

    def test_fingerprint_content_sha_stable_across_calls(self):
        import tempfile

        from alphalens.rotation.config import compute_fingerprint

        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(tmp, _VALID_CONFIG)
            sha = "d" * 40
            completed = MagicMock(returncode=0, stdout=f"{sha}\n", stderr="")
            with patch("alphalens.rotation.config.subprocess.run", return_value=completed):
                fp1 = compute_fingerprint(path, allow_dirty=True)
                fp2 = compute_fingerprint(path, allow_dirty=True)

        self.assertEqual(fp1.content_sha256, fp2.content_sha256)


if __name__ == "__main__":
    unittest.main()
