import unittest
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner


class TestWatchdogCLI(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def test_watchdog_group_exposes_run_once(self):
        from alphalens_cli.commands.watchdog import watchdog_app

        result = self.runner.invoke(watchdog_app, ["--help"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("run-once", result.stdout)

    @patch("alphalens_cli.commands.watchdog._build_watchdog")
    def test_run_once_invokes_watchdog(self, mock_build):
        from alphalens_cli.commands.watchdog import watchdog_app

        fake_wd = MagicMock()
        fake_wd.run_once.return_value = {"events_detected": 3, "events_dispatched": 3}
        mock_build.return_value = fake_wd

        result = self.runner.invoke(watchdog_app, ["run-once"])
        self.assertEqual(result.exit_code, 0, msg=result.stdout)
        fake_wd.run_once.assert_called_once()


if __name__ == "__main__":
    unittest.main()
