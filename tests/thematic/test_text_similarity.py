"""Unit tests for alphalens.thematic.text_similarity.

Two-tier clustering refactor uses these helpers at both ingest (Tier 1, lexical
title comparison) and resolver (Tier 2, entity-set comparison). All math here
is pure stdlib — no sklearn, no nltk — so the assertions are exact.
"""

import unittest

from alphalens.thematic import text_similarity as ts


class TestNormalizeTitle(unittest.TestCase):
    def test_lowercases_and_strips_punctuation(self):
        self.assertEqual(
            ts.normalize_title("SpaceX's IPO Filing Lands!"),
            frozenset({"spacexs", "filing", "lands"}),
        )

    def test_drops_tokens_three_chars_or_shorter(self):
        # "AI", "is", "on", "the" all drop; "rise" survives
        self.assertEqual(
            ts.normalize_title("AI is on the rise"),
            frozenset({"rise"}),
        )

    def test_drops_inline_stopwords_longer_than_three_chars(self):
        # "says", "with", "from", "report", "after" should be dropped
        out = ts.normalize_title("Reuters says report shows after market shock")
        self.assertNotIn("says", out)
        self.assertNotIn("report", out)
        self.assertNotIn("after", out)
        self.assertIn("reuters", out)
        self.assertIn("shows", out)
        self.assertIn("market", out)
        self.assertIn("shock", out)

    def test_empty_returns_empty(self):
        self.assertEqual(ts.normalize_title(""), frozenset())

    def test_collapses_whitespace_and_unicode_punctuation(self):
        # em-dash, curly quotes, multiple spaces
        out = ts.normalize_title("Apple—Tesla’s   deal closes")
        self.assertEqual(out, frozenset({"apple", "teslas", "deal", "closes"}))


class TestTitlesSimilar(unittest.TestCase):
    def test_true_cluster_high_overlap(self):
        # 5 content tokens each, 4 overlap → jaccard 4/6 = 0.66, overlap=4
        a = "SpaceX IPO filing approval lands soon"
        b = "SpaceX IPO filing approval lands tomorrow"
        self.assertTrue(ts.titles_similar(a, b))

    def test_false_positive_short_headline_min_overlap_guard(self):
        # 2-token overlap {apple, earnings} fails MIN_TOKEN_OVERLAP=3
        a = "Apple Q3 earnings beat"
        b = "Apple Q4 earnings preview"
        self.assertFalse(ts.titles_similar(a, b))

    def test_different_stories_share_only_two_tokens(self):
        # Overlap {announces, cloud} = 2 tokens; below threshold
        a = "Microsoft announces brand new cloud product launch"
        b = "Google announces unrelated cloud rival service price"
        self.assertFalse(ts.titles_similar(a, b))

    def test_stopword_only_overlap_rejected(self):
        # After stopword + ≤3 strip, real content tokens differ entirely
        a = "Markets close higher today"
        b = "Some markets pause today"
        self.assertFalse(ts.titles_similar(a, b))

    def test_empty_inputs_return_false(self):
        self.assertFalse(ts.titles_similar("", ""))
        self.assertFalse(ts.titles_similar("Something happens here", ""))
        self.assertFalse(ts.titles_similar("", "Something happens here"))

    def test_threshold_and_min_overlap_are_kwargs(self):
        # Lower bar to threshold=0.4, min_overlap=2 → previously-failing pair passes
        a = "Apple Q3 earnings beat"
        b = "Apple Q4 earnings preview"
        self.assertTrue(ts.titles_similar(a, b, threshold=0.4, min_overlap=2))

    def test_identical_titles_cluster(self):
        a = "Federal Reserve raises interest rates again"
        b = "Federal Reserve raises interest rates again"
        self.assertTrue(ts.titles_similar(a, b))

    def test_subset_passes_when_meets_both_bars(self):
        # b is a subset-superset variant: 4 content tokens, 4 overlap, 1 extra
        # jaccard = 4/5 = 0.8, overlap=4 → both bars met
        a = "SpaceX rocket launch successful flight"
        b = "SpaceX rocket launch successful flight breaks records"
        self.assertTrue(ts.titles_similar(a, b))


class TestEntityJaccard(unittest.TestCase):
    def test_empty_inputs_return_zero(self):
        self.assertEqual(ts.entity_jaccard(set(), set()), 0.0)
        self.assertEqual(ts.entity_jaccard({"A"}, set()), 0.0)
        self.assertEqual(ts.entity_jaccard(set(), {"A"}), 0.0)

    def test_identical_sets_return_one(self):
        self.assertEqual(ts.entity_jaccard({"A", "B"}, {"A", "B"}), 1.0)

    def test_partial_overlap(self):
        # |∩|=2, |∪|=4 → 0.5
        self.assertAlmostEqual(
            ts.entity_jaccard({"A", "B", "C"}, {"B", "C", "D"}),
            0.5,
            places=4,
        )

    def test_no_overlap(self):
        self.assertEqual(
            ts.entity_jaccard({"A", "B"}, {"C", "D", "E", "F"}),
            0.0,
        )

    def test_subset(self):
        # |∩|=2, |∪|=5 → 0.4
        self.assertAlmostEqual(
            ts.entity_jaccard({"A", "B"}, {"A", "B", "C", "D", "E"}),
            0.4,
            places=4,
        )

    def test_sparse_match_below_default_threshold(self):
        # Tier 2 default ENTITY_JACCARD_THRESHOLD = 0.3
        # {SpaceX, Musk} vs {SpaceX, NASA, Boeing, Lockheed} → 1/5 = 0.2
        score = ts.entity_jaccard({"SpaceX", "Musk"}, {"SpaceX", "NASA", "Boeing", "Lockheed"})
        self.assertLess(score, ts.ENTITY_JACCARD_THRESHOLD)


class TestModuleConstants(unittest.TestCase):
    """Lock the tunable defaults so future drift requires a deliberate test edit."""

    def test_defaults(self):
        self.assertEqual(ts.JACCARD_THRESHOLD, 0.6)
        self.assertEqual(ts.MIN_TOKEN_OVERLAP, 3)
        self.assertEqual(ts.ENTITY_JACCARD_THRESHOLD, 0.3)


if __name__ == "__main__":
    unittest.main()
