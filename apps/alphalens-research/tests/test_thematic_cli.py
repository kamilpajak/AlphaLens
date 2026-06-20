"""CLI integration tests for `alphalens thematic map-themes` output rendering."""

from __future__ import annotations

import datetime as dt
import os
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd
from alphalens_cli.main import app
from typer.testing import CliRunner

_FAKE_CANDIDATES = pd.DataFrame(
    [
        {
            "theme": "quantum_computing",
            "ticker": "QUBT",
            "company_name": "Quantum Computing Inc",
            "rationale": "Pure-play quantum hardware",
            "llm_confidence": 0.85,
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
        # PR-G (2026-05-30) swapped GOOGLE_API_KEY → OPENROUTER_API_KEY for
        # the thematic CLI; the whole pipeline now routes LLM calls through
        # OpenRouter, so GOOGLE_API_KEY is no longer part of the env.
        base = {
            "OPENROUTER_API_KEY": "fake",
            "POLYGON_API_KEY": "fake",
        }
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

    def test_map_themes_passes_theme_novelty_mapping_to_mapper(self):
        # The CLI ranks the rolled-up novel themes and truncates to
        # head(max_themes); that rank + novelty_score is the selection covariate
        # the EDGE attribution pass needs. It must reach the mapper as
        # ``theme_novelty={theme: (rank, score)}`` (rank = 1-based position in
        # the truncated, already-sorted novel frame) — not be dropped.
        novel = pd.DataFrame(
            [
                {"theme": "defense_procurement", "novelty_score": 9.5, "count_window": 7},
                {"theme": "gas_pipelines", "novelty_score": 4.2, "count_window": 3},
            ]
        )
        captured = {}

        def _fake_map_themes(*, themes, theme_novelty, **_kwargs):
            captured["themes"] = list(themes)
            captured["theme_novelty"] = theme_novelty
            return _FAKE_CANDIDATES.copy()

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch.dict(os.environ, self._env(), clear=False),
            patch(
                "alphalens_cli.commands.thematic.themes_mod.roll_up",
                return_value=novel.copy(),
            ),
            patch(
                "alphalens_cli.commands.thematic.themes_mod.flag_novel",
                return_value=novel.copy(),
            ),
            patch(
                "alphalens_cli.commands.thematic.orchestrator.map_themes",
                side_effect=_fake_map_themes,
            ),
        ):
            result = self.runner.invoke(
                app,
                ["thematic", "map-themes", "--date", "2026-05-15", "--output-dir", tmpdir],
            )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(
            captured["theme_novelty"],
            {"defense_procurement": (1, 9.5), "gas_pipelines": (2, 4.2)},
        )

    def test_map_themes_passes_novelty_config_version_to_mapper(self):
        # The novelty config token (window/recent/threshold) must reach the mapper
        # so every stamped candidate records WHICH novelty definition ranked it —
        # a future tune of those params keeps pre/post outcomes non-poolable.
        from alphalens_pipeline.thematic.extraction import themes as themes_mod

        novel = pd.DataFrame([{"theme": "t_a", "novelty_score": 5.0, "count_window": 4}])
        captured = {}

        def _fake_map_themes(*, novelty_config_version, **_kwargs):
            captured["ncv"] = novelty_config_version
            return _FAKE_CANDIDATES.copy()

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch.dict(os.environ, self._env(), clear=False),
            patch("alphalens_cli.commands.thematic.themes_mod.roll_up", return_value=novel.copy()),
            patch(
                "alphalens_cli.commands.thematic.themes_mod.flag_novel", return_value=novel.copy()
            ),
            patch(
                "alphalens_cli.commands.thematic.orchestrator.map_themes",
                side_effect=_fake_map_themes,
            ),
        ):
            result = self.runner.invoke(
                app,
                ["thematic", "map-themes", "--date", "2026-05-15", "--output-dir", tmpdir],
            )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        expected = themes_mod.novelty_config_version(
            window_days=themes_mod.DEFAULT_WINDOW_DAYS,
            recent_days=themes_mod.DEFAULT_RECENT_DAYS,
            threshold=themes_mod.DEFAULT_NOVELTY_THRESHOLD,
        )
        self.assertEqual(captured["ncv"], expected)

    def test_map_themes_missing_novelty_score_does_not_crash(self):
        # The novelty stamp is telemetry only — a malformed novel frame (no
        # novelty_score column) must degrade the score to NA, never abort the
        # daily build. Rank still derives from position.
        novel = pd.DataFrame({"theme": ["alpha_theme", "beta_theme"]})
        captured = {}

        def _fake_map_themes(*, theme_novelty, **_kwargs):
            captured["theme_novelty"] = theme_novelty
            return _FAKE_CANDIDATES.copy()

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch.dict(os.environ, self._env(), clear=False),
            patch("alphalens_cli.commands.thematic.themes_mod.roll_up", return_value=novel.copy()),
            patch(
                "alphalens_cli.commands.thematic.themes_mod.flag_novel", return_value=novel.copy()
            ),
            patch(
                "alphalens_cli.commands.thematic.orchestrator.map_themes",
                side_effect=_fake_map_themes,
            ),
        ):
            result = self.runner.invoke(
                app,
                ["thematic", "map-themes", "--date", "2026-05-15", "--output-dir", tmpdir],
            )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        mapping = captured["theme_novelty"]
        self.assertEqual(mapping["alpha_theme"][0], 1)
        self.assertEqual(mapping["beta_theme"][0], 2)
        self.assertTrue(np.isnan(mapping["alpha_theme"][1]))

    def test_map_themes_no_novel_themes_writes_empty_parquet(self):
        # A genuinely quiet news day yields 0 novel themes — a documented,
        # EXPECTED state. map-themes must still write a typed-empty candidates
        # parquet (NOT call the LLM) so the next stage (score) finds the file
        # and the run_thematic_day.sh `set -e` chain does NOT abort before
        # brief + rebuild-cache. Regression for the zero-novel chain-halt.
        from pathlib import Path

        from alphalens_pipeline.thematic.mapping.orchestrator import _MAP_THEMES_COLUMNS

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
                # The LLM proposal path must NOT run on a zero-novel day.
                "alphalens_cli.commands.thematic.orchestrator.map_themes",
                side_effect=AssertionError("must not be called"),
            ),
            patch("alphalens_cli.commands.thematic._emit_stage_volume") as emit,
        ):
            result = self.runner.invoke(
                app,
                ["thematic", "map-themes", "--date", "2026-05-15", "--output-dir", tmpdir],
            )
            # Assert inside the `with` so the TemporaryDirectory still exists.
            self.assertEqual(result.exit_code, 0, msg=result.output)
            # Observability contract stays uniform: the quiet-day path emits the
            # true 0/0 volume so the gauge does not carry stale values.
            emit.assert_called_once_with("map-themes", output_rows=0, input_rows=0)
            self.assertIn("No novel themes", result.output)
            out_path = Path(tmpdir) / "2026-05-15.parquet"
            self.assertTrue(
                out_path.exists(), msg="zero-novel day must still write a candidates parquet"
            )
            written = pd.read_parquet(out_path)
            self.assertEqual(len(written), 0)
            # Carries the full candidate schema + the freeze stamp so score /
            # brief / Django ingest read it like any other (empty) day.
            for col in _MAP_THEMES_COLUMNS:
                self.assertIn(col, written.columns)
            self.assertIn("mapper_config_version", written.columns)

    def test_zero_novel_then_score_does_not_halt_the_chain(self):
        # End-to-end proof of the fix: map-themes (0 novel) -> score for the
        # SAME date must both exit 0, so `set -euo pipefail` in
        # run_thematic_day.sh does not abort the daily build. Before the fix,
        # map-themes wrote nothing and score raised BadParameter (exit 1).
        from pathlib import Path

        with tempfile.TemporaryDirectory() as cdir, tempfile.TemporaryDirectory() as sdir:
            with (
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
                r_map = self.runner.invoke(
                    app,
                    ["thematic", "map-themes", "--date", "2026-05-15", "--output-dir", cdir],
                )
            self.assertEqual(r_map.exit_code, 0, msg=r_map.output)

            r_score = self.runner.invoke(
                app,
                [
                    "thematic",
                    "score",
                    "--date",
                    "2026-05-15",
                    "--candidates-dir",
                    cdir,
                    "--output-dir",
                    sdir,
                ],
            )
            self.assertEqual(r_score.exit_code, 0, msg=r_score.output)
            self.assertIn("Wrote 0 scored rows", r_score.output)
            self.assertTrue((Path(sdir) / "2026-05-15.parquet").exists())

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

    def test_map_themes_renders_reused_frozen_candidates_with_ndarray_gates(self):
        """The idempotent-freeze reuse path (PR #611) reloads candidates from
        parquet, where list columns (``gates_passed`` / ``gates_unknown``)
        deserialize as numpy ndarrays. The display loop must render them
        without crashing.

        Regression: the old ``row.get("gates_unknown", []) or []`` evaluated
        ndarray truthiness, raising ``ValueError: truth value of an empty
        array is ambiguous`` and halting the daily pipeline on EVERY reuse
        run (VPS 2026-06-17, slots 20:30 / 04:30 UTC). The common case is an
        EMPTY ``gates_unknown`` (most candidates carry no unknown gates),
        which round-trips as an empty ndarray — and numpy raises only for
        arrays whose length is not 1, so the empty case is the live crash.
        """
        with tempfile.TemporaryDirectory() as rtdir:
            # Round-trip through parquet so the list columns carry the REAL
            # production reuse dtype (object cells holding numpy ndarrays),
            # not a fabricated one. gates_unknown is EMPTY — the exact shape
            # that produced "truth value of an empty array is ambiguous".
            seed = pd.DataFrame(
                [
                    {
                        "theme": "quantum_computing",
                        "ticker": "QUBT",
                        "company_name": "Quantum Computing Inc",
                        "rationale": "Pure-play quantum hardware",
                        "llm_confidence": 0.85,
                        "market_cap": 1_780_000_000.0,
                        "gates_passed": ["tenk", "press"],
                        "gates_passed_str": "tenk,press",
                        "n_gates_passed": 2,
                        "gates_failed": ["etf"],
                        "gates_failed_str": "etf",
                        "n_gates_failed": 1,
                        "gates_unknown": [],
                        "gates_unknown_str": "",
                        "n_gates_unknown": 0,
                        "verified": True,
                    }
                ]
            )
            rt_path = f"{rtdir}/seed.parquet"
            seed.to_parquet(rt_path, index=False)
            df = pd.read_parquet(rt_path)
        # Sanity: the reuse dtype really is ndarray, not list (otherwise the
        # test would not exercise the regression).
        self.assertIsInstance(df.iloc[0]["gates_passed"], np.ndarray)
        df.attrs["dropped_total"] = 0
        df.attrs["dropped_all_unknown"] = 0

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
                ["thematic", "map-themes", "--date", "2026-05-15", "--output-dir", tmpdir],
            )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("QUBT", result.output)
        self.assertIn("tenk,press", result.output)


class TestMapThemesGateCellFormatting(unittest.TestCase):
    """``_fmt_gate_cell`` formats a ``gates_*`` list cell for the operator
    table that iterates ``df.head(25).iterrows()``. It must accept a Python
    list (fresh-compute path), a numpy ndarray (frozen-candidate parquet
    round-trip — list columns deserialize as ndarray), or None, and must
    NEVER evaluate array truthiness (``ndarray or []`` raises ValueError)."""

    def setUp(self):
        from alphalens_cli.commands.thematic import _fmt_gate_cell

        self._fmt = _fmt_gate_cell

    def test_joins_python_list(self):
        self.assertEqual(self._fmt(["tenk", "press"], empty="(none)"), "tenk,press")

    def test_empty_python_list_returns_empty_marker(self):
        self.assertEqual(self._fmt([], empty="-"), "-")

    def test_none_returns_empty_marker(self):
        self.assertEqual(self._fmt(None, empty="-"), "-")

    def test_joins_numpy_array(self):
        # The motivating regression: a populated ndarray from a parquet
        # round-trip must render, not raise.
        self.assertEqual(
            self._fmt(np.array(["tenk", "press"], dtype=object), empty="(none)"),
            "tenk,press",
        )

    def test_empty_numpy_array_returns_empty_marker(self):
        # An empty ndarray's truthiness is ALSO ambiguous — the helper must
        # short-circuit on length, not on ``or``.
        self.assertEqual(self._fmt(np.array([], dtype=object), empty="-"), "-")


class TestExtractCLIModelEnvVar(unittest.TestCase):
    """`extract --model` reads the post-DeepSeek env var, not the retired GEMINI_MODEL."""

    def setUp(self):
        self.runner = CliRunner()

    def test_extract_model_default_comes_from_alphalens_extract_model_env(self):
        # A sentinel distinct from event_extractor.DEFAULT_MODEL so the test proves
        # the env var (not the hard-coded default) supplied the value.
        sentinel = "deepseek/deepseek-v4-flash-envtest"
        captured = {}

        def fake_extract_daily(*, date, news_dir, events_dir, api_key, model):
            captured["model"] = model
            return pd.DataFrame()

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch.dict(
                os.environ,
                {"OPENROUTER_API_KEY": "fake", "ALPHALENS_EXTRACT_MODEL": sentinel},
                clear=False,
            ),
            patch(
                "alphalens_cli.commands.thematic.event_extractor.extract_daily",
                side_effect=fake_extract_daily,
            ),
            patch(
                "alphalens_cli.commands.thematic.themes_mod.roll_up",
                return_value=pd.DataFrame(),
            ),
            patch(
                "alphalens_cli.commands.thematic.themes_mod.flag_novel",
                return_value=pd.DataFrame(),
            ),
        ):
            result = self.runner.invoke(
                app,
                [
                    "thematic",
                    "extract",
                    "--date",
                    "2026-05-15",
                    "--news-dir",
                    tmpdir,
                    "--events-dir",
                    tmpdir,
                ],
            )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(captured["model"], sentinel)


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
                    "llm_confidence": 0.85,
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


class TestPrintScorePreviewNaNSafety(unittest.TestCase):
    """``_print_score_preview`` is the post-score CLI summary panel that
    iterates ``enriched.head(25).iterrows()``. Pandas missing values land
    as ``float('nan')`` (NOT ``None``) for object-dtype string columns —
    a defensive ``row.get("col") or "?"`` accepts NaN as truthy because
    Python evaluates ``bool(float('nan')) is True``. The downstream
    ``[:19]`` slice then crashes with ``TypeError: 'float' object is not
    subscriptable``.

    Bug observed on VPS 2026-05-30 (industry_name NaN halted the daily
    pipeline before the verify-cache + rebuild-cache hooks could run).
    These tests pin the NaN-safe contract so any future regression
    (new str column added without a NaN guard) fails CI loud.
    """

    def setUp(self):
        # Import here so the test surfaces in CI even if a future refactor
        # moves the helpers to a private module — the assertion is on
        # behaviour, not the location.
        from alphalens_cli.commands.thematic import (
            _fmt_str_or_dash,
            _print_score_preview,
        )

        self._fmt_str_or_dash = _fmt_str_or_dash
        self._print_score_preview = _print_score_preview

    def _row(self, **overrides) -> dict:
        """Minimal row schema matching what ``score_candidates`` writes
        into ``enriched`` parquet — every column the preview reads."""
        base = {
            "ticker": "QUBT",
            "industry_name": "Computer Hardware",
            "layer4_weighted_score": 3,
            "insider_score_usd": 1500.0,
            "fcff_yield_pct": 8.7,
            "valuation_composite_sector_percentile": 55.0,
            "technicals_summary_str": "RSI 65 / MA50 +15.7%",
        }
        base.update(overrides)
        return base

    # ---- _fmt_str_or_dash unit cases ----

    def test_fmt_str_or_dash_returns_value_when_str(self):
        self.assertEqual(self._fmt_str_or_dash("Computer Hardware", 19), "Computer Hardware")

    def test_fmt_str_or_dash_truncates_to_max_len(self):
        self.assertEqual(
            self._fmt_str_or_dash("Services-Prepackaged Software", 19),
            "Services-Prepackage",
        )

    def test_fmt_str_or_dash_returns_dash_on_none(self):
        self.assertEqual(self._fmt_str_or_dash(None, 19), "-")

    def test_fmt_str_or_dash_returns_dash_on_nan(self):
        # The motivating crash: ``float('nan')`` masquerades as a string
        # in object-dtype pandas columns. The helper must short-circuit
        # BEFORE the slice.
        self.assertEqual(self._fmt_str_or_dash(float("nan"), 19), "-")

    def test_fmt_str_or_dash_returns_dash_on_pandas_na(self):
        self.assertEqual(self._fmt_str_or_dash(pd.NA, 19), "-")

    def test_fmt_str_or_dash_returns_dash_on_empty_string(self):
        # Empty string from a coalesced source is treated the same as
        # missing — matches the visual intent of "-" in the table.
        self.assertEqual(self._fmt_str_or_dash("", 19), "-")

    # ---- _print_score_preview end-to-end NaN safety ----

    def test_preview_does_not_crash_on_nan_industry_name(self):
        df = pd.DataFrame([self._row(industry_name=float("nan"))])
        try:
            self._print_score_preview(df)
        except TypeError as exc:
            self.fail(f"_print_score_preview crashed on NaN industry_name: {exc}")

    def test_preview_does_not_crash_on_nan_technicals_summary(self):
        # Same defensive shape — ``row.get(..., '')[:50]`` has the same
        # bug class. NaN bypasses the ``''`` default because dict.get
        # only returns the default on a MISSING key, not on a present-
        # but-NaN value.
        df = pd.DataFrame([self._row(technicals_summary_str=float("nan"))])
        try:
            self._print_score_preview(df)
        except TypeError as exc:
            self.fail(f"_print_score_preview crashed on NaN technicals_summary_str: {exc}")

    def test_preview_does_not_crash_on_multiple_nan_string_columns(self):
        df = pd.DataFrame(
            [
                self._row(
                    industry_name=float("nan"),
                    technicals_summary_str=float("nan"),
                ),
                self._row(),  # second row to exercise iteration past a NaN
            ]
        )
        try:
            self._print_score_preview(df)
        except TypeError as exc:
            self.fail(f"_print_score_preview crashed on NaN-heavy frame: {exc}")

    def test_preview_renders_dash_marker_for_nan_industry(self):
        # Belt-and-suspenders: the preview should not silently turn a
        # NaN industry into the literal string "nan" (pandas's default
        # str-conversion). The dash placeholder makes the missing-value
        # visible to the operator scanning the summary panel.
        df = pd.DataFrame([self._row(industry_name=float("nan"))])
        with patch("typer.echo") as echo:
            self._print_score_preview(df)
        rendered = "\n".join(call.args[0] for call in echo.call_args_list if call.args)
        self.assertIn("QUBT", rendered, "ticker must still render alongside the dash marker")
        self.assertNotIn(" nan ", rendered, "raw 'nan' must NOT leak into the rendered row")


class TestBriefCLI(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def test_brief_reads_scored_writes_enriched_parquet(self):
        from pathlib import Path

        scored = pd.DataFrame(
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
                }
            ]
        )
        enriched = scored.copy()
        enriched["brief_model_used"] = "gemini-2.5-flash"
        enriched["brief_tldr"] = "tldr"
        enriched.attrs["n_pro"] = 0
        enriched.attrs["n_flash"] = 1

        with (
            tempfile.TemporaryDirectory() as scored_tmp,
            tempfile.TemporaryDirectory() as out_tmp,
        ):
            spath = Path(scored_tmp) / "2026-04-14.parquet"
            scored.to_parquet(spath, index=False)

            def fake_generate_briefs(scored_arg, *, asof, output_dir, **kwargs):
                # Mimic the orchestrator side effect: write the brief parquet.
                enriched.to_parquet(output_dir / f"{asof.isoformat()}.parquet", index=False)
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
            self.assertFalse((Path(out_tmp) / "2026-04-14.md").exists())

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
