"""Tests for the exit-stop WHAT-IF lens registry.

Each lens recomputes realized R under an alternate EXIT-STOP policy over the SAME
picks + price paths: a break-even / trailing stop (PR #722) or a fill-anchored stop
(exit-geometry path b). The registry is data-driven so a new lens is one entry, and
the grid is a `{lens_id: realized_r}` map stamped display-only.
"""

from __future__ import annotations

import unittest

from alphalens_pipeline.feedback.breakeven_lenses import BREAKEVEN_LENSES, breakeven_grid
from alphalens_pipeline.feedback.ladder_replay import (
    realized_r_fill_anchored,
    replay_ladder_breakeven,
)


def _bar(t: int, low: float, high: float, close: float) -> dict:
    return {"t": t, "l": low, "h": high, "c": close}


def _setup(
    *,
    entries: list[tuple[float, float]],
    tps: list[tuple[float, float]],
    stop: float,
    atr: float | None = None,
) -> dict:
    setup: dict = {
        "status": "OK",
        "disaster_stop": stop,
        "entry_tiers": [{"limit": p, "alloc_pct": w} for p, w in entries],
        "tp_tranches": [{"target": p, "tranche_pct": w} for p, w in tps],
    }
    if atr is not None:
        setup["atr"] = atr
    return setup


# Fill E1(100), peak +0.6R (high 106), dip back, then crash to the stop (90).
# ATR=10 so the fill-anchored lens (stop = 100 - 0.5*10 = 95) also resolves.
_SETUP = _setup(entries=[(100.0, 100.0)], tps=[(200.0, 100.0)], stop=90.0, atr=10.0)
_BARS = [
    _bar(1, 99.0, 101.0, 100.0),
    _bar(2, 103.0, 106.0, 105.0),
    _bar(3, 99.0, 102.0, 100.0),
    _bar(4, 89.0, 95.0, 90.0),
]


class TestBreakevenRegistry(unittest.TestCase):
    def test_registry_nonempty_unique_ids_valid_status(self):
        self.assertTrue(BREAKEVEN_LENSES)
        ids = [lens.lens_id for lens in BREAKEVEN_LENSES]
        self.assertEqual(len(ids), len(set(ids)), "lens_ids must be unique")
        for lens in BREAKEVEN_LENSES:
            self.assertIn(lens.status, {"in_sample", "validated"})
            self.assertTrue(lens.label)

    def test_grid_keyed_by_lens_id_matches_replay(self):
        grid = breakeven_grid(_SETUP, _BARS)
        self.assertEqual(set(grid), {lens.lens_id for lens in BREAKEVEN_LENSES})
        for lens in BREAKEVEN_LENSES:
            if lens.kind == "fill_anchored":
                expected = realized_r_fill_anchored(
                    _SETUP, _BARS, stop_atr_mult=lens.stop_atr_mult or 0.5
                )
            else:
                expected = replay_ladder_breakeven(
                    _SETUP, _BARS, mfe_trigger_r=lens.mfe_trigger_r, trail_frac=lens.trail_frac
                )
            self.assertEqual(grid[lens.lens_id], expected)

    def test_fill_anchored_lens_registered_and_dispatched(self):
        by_id = {lens.lens_id: lens for lens in BREAKEVEN_LENSES}
        self.assertIn("fill_anchored_0p5atr", by_id)
        self.assertEqual(by_id["fill_anchored_0p5atr"].kind, "fill_anchored")
        # Dispatched to realized_r_fill_anchored: single-tier E1=100 @ stop 100-1=99
        # (ATR=2), winner to TP=110 -> (110-100)/1 = +10.0R.
        setup = _setup(entries=[(100.0, 100.0)], tps=[(110.0, 100.0)], stop=90.0, atr=2.0)
        bars = [_bar(1, 99.5, 101.0, 100.0), _bar(2, 105.0, 111.0, 110.0)]
        self.assertAlmostEqual(breakeven_grid(setup, bars)["fill_anchored_0p5atr"], 10.0, places=6)

    def test_grid_none_when_no_fill(self):
        grid = breakeven_grid(_SETUP, [_bar(1, 101.0, 105.0, 103.0)])  # never touches 100
        self.assertTrue(grid)  # still keyed by every lens
        self.assertTrue(all(v is None for v in grid.values()))


if __name__ == "__main__":
    unittest.main()
