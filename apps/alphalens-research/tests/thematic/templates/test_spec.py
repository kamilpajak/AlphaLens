"""Contracts for ``TemplateSpec`` / ``TemplateEvent`` dataclasses.

These tests pin the in-memory shape that the engine + every PR-2/3/4 callsite
consume. YAML is the authoring surface; the engine itself never sees a dict.
"""

from __future__ import annotations

import datetime as dt
import tempfile
import textwrap
import unittest
from pathlib import Path

from alphalens_pipeline.thematic.extraction.templates.spec import (
    Article,
    EntityRequirement,
    FieldExtraction,
    PredicateRef,
    ResolvedEntity,
    TemplateEvent,
    TemplateSpec,
)


class TestArticleShape(unittest.TestCase):
    def test_article_minimum_fields(self):
        article = Article(
            id="poly:abc",
            source="polygon",
            title="NVDA announces acquisition of XYZ",
            body="...",
            url="https://example.com/x",
            published_at=dt.datetime(2026, 5, 30, tzinfo=dt.UTC),
            tickers_raw=["NVDA", "XYZ"],
        )
        self.assertEqual(article.tickers_raw, ["NVDA", "XYZ"])
        # Article must be immutable so callers don't quietly mutate the
        # row mid-pipeline and corrupt downstream determinism.
        with self.assertRaises(Exception):
            article.title = "mutated"  # type: ignore[misc]


class TestTemplateSpecFromYaml(unittest.TestCase):
    def _write(self, content: str) -> Path:
        tmp = Path(tempfile.mkdtemp()) / "m_and_a_press_release.yaml"
        tmp.write_text(textwrap.dedent(content))
        return tmp

    def test_minimal_valid_template(self):
        path = self._write(
            """\
            template_id: m_and_a_press_release
            event_type: m_and_a
            description: "Acquirer announces acquisition of target"
            article_predicates:
              - is_press_release
              - amount_mentioned
            entity_requirements:
              acquirer:
                type: company
                required: true
              target:
                type: company
                required: true
            extraction:
              - field: acquirer_ticker
                source: "entity:acquirer"
              - field: target_ticker
                source: "entity:target"
            """
        )
        spec = TemplateSpec.from_yaml(path)
        self.assertEqual(spec.template_id, "m_and_a_press_release")
        self.assertEqual(spec.event_type, "m_and_a")
        self.assertEqual(len(spec.article_predicates), 2)
        self.assertIsInstance(spec.article_predicates[0], PredicateRef)
        self.assertEqual(spec.article_predicates[0].name, "is_press_release")
        self.assertEqual(spec.article_predicates[0].kwargs, {})
        self.assertEqual(spec.entity_requirements["acquirer"].required, True)
        self.assertEqual(spec.entity_requirements["target"].type, "company")
        self.assertEqual(spec.extraction[0].field, "acquirer_ticker")
        self.assertEqual(spec.extraction[0].source, "entity:acquirer")

    def test_predicate_with_kwargs(self):
        # Local helper writes `m_and_a_press_release.yaml`; rename to
        # match this test's `earnings_surprise` template_id since the
        # filename-stem == template_id rule is enforced by from_yaml.
        original = self._write(
            """\
            template_id: earnings_surprise
            event_type: earnings
            description: ""
            article_predicates:
              - name: any_sentence_contains
                kwargs:
                  words: ["beats", "misses", "tops estimates"]
            entity_requirements:
              reporter:
                type: company
                required: true
            extraction: []
            """
        )
        path = original.parent / "earnings_surprise.yaml"
        original.rename(path)
        spec = TemplateSpec.from_yaml(path)
        self.assertEqual(spec.article_predicates[0].name, "any_sentence_contains")
        self.assertEqual(
            spec.article_predicates[0].kwargs,
            {"words": ["beats", "misses", "tops estimates"]},
        )

    def test_filename_must_match_template_id(self):
        path = Path(tempfile.mkdtemp()) / "wrong_name.yaml"
        path.write_text(
            textwrap.dedent(
                """\
                template_id: m_and_a_press_release
                event_type: m_and_a
                description: ""
                article_predicates: []
                entity_requirements: {}
                extraction: []
                """
            )
        )
        with self.assertRaises(ValueError) as cm:
            TemplateSpec.from_yaml(path)
        self.assertIn("filename", str(cm.exception).lower())


class TestTemplateEvent(unittest.TestCase):
    def test_event_carries_all_required_provenance(self):
        event = TemplateEvent(
            template_id="m_and_a_press_release",
            event_type="m_and_a",
            entities={
                "acquirer": ResolvedEntity(ticker="NVDA", name="NVIDIA", role="company"),
                "target": ResolvedEntity(ticker="XYZ", name="XYZ Corp", role="company"),
            },
            fields={"consideration_usd": 5_000_000_000},
            source_article_id="poly:abc",
            matched_predicates=["is_press_release", "amount_mentioned"],
        )
        self.assertEqual(event.entities["acquirer"].ticker, "NVDA")
        self.assertEqual(event.fields["consideration_usd"], 5_000_000_000)
        self.assertEqual(event.matched_predicates, ["is_press_release", "amount_mentioned"])
        # Engine never emits a TemplateEvent without provenance: an empty
        # source_article_id is a structural bug worth catching at write time.
        with self.assertRaises(ValueError):
            TemplateEvent(
                template_id="x",
                event_type="m_and_a",
                entities={},
                fields={},
                source_article_id="",
                matched_predicates=[],
            )


class TestFieldExtraction(unittest.TestCase):
    def test_regex_field_carries_patterns(self):
        path = Path(tempfile.mkdtemp()) / "x.yaml"
        path.write_text(
            textwrap.dedent(
                """\
                template_id: x
                event_type: m_and_a
                description: ""
                article_predicates: []
                entity_requirements: {}
                extraction:
                  - field: consideration_usd
                    patterns: '\\$(?P<amount>[\\d.]+)\\s*(?P<unit>billion|million|B|M)'
                    post_process: [normalize_amount_usd]
                """
            )
        )
        # filename must equal template_id — rename probe
        renamed = path.parent / "x.yaml"
        spec = TemplateSpec.from_yaml(renamed)
        f = spec.extraction[0]
        self.assertIsInstance(f, FieldExtraction)
        self.assertEqual(f.field, "consideration_usd")
        self.assertIn("amount", f.patterns or "")
        self.assertEqual(f.post_process, ["normalize_amount_usd"])

    def test_entity_requirement_dataclass(self):
        req = EntityRequirement(role="acquirer", type="company", required=True)
        self.assertEqual(req.role, "acquirer")
        self.assertEqual(req.type, "company")
        self.assertTrue(req.required)


if __name__ == "__main__":
    unittest.main()
