"""Shadow-arm sampling, per ``docs/research/theme_shadow_arm_contract_2026_08_23.md``.

Every test names the clause it pins. The contract was committed before this
file existed, so none of these thresholds were chosen with a result in view.
"""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from alphalens_pipeline.thematic.mapping.shadow_sampler import (
    MIN_COUNT_RECENT,
    SHADOW_THEMES_PER_BAND,
    ShadowDraw,
    already_collected,
    build_shadow_frame,
    sample_shadow_themes,
    shadow_store_path,
)

_ASOF = dt.date(2026, 8, 21)


def _rollup(n: int = 120) -> pd.DataFrame:
    """A ranked pool: novelty falls with rank, every theme is eligible."""
    return pd.DataFrame(
        {
            "theme": [f"theme_{i:03d}" for i in range(n)],
            "novelty_score": [20.0 - i * 0.1 for i in range(n)],
            "count_recent": [50 - (i % 40) for i in range(n)],
            "count_window": [100 - i for i in range(n)],
        }
    )


class TestEligibility(unittest.TestCase):
    def test_below_the_novelty_threshold_is_never_drawn(self):
        pool = _rollup()
        pool.loc[pool.index >= 10, "novelty_score"] = 1.0

        draw = sample_shadow_themes(pool, asof=_ASOF, selected=set())

        self.assertEqual(draw.near, [])
        self.assertEqual(draw.far, [])

    def test_thin_themes_are_excluded_by_count_recent(self):
        """Contract §3: the unrestricted pool is mostly single-article noise the
        selector would never reach. Comparing against it answers a different
        question."""
        pool = _rollup()
        pool["count_recent"] = MIN_COUNT_RECENT - 1

        draw = sample_shadow_themes(pool, asof=_ASOF, selected=set())

        self.assertEqual(draw.all_themes, [])

    def test_a_theme_exactly_at_the_floor_is_eligible(self):
        pool = _rollup(n=60)
        pool["count_recent"] = MIN_COUNT_RECENT

        draw = sample_shadow_themes(pool, asof=_ASOF, selected=set())

        self.assertEqual(len(draw.all_themes), 2 * SHADOW_THEMES_PER_BAND)


class TestBands(unittest.TestCase):
    def test_near_comes_from_ranks_11_to_30_and_far_from_below(self):
        pool = _rollup()

        draw = sample_shadow_themes(pool, asof=_ASOF, selected=set())

        order = list(pool["theme"])
        near_ranks = sorted(order.index(t) for t in draw.near)
        far_ranks = sorted(order.index(t) for t in draw.far)
        self.assertTrue(all(10 <= r < 30 for r in near_ranks), near_ranks)
        self.assertTrue(all(r >= 30 for r in far_ranks), far_ranks)

    def test_bands_do_not_overlap(self):
        draw = sample_shadow_themes(_rollup(), asof=_ASOF, selected=set())

        self.assertEqual(set(draw.near) & set(draw.far), set())

    def test_a_short_pool_yields_what_it_has_rather_than_raising(self):
        pool = _rollup(n=35)

        draw = sample_shadow_themes(pool, asof=_ASOF, selected=set())

        self.assertEqual(len(draw.near), SHADOW_THEMES_PER_BAND)
        self.assertEqual(len(draw.far), 5)


class TestNeverShadowsASelectedTheme(unittest.TestCase):
    def test_selected_themes_are_excluded_even_when_they_rank_low(self):
        """The arms must be disjoint or the comparison is against itself."""
        pool = _rollup()
        selected = {f"theme_{i:03d}" for i in range(10)} | {"theme_015", "theme_040"}

        draw = sample_shadow_themes(pool, asof=_ASOF, selected=selected)

        self.assertEqual(set(draw.all_themes) & selected, set())


class TestDeterminism(unittest.TestCase):
    def test_the_same_day_draws_the_same_themes(self):
        pool = _rollup()

        a = sample_shadow_themes(pool, asof=_ASOF, selected=set())
        b = sample_shadow_themes(pool, asof=_ASOF, selected=set())

        self.assertEqual(a.all_themes, b.all_themes)

    def test_a_different_day_draws_differently(self):
        """A seed that ignored the date would pass the determinism test."""
        pool = _rollup()

        a = sample_shadow_themes(pool, asof=_ASOF, selected=set())
        b = sample_shadow_themes(pool, asof=_ASOF + dt.timedelta(days=1), selected=set())

        self.assertNotEqual(a.all_themes, b.all_themes)

    def test_the_seed_does_not_depend_on_process_hashing(self):
        """PYTHONHASHSEED salts the builtin hash(); the draw must not use it."""
        draw = sample_shadow_themes(_rollup(), asof=_ASOF, selected=set())

        self.assertEqual(draw.seed, ShadowDraw.seed_for(_ASOF))


class TestStorePathIsOutsideTheCandidateTree(unittest.TestCase):
    """Contract §4: the shadow arm never reaches a card. Enforced, not intended."""

    def test_the_store_is_not_under_thematic_candidates(self):
        path = shadow_store_path(_ASOF)

        self.assertNotIn("thematic_candidates", str(path))
        self.assertTrue(str(path).endswith("2026-08-21.parquet"))

    def test_the_store_is_not_the_briefs_directory_either(self):
        path = shadow_store_path(_ASOF)

        self.assertNotIn("thematic_briefs", str(path))


class TestShadowFrame(unittest.TestCase):
    """The funnel the mapper produced, stamped with what the read needs."""

    def _draw(self) -> ShadowDraw:
        return ShadowDraw(
            asof=_ASOF,
            near=["alpha", "beta"],
            far=["gamma"],
            seed="deadbeef",
            eligible_pool=226,
        )

    def _funnel(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "theme": ["alpha", "alpha", "gamma"],
                "ticker": ["AAA", "BBB", "CCC"],
                "bracket_verdict": ["in_bracket", "too_big", "in_bracket"],
            }
        )

    def test_every_row_carries_its_band(self):
        """Asserted per theme, not positionally: the frame also gains a row for
        each drawn theme that produced nothing, so a positional expectation
        would encode the row ORDER rather than the banding."""
        out = build_shadow_frame(self._funnel(), self._draw())

        self.assertEqual(
            out.groupby("theme")["shadow_band"].first().to_dict(),
            {"alpha": "near", "beta": "near", "gamma": "far"},
        )
        self.assertTrue(out["shadow_band"].notna().all())

    def test_a_theme_that_produced_nothing_still_appears_with_no_ticker(self):
        """Contract §6 counts themes that yielded no proposal at all; dropping
        them would silently inflate the per-theme-day yield of both arms."""
        out = build_shadow_frame(self._funnel(), self._draw())

        beta = out[out["theme"] == "beta"]
        self.assertEqual(len(beta), 1)
        self.assertTrue(pd.isna(beta["ticker"].iloc[0]))
        self.assertEqual(beta["shadow_band"].iloc[0], "near")

    def test_the_draw_metadata_travels_with_the_rows(self):
        out = build_shadow_frame(self._funnel(), self._draw())

        self.assertEqual(set(out["shadow_seed"]), {"deadbeef"})
        self.assertEqual(set(out["asof"]), {_ASOF})
        self.assertEqual(set(out["eligible_pool"]), {226})

    def test_a_row_for_a_theme_outside_the_draw_is_refused(self):
        """A funnel carrying a theme the draw never asked for means the mapper
        was called with the wrong list — a silent arm contamination."""
        rogue = self._funnel()
        rogue.loc[len(rogue)] = ["not_drawn", "ZZZ", "in_bracket"]

        with self.assertRaises(ValueError):
            build_shadow_frame(rogue, self._draw())


if __name__ == "__main__":
    unittest.main()


class TestIdempotencePerDate(unittest.TestCase):
    """The daily pipeline fires SIX times on the same asof.

    Without a per-date skip the collector would redraw on every slot: 120
    themes a day instead of 20, six times the spend, and a sample whose size
    depends on how many slots happened to succeed.
    """

    def test_an_existing_day_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            shadow_store_path(_ASOF, store_dir=store).write_bytes(b"x")

            self.assertTrue(already_collected(_ASOF, store_dir=store))

    def test_a_fresh_day_is_not_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(already_collected(_ASOF, store_dir=Path(tmp)))

    def test_a_missing_store_directory_is_not_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(already_collected(_ASOF, store_dir=Path(tmp) / "absent"))

    def test_the_raw_subdirectory_is_covered_by_the_same_guarantee(self):
        """The mapper writes its own candidate parquet and funnel under
        ``<store>/raw/``. That inherits the store's location, so the §4
        guarantee covers it — asserted rather than argued."""
        raw = shadow_store_path(_ASOF).parent / "raw"

        self.assertNotIn("thematic_candidates", str(raw))
        self.assertNotIn("thematic_briefs", str(raw))


class TestBandsUseOriginalRanks(unittest.TestCase):
    """The review claimed bands are cut AFTER removing the selected themes, so a
    theme originally at rank 11 would slide into rank 10 and change band. It is
    not what the code does — ranks come from the full eligible frame — but
    nothing pinned it, so a future edit could make the claim true.
    """

    def test_removing_low_ranked_selections_does_not_shift_the_bands(self):
        pool = _rollup()
        order = list(pool["theme"])
        # The selector took the top 10 AND two stragglers from deep in the pool.
        selected = set(order[:10]) | {order[12], order[45]}

        draw = sample_shadow_themes(pool, asof=_ASOF, selected=selected)

        near_ranks = sorted(order.index(t) for t in draw.near)
        far_ranks = sorted(order.index(t) for t in draw.far)
        self.assertTrue(all(10 <= r < 30 for r in near_ranks), near_ranks)
        self.assertTrue(all(r >= 30 for r in far_ranks), far_ranks)

    def test_a_selector_that_took_fewer_than_ten_does_not_shift_them_either(self):
        pool = _rollup()
        order = list(pool["theme"])

        draw = sample_shadow_themes(pool, asof=_ASOF, selected=set(order[:4]))

        near_ranks = sorted(order.index(t) for t in draw.near)
        self.assertTrue(all(10 <= r < 30 for r in near_ranks), near_ranks)
