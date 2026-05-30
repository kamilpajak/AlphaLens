"""JSON Schema validation of template YAML files.

The schema enforces top-level shape + that every predicate name referenced in
``article_predicates`` exists in the engine's predicate registry. The intent
is that ``alphalens templates validate`` catches structural drift before a
broken template lands on main.
"""

from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from alphalens_pipeline.thematic.extraction.templates.yaml_schema import (
    TEMPLATE_JSON_SCHEMA,
    validate_template_file,
)


class TestSchemaShape(unittest.TestCase):
    def test_top_level_required_keys(self):
        required = TEMPLATE_JSON_SCHEMA["required"]
        for key in (
            "template_id",
            "event_type",
            "description",
            "article_predicates",
            "entity_requirements",
            "extraction",
        ):
            self.assertIn(key, required)

    def test_event_type_must_be_in_event_types_enum(self):
        # event_type field constrained to the canonical ``EVENT_TYPES`` enum
        # from ``thematic.extraction.schema`` — drift is a structural break.
        from alphalens_pipeline.thematic.extraction.schema import EVENT_TYPES

        ev = TEMPLATE_JSON_SCHEMA["properties"]["event_type"]["enum"]
        self.assertEqual(set(ev), set(EVENT_TYPES))


def _write(content: str, name: str = "m_and_a_press_release.yaml") -> Path:
    tmp = Path(tempfile.mkdtemp()) / name
    tmp.write_text(textwrap.dedent(content))
    return tmp


class TestValidateGoodTemplates(unittest.TestCase):
    def test_minimal_valid(self):
        path = _write(
            """\
            template_id: m_and_a_press_release
            event_type: m_and_a
            description: ""
            article_predicates:
              - is_press_release
            entity_requirements:
              acquirer:
                type: company
                required: true
            extraction: []
            """
        )
        errors = validate_template_file(path)
        self.assertEqual(errors, [])

    def test_predicate_with_kwargs_valid(self):
        path = _write(
            """\
            template_id: earnings_surprise
            event_type: earnings
            description: ""
            article_predicates:
              - name: any_sentence_contains
                kwargs:
                  words: ["beats"]
            entity_requirements:
              reporter:
                type: company
                required: true
            extraction: []
            """,
            name="earnings_surprise.yaml",
        )
        errors = validate_template_file(path)
        self.assertEqual(errors, [])


class TestValidateBadTemplates(unittest.TestCase):
    def test_unknown_predicate_name_fails(self):
        path = _write(
            """\
            template_id: bad_predicate
            event_type: m_and_a
            description: ""
            article_predicates:
              - does_not_exist
            entity_requirements: {}
            extraction: []
            """,
            name="bad_predicate.yaml",
        )
        errors = validate_template_file(path)
        self.assertTrue(errors)
        self.assertTrue(
            any("does_not_exist" in e for e in errors),
            f"expected unknown-predicate error, got: {errors}",
        )

    def test_unknown_event_type_fails(self):
        path = _write(
            """\
            template_id: bad_event
            event_type: not_a_real_event_type
            description: ""
            article_predicates: []
            entity_requirements: {}
            extraction: []
            """,
            name="bad_event.yaml",
        )
        errors = validate_template_file(path)
        self.assertTrue(errors)

    def test_missing_required_key_fails(self):
        path = _write(
            """\
            template_id: missing
            description: ""
            article_predicates: []
            entity_requirements: {}
            extraction: []
            """,
            name="missing.yaml",
        )
        errors = validate_template_file(path)
        self.assertTrue(errors)
        self.assertTrue(any("event_type" in e for e in errors))

    def test_template_id_mismatch_filename_fails(self):
        path = _write(
            """\
            template_id: actual_id
            event_type: m_and_a
            description: ""
            article_predicates: []
            entity_requirements: {}
            extraction: []
            """,
            name="different_filename.yaml",
        )
        errors = validate_template_file(path)
        self.assertTrue(errors)
        self.assertTrue(any("filename" in e.lower() for e in errors))

    def test_malformed_yaml_fails_gracefully(self):
        path = Path(tempfile.mkdtemp()) / "bad_yaml.yaml"
        path.write_text("template_id: bad_yaml\nevent_type: [unclosed")
        errors = validate_template_file(path)
        self.assertTrue(errors)
        self.assertTrue(any("yaml" in e.lower() or "parse" in e.lower() for e in errors))

    def test_template_id_with_prometheus_unsafe_chars_fails(self):
        # template_id flows into a Prometheus label without escaping
        # (see holdout.flush). Regression for zen-review MEDIUM (PR #322):
        # an analyst naming a template `m&a press release` would silently
        # break the scrape. JSON Schema regex now rejects non-snake-case.
        path = Path(tempfile.mkdtemp()) / "m&a_press_release.yaml"
        path.write_text(
            textwrap.dedent(
                """\
                template_id: "m&a_press_release"
                event_type: m_and_a
                description: ""
                article_predicates: []
                entity_requirements: {}
                extraction: []
                """
            )
        )
        errors = validate_template_file(path)
        self.assertTrue(errors)
        self.assertTrue(
            any("template_id" in e or "pattern" in e.lower() for e in errors),
            f"expected template_id pattern violation, got: {errors}",
        )


if __name__ == "__main__":
    unittest.main()
