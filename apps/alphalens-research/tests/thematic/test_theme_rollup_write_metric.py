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

import ast
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

    def test_an_unknown_outcome_gets_its_own_series(self):
        # A typo must not publish an all-zero gauge set — all-zero reads as "the
        # stage never ran", which is the one thing a typo must not be able to
        # fake. It gets a distinct label value instead, so the degraded state is
        # visible as itself rather than as a healthy day or a missing exporter.
        metrics = thematic_cmd._theme_rollup_write_metrics("wrote")
        self.assertEqual(_gauge(metrics, "unknown"), 1)
        self.assertEqual(sum(metrics.values()), 1)
        for outcome in ("written", "skipped", "empty", "failed"):
            self.assertEqual(_gauge(metrics, outcome), 0, msg=outcome)

    def test_an_unknown_outcome_never_raises(self):
        # The call sits in the ARGUMENT position of _emit_stage_volume, i.e.
        # outside that function's try/except, and runs AFTER the candidate
        # parquet is on disk. A raise here exits map-themes non-zero and
        # run_thematic_day.sh's `set -euo pipefail` then aborts score, brief and
        # rebuild-cache for the whole day — telemetry costing the briefs, which
        # is exactly what the best-effort write a few functions above forbids.
        for outcome in ("wrote", "", "WRITTEN", "failed "):
            with self.subTest(outcome=outcome):
                thematic_cmd._theme_rollup_write_metrics(outcome)


class TestNoCodePathPassesAnUnknownOutcome(unittest.TestCase):
    """The strictness the runtime gave up, kept where it costs nothing.

    Degrading instead of raising means a typo now ships quietly. It is caught
    here instead: statically, over the real source, at no risk to a production
    run. ``unknown`` is the emitter's fallback, never something a caller names.
    """

    REAL_OUTCOMES = ("written", "skipped", "empty", "failed")

    def _module_ast(self) -> ast.Module:
        source = Path(thematic_cmd.__file__).read_text(encoding="utf-8")
        return ast.parse(source)

    def test_every_literal_handed_to_the_gauge_is_a_real_outcome(self):
        found = []
        for node in ast.walk(self._module_ast()):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name != "_theme_rollup_write_metrics":
                continue
            for arg in node.args:
                if isinstance(arg, ast.Constant):
                    found.append(arg.value)
        self.assertTrue(found, "no literal call sites found — has the helper been renamed?")
        for literal in found:
            self.assertIn(literal, self.REAL_OUTCOMES)

    def test_the_writer_only_ever_returns_a_real_outcome(self):
        # The other way an outcome reaches the gauge: the best-effort writer's
        # return value, threaded through a local and invisible to the check above.
        writer = next(
            node
            for node in ast.walk(self._module_ast())
            if isinstance(node, ast.FunctionDef) and node.name == "_write_theme_rollup_best_effort"
        )
        returns = [
            node.value.value
            for node in ast.walk(writer)
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Constant)
        ]
        conditional = [
            branch.value
            for node in ast.walk(writer)
            if isinstance(node, ast.Return) and isinstance(node.value, ast.IfExp)
            for branch in (node.value.body, node.value.orelse)
            if isinstance(branch, ast.Constant)
        ]
        self.assertTrue(returns + conditional, "the writer returns no string literal at all")
        for literal in returns + conditional:
            self.assertIn(literal, self.REAL_OUTCOMES)


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


class TestATypoInAnOutcomeCannotKillTheDay(unittest.TestCase):
    """The whole day, end to end, with a drifted outcome literal in the code.

    Reproduces the real shape: ``map-themes`` exits non-zero AFTER the candidate
    parquet is already on disk, and no metrics file is written at all — so the
    stage that was supposed to make a hole visible instead makes a bigger one,
    and ``run_thematic_day.sh`` (``set -euo pipefail``) drops score, brief and
    rebuild-cache for the day.
    """

    def test_the_stage_still_exits_zero_and_publishes(self):
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
            # The defect under test: a future edit renames an outcome on one side
            # only. Everything upstream of it succeeded.
            stack.enter_context(
                patch.object(thematic_cmd, "_write_theme_rollup_best_effort", return_value="wrote")
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
            prom_path = root / "metrics" / "alphalens_domain_thematic-map-themes.prom"
            published = prom_path.read_text() if prom_path.exists() else ""

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn(f'{_METRIC}{{outcome="unknown"}} 1', published)
        self.assertIn(f'{_METRIC}{{outcome="written"}} 0', published)
        # Not an all-zero set: that reads as "the stage never ran", and a typo
        # must not be able to fake a stopped exporter.
        self.assertIn('alphalens_thematic_stage_output_rows{stage="map-themes"}', published)


if __name__ == "__main__":
    unittest.main()
