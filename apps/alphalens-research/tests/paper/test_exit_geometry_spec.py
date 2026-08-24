"""Tests for ``paper/sizing.py::build_exit_geometry_spec`` / ``planned_blended_entry``
-- the PR-6a dark exit-geometry shadow-stamp build (memo
``docs/research/broker_manager_extraction_and_exit_geometry_2026_07_31.md``
section 4.1 / 4.3).

Pins that the ATR bracket built here matches the SAME ``atr_bracket_1p5``
policy the ``/edge`` replay leaf (``feedback/ladder_replay.py::
replay_ladder_atr_bracket``) reads off an IDENTICAL setup dict, so "live ==
replay via shared formula" holds for the anchor facts (blend, ATR, ceiling).
The one deliberate divergence: replay anchors the blend at tiers that
actually TOUCHED in a bar-replay walk; at placement time no bars/fills exist
yet, so the blend here is the alloc-weighted mean over ALL intended entry
tiers (the "planned" blend, memo section 4.3).
"""

from __future__ import annotations

import unittest

from alphalens_pipeline.feedback.ladder_replay import replay_ladder_atr_bracket
from alphalens_pipeline.paper.sizing import build_exit_geometry_spec, planned_blended_entry
from broker_contract.exit_geometry.registry import resolve_policy
from broker_contract.trade_intent.schema import ExitGeometrySpec, ReanchorOnFill

from tests.incident_1112_fixture import (
    SMG_ATR,
    SMG_GEOMETRY_STOP,
    SMG_GEOMETRY_TP,
    SMG_PLANNED_BLEND,
    SMG_TIERS,
    SMG_TP_TRANCHES,
    smg_brief_trade_setup,
)


def _bar(t: int, low: float, high: float, close: float) -> dict:
    return {"t": t, "l": low, "h": high, "c": close}


def _setup(
    *,
    entries: list[tuple[float, float]],
    tps: list[tuple[float, float]] | None = None,
    stop: float = 90.0,
    status: str = "OK",
    atr: float | None = None,
) -> dict:
    setup: dict = {
        "status": status,
        "disaster_stop": stop,
        "entry_tiers": [{"limit": p, "alloc_pct": w} for p, w in entries],
        "tp_tranches": [{"target": p, "tranche_pct": w} for p, w in (tps or [])],
    }
    if atr is not None:
        setup["atr"] = atr
    return setup


class TestPlannedBlendedEntry(unittest.TestCase):
    def test_single_tier_returns_the_limit(self) -> None:
        setup = _setup(entries=[(100.0, 100.0)])
        self.assertAlmostEqual(planned_blended_entry(setup), 100.0)

    def test_alloc_weighted_over_all_intended_tiers(self) -> None:
        # 100@50 + 98@50 -> 99, regardless of whether either would fill.
        setup = _setup(entries=[(100.0, 50.0), (98.0, 50.0)])
        self.assertAlmostEqual(planned_blended_entry(setup), 99.0)

    def test_equal_weight_fallback_when_alloc_pct_absent(self) -> None:
        setup = {"entry_tiers": [{"limit": 100.0}, {"limit": 98.0}]}
        self.assertAlmostEqual(planned_blended_entry(setup), 99.0)

    def test_drops_non_positive_limit_tiers(self) -> None:
        setup = {
            "entry_tiers": [
                {"limit": 100.0, "alloc_pct": 50.0},
                {"limit": 0.0, "alloc_pct": 50.0},
            ]
        }
        self.assertAlmostEqual(planned_blended_entry(setup), 100.0)

    def test_none_when_no_entry_tiers(self) -> None:
        self.assertIsNone(planned_blended_entry({"entry_tiers": []}))
        self.assertIsNone(planned_blended_entry({}))

    def test_none_when_not_a_mapping(self) -> None:
        self.assertIsNone(planned_blended_entry("not a dict"))  # type: ignore[arg-type]
        self.assertIsNone(planned_blended_entry(None))  # type: ignore[arg-type]


class TestBuildExitGeometrySpec(unittest.TestCase):
    def test_returns_an_exit_geometry_spec(self) -> None:
        setup = _setup(entries=[(100.0, 100.0)], atr=2.0)
        spec = build_exit_geometry_spec(setup)
        self.assertIsInstance(spec, ExitGeometrySpec)

    def test_single_tier_levels_match_the_pinned_bezpazery_v1_bracket(self) -> None:
        # Same fixture as TestAtrBracketWhatIf._BRACKET_SETUP in
        # test_feedback_ladder_replay.py: blended=100, atr=2 ->
        # stop = 100 - 1.5*2 = 97, tp = max(100.6, 103) = 103 (uncapped).
        setup = _setup(entries=[(100.0, 100.0)], tps=[(101.0, 100.0)], stop=90.0, atr=2.0)
        spec = build_exit_geometry_spec(setup)
        assert spec is not None
        self.assertAlmostEqual(spec.initial_levels.stop, 97.0)
        self.assertAlmostEqual(spec.initial_levels.tp, 103.0)

    def test_levels_byte_identical_to_the_numeric_registry_policy(self) -> None:
        # Task 4 routes the build through the BEHAVIORAL ExitPolicy
        # (``resolve_exit_policy("atr_bracket_1p5").decide_placement_geometry``)
        # instead of the numeric registry's ``resolve_policy(...).levels(...)``.
        # Both must produce byte-identical stop/tp AND the reanchor k_atr must
        # stay the policy's pinned 1.5x — a pure name→registry refactor of WHICH
        # policy decides, not WHAT it decides.
        setup = _setup(entries=[(100.0, 50.0), (98.0, 50.0)], tps=[(101.0, 100.0)], atr=2.0)
        blended = planned_blended_entry(setup)
        assert blended is not None
        numeric = resolve_policy("atr_bracket_1p5")
        expected = numeric.levels(blended, 2.0, ceiling_price=None)
        assert expected is not None
        expected_stop, expected_tp = expected

        spec = build_exit_geometry_spec(setup)
        assert spec is not None
        self.assertAlmostEqual(spec.initial_levels.stop, expected_stop)
        self.assertAlmostEqual(spec.initial_levels.tp, expected_tp)
        self.assertAlmostEqual(spec.reaction_plan[0].k_atr, numeric.stop_atr_mult)
        self.assertAlmostEqual(spec.reaction_plan[0].k_atr, 1.5)

    def test_reaction_plan_carries_a_single_reanchor_on_fill(self) -> None:
        setup = _setup(entries=[(100.0, 100.0)], atr=2.0)
        spec = build_exit_geometry_spec(setup)
        assert spec is not None
        self.assertEqual(len(spec.reaction_plan), 1)
        reanchor = spec.reaction_plan[0]
        self.assertIsInstance(reanchor, ReanchorOnFill)
        self.assertAlmostEqual(reanchor.k_atr, 1.5)  # atr_bracket_1p5 pinned stop_atr_mult
        self.assertAlmostEqual(reanchor.atr, 2.0)
        self.assertIsNone(reanchor.ceiling_price)

    def test_ceiling_derived_from_pct_off_52w_high_kwarg_not_the_setup_dict(self) -> None:
        # technical_pct_off_52w_high is NOT a key inside brief_trade_setup (it is
        # a sibling column on the candidate/brief row) -- confirmed against
        # population_ladder_monitor.py's _replay_candidate call site. It must be
        # passed in explicitly; a key of the same name INSIDE the setup dict is
        # NOT read.
        setup = _setup(entries=[(100.0, 100.0)], tps=[(101.0, 100.0)], stop=90.0, atr=2.0)
        setup["asof_close"] = 100.0
        setup["technical_pct_off_52w_high"] = -2.0  # would NOT be read from here
        spec_ignored = build_exit_geometry_spec(setup)
        assert spec_ignored is not None
        self.assertAlmostEqual(spec_ignored.initial_levels.tp, 103.0)  # uncapped, kwarg omitted

        spec_capped = build_exit_geometry_spec(setup, pct_off_52w_high=-2.0)
        assert spec_capped is not None
        ceiling = spec_capped.reaction_plan[0].ceiling_price
        self.assertIsNotNone(ceiling)
        self.assertAlmostEqual(spec_capped.initial_levels.tp, min(103.0, ceiling))

    def test_multi_tier_blend_matches_replay_when_every_tier_fills(self) -> None:
        # Mirrors TestAtrBracketWhatIf.test_multi_tier_fills_anchor_bracket_at_blended_entry:
        # both tiers fill on bar 1 -> blend 99, bracket stop 96, TP 102 (rally hits it).
        setup = _setup(
            entries=[(100.0, 50.0), (98.0, 50.0)], tps=[(120.0, 100.0)], stop=90.0, atr=2.0
        )
        bars = [_bar(1, 97.5, 100.0, 98.0), _bar(2, 98.0, 102.5, 102.0)]
        replayed_r = replay_ladder_atr_bracket(setup, bars)
        self.assertAlmostEqual(replayed_r, 1.0, places=6)

        spec = build_exit_geometry_spec(setup)
        assert spec is not None
        self.assertAlmostEqual(spec.initial_levels.stop, 96.0)
        # DIVERGENCE, deliberate (issue #1112 step 3): live and the /edge replay
        # lens no longer agree on the TAKE-PROFIT. The unclamped ATR target here
        # is 102.0, which the replay still uses; live now clamps it up to the
        # brief's own first tranche (120.0). ``ladder_replay`` is NOT given the
        # same clamp on purpose — retro-fitting it would rewrite the historical
        # what-if series that issues #1114 / #1115 depend on. The STOP and the
        # anchor blend (asserted above) still match.
        self.assertAlmostEqual(spec.initial_levels.tp, 120.0)

    def test_none_when_no_entry_tiers(self) -> None:
        setup = _setup(entries=[], atr=2.0)
        self.assertIsNone(build_exit_geometry_spec(setup))

    def test_none_when_atr_missing(self) -> None:
        setup = _setup(entries=[(100.0, 100.0)])
        self.assertIsNone(build_exit_geometry_spec(setup))

    def test_none_when_atr_non_positive(self) -> None:
        setup = _setup(entries=[(100.0, 100.0)], atr=0.0)
        self.assertIsNone(build_exit_geometry_spec(setup))

    def test_none_when_atr_nan(self) -> None:
        setup = _setup(entries=[(100.0, 100.0)], atr=float("nan"))
        self.assertIsNone(build_exit_geometry_spec(setup))

    def test_none_when_bracket_stop_at_or_below_zero(self) -> None:
        # ATR=80 -> bracket stop = 100 - 1.5*80 = -20: not constructible.
        setup = _setup(entries=[(100.0, 100.0)], atr=80.0)
        self.assertIsNone(build_exit_geometry_spec(setup))

    def test_none_when_ceiling_at_or_below_cost_floor(self) -> None:
        setup = _setup(entries=[(100.0, 100.0)], atr=2.0)
        setup["asof_close"] = 100.0
        # ceiling = asof_close / (1 + pct/100); pct=0.0 -> ceiling=100.0 <= cost
        # floor 100.6 -> degenerate bracket.
        self.assertIsNone(build_exit_geometry_spec(setup, pct_off_52w_high=0.0))

    def test_none_on_non_dict_input(self) -> None:
        self.assertIsNone(build_exit_geometry_spec(object()))  # type: ignore[arg-type]
        self.assertIsNone(build_exit_geometry_spec(None))  # type: ignore[arg-type]


class TestIncidentAnchorsReconcile(unittest.TestCase):
    """Anchor check for issue #1112: the constants in
    ``tests/incident_1112_fixture.py`` are the numbers the REAL builder produces
    for the SMG setup. If this breaks, the anchor arithmetic changed and every
    other #1112 test is measuring something else."""

    def test_the_smg_blend_and_stop_are_reproduced_to_1e_9(self) -> None:
        setup = smg_brief_trade_setup()
        self.assertAlmostEqual(planned_blended_entry(setup), SMG_PLANNED_BLEND, places=9)
        spec = build_exit_geometry_spec(setup)
        assert spec is not None
        self.assertAlmostEqual(spec.initial_levels.stop, SMG_GEOMETRY_STOP, places=9)

    def test_the_unclamped_atr_target_sits_below_the_top_entry_tier(self) -> None:
        # The defect itself, stated as arithmetic: blend + 1.5*ATR = 59.6277 is
        # BELOW the top tier limit 59.786017, so a fill on that tier is already
        # past its own take-profit.
        self.assertLess(SMG_GEOMETRY_TP, SMG_TIERS[0][0])


class TestTargetNeverBelowFirstBriefTranche(unittest.TestCase):
    """Issue #1112 step 3: the policy may not place the take-profit below the
    brief's own FIRST take-profit tranche — the take-profit-side mirror of the
    never-below-brief-floor rule ``clamp_reanchor_target`` already enforces on
    the stop side."""

    def test_the_smg_target_is_clamped_up_to_the_first_brief_tranche(self) -> None:
        spec = build_exit_geometry_spec(smg_brief_trade_setup())
        assert spec is not None
        self.assertAlmostEqual(spec.initial_levels.tp, SMG_TP_TRANCHES[0])

    def test_the_clamp_touches_the_take_profit_only(self) -> None:
        # Step 4 (the take-profit half of the PR-6b re-anchor) is explicitly NOT
        # in this change: the stop and the ReanchorOnFill reaction plan must come
        # out exactly as before.
        spec = build_exit_geometry_spec(smg_brief_trade_setup())
        assert spec is not None
        self.assertAlmostEqual(spec.initial_levels.stop, SMG_GEOMETRY_STOP, places=9)
        self.assertEqual(len(spec.reaction_plan), 1)
        reanchor = spec.reaction_plan[0]
        self.assertIsInstance(reanchor, ReanchorOnFill)
        self.assertAlmostEqual(reanchor.k_atr, 1.5)
        self.assertAlmostEqual(reanchor.atr, SMG_ATR)

    def test_a_first_tranche_below_the_atr_target_leaves_it_unchanged(self) -> None:
        # max() semantics: the clamp is a FLOOR, never a cap. Pinned fixture:
        # blended 100, atr 2 -> tp 103; first tranche 101 -> still 103.
        setup = _setup(entries=[(100.0, 100.0)], tps=[(101.0, 100.0)], stop=90.0, atr=2.0)
        spec = build_exit_geometry_spec(setup)
        assert spec is not None
        self.assertAlmostEqual(spec.initial_levels.tp, 103.0)

    def test_no_tranches_leaves_the_target_unchanged(self) -> None:
        setup = _setup(entries=[(100.0, 100.0)], tps=[], atr=2.0)
        spec = build_exit_geometry_spec(setup)
        assert spec is not None
        self.assertAlmostEqual(spec.initial_levels.tp, 103.0)

    def test_a_degenerate_first_tranche_leaves_the_target_unchanged(self) -> None:
        for label, tranches in (
            ("zero", [{"target": 0.0}]),
            ("negative", [{"target": -5.0}]),
            ("nan", [{"target": float("nan")}]),
            ("missing key", [{"tranche_pct": 100.0}]),
            ("not a mapping", ["120.0"]),
        ):
            with self.subTest(case=label):
                setup = _setup(entries=[(100.0, 100.0)], atr=2.0)
                setup["tp_tranches"] = tranches
                spec = build_exit_geometry_spec(setup)
                assert spec is not None
                self.assertAlmostEqual(spec.initial_levels.tp, 103.0)

    def test_the_clamp_wins_over_the_52w_ceiling(self) -> None:
        # DECIDED, not incidental: the brief tranche is a research level the
        # strategy committed to, the 52w ceiling is a do-not-chase heuristic. A
        # target below the brief's own first tranche is the defect this issue
        # exists to close, so the floor outranks the cap. Fixture: blended 100,
        # atr 2, asof_close 100, pct_off -2 -> ceiling 102.0408; first tranche
        # 120 -> 120, above the ceiling.
        setup = _setup(entries=[(100.0, 100.0)], tps=[(120.0, 100.0)], stop=90.0, atr=2.0)
        setup["asof_close"] = 100.0
        spec = build_exit_geometry_spec(setup, pct_off_52w_high=-2.0)
        assert spec is not None
        ceiling = spec.reaction_plan[0].ceiling_price
        assert ceiling is not None
        self.assertGreater(120.0, ceiling)
        self.assertAlmostEqual(spec.initial_levels.tp, 120.0)


if __name__ == "__main__":
    unittest.main()
