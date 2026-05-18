import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from alphalens.thematic.argumentation import generator, orchestrator


def _scored_df():
    """Minimal Phase D-shaped DataFrame with 3 rows: 1 unverified + 2 verified."""
    return pd.DataFrame(
        [
            {
                "theme": "quantum_computing",
                "ticker": "QUBT",
                "company_name": "Quantum Computing Inc",
                "rationale": "Pure-play",
                "gemini_confidence": 0.85,
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
                "gemini_confidence": 0.95,
                "market_cap": 19.4e9,
                "gates_passed_str": "etf,tenk,press",
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
                "gemini_confidence": 0.3,
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

    def test_writes_parquet_and_markdown_bundle(self):
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
        self.assertIn("brief_full_md", out.columns)
        self.assertIn("brief_model_used", out.columns)

    def test_writes_parquet_and_md_files_to_disk(self):
        with patch.object(orchestrator, "_brief_for_row", return_value=(_FAKE_BRIEF_FLASH, None)):
            with tempfile.TemporaryDirectory() as tmp:
                output_dir = Path(tmp)
                orchestrator.generate_briefs(
                    _scored_df(), asof=dt.date(2026, 4, 14), output_dir=output_dir
                )
                self.assertTrue((output_dir / "2026-04-14.parquet").exists())
                self.assertTrue((output_dir / "2026-04-14.md").exists())
                md = (output_dir / "2026-04-14.md").read_text()
                self.assertIn("2026-04-14", md)
                self.assertIn("QUBT", md)

    def test_brief_failure_still_renders_deterministic_signals(self):
        # _brief_for_row returns None (LLM failed) — orchestrator now
        # renders deterministic facts (ticker, catalyst, signal panel,
        # verified gates) via the graceful-degradation renderer so the
        # operator never loses visibility on Phase D data when Flash
        # truncates (2026-05-17 QUBT incident).
        with patch.object(orchestrator, "_brief_for_row", return_value=(None, None)):
            with tempfile.TemporaryDirectory() as tmp:
                out = orchestrator.generate_briefs(
                    _scored_df(), asof=dt.date(2026, 4, 14), output_dir=Path(tmp)
                )
        self.assertEqual(len(out), 2)
        for _, row in out.iterrows():
            md = row["brief_full_md"]
            self.assertNotEqual(md, "(brief unavailable)")
            self.assertIn(row["ticker"], md)
            self.assertIn("| Signal | Value |", md)
            self.assertIn("LLM brief unavailable", md)
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
    """The fetched next_earnings_date must reach BOTH the brief parquet AND
    the rendered markdown — not just the LLM prompt."""

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

    def test_next_earnings_date_rendered_in_markdown(self):
        with patch.object(
            orchestrator, "_brief_for_row", return_value=(_FAKE_BRIEF_FLASH, "2026-05-08")
        ):
            with tempfile.TemporaryDirectory() as tmp:
                out = orchestrator.generate_briefs(
                    _scored_df(), asof=dt.date(2026, 4, 14), output_dir=Path(tmp)
                )
        for _, row in out.iterrows():
            self.assertIn("| Next earnings |", row["brief_full_md"])
            self.assertIn("2026-05-08", row["brief_full_md"])

    def test_next_earnings_date_none_when_fetch_returns_none(self):
        with patch.object(orchestrator, "_brief_for_row", return_value=(_FAKE_BRIEF_FLASH, None)):
            with tempfile.TemporaryDirectory() as tmp:
                out = orchestrator.generate_briefs(
                    _scored_df(), asof=dt.date(2026, 4, 14), output_dir=Path(tmp)
                )
        for _, row in out.iterrows():
            self.assertIsNone(row["next_earnings_date"])
            self.assertNotIn("| Next earnings |", row["brief_full_md"])


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
            for col in ("ticker", "brief_full_md", "brief_position_pct", "next_earnings_date"):
                self.assertIn(col, out.columns)
            self.assertTrue((Path(tmp) / "2026-04-14.parquet").exists())
            self.assertTrue((Path(tmp) / "2026-04-14.md").exists())
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


if __name__ == "__main__":
    unittest.main()
