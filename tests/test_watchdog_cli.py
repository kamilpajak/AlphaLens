import unittest
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner


class TestWatchdogCLI(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def test_cli_has_run_once_and_process_queue_subcommands(self):
        from cli.watchdog_main import watchdog_app

        result = self.runner.invoke(watchdog_app, ["--help"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("run-once", result.stdout)
        self.assertIn("process-queue", result.stdout)

    @patch("cli.watchdog_main._build_watchdog")
    def test_run_once_invokes_watchdog(self, mock_build):
        from cli.watchdog_main import watchdog_app

        fake_wd = MagicMock()
        fake_wd.run_once.return_value = {"events_detected": 3, "events_dispatched": 3}
        mock_build.return_value = fake_wd

        result = self.runner.invoke(watchdog_app, ["run-once"])
        self.assertEqual(result.exit_code, 0, msg=result.stdout)
        fake_wd.run_once.assert_called_once()

    @patch("cli.watchdog_main._build_worker")
    def test_process_queue_invokes_worker(self, mock_build):
        from cli.watchdog_main import watchdog_app

        fake_worker = MagicMock()
        fake_worker.process_all.return_value = 2
        mock_build.return_value = fake_worker

        result = self.runner.invoke(watchdog_app, ["process-queue"])
        self.assertEqual(result.exit_code, 0, msg=result.stdout)
        fake_worker.process_all.assert_called_once()


if __name__ == "__main__":
    unittest.main()
