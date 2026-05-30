"""Static lint over the 5 ship templates under ``templates/templates/*.yaml``.

The intent is to fail CI fast if a template is added that the engine
cannot load. The actual matching semantics of each template against
representative articles are exercised in ``test_engine.py``; this file
just guarantees the YAML / schema layer holds.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from alphalens_pipeline.thematic.extraction.schema import EVENT_TYPES
from alphalens_pipeline.thematic.extraction.templates.predicates import (
    available_predicates,
)
from alphalens_pipeline.thematic.extraction.templates.spec import TemplateSpec
from alphalens_pipeline.thematic.extraction.templates.yaml_schema import (
    validate_template_file,
)

TEMPLATES_DIR = (
    Path(__file__).parent.parent.parent.parent.parent
    / "alphalens-pipeline"
    / "alphalens_pipeline"
    / "thematic"
    / "extraction"
    / "templates"
    / "templates"
)

EXPECTED_SHIP_TEMPLATES = {
    "m_and_a_press_release",
    "earnings_surprise",
    "financing_announcement",
    "guidance_update",
    "regulatory_action",
}


class TestShipTemplatesPresent(unittest.TestCase):
    def test_templates_dir_exists(self):
        self.assertTrue(
            TEMPLATES_DIR.is_dir(),
            f"expected ship templates dir at {TEMPLATES_DIR}",
        )

    def test_exactly_the_expected_5_templates(self):
        actual = {p.stem for p in TEMPLATES_DIR.glob("*.yaml")}
        # Equality (not subset) is intentional — adding a 6th template
        # without updating this assertion catches accidental drift between
        # ship-set and design memo §2.2.
        self.assertEqual(actual, EXPECTED_SHIP_TEMPLATES)


class TestShipTemplatesValid(unittest.TestCase):
    def test_every_template_passes_validation(self):
        for path in sorted(TEMPLATES_DIR.glob("*.yaml")):
            with self.subTest(template=path.name):
                errors = validate_template_file(path)
                self.assertEqual(
                    errors,
                    [],
                    f"{path.name} validation errors: {errors}",
                )

    def test_every_template_compiles_to_spec(self):
        for path in sorted(TEMPLATES_DIR.glob("*.yaml")):
            with self.subTest(template=path.name):
                spec = TemplateSpec.from_yaml(path)
                self.assertEqual(spec.template_id, path.stem)
                self.assertIn(spec.event_type, EVENT_TYPES)
                # Every predicate name reference must resolve in the registry.
                known = set(available_predicates())
                for pred in spec.article_predicates:
                    self.assertIn(
                        pred.name,
                        known,
                        f"{path.name} references unknown predicate {pred.name!r}",
                    )

    def test_every_template_has_at_least_one_predicate(self):
        # A template with zero predicates would fire on every article that
        # has the right entity shape — basically a tautology. Catch the
        # mistake at lint time.
        for path in sorted(TEMPLATES_DIR.glob("*.yaml")):
            with self.subTest(template=path.name):
                spec = TemplateSpec.from_yaml(path)
                self.assertGreater(len(spec.article_predicates), 0)


if __name__ == "__main__":
    unittest.main()
