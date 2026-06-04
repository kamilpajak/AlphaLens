"""``TemplateEngine`` — the deterministic half of hybrid extraction.

Per design memo §1.1:
- Engine iterates the loaded ``TemplateSpec`` list in directory order
- First template that passes all predicates + entity-requirements wins
- Returns ``TemplateEvent | None`` (None ⇒ drop, with reason recorded
  on the engine's :class:`TemplateMetrics` accumulator)

Pipeline integration (PR-2) calls ``match`` from ``event_extractor.py``
ahead of the DeepSeek Flash fallback path. PR-1 ships the engine
standalone — no event_extractor wiring.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from alphalens_pipeline.thematic.extraction.templates.holdout import (
    HOLDOUT_ALL_PREDICATES_FAILED,
    HOLDOUT_ENTITY_UNRESOLVED,
    HOLDOUT_NO_TEMPLATE_MATCH,
    TemplateMetrics,
)
from alphalens_pipeline.thematic.extraction.templates.predicates import (
    PredicateContext,
    evaluate,
)
from alphalens_pipeline.thematic.extraction.templates.spec import (
    Article,
    ResolvedEntity,
    TemplateEvent,
    TemplateSpec,
)

logger = logging.getLogger(__name__)


# Post-process registry. Templates name post-process functions; the engine
# looks them up here. Each function takes the raw regex-group dict + returns
# the canonical value. Adding one requires a name here + a unit test in
# ``test_engine.py``.
def _normalize_amount_usd(groups: dict[str, str]) -> int | None:
    """Convert ``{"amount": "5", "unit": "billion"}`` → ``5_000_000_000``."""
    amount_raw = (groups.get("amount") or "").replace(",", "")
    unit = (groups.get("unit") or "").lower()
    if not amount_raw:
        return None
    try:
        amount = float(amount_raw)
    except ValueError:
        return None
    multiplier = {
        "billion": 1_000_000_000,
        "bn": 1_000_000_000,
        "b": 1_000_000_000,
        "million": 1_000_000,
        "mn": 1_000_000,
        "m": 1_000_000,
    }.get(unit)
    if multiplier is None:
        return None
    return int(amount * multiplier)


_POST_PROCESS_REGISTRY: dict[str, Any] = {
    "normalize_amount_usd": _normalize_amount_usd,
}


class TemplateEngine:
    """Match articles against a fixed library of compiled templates."""

    def __init__(
        self,
        specs: list[TemplateSpec],
        *,
        blocklists: dict[str, list[str]] | None = None,
        metrics: TemplateMetrics | None = None,
    ) -> None:
        self.specs: list[TemplateSpec] = list(specs)
        self.blocklists: dict[str, list[str]] = dict(blocklists or {})
        self.metrics: TemplateMetrics = metrics or TemplateMetrics()

    # -- alt constructors ---------------------------------------------------

    @classmethod
    def from_specs(cls, specs: list[TemplateSpec]) -> TemplateEngine:
        return cls(specs)

    @classmethod
    def from_dir(cls, templates_dir: Path) -> TemplateEngine:
        """Compile every ``*.yaml`` file under ``templates_dir`` in sorted order.

        Sorted order keeps multi-template precedence (§"first-match-wins"
        in test_engine.py) deterministic across machines and across
        ``os.listdir`` ordering quirks.
        """
        templates_dir = Path(templates_dir)
        specs = [TemplateSpec.from_yaml(p) for p in sorted(templates_dir.glob("*.yaml"))]
        return cls(specs)

    # -- core match ---------------------------------------------------------

    def match(
        self,
        article: Article,
        entities: list[ResolvedEntity],
    ) -> TemplateEvent | None:
        """Return the first matching template event, else None.

        Side effects: increments accumulator counters on
        ``self.metrics`` according to the path taken (attempt, match,
        predicate outcomes, drop reason).
        """
        # Step 1: no templates loaded → record + drop.
        if not self.specs:
            self.metrics.record_drop(HOLDOUT_NO_TEMPLATE_MATCH)
            return None

        # Step 2: zero resolved entities → no template can fire; drop with
        # the distinct entity_unresolved reason so operators can tell the
        # difference from "templates exist but none fit".
        if not entities:
            self.metrics.record_drop(HOLDOUT_ENTITY_UNRESOLVED)
            return None

        # Step 3: try each template in order. First successful match wins.
        any_attempt_failed = False
        for spec in self.specs:
            self.metrics.record_attempt(spec.template_id)
            event = self._try_match(spec, article, entities)
            if event is not None:
                self.metrics.record_match(spec.template_id)
                return event
            any_attempt_failed = True

        # Step 4: at least one template was tried but none matched →
        # all_predicates_failed (collapses predicate failure + missing
        # required role into one drop reason; see test_engine.py).
        if any_attempt_failed:
            self.metrics.record_drop(HOLDOUT_ALL_PREDICATES_FAILED)
        return None

    # -- helpers ------------------------------------------------------------

    def _try_match(
        self,
        spec: TemplateSpec,
        article: Article,
        entities: list[ResolvedEntity],
    ) -> TemplateEvent | None:
        ctx = PredicateContext(
            article=article,
            resolved_entities=entities,
            blocklists=self.blocklists,
        )

        # Predicates: every required predicate must pass. Record outcomes
        # for telemetry, short-circuit on the first fail so a template with
        # 10 predicates doesn't pay 9 unnecessary calls.
        matched: list[str] = []
        for pred in spec.article_predicates:
            try:
                ok = evaluate(pred, ctx)
            except KeyError:
                logger.warning(
                    "template %s references unknown predicate %s — did the YAML bypass validation?",
                    spec.template_id,
                    pred.name,
                )
                self.metrics.record_predicate(pred.name, outcome="fail")
                return None
            self.metrics.record_predicate(pred.name, outcome="pass" if ok else "fail")
            if not ok:
                return None
            matched.append(pred.name)

        # Entity role assignment: positional. Required roles in declaration
        # order are filled from ``entities`` in declaration order. If the
        # template requires N roles and only M < N entities are resolved,
        # the unfilled required roles cause the template to drop.
        assigned: dict[str, ResolvedEntity] = {}
        cursor = 0
        for role_name, requirement in spec.entity_requirements.items():
            if cursor < len(entities):
                assigned[role_name] = entities[cursor]
                cursor += 1
            elif requirement.required:
                return None
            # else: optional role left unfilled — fine

        # Field extraction.
        fields: dict[str, Any] = {}
        for extr in spec.extraction:
            value = self._extract_field(extr, article, assigned)
            if value is not None:
                fields[extr.field] = value

        return TemplateEvent(
            template_id=spec.template_id,
            event_type=spec.event_type,
            entities=assigned,
            fields=fields,
            source_article_id=article.id,
            matched_predicates=matched,
        )

    def _extract_field(
        self,
        extr: Any,
        article: Article,
        assigned: dict[str, ResolvedEntity],
    ) -> Any:
        source = extr.source or ""
        if source.startswith("entity:"):
            return self._extract_entity_field(extr, assigned)
        if source.startswith("article."):
            attr = source.split(".", 1)[1]
            return getattr(article, attr, None)
        if extr.patterns:
            return self._extract_regex_field(extr, article)
        return None

    def _extract_entity_field(self, extr: Any, assigned: dict[str, ResolvedEntity]) -> Any:
        """``entity:<role>`` source — read from the role assignment map.

        MVP convention (covers all 5 dnia-jeden ship templates): if the
        YAML field name ends in ``_ticker`` we return the ticker symbol,
        otherwise we return the resolved entity's display name. This is
        implicit and brittle — a new template using ``field: target``
        (without ``_ticker`` suffix) silently gets the company name
        rather than the symbol. A more robust mapping (explicit
        ``source_kind: ticker|name`` in YAML) is deferred to a follow-up
        PR once the template library grows beyond the initial five.
        Caught by zen pre-merge review of PR #322 (MEDIUM, deferred).
        """
        role_name = extr.source.split(":", 1)[1]
        ent = assigned.get(role_name)
        if ent is None:
            return None
        return ent.ticker if extr.field.endswith("_ticker") else ent.name

    def _extract_regex_field(self, extr: Any, article: Article) -> Any:
        """Regex source — run pattern against title+body, apply post_process."""
        haystack = f"{article.title}\n{article.body}"
        m = re.search(extr.patterns, haystack, re.IGNORECASE)
        if not m:
            return None
        groups = m.groupdict()
        value: Any = groups or m.group(0)
        for fn_name in extr.post_process:
            fn = _POST_PROCESS_REGISTRY.get(fn_name)
            if fn is None:
                logger.warning("unknown post_process function: %s", fn_name)
                continue
            value = fn(value if isinstance(value, dict) else groups)
        return value
