import datetime as dt
import unittest
from unittest.mock import patch

import pandas as pd
from alphalens_pipeline.thematic.screening import scorer


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
    def _kw(self, **overrides):
        base = {
            "insider_positive": False,
            "fcff_positive": False,
            "magic_formula_top_quartile": False,
            "deep_drawdown_reversal": False,
            "technicals_positive": False,
            "catalyst_strength": 0.0,
        }
        base.update(overrides)
        return base

    def test_all_positive_with_strong_catalyst_clips_to_5(self):
        # 2*1 + 1 + 1 + 1 + 2 (catalyst floor strong) = 7, clipped to 5
        self.assertEqual(
            scorer.compose_weighted_score(
                **self._kw(
                    insider_positive=True,
                    fcff_positive=True,
                    magic_formula_top_quartile=True,
                    technicals_positive=True,
                    catalyst_strength=0.90,
                )
            ),
            5,
        )

    def test_all_negative_floors_to_1(self):
        self.assertEqual(scorer.compose_weighted_score(**self._kw()), 1)

    def test_insider_only_counts_double(self):
        self.assertEqual(
            scorer.compose_weighted_score(**self._kw(insider_positive=True)),
            2,
        )

    def test_reversal_substitutes_for_magic_formula_in_value_slot(self):
        # MF false, reversal true → val_or_reversal = 1; +tech = 2
        self.assertEqual(
            scorer.compose_weighted_score(
                **self._kw(deep_drawdown_reversal=True, technicals_positive=True)
            ),
            2,
        )

    def test_strong_catalyst_adds_2_floor(self):
        # base 1 (tech only) + catalyst_floor 2 = 3
        self.assertEqual(
            scorer.compose_weighted_score(
                **self._kw(technicals_positive=True, catalyst_strength=0.80)
            ),
            3,
        )

    def test_moderate_catalyst_adds_1_floor(self):
        # 0.50 is mid-tier (≥ 0.45 threshold, < 0.70 strong).
        self.assertEqual(
            scorer.compose_weighted_score(
                **self._kw(technicals_positive=True, catalyst_strength=0.50)
            ),
            2,
        )

    def test_weak_catalyst_adds_no_floor(self):
        # 0.30 is below the 0.45 moderate threshold post-zen-tuning.
        self.assertEqual(
            scorer.compose_weighted_score(
                **self._kw(technicals_positive=True, catalyst_strength=0.30)
            ),
            1,
        )
        self.assertEqual(
            scorer.compose_weighted_score(
                **self._kw(technicals_positive=True, catalyst_strength=0.10)
            ),
            1,
        )

    def test_qubt_class_with_strong_catalyst_promotes_to_4(self):
        # Pre-profit thematic momentum: no insider, no fcff, no MF, but
        # deep_drawdown_reversal=True, technicals_positive=True, strong catalyst.
        # 0 + 0 + 1 (reversal) + 1 (tech) + 2 (catalyst floor) = 4. Matches the
        # 2026-05-18 NVDA→QUBT replay design goal (QUBT 1/5 → 4/5).
        self.assertEqual(
            scorer.compose_weighted_score(
                **self._kw(
                    deep_drawdown_reversal=True, technicals_positive=True, catalyst_strength=0.80
                )
            ),
            4,
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
        # Mocked industry ID is the real SEC SIC code for Semiconductors
        # (3674) so the fixture matches the post-SimFin reality, even though
        # the value is opaque to the scorer.
        self.patches = [
            patch.object(
                scorer.sector_peers,
                "get_industry_id",
                side_effect=lambda t: 3674 if t in ("QUBT", "IONQ") else None,
            ),
            patch.object(
                scorer.sector_peers,
                "industry_label",
                return_value=("Semiconductors & Related Devices", "Manufacturing"),
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
            # Stub feature_fetcher with realistic per-ticker SimFin shapes so
            # Magic Formula compute helpers can derive ROIC/ROE/EV-EBITDA.
            patch.object(
                scorer,
                "_build_feature_fetcher",
                return_value=lambda t, asof: {
                    "QUBT": {
                        "operating_income_ttm": -50_000_000.0,  # health-gate FAIL
                        "interest_expense_ttm": 1_000_000.0,
                        "net_income_ttm": -60_000_000.0,
                        "revenue_ttm": 5_000_000.0,
                        "da_ttm": 5_000_000.0,
                        "long_term_debt": 100_000_000.0,
                        "short_term_debt": 50_000_000.0,
                        "cash_and_equivalents": 200_000_000.0,
                        "total_equity": 400_000_000.0,
                        "price": 18.0,
                        "shares_outstanding": 100_000_000.0,
                    },
                    "IONQ": {
                        "operating_income_ttm": 80_000_000.0,
                        "interest_expense_ttm": 5_000_000.0,
                        "net_income_ttm": 60_000_000.0,
                        "revenue_ttm": 800_000_000.0,
                        "da_ttm": 20_000_000.0,
                        "long_term_debt": 100_000_000.0,
                        "short_term_debt": 50_000_000.0,
                        "cash_and_equivalents": 300_000_000.0,
                        "total_equity": 600_000_000.0,
                        "price": 22.0,
                        "shares_outstanding": 200_000_000.0,
                    },
                }[t],
            ),
            patch.object(
                scorer, "_build_ohlcv_loader", return_value=lambda t, asof: pd.DataFrame()
            ),
            # Catalyst lookup — return None so cs=0 and the existing test
            # invariants on weighted_score still hold (no catalyst lift).
            patch(
                "alphalens_pipeline.thematic.mapping.catalyst_resolver.find_trigger_event",
                return_value=None,
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
            "valuation_ev_ebitda",
            "valuation_fcf_margin",
            "valuation_composite_sector_percentile",
            "roic_pct",
            "roe_pct",
            "magic_formula_health_pass",
            "magic_formula_rank",
            "magic_formula_cohort_n",
            "deep_drawdown_reversal",
            "catalyst_strength",
            "catalyst_event_type",
            "catalyst_confidence",
            "technical_rsi",
            "technical_ma50_distance_pct",
            "technical_atr_pct",
            "technical_volume_zscore",
            "technicals_summary_str",
            "layer4_weighted_score",
        ):
            self.assertIn(col, out.columns, f"missing column {col}")

    def test_magic_formula_columns_reflect_health_and_rank(self):
        candidates = _candidates_df(["QUBT", "IONQ"])
        out = scorer.score_candidates(candidates, asof=dt.date(2026, 4, 14))
        qubt = out[out["ticker"] == "QUBT"].iloc[0]
        ionq = out[out["ticker"] == "IONQ"].iloc[0]
        # QUBT: EBIT negative → health gate FAIL.
        self.assertFalse(bool(qubt["magic_formula_health_pass"]))
        # IONQ: EBIT positive, modest leverage → health gate PASS.
        self.assertTrue(bool(ionq["magic_formula_health_pass"]))
        # Cohort n=1 survivor → small-cohort guard → rank=NaN for all rows.
        self.assertTrue(pd.isna(qubt["magic_formula_rank"]))
        self.assertTrue(pd.isna(ionq["magic_formula_rank"]))
        self.assertEqual(int(ionq["magic_formula_cohort_n"]), 1)

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
        # Catalyst is mocked to None → cs=0, no catalyst floor. Reversal is
        # False (no source_event_url in fixture). So formula matches the pre-
        # catalyst behaviour.
        # QUBT: insider neg, fcff sub-median, MF rank NaN (health gate FAIL),
        # reversal False, technicals ok. -> clip(0+0+0+0+1+0, 1, 5) = 1.
        self.assertEqual(int(qubt["layer4_weighted_score"]), 1)
        # IONQ: insider pos (2), fcff pos (1), MF top-quartile False (cohort
        # n=1 → rank NaN → quartile False), reversal False, technicals not
        # positive (RSI 80 too high). -> 2+1+0+0+0+0 = 3.
        self.assertEqual(int(ionq["layer4_weighted_score"]), 3)


class TestScoreCandidatesIsResilientToSignalExceptions(unittest.TestCase):
    def setUp(self):
        # Patch sector + factory stubs as in the end-to-end test.
        self.patches = [
            patch.object(scorer.sector_peers, "get_industry_id", return_value=3674),
            patch.object(
                scorer.sector_peers,
                "industry_label",
                return_value=("Semiconductors & Related Devices", "Manufacturing"),
            ),
            patch.object(scorer.sector_peers, "iter_industry_peers", return_value=["QUBT"]),
            patch.object(scorer, "_build_feature_fetcher", return_value=lambda t, asof: None),
            patch.object(
                scorer, "_build_ohlcv_loader", return_value=lambda t, asof: pd.DataFrame()
            ),
            patch(
                "alphalens_pipeline.thematic.mapping.catalyst_resolver.find_trigger_event",
                return_value=None,
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
        asof = dt.date(2026, 4, 14)
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            cache_path = cache_dir / f"QUBT_{asof.isoformat()}.parquet"
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cached_df.to_parquet(cache_path)
            with (
                patch.object(scorer, "_THEMATIC_OHLCV_CACHE", cache_dir),
                patch("yfinance.Ticker", side_effect=AssertionError("must not fetch live")),
            ):
                loader = scorer._build_ohlcv_loader()
                df = loader("QUBT", asof)
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
        asof = dt.date(2026, 4, 14)
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            fake_ticker = MagicMock()
            fake_ticker.history.return_value = live_df
            with (
                patch.object(scorer, "_THEMATIC_OHLCV_CACHE", cache_dir),
                patch("yfinance.Ticker", return_value=fake_ticker),
            ):
                loader = scorer._build_ohlcv_loader()
                _ = loader("RGTI", asof)
            self.assertTrue((cache_dir / f"RGTI_{asof.isoformat()}.parquet").exists())


class TestFeatureFetcherFallback(unittest.TestCase):
    def test_preload_abort_returns_stub_fetcher_not_raises(self):
        # EdgarFundamentalsStore.preload raises (e.g. SEC outage) →
        # _build_feature_fetcher returns a fetcher that always yields None
        # instead of propagating. Layer 4 stays alive on poor-coverage cohorts.
        with patch(
            "alphalens_pipeline.data.store.edgar_fundamentals.EdgarFundamentalsStore"
        ) as mock_store_cls:
            mock_store = mock_store_cls.return_value
            mock_store.preload.side_effect = RuntimeError("SEC EDGAR unreachable")
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
            patch(
                "alphalens_pipeline.thematic.mapping.catalyst_resolver.find_trigger_event",
                return_value=None,
            ),
        ):
            out = scorer.score_candidates(_candidates_df(["UNKN"]), asof=dt.date(2026, 4, 14))
        row = out.iloc[0]
        self.assertTrue(pd.isna(row["industry_id"]))
        self.assertEqual(int(row["layer4_weighted_score"]), 1)


if __name__ == "__main__":
    unittest.main()
