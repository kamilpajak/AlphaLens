import unittest
from datetime import date
from unittest.mock import MagicMock


def _valid_output(n=3):
    from alphalens.lean_screener.schema import LeanOutput, RankingRow

    rows = [
        RankingRow(
            ticker=f"T{i}",
            rank=i + 1,
            score=1.0 - i * 0.1,
            roc5=0.01,
            roc20=0.05,
            roc60=0.10,
            volume_surprise=2.0,
            trend_strength=1.0,
            breakout=(i == 0),
            near_high=0.9,
            last_close=100.0,
            avg_dollar_volume=10_000_000.0,
        )
        for i in range(n)
    ]
    return LeanOutput(
        status="success",
        timestamp="2026-04-18T21:00:00+00:00",
        version="1.0",
        total_scored=n,
        universe_size=500,
        rankings=rows,
    )


class TestOutputToDataFrame(unittest.TestCase):
    def test_columns_and_rows(self):
        from alphalens.lean_screener.pipeline import lean_output_to_dataframe

        df = lean_output_to_dataframe(_valid_output(2))

        self.assertEqual(len(df), 2)
        for col in [
            "ticker", "rank", "score", "roc5", "roc20", "roc60",
            "volume_surprise", "trend_strength", "breakout", "near_high",
            "last_close", "avg_dollar_volume",
        ]:
            self.assertIn(col, df.columns)
        self.assertEqual(df.iloc[0]["ticker"], "T0")

    def test_empty_output_returns_empty_frame_with_columns(self):
        from alphalens.lean_screener.pipeline import lean_output_to_dataframe
        from alphalens.lean_screener.schema import LeanOutput

        empty = LeanOutput(
            status="success",
            timestamp="x",
            version="1.0",
            total_scored=0,
            universe_size=500,
            rankings=[],
        )
        df = lean_output_to_dataframe(empty)
        self.assertTrue(df.empty)
        self.assertIn("ticker", df.columns)


class TestPipelineRun(unittest.TestCase):
    def test_run_calls_sync_then_runner_and_returns_top_n(self):
        from alphalens.lean_screener.pipeline import LeanScreenerPipeline

        runner = MagicMock()
        runner.run.return_value = _valid_output(5)
        sync = MagicMock()
        sync.incremental_sync.return_value = MagicMock(
            dates_synced=["2026-04-17"], tickers_written=500, bars_written=500,
        )

        pipe = LeanScreenerPipeline(runner=runner, sync=sync)
        df = pipe.run(today=date(2026, 4, 18), top_n=3)

        sync.incremental_sync.assert_called_once()
        runner.run.assert_called_once()
        self.assertEqual(len(df), 3)

    def test_run_without_sync_just_runs_lean(self):
        from alphalens.lean_screener.pipeline import LeanScreenerPipeline

        runner = MagicMock()
        runner.run.return_value = _valid_output(5)

        pipe = LeanScreenerPipeline(runner=runner)
        df = pipe.run(top_n=2)

        self.assertEqual(len(df), 2)
        runner.run.assert_called_once()

    def test_run_without_runner_raises(self):
        from alphalens.lean_screener.pipeline import LeanScreenerPipeline

        with self.assertRaises(RuntimeError):
            LeanScreenerPipeline().run()


class TestToCandidates(unittest.TestCase):
    def test_builds_lean_source_priority_15(self):
        from alphalens.lean_screener.pipeline import (
            LeanScreenerPipeline,
            lean_output_to_dataframe,
        )

        df = lean_output_to_dataframe(_valid_output(2))
        pipe = LeanScreenerPipeline()
        cands = pipe.to_candidates(df)

        self.assertEqual(len(cands), 2)
        for c in cands:
            self.assertEqual(c.source, "lean")
            self.assertEqual(c.priority, 15)
        self.assertEqual(cands[0].ticker, "T0")

    def test_payload_carries_score_and_features(self):
        from alphalens.lean_screener.pipeline import (
            LeanScreenerPipeline,
            lean_output_to_dataframe,
        )

        df = lean_output_to_dataframe(_valid_output(1))
        cand = LeanScreenerPipeline().to_candidates(df)[0]

        for key in (
            "score", "rank", "roc20", "roc60", "volume_surprise",
            "trend_strength", "breakout", "near_high", "last_close",
        ):
            self.assertIn(key, cand.payload)

    def test_empty_dataframe_produces_no_candidates(self):
        import pandas as pd

        from alphalens.lean_screener.pipeline import LeanScreenerPipeline

        self.assertEqual(LeanScreenerPipeline().to_candidates(pd.DataFrame()), [])

    def test_dedup_key_unique_across_tickers(self):
        from alphalens.lean_screener.pipeline import (
            LeanScreenerPipeline,
            lean_output_to_dataframe,
        )

        df = lean_output_to_dataframe(_valid_output(3))
        cands = LeanScreenerPipeline().to_candidates(df)
        keys = {c.dedup_key for c in cands}
        self.assertEqual(len(keys), 3)


if __name__ == "__main__":
    unittest.main()
