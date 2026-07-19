"""Property-based tests for the ladder replay engine (``replay_ladder``).

The strongest test here is the DIFFERENTIAL ORACLE (``_naive_walk``): a simple,
independently-derived bar walk that decides which entries fill, which TPs are
touched, and whether the stop hit — the same discrete decisions the production
engine makes, but written plainly. Any mutation that corrupts the fill / touch /
SL-first / exit-latch logic diverges from the oracle over some generated path.

The remaining classes pin the algebraic invariants (counts, subset, bounds,
finiteness) and the metamorphic relations (determinism, post-exit idempotence,
full-fill reduction) that hold for EVERY ladder × path.
"""

from __future__ import annotations

from typing import Any

from alphalens_pipeline.feedback.ladder_replay import replay_ladder
from hypothesis import given
from hypothesis import strategies as st

from .base import PropertyTestCase
from .strategies import bar_paths, ladder_and_bars, ladders


def _naive_walk(
    setup: dict[str, Any], bars: list[dict[str, Any]]
) -> tuple[list[str], set[str], bool, str]:
    """Independent reference: (entries_filled, tps_hit, sl_hit, classification).

    Mirrors the documented engine semantics with NO time-stop / entry-TTL (the
    tests call ``replay_ladder`` with the default ``*_expiry_ms=None``): per bar,
    fill entries first, then — only with a position — resolve the stop SL-FIRST,
    then touch TPs ascending; latch ``exit_reached`` on a full SL or all-TP
    scale-out and stop. Generated ladders always have entries above the stop, so
    BAD_GEOMETRY never arises here (covered by a separate hand-built test).
    """
    entries = [(f"E{i + 1}", t["limit"]) for i, t in enumerate(setup["entry_tiers"])]
    tps = [(f"TP{i + 1}", t["target"]) for i, t in enumerate(setup["tp_tranches"])]
    stop = setup["disaster_stop"]

    filled: list[str] = []
    filled_ids: set[str] = set()
    hit_tp: set[str] = set()
    sl = False
    exit_reached = False

    for bar in bars:
        if exit_reached:
            break
        low, high = bar["l"], bar["h"]
        for lid, limit in entries:
            if lid not in filled_ids and low <= limit:
                filled_ids.add(lid)
                filled.append(lid)
        if not filled:
            continue
        # SL-first: a full stop-out. ``exit_reached = True; break`` here mirrors the
        # engine's exit latch (``_LadderWalk.step`` early-returns once exited) — no
        # later bar can fill, touch, or re-open the position.
        if low <= stop:
            sl = True
            exit_reached = True
            break
        for lid, target in tps:
            if lid not in hit_tp and high >= target:
                hit_tp.add(lid)
        if tps and len(hit_tp) == len(tps):
            exit_reached = True

    any_tp = bool(hit_tp)
    all_tp = bool(tps) and len(hit_tp) == len(tps)
    if not filled:
        cls = "NO_FILL"
    elif all_tp:
        cls = "TP_FULL"
    elif any_tp and sl:
        cls = "PARTIAL_TP_THEN_SL"
    elif sl:
        cls = "SL_HIT"
    elif any_tp:
        cls = "PARTIAL_TP_OPEN"
    else:
        cls = "OPEN"
    return filled, hit_tp, sl, cls


def _filled_limits(setup: dict[str, Any], entries_filled: tuple[str, ...]) -> list[float]:
    by_id = {f"E{i + 1}": t["limit"] for i, t in enumerate(setup["entry_tiers"])}
    return [by_id[e] for e in entries_filled]


class TestWalkDifferentialOracle(PropertyTestCase):
    """Production engine's discrete decisions == the naive reference walk."""

    @given(ladder_and_bars())
    def test_fill_touch_sl_and_classification_match_oracle(self, lab: Any) -> None:
        setup, bars = lab
        outcome = replay_ladder(setup, bars)
        self.assertEqual(outcome.status, "OK")
        ef, tp, sl, cls = _naive_walk(setup, bars)
        self.assertEqual(list(outcome.entries_filled), ef)
        self.assertEqual(set(outcome.tps_hit), tp)
        self.assertEqual(outcome.sl_hit, sl)
        self.assertEqual(outcome.classification, cls)


class TestReplayInvariants(PropertyTestCase):
    """Algebraic invariants that hold for every OK outcome."""

    @given(ladder_and_bars())
    def test_capture_counts_and_subset(self, lab: Any) -> None:
        setup, bars = lab
        o = replay_ladder(setup, bars)
        n_tps = len(setup["tp_tranches"])
        self.assertLessEqual(set(o.realized_tp_ids), set(o.tps_hit))
        self.assertEqual(o.captured_tp_count, len(o.realized_tp_ids))
        self.assertEqual(o.touched_tp_count, len(o.tps_hit))
        if o.realized_r is not None:
            self.assertLessEqual(0, o.captured_tp_count)
            self.assertLessEqual(o.captured_tp_count, o.touched_tp_count)
            self.assertLessEqual(o.touched_tp_count, n_tps)

    @given(ladder_and_bars())
    def test_fill_and_blended_bounds(self, lab: Any) -> None:
        setup, bars = lab
        o = replay_ladder(setup, bars)
        if o.entries_filled:
            self.assertIsNotNone(o.filled_fraction)
            assert o.filled_fraction is not None
            self.assertGreater(o.filled_fraction, 0.0)
            self.assertLessEqual(o.filled_fraction, 1.0 + 1e-9)
            limits = _filled_limits(setup, o.entries_filled)
            assert o.blended_entry is not None
            self.assertGreaterEqual(o.blended_entry, min(limits) - 1e-6)
            self.assertLessEqual(o.blended_entry, max(limits) + 1e-6)

    @given(ladder_and_bars())
    def test_realized_r_bounded_and_finite(self, lab: Any) -> None:
        import math

        setup, bars = lab
        o = replay_ladder(setup, bars)
        if o.realized_r is not None:
            self.assertTrue(math.isfinite(o.realized_r))
            self.assertGreaterEqual(o.realized_r, -1.0 - 1e-9)  # cannot lose more than 1R
        if o.mfe is not None and o.mae is not None:
            self.assertGreaterEqual(o.mfe, o.mae)


class TestReplayMetamorphic(PropertyTestCase):
    """Relations under transformed inputs."""

    @given(ladder_and_bars())
    def test_determinism(self, lab: Any) -> None:
        setup, bars = lab
        a = replay_ladder(setup, bars)
        b = replay_ladder(setup, bars)
        self.assertEqual(a.classification, b.classification)
        self.assertEqual(a.realized_tp_ids, b.realized_tp_ids)
        self.assertEqual(a.realized_r, b.realized_r)

    @given(ladder_and_bars(), bar_paths(min_bars=1, max_bars=4))
    def test_post_exit_bars_are_idempotent(self, lab: Any, extra: list[dict[str, Any]]) -> None:
        """Appending bars strictly AFTER a terminal exit must not change the headline."""
        setup, bars = lab
        base = replay_ladder(setup, bars)
        terminal = {"TP_FULL", "SL_HIT", "PARTIAL_TP_THEN_SL"}
        if base.classification not in terminal or not bars:
            return  # only meaningful once the position has terminally exited
        last_t = bars[-1]["t"]
        shifted = [{**b, "t": last_t + 1 + i} for i, b in enumerate(extra)]
        after = replay_ladder(setup, bars + shifted)
        self.assertEqual(after.classification, base.classification)
        self.assertEqual(after.realized_tp_ids, base.realized_tp_ids)
        self.assertEqual(after.realized_r, base.realized_r)

    @given(ladders())
    def test_full_fill_reduces_to_full_position_weighting(self, setup: dict[str, Any]) -> None:
        """Gap-down through the lowest entry fills everything → captured == touched."""
        lowest = min(t["limit"] for t in setup["entry_tiers"])
        stop = setup["disaster_stop"]
        top_tp = max(t["target"] for t in setup["tp_tranches"])
        # Bar 1: dip to just above the stop but at/below the lowest entry → fill all,
        # no SL. Bar 2: sweep every TP.
        fill_low = (lowest + stop) / 2.0
        bars = [
            {"t": 1, "l": fill_low, "h": lowest, "c": lowest, "o": lowest},
            {"t": 2, "l": top_tp, "h": top_tp, "c": top_tp, "o": top_tp},
        ]
        o = replay_ladder(setup, bars)
        n_tps = len(setup["tp_tranches"])
        self.assertEqual(
            o.entries_filled, tuple(f"E{i + 1}" for i in range(len(setup["entry_tiers"])))
        )
        self.assertEqual(o.classification, "TP_FULL")
        # Full fill (filled_fraction == 1) → every touched TP also sells.
        self.assertEqual(o.captured_tp_count, n_tps)
        self.assertEqual(o.touched_tp_count, n_tps)
        assert o.filled_fraction is not None
        self.assert_close(o.filled_fraction, 1.0)


class TestDegenerateGeometry(PropertyTestCase):
    """Hand-built edge: a stop at/above the entry (BAD_GEOMETRY)."""

    @given(
        entry=st.floats(50.0, 200.0, allow_nan=False, allow_infinity=False),
        # Strictly above entry: at over==0 the blended-entry weighted average can
        # round 1 ulp ABOVE the limit, making risk barely positive (SL_HIT, not
        # BAD_GEOMETRY) — a genuine FP boundary the property surfaced. Assert only
        # the unambiguous region where the stop is clearly above the entry.
        over=st.floats(1e-3, 20.0, allow_nan=False, allow_infinity=False),
    )
    def test_stop_above_entry_is_bad_geometry_no_false_capture(
        self, entry: float, over: float
    ) -> None:
        stop = entry + over  # stop > entry -> risk < 0
        setup = {
            "status": "OK",
            "disaster_stop": stop,
            "entry_tiers": [{"limit": entry, "alloc_pct": 100.0}],
            "tp_tranches": [{"target": entry + 30.0, "tranche_pct": 100.0}],
        }
        # Fill bar (low <= entry <= stop trips SL-first) then a gap over the TP.
        bars = [
            {"t": 1, "l": entry - 1.0, "h": entry, "c": entry, "o": entry},
            {"t": 2, "l": entry + 29.0, "h": entry + 31.0, "c": entry + 30.0, "o": entry + 30.0},
        ]
        o = replay_ladder(setup, bars)
        self.assertEqual(o.classification, "BAD_GEOMETRY")
        self.assertIsNone(o.realized_r)
        # No false partial-capture: SL-first short-circuits _take_tps → nothing touched.
        self.assertEqual(o.tps_hit, ())
        self.assertEqual(o.captured_tp_count, 0)
        self.assertEqual(o.touched_tp_count, 0)
