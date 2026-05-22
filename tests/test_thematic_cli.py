"""CLI integration tests for `alphalens thematic map-themes` output rendering."""

from __future__ import annotations

import datetime as dt
import os
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
from typer.testing import CliRunner

from alphalens_cli.main import app

_FAKE_CANDIDATES = pd.DataFrame(
    [
        {
            "theme": "quantum_computing",
            "ticker": "QUBT",
            "company_name": "Quantum Computing Inc",
            "rationale": "Pure-play quantum hardware",
            "gemini_confidence": 0.85,
            "market_cap": 1_780_000_000.0,
            "gates_passed": ["tenk", "press"],
            "gates_passed_str": "tenk,press",
            "n_gates_passed": 2,
            "gates_failed": ["etf", "insider"],
            "gates_failed_str": "etf,insider",
            "n_gates_failed": 2,
            "gates_unknown": [],
            "gates_unknown_str": "",
            "n_gates_unknown": 0,
            "verified": True,
        }
    ]
)


class TestMapThemesCLI(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def _env(self, **overrides):
        base = {"GOOGLE_API_KEY": "fake", "POLYGON_API_KEY": "fake"}
        base.update(overrides)
        return base

    def test_map_themes_renders_summary_with_drop_counts(self):
        df = _FAKE_CANDIDATES.copy()
        df.attrs["dropped_total"] = 3
        df.attrs["dropped_all_unknown"] = 1

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch.dict(os.environ, self._env(), clear=False),
            patch(
                "alphalens_cli.commands.thematic.themes_mod.roll_up",
                return_value=pd.DataFrame(
                    [
                        {
                            "theme": "quantum_computing",
                            "novelty_score": 5.0,
                            "count_recent": 3,
                            "count_baseline": 0,
                            "count_window": 3,
                            "first_seen": dt.date(2026, 5, 1),
                            "latest_seen": dt.date(2026, 5, 15),
                        }
                    ]
                ),
            ),
            patch(
                "alphalens_cli.commands.thematic.themes_mod.flag_novel",
                return_value=pd.DataFrame(
                    [
                        {
                            "theme": "quantum_computing",
                            "novelty_score": 5.0,
                            "count_recent": 3,
                            "count_baseline": 0,
                            "count_window": 3,
                            "first_seen": dt.date(2026, 5, 1),
                            "latest_seen": dt.date(2026, 5, 15),
                        }
                    ]
                ),
            ),
            patch(
                "alphalens_cli.commands.thematic.orchestrator.map_themes",
                return_value=df,
            ),
        ):
            result = self.runner.invoke(
                app,
                [
                    "thematic",
                    "map-themes",
                    "--date",
                    "2026-05-15",
                    "--output-dir",
                    tmpdir,
                ],
            )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("Wrote 1 candidate rows", result.output)
        self.assertIn("dropped 3 unverified", result.output)
        self.assertIn("1 were all-unknown", result.output)
        # Table header + candidate row rendered
        self.assertIn("QUBT", result.output)
        self.assertIn("tenk,press", result.output)

    def test_map_themes_no_novel_themes_short_circuits(self):
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch.dict(os.environ, self._env(), clear=False),
            patch(
                "alphalens_cli.commands.thematic.themes_mod.roll_up",
                return_value=pd.DataFrame(),
            ),
            patch(
                "alphalens_cli.commands.thematic.themes_mod.flag_novel",
                return_value=pd.DataFrame(),
            ),
            patch(
                "alphalens_cli.commands.thematic.orchestrator.map_themes",
                side_effect=AssertionError("must not be called"),
            ),
        ):
            result = self.runner.invoke(
                app,
                ["thematic", "map-themes", "--date", "2026-05-15", "--output-dir", tmpdir],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("No novel themes", result.output)

    def test_map_themes_missing_api_key_raises(self):
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch.dict(os.environ, {}, clear=True),
        ):
            result = self.runner.invoke(
                app,
                ["thematic", "map-themes", "--date", "2026-05-15", "--output-dir", tmpdir],
            )
        # typer.BadParameter raises non-zero
        self.assertNotEqual(result.exit_code, 0)


class TestScoreCLI(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def test_score_reads_phase_c_writes_enriched(self):
        candidates = pd.DataFrame(
            [
                {
                    "theme": "quantum_computing",
                    "ticker": "QUBT",
                    "company_name": "Quantum Computing Inc",
                    "rationale": "x",
                    "gemini_confidence": 0.85,
                    "market_cap": 1.78e9,
                    "gates_passed": ["tenk"],
                    "gates_passed_str": "tenk",
                    "n_gates_passed": 1,
                    "gates_failed": [],
                    "gates_failed_str": "",
                    "n_gates_failed": 0,
                    "gates_unknown": [],
                    "gates_unknown_str": "",
                    "n_gates_unknown": 0,
                    "verified": True,
                }
            ]
        )
        enriched = candidates.copy()
        enriched["industry_id"] = 101001
        enriched["industry_name"] = "Quantum Computing Software"
        enriched["sector_name"] = "Technology"
        enriched["insider_score_usd"] = 50_000.0
        enriched["insider_score_sector_percentile"] = 75.0
        enriched["fcff_yield_pct"] = 4.0
        enriched["fcff_yield_sector_percentile"] = 80.0
        enriched["valuation_pe"] = None
        enriched["valuation_ps"] = 30.0
        enriched["valuation_ev_rev"] = 32.0
        enriched["valuation_fcf_margin"] = -0.5
        enriched["valuation_composite_sector_percentile"] = 60.0
        enriched["technical_rsi"] = 55.0
        enriched["technical_ma50_distance_pct"] = 5.0
        enriched["technical_atr_pct"] = 4.0
        enriched["technical_volume_zscore"] = 1.0
        enriched["technicals_summary_str"] = "RSI 55 / MA50 +5.0% / ATR 4.0% / volZ 1.0"
        enriched["layer4_weighted_score"] = 4

        from pathlib import Path

        with tempfile.TemporaryDirectory() as cdir, tempfile.TemporaryDirectory() as tmp:
            cpath = Path(cdir) / "2026-04-14.parquet"
            candidates.to_parquet(cpath, index=False)
            with patch(
                "alphalens_cli.commands.thematic.screening_scorer.score_candidates",
                return_value=enriched,
            ):
                result = self.runner.invoke(
                    app,
                    [
                        "thematic",
                        "score",
                        "--date",
                        "2026-04-14",
                        "--candidates-dir",
                        cdir,
                        "--output-dir",
                        tmp,
                    ],
                )
            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("Scoring 1 candidates", result.output)
            self.assertIn("Wrote 1 scored rows", result.output)
            self.assertIn("QUBT", result.output)
            out_path = Path(tmp) / "2026-04-14.parquet"
            self.assertTrue(out_path.exists())
            round_trip = pd.read_parquet(out_path)
            self.assertIn("layer4_weighted_score", round_trip.columns)

    def test_score_errors_when_phase_c_parquet_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.runner.invoke(
                app,
                [
                    "thematic",
                    "score",
                    "--date",
                    "2026-04-14",
                    "--candidates-dir",
                    tmp,
                    "--output-dir",
                    tmp,
                ],
            )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Phase C parquet missing", result.output)

    def test_score_empty_candidates_writes_empty_scored_parquet(self):
        # Thin days (upstream outages, strict gating) yield zero verified
        # candidates. The score stage must persist an empty Phase D parquet
        # and exit 0 so the downstream brief + api stages can short-circuit
        # via their own empty-input handlers.
        candidates = pd.DataFrame(
            columns=[
                "theme",
                "ticker",
                "verified",
                "gates_passed",
                "gates_passed_str",
                "n_gates_passed",
            ]
        )
        from pathlib import Path

        with tempfile.TemporaryDirectory() as cdir, tempfile.TemporaryDirectory() as tmp:
            cpath = Path(cdir) / "2026-05-21.parquet"
            candidates.to_parquet(cpath, index=False)
            result = self.runner.invoke(
                app,
                [
                    "thematic",
                    "score",
                    "--date",
                    "2026-05-21",
                    "--candidates-dir",
                    cdir,
                    "--output-dir",
                    tmp,
                ],
            )
            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("Wrote 0 scored rows", result.output)
            out_path = Path(tmp) / "2026-05-21.parquet"
            self.assertTrue(out_path.exists())
            round_trip = pd.read_parquet(out_path)
            self.assertEqual(len(round_trip), 0)


class TestBriefCLI(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def test_brief_reads_scored_writes_enriched_and_markdown(self):
        from pathlib import Path

        scored = pd.DataFrame(
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
                }
            ]
        )
        enriched = scored.copy()
        enriched["brief_model_used"] = "gemini-2.5-flash"
        enriched["brief_tldr"] = "tldr"
        enriched["brief_full_md"] = "## QUBT brief..."
        enriched.attrs["n_pro"] = 0
        enriched.attrs["n_flash"] = 1

        with (
            tempfile.TemporaryDirectory() as scored_tmp,
            tempfile.TemporaryDirectory() as out_tmp,
        ):
            spath = Path(scored_tmp) / "2026-04-14.parquet"
            scored.to_parquet(spath, index=False)

            def fake_generate_briefs(scored_arg, *, asof, output_dir, **kwargs):
                # Mimic the orchestrator side effect: write the parquet + .md.
                enriched.to_parquet(output_dir / f"{asof.isoformat()}.parquet", index=False)
                (output_dir / f"{asof.isoformat()}.md").write_text("# bundle\n## QUBT ...")
                return enriched

            with patch(
                "alphalens_cli.commands.thematic.brief_orchestrator.generate_briefs",
                side_effect=fake_generate_briefs,
            ):
                result = self.runner.invoke(
                    app,
                    [
                        "thematic",
                        "brief",
                        "--date",
                        "2026-04-14",
                        "--scored-dir",
                        scored_tmp,
                        "--output-dir",
                        out_tmp,
                    ],
                )
            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("Generating briefs for 1 scored rows", result.output)
            self.assertIn("Wrote 1 briefs", result.output)
            self.assertIn("Pro: 0, Flash: 1", result.output)
            self.assertTrue((Path(out_tmp) / "2026-04-14.parquet").exists())
            self.assertTrue((Path(out_tmp) / "2026-04-14.md").exists())

    def test_brief_errors_when_scored_parquet_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.runner.invoke(
                app,
                [
                    "thematic",
                    "brief",
                    "--date",
                    "2026-04-14",
                    "--scored-dir",
                    tmp,
                    "--output-dir",
                    tmp,
                ],
            )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Phase D scored parquet missing", result.output)


if __name__ == "__main__":
    unittest.main()
