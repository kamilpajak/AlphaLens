"""``ONeilExpert`` — the numeric-only Expert Protocol adapter.

O'Neil satisfies the :class:`~alphalens_pipeline.experts.base.Expert` Protocol
structurally. It is numeric-only: ``assess_qualitative`` returns ``None`` (zero
LLM cost) and it is NOT a ``QualEnrichExpert`` (no ``enrich_brief_frame`` /
``migrate_qual_cache``), so the qualitative enrich driver skips it.

The production scoring path is :func:`oneil.quant_enrichment.enrich`, which has the
score-stage frame and so can supply O'Neil's frame-derived technical terms (N / L).
The Protocol ``compute_panel`` here only has ``(ticker, asof)`` — it cannot see the
frame — so it computes the store-derivable earnings + split terms and leaves the
technicals ``None``. ``compute_score`` scores whatever panel it is given (the frame
path passes a full panel; the Protocol path's technical-less panel scores ``None``
under the mandatory-N gate). This asymmetry is intentional and documented.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import asdict
from typing import Any, cast

from alphalens_pipeline.experts.base import Assessment, Panel
from alphalens_pipeline.experts.oneil.comparison import ONeilPanel
from alphalens_pipeline.experts.oneil.quant_enrichment import (
    ONEIL_COLUMNS,
    PanelFn,
    build_default_panel_fn,
)
from alphalens_pipeline.experts.oneil.score import compute_oneil_score


def _panel_from_dict(panel: Panel) -> ONeilPanel:
    """Reconstruct an :class:`ONeilPanel` from a panel ``dict`` (``asdict`` round-trip)."""
    return ONeilPanel(**cast("dict[str, Any]", panel))


class ONeilExpert:
    """Momentum / technical lens. Satisfies the :class:`Expert` Protocol structurally.

    Numeric-only — NOT a ``QualEnrichExpert``: no ``enrich_brief_frame`` /
    ``migrate_qual_cache``, and ``assess_qualitative`` returns ``None``.
    """

    id = "oneil"
    name = "William O'Neil (momentum / technicals)"
    column_names = ONEIL_COLUMNS

    def __init__(self, panel_fn: PanelFn | None = None) -> None:
        self._panel_fn = panel_fn

    def _resolve_panel_fn(self) -> PanelFn:
        if self._panel_fn is None:
            self._panel_fn = build_default_panel_fn([])
        return self._panel_fn

    def compute_panel(
        self, ticker: str, asof: dt.date, *, context: Mapping[str, str] | None = None
    ) -> Panel | None:
        # The Protocol path lacks the frame, so the three reused technicals are
        # ``None`` here; the production frame path (quant_enrichment.enrich) supplies
        # them. The earnings + split terms are still computed from the wired store.
        theme = (context or {}).get("theme", "")
        panel = self._resolve_panel_fn()(ticker, theme, asof, dict.fromkeys(_TECHNICAL_FIELDS))
        return None if panel is None else asdict(panel)

    def compute_score(self, panel: Panel | None) -> float | None:
        if panel is None:
            return None
        return compute_oneil_score(_panel_from_dict(panel))

    def assess_qualitative(
        self, panel: Panel | None, asof: dt.date, ticker: str
    ) -> Assessment | None:
        # Numeric-only: O'Neil has no qualitative layer (zero LLM cost).
        return None


# The three frame-derived technical fields the Protocol path cannot populate.
_TECHNICAL_FIELDS = ("pct_off_52w_high", "ma200_slope_pct_per_day", "ma200_distance_pct")


__all__ = ["ONeilExpert"]
