"""End-to-end ``TemplateEngine.match`` contract.

The engine is the integration point all of PR-1's other modules feed into.
Tests construct ``TemplateSpec`` in code (no YAML) to keep failures pinned
to the matching algorithm itself.
"""

from __future__ import annotations

import datetime as dt
import unittest

from alphalens_pipeline.thematic.extraction.templates.engine import TemplateEngine
from alphalens_pipeline.thematic.extraction.templates.holdout import (
    HOLDOUT_ALL_PREDICATES_FAILED,
    HOLDOUT_ENTITY_UNRESOLVED,
    HOLDOUT_NO_TEMPLATE_MATCH,
)
from alphalens_pipeline.thematic.extraction.templates.spec import (
    Article,
    EntityRequirement,
    FieldExtraction,
    PredicateRef,
    ResolvedEntity,
    TemplateSpec,
)


def _article(
    title: str = "NVDA announces $5 billion acquisition of XYZ",
    body: str = "NVIDIA today announced a $5 billion all-cash acquisition of XYZ Corp.",
    source: str = "businesswire",
    url: str = "https://www.businesswire.com/news/home/x",
    tickers: list[str] | None = None,
) -> Article:
    return Article(
        id="bw:abc",
        source=source,
        title=title,
        body=body,
        url=url,
        published_at=dt.datetime(2026, 5, 30, tzinfo=dt.UTC),
        tickers_raw=tickers if tickers is not None else ["NVDA", "XYZ"],
    )


def _m_and_a_spec() -> TemplateSpec:
    return TemplateSpec(
        template_id="m_and_a_press_release",
        event_type="m_and_a",
        description="Acquirer announces acquisition of target",
        article_predicates=[
            PredicateRef(name="is_press_release", kwargs={}),
            PredicateRef(name="amount_mentioned", kwargs={}),
            PredicateRef(name="not_listicle", kwargs={}),
        ],
        entity_requirements={
            "acquirer": EntityRequirement(role="acquirer", type="company", required=True),
            "target": EntityRequirement(role="target", type="company", required=True),
        },
        extraction=[
            FieldExtraction(field="acquirer_ticker", source="entity:acquirer"),
            FieldExtraction(field="target_ticker", source="entity:target"),
            FieldExtraction(
                field="consideration_usd",
                patterns=r"\$(?P<amount>[\d.]+)\s*(?P<unit>billion|million|B|M)",
                post_process=["normalize_amount_usd"],
            ),
        ],
    )


def _resolved(*tickers: str) -> list[ResolvedEntity]:
    return [ResolvedEntity(ticker=t, name=t, role="company") for t in tickers]


class TestMatchSuccessPath(unittest.TestCase):
    def test_full_match_emits_template_event(self):
        engine = TemplateEngine.from_specs([_m_and_a_spec()])
        article = _article()
        event = engine.match(article, entities=_resolved("NVDA", "XYZ"))
        self.assertIsNotNone(event)
        assert event is not None  # for type narrowing
        self.assertEqual(event.template_id, "m_and_a_press_release")
        self.assertEqual(event.event_type, "m_and_a")
        # Entities assigned positionally to declared roles (PR-1 MVP).
        self.assertEqual(event.entities["acquirer"].ticker, "NVDA")
        self.assertEqual(event.entities["target"].ticker, "XYZ")
        # Regex field extraction populated.
        self.assertEqual(event.fields.get("consideration_usd"), 5_000_000_000)
        # All three predicates recorded as matched.
        self.assertEqual(
            set(event.matched_predicates),
            {"is_press_release", "amount_mentioned", "not_listicle"},
        )
        # Provenance is the article id, not anything synthesised.
        self.assertEqual(event.source_article_id, "bw:abc")

    def test_metrics_increment_on_match(self):
        engine = TemplateEngine.from_specs([_m_and_a_spec()])
        engine.match(_article(), entities=_resolved("NVDA", "XYZ"))
        snap = engine.metrics.snapshot()
        self.assertEqual(snap["attempts"]["m_and_a_press_release"], 1)
        self.assertEqual(snap["matches"]["m_and_a_press_release"], 1)


class TestNoMatchPaths(unittest.TestCase):
    def test_zero_resolved_entities_records_entity_unresolved(self):
        engine = TemplateEngine.from_specs([_m_and_a_spec()])
        event = engine.match(_article(tickers=[]), entities=[])
        self.assertIsNone(event)
        snap = engine.metrics.snapshot()
        self.assertEqual(snap["holdout"][HOLDOUT_ENTITY_UNRESOLVED], 1)

    def test_predicate_failure_records_all_predicates_failed(self):
        engine = TemplateEngine.from_specs([_m_and_a_spec()])
        article = _article(
            source="seekingalpha",
            url="https://seekingalpha.com/article/x",
            title="Listicle: Top 5 M&A Deals This Week",
            body="No specific amounts mentioned.",
        )
        event = engine.match(article, entities=_resolved("NVDA", "XYZ"))
        self.assertIsNone(event)
        snap = engine.metrics.snapshot()
        # The all-predicates-failed reason fires when at least one
        # template was attempted but none of its predicates passed.
        self.assertGreaterEqual(snap["holdout"][HOLDOUT_ALL_PREDICATES_FAILED], 1)

    def test_no_template_in_library_records_no_template_match(self):
        engine = TemplateEngine.from_specs([])
        event = engine.match(_article(), entities=_resolved("NVDA"))
        self.assertIsNone(event)
        snap = engine.metrics.snapshot()
        self.assertEqual(snap["holdout"][HOLDOUT_NO_TEMPLATE_MATCH], 1)


class TestEntityRequirementSemantics(unittest.TestCase):
    def test_missing_required_role_records_all_predicates_failed(self):
        # Template requires acquirer + target; only one resolved → reject.
        engine = TemplateEngine.from_specs([_m_and_a_spec()])
        event = engine.match(_article(tickers=["NVDA"]), entities=_resolved("NVDA"))
        self.assertIsNone(event)
        snap = engine.metrics.snapshot()
        # Required-role miss collapses into the same drop family as a
        # predicate failure (both = "template didn't fit"). The Grafana
        # panel groups them — separating would inflate the reason set
        # without operational value at this stage.
        self.assertGreaterEqual(snap["holdout"][HOLDOUT_ALL_PREDICATES_FAILED], 1)

    def test_optional_role_can_be_absent(self):
        spec = TemplateSpec(
            template_id="x",
            event_type="m_and_a",
            description="",
            article_predicates=[PredicateRef(name="is_press_release", kwargs={})],
            entity_requirements={
                "acquirer": EntityRequirement(role="acquirer", type="company", required=True),
                "target": EntityRequirement(role="target", type="company", required=False),
            },
            extraction=[FieldExtraction(field="acquirer_ticker", source="entity:acquirer")],
        )
        engine = TemplateEngine.from_specs([spec])
        article = _article(tickers=["NVDA"])
        event = engine.match(article, entities=_resolved("NVDA"))
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.entities["acquirer"].ticker, "NVDA")
        self.assertNotIn("target", event.entities)


class TestMultiTemplateOrdering(unittest.TestCase):
    def test_first_match_wins_deterministic_order(self):
        spec_first = TemplateSpec(
            template_id="m_and_a_press_release",
            event_type="m_and_a",
            description="",
            article_predicates=[PredicateRef(name="is_press_release", kwargs={})],
            entity_requirements={
                "acquirer": EntityRequirement(role="acquirer", type="company", required=True),
            },
            extraction=[],
        )
        spec_second = TemplateSpec(
            template_id="earnings_surprise",
            event_type="earnings",
            description="",
            article_predicates=[PredicateRef(name="is_press_release", kwargs={})],
            entity_requirements={
                "reporter": EntityRequirement(role="reporter", type="company", required=True),
            },
            extraction=[],
        )
        engine = TemplateEngine.from_specs([spec_first, spec_second])
        event = engine.match(_article(tickers=["NVDA"]), entities=_resolved("NVDA"))
        assert event is not None
        # First template in the library that passes wins; downstream
        # PR-2 catalyst_resolver implements the cross-source precedence.
        self.assertEqual(event.template_id, "m_and_a_press_release")


class TestEngineFromDir(unittest.TestCase):
    def test_loads_yaml_directory(self):
        import tempfile
        import textwrap
        from pathlib import Path

        tmpdir = Path(tempfile.mkdtemp())
        (tmpdir / "smoke.yaml").write_text(
            textwrap.dedent(
                """\
                template_id: smoke
                event_type: m_and_a
                description: "loader smoke"
                article_predicates:
                  - is_press_release
                entity_requirements:
                  acquirer:
                    type: company
                    required: true
                extraction:
                  - field: acquirer_ticker
                    source: "entity:acquirer"
                """
            )
        )
        engine = TemplateEngine.from_dir(tmpdir)
        self.assertEqual(len(engine.specs), 1)
        self.assertEqual(engine.specs[0].template_id, "smoke")


if __name__ == "__main__":
    unittest.main()
