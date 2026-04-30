import unittest
from datetime import date
from unittest.mock import MagicMock


def _feature(insider_count: int, aggregate_dollar: float = 100_000.0) -> dict:
    return {
        "insider_count": insider_count,
        "aggregate_dollar": aggregate_dollar,
        "cluster_window_days": 30,
        "asof": "2026-04-22",
    }


def _make_pipeline(features_by_ticker: dict, universe: list[str]):
    from alphalens.archive.screeners.insider.pipeline import InsiderPipeline

    scorer = MagicMock()
    scorer.features_as_of.side_effect = lambda t, asof: features_by_ticker.get(t)
    return InsiderPipeline(
        scorer=scorer,
        universe_loader=lambda: universe,
    ), scorer


class TestRun(unittest.TestCase):
    def test_ranks_tickers_by_insider_count_descending(self):
        pipeline, _ = _make_pipeline(
            features_by_ticker={
                "UPST": _feature(3),
                "SMCI": _feature(5),
                "GPC": _feature(4),
            },
            universe=["UPST", "SMCI", "GPC"],
        )

        df = pipeline.run(curr_date=date(2026, 4, 22), top_n=10)

        self.assertEqual(list(df["ticker"]), ["SMCI", "GPC", "UPST"])
        self.assertEqual(list(df["insider_count"]), [5, 4, 3])

    def test_excludes_tickers_without_cluster(self):
        pipeline, _ = _make_pipeline(
            features_by_ticker={
                "UPST": _feature(3),
                "SMCI": None,  # type: ignore[dict-item]
                "GPC": _feature(4),
            },
            universe=["UPST", "SMCI", "GPC"],
        )

        df = pipeline.run(curr_date=date(2026, 4, 22), top_n=10)

        self.assertEqual(set(df["ticker"]), {"UPST", "GPC"})
        self.assertNotIn("SMCI", list(df["ticker"]))

    def test_empty_universe_returns_empty_df_with_columns(self):
        pipeline, _ = _make_pipeline(features_by_ticker={}, universe=[])

        df = pipeline.run(curr_date=date(2026, 4, 22), top_n=10)

        self.assertTrue(df.empty)
        self.assertIn("ticker", df.columns)
        self.assertIn("insider_count", df.columns)

    def test_no_clusters_returns_empty_df(self):
        pipeline, _ = _make_pipeline(
            features_by_ticker={"UPST": None, "SMCI": None},  # type: ignore[dict-item]
            universe=["UPST", "SMCI"],
        )

        df = pipeline.run(curr_date=date(2026, 4, 22), top_n=10)

        self.assertTrue(df.empty)

    def test_top_n_limits_output(self):
        pipeline, _ = _make_pipeline(
            features_by_ticker={t: _feature(i + 3) for i, t in enumerate(["A", "B", "C", "D"])},
            universe=["A", "B", "C", "D"],
        )

        df = pipeline.run(curr_date=date(2026, 4, 22), top_n=2)

        self.assertEqual(len(df), 2)
        # Highest insider_count first.
        self.assertEqual(df.iloc[0]["ticker"], "D")
        self.assertEqual(df.iloc[1]["ticker"], "C")

    def test_scorer_called_with_curr_date(self):
        pipeline, scorer = _make_pipeline(
            features_by_ticker={"UPST": _feature(3)},
            universe=["UPST"],
        )
        asof = date(2024, 6, 30)

        pipeline.run(curr_date=asof, top_n=10)

        _, kwargs = scorer.features_as_of.call_args
        # Accept both positional and keyword invocation.
        call_args = scorer.features_as_of.call_args
        _, kwargs = call_args
        positional = call_args.args
        if positional:
            self.assertEqual(positional[1], asof)
        else:
            self.assertEqual(kwargs["asof"], asof)


class TestToCandidates(unittest.TestCase):
    def test_empty_df_returns_empty_list(self):
        import pandas as pd

        from alphalens.archive.screeners.insider.pipeline import InsiderPipeline

        pipeline = InsiderPipeline(scorer=MagicMock(), universe_loader=list)

        candidates = pipeline.to_candidates(pd.DataFrame(columns=["ticker", "insider_count"]))

        self.assertEqual(candidates, [])

    def test_candidates_carry_insider_metrics_in_payload(self):
        pipeline, _ = _make_pipeline(
            features_by_ticker={
                "UPST": _feature(3, aggregate_dollar=500_000),
                "SMCI": _feature(5, aggregate_dollar=1_000_000),
            },
            universe=["UPST", "SMCI"],
        )

        df = pipeline.run(curr_date=date(2026, 4, 22), top_n=10)
        candidates = pipeline.to_candidates(df)

        self.assertEqual(len(candidates), 2)
        smci = next(c for c in candidates if c.ticker == "SMCI")
        self.assertEqual(smci.source, "insider")
        self.assertEqual(smci.payload["insider_count"], 5)
        self.assertEqual(smci.payload["aggregate_dollar"], 1_000_000.0)
        self.assertIn("weight", smci.payload)
        self.assertEqual(smci.payload["weighting_scheme"], "linear")

    def test_linear_weights_sum_close_to_one(self):
        pipeline, _ = _make_pipeline(
            features_by_ticker={t: _feature(i + 3) for i, t in enumerate(["A", "B", "C"])},
            universe=["A", "B", "C"],
        )

        df = pipeline.run(curr_date=date(2026, 4, 22), top_n=3)
        candidates = pipeline.to_candidates(df, weighting="linear")

        total_weight = sum(c.payload["weight"] for c in candidates)
        self.assertAlmostEqual(total_weight, 1.0, places=3)


class TestSourceNaming(unittest.TestCase):
    def test_default_source_name_is_insider(self):
        from alphalens.archive.screeners.insider.pipeline import InsiderPipeline

        pipeline = InsiderPipeline(scorer=MagicMock(), universe_loader=list)

        self.assertEqual(pipeline.source_name, "insider")

    def test_custom_source_name_override(self):
        from alphalens.archive.screeners.insider.pipeline import InsiderPipeline

        pipeline = InsiderPipeline(
            scorer=MagicMock(),
            universe_loader=list,
            source_name="insider-cluster",
        )

        self.assertEqual(pipeline.source_name, "insider-cluster")


if __name__ == "__main__":
    unittest.main()
