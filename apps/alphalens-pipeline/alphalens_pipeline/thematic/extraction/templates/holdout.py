"""Drop-reason accumulator + Prometheus textfile flush.

The engine doesn't write Prometheus files on every drop — instead it
accumulates counts in :class:`TemplateMetrics` for the duration of one
CLI invocation, then flushes once via the existing
``observability.textfile.emit_domain_metrics`` surface
(``apps/alphalens-pipeline/alphalens_pipeline/observability/textfile.py``).

This keeps the "no black-box scoring" doctrine honest (every drop is
counted by reason) without forcing one disk write per article.

Reason strings live as module-level constants — plain ``str`` (not
``Enum``) so they slot into Prometheus label values without coercion.
"""

from __future__ import annotations

from collections import defaultdict

from alphalens_pipeline.observability.textfile import emit_domain_metrics

# Drop reasons, per design memo §2.4. Adding a reason requires:
#   1. Append the constant here
#   2. Add to ALL_HOLDOUT_REASONS
#   3. Update the Grafana panel's by-reason breakdown JSON
#   4. Update test_holdout.py::test_reason_set_matches_design_memo
HOLDOUT_NO_TEMPLATE_MATCH = "no_template_match"
HOLDOUT_ENTITY_UNRESOLVED = "entity_unresolved"
HOLDOUT_ALL_PREDICATES_FAILED = "all_predicates_failed"
# Used in PR-2 when Flash returns a noise event_type or confidence < 0.5
# AND no template matched. Defined here so the Grafana panel can be
# wired in PR-1 with the final reason set (avoids a panel JSON edit
# at PR-2 review time).
HOLDOUT_LOW_CONFIDENCE_NO_TEMPLATE = "low_confidence_no_template"
# Fires when a Flash event is dropped from the catalyst-resolver's working
# set because a template event exists for the same (primary_entity_ticker,
# event_type) within a 24h window (PR-2 precedence rule, design memo §1.1).
# The Flash event is NOT deleted from the events parquet — only filtered
# from the resolver pass so the "two truths" problem doesn't propagate to
# brief generation.
HOLDOUT_SUPERSEDED_BY_TEMPLATE = "superseded_by_template"

ALL_HOLDOUT_REASONS: frozenset[str] = frozenset(
    {
        HOLDOUT_NO_TEMPLATE_MATCH,
        HOLDOUT_ENTITY_UNRESOLVED,
        HOLDOUT_ALL_PREDICATES_FAILED,
        HOLDOUT_LOW_CONFIDENCE_NO_TEMPLATE,
        HOLDOUT_SUPERSEDED_BY_TEMPLATE,
    }
)

_VALID_PREDICATE_OUTCOMES = frozenset({"pass", "fail"})


class TemplateMetrics:
    """In-process accumulator. Single-threaded; one instance per engine.

    ``snapshot`` returns a plain-dict view used by tests + the CLI's
    end-of-run summary. ``flush`` serialises the accumulators to a
    Prometheus textfile via the canonical emitter.
    """

    def __init__(self) -> None:
        # Initialise all reasons to 0 so the flushed file always carries
        # the full reason set (Prometheus distinguishes absent-series
        # from zero-count; the panel's range queries need a series).
        self._holdout: dict[str, int] = dict.fromkeys(ALL_HOLDOUT_REASONS, 0)
        self._predicates: dict[tuple[str, str], int] = defaultdict(int)
        self._attempts: dict[str, int] = defaultdict(int)
        self._matches: dict[str, int] = defaultdict(int)

    def record_drop(self, reason: str) -> None:
        if reason not in ALL_HOLDOUT_REASONS:
            raise ValueError(f"unknown holdout reason: {reason!r}")
        self._holdout[reason] += 1

    def record_predicate(self, name: str, *, outcome: str) -> None:
        if outcome not in _VALID_PREDICATE_OUTCOMES:
            raise ValueError(f"predicate outcome must be 'pass' or 'fail', got {outcome!r}")
        self._predicates[(name, outcome)] += 1

    def record_attempt(self, template_id: str) -> None:
        self._attempts[template_id] += 1

    def record_match(self, template_id: str) -> None:
        self._matches[template_id] += 1

    def snapshot(self) -> dict:
        return {
            "holdout": dict(self._holdout),
            "predicates": dict(self._predicates),
            "attempts": dict(self._attempts),
            "matches": dict(self._matches),
        }

    def flush(self, job: str = "template-engine") -> None:
        """Write the accumulated state to a Prometheus textfile.

        Idempotent in the file-system sense (the textfile is overwritten
        on every call) but cumulative in the accumulator sense (counts
        are not reset). Callers wanting to start fresh should construct
        a new :class:`TemplateMetrics`.
        """
        metrics: dict[str, float | int] = {}
        for reason, count in self._holdout.items():
            metrics[f'alphalens_template_holdout_total{{reason="{reason}"}}'] = count
        for (name, outcome), count in self._predicates.items():
            metrics[f'alphalens_template_predicate_total{{name="{name}",outcome="{outcome}"}}'] = (
                count
            )
        for template_id, count in self._attempts.items():
            metrics[f'alphalens_template_attempt_total{{template_id="{template_id}"}}'] = count
        for template_id, count in self._matches.items():
            metrics[f'alphalens_template_match_total{{template_id="{template_id}"}}'] = count
        emit_domain_metrics(job=job, metrics=metrics)
