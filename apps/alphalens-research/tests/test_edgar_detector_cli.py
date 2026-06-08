import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner


class TestDetectorCLI(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def test_edgar_group_exposes_detect(self):
        from alphalens_cli.commands.edgar import edgar_app

        result = self.runner.invoke(edgar_app, ["--help"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("detect", result.stdout)

    @patch("alphalens_cli.commands.edgar._build_detector")
    def test_detect_invokes_detector(self, mock_build):
        from alphalens_cli.commands import edgar
        from alphalens_cli.commands.edgar import edgar_app

        fake_detector = MagicMock()
        fake_detector.run_once.return_value = {"events_detected": 3, "events_dispatched": 3}
        mock_build.return_value = fake_detector

        # Redirect dispatch-state persistence to a temp dir so the run does not
        # write a stray dispatch_state.json into the real ~/.alphalens.
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(edgar, "_state_home", return_value=Path(tmp)),
        ):
            result = self.runner.invoke(edgar_app, ["detect"])
        self.assertEqual(result.exit_code, 0, msg=result.stdout)
        fake_detector.run_once.assert_called_once()

    def test_dispatch_state_failure_emits_fail_loud_sentinel(self):
        # If dispatch-state persistence raises (disk full, perms flip), the
        # no-dispatch gauge must NOT fall back to 0 — that reads the same as
        # "just dispatched" and the alert (> 5) would never fire, silently
        # masking a jammed gauge that JobStale cannot catch (the cron still
        # succeeds). It must emit a sentinel ABOVE the alert threshold so a
        # persistent state failure is loud.
        from alphalens_cli.commands import edgar
        from alphalens_pipeline.edgar_detector.dispatch_state import (
            DISPATCH_STATE_FAILURE_SENTINEL,
        )

        self.assertGreater(DISPATCH_STATE_FAILURE_SENTINEL, 5)
        with patch.object(
            edgar, "_trading_days_since_dispatch_gauge", side_effect=OSError("disk full")
        ):
            self.assertEqual(
                edgar._safe_trading_days_since_dispatch_gauge(0),
                DISPATCH_STATE_FAILURE_SENTINEL,
            )

    def test_safe_gauge_passes_through_on_success(self):
        from alphalens_cli.commands import edgar

        with patch.object(edgar, "_trading_days_since_dispatch_gauge", return_value=3):
            self.assertEqual(edgar._safe_trading_days_since_dispatch_gauge(0), 3)


if __name__ == "__main__":
    unittest.main()
