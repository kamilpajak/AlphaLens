"""Unit tests for the beta-provenance tally printed by ``scripts/diagnose_selection.py``.

An event whose windows have not elapsed gets no beta at all (``None``), which is
NOT the same as an event whose estimate was attempted and fell back to 1.0. The
printed summary used to conflate the two.
"""

from __future__ import annotations

import importlib
import unittest

import pandas as pd
from alphalens_research.diagnostics import fixed_horizon as fh


def _import_script():
    return importlib.import_module("scripts.diagnose_selection")


class TestBetaCounts(unittest.TestCase):
    def test_skipped_events_are_not_counted_as_fallbacks(self):
        mod = _import_script()
        sources = pd.Series(
            [fh.BETA_ESTIMATED, fh.BETA_ESTIMATED, fh.BETA_FALLBACK_THIN, None, None]
        )

        counts = mod._beta_counts(sources)

        self.assertEqual(counts.estimated, 2)
        self.assertEqual(counts.fell_back, 1)
        self.assertEqual(counts.not_attempted, 2)

    def test_both_fallback_tags_count_as_fallbacks(self):
        mod = _import_script()
        sources = pd.Series([fh.BETA_FALLBACK_THIN, fh.BETA_FALLBACK_DEGENERATE])

        counts = mod._beta_counts(sources)

        self.assertEqual(counts.fell_back, 2)
        self.assertEqual(counts.estimated, 0)
        self.assertEqual(counts.not_attempted, 0)

    def test_the_buckets_partition_every_row(self):
        mod = _import_script()
        sources = pd.Series(
            [fh.BETA_ESTIMATED, fh.BETA_FALLBACK_DEGENERATE, None, fh.BETA_ESTIMATED]
        )

        counts = mod._beta_counts(sources)

        self.assertEqual(sum(counts), len(sources))
        self.assertEqual(counts.unexpected, 0)

    def test_a_tag_this_helper_does_not_know_lands_in_unexpected(self):
        # A future beta_source value must not be absorbed into fell_back, which is the
        # same conflation this helper exists to remove.
        mod = _import_script()
        sources = pd.Series([fh.BETA_ESTIMATED, "some_future_tag"])

        counts = mod._beta_counts(sources)

        self.assertEqual(counts.unexpected, 1)
        self.assertEqual(counts.fell_back, 0)
        self.assertEqual(sum(counts), len(sources))

    def test_an_all_none_column_is_all_not_attempted(self):
        mod = _import_script()

        counts = mod._beta_counts(pd.Series([None, None, None]))

        self.assertEqual(counts.not_attempted, 3)
        self.assertEqual(counts.estimated, 0)
        self.assertEqual(counts.fell_back, 0)


if __name__ == "__main__":
    unittest.main()
