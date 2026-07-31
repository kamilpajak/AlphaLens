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
from alphalens_pipeline.trade_intent.schema import ExitGeometrySpec, ReanchorOnFill


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
        self.assertAlmostEqual(spec.initial_levels.tp, 102.0)

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


if __name__ == "__main__":
    unittest.main()
