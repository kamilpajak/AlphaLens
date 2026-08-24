"""The ATR-bracket what-if lens must STATE which entry anchor it replays (#1114).

Two anchors exist for the same bracket policy and they disagree on every partial
fill:

* ``"planned"`` — the alloc-weighted blend over ALL intended entry tiers. This is
  what the live rail places against (``paper/sizing.py::planned_blended_entry``).
* ``"realised"`` — the alloc-weighted blend over the tiers that TOUCHED in the
  bar walk. This is what the lens replayed silently until #1114.

Every number in ``TestSmgAnchorsAreTheMeasuredIncidentNumbers`` was measured on
the SMG live journal of 2026-08-24 (see ``tests/incident_1112_fixture.py`` for
the provenance of each constant).
"""

from __future__ import annotations

import unittest

from alphalens_pipeline.feedback.ladder_replay import (
    atr_bracket_anchor,
    replay_ladder_atr_bracket,
)
from alphalens_pipeline.paper.sizing import build_exit_geometry_spec
from broker_contract.exit_geometry.levels import atr_bracket_levels

from tests.incident_1112_fixture import (
    SMG_ACTUAL_FILL,
    SMG_ATR,
    SMG_AVG_PRICE_GEOMETRY_TP,
    SMG_E1_LIMIT,
    SMG_GEOMETRY_STOP,
    SMG_GEOMETRY_TP,
    SMG_PLANNED_BLEND,
    SMG_REALISED_GEOMETRY_STOP,
    SMG_REALISED_GEOMETRY_TP,
    SMG_TIERS,
    smg_brief_trade_setup,
)

# The bezpazery v1 pinned bracket parameters (memo
# docs/research/bezpazery_lens_design_2026_07_16.md section 2).
_STOP_ATR_MULT = 1.5
_TP_ATR_MULT = 1.5
_TP_FLOOR_FRAC = 0.006


def _bar(t: int, low: float, high: float, close: float) -> dict:
    return {"t": t, "l": low, "h": high, "c": close}


def _bracket(anchor: float) -> tuple[float, float]:
    levels = atr_bracket_levels(
        anchor,
        SMG_ATR,
        stop_atr_mult=_STOP_ATR_MULT,
        tp_atr_mult=_TP_ATR_MULT,
        tp_floor_frac=_TP_FLOOR_FRAC,
    )
    assert levels is not None
    return levels


# Only the TOP tier is reachable: the low stops just under 59.786017 and never
# comes near tier 2 (55.754064). This is the SMG shape -- a single-tier fill.
_SMG_TOP_TIER_ONLY_BARS = [
    _bar(1, 59.70, 59.80, 59.75),
    _bar(2, 59.60, 60.50, 60.40),
]

# The whole ladder fills in one bar (low drops through 53.599998) and the rally
# clears both anchors' take-profits.
_SMG_EVERY_TIER_BARS = [_bar(1, 53.50, 60.00, 59.00)]

_SMG_BARS = _SMG_TOP_TIER_ONLY_BARS


class TestAnchorModeIsRequired(unittest.TestCase):
    """A caller must not be able to pick an anchor by accident (#1114 option 1)."""

    def test_the_lens_cannot_be_called_without_stating_the_anchor(self):
        with self.assertRaises(TypeError):
            replay_ladder_atr_bracket(smg_brief_trade_setup(), _SMG_BARS)  # type: ignore[call-arg]

    def test_an_unknown_anchor_raises_value_error(self):
        # A typo must not fall through to one of the two real modes.
        with self.assertRaises(ValueError):
            replay_ladder_atr_bracket(
                smg_brief_trade_setup(),
                _SMG_BARS,
                anchor="avg_price",  # type: ignore[arg-type]
            )

    def test_the_anchor_seam_also_refuses_an_unknown_anchor(self):
        with self.assertRaises(ValueError):
            atr_bracket_anchor(
                smg_brief_trade_setup(),
                _SMG_BARS,
                anchor="avg_price",  # type: ignore[arg-type]
            )


class TestSmgAnchorsAreTheMeasuredIncidentNumbers(unittest.TestCase):
    """Both anchors reproduce the numbers measured on the SMG live journal."""

    def test_the_planned_anchor_reproduces_the_live_geometry_target(self):
        anchor = atr_bracket_anchor(
            smg_brief_trade_setup(), _SMG_TOP_TIER_ONLY_BARS, anchor="planned"
        )
        assert anchor is not None
        self.assertAlmostEqual(anchor, SMG_PLANNED_BLEND, places=9)
        stop, tp = _bracket(anchor)
        self.assertAlmostEqual(tp, SMG_GEOMETRY_TP, places=9)
        self.assertAlmostEqual(stop, SMG_GEOMETRY_STOP, places=9)

    def test_the_planned_anchor_equals_what_the_live_builder_places_against(self):
        # The planned mode must CALL production's own blend, not re-implement it,
        # so the lens and the money rail cannot drift apart again.
        spec = build_exit_geometry_spec(smg_brief_trade_setup())
        assert spec is not None
        anchor = atr_bracket_anchor(
            smg_brief_trade_setup(), _SMG_TOP_TIER_ONLY_BARS, anchor="planned"
        )
        assert anchor is not None
        _, tp = _bracket(anchor)
        self.assertAlmostEqual(spec.initial_levels.stop, _bracket(anchor)[0], places=9)
        # The take-profit deliberately does NOT match: #1112 step 3 clamps the
        # live target up to the brief's own first tranche (65.25) and the lens
        # does not. The STOP and the ANCHOR are what must agree.
        self.assertGreater(spec.initial_levels.tp, tp)

    def test_the_realised_anchor_is_the_touched_tier_limit_not_the_broker_fill(self):
        anchor = atr_bracket_anchor(
            smg_brief_trade_setup(), _SMG_TOP_TIER_ONLY_BARS, anchor="realised"
        )
        assert anchor is not None
        self.assertAlmostEqual(anchor, SMG_E1_LIMIT, places=9)
        stop, tp = _bracket(anchor)
        self.assertAlmostEqual(tp, SMG_REALISED_GEOMETRY_TP, places=9)
        self.assertAlmostEqual(stop, SMG_REALISED_GEOMETRY_STOP, places=9)
        # The replay fills a tier AT its limit, so "realised" here is the LIMIT
        # of the tier that touched -- NOT the broker's actual fill 59.9261. A
        # true average-fill anchor would give 63.9581, a third number no code
        # path produces today (it is what #1112 step 4 would move live onto).
        self.assertNotAlmostEqual(tp, SMG_ACTUAL_FILL + _TP_ATR_MULT * SMG_ATR, places=3)
        self.assertAlmostEqual(SMG_ACTUAL_FILL + _TP_ATR_MULT * SMG_ATR, SMG_AVG_PRICE_GEOMETRY_TP)

    def test_the_two_anchors_are_far_apart_on_the_incident_ladder(self):
        planned = atr_bracket_anchor(
            smg_brief_trade_setup(), _SMG_TOP_TIER_ONLY_BARS, anchor="planned"
        )
        realised = atr_bracket_anchor(
            smg_brief_trade_setup(), _SMG_TOP_TIER_ONLY_BARS, anchor="realised"
        )
        assert planned is not None and realised is not None
        self.assertAlmostEqual(realised - planned, SMG_E1_LIMIT - SMG_PLANNED_BLEND, places=9)
        self.assertGreater(realised - planned, 4.0)


class TestTheTwoModesDisagreeOnAPartialFill(unittest.TestCase):
    """A single-tier fill makes the two modes report DIFFERENT realized R."""

    def test_only_the_top_tier_fills_and_the_two_modes_return_different_r(self):
        setup = smg_brief_trade_setup()
        planned_r = replay_ladder_atr_bracket(setup, _SMG_TOP_TIER_ONLY_BARS, anchor="planned")
        realised_r = replay_ladder_atr_bracket(setup, _SMG_TOP_TIER_ONLY_BARS, anchor="realised")
        assert planned_r is not None and realised_r is not None
        self.assertNotAlmostEqual(planned_r, realised_r, places=6)

        # PLANNED: the bracket take-profit (59.6277) sits BELOW the tier that
        # filled (59.786017), so the first bar's high takes the whole position
        # out at a LOSS. This is the incident, reproduced as a number.
        expected_planned = (SMG_GEOMETRY_TP - SMG_E1_LIMIT) / (SMG_E1_LIMIT - SMG_GEOMETRY_STOP)
        self.assertLess(expected_planned, 0.0)
        self.assertAlmostEqual(planned_r, expected_planned, places=9)

        # REALISED: the bracket sits AROUND the tier that filled, so neither its
        # take-profit (63.818017) nor its stop (55.754017) is reached and the
        # remainder is marked to the last close.
        last_close = _SMG_TOP_TIER_ONLY_BARS[-1]["c"]
        expected_realised = (last_close - SMG_E1_LIMIT) / (
            SMG_E1_LIMIT - SMG_REALISED_GEOMETRY_STOP
        )
        self.assertGreater(expected_realised, 0.0)
        self.assertAlmostEqual(realised_r, expected_realised, places=9)


class TestTheModesAgreeWhenEveryTierFills(unittest.TestCase):
    """Regression asked for by #1114: with a full ladder fill the anchor choice
    stops mattering, so the two lenses must report the identical number."""

    def test_every_tier_filling_makes_the_two_anchors_identical(self):
        setup = smg_brief_trade_setup()
        planned = atr_bracket_anchor(setup, _SMG_EVERY_TIER_BARS, anchor="planned")
        realised = atr_bracket_anchor(setup, _SMG_EVERY_TIER_BARS, anchor="realised")
        assert planned is not None and realised is not None
        self.assertAlmostEqual(planned, realised, places=12)
        self.assertAlmostEqual(planned, SMG_PLANNED_BLEND, places=9)
        self.assertEqual(
            replay_ladder_atr_bracket(setup, _SMG_EVERY_TIER_BARS, anchor="planned"),
            replay_ladder_atr_bracket(setup, _SMG_EVERY_TIER_BARS, anchor="realised"),
        )

    def test_they_agree_with_no_alloc_weights_because_both_fall_back_to_equal_weight(self):
        # The two blend helpers live in different modules; assert their shared
        # equal-weight fallback rather than assuming it.
        setup = smg_brief_trade_setup()
        for tier in setup["entry_tiers"]:
            del tier["alloc_pct"]
        planned = atr_bracket_anchor(setup, _SMG_EVERY_TIER_BARS, anchor="planned")
        realised = atr_bracket_anchor(setup, _SMG_EVERY_TIER_BARS, anchor="realised")
        assert planned is not None and realised is not None
        self.assertAlmostEqual(planned, realised, places=12)
        self.assertAlmostEqual(planned, sum(p for p, _ in SMG_TIERS) / len(SMG_TIERS), places=9)

    def test_they_agree_when_a_tier_carries_a_non_positive_limit(self):
        # planned_blended_entry DROPS a tier with limit <= 0; parse_ladder KEEPS
        # it but it can never fill (no low reaches it), so both sides end up
        # blending the same three tiers.
        setup = smg_brief_trade_setup()
        setup["entry_tiers"].append({"limit": -5.0, "alloc_pct": 10.0, "tag": "E4"})
        planned = atr_bracket_anchor(setup, _SMG_EVERY_TIER_BARS, anchor="planned")
        realised = atr_bracket_anchor(setup, _SMG_EVERY_TIER_BARS, anchor="realised")
        assert planned is not None and realised is not None
        self.assertAlmostEqual(planned, realised, places=12)

    def test_a_non_finite_tier_limit_makes_the_planned_anchor_none_not_nan(self):
        # planned_blended_entry lets a NaN limit through (NaN <= 0 is False), and
        # a NaN inside the stamped JSON map is not valid JSON for a strict reader.
        setup = smg_brief_trade_setup()
        setup["entry_tiers"][0]["limit"] = float("nan")
        self.assertIsNone(atr_bracket_anchor(setup, _SMG_EVERY_TIER_BARS, anchor="planned"))
        self.assertIsNone(replay_ladder_atr_bracket(setup, _SMG_EVERY_TIER_BARS, anchor="planned"))


class TestBothModesShareTheNoFillGate(unittest.TestCase):
    """The anchor changes the bracket, never the cohort: a path where nothing
    touches is ``None`` under both modes, so the two lenses stay comparable."""

    def test_nothing_fills_under_either_anchor(self):
        setup = smg_brief_trade_setup()
        above_every_tier = [_bar(1, 61.0, 62.0, 61.5)]
        self.assertIsNone(atr_bracket_anchor(setup, above_every_tier, anchor="planned"))
        self.assertIsNone(atr_bracket_anchor(setup, above_every_tier, anchor="realised"))
        self.assertIsNone(replay_ladder_atr_bracket(setup, above_every_tier, anchor="planned"))
        self.assertIsNone(replay_ladder_atr_bracket(setup, above_every_tier, anchor="realised"))


class TestTheTakeProfitFloorIsTheSameLeafOnBothSides(unittest.TestCase):
    """CHARACTERIZATION -- this passes on the code as it stands, deliberately.

    Issue #1114 says the 0.6% take-profit floor is a divergence the lens applies
    and production does not. That premise is wrong in BEHAVIOUR: both sides reach
    the floor through the one shared leaf
    ``broker_contract.exit_geometry.levels.atr_bracket_levels``, which production
    gets via ``AtrBracketPolicy.decide_placement_geometry`` and the lens passes
    ``tp_floor_frac`` into directly. The floor is one-sided only in TELEMETRY --
    the geometry stamp never named it -- which is fixed separately by
    ``test_geometry_stamp_anchor.py``. This test is the deliberate settlement the
    ticket asked for: it is kept so a future edit that moves the floor into one
    side only turns it red.
    """

    _FLOOR_BINDING_SETUP = {
        "status": "OK",
        "disaster_stop": 90.0,
        # ATR is tiny, so 1.5*ATR (0.15) is far under the 0.6% floor (0.6).
        "entry_tiers": [{"limit": 100.0, "alloc_pct": 100.0}],
        "tp_tranches": [],
        "atr": 0.1,
    }
    _EXPECTED_FLOOR_TP = 100.6

    def test_a_binding_floor_produces_the_identical_target_live_and_in_the_planned_lens(self):
        spec = build_exit_geometry_spec(dict(self._FLOOR_BINDING_SETUP))
        assert spec is not None
        self.assertAlmostEqual(spec.initial_levels.tp, self._EXPECTED_FLOOR_TP, places=9)

        bars = [_bar(1, 99.0, 100.2, 100.0)]
        anchor = atr_bracket_anchor(dict(self._FLOOR_BINDING_SETUP), bars, anchor="planned")
        assert anchor is not None
        levels = atr_bracket_levels(
            anchor,
            self._FLOOR_BINDING_SETUP["atr"],
            stop_atr_mult=_STOP_ATR_MULT,
            tp_atr_mult=_TP_ATR_MULT,
            tp_floor_frac=_TP_FLOOR_FRAC,
        )
        assert levels is not None
        self.assertAlmostEqual(levels[1], self._EXPECTED_FLOOR_TP, places=9)
        # Positive control: the floor really is the binding term here.
        self.assertGreater(self._EXPECTED_FLOOR_TP, anchor + _TP_ATR_MULT * 0.1)
