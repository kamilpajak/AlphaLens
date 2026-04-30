import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import yaml
from typer.testing import CliRunner

runner = CliRunner()


def _write_yaml(path: Path, content) -> None:
    path.write_text(yaml.safe_dump(content))


class TestInsiderScreenCommand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

        self.iwm_path = self.root / "iwm.yaml"
        self.cik_map_path = self.root / "cik.yaml"
        _write_yaml(self.iwm_path, {"tickers": ["UPST", "SMCI"]})
        _write_yaml(self.cik_map_path, {"UPST": 111, "SMCI": 222})

    def _invoke(self, args, env=None, pipeline_result=None):
        from alphalens_cli.commands.insider import insider_app

        merged_env = {"SEC_EDGAR_USER_AGENT": "AlphaLens test@example.com"}
        if env:
            merged_env.update(env)

        df = (
            pipeline_result
            if pipeline_result is not None
            else pd.DataFrame(columns=["ticker", "insider_count", "aggregate_dollar", "asof"])
        )

        with patch("alphalens.archive.screeners.insider.pipeline.InsiderPipeline") as mock_cls:
            instance = MagicMock()
            instance.run.return_value = df
            instance.to_candidates.return_value = []
            mock_cls.return_value = instance
            with patch.dict("os.environ", merged_env, clear=False):
                return runner.invoke(insider_app, args)

    def test_missing_user_agent_fails(self):
        from alphalens_cli.commands.insider import insider_app

        with patch.dict("os.environ", {}, clear=True):
            result = runner.invoke(
                insider_app,
                [
                    "screen",
                    "--dry-run",
                    "--universe-file",
                    str(self.iwm_path),
                    "--cik-map-file",
                    str(self.cik_map_path),
                ],
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("SEC_EDGAR_USER_AGENT", str(result.output) + str(result.exception))

    def test_missing_data_file_raises_with_bootstrap_hint(self):
        result = self._invoke(
            [
                "screen",
                "--dry-run",
                "--universe-file",
                str(self.root / "missing.yaml"),
                "--cik-map-file",
                str(self.cik_map_path),
            ]
        )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Bootstrap", str(result.output) + str(result.exception))

    def test_dry_run_prints_no_clusters_message_on_empty_result(self):
        result = self._invoke(
            [
                "screen",
                "--dry-run",
                "--universe-file",
                str(self.iwm_path),
                "--cik-map-file",
                str(self.cik_map_path),
            ]
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("No clusters detected", result.output)

    def test_dry_run_prints_cluster_table(self):
        df = pd.DataFrame(
            [
                {
                    "ticker": "UPST",
                    "insider_count": 4,
                    "aggregate_dollar": 500_000,
                    "asof": "2026-04-22",
                },
                {
                    "ticker": "SMCI",
                    "insider_count": 3,
                    "aggregate_dollar": 250_000,
                    "asof": "2026-04-22",
                },
            ]
        )

        result = self._invoke(
            [
                "screen",
                "--dry-run",
                "--universe-file",
                str(self.iwm_path),
                "--cik-map-file",
                str(self.cik_map_path),
            ],
            pipeline_result=df,
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("UPST", result.output)
        self.assertIn("SMCI", result.output)
        self.assertIn("2 cluster event(s)", result.output)

    def test_report_option_writes_file(self):
        df = pd.DataFrame(
            [
                {
                    "ticker": "UPST",
                    "insider_count": 3,
                    "aggregate_dollar": 100,
                    "asof": "2026-04-22",
                },
            ]
        )
        report_path = self.root / "report.md"

        result = self._invoke(
            [
                "screen",
                "--dry-run",
                "--universe-file",
                str(self.iwm_path),
                "--cik-map-file",
                str(self.cik_map_path),
                "--report",
                str(report_path),
            ],
            pipeline_result=df,
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(report_path.exists())
        content = report_path.read_text()
        self.assertIn("UPST", content)

    def test_top_n_passed_to_pipeline(self):
        with patch("alphalens.archive.screeners.insider.pipeline.InsiderPipeline") as mock_cls:
            instance = MagicMock()
            instance.run.return_value = pd.DataFrame(
                columns=["ticker", "insider_count", "aggregate_dollar", "asof"]
            )
            mock_cls.return_value = instance

            with patch.dict(
                "os.environ",
                {"SEC_EDGAR_USER_AGENT": "AlphaLens test@example.com"},
                clear=False,
            ):
                from alphalens_cli.commands.insider import insider_app

                runner.invoke(
                    insider_app,
                    [
                        "screen",
                        "--dry-run",
                        "--top-n",
                        "25",
                        "--universe-file",
                        str(self.iwm_path),
                        "--cik-map-file",
                        str(self.cik_map_path),
                    ],
                )

        _, kwargs = instance.run.call_args
        self.assertEqual(kwargs["top_n"], 25)


if __name__ == "__main__":
    unittest.main()
