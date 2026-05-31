"""Each of the 6 dnia-jeden predicates exercised in isolation.

Predicate boundaries are deterministic and pure: no LLM, no network. The
purpose of the test set is to lock semantics so a "fix" of one predicate
that breaks another is caught at TDD time.
"""

from __future__ import annotations

import datetime as dt
import unittest

from alphalens_pipeline.thematic.extraction.templates.predicates import (
    PREDICATE_REGISTRY,
    PredicateContext,
    available_predicates,
    evaluate,
)
from alphalens_pipeline.thematic.extraction.templates.spec import (
    Article,
    PredicateRef,
    ResolvedEntity,
)


def _article(
    title: str = "",
    body: str = "",
    url: str = "https://example.com/x",
    source: str = "polygon",
    tickers: list[str] | None = None,
) -> Article:
    return Article(
        id="x",
        source=source,
        title=title,
        body=body,
        url=url,
        published_at=dt.datetime(2026, 5, 30, tzinfo=dt.UTC),
        tickers_raw=tickers or [],
    )


def _ctx(article: Article, entities: list[ResolvedEntity] | None = None) -> PredicateContext:
    return PredicateContext(
        article=article,
        resolved_entities=entities or [],
        blocklists={},
    )


class TestRegistryShape(unittest.TestCase):
    def test_dnia_jeden_predicate_set_is_exactly_six(self):
        # Adding/removing a predicate without updating this assertion is a
        # documented tripwire — the 20% custom-Python escape rule in the
        # design memo counts against this fixed denominator.
        expected = {
            "any_sentence_contains",
            "amount_mentioned",
            "entity_type_present",
            "not_in_blocklist",
            "is_press_release",
            "not_listicle",
        }
        self.assertEqual(set(available_predicates()), expected)
        self.assertEqual(set(PREDICATE_REGISTRY.keys()), expected)


class TestAnySentenceContains(unittest.TestCase):
    def test_match_case_insensitive(self):
        article = _article(body="The company Beats consensus expectations.")
        self.assertTrue(
            evaluate(
                PredicateRef(name="any_sentence_contains", kwargs={"words": ["beats"]}),
                _ctx(article),
            )
        )

    def test_no_match_when_word_absent(self):
        article = _article(body="The quarter was uneventful.")
        self.assertFalse(
            evaluate(
                PredicateRef(name="any_sentence_contains", kwargs={"words": ["beats"]}),
                _ctx(article),
            )
        )

    def test_word_boundary_avoids_substring_false_positive(self):
        # "beats" must not match "heartbeats".
        article = _article(body="His heartbeats were normal during the earnings call.")
        self.assertFalse(
            evaluate(
                PredicateRef(name="any_sentence_contains", kwargs={"words": ["beats"]}),
                _ctx(article),
            )
        )


class TestAmountMentioned(unittest.TestCase):
    def test_billion_with_dollar_sign(self):
        article = _article(body="A $5 billion all-cash deal.")
        self.assertTrue(evaluate(PredicateRef(name="amount_mentioned", kwargs={}), _ctx(article)))

    def test_short_form_BM(self):
        article = _article(body="The transaction is valued at $250M.")
        self.assertTrue(evaluate(PredicateRef(name="amount_mentioned", kwargs={}), _ctx(article)))

    def test_no_amount_returns_false(self):
        article = _article(body="The companies announced cooperation.")
        self.assertFalse(evaluate(PredicateRef(name="amount_mentioned", kwargs={}), _ctx(article)))

    def test_decimal_amount(self):
        article = _article(body="Valued at $1.2 billion.")
        self.assertTrue(evaluate(PredicateRef(name="amount_mentioned", kwargs={}), _ctx(article)))


class TestEntityTypePresent(unittest.TestCase):
    def test_match_company_when_company_resolved(self):
        ent = [ResolvedEntity(ticker="NVDA", name="NVIDIA", role="company")]
        self.assertTrue(
            evaluate(
                PredicateRef(name="entity_type_present", kwargs={"type": "company"}),
                _ctx(_article(), entities=ent),
            )
        )

    def test_no_match_when_empty_entity_set(self):
        self.assertFalse(
            evaluate(
                PredicateRef(name="entity_type_present", kwargs={"type": "company"}),
                _ctx(_article(), entities=[]),
            )
        )

    def test_no_match_when_type_differs(self):
        ent = [ResolvedEntity(ticker="NVDA", name="NVIDIA", role="company")]
        self.assertFalse(
            evaluate(
                PredicateRef(name="entity_type_present", kwargs={"type": "regulator"}),
                _ctx(_article(), entities=ent),
            )
        )


class TestNotInBlocklist(unittest.TestCase):
    def test_passes_when_url_not_in_named_list(self):
        ctx = PredicateContext(
            article=_article(url="https://nvidianews.nvidia.com/press"),
            resolved_entities=[],
            blocklists={"url_blocklist": [r"(?i)/coupons?/", r"(?i)/promo/"]},
        )
        self.assertTrue(
            evaluate(
                PredicateRef(name="not_in_blocklist", kwargs={"list": "url_blocklist"}),
                ctx,
            )
        )

    def test_fails_when_url_matches_blocklist(self):
        ctx = PredicateContext(
            article=_article(url="https://example.com/coupons/save50"),
            resolved_entities=[],
            blocklists={"url_blocklist": [r"(?i)/coupons?/", r"(?i)/promo/"]},
        )
        self.assertFalse(
            evaluate(
                PredicateRef(name="not_in_blocklist", kwargs={"list": "url_blocklist"}),
                ctx,
            )
        )

    def test_missing_named_list_passes(self):
        # A missing named list must NOT silently fail closed — that would
        # let a template misconfiguration block legitimate articles.
        # Behaviour: passes (cannot prove blocked); engine records a
        # warning via predicate telemetry (assertion not made here, the
        # holdout test owns that path).
        ctx = PredicateContext(
            article=_article(url="https://example.com/x"),
            resolved_entities=[],
            blocklists={},
        )
        self.assertTrue(
            evaluate(
                PredicateRef(name="not_in_blocklist", kwargs={"list": "unknown"}),
                ctx,
            )
        )


class TestIsPressRelease(unittest.TestCase):
    def test_prnewswire_source_matches(self):
        ctx = _ctx(_article(source="prnewswire"))
        self.assertTrue(evaluate(PredicateRef(name="is_press_release", kwargs={}), ctx))

    def test_businesswire_source_matches(self):
        ctx = _ctx(_article(source="businesswire"))
        self.assertTrue(evaluate(PredicateRef(name="is_press_release", kwargs={}), ctx))

    def test_edgar_press_release_source_matches(self):
        # 8-K Exhibit 99.1 issuer press releases enter via the
        # edgar_press_release source (PR-6) — issuer-direct, so they qualify
        # as a press release for the template engine's is_press_release gate.
        ctx = _ctx(_article(source="edgar_press_release"))
        self.assertTrue(evaluate(PredicateRef(name="is_press_release", kwargs={}), ctx))

    def test_title_with_press_release_marker_matches(self):
        ctx = _ctx(_article(source="generic", title="NVDA announces buyback (press release)"))
        self.assertTrue(evaluate(PredicateRef(name="is_press_release", kwargs={}), ctx))

    def test_ir_subdomain_matches(self):
        ctx = _ctx(
            _article(
                source="generic",
                url="https://ir.somecompany.com/news/2026/05/announcement",
            )
        )
        self.assertTrue(evaluate(PredicateRef(name="is_press_release", kwargs={}), ctx))

    def test_third_party_commentary_does_not_match(self):
        ctx = _ctx(
            _article(
                source="seekingalpha",
                url="https://seekingalpha.com/article/123-some-analysis",
                title="My take on the deal",
            )
        )
        self.assertFalse(evaluate(PredicateRef(name="is_press_release", kwargs={}), ctx))


class TestNotListicle(unittest.TestCase):
    def test_top_10_listicle_blocked(self):
        ctx = _ctx(_article(title="Top 10 best VPN services in 2026"))
        self.assertFalse(evaluate(PredicateRef(name="not_listicle", kwargs={}), ctx))

    def test_guide_to_listicle_blocked(self):
        ctx = _ctx(_article(title="A Guide to the Cheapest Webcams"))
        self.assertFalse(evaluate(PredicateRef(name="not_listicle", kwargs={}), ctx))

    def test_normal_headline_passes(self):
        ctx = _ctx(_article(title="NVIDIA announces $5 billion buyback program"))
        self.assertTrue(evaluate(PredicateRef(name="not_listicle", kwargs={}), ctx))

    def test_surfshark_pattern_blocked(self):
        # The exact concern that opened issue #143.
        ctx = _ctx(_article(title="Best Surfshark Coupon Codes (May 2026)"))
        self.assertFalse(evaluate(PredicateRef(name="not_listicle", kwargs={}), ctx))

    def test_best_buy_corporate_headline_passes(self):
        # Regression for zen-review HIGH finding (PR #322): `\d*` instead
        # of `\d+` on the listicle pattern caused "Best Buy Reports Record
        # Holiday Sales" to be classified as a listicle (because "Best "
        # plus zero digits matched). The fix tightens to `\d+` so the
        # digit suffix is mandatory.
        ctx = _ctx(_article(title="Best Buy Reports Record Holiday Sales"))
        self.assertTrue(evaluate(PredicateRef(name="not_listicle", kwargs={}), ctx))


class TestUnknownPredicateRaises(unittest.TestCase):
    def test_evaluating_unknown_predicate_raises(self):
        with self.assertRaises(KeyError):
            evaluate(PredicateRef(name="does_not_exist", kwargs={}), _ctx(_article()))


if __name__ == "__main__":
    unittest.main()
