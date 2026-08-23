"""The `thematic shadow-map` command body, with the mapper faked out.

The orchestration layer is where this session's worst failures lived: a guard
reading invented keys, a loop exiting on the wrong signal, a test reading the
real production store. None of them were reachable from a unit test of a pure
function, so the command gets its own.

No LLM call is made. `orchestrator.map_themes` is replaced by a stub that writes
the funnel it would have written.
"""

from __future__ import annotations

import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd
from alphalens_cli.commands import thematic as cmd
from alphalens_pipeline.thematic.mapping.shadow_sampler import shadow_store_path
from typer.testing import CliRunner

_ASOF = dt.date(2026, 8, 21)
_SELECTED = [f"top_{i:02d}" for i in range(10)]


def _rollup() -> pd.DataFrame:
    """Selected themes rank first, then 40 the selector passed over."""
    themes = [*_SELECTED, *[f"rest_{i:02d}" for i in range(40)]]
    n = len(themes)
    return pd.DataFrame(
        {
            "theme": themes,
            "novelty_score": [20.0 - i * 0.1 for i in range(n)],
            "count_recent": [30] * n,
            "count_window": [100 - i for i in range(n)],
        }
    )


class _Harness:
    """Tmp dirs plus a stub mapper that records what it was asked for."""

    def __init__(self, tmp: Path, *, proposals_per_theme: int = 2) -> None:
        self.root = tmp
        self.funnel_dir = tmp / "prod_funnel"
        self.store_dir = tmp / "shadow"
        self.funnel_dir.mkdir(parents=True)
        self.asked: list[list[str]] = []
        self.kwargs: list[dict] = []
        self.proposals_per_theme = proposals_per_theme
        pd.DataFrame({"theme": _SELECTED, "ticker": ["X"] * 10}).to_parquet(
            self.funnel_dir / f"{_ASOF.isoformat()}.parquet"
        )

    def fake_map_themes(self, *, themes, asof, output_dir, **kwargs):
        self.asked.append(list(themes))
        self.kwargs.append(kwargs)
        rows = [
            {
                "theme": t,
                "ticker": f"{t[:3].upper()}{i}",
                "bracket_verdict": "in_bracket" if i == 0 else "too_big",
            }
            for t in themes
            for i in range(self.proposals_per_theme)
        ]
        out = Path(output_dir) / "proposal_funnel"
        out.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows).to_parquet(out / f"{asof.isoformat()}.parquet")
        return pd.DataFrame(rows)

    def run(self, *args: str):
        with (
            mock.patch.object(cmd.themes_mod, "roll_up", return_value=_rollup()),
            mock.patch.object(cmd.orchestrator, "map_themes", self.fake_map_themes),
            mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}),
        ):
            return CliRunner().invoke(
                cmd.thematic_app,
                [
                    "shadow-map",
                    "--date",
                    _ASOF.isoformat(),
                    "--funnel-dir",
                    str(self.funnel_dir),
                    "--store-dir",
                    str(self.store_dir),
                    *args,
                ],
            )


class TestShadowMapCommand(unittest.TestCase):
    def test_writes_a_store_and_never_asks_about_a_selected_theme(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = _Harness(Path(tmp))

            result = h.run("--per-band", "3")

            self.assertEqual(result.exit_code, 0, result.output)
            out = pd.read_parquet(shadow_store_path(_ASOF, store_dir=h.store_dir))
            self.assertEqual(out["theme"].nunique(), 6)
            self.assertEqual(set(h.asked[0]) & set(_SELECTED), set())
            self.assertEqual(set(out["shadow_band"]), {"near", "far"})

    def test_the_second_run_of_a_date_asks_the_mapper_nothing(self):
        """The pipeline fires six times on one asof; the draw happens once."""
        with tempfile.TemporaryDirectory() as tmp:
            h = _Harness(Path(tmp))
            h.run("--per-band", "3")

            result = h.run("--per-band", "3")

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(len(h.asked), 1)
            self.assertIn("already collected", result.output)

    def test_rebuild_redraws_the_same_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = _Harness(Path(tmp))
            h.run("--per-band", "3")

            result = h.run("--per-band", "3", "--rebuild")

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(len(h.asked), 2)

    def test_a_missing_production_funnel_fails_rather_than_guessing(self):
        """Without it the arms cannot be kept disjoint, and a shadow arm that
        silently re-maps a selected theme is worse than a missing day."""
        with tempfile.TemporaryDirectory() as tmp:
            h = _Harness(Path(tmp))
            (h.funnel_dir / f"{_ASOF.isoformat()}.parquet").unlink()

            result = h.run()

            self.assertEqual(result.exit_code, 1)
            self.assertIn("Run map-themes first", result.output)
            self.assertEqual(h.asked, [])

    def test_the_mapper_is_called_with_the_novelty_stamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = _Harness(Path(tmp))

            h.run("--per-band", "2")

            passed = h.kwargs[0]
            self.assertIn("theme_novelty", passed)
            self.assertIn("novelty_config_version", passed)
            self.assertIn("keep_unverified", passed)
            ranks = {rank for rank, _ in passed["theme_novelty"].values()}
            # Every drawn theme sits below the ten the selector took.
            self.assertTrue(all(r > 10 for r in ranks), ranks)

    def test_an_empty_rollup_exits_cleanly_without_calling_the_mapper(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = _Harness(Path(tmp))

            with mock.patch.object(cmd.themes_mod, "roll_up", return_value=pd.DataFrame()):
                with (
                    mock.patch.object(cmd.orchestrator, "map_themes", h.fake_map_themes),
                    mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "k"}),
                ):
                    result = CliRunner().invoke(
                        cmd.thematic_app,
                        [
                            "shadow-map",
                            "--date",
                            _ASOF.isoformat(),
                            "--funnel-dir",
                            str(h.funnel_dir),
                            "--store-dir",
                            str(h.store_dir),
                        ],
                    )

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(h.asked, [])
            self.assertFalse(shadow_store_path(_ASOF, store_dir=h.store_dir).exists())

    def test_a_theme_the_mapper_returned_nothing_for_still_lands_in_the_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = _Harness(Path(tmp), proposals_per_theme=0)

            result = h.run("--per-band", "2")

            self.assertEqual(result.exit_code, 0, result.output)
            out = pd.read_parquet(shadow_store_path(_ASOF, store_dir=h.store_dir))
            self.assertEqual(out["theme"].nunique(), 4)
            self.assertTrue(out["ticker"].isna().all())


if __name__ == "__main__":
    unittest.main()
