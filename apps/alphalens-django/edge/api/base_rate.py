"""ORM wrapper for the ``/v1/days`` ``meta.validation.edge_base_rate`` block.

Thin seam between the briefs API (which composes the day payload) and the
LadderOutcome mirror: the ORM query stays edge-side so LadderOutcome knowledge
never leaks into ``briefs``; the math stays in the Django-free
``edge.api.summary.build_validation_base_rate``.

Computed per request, no cache: the query reads 4 narrow columns over the
mirror (~hundreds of rows, grows ~15/day) and the mirror changes once daily
(the 06:30 UTC ExecStartPost rebuild), so per-request recompute is the only
way the block can never rot. If it ever shows up in profiles, memoize keyed
on ``max(DayMetaLadderOutcome.rebuilt_at)``.
"""

from __future__ import annotations

from typing import Any

from edge.api.summary import build_validation_base_rate
from edge.models import LadderOutcome


def get_edge_base_rate() -> dict[str, Any]:
    """Live edge base rate over ALL dates (same default window as ``/edge``)."""
    rows = LadderOutcome.objects.filter(plannable=True, terminal=True).values(
        "plannable", "terminal", "realized_r", "matured_at"
    )
    return build_validation_base_rate(rows)
