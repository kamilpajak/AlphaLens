"""Tests for ``paper/sizing.py`` — the brief path's exit DECLARATION (#1414) and
``planned_blended_entry``.

Until #1414 this module built an ATR bracket and put it in the document as
``initial_levels``. The deployed policy (``applies_geometry=False``) journaled
those levels and never placed them, so the document said one thing and the
broker saw another. #1414 made presence of levels the whole placement
instruction, which forced this path to stop emitting them: it never wanted them
placed. What is left is a declaration about MANAGING the stop.

The ATR bracket itself did not die — it survives where it is still read, in the
``/edge`` what-if lens and the research replay, both through the shared
``atr_bracket_levels`` leaf. Its numbers are pinned by
``tests/diagnostics/test_exit_policy_replay.py::TestSmgIncidentPin``.
"""

from __future__ import annotations

import unittest

from alphalens_pipeline.paper.sizing import build_exit_declaration, planned_blended_entry
from broker_contract.exit_geometry.registry import resolve_exit_policy
from broker_contract.trade_intent.schema import ExitGeometrySpec, TrailingStop


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
        self.assertAlmostEqual(planned_blended_entry(_setup(entries=[(100.0, 100.0)])), 100.0)

    def test_alloc_weighted_over_all_intended_tiers(self) -> None:
        setup = _setup(entries=[(100.0, 75.0), (90.0, 25.0)])
        self.assertAlmostEqual(planned_blended_entry(setup), 97.5)

    def test_equal_weight_fallback_when_alloc_pct_absent(self) -> None:
        setup = {"entry_tiers": [{"limit": 100.0}, {"limit": 90.0}]}
        self.assertAlmostEqual(planned_blended_entry(setup), 95.0)

    def test_drops_non_positive_limit_tiers(self) -> None:
        setup = {
            "entry_tiers": [
                {"limit": 100.0, "alloc_pct": 50.0},
                {"limit": 0.0, "alloc_pct": 50.0},
            ]
        }
        self.assertAlmostEqual(planned_blended_entry(setup), 100.0)

    def test_none_when_no_entry_tiers(self) -> None:
        self.assertIsNone(planned_blended_entry(_setup(entries=[])))

    def test_none_when_not_a_mapping(self) -> None:
        self.assertIsNone(planned_blended_entry(object()))  # type: ignore[arg-type]


class TestBuildExitDeclaration(unittest.TestCase):
    def test_returns_an_exit_geometry_spec(self) -> None:
        self.assertIsInstance(build_exit_declaration(), ExitGeometrySpec)

    def test_supplies_no_levels_to_place(self) -> None:
        """The regression #1414 turns on.

        Since presence of ``initial_levels`` MEANS "place these", a brief pick
        that still carried them would start placing an ATR bracket that has not
        reached the broker since 2026-08-27.
        """
        self.assertIsNone(build_exit_declaration().initial_levels)

    def test_declares_exactly_one_stop_management_primitive(self) -> None:
        plan = build_exit_declaration().reaction_plan
        self.assertEqual(len(plan), 1)
        self.assertIsInstance(plan[0], TrailingStop)

    def test_the_declaration_carries_the_deployed_numbers(self) -> None:
        """Read off the registry, not retyped — a drift between what the
        document declares and what the policy does is the #1236 defect."""
        deployed = resolve_exit_policy("breakeven_trail")
        trail = build_exit_declaration().reaction_plan[0]
        assert isinstance(trail, TrailingStop)
        self.assertAlmostEqual(trail.arm_trigger_r, deployed.activation_r)
        self.assertAlmostEqual(trail.trail_frac, deployed.trail_frac)

    def test_never_returns_none(self) -> None:
        """The old builder returned ``None`` on a missing / degenerate ATR, and
        a pick with no exit declares nothing and never moves its stop. There is
        no bracket left to fail to build, so that silent opt-out is gone."""
        self.assertIsNotNone(build_exit_declaration())

    def test_does_not_read_the_brief(self) -> None:
        """Positive control for the paragraph above: the declaration is the same
        for every pick, so no brief shape can suppress it."""
        self.assertEqual(build_exit_declaration(), build_exit_declaration())


if __name__ == "__main__":
    unittest.main()
