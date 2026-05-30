"""JSON Schema for template YAML files + a file-level validator.

Used by:
- ``alphalens templates validate`` CLI as a pre-commit-hook surface
- ``test_ship_templates.py`` to lint every shipped template
- the engine's startup loader as a fail-fast guard before
  :func:`TemplateSpec.from_yaml` runs (a bad YAML that survives the
  schema would surface as a bare ``KeyError`` downstream, much less
  actionable than the schema's path-pointed error message)
"""

from __future__ import annotations

from pathlib import Path

import jsonschema
import yaml

from alphalens_pipeline.thematic.extraction.schema import EVENT_TYPES
from alphalens_pipeline.thematic.extraction.templates.predicates import (
    available_predicates,
)

# JSON Schema for a single template file. The ``event_type`` enum + the
# ``article_predicates[].name`` enum are computed at module import time
# from the live registry so a new predicate or event_type is picked up
# automatically — no schema edit needed in lock-step.
TEMPLATE_JSON_SCHEMA: dict = {
    "$schema": "https://json-schema.org/draft-07/schema",
    "type": "object",
    "required": [
        "template_id",
        "event_type",
        "description",
        "article_predicates",
        "entity_requirements",
        "extraction",
    ],
    "additionalProperties": False,
    "properties": {
        "template_id": {"type": "string", "minLength": 1},
        "event_type": {"type": "string", "enum": list(EVENT_TYPES)},
        "description": {"type": "string"},
        "article_predicates": {
            "type": "array",
            "items": {
                "oneOf": [
                    # Shorthand form: a bare predicate name.
                    {"type": "string", "enum": available_predicates()},
                    # Full form: {name: ..., kwargs: {...}}.
                    {
                        "type": "object",
                        "required": ["name"],
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string", "enum": available_predicates()},
                            "kwargs": {"type": "object"},
                        },
                    },
                ]
            },
        },
        "entity_requirements": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "required": ["type"],
                "additionalProperties": False,
                "properties": {
                    "type": {"type": "string"},
                    "required": {"type": "boolean"},
                },
            },
        },
        "extraction": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["field"],
                "additionalProperties": False,
                "properties": {
                    "field": {"type": "string", "minLength": 1},
                    "source": {"type": "string"},
                    "patterns": {"type": "string"},
                    "post_process": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
}


def validate_template_file(path: Path) -> list[str]:
    """Return a list of human-readable error messages for ``path``.

    Empty list ⇒ valid. The CLI prints each line + exits non-zero on
    non-empty output; the pre-commit hook contract relies on that.

    The validator also enforces ``template_id == path.stem`` (load-time
    convention; see :meth:`TemplateSpec.from_yaml`) and tries to surface
    YAML-parse errors with line numbers when PyYAML provides them.
    """
    errors: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"{path.name}: cannot read file: {exc}"]

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        # PyYAML's MarkedYAMLError carries .problem_mark with .line/.column.
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            return [f"{path.name}:{mark.line + 1}: yaml parse error: {exc.problem}"]
        return [f"{path.name}: yaml parse error: {exc}"]

    if not isinstance(data, dict):
        return [f"{path.name}: top-level YAML must be a mapping"]

    try:
        jsonschema.validate(instance=data, schema=TEMPLATE_JSON_SCHEMA)
    except jsonschema.ValidationError as exc:
        # ``json_path`` reads e.g. "$.article_predicates[0]" which is
        # immediately readable in a terminal.
        path_marker = getattr(exc, "json_path", "$")
        errors.append(f"{path.name}: schema violation at {path_marker}: {exc.message}")

    template_id = data.get("template_id")
    if template_id and template_id != path.stem:
        errors.append(
            f"{path.name}: filename stem {path.stem!r} must match template_id {template_id!r}"
        )

    return errors
