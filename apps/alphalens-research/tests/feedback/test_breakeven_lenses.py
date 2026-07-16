"""Tests for the exit-stop WHAT-IF lens registry.

Each lens recomputes realized R under an alternate EXIT-STOP policy over the SAME
picks + price paths: a break-even / trailing stop (PR #722) or a fill-anchored stop
(exit-geometry path b). The registry is data-driven so a new lens is one entry, and
the grid is a `{lens_id: realized_r}` map stamped display-only.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from alphalens_pipeline.feedback.breakeven_lenses import (
    BREAKEVEN_LENSES,
    BreakevenLens,
    _lens_realized_r,
    breakeven_grid,
)
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

    def test_spa_registry_mirrors_pipeline_lenses(self):
        """Cross-language parity guard: every pipeline lens must appear in the SPA
        mirror (WHATIF_LENS_REGISTRY in edgeWhatif.ts) with a matching label +
        status. The slim Django image serves only lens_id, so labels/status live
        SPA-side; sync is otherwise convention-only, and a pipeline lens forgotten
        in the mirror ships with a raw-id label. This binds the two so a new lens
        cannot silently diverge."""
        repo_root = Path(__file__).resolve().parents[4]
        src = (repo_root / "apps/web/src/lib/edgeWhatif.ts").read_text(encoding="utf-8")
        for lens in BREAKEVEN_LENSES:
            # Flat object literals (no nested braces) -> capture id: { ... } up to
            # the first closing brace; matches both single- and multi-line entries.
            m = re.search(re.escape(lens.lens_id) + r"\s*:\s*\{(.*?)\}", src, re.DOTALL)
            assert m is not None, f"{lens.lens_id} missing from SPA WHATIF_LENS_REGISTRY"
            block = m.group(1)
            self.assertIn(f"label: '{lens.label}'", block, f"{lens.lens_id} label drift in mirror")
            self.assertIn(
                f"status: '{lens.status}'", block, f"{lens.lens_id} status drift in mirror"
            )

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

    def test_trail_lens_registered_with_exact_params_and_preregistered_ref(self):
        # The pre-registered trailing variant (exit-geometry memo §7 row
        # "be@0.5R + trail0.6") — same trigger as be_0p5r plus a 0.6-fraction
        # trailing lock-in, carrying its provenance ref on the lens record.
        by_id = {lens.lens_id: lens for lens in BREAKEVEN_LENSES}
        self.assertIn("be_0p5r_trail0p6", by_id)
        lens = by_id["be_0p5r_trail0p6"]
        self.assertEqual(lens.kind, "breakeven")
        self.assertEqual(lens.mfe_trigger_r, 0.5)
        self.assertEqual(lens.trail_frac, 0.6)
        self.assertEqual(lens.status, "in_sample")
        self.assertEqual(lens.category, "exit-stop")
        self.assertEqual(lens.preregistered_ref, "exit_geometry_2026_06_30 s7 be0.5/trail0.6")

    def test_existing_lenses_carry_no_preregistered_ref(self):
        # Only the trailing variant was pre-registered; the two original lenses
        # were tuned in-sample and must not claim a pre-registration.
        by_id = {lens.lens_id: lens for lens in BREAKEVEN_LENSES}
        self.assertIsNone(by_id["be_0p5r"].preregistered_ref)
        self.assertIsNone(by_id["fill_anchored_0p5atr"].preregistered_ref)

    def test_trail_lens_exits_at_trailed_stop_while_plain_be_exits_flat(self):
        # Shared path (fill 100, peak +0.6R at 106, pullback, crash): the plain
        # break-even lens exits the remainder at the blended entry (0.00R) while
        # the trailing lens locks in 0.6 of the peak gain — eff stop
        # 100 + 0.6*(106-100) = 103.6, pierced by the pullback low 99 -> +0.36R.
        grid = breakeven_grid(_SETUP, _BARS)
        self.assertAlmostEqual(grid["be_0p5r"], 0.0, places=6)
        self.assertAlmostEqual(grid["be_0p5r_trail0p6"], 0.36, places=6)

    def test_trail_lens_grid_matches_direct_replay(self):
        grid = breakeven_grid(_SETUP, _BARS)
        expected = replay_ladder_breakeven(_SETUP, _BARS, mfe_trigger_r=0.5, trail_frac=0.6)
        self.assertEqual(grid["be_0p5r_trail0p6"], expected)

    def test_django_summary_mirrors_preregistered_refs(self):
        """Cross-app parity guard: the slim Django image cannot import this
        registry, so ``edge/api/summary.py`` carries a mirror map
        ``_LENS_PREREGISTERED_REF``. Every pipeline lens with a non-None
        ``preregistered_ref`` must appear there verbatim, so the served payload
        cannot silently drift from the lens record."""
        repo_root = Path(__file__).resolve().parents[4]
        src = (repo_root / "apps/alphalens-django/edge/api/summary.py").read_text(encoding="utf-8")
        refs = [lens for lens in BREAKEVEN_LENSES if lens.preregistered_ref is not None]
        self.assertTrue(refs, "positive control: at least one lens carries a preregistered_ref")
        for lens in refs:
            entry = f'"{lens.lens_id}": "{lens.preregistered_ref}"'
            self.assertIn(entry, src, f"{lens.lens_id} missing/drifted in _LENS_PREREGISTERED_REF")

    def test_fill_anchored_lens_registered_and_dispatched(self):
        by_id = {lens.lens_id: lens for lens in BREAKEVEN_LENSES}
        self.assertIn("fill_anchored_0p5atr", by_id)
        self.assertEqual(by_id["fill_anchored_0p5atr"].kind, "fill_anchored")
        # Dispatched to realized_r_fill_anchored: single-tier E1=100 @ stop 100-1=99
        # (ATR=2), winner to TP=110 -> (110-100)/1 = +10.0R.
        setup = _setup(entries=[(100.0, 100.0)], tps=[(110.0, 100.0)], stop=90.0, atr=2.0)
        bars = [_bar(1, 99.5, 101.0, 100.0), _bar(2, 105.0, 111.0, 110.0)]
        self.assertAlmostEqual(breakeven_grid(setup, bars)["fill_anchored_0p5atr"], 10.0, places=6)

    def test_unknown_lens_kind_raises(self):
        # A registered lens with an unrecognised kind is a config error that must
        # fail loudly at dispatch, not silently fall through to the break-even path.
        bogus = BreakevenLens(
            lens_id="bogus", label="bogus", category="exit-stop", status="in_sample", kind="bogus"
        )
        with self.assertRaises(ValueError):
            _lens_realized_r(bogus, _SETUP, _BARS)

    def test_grid_none_when_no_fill(self):
        grid = breakeven_grid(_SETUP, [_bar(1, 101.0, 105.0, 103.0)])  # never touches 100
        self.assertTrue(grid)  # still keyed by every lens
        self.assertTrue(all(v is None for v in grid.values()))


if __name__ == "__main__":
    unittest.main()
