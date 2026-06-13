"""The Expert Protocol — one orthogonal lens over a candidate.

An expert produces, per candidate, three things: a numeric PANEL (facts from
authoritative sources, NEVER the LLM), a 0-100 composite SCORE, and an optional
QUALITATIVE assessment (an LLM classification over injected facts, or ``None`` for
a numeric-only expert such as O'Neil). Each output is a CHARACTERISTIC, display-
only until that expert's Expert×EDGE correlation is validated — no expert output
feeds selection or ordering.

The interface is intentionally minimal and STRUCTURAL (:class:`typing.Protocol`)
so a new expert is a new class + a registry entry, never an interface change. The
panel/assessment are flat ``dict`` mappings rather than per-expert dataclasses, so
each expert owns its own fact shape without leaking it into the Protocol; the only
shared contract is "column name -> scalar".
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Protocol, runtime_checkable

# A panel is a flat mapping of column name -> scalar (or None). Deliberately a
# wide scalar union so a momentum expert (floats) and a quality expert (floats +
# bools) share one type without a per-expert dataclass in the interface.
Panel = dict[str, float | bool | str | None]

# A qualitative assessment is a flat mapping of column name -> classification
# value (enum string / bool / free text), or ``None`` when the expert has none.
Assessment = dict[str, str | bool | None]


@runtime_checkable
class Expert(Protocol):
    """One orthogonal lens. Implementations are thin adapters over the expert's
    existing numeric + (optional) qualitative machinery."""

    #: Stable short id used as the registry key + persistence key (PR-3+).
    id: str
    #: Human-readable label.
    name: str
    #: The flat column names this expert stamps (panel + score + qualitative).
    column_names: tuple[str, ...]

    def compute_panel(
        self, ticker: str, asof: dt.date, *, context: Mapping[str, str] | None = None
    ) -> Panel | None:
        """Numeric facts for one candidate, or ``None`` when unavailable.

        NEVER the LLM — authoritative sources only (doctrine). ``context`` carries
        non-numeric hints (e.g. the candidate's ``theme``) WITHOUT making them
        positional, so a future expert can read more context keys without a
        signature change.
        """
        ...

    def compute_score(self, panel: Panel | None) -> float | None:
        """A 0-100 composite over ``panel`` (or ``None`` when there is no panel)."""
        ...

    def assess_qualitative(
        self, panel: Panel | None, asof: dt.date, ticker: str
    ) -> Assessment | None:
        """An optional LLM classification over injected facts.

        Returns ``None`` for a numeric-only expert (e.g. O'Neil) — zero LLM cost.
        """
        ...


__all__ = ["Assessment", "Expert", "Panel"]
