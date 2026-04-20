"""Phase B: momentum screener must expose `to_candidates()` and CLI --analyze submits to queue."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
from typer.testing import CliRunner


class TestMomentumToCandidates(unittest.TestCase):
    def test_to_candidates_maps_dataframe_rows_to_candidate_objects(self):
        from alphalens.candidates import Candidate
        from alphalens.screeners.themed.pipeline import ThemedPipeline

        df = pd.DataFrame(
            [
                {"ticker": "AAPL", "momentum_score": 0.92, "themes": ["AI", "MegaCap"]},
                {"ticker": "NVDA", "momentum_score": 0.88, "themes": ["AI"]},
            ]
        )

        candidates = ThemedPipeline().to_candidates(df)
        self.assertEqual(len(candidates), 2)
        for c in candidates:
            self.assertIsInstance(c, Candidate)
            self.assertEqual(c.source, "momentum")
            self.assertEqual(c.priority, 10)

        first = candidates[0]
        self.assertEqual(first.ticker, "AAPL")
        self.assertAlmostEqual(first.payload["momentum_score"], 0.92)
        self.assertEqual(first.payload["themes"], ["AI", "MegaCap"])

    def test_to_candidates_on_empty_dataframe_returns_empty_list(self):
        from alphalens.screeners.themed.pipeline import ThemedPipeline

        df = pd.DataFrame(columns=["ticker", "momentum_score", "themes"])
        self.assertEqual(ThemedPipeline().to_candidates(df), [])

    def test_linear_weighting_emits_descending_weights(self):
        """Rank 1 (najwyższy score) dostaje największą wagę, wagi sumują do 1.0."""
        from alphalens.screeners.themed.pipeline import ThemedPipeline

        df = pd.DataFrame(
            [
                {"ticker": "A", "momentum_score": 0.9, "themes": ["X"]},
                {"ticker": "B", "momentum_score": 0.8, "themes": ["X"]},
                {"ticker": "C", "momentum_score": 0.7, "themes": ["X"]},
            ]
        )
        candidates = ThemedPipeline().to_candidates(df, weighting="linear")
        weights = [c.payload["weight"] for c in candidates]
        self.assertAlmostEqual(sum(weights), 1.0)
        self.assertGreater(weights[0], weights[1])
        self.assertGreater(weights[1], weights[2])
        for c in candidates:
            self.assertEqual(c.payload["weighting_scheme"], "linear")

    def test_equal_weighting_fallback(self):
        from alphalens.screeners.themed.pipeline import ThemedPipeline

        df = pd.DataFrame(
            [{"ticker": f"T{i}", "momentum_score": 1.0 - i * 0.1, "themes": ["X"]}
             for i in range(5)]
        )
        candidates = ThemedPipeline().to_candidates(df, weighting="equal")
        weights = [c.payload["weight"] for c in candidates]
        self.assertAlmostEqual(sum(weights), 1.0)
        for w in weights:
            self.assertAlmostEqual(w, 0.2)


class TestMomentumScreenCLIAnalyzeFlag(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "candidates.db"
        self.runner = CliRunner()

    def tearDown(self):
        self.tmp.cleanup()

    def _fake_df(self):
        return pd.DataFrame(
            [
                {"ticker": "AAPL", "momentum_score": 0.91, "themes": ["AI"]},
                {"ticker": "NVDA", "momentum_score": 0.88, "themes": ["AI"]},
            ]
        )

    @patch("alphalens_cli.watchdog_main.default_queue_path")
    @patch("alphalens_cli.watchdog_main.TelegramHandler")
    @patch.dict(
        "os.environ",
        {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"},
        clear=False,
    )
    def test_analyze_flag_submits_candidates_to_queue(
        self, _mock_telegram, mock_queue_path
    ):
        from alphalens_cli.watchdog_main import watchdog_app

        mock_queue_path.return_value = self.db

        with patch(
            "alphalens.screeners.themed.pipeline.ThemedPipeline.run",
            return_value=self._fake_df(),
        ):
            result = self.runner.invoke(
                watchdog_app, ["momentum-screen", "--top-n", "2", "--analyze", "--dry-run"]
            )

        self.assertEqual(result.exit_code, 0, msg=result.stdout)

        from alphalens.queue import CandidateQueue

        with CandidateQueue(self.db) as q:
            pending = q.list_by_status("pending")
            tickers = sorted(r["ticker"] for r in pending)
            self.assertEqual(tickers, ["AAPL", "NVDA"])
            self.assertTrue(all(r["source"] == "momentum" for r in pending))
            self.assertTrue(all(r["priority"] == 10 for r in pending))

    @patch("alphalens_cli.watchdog_main.default_queue_path")
    @patch("alphalens_cli.watchdog_main.TelegramHandler")
    @patch.dict(
        "os.environ",
        {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"},
        clear=False,
    )
    def test_without_analyze_flag_queue_stays_empty(
        self, _mock_telegram, mock_queue_path
    ):
        from alphalens_cli.watchdog_main import watchdog_app

        mock_queue_path.return_value = self.db

        with patch(
            "alphalens.screeners.themed.pipeline.ThemedPipeline.run",
            return_value=self._fake_df(),
        ):
            result = self.runner.invoke(
                watchdog_app, ["momentum-screen", "--top-n", "2", "--dry-run"]
            )

        self.assertEqual(result.exit_code, 0, msg=result.stdout)

        # No DB expected — queue path not touched when --analyze absent
        if self.db.exists():
            from alphalens.queue import CandidateQueue

            with CandidateQueue(self.db) as q:
                self.assertEqual(q.list_by_status("pending"), [])


if __name__ == "__main__":
    unittest.main()
