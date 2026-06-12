import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from alphalens_pipeline.thematic.argumentation import generator, orchestrator


def _scored_df():
    """Minimal Phase D-shaped DataFrame with 3 rows: 1 unverified + 2 verified."""
    return pd.DataFrame(
        [
            {
                "theme": "quantum_computing",
                "ticker": "QUBT",
                "company_name": "Quantum Computing Inc",
                "rationale": "Pure-play",
                "llm_confidence": 0.85,
                "market_cap": 1.78e9,
                "gates_passed_str": "tenk,press",
                "verified": True,
                "industry_id": 101001,
                "industry_name": "Computer Hardware",
                "sector_name": "Technology",
                "insider_score_usd": 0.0,
                "insider_score_sector_percentile": 50.0,
                "fcff_yield_pct": None,
                "fcff_yield_sector_percentile": None,
                "valuation_ps": 30.0,
                "valuation_ev_rev": 32.0,
                "valuation_fcf_margin": -0.5,
                "valuation_composite_sector_percentile": 1.0,
                "technical_rsi": 60.0,
                "technical_ma50_distance_pct": 4.1,
                "technical_atr_pct": 6.6,
                "technical_volume_zscore": 3.8,
                "technicals_summary_str": "RSI 60 / MA50 +4.1%",
                "layer4_weighted_score": 2,
            },
            {
                "theme": "quantum_computing",
                "ticker": "IONQ",
                "company_name": "IonQ Inc",
                "rationale": "Trapped-ion",
                "llm_confidence": 0.95,
                "market_cap": 19.4e9,
                "gates_passed_str": "tenk,press",
                "verified": True,
                "industry_id": 101001,
                "industry_name": "Computer Hardware",
                "sector_name": "Technology",
                "insider_score_usd": 250_000.0,
                "insider_score_sector_percentile": 100.0,
                "fcff_yield_pct": 4.0,
                "fcff_yield_sector_percentile": 80.0,
                "valuation_ps": 18.0,
                "valuation_ev_rev": 20.0,
                "valuation_fcf_margin": -0.2,
                "valuation_composite_sector_percentile": 75.0,
                "technical_rsi": 55.0,
                "technical_ma50_distance_pct": 5.0,
                "technical_atr_pct": 4.0,
                "technical_volume_zscore": 1.0,
                "technicals_summary_str": "RSI 55 / MA50 +5.0%",
                "layer4_weighted_score": 4,
            },
            {
                # Unverified — should be skipped by orchestrator (no brief).
                "theme": "quantum_computing",
                "ticker": "MADEUP",
                "company_name": "Made Up Corp",
                "rationale": "halluc",
                "llm_confidence": 0.3,
                "market_cap": 5e8,
                "gates_passed_str": "",
                "verified": False,
                "industry_id": 101001,
                "industry_name": "Computer Hardware",
                "sector_name": "Technology",
                "insider_score_usd": None,
                "insider_score_sector_percentile": None,
                "fcff_yield_pct": None,
                "fcff_yield_sector_percentile": None,
                "valuation_ps": None,
                "valuation_ev_rev": None,
                "valuation_fcf_margin": None,
                "valuation_composite_sector_percentile": None,
                "technical_rsi": None,
                "technical_ma50_distance_pct": None,
                "technical_atr_pct": None,
                "technical_volume_zscore": None,
                "technicals_summary_str": "no data",
                "layer4_weighted_score": 1,
            },
        ]
    )


_FAKE_BRIEF_FLASH = {
    "tldr": "QUBT pure-play.",
    "supply_chain_reasoning": "Ising adoption raises demand for QUBT photonic processors.",
    "bear_summary": "Pre-revenue; zero insider buying.",
    "catalyst_failure_exit": "Exit if NVIDIA drops quantum roadmap.",
    "entry_price_note": "prefer 5-10 bps below current.",
    "model_used": generator.FLASH_MODEL,
}

_FAKE_BRIEF_PRO = {
    "tldr": "IONQ leads trapped-ion quantum.",
    "supply_chain_reasoning": "IonQ's trapped-ion roadmap aligns with Ising error correction.",
    "bear_summary": "Mega-cap risk, no FCFF, dilution.",
    "catalyst_failure_exit": "Exit on competitor product launch.",
    "entry_price_note": "wait for RSI pullback.",
    "model_used": generator.PRO_MODEL,
}


def _og_html(title):
    return f'<html><head><meta property="og:title" content="{title}"></head><body>x</body></html>'


_GDELT_MANGLED = (
    "Scientists are fast-tracking 3 Ebola vaccines in hopes of "
    "shortening the outbreak when could they be ready?"
)
_PUBLISHER_CLEAN = (
    "3 new Ebola vaccines are being fast-tracked amid the current "
    "outbreak — when could they be ready?"
)


class TestEnrichEventTitles(unittest.TestCase):
    def test_replaces_title_with_canonical_og_title(self):
        df = pd.DataFrame(
            [{"source_event_url": "https://pub.test/a", "source_event_title": _GDELT_MANGLED}]
        )
        out = orchestrator._enrich_event_titles(df, fetcher=lambda url: _og_html(_PUBLISHER_CLEAN))
        self.assertEqual(out.at[0, "source_event_title"], _PUBLISHER_CLEAN)
        self.assertIn("—", out.at[0, "source_event_title"])

    def test_keeps_original_when_no_url(self):
        df = pd.DataFrame([{"source_event_url": "", "source_event_title": _GDELT_MANGLED}])
        out = orchestrator._enrich_event_titles(df, fetcher=lambda url: _og_html("anything"))
        self.assertEqual(out.at[0, "source_event_title"], _GDELT_MANGLED)

    def test_noop_when_no_url_column(self):
        df = pd.DataFrame([{"source_event_title": _GDELT_MANGLED}])
        out = orchestrator._enrich_event_titles(df, fetcher=lambda url: _og_html("x"))
        self.assertEqual(out.at[0, "source_event_title"], _GDELT_MANGLED)

    def test_noop_when_title_column_absent(self):
        # source_event_url present but source_event_title column missing must not
        # KeyError (malformed upstream frame).
        df = pd.DataFrame([{"source_event_url": "https://pub.test/a"}])
        out = orchestrator._enrich_event_titles(df, fetcher=lambda url: _og_html(_PUBLISHER_CLEAN))
        self.assertEqual(list(out.columns), ["source_event_url"])

    def test_noop_when_disabled(self):
        df = pd.DataFrame(
            [{"source_event_url": "https://pub.test/a", "source_event_title": _GDELT_MANGLED}]
        )
        with patch.object(orchestrator, "_CANONICAL_TITLE_ENABLED", False):
            out = orchestrator._enrich_event_titles(
                df, fetcher=lambda url: _og_html(_PUBLISHER_CLEAN)
            )
        self.assertEqual(out.at[0, "source_event_title"], _GDELT_MANGLED)


class TestGenerateBriefs(unittest.TestCase):
    def test_skips_unverified_candidates(self):
        with patch.object(
            orchestrator,
            "_brief_for_row",
            side_effect=lambda row, **kw: (
                (_FAKE_BRIEF_PRO if row["ticker"] == "IONQ" else _FAKE_BRIEF_FLASH),
                None,
            ),
        ):
            with tempfile.TemporaryDirectory() as tmp:
                out = orchestrator.generate_briefs(
                    _scored_df(),
                    asof=dt.date(2026, 4, 14),
                    output_dir=Path(tmp),
                )
        # MADEUP (verified=False) skipped; QUBT + IONQ retained.
        self.assertEqual(set(out["ticker"]), {"QUBT", "IONQ"})

    def test_canonical_title_enrichment_reaches_output(self):
        df = _scored_df()
        df["source_event_url"] = "https://pub.test/a"
        df["source_event_title"] = _GDELT_MANGLED

        def fake_canonical(url, *, fallback, **kw):
            return _PUBLISHER_CLEAN if url else fallback

        with (
            patch.object(
                orchestrator, "_brief_for_row", side_effect=lambda row, **kw: (None, None)
            ),
            patch(
                "alphalens_pipeline.thematic.sources.canonical_title.canonical_title_for",
                side_effect=fake_canonical,
            ),
        ):
            with tempfile.TemporaryDirectory() as tmp:
                out = orchestrator.generate_briefs(
                    df, asof=dt.date(2026, 4, 14), output_dir=Path(tmp)
                )
        # Every verified row's displayed title is now the clean publisher headline.
        self.assertTrue((out["source_event_title"] == _PUBLISHER_CLEAN).all())

    def test_routes_pro_vs_flash_by_weighted_score(self):
        captured_models: dict[str, str] = {}

        def fake_brief(row, **kw):
            brief = _FAKE_BRIEF_PRO if row["layer4_weighted_score"] >= 4 else _FAKE_BRIEF_FLASH
            captured_models[row["ticker"]] = brief["model_used"]
            return brief, None

        with patch.object(orchestrator, "_brief_for_row", side_effect=fake_brief):
            with tempfile.TemporaryDirectory() as tmp:
                orchestrator.generate_briefs(
                    _scored_df(), asof=dt.date(2026, 4, 14), output_dir=Path(tmp)
                )
        self.assertEqual(captured_models["QUBT"], generator.FLASH_MODEL)  # score 2 → Flash
        self.assertEqual(captured_models["IONQ"], generator.PRO_MODEL)  # score 4 → Pro

    def test_writes_parquet_with_structured_brief_columns(self):
        with patch.object(orchestrator, "_brief_for_row", return_value=(_FAKE_BRIEF_FLASH, None)):
            with tempfile.TemporaryDirectory() as tmp:
                output_dir = Path(tmp)
                out = orchestrator.generate_briefs(
                    _scored_df(), asof=dt.date(2026, 4, 14), output_dir=output_dir
                )
        # Re-enter the temp dir for assertions before it's cleaned up.
        # (output_dir lifetime extends through the with block but assertions live outside.)
        self.assertEqual(len(out), 2)
        self.assertIn("brief_tldr", out.columns)
        self.assertIn("brief_model_used", out.columns)
        # The markdown blob was retired (2026-05-26): structured brief_*
        # columns feed the API + UI directly, no rendered string is stored.
        self.assertNotIn("brief_full_md", out.columns)

    def test_writes_parquet_only_no_markdown_file(self):
        with patch.object(orchestrator, "_brief_for_row", return_value=(_FAKE_BRIEF_FLASH, None)):
            with tempfile.TemporaryDirectory() as tmp:
                output_dir = Path(tmp)
                orchestrator.generate_briefs(
                    _scored_df(), asof=dt.date(2026, 4, 14), output_dir=output_dir
                )
                self.assertTrue((output_dir / "2026-04-14.parquet").exists())
                # The per-day .md bundle is no longer emitted.
                self.assertFalse((output_dir / "2026-04-14.md").exists())

    def test_brief_failure_keeps_deterministic_columns(self):
        # _brief_for_row returns None (LLM failed). With the markdown blob
        # gone, graceful degradation is inherent: the deterministic Phase D
        # signal columns are always persisted, so a Flash truncation never
        # hides the quantitative signal (2026-05-17 QUBT incident). The
        # brief_* prose columns simply stay None.
        with patch.object(orchestrator, "_brief_for_row", return_value=(None, None)):
            with tempfile.TemporaryDirectory() as tmp:
                out = orchestrator.generate_briefs(
                    _scored_df(), asof=dt.date(2026, 4, 14), output_dir=Path(tmp)
                )
        self.assertEqual(len(out), 2)
        # Deterministic Phase D facts survive the LLM failure.
        for col in ("ticker", "insider_score_usd", "layer4_weighted_score"):
            self.assertIn(col, out.columns)
        for _, row in out.iterrows():
            self.assertIsNone(row["brief_tldr"])

    def test_attrs_contain_per_model_counts(self):
        def fake_brief(row, **kw):
            brief = _FAKE_BRIEF_PRO if row["layer4_weighted_score"] >= 4 else _FAKE_BRIEF_FLASH
            return brief, None

        with patch.object(orchestrator, "_brief_for_row", side_effect=fake_brief):
            with tempfile.TemporaryDirectory() as tmp:
                out = orchestrator.generate_briefs(
                    _scored_df(), asof=dt.date(2026, 4, 14), output_dir=Path(tmp)
                )
        self.assertEqual(out.attrs.get("n_pro"), 1)
        self.assertEqual(out.attrs.get("n_flash"), 1)


class TestEarningsDatePropagation(unittest.TestCase):
    """The fetched next_earnings_date must be persisted to the brief
    parquet as ``next_earnings_date`` — not just passed to the LLM prompt."""

    def test_next_earnings_date_persisted_to_parquet(self):
        # _brief_for_row returns (brief, next_earnings_iso_str). Test
        # exercises orchestrator's promise to persist the 2nd tuple slot
        # into the brief parquet as ``next_earnings_date``.
        with patch.object(
            orchestrator, "_brief_for_row", return_value=(_FAKE_BRIEF_FLASH, "2026-05-08")
        ):
            with tempfile.TemporaryDirectory() as tmp:
                out = orchestrator.generate_briefs(
                    _scored_df(), asof=dt.date(2026, 4, 14), output_dir=Path(tmp)
                )
        self.assertIn("next_earnings_date", out.columns)
        for _, row in out.iterrows():
            self.assertEqual(row["next_earnings_date"], "2026-05-08")

    def test_next_earnings_date_none_when_fetch_returns_none(self):
        with patch.object(orchestrator, "_brief_for_row", return_value=(_FAKE_BRIEF_FLASH, None)):
            with tempfile.TemporaryDirectory() as tmp:
                out = orchestrator.generate_briefs(
                    _scored_df(), asof=dt.date(2026, 4, 14), output_dir=Path(tmp)
                )
        for _, row in out.iterrows():
            self.assertIsNone(row["next_earnings_date"])


class TestEmptyScoredFrame(unittest.TestCase):
    def test_empty_scored_writes_typed_empty_parquet_and_zero_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = orchestrator.generate_briefs(
                pd.DataFrame(), asof=dt.date(2026, 4, 14), output_dir=Path(tmp)
            )
            self.assertEqual(len(out), 0)
            # Schema present so downstream readers don't crash on zero-column
            # frames. ``next_earnings_date`` included so the empty schema mirrors
            # the populated schema (zen review 2026-05-18: L1 schema asymmetry).
            for col in ("ticker", "brief_trade_setup", "next_earnings_date"):
                self.assertIn(col, out.columns)
            self.assertNotIn("brief_full_md", out.columns)
            self.assertTrue((Path(tmp) / "2026-04-14.parquet").exists())
            self.assertFalse((Path(tmp) / "2026-04-14.md").exists())
            sidecar_path = Path(tmp) / "2026-04-14.meta.json"
            self.assertTrue(sidecar_path.exists())
            import json

            meta = json.loads(sidecar_path.read_text())
            self.assertEqual(meta["n_pro"], 0)
            self.assertEqual(meta["n_flash"], 0)

    def test_none_scored_writes_typed_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = orchestrator.generate_briefs(
                None, asof=dt.date(2026, 4, 14), output_dir=Path(tmp)
            )
            self.assertEqual(len(out), 0)
            self.assertIn("ticker", out.columns)

    def test_all_unverified_scored_writes_typed_empty(self):
        df = _scored_df()
        df = df[df["ticker"] == "MADEUP"].copy()  # only the unverified row
        with tempfile.TemporaryDirectory() as tmp:
            out = orchestrator.generate_briefs(df, asof=dt.date(2026, 4, 14), output_dir=Path(tmp))
            self.assertEqual(len(out), 0)


class TestSidecarPersistence(unittest.TestCase):
    def test_sidecar_records_per_model_counts(self):
        def fake_brief(row, **kw):
            brief = _FAKE_BRIEF_PRO if row["layer4_weighted_score"] >= 4 else _FAKE_BRIEF_FLASH
            return brief, None

        with patch.object(orchestrator, "_brief_for_row", side_effect=fake_brief):
            with tempfile.TemporaryDirectory() as tmp:
                orchestrator.generate_briefs(
                    _scored_df(), asof=dt.date(2026, 4, 14), output_dir=Path(tmp)
                )
                import json

                meta = json.loads((Path(tmp) / "2026-04-14.meta.json").read_text())
                self.assertEqual(meta["n_pro"], 1)
                self.assertEqual(meta["n_flash"], 1)
                self.assertEqual(meta["asof"], "2026-04-14")


class TestDedupOnMerge(unittest.TestCase):
    def test_duplicate_tickers_in_scored_dont_cause_cartesian(self):
        # Build a scored df with duplicate verified=True QUBT rows; output
        # should keep just one brief per ticker.
        df = _scored_df()
        verified = df[df["verified"]].copy()
        duped = pd.concat([verified, verified.iloc[[0]]], ignore_index=True)
        with patch.object(orchestrator, "_brief_for_row", return_value=(_FAKE_BRIEF_FLASH, None)):
            with tempfile.TemporaryDirectory() as tmp:
                out = orchestrator.generate_briefs(
                    duped, asof=dt.date(2026, 4, 14), output_dir=Path(tmp)
                )
        # Without dedup, merge on ticker would Cartesian — without it
        # QUBT (the duplicate) would produce 2*1 = 2 rows. With dedup, 1.
        ticker_counts = out["ticker"].value_counts().to_dict()
        for ticker, count in ticker_counts.items():
            self.assertEqual(count, 1, f"{ticker} duplicated to {count} rows")


class TestCacheOnlyOhlcvLoader(unittest.TestCase):
    def test_truncates_rows_after_asof(self):
        asof = dt.date(2026, 4, 14)
        idx = pd.to_datetime(["2026-04-12", "2026-04-14", "2026-04-15"])
        df = pd.DataFrame(
            {
                "open": [1.0, 2.0, 3.0],
                "high": [1.0, 2.0, 3.0],
                "low": [1.0, 2.0, 3.0],
                "close": [1.0, 2.0, 3.0],
                "volume": [1.0, 1.0, 1.0],
            },
            index=idx,
        )
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            df.to_parquet(cache_dir / f"FOO_{asof.isoformat()}.parquet")
            loader = orchestrator._cache_only_ohlcv_loader(cache_dir=cache_dir)
            out = loader("FOO", asof)
            # The post-asof 2026-04-15 row must be dropped — parity with the
            # Layer-4 scorer loader so Phase D and Phase E see identical bars.
            self.assertEqual(len(out), 2)
            self.assertLessEqual(out.index.max(), pd.Timestamp(asof))

    def test_missing_cache_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            loader = orchestrator._cache_only_ohlcv_loader(cache_dir=Path(tmp))
            self.assertTrue(loader("NOPE", dt.date(2026, 4, 14)).empty)


if __name__ == "__main__":
    unittest.main()
