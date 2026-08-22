"""The stored rollup must describe the slate the mapper actually acted upon.

``map-themes`` runs SIX times on the same asof. The mapper is frozen per
``(asof, mapper_config_version)``: slot 1 proposes, slots 2-6 reuse slot 1's
parquet and ignore the ``themes`` argument entirely. The rollup, meanwhile, is
recomputed from that slot's grown event counts — new scores, new ranks, a new
draw. Written unconditionally, it therefore records a slate that was never
mapped.

That is fatal rather than cosmetic: a ``selection_propensity`` of 0.0 attached to
a theme that WAS mapped is an infinite inverse-propensity weight, and the
off-policy estimator these columns exist to enable becomes undefined rather than
merely noisy. Dropping the affected rows is not a repair either — it conditions
on the outcome.

So the rollup is written AFTER the mapper returns, and only when the mapper
really re-derived the slate. A day whose rollup is missing is honest; a day whose
rollup is wrong is not.
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
from alphalens_pipeline.thematic.mapping import orchestrator, theme_mapper
from alphalens_pipeline.thematic.mapping.catalyst_contract import CatalystPayload
from typer.testing import CliRunner

from tests.thematic.mapping_stubs import patch_assessor, theme_proposal

ASOF = dt.date(2026, 8, 5)
# Two slots wide, so a change in the day's event counts moves the whole slate
# rather than one boundary member — the divergence under test is then visible in
# the `selected` set instead of hiding inside a tie.
MAX_THEMES = 2

_CATALYST = CatalystPayload(
    url="https://example.com/catalyst",
    title="Stub catalyst",
    published_at=ASOF.isoformat(),
    event_type="contract_award",
    primary_entities=[],
    confidence=0.8,
    second_order_implications=[],
    echo_count=1,
    trigger_url="https://example.com/catalyst",
    trigger_published_at=ASOF.isoformat(),
    is_amplified=False,
    template_id=None,
    template_facts=None,
)


def _event_row(news_id: str, theme: str) -> dict:
    return {
        "news_id": news_id,
        "event_type": "product_launch",
        "primary_entities": [],
        "themes": [theme],
        "sentiment": "positive",
        "second_order_implications": [],
        "confidence": 0.8,
        "model": "deepseek-v4-flash",
        "extracted_at": pd.Timestamp(ASOF, tz="UTC"),
    }


def _write_events(events_dir: Path, counts: dict[str, int]) -> None:
    """(Re)write the asof event parquet with ``counts`` events per theme.

    ``extract`` appends to the same asof file on every slot, so the day's counts
    GROW between slots and the rollup's ranking moves with them. That is the
    whole mechanism this file is about.
    """
    rows = [_event_row(f"{theme}-{i}", theme) for theme, n in counts.items() for i in range(n)]
    events_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(events_dir / f"{ASOF.isoformat()}.parquet", index=False)


def _candidate_row(theme: str) -> dict:
    return {
        "theme": theme,
        "ticker": "AAA",
        "company_name": "Example Corp",
        "rationale": "serves the theme",
        "llm_confidence": 0.9,
        "market_cap": 1_000_000_000,
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
        "gate_verdict_json": "{}",
        "source_event_url": "https://example.com/news",
        "source_event_title": "headline",
        "source_event_published_at": ASOF.isoformat(),
        "theme_search_keywords": ["kw"],
    }


class _Slots:
    """One temp world: events in, candidates + rollup out, six slots to fire."""

    def __init__(self, stack: ExitStack) -> None:
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.events_dir = root / "events"
        self.output_dir = root / "candidates"
        self.rollup_dir = root / "rollup"
        self.runner = CliRunner()
        self.mapped: list[list[str]] = []
        stack.enter_context(patch.dict(os.environ, {"OPENROUTER_API_KEY": "fake"}, clear=False))
        stack.enter_context(
            patch.object(thematic_cmd.themes_mod, "DEFAULT_THEME_ROLLUP_DIR", self.rollup_dir)
        )

    def run(self, *extra_args: str):
        """Fire one slot through the REAL mapper (only the LLM legs are stubbed).

        The freeze must be genuine here: a patched-wholesale ``map_themes`` would
        assert nothing about the reuse branch that causes the defect.
        """
        real_map_themes = orchestrator.map_themes

        def _spy(*, themes, **kwargs):
            self.mapped.append(list(themes))
            return real_map_themes(themes=themes, **kwargs)

        with (
            patch_assessor(),
            patch.object(orchestrator, "map_themes", side_effect=_spy),
            patch.object(orchestrator, "_resolve_catalyst", return_value=_CATALYST),
            patch.object(
                orchestrator,
                "_propose_and_bracket",
                return_value=theme_proposal(
                    proposed=[{"ticker": "AAA"}],
                    in_bracket={"AAA": 1_000_000_000.0},
                    outcome=theme_mapper.MapperOutcome.SUCCESS,
                ),
            ),
            patch.object(
                orchestrator,
                "_verify_candidates_for_theme",
                side_effect=lambda *, theme, **_kw: ([_candidate_row(theme)], 0, 0),
            ),
            patch.object(orchestrator, "_init_pro_client"),
            patch.object(orchestrator, "_fetch_press_window", return_value=None),
        ):
            result = self.runner.invoke(
                app,
                [
                    "thematic",
                    "map-themes",
                    "--date",
                    ASOF.isoformat(),
                    "--events-dir",
                    str(self.events_dir),
                    "--output-dir",
                    str(self.output_dir),
                    "--max-themes",
                    str(MAX_THEMES),
                    *extra_args,
                ],
            )
        if result.exit_code != 0:  # surface the real traceback, not just stdout
            raise AssertionError(result.output) from result.exception
        return result

    @property
    def rollup_path(self) -> Path:
        return self.rollup_dir / f"{ASOF.isoformat()}.parquet"

    @property
    def candidates_path(self) -> Path:
        return self.output_dir / f"{ASOF.isoformat()}.parquet"

    def rollup(self) -> pd.DataFrame:
        return pd.read_parquet(self.rollup_path)

    def candidates(self) -> pd.DataFrame:
        return pd.read_parquet(self.candidates_path)

    def selected(self) -> set[str]:
        frame = self.rollup()
        return set(frame.loc[frame["selected"], "theme"])

    def propensities(self) -> dict[str, float]:
        frame = self.rollup()
        return dict(zip(frame["theme"], frame["selection_propensity"], strict=True))


class TestRollupMatchesTheMappedSlate(unittest.TestCase):
    def test_six_slots_leave_the_slot_one_rollup_untouched(self):
        # Slot 1 maps {aa, bb}. Slots 2-6 see counts that would rank {cc, dd}
        # first, but the mapper is frozen and never sees them. The rollup on disk
        # must still describe slot 1's decision.
        with ExitStack() as stack:
            slots = _Slots(stack)
            _write_events(slots.events_dir, {"aa_theme": 4, "bb_theme": 4, "cc_theme": 1})
            slots.run()
            after_first = slots.rollup()

            _write_events(
                slots.events_dir,
                {"aa_theme": 4, "bb_theme": 4, "cc_theme": 30, "dd_theme": 30},
            )
            for _ in range(5):
                slots.run()
            after_sixth = slots.rollup()

        pd.testing.assert_frame_equal(after_first, after_sixth)

    def test_the_recorded_slate_is_the_one_the_mapper_used(self):
        with ExitStack() as stack:
            slots = _Slots(stack)
            _write_events(slots.events_dir, {"aa_theme": 4, "bb_theme": 4, "cc_theme": 1})
            slots.run()
            mapped_slate = set(slots.mapped[0])

            _write_events(
                slots.events_dir,
                {"aa_theme": 4, "bb_theme": 4, "cc_theme": 30, "dd_theme": 30},
            )
            slots.run()
            recorded = slots.selected()

        self.assertEqual(len(mapped_slate), MAX_THEMES)
        self.assertEqual(recorded, mapped_slate)

    def test_a_theme_the_mapper_used_never_carries_a_zero_propensity(self):
        # The failure this whole file exists to prevent, stated as arithmetic: a
        # mapped theme recorded at propensity 0.0 is an infinite IPS weight.
        with ExitStack() as stack:
            slots = _Slots(stack)
            _write_events(slots.events_dir, {"aa_theme": 4, "bb_theme": 4, "cc_theme": 1})
            slots.run()
            mapped_slate = set(slots.mapped[0])

            _write_events(
                slots.events_dir,
                {"aa_theme": 4, "bb_theme": 4, "cc_theme": 30, "dd_theme": 30},
            )
            slots.run()
            prop = slots.propensities()

        for theme in mapped_slate:
            self.assertGreater(prop[theme], 0.0, msg=theme)

    def test_a_zero_novel_rerun_never_zeroes_a_mapped_themes_propensity(self):
        # The SAME invariant, reached down the other branch. When a rerun of an
        # already-decided day computes zero novel themes — an operator raising
        # --novelty-threshold, lowering --max-themes, or a shrunk re-extract —
        # the zero-novel path used to rewrite the whole day at propensity 0.0,
        # mapped themes included. Identical arithmetic, identical damage.
        with ExitStack() as stack:
            slots = _Slots(stack)
            _write_events(slots.events_dir, {"aa_theme": 4, "bb_theme": 4, "cc_theme": 1})
            slots.run()
            mapped_slate = set(slots.mapped[0])

            slots.run("--novelty-threshold", "999")
            prop = slots.propensities()

        self.assertEqual(len(mapped_slate), MAX_THEMES)
        for theme in mapped_slate:
            self.assertGreater(prop[theme], 0.0, msg=theme)

    def test_rebuild_recomputes_the_slate_and_the_rollup_together(self):
        # The escape hatch: --rebuild re-rolls the mapper, so the rollup must
        # follow it to the new slate rather than staying on the frozen one.
        with ExitStack() as stack:
            slots = _Slots(stack)
            _write_events(slots.events_dir, {"aa_theme": 4, "bb_theme": 4, "cc_theme": 1})
            slots.run()

            _write_events(
                slots.events_dir,
                {"aa_theme": 4, "bb_theme": 4, "cc_theme": 30, "dd_theme": 30},
            )
            slots.run("--rebuild")
            recorded = slots.selected()

        self.assertEqual(recorded, set(slots.mapped[-1]))
        self.assertEqual(recorded, {"cc_theme", "dd_theme"})

    def test_the_rollup_write_is_skipped_entirely_on_a_frozen_slot(self):
        # Not "written and then corrected" — never written. A missing rollup for
        # a slot is the honest record of a slot that made no selection decision.
        with ExitStack() as stack:
            slots = _Slots(stack)
            _write_events(slots.events_dir, {"aa_theme": 4, "bb_theme": 4, "cc_theme": 1})
            slots.run()
            slots.rollup_path.unlink()

            slots.run()

            self.assertFalse(
                slots.rollup_path.exists(),
                msg="a frozen slot re-derived no slate, so it has nothing to record",
            )


class TestAZeroNovelSlotCannotEraseADecidedDay(unittest.TestCase):
    """The zero-novel branch never consulted the freeze, so it overwrote both files.

    ``write_empty_candidates`` replaces the day's candidate parquet outright, and
    the rollup write that follows it re-describes the day as an empty draw. Run
    against an asof that was ALREADY mapped, that erases a real slate and rewrites
    every mapped theme to ``selection_propensity`` 0.0 — the infinite
    inverse-propensity weight the sibling class exists to prevent, arrived at from
    the other side.

    Production's 6x/day cron cannot reach it today: it passes no flags, and within
    one asof the event counts only grow, so a day that had novel themes keeps
    having them. An operator rerun with a different ``--novelty-threshold`` or
    ``--max-themes``, or a re-extract that shrank the day, reaches it immediately.

    A day that has a mapped, non-empty slate is DECIDED. Only ``--rebuild`` may
    take that back, because only ``--rebuild`` recomputes the slate to replace it.
    """

    def _decided_day(self, stack: ExitStack) -> _Slots:
        slots = _Slots(stack)
        _write_events(slots.events_dir, {"aa_theme": 4, "bb_theme": 4, "cc_theme": 1})
        slots.run()
        return slots

    def test_the_candidate_parquet_survives_a_zero_novel_slot(self):
        with ExitStack() as stack:
            slots = self._decided_day(stack)
            before = slots.candidates()

            slots.run("--novelty-threshold", "999")
            after = slots.candidates()

        self.assertFalse(before.empty)
        pd.testing.assert_frame_equal(before, after)

    def test_the_rollup_survives_a_zero_novel_slot(self):
        with ExitStack() as stack:
            slots = self._decided_day(stack)
            before = slots.rollup()

            slots.run("--novelty-threshold", "999")
            after = slots.rollup()

        pd.testing.assert_frame_equal(before, after)

    def test_the_mapper_is_not_called_to_defend_the_day(self):
        # The day is preserved by NOT writing, not by re-deriving it. A rerun
        # that re-rolled the frozen LLM proposal to keep the file alive would be
        # a different (and expensive) bug.
        with ExitStack() as stack:
            slots = self._decided_day(stack)
            calls_after_decision = len(slots.mapped)

            slots.run("--novelty-threshold", "999")

            self.assertEqual(len(slots.mapped), calls_after_decision)

    def test_rebuild_still_recomputes_a_zero_novel_day(self):
        # The escape hatch stays open: --rebuild is the operator saying "replace
        # what is there", and an empty draw is a legitimate thing to replace it
        # with. Without this the fix would be a lock, not a guard.
        with ExitStack() as stack:
            slots = self._decided_day(stack)

            slots.run("--novelty-threshold", "999", "--rebuild")
            candidates = slots.candidates()
            selected = slots.selected()
            prop = slots.propensities()

        self.assertEqual(len(candidates), 0)
        self.assertEqual(selected, set())
        self.assertEqual(set(prop.values()), {0.0})

    def test_a_first_ever_zero_novel_slot_still_writes_the_empty_day(self):
        # Positive control: with nothing on disk to protect, the quiet-day
        # contract is unchanged — `score` still needs the typed-empty parquet or
        # run_thematic_day.sh's `set -e` aborts before brief + rebuild-cache.
        with ExitStack() as stack:
            slots = _Slots(stack)
            _write_events(slots.events_dir, {"aa_theme": 4, "bb_theme": 4, "cc_theme": 1})

            slots.run("--novelty-threshold", "999")
            candidates = slots.candidates()

        self.assertEqual(len(candidates), 0)


class TestFrozenReuseIsReportedByTheMapper(unittest.TestCase):
    """The CLI cannot know the mapper ignored its slate unless the mapper says so."""

    def _map(self, *, rebuild: bool = False):
        with ExitStack() as stack:
            out = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            stack.enter_context(patch_assessor())
            stack.enter_context(
                patch.object(orchestrator, "_resolve_catalyst", return_value=_CATALYST)
            )
            stack.enter_context(
                patch.object(
                    orchestrator,
                    "_propose_and_bracket",
                    return_value=theme_proposal(
                        proposed=[{"ticker": "AAA"}],
                        in_bracket={"AAA": 1_000_000_000.0},
                        outcome=theme_mapper.MapperOutcome.SUCCESS,
                    ),
                )
            )
            stack.enter_context(
                patch.object(
                    orchestrator,
                    "_verify_candidates_for_theme",
                    side_effect=lambda *, theme, **_kw: ([_candidate_row(theme)], 0, 0),
                )
            )
            stack.enter_context(patch.object(orchestrator, "_init_pro_client"))
            stack.enter_context(
                patch.object(orchestrator, "_fetch_press_window", return_value=None)
            )
            fresh = orchestrator.map_themes(themes=["aa_theme"], asof=ASOF, output_dir=out)
            second = orchestrator.map_themes(
                themes=["bb_theme"], asof=ASOF, output_dir=out, rebuild=rebuild
            )
        return fresh, second

    def test_a_fresh_run_reports_no_reuse(self):
        fresh, _second = self._map()
        self.assertIs(fresh.attrs[orchestrator.FROZEN_REUSE_ATTR], False)

    def test_a_frozen_rerun_reports_the_reuse(self):
        _fresh, second = self._map()
        self.assertIs(second.attrs[orchestrator.FROZEN_REUSE_ATTR], True)

    def test_rebuild_reports_no_reuse(self):
        _fresh, second = self._map(rebuild=True)
        self.assertIs(second.attrs[orchestrator.FROZEN_REUSE_ATTR], False)


if __name__ == "__main__":
    unittest.main()
