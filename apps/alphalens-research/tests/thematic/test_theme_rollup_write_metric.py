"""A rollup that never lands must be visible; a deliberate skip must not read as a loss.

The theme rollup used to be attempted on all six daily slots, so a transient IO
error healed itself a few hours later. It is now written EXACTLY ONCE per asof —
the deciding slot — and the call is best-effort, so the exception is swallowed.
Those two together mean one failed write erases the day from the store for good:
a later slot cannot redo it (the propensities need the deciding slot's event
counts, and the growing events parquet has already overwritten them), and
``--rebuild`` would re-roll the frozen LLM proposal to get them back.

The write stays best-effort — it is telemetry and must never cost the day's
briefs. What changes is that the gap stops being silent: every ``map-themes`` run
publishes a one-hot gauge naming what happened to the rollup, so a permanent hole
is a PromQL query rather than an archaeology exercise.

The states must stay APART. "skipped because the mapper served a frozen set" is
the normal reading on five of six slots; "the write raised" is a hole. A gauge
that reads the same for both would answer no question worth asking.
"""

from __future__ import annotations

import datetime as dt
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from alphalens_cli.commands import thematic as thematic_cmd
from alphalens_cli.main import app
from alphalens_pipeline.thematic.extraction import themes
from alphalens_pipeline.thematic.mapping import orchestrator
from typer.testing import CliRunner

ASOF = dt.date(2026, 8, 5)
_METRIC = "alphalens_thematic_theme_rollup_write"


def _gauge(metrics: dict, outcome: str) -> float | int:
    return metrics[f'{_METRIC}{{outcome="{outcome}"}}']


def _rollup_frame() -> pd.DataFrame:
    """A two-theme rollup shaped like the one ``roll_up`` really returns."""
    day = pd.Timestamp(ASOF, tz="UTC")
    frame = pd.DataFrame(
        [
            {
                "theme": "aa_theme",
                "count_window": 4,
                "count_recent": 4,
                "count_baseline": 0,
                "novelty_score": 8.0,
                "rate_surprise": 2.0,
                "excess_activity": 3.0,
                "first_seen": day,
                "latest_seen": day,
            },
            {
                "theme": "bb_theme",
                "count_window": 3,
                "count_recent": 3,
                "count_baseline": 0,
                "novelty_score": 6.0,
                "rate_surprise": 1.5,
                "excess_activity": 2.0,
                "first_seen": day,
                "latest_seen": day,
            },
        ]
    )
    return themes.apply_tiebreak(frame, asof=ASOF)


def _candidates(*, frozen_reuse: bool) -> pd.DataFrame:
    frame = pd.DataFrame(
        [
            {
                "theme": "aa_theme",
                "ticker": "AAA",
                "gates_passed": ["tenk"],
                "gates_unknown": [],
                "llm_confidence": 0.9,
                "rationale": "serves the theme",
            }
        ]
    )
    frame.attrs[orchestrator.FROZEN_REUSE_ATTR] = frozen_reuse
    return frame


def _run_slot(
    *,
    rollup: pd.DataFrame,
    novel: pd.DataFrame,
    candidates: pd.DataFrame | None,
    write_raises: BaseException | None = None,
    extra_args: tuple[str, ...] = (),
) -> dict:
    """Fire one ``map-themes`` slot; return the metrics dict it emitted.

    Everything below the CLI is stubbed on purpose: the question here is which
    gauge the command publishes for a given rollup outcome, not whether the
    mapper works.
    """
    with ExitStack() as stack:
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        stack.enter_context(patch.dict(os.environ, {"OPENROUTER_API_KEY": "fake"}, clear=False))
        stack.enter_context(
            patch.object(thematic_cmd.themes_mod, "DEFAULT_THEME_ROLLUP_DIR", root / "rollup")
        )
        stack.enter_context(patch.object(thematic_cmd.themes_mod, "roll_up", return_value=rollup))
        stack.enter_context(patch.object(thematic_cmd.themes_mod, "flag_novel", return_value=novel))
        if write_raises is not None:
            stack.enter_context(
                patch.object(
                    thematic_cmd.themes_mod, "write_theme_rollup", side_effect=write_raises
                )
            )
        if candidates is not None:
            stack.enter_context(
                patch.object(thematic_cmd.orchestrator, "map_themes", return_value=candidates)
            )
        emit = stack.enter_context(patch.object(thematic_cmd, "emit_domain_metrics"))
        result = CliRunner().invoke(
            app,
            [
                "thematic",
                "map-themes",
                "--date",
                ASOF.isoformat(),
                "--output-dir",
                str(root / "candidates"),
                *extra_args,
            ],
        )
        if result.exit_code != 0:  # surface the real traceback, not just stdout
            raise AssertionError(result.output) from result.exception
        emit.assert_called_once()
        return emit.call_args.kwargs["metrics"]


class TestTheRollupWriteGaugeIsOneHot(unittest.TestCase):
    """Every outcome is published on every run, so no series ever goes absent."""

    def test_every_outcome_has_a_series(self):
        metrics = thematic_cmd._theme_rollup_write_metrics("written")
        for outcome in thematic_cmd.THEME_ROLLUP_WRITE_OUTCOMES:
            self.assertIn(f'{_METRIC}{{outcome="{outcome}"}}', metrics)

    def test_exactly_one_series_reads_one(self):
        for outcome in thematic_cmd.THEME_ROLLUP_WRITE_OUTCOMES:
            metrics = thematic_cmd._theme_rollup_write_metrics(outcome)
            with self.subTest(outcome=outcome):
                self.assertEqual(_gauge(metrics, outcome), 1)
                self.assertEqual(sum(metrics.values()), 1)

    def test_an_unknown_outcome_is_refused(self):
        # A typo must not publish an all-zero gauge set that reads as "no run".
        with self.assertRaises(ValueError):
            thematic_cmd._theme_rollup_write_metrics("wrote")


class TestTheSlotPublishesWhatHappenedToTheRollup(unittest.TestCase):
    def test_a_deciding_slot_reports_written(self):
        rollup = _rollup_frame()
        metrics = _run_slot(
            rollup=rollup, novel=rollup.copy(), candidates=_candidates(frozen_reuse=False)
        )
        self.assertEqual(_gauge(metrics, "written"), 1)
        self.assertEqual(_gauge(metrics, "failed"), 0)

    def test_a_frozen_slot_reports_skipped_not_failed(self):
        # Five of six slots land here. Reading this as a failure would page
        # nightly; reading a failure as this would hide every real gap.
        rollup = _rollup_frame()
        metrics = _run_slot(
            rollup=rollup, novel=rollup.copy(), candidates=_candidates(frozen_reuse=True)
        )
        self.assertEqual(_gauge(metrics, "skipped"), 1)
        self.assertEqual(_gauge(metrics, "written"), 0)
        self.assertEqual(_gauge(metrics, "failed"), 0)

    def test_a_swallowed_write_failure_reports_failed(self):
        # The exception is still swallowed — the command exits 0 — but the day
        # is now countable instead of merely absent.
        rollup = _rollup_frame()
        metrics = _run_slot(
            rollup=rollup,
            novel=rollup.copy(),
            candidates=_candidates(frozen_reuse=False),
            write_raises=OSError("disk full"),
        )
        self.assertEqual(_gauge(metrics, "failed"), 1)
        self.assertEqual(_gauge(metrics, "written"), 0)

    def test_a_quiet_day_reports_nothing_to_write(self):
        # No events in the window at all: the writer stores no file and there is
        # no gap to chase. Distinct from a failure, and distinct from a skip —
        # no other slot holds this day's rollup either.
        empty = pd.DataFrame()
        metrics = _run_slot(rollup=empty, novel=empty.copy(), candidates=None)
        self.assertEqual(_gauge(metrics, "empty"), 1)
        self.assertEqual(_gauge(metrics, "failed"), 0)
        self.assertEqual(_gauge(metrics, "written"), 0)

    def test_the_gauge_lands_in_the_prom_file(self):
        # End-to-end through the REAL emitter: the mocked tests above never
        # serialize, so the exposition-format label string is unexercised there
        # and a malformed one would reach node_exporter unnoticed.
        with ExitStack() as stack:
            root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            stack.enter_context(
                patch.dict(
                    os.environ,
                    {
                        "OPENROUTER_API_KEY": "fake",
                        "ALPHALENS_TEXTFILE_DIR": str(root / "metrics"),
                    },
                    clear=False,
                )
            )
            rollup = _rollup_frame()
            stack.enter_context(
                patch.object(thematic_cmd.themes_mod, "DEFAULT_THEME_ROLLUP_DIR", root / "rollup")
            )
            stack.enter_context(
                patch.object(thematic_cmd.themes_mod, "roll_up", return_value=rollup)
            )
            stack.enter_context(
                patch.object(thematic_cmd.themes_mod, "flag_novel", return_value=rollup.copy())
            )
            stack.enter_context(
                patch.object(
                    thematic_cmd.orchestrator,
                    "map_themes",
                    return_value=_candidates(frozen_reuse=False),
                )
            )
            result = CliRunner().invoke(
                app,
                [
                    "thematic",
                    "map-themes",
                    "--date",
                    ASOF.isoformat(),
                    "--output-dir",
                    str(root / "candidates"),
                ],
            )
            self.assertEqual(result.exit_code, 0, msg=result.output)
            prom = (root / "metrics" / "alphalens_domain_thematic-map-themes.prom").read_text()

        self.assertIn(f'{_METRIC}{{outcome="written"}} 1', prom)
        self.assertIn(f'{_METRIC}{{outcome="failed"}} 0', prom)

    def test_the_gauge_rides_the_stage_emit(self):
        # ``emit_domain_metrics`` OVERWRITES the job's .prom file, so a second
        # call would delete the volume gauges. One emit, both metric families.
        rollup = _rollup_frame()
        metrics = _run_slot(
            rollup=rollup, novel=rollup.copy(), candidates=_candidates(frozen_reuse=False)
        )
        self.assertIn('alphalens_thematic_stage_output_rows{stage="map-themes"}', metrics)
        self.assertIn(f'{_METRIC}{{outcome="written"}}', metrics)


if __name__ == "__main__":
    unittest.main()
