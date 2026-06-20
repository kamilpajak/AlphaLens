"""``map_themes`` stamps the per-theme novelty rank + score onto every candidate.

The novelty rank/score are computed transiently in the CLI when it truncates
the rolled-up novel themes to ``head(max_themes)`` and then thrown away — only
``list(novel["theme"])`` reaches the mapper. That makes the selection covariate
"how novel was the theme that surfaced this ticker" impossible to recover later
without reconstructing the rollup (lossy: the 30-day event window ages out).

These tests pin that ``map_themes`` records ``novelty_rank`` + ``novelty_score``
on the candidates parquet (the EDGE-outcome join target keyed by theme), so a
future N>=30 attribution pass can join novelty without reconstruction. Stamp is
display/telemetry only — it never feeds selection or ordering.
"""

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from alphalens_pipeline.thematic.mapping import orchestrator

from .test_theme_mapping import _catalyst_payload

ASOF = dt.date(2026, 6, 18)


def _theme_row(theme: str, ticker: str) -> dict:
    """Minimal verified candidate row the way ``_verify_candidates_for_theme``
    emits it (only the sort keys + ``theme`` matter for the stamp test)."""
    return {
        "theme": theme,
        "ticker": ticker,
        "company_name": f"{ticker} Inc",
        "rationale": "stub",
        "llm_confidence": 0.80,
        "market_cap": 1_000_000_000,
        "n_gates_passed": 1,
        "verified": True,
    }


def _run_map_themes(
    themes, theme_novelty, out_dir, *, yield_rows=True, novelty_config_version=None
):
    """Drive ``map_themes`` with all network/LLM stages mocked so the only thing
    under test is the novelty stamp. ``yield_rows=False`` makes every theme a
    no-catalyst skip → the empty-frame branch."""
    catalyst = _catalyst_payload() if yield_rows else None

    def _verify(*, theme, **_kwargs):
        return ([_theme_row(theme, f"T{theme[-1].upper()}")], 0, 0)

    with (
        patch.object(orchestrator, "_init_pro_client", return_value=object()),
        patch.object(orchestrator, "_fetch_press_window", return_value=pd.DataFrame()),
        patch.object(orchestrator, "_resolve_catalyst", return_value=catalyst),
        patch.object(
            orchestrator,
            "_propose_and_filter_candidates",
            return_value=(["TIC"], {"TIC": True}, ["kw"]),
        ),
        patch.object(orchestrator, "_verify_candidates_for_theme", side_effect=_verify),
    ):
        return orchestrator.map_themes(
            themes=themes,
            asof=ASOF,
            api_key="dummy",
            output_dir=out_dir,
            theme_novelty=theme_novelty,
            novelty_config_version=novelty_config_version,
            rebuild=True,
        )


class TestMapThemesNoveltyStamp(unittest.TestCase):
    def test_stamps_rank_and_score_per_theme(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            theme_novelty = {"theme_a": (1, 9.5), "theme_b": (2, 4.2)}
            df = _run_map_themes(["theme_a", "theme_b"], theme_novelty, out)

            self.assertIn("novelty_rank", df.columns)
            self.assertIn("novelty_score", df.columns)
            by_theme = df.set_index("theme")
            self.assertEqual(int(by_theme.loc["theme_a", "novelty_rank"]), 1)
            self.assertAlmostEqual(float(by_theme.loc["theme_a", "novelty_score"]), 9.5)
            self.assertEqual(int(by_theme.loc["theme_b", "novelty_rank"]), 2)
            self.assertAlmostEqual(float(by_theme.loc["theme_b", "novelty_score"]), 4.2)

    def test_written_parquet_carries_novelty(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            _run_map_themes(["theme_a"], {"theme_a": (1, 7.0)}, out)
            roundtrip = pd.read_parquet(out / f"{ASOF.isoformat()}.parquet")
            self.assertIn("novelty_rank", roundtrip.columns)
            self.assertIn("novelty_score", roundtrip.columns)
            self.assertEqual(int(roundtrip.loc[0, "novelty_rank"]), 1)
            self.assertAlmostEqual(float(roundtrip.loc[0, "novelty_score"]), 7.0)

    def test_unmapped_theme_gets_null_novelty(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            # theme_b is absent from the mapping -> its rows carry NA, never an error.
            df = _run_map_themes(["theme_a", "theme_b"], {"theme_a": (1, 5.0)}, out)
            by_theme = df.set_index("theme")
            self.assertEqual(int(by_theme.loc["theme_a", "novelty_rank"]), 1)
            self.assertTrue(pd.isna(by_theme.loc["theme_b", "novelty_rank"]))
            self.assertTrue(pd.isna(by_theme.loc["theme_b", "novelty_score"]))

    def test_none_mapping_keeps_columns_all_null(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            df = _run_map_themes(["theme_a"], None, out)
            self.assertIn("novelty_rank", df.columns)
            self.assertIn("novelty_score", df.columns)
            self.assertTrue(df["novelty_rank"].isna().all())
            self.assertTrue(df["novelty_score"].isna().all())

    def test_empty_day_carries_novelty_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            df = _run_map_themes(["theme_a"], {"theme_a": (1, 5.0)}, out, yield_rows=False)
            self.assertEqual(len(df), 0)
            self.assertIn("novelty_rank", df.columns)
            self.assertIn("novelty_score", df.columns)

    def test_novelty_columns_in_schema_tuple(self):
        # write_empty_candidates + the all-dropped branch build the typed-empty
        # frame from this tuple, so all three novelty columns must be present.
        self.assertIn("novelty_rank", orchestrator._MAP_THEMES_COLUMNS)
        self.assertIn("novelty_score", orchestrator._MAP_THEMES_COLUMNS)
        self.assertIn("novelty_config_version", orchestrator._MAP_THEMES_COLUMNS)

    def test_stamps_novelty_config_version_on_every_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            token = '{"schema":1,"window_days":30}'
            df = _run_map_themes(
                ["theme_a", "theme_b"],
                {"theme_a": (1, 9.5), "theme_b": (2, 4.2)},
                out,
                novelty_config_version=token,
            )
            self.assertIn("novelty_config_version", df.columns)
            self.assertTrue((df["novelty_config_version"] == token).all())

    def test_empty_day_carries_novelty_config_version_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            df = _run_map_themes(
                ["theme_a"],
                {"theme_a": (1, 5.0)},
                out,
                yield_rows=False,
                novelty_config_version='{"schema":1}',
            )
            self.assertEqual(len(df), 0)
            self.assertIn("novelty_config_version", df.columns)


if __name__ == "__main__":
    unittest.main()
