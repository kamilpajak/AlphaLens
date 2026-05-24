import unittest
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
        from alphalens_cli.commands.edgar import edgar_app

        fake_detector = MagicMock()
        fake_detector.run_once.return_value = {"events_detected": 3, "events_dispatched": 3}
        mock_build.return_value = fake_detector

        result = self.runner.invoke(edgar_app, ["detect"])
        self.assertEqual(result.exit_code, 0, msg=result.stdout)
        fake_detector.run_once.assert_called_once()


if __name__ == "__main__":
    unittest.main()
