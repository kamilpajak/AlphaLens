import datetime as dt
import unittest
from unittest.mock import patch

import pandas as pd

from alphalens.thematic.screening import scorer


def _candidates_df(tickers: list[str]) -> pd.DataFrame:
    """Minimal Phase C parquet shape — just the columns scorer reads."""
    return pd.DataFrame(
        {
            "theme": ["quantum_computing"] * len(tickers),
            "ticker": tickers,
            "company_name": [f"{t} Corp" for t in tickers],
            "rationale": ["x"] * len(tickers),
            "gemini_confidence": [0.9] * len(tickers),
            "market_cap": [1e9] * len(tickers),
            "gates_passed": [["tenk"]] * len(tickers),
            "gates_passed_str": ["tenk"] * len(tickers),
            "n_gates_passed": [1] * len(tickers),
            "gates_failed": [[]] * len(tickers),
            "gates_failed_str": [""] * len(tickers),
            "n_gates_failed": [0] * len(tickers),
            "gates_unknown": [[]] * len(tickers),
            "gates_unknown_str": [""] * len(tickers),
            "n_gates_unknown": [0] * len(tickers),
            "verified": [True] * len(tickers),
        }
    )


# --- Composer rule -------------------------------------------------------


class TestWeightedScore(unittest.TestCase):
    def test_all_positive_clips_to_5(self):
        # 2*1 (insider) + 1+1+1 = 5
        self.assertEqual(
            scorer.compose_weighted_score(
                insider_positive=True,
                fcff_positive=True,
                valuation_positive=True,
                technicals_positive=True,
            ),
            5,
        )

    def test_all_negative_floors_to_1(self):
        self.assertEqual(
            scorer.compose_weighted_score(
                insider_positive=False,
                fcff_positive=False,
                valuation_positive=False,
                technicals_positive=False,
            ),
            1,
        )

    def test_insider_only_counts_double(self):
        # 2*1 = 2
        self.assertEqual(
            scorer.compose_weighted_score(
                insider_positive=True,
                fcff_positive=False,
                valuation_positive=False,
                technicals_positive=False,
            ),
            2,
        )

    def test_three_non_insider_signals_equals_three(self):
        # 1+1+1 = 3, no insider weight
        self.assertEqual(
            scorer.compose_weighted_score(
                insider_positive=False,
                fcff_positive=True,
                valuation_positive=True,
                technicals_positive=True,
            ),
            3,
        )


class TestSignalPositiveRules(unittest.TestCase):
    def test_insider_positive_when_net_buy(self):
        self.assertTrue(scorer.insider_is_positive(score_usd=50_001.0))
        self.assertFalse(scorer.insider_is_positive(score_usd=0.0))
        self.assertFalse(scorer.insider_is_positive(score_usd=-1.0))
        self.assertFalse(scorer.insider_is_positive(score_usd=None))

    def test_fcff_positive_when_above_sector_median(self):
        self.assertTrue(scorer.fcff_is_positive(sector_percentile=60.0))
        self.assertFalse(scorer.fcff_is_positive(sector_percentile=49.9))
        self.assertFalse(scorer.fcff_is_positive(sector_percentile=None))

    def test_valuation_positive_when_above_sector_median(self):
        self.assertTrue(scorer.valuation_is_positive(composite_percentile=51.0))
        self.assertFalse(scorer.valuation_is_positive(composite_percentile=49.0))
        self.assertFalse(scorer.valuation_is_positive(composite_percentile=None))

    def test_technicals_positive_in_healthy_range(self):
        # RSI in [30, 70] AND price within 15% of MA50.
        self.assertTrue(scorer.technicals_are_positive(rsi=50.0, ma_distance_pct=2.0))
        self.assertFalse(scorer.technicals_are_positive(rsi=85.0, ma_distance_pct=2.0))
        self.assertFalse(scorer.technicals_are_positive(rsi=50.0, ma_distance_pct=-25.0))
        self.assertFalse(scorer.technicals_are_positive(rsi=None, ma_distance_pct=2.0))


# --- End-to-end orchestrator --------------------------------------------


class TestScoreCandidatesEndToEnd(unittest.TestCase):
    def setUp(self):
        # Patch all 4 signal scorers + sector resolution + SimFin store fetch.
        self.patches = [
            patch.object(
                scorer.sector_peers,
                "get_industry_id",
                side_effect=lambda t: 101001 if t in ("QUBT", "IONQ") else None,
            ),
            patch.object(
                scorer.sector_peers, "industry_label", return_value=("Quantum SW", "Tech")
            ),
            patch.object(scorer.sector_peers, "iter_industry_peers", return_value=["QUBT", "IONQ"]),
            patch.object(
                scorer.insider_signal,
                "score_insider",
                side_effect=lambda *, ticker, asof, peers, **kw: {
                    "QUBT": {"score_usd": -100.0, "sector_percentile": 0.0},
                    "IONQ": {"score_usd": 200_000.0, "sector_percentile": 100.0},
                }[ticker],
            ),
            patch.object(
                scorer.fcff_signal,
                "score_fcff",
                side_effect=lambda *, ticker, asof, peers, feature_fetcher: {
                    "QUBT": {"yield_pct": -5.0, "sector_percentile": 10.0},
                    "IONQ": {"yield_pct": 4.0, "sector_percentile": 80.0},
                }[ticker],
            ),
            patch.object(
                scorer.valuation_signal,
                "score_valuation",
                side_effect=lambda *, ticker, asof, peers, feature_fetcher: {
                    "QUBT": {
                        "pe": None,
                        "ps": 30.0,
                        "ev_rev": 32.0,
                        "fcf_margin": -0.5,
                        "composite_sector_percentile": 10.0,
                    },
                    "IONQ": {
                        "pe": None,
                        "ps": 18.0,
                        "ev_rev": 20.0,
                        "fcf_margin": -0.2,
                        "composite_sector_percentile": 75.0,
                    },
                }[ticker],
            ),
            patch.object(
                scorer.technicals_signal,
                "score_technicals",
                side_effect=lambda *, ticker, asof, loader: {
                    "QUBT": {
                        "rsi": 65.0,
                        "ma50_distance_pct": 8.0,
                        "atr_pct": 5.0,
                        "volume_zscore": 1.5,
                        "summary": "RSI 65 / MA50 +8.0% / ATR 5.0% / volZ 1.5",
                    },
                    "IONQ": {
                        "rsi": 80.0,
                        "ma50_distance_pct": 25.0,
                        "atr_pct": 7.0,
                        "volume_zscore": 2.5,
                        "summary": "RSI 80 / MA50 +25.0% / ATR 7.0% / volZ 2.5",
                    },
                }[ticker],
            ),
            # Stub out the feature_fetcher / OHLCV loader factories so the
            # orchestrator builds something the patched signal scorers receive.
            patch.object(scorer, "_build_feature_fetcher", return_value=lambda t, asof: {}),
            patch.object(
                scorer, "_build_ohlcv_loader", return_value=lambda t, asof: pd.DataFrame()
            ),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_returns_df_with_all_new_columns(self):
        candidates = _candidates_df(["QUBT", "IONQ"])
        out = scorer.score_candidates(candidates, asof=dt.date(2026, 4, 14))
        for col in (
            "industry_id",
            "industry_name",
            "sector_name",
            "insider_score_usd",
            "insider_score_sector_percentile",
            "fcff_yield_pct",
            "fcff_yield_sector_percentile",
            "valuation_pe",
            "valuation_ps",
            "valuation_ev_rev",
            "valuation_fcf_margin",
            "valuation_composite_sector_percentile",
            "technical_rsi",
            "technical_ma50_distance_pct",
            "technical_atr_pct",
            "technical_volume_zscore",
            "technicals_summary_str",
            "layer4_weighted_score",
        ):
            self.assertIn(col, out.columns, f"missing column {col}")

    def test_preserves_phase_c_columns(self):
        candidates = _candidates_df(["QUBT", "IONQ"])
        out = scorer.score_candidates(candidates, asof=dt.date(2026, 4, 14))
        for col in ("theme", "ticker", "verified", "gates_passed_str"):
            self.assertIn(col, out.columns)
        self.assertEqual(list(out["ticker"]), ["QUBT", "IONQ"])

    def test_weighted_score_reflects_signal_alignment(self):
        candidates = _candidates_df(["QUBT", "IONQ"])
        out = scorer.score_candidates(candidates, asof=dt.date(2026, 4, 14))
        qubt = out[out["ticker"] == "QUBT"].iloc[0]
        ionq = out[out["ticker"] == "IONQ"].iloc[0]
        # QUBT: insider neg, fcff sub-median, valuation sub-median, technicals
        # ok (RSI 65 in band, MA50 +8% within ±15%). Only technicals positive
        # -> weighted = clip(0+0+0+1, 1, 5) = 1.
        self.assertEqual(int(qubt["layer4_weighted_score"]), 1)
        # IONQ: insider pos (2), fcff pos (1), valuation pos (1), technicals
        # not positive (RSI 80 too high). -> 2+1+1+0 = 4.
        self.assertEqual(int(ionq["layer4_weighted_score"]), 4)


class TestScoreCandidatesIsResilientToSignalExceptions(unittest.TestCase):
    def setUp(self):
        # Patch sector + factory stubs as in the end-to-end test.
        self.patches = [
            patch.object(scorer.sector_peers, "get_industry_id", return_value=101001),
            patch.object(
                scorer.sector_peers, "industry_label", return_value=("Quantum SW", "Tech")
            ),
            patch.object(scorer.sector_peers, "iter_industry_peers", return_value=["QUBT"]),
            patch.object(scorer, "_build_feature_fetcher", return_value=lambda t, asof: None),
            patch.object(
                scorer, "_build_ohlcv_loader", return_value=lambda t, asof: pd.DataFrame()
            ),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_insider_signal_exception_does_not_abort_batch(self):
        # Insider raises → row still emitted with insider_* = NaN; other 3
        # signals run normally (return their default "no data" shapes).
        with patch.object(
            scorer.insider_signal,
            "score_insider",
            side_effect=RuntimeError("form4 parquet corrupted"),
        ):
            out = scorer.score_candidates(_candidates_df(["QUBT"]), asof=dt.date(2026, 4, 14))
        self.assertEqual(len(out), 1)
        row = out.iloc[0]
        self.assertTrue(pd.isna(row["insider_score_usd"]))
        # weighted_score floored at 1 — single-signal exception ≠ batch abort.
        self.assertEqual(int(row["layer4_weighted_score"]), 1)

    def test_fcff_signal_exception_does_not_abort_batch(self):
        with patch.object(
            scorer.fcff_signal,
            "score_fcff",
            side_effect=RuntimeError("simfin row missing"),
        ):
            out = scorer.score_candidates(_candidates_df(["QUBT"]), asof=dt.date(2026, 4, 14))
        self.assertEqual(len(out), 1)
        self.assertTrue(pd.isna(out.iloc[0]["fcff_yield_pct"]))


class TestOhlcvLoaderDiskCache(unittest.TestCase):
    def test_reads_from_parquet_cache_when_present(self):
        import tempfile
        from pathlib import Path

        cached_df = pd.DataFrame(
            {"open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5], "volume": [1000.0]},
            index=pd.DatetimeIndex(["2026-04-10"]),
        )
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            (cache_dir / "QUBT.parquet").parent.mkdir(parents=True, exist_ok=True)
            cached_df.to_parquet(cache_dir / "QUBT.parquet")
            with (
                patch.object(scorer, "_THEMATIC_OHLCV_CACHE", cache_dir),
                patch("yfinance.Ticker", side_effect=AssertionError("must not fetch live")),
            ):
                loader = scorer._build_ohlcv_loader()
                df = loader("QUBT", dt.date(2026, 4, 14))
        self.assertEqual(len(df), 1)
        self.assertEqual(float(df["close"].iloc[0]), 1.5)

    def test_writes_parquet_after_live_fetch(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import MagicMock

        live_df = pd.DataFrame(
            {
                "Open": [10.0],
                "High": [11.0],
                "Low": [9.0],
                "Close": [10.5],
                "Volume": [5000.0],
            },
            index=pd.DatetimeIndex(["2026-04-10"]),
        )
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            fake_ticker = MagicMock()
            fake_ticker.history.return_value = live_df
            with (
                patch.object(scorer, "_THEMATIC_OHLCV_CACHE", cache_dir),
                patch("yfinance.Ticker", return_value=fake_ticker),
            ):
                loader = scorer._build_ohlcv_loader()
                _ = loader("RGTI", dt.date(2026, 4, 14))
            self.assertTrue((cache_dir / "RGTI.parquet").exists())


class TestFeatureFetcherFallback(unittest.TestCase):
    def test_preload_abort_returns_stub_fetcher_not_raises(self):
        # SimFinFundamentalsStore.preload raises (e.g. <50% coverage) →
        # _build_feature_fetcher returns a fetcher that always yields None
        # instead of propagating. Layer 4 stays alive on poor-coverage cohorts.
        with patch("alphalens.data.store.simfin.SimFinFundamentalsStore") as mock_store_cls:
            mock_store = mock_store_cls.return_value
            mock_store.preload.side_effect = RuntimeError("SimFin <50% coverage")
            fetcher = scorer._build_feature_fetcher(["A", "B"])
        self.assertIsNone(fetcher("A", dt.date(2026, 5, 15)))
        self.assertIsNone(fetcher("B", dt.date(2026, 5, 15)))


class TestScoreCandidatesUnknownIndustry(unittest.TestCase):
    def test_score_is_floor_when_industry_cannot_be_resolved(self):
        with (
            patch.object(scorer.sector_peers, "get_industry_id", return_value=None),
            patch.object(scorer, "_build_feature_fetcher", return_value=lambda t, asof: None),
            patch.object(
                scorer, "_build_ohlcv_loader", return_value=lambda t, asof: pd.DataFrame()
            ),
        ):
            out = scorer.score_candidates(_candidates_df(["UNKN"]), asof=dt.date(2026, 4, 14))
        row = out.iloc[0]
        self.assertTrue(pd.isna(row["industry_id"]))
        self.assertEqual(int(row["layer4_weighted_score"]), 1)


if __name__ == "__main__":
    unittest.main()
