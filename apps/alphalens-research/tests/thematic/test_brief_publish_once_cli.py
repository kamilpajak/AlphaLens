"""The pipeline stages keep a published brief as it is, and create none after the open (#1479).

On 16 of 114 dates the candidate list was recomputed after the open, so `/edge`
measured a list the owner never read. These tests drive the real CLI commands:

- a published date is not re-mapped and its brief is not regenerated, even when
  the mapper config changed; only missing options telemetry may be filled in;
- a scheduled run (no ``--date``) after the arrival open creates nothing;
- ``--rebuild`` is the deliberate way to replace a published date.
"""

from __future__ import annotations

import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from alphalens_cli.commands import thematic as thematic_cmd
from alphalens_cli.main import app
from typer.testing import CliRunner

ASOF = dt.date(2026, 9, 10)  # Thursday; arrival Friday 2026-09-11 13:30 UTC
PUBLISHED_AT = pd.Timestamp("2026-09-11T01:40:00Z")
# Scheduled runs take asof = yesterday (UTC) of this clock.
BEFORE_OPEN = dt.datetime(2026, 9, 11, 4, 35, tzinfo=dt.UTC)
AFTER_OPEN = dt.datetime(2026, 9, 11, 13, 35, tzinfo=dt.UTC)


def _candidates() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "theme": ["quantum_computing"],
            "ticker": ["QUBT"],
            "company_name": ["Quantum Computing Inc"],
            "rationale": ["serves the theme"],
            "llm_confidence": [0.8],
            "gates_passed": [["tenk"]],
            "gates_unknown": [[]],
            "verified": [True],
            "mapper_config_version": ["old-config"],
        }
    )


def _published_brief(snapshot: str | None) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "theme": ["quantum_computing", "quantum_computing"],
            "ticker": ["QUBT", "IONQ"],
            "verified": [True, True],
            "brief_model_used": ["deepseek-flash", "deepseek-flash"],
            "brief_status": ["ok", "ok"],
            "brief_tldr": ["read by the owner", "also read"],
            "brief_trade_setup": ['{"entry": 10.0}', '{"entry": 20.0}'],
            "options_snapshot_utc": [snapshot, "2026-09-11T00:40:00Z"],
            "options_ivx30": [None, 0.55],
            "brief_published_at": [PUBLISHED_AT, PUBLISHED_AT],
        }
    )


class _World:
    """The production layout: candidates, scored and briefs as sibling directories."""

    def __init__(self, test: unittest.TestCase) -> None:
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.candidates = root / "thematic_candidates"
        self.scored = root / "thematic_scored"
        self.briefs = root / "thematic_briefs"
        for d in (self.candidates, self.scored, self.briefs):
            d.mkdir()
        self.runner = CliRunner()

    def file(self, directory: Path) -> Path:
        return directory / f"{ASOF.isoformat()}.parquet"

    def invoke(self, *args: str):
        return self.runner.invoke(app, ["thematic", *args], catch_exceptions=False)


class MapThemesKeepsAPublishedListTest(unittest.TestCase):
    def setUp(self) -> None:
        self.w = _World(self)
        _candidates().to_parquet(self.w.file(self.w.candidates), index=False)
        _published_brief("2026-09-11T00:40:00Z").to_parquet(self.w.file(self.w.briefs), index=False)
        env = patch.dict(os.environ, {"OPENROUTER_API_KEY": "fake"}, clear=False)
        env.start()
        self.addCleanup(env.stop)

    def _map(self, *extra: str):
        return self.w.invoke(
            "map-themes", "--date", ASOF.isoformat(), "--output-dir", str(self.w.candidates), *extra
        )

    def test_a_published_date_is_not_mapped_again(self) -> None:
        before = self.w.file(self.w.candidates).read_bytes()
        with (
            patch.object(thematic_cmd.orchestrator, "map_themes") as mapper,
            patch.object(thematic_cmd.themes_mod, "roll_up") as roll_up,
            patch.object(thematic_cmd, "_emit_stage_volume") as emit,
        ):
            result = self._map()
        self.assertEqual(result.exit_code, 0, msg=result.output)
        mapper.assert_not_called()
        roll_up.assert_not_called()
        self.assertEqual(self.w.file(self.w.candidates).read_bytes(), before)
        self.assertIn("published", result.output)
        self.assertEqual(emit.call_args.kwargs["output_rows"], 1)
        self.assertEqual(emit.call_args.kwargs["input_rows"], 0)

    def test_rebuild_maps_a_published_date(self) -> None:
        novel = pd.DataFrame({"theme": ["quantum_computing"], "novelty_score": [3.0]})
        with (
            patch.object(thematic_cmd.orchestrator, "map_themes", return_value=_candidates()) as m,
            patch.object(thematic_cmd.themes_mod, "roll_up", return_value=novel),
            patch.object(thematic_cmd.themes_mod, "flag_novel", return_value=novel),
            patch.object(thematic_cmd, "_write_theme_rollup_best_effort", return_value="written"),
            patch.object(thematic_cmd, "_emit_stage_volume"),
        ):
            result = self._map("--rebuild")
        self.assertEqual(result.exit_code, 0, msg=result.output)
        m.assert_called_once()

    def test_an_unreadable_brief_is_not_mapped_again(self) -> None:
        self.w.file(self.w.briefs).write_bytes(b"not a parquet")
        with (
            patch.object(thematic_cmd.orchestrator, "map_themes") as mapper,
            patch.object(thematic_cmd, "_emit_stage_volume"),
        ):
            result = self._map()
        self.assertEqual(result.exit_code, 0, msg=result.output)
        mapper.assert_not_called()


class ScheduledRunAfterTheOpenCreatesNothingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.w = _World(self)
        env = patch.dict(os.environ, {"OPENROUTER_API_KEY": "fake"}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        clock = patch.object(thematic_cmd, "_now_utc", return_value=AFTER_OPEN)
        clock.start()
        self.addCleanup(clock.stop)

    def test_map_themes_writes_no_candidates(self) -> None:
        with (
            patch.object(thematic_cmd.orchestrator, "map_themes") as mapper,
            patch.object(thematic_cmd.themes_mod, "roll_up") as roll_up,
            patch.object(thematic_cmd, "_emit_stage_volume"),
        ):
            result = self.w.invoke("map-themes", "--output-dir", str(self.w.candidates))
        self.assertEqual(result.exit_code, 0, msg=result.output)
        mapper.assert_not_called()
        roll_up.assert_not_called()
        self.assertEqual(list(self.w.candidates.iterdir()), [])
        self.assertIn("deadline", result.output)

    def test_score_skips_without_failing_on_missing_candidates(self) -> None:
        result = self.w.invoke(
            "score",
            "--candidates-dir",
            str(self.w.candidates),
            "--output-dir",
            str(self.w.scored),
        )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(list(self.w.scored.iterdir()), [])

    def test_brief_writes_nothing(self) -> None:
        pd.DataFrame({"ticker": ["QUBT"], "verified": [True]}).to_parquet(
            self.w.file(self.w.scored), index=False
        )
        with patch.object(thematic_cmd.brief_orchestrator, "generate_briefs") as gen:
            result = self.w.invoke(
                "brief", "--scored-dir", str(self.w.scored), "--output-dir", str(self.w.briefs)
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        gen.assert_not_called()
        self.assertEqual(list(self.w.briefs.iterdir()), [])

    def test_before_the_open_the_scheduled_run_still_maps(self) -> None:
        novel = pd.DataFrame({"theme": ["quantum_computing"], "novelty_score": [3.0]})
        with (
            patch.object(thematic_cmd, "_now_utc", return_value=BEFORE_OPEN),
            patch.object(thematic_cmd.orchestrator, "map_themes", return_value=_candidates()) as m,
            patch.object(thematic_cmd.themes_mod, "roll_up", return_value=novel),
            patch.object(thematic_cmd.themes_mod, "flag_novel", return_value=novel),
            patch.object(thematic_cmd, "_write_theme_rollup_best_effort", return_value="written"),
            patch.object(thematic_cmd, "_emit_stage_volume"),
        ):
            result = self.w.invoke("map-themes", "--output-dir", str(self.w.candidates))
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(m.call_args.kwargs["asof"], ASOF)


class BriefKeepsAPublishedBriefTest(unittest.TestCase):
    def setUp(self) -> None:
        self.w = _World(self)
        scored = _published_brief(None).drop(
            columns=["brief_model_used", "brief_status", "brief_tldr", "brief_trade_setup"]
        )
        # A later slot re-scored the list: new ORDER, a new name and a late
        # options stamp for QUBT. Only the stamp may reach the published brief.
        scored = pd.concat(
            [scored, pd.DataFrame({"ticker": ["NEWCO"], "verified": [True]})], ignore_index=True
        ).iloc[::-1]
        scored.loc[scored["ticker"] == "QUBT", "options_snapshot_utc"] = "2026-09-11T04:40:00Z"
        scored.loc[scored["ticker"] == "QUBT", "options_ivx30"] = 0.61
        scored.to_parquet(self.w.file(self.w.scored), index=False)

    def _brief(self, *extra: str):
        return self.w.invoke(
            "brief",
            "--date",
            ASOF.isoformat(),
            "--scored-dir",
            str(self.w.scored),
            "--output-dir",
            str(self.w.briefs),
            *extra,
        )

    def test_only_a_missing_options_stamp_is_filled_in(self) -> None:
        _published_brief(None).to_parquet(self.w.file(self.w.briefs), index=False)
        with patch.object(thematic_cmd.brief_orchestrator, "generate_briefs") as gen:
            result = self._brief()
        self.assertEqual(result.exit_code, 0, msg=result.output)
        gen.assert_not_called()
        stored = pd.read_parquet(self.w.file(self.w.briefs))
        expected = _published_brief(None)
        self.assertEqual(list(stored["ticker"]), ["QUBT", "IONQ"])
        for col in ("brief_tldr", "brief_trade_setup", "brief_model_used"):
            self.assertEqual(list(stored[col]), list(expected[col]))
        self.assertTrue((stored["brief_published_at"] == PUBLISHED_AT).all())
        self.assertEqual(stored.loc[0, "options_snapshot_utc"], "2026-09-11T04:40:00Z")
        self.assertAlmostEqual(float(stored.loc[0, "options_ivx30"]), 0.61)
        self.assertAlmostEqual(float(stored.loc[1, "options_ivx30"]), 0.55)

    def test_a_complete_brief_is_not_rewritten(self) -> None:
        _published_brief("2026-09-11T00:40:00Z").to_parquet(self.w.file(self.w.briefs), index=False)
        before = self.w.file(self.w.briefs).read_bytes()
        with patch.object(thematic_cmd.brief_orchestrator, "generate_briefs") as gen:
            result = self._brief()
        self.assertEqual(result.exit_code, 0, msg=result.output)
        gen.assert_not_called()
        self.assertEqual(self.w.file(self.w.briefs).read_bytes(), before)
        self.assertIn("published", result.output)

    def test_metrics_count_the_published_models(self) -> None:
        _published_brief("2026-09-11T00:40:00Z").to_parquet(self.w.file(self.w.briefs), index=False)
        with patch.object(thematic_cmd, "emit_domain_metrics") as emit:
            result = self._brief()
        self.assertEqual(result.exit_code, 0, msg=result.output)
        metrics = emit.call_args.kwargs["metrics"]
        self.assertEqual(metrics["alphalens_thematic_briefs_total"], 2)
        self.assertEqual(metrics['alphalens_thematic_briefs_by_model{model="flash"}'], 2)

    def test_rebuild_regenerates_a_published_brief(self) -> None:
        _published_brief("2026-09-11T00:40:00Z").to_parquet(self.w.file(self.w.briefs), index=False)
        regenerated = _published_brief("2026-09-11T00:40:00Z")
        with patch.object(
            thematic_cmd.brief_orchestrator, "generate_briefs", return_value=regenerated
        ) as gen:
            result = self._brief("--rebuild")
        self.assertEqual(result.exit_code, 0, msg=result.output)
        gen.assert_called_once()

    def test_an_unreadable_brief_is_left_alone(self) -> None:
        self.w.file(self.w.briefs).write_bytes(b"not a parquet")
        with patch.object(thematic_cmd.brief_orchestrator, "generate_briefs") as gen:
            result = self._brief()
        self.assertEqual(result.exit_code, 0, msg=result.output)
        gen.assert_not_called()
        self.assertEqual(self.w.file(self.w.briefs).read_bytes(), b"not a parquet")

    def test_an_empty_brief_is_regenerated_before_the_open(self) -> None:
        pd.DataFrame({"ticker": pd.Series([], dtype=str)}).to_parquet(
            self.w.file(self.w.briefs), index=False
        )
        with patch.object(
            thematic_cmd.brief_orchestrator,
            "generate_briefs",
            return_value=_published_brief("2026-09-11T00:40:00Z"),
        ) as gen:
            result = self._brief()
        self.assertEqual(result.exit_code, 0, msg=result.output)
        gen.assert_called_once()


if __name__ == "__main__":
    unittest.main()
