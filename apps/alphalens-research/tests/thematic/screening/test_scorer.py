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
            "llm_confidence": [0.9] * len(tickers),
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
        # Insider is intentionally absent — held out of layer4 pending Phase 4.
        base = {
            "fcff_positive": False,
            "magic_formula_top_quartile": False,
            "deep_drawdown_reversal": False,
            "technicals_positive": False,
            "catalyst_strength": 0.0,
        }
        base.update(overrides)
        return base

    def test_all_positive_with_strong_catalyst_clips_to_5(self):
        # 1 (fcff) + 1 (MF) + 1 (tech) + 2 (catalyst floor strong) = 5
        self.assertEqual(
            scorer.compose_weighted_score(
                **self._kw(
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

    def test_insider_is_not_a_component(self):
        # Insider held out of the ordering score (Phase 2): compose must reject
        # an insider_positive kwarg, and a lone fcff signal scores 1 (no 2×).
        with self.assertRaises(TypeError):
            scorer.compose_weighted_score(**self._kw(), insider_positive=True)  # type: ignore[call-arg]
        self.assertEqual(scorer.compose_weighted_score(**self._kw(fcff_positive=True)), 1)

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
            patch.object(
                scorer.sector_peers,
                "iter_industry_peers_fallback",
                return_value=(["QUBT", "IONQ"], "sic4"),
            ),
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
            "insider_signal_version",
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
            "catalyst_config_version",
            "technical_rsi",
            "technical_ma50_distance_pct",
            "technical_atr_pct",
            "technical_volume_zscore",
            "technicals_summary_str",
            "layer4_weighted_score",
            "atr_penalty",
            "selection_score",
            "scorer_config_version",
        ):
            self.assertIn(col, out.columns, f"missing column {col}")

    def test_scorer_config_version_is_stamped(self):
        from alphalens_pipeline.thematic.screening import selection_score as ss_mod

        candidates = _candidates_df(["QUBT", "IONQ"])
        out = scorer.score_candidates(candidates, asof=dt.date(2026, 4, 14))
        for _, row in out.iterrows():
            self.assertEqual(row["scorer_config_version"], ss_mod.SCORER_CONFIG_VERSION)

    def test_catalyst_config_version_is_stamped(self):
        # Fixture patches find_trigger_event to None, so this also proves the
        # stamp lands on NO-catalyst rows (poolability key must be universal).
        from alphalens_pipeline.thematic.screening import catalyst_signals as cs_mod

        candidates = _candidates_df(["QUBT", "IONQ"])
        out = scorer.score_candidates(candidates, asof=dt.date(2026, 4, 14))
        for _, row in out.iterrows():
            self.assertEqual(row["catalyst_config_version"], cs_mod.catalyst_config_version())

    def test_selection_score_equals_layer4_minus_atr_penalty(self):
        candidates = _candidates_df(["QUBT", "IONQ"])
        out = scorer.score_candidates(candidates, asof=dt.date(2026, 4, 14))
        for _, row in out.iterrows():
            expected = float(row["layer4_weighted_score"]) - float(row["atr_penalty"])
            self.assertAlmostEqual(float(row["selection_score"]), expected, places=9)

    def test_atr_penalty_is_zero_when_atr_at_or_below_ramp_lo(self):
        # QUBT mock: technical_atr_pct = 5.0, which is <= ATR_RAMP_LO (5.77)
        candidates = _candidates_df(["QUBT"])
        out = scorer.score_candidates(candidates, asof=dt.date(2026, 4, 14))
        row = out.iloc[0]
        self.assertAlmostEqual(float(row["atr_penalty"]), 0.0, places=9)
        self.assertAlmostEqual(
            float(row["selection_score"]), float(row["layer4_weighted_score"]), places=9
        )

    def test_scorer_config_version_survives_parquet_roundtrip(self):
        import tempfile
        from pathlib import Path

        candidates = _candidates_df(["QUBT"])
        out = scorer.score_candidates(candidates, asof=dt.date(2026, 4, 14))
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "scored.parquet"
            out.to_parquet(p, index=False)
            loaded = __import__("pandas").read_parquet(p)
        self.assertIn("scorer_config_version", loaded.columns)
        from alphalens_pipeline.thematic.screening import selection_score as ss_mod

        self.assertEqual(loaded.iloc[0]["scorer_config_version"], ss_mod.SCORER_CONFIG_VERSION)

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
        # Insider is HELD OUT of layer4 (Phase 2) — it no longer contributes.
        # QUBT: fcff sub-median, MF rank NaN (health gate FAIL), reversal False,
        # technicals ok. -> clip(0+0+1+0, 1, 5) = 1.
        self.assertEqual(int(qubt["layer4_weighted_score"]), 1)
        # IONQ: fcff pos (1), MF top-quartile False (cohort n=1 → rank NaN →
        # quartile False), reversal False, technicals not positive (RSI 80 too
        # high). Insider no longer adds its old +2. -> clip(1+0+0+0, 1, 5) = 1.
        self.assertEqual(int(ionq["layer4_weighted_score"]), 1)


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
            patch.object(
                scorer.sector_peers,
                "iter_industry_peers_fallback",
                return_value=(["QUBT"], "sic4"),
            ),
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
    """The loader now delegates to the canonical ``YFinanceClient`` (disk +
    in-process cache + throttle/retry/stale-fallback live there; the client's
    own ``test_yfinance_client.py`` covers the retry/stale paths). These tests
    pin the delegation contract end-to-end through ``_build_ohlcv_loader``."""

    def setUp(self):
        import tempfile
        from pathlib import Path

        from alphalens_pipeline.data.alt_data import yfinance_client as yc

        self._yc = yc
        self._td = tempfile.TemporaryDirectory()
        self._cache_dir = Path(self._td.name)
        yc._reset_default_client_for_tests()
        yc._DEFAULT_CLIENT = yc.YFinanceClient(
            min_interval_s=0.0, cache_dir=self._cache_dir, sleep=lambda _s: None
        )

    def tearDown(self):
        self._yc._reset_default_client_for_tests()
        self._td.cleanup()

    def test_reads_from_parquet_cache_when_present(self):
        cached_df = pd.DataFrame(
            {"open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5], "volume": [1000.0]},
            index=pd.DatetimeIndex(["2026-04-10"]),
        )
        asof = dt.date(2026, 4, 14)
        cached_df.to_parquet(self._cache_dir / f"QUBT_{asof.isoformat()}.parquet")
        with patch("yfinance.Ticker", side_effect=AssertionError("must not fetch live")):
            loader = scorer._build_ohlcv_loader()
            df = loader("QUBT", asof)
        self.assertEqual(len(df), 1)
        self.assertEqual(float(df["close"].iloc[0]), 1.5)

    def test_writes_parquet_after_live_fetch(self):
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
        fake_ticker = MagicMock()
        fake_ticker.history.return_value = live_df
        with patch("yfinance.Ticker", return_value=fake_ticker):
            loader = scorer._build_ohlcv_loader()
            _ = loader("RGTI", asof)
        self.assertTrue((self._cache_dir / f"RGTI_{asof.isoformat()}.parquet").exists())


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
        # Issue #197: thin-cohort label must surface so the UI can suppress
        # the percentile bar.
        self.assertEqual(row["peer_cohort_level"], "thin")


class TestPeerCohortLevelSurfaced(unittest.TestCase):
    """Issue #197: ``peer_cohort_level`` must propagate to brief output,
    and ``thin`` must null the percentile columns so the renderer falls
    back to the thin-cohort badge instead of a midpoint bar."""

    def _patch_signal_fixtures(self):
        return [
            patch.object(
                scorer.insider_signal,
                "score_insider",
                return_value={"score_usd": 100_000.0, "sector_percentile": 75.0},
            ),
            patch.object(
                scorer.fcff_signal,
                "score_fcff",
                return_value={"yield_pct": 5.0, "sector_percentile": 80.0},
            ),
            patch.object(
                scorer.valuation_signal,
                "score_valuation",
                return_value={
                    "pe": 15.0,
                    "ps": 2.0,
                    "ev_rev": 2.5,
                    "fcf_margin": 0.10,
                    "composite_sector_percentile": 70.0,
                },
            ),
            patch.object(
                scorer.technicals_signal,
                "score_technicals",
                return_value={
                    "rsi": 50.0,
                    "ma50_distance_pct": 2.0,
                    "atr_pct": 4.0,
                    "volume_zscore": 0.0,
                    "summary": "ok",
                },
            ),
            patch.object(scorer, "_build_feature_fetcher", return_value=lambda t, asof: None),
            patch.object(
                scorer, "_build_ohlcv_loader", return_value=lambda t, asof: pd.DataFrame()
            ),
            patch(
                "alphalens_pipeline.thematic.mapping.catalyst_resolver.find_trigger_event",
                return_value=None,
            ),
        ]

    def test_sic4_level_passes_through(self):
        ps = self._patch_signal_fixtures()
        ps += [
            patch.object(scorer.sector_peers, "get_industry_id", return_value=3674),
            patch.object(
                scorer.sector_peers,
                "industry_label",
                return_value=("Semiconductors", "Manufacturing"),
            ),
            patch.object(
                scorer.sector_peers,
                "iter_industry_peers_fallback",
                return_value=(["A", "B", "C", "D", "E", "F", "G", "H"], "sic4"),
            ),
        ]
        for p in ps:
            p.start()
        try:
            out = scorer.score_candidates(_candidates_df(["X"]), asof=dt.date(2026, 4, 14))
        finally:
            for p in ps:
                p.stop()
        row = out.iloc[0]
        self.assertEqual(row["peer_cohort_level"], "sic4")
        self.assertAlmostEqual(float(row["fcff_yield_sector_percentile"]), 80.0)

    def test_sic3_level_passes_through(self):
        ps = self._patch_signal_fixtures()
        ps += [
            patch.object(scorer.sector_peers, "get_industry_id", return_value=7372),
            patch.object(
                scorer.sector_peers,
                "industry_label",
                return_value=("Services-Prepackaged Software", "Services"),
            ),
            patch.object(
                scorer.sector_peers,
                "iter_industry_peers_fallback",
                return_value=(["P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8"], "sic3"),
            ),
        ]
        for p in ps:
            p.start()
        try:
            out = scorer.score_candidates(_candidates_df(["X"]), asof=dt.date(2026, 4, 14))
        finally:
            for p in ps:
                p.stop()
        row = out.iloc[0]
        self.assertEqual(row["peer_cohort_level"], "sic3")
        # Percentile preserved — sic3 is still trustworthy.
        self.assertAlmostEqual(float(row["fcff_yield_sector_percentile"]), 80.0)

    def test_thin_level_nulls_percentiles(self):
        ps = self._patch_signal_fixtures()
        ps += [
            patch.object(scorer.sector_peers, "get_industry_id", return_value=7380),
            patch.object(
                scorer.sector_peers,
                "industry_label",
                return_value=("Services-Misc Business Services", "Services"),
            ),
            patch.object(
                scorer.sector_peers,
                "iter_industry_peers_fallback",
                return_value=([], "thin"),
            ),
        ]
        for p in ps:
            p.start()
        try:
            out = scorer.score_candidates(_candidates_df(["DFIN"]), asof=dt.date(2026, 4, 14))
        finally:
            for p in ps:
                p.stop()
        row = out.iloc[0]
        self.assertEqual(row["peer_cohort_level"], "thin")
        # Percentile cohort fields null — the UI badge fires off this.
        self.assertTrue(pd.isna(row["fcff_yield_sector_percentile"]))
        self.assertTrue(pd.isna(row["insider_score_sector_percentile"]))
        self.assertTrue(pd.isna(row["valuation_composite_sector_percentile"]))
        # Candidate's own metrics still present — fcff yield is a candidate
        # property, independent of cohort.
        self.assertAlmostEqual(float(row["fcff_yield_pct"]), 5.0)
        self.assertAlmostEqual(float(row["insider_score_usd"]), 100_000.0)


if __name__ == "__main__":
    unittest.main()
