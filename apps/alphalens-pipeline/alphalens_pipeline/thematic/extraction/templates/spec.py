"""In-memory contracts the template engine consumes + produces.

The engine never operates on raw YAML or dicts — every code path past
``TemplateSpec.from_yaml`` works on the typed dataclasses defined here. YAML
exists only at the authoring + validation boundary. The locked design
(``docs/research/template_engine_design_2026_05_30.md`` §1.2) explicitly
calls for this split so tests can build templates in code and so PR-2 has
no YAML-shape coupling to discover.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Article + resolved entity (engine I/O)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Article:
    """One row of the unified ``thematic_news`` feed.

    Frozen because the engine evaluates predicates against the same
    ``Article`` repeatedly (once per template) and a mutation between
    those calls would make matching non-deterministic — exactly the
    failure mode templates exist to eliminate.
    """

    id: str
    source: str
    title: str
    body: str
    url: str
    published_at: Any  # datetime, kept loose to avoid timezone-coupling here
    tickers_raw: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ResolvedEntity:
    """A ticker the resolver has tied back to a real listed company.

    ``role`` is a coarse type tag (``company`` | ``regulator`` | ``person``
    | ``currency``) — finer-grained role assignment (acquirer / target)
    happens inside the engine based on positional order in the resolved
    list, not on this field.
    """

    ticker: str
    name: str
    role: str


# ---------------------------------------------------------------------------
# Template DSL primitives
# ---------------------------------------------------------------------------


@dataclass
class PredicateRef:
    """A reference to a named predicate from the registry."""

    name: str
    kwargs: dict[str, Any] = field(default_factory=dict)


@dataclass
class EntityRequirement:
    """One role the template declares against the resolved entity set."""

    role: str
    type: str
    required: bool


@dataclass
class FieldExtraction:
    """Either an entity-pointer extraction or a regex extraction."""

    field: str
    source: str | None = None  # e.g. "entity:acquirer" or "article.published_at"
    patterns: str | None = None
    post_process: list[str] = field(default_factory=list)


@dataclass
class TemplateSpec:
    """In-memory representation of one YAML template file."""

    template_id: str
    event_type: str
    description: str
    article_predicates: list[PredicateRef]
    entity_requirements: dict[str, EntityRequirement]
    extraction: list[FieldExtraction]

    @classmethod
    def from_yaml(cls, path: Path) -> TemplateSpec:
        """Compile a YAML template file into a :class:`TemplateSpec`.

        Raises:
            ValueError: when the filename stem does not equal
                ``template_id`` — the convention is load-bearing for
                ``alphalens templates validate <path>`` per-file errors
                and for the engine's deterministic load order.
        """
        text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
        if not isinstance(data, dict):
            raise ValueError(f"{path}: top-level YAML must be a mapping")

        template_id = data.get("template_id")
        if template_id != path.stem:
            raise ValueError(
                f"{path}: filename {path.stem!r} must match template_id {template_id!r}"
            )

        predicates = [_parse_predicate(p) for p in data.get("article_predicates", [])]

        entity_requirements: dict[str, EntityRequirement] = {}
        for role_name, role_spec in (data.get("entity_requirements") or {}).items():
            entity_requirements[role_name] = EntityRequirement(
                role=role_name,
                type=role_spec.get("type", "company"),
                required=bool(role_spec.get("required", True)),
            )

        extractions: list[FieldExtraction] = []
        for raw in data.get("extraction") or []:
            extractions.append(
                FieldExtraction(
                    field=raw["field"],
                    source=raw.get("source"),
                    patterns=raw.get("patterns"),
                    post_process=list(raw.get("post_process") or []),
                )
            )

        return cls(
            template_id=template_id,
            event_type=data["event_type"],
            description=data.get("description", ""),
            article_predicates=predicates,
            entity_requirements=entity_requirements,
            extraction=extractions,
        )


def _parse_predicate(raw: Any) -> PredicateRef:
    """Accept either ``"predicate_name"`` or ``{"name": ..., "kwargs": ...}``."""
    if isinstance(raw, str):
        return PredicateRef(name=raw, kwargs={})
    if isinstance(raw, dict):
        return PredicateRef(name=raw["name"], kwargs=dict(raw.get("kwargs") or {}))
    raise ValueError(f"unrecognised predicate spec: {raw!r}")


# ---------------------------------------------------------------------------
# Engine output
# ---------------------------------------------------------------------------


@dataclass
class TemplateEvent:
    """Engine result when an article matches a template."""

    template_id: str
    event_type: str
    entities: dict[str, ResolvedEntity]
    fields: dict[str, Any]
    source_article_id: str
    matched_predicates: list[str]

    def __post_init__(self) -> None:
        # Provenance is non-negotiable — the brief generator (PR-3)
        # cites template_facts back to the source article id; an empty
        # value would break that audit trail silently.
        if not self.source_article_id:
            raise ValueError("TemplateEvent.source_article_id must be non-empty")
