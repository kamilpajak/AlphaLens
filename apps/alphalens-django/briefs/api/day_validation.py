"""The ``/v1/days/{date}`` ``meta.validation`` block — machine-generated
honesty context.

Every day payload carries the same standing caveats the dashboards show
(selection unvalidated, live edge base rate, T-1 dating, scorer version) so an
external consumer reading raw JSON cannot mistake the ranked list for a
validated signal. All numbers are COMPUTED from the same stores the dashboards
read — never hand-written prose — so the block can not rot.

The constants below are pinned by exact-string tests (external consumers read
them; plain English, no jargon).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from briefs.models import Brief
from edge.api.base_rate import get_edge_base_rate

# The selection funnel has shown no edge past multiplicity to date; every
# attribution pass so far came back null (see the paradigm postmortem).
SELECTION_STATUS = "UNVALIDATED"

# One factual sentence about the composite score external consumers sort on.
LAYER4_NOTE = (
    "layer4_weighted_score is a display-only composite; "
    "selection shows no edge past multiplicity to date."
)

# Briefs are dated T-1: the pipeline run on day N writes the brief for N-1.
BRIEF_DATING = "T-1"


def _resolve_scorer_config_version(asof: dt.date) -> str | None:
    """The day's scorer version — exactly one non-blank stamped value, else None.

    The pipeline stamps one version per day, so the distinct set is normally a
    single value. Blank ("") marks a pre-atr-tilt date. A mixed day should not
    occur (one score-stage run per day); serving any single value would
    misattribute the rest of the rows, so mixed → None (conservative).
    """
    distinct = set(
        Brief.objects.filter(date=asof).values_list("scorer_config_version", flat=True).distinct()
    )
    if len(distinct) == 1:
        value = next(iter(distinct))
        return value or None
    return None


def build_validation_block(asof: dt.date) -> dict[str, Any]:
    """Assemble ``meta.validation`` for one day payload."""
    return {
        "selection_status": SELECTION_STATUS,
        "layer4_weighted_score_note": LAYER4_NOTE,
        "edge_base_rate": get_edge_base_rate(),
        "brief_dating": BRIEF_DATING,
        "scorer_config_version": _resolve_scorer_config_version(asof),
    }
