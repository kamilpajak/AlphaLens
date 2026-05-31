"""Structured event templates (issue #143, PR-1 standalone module).

The package is the deterministic half of the hybrid extraction architecture
locked in ``docs/research/template_engine_design_2026_05_30.md``. PR-1 ships
the engine + 5 ship templates + Prometheus telemetry as a standalone module
not yet wired into the pipeline. PR-2 calls ``TemplateEngine.match`` from
``event_extractor.py`` ahead of the DeepSeek Flash fallback path.

Public surface (re-exported here for convenient imports):

- :class:`TemplateEngine` — match articles against the template library
- :class:`TemplateSpec` / :class:`TemplateEvent` — in-memory contracts
- :class:`Article` / :class:`ResolvedEntity` — engine I/O dataclasses
- :class:`EntityResolver` — feed-tag normalize + alias resolution
- :class:`TemplateMetrics` — accumulator that flushes to Prometheus textfile
- :func:`validate_template_file` — JSON Schema validator (used by CLI)
"""

from __future__ import annotations

from alphalens_pipeline.thematic.extraction.templates.engine import TemplateEngine
from alphalens_pipeline.thematic.extraction.templates.entity_resolver import (
    EntityResolver,
)
from alphalens_pipeline.thematic.extraction.templates.holdout import (
    ALL_HOLDOUT_REASONS,
    HOLDOUT_ALL_PREDICATES_FAILED,
    HOLDOUT_ENTITY_UNRESOLVED,
    HOLDOUT_LOW_CONFIDENCE_NO_TEMPLATE,
    HOLDOUT_NO_TEMPLATE_MATCH,
    TemplateMetrics,
)
from alphalens_pipeline.thematic.extraction.templates.predicates import (
    PREDICATE_REGISTRY,
    PredicateContext,
    available_predicates,
    evaluate,
)
from alphalens_pipeline.thematic.extraction.templates.spec import (
    Article,
    EntityRequirement,
    FieldExtraction,
    PredicateRef,
    ResolvedEntity,
    TemplateEvent,
    TemplateSpec,
)
from alphalens_pipeline.thematic.extraction.templates.yaml_schema import (
    TEMPLATE_JSON_SCHEMA,
    validate_template_file,
)

__all__ = [
    "ALL_HOLDOUT_REASONS",
    "HOLDOUT_ALL_PREDICATES_FAILED",
    "HOLDOUT_ENTITY_UNRESOLVED",
    "HOLDOUT_LOW_CONFIDENCE_NO_TEMPLATE",
    "HOLDOUT_NO_TEMPLATE_MATCH",
    "PREDICATE_REGISTRY",
    "TEMPLATE_JSON_SCHEMA",
    "Article",
    "EntityRequirement",
    "EntityResolver",
    "FieldExtraction",
    "PredicateContext",
    "PredicateRef",
    "ResolvedEntity",
    "TemplateEngine",
    "TemplateEvent",
    "TemplateMetrics",
    "TemplateSpec",
    "available_predicates",
    "evaluate",
    "validate_template_file",
]
