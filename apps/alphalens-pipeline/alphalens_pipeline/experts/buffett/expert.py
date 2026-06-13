"""The Buffett expert — value / quality lens adapter over the existing machinery.

Wraps (does NOT duplicate) the moved Buffett functions behind the
:class:`~alphalens_pipeline.experts.base.Expert` Protocol: the numeric panel comes
from the existing ``build_default_panel_fn`` + ``compute_panel``, the 0-100 score
from ``compute_quality_score``, and the qualitative classification from
``assess_panel_qualitative``.

PR-1 scaffolding: this adapter + the registry are consumed by tests. The WIRED
thematic score stage still calls ``quant_enrichment.enrich`` directly; routing the
generalized enrichment through the registry (and adding O'Neil) is PR-2 / PR-7.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import asdict
from typing import Any, cast

from alphalens_pipeline.experts.base import Assessment, Panel
from alphalens_pipeline.experts.buffett.comparison import BuffettPanel
from alphalens_pipeline.experts.buffett.quality_score import compute_quality_score
from alphalens_pipeline.experts.buffett.quant_enrichment import (
    BUFFETT_COLUMNS,
    PanelFn,
    build_default_panel_fn,
)

# The five qualitative CONTENT columns the assessment yields. The three
# provenance columns (used_scuttlebutt / qual_computed_at / qual_config_version)
# are stamped by the qual-enrich pass and join the panel surface in PR-2.
_QUAL_CONTENT_COLUMNS: tuple[str, ...] = (
    "buffett_moat_type",
    "buffett_moat_trend",
    "buffett_management_candor",
    "buffett_understandable",
    "buffett_qualitative_rationale",
)


def _panel_from_dict(panel: Panel) -> BuffettPanel:
    """Reconstruct a :class:`BuffettPanel` from a panel ``dict``.

    ``Panel`` is a wide scalar union (``float | bool | str | None``) while the
    dataclass fields are precisely typed; the round-trip is lossless because the
    dict was produced by ``asdict`` of a ``BuffettPanel``, so the cast is safe and
    silences the spread-type check.
    """
    return BuffettPanel(**cast("dict[str, Any]", panel))


class BuffettExpert:
    """Value / quality lens. Satisfies the :class:`Expert` Protocol structurally."""

    id = "buffett"
    name = "Warren Buffett (value / quality)"
    column_names = BUFFETT_COLUMNS + _QUAL_CONTENT_COLUMNS

    def __init__(self, panel_fn: PanelFn | None = None) -> None:
        # Injectable for tests; the default wires the real store LAZILY on first
        # use and reuses it across tickers (the store preload is an optimisation,
        # not a correctness requirement, so an empty preload list is fine here —
        # the wired hot path keeps its own per-batch panel_fn in quant_enrichment).
        self._panel_fn = panel_fn

    def _resolve_panel_fn(self) -> PanelFn:
        # Empty preload: the registry is only used by tests in PR-1; the wired hot
        # path (the thematic score stage) passes its own per-batch panel_fn with
        # the candidate tickers preloaded, so this lazy instance never carries the
        # preload optimisation (correctness is unaffected — fetches are lazy).
        if self._panel_fn is None:
            self._panel_fn = build_default_panel_fn([])
        return self._panel_fn

    def compute_panel(
        self, ticker: str, asof: dt.date, *, context: Mapping[str, str] | None = None
    ) -> Panel | None:
        theme = (context or {}).get("theme", "")
        panel = self._resolve_panel_fn()(ticker, theme, asof)
        return None if panel is None else asdict(panel)

    def compute_score(self, panel: Panel | None) -> float | None:
        if panel is None:
            return None
        return compute_quality_score(_panel_from_dict(panel))

    def assess_qualitative(
        self,
        panel: Panel | None,
        asof: dt.date,
        ticker: str,
        *,
        scuttlebutt_client=None,
    ) -> Assessment | None:
        # Lazy import: keeps the qual (10-K + LLM) machinery off the import path
        # of anything that only needs the numeric seams.
        from alphalens_pipeline.experts.buffett.qual_enrichment import (
            assess_panel_qualitative,
        )

        if panel is None:
            return None
        assessment = assess_panel_qualitative(
            _panel_from_dict(panel), asof, scuttlebutt_client=scuttlebutt_client
        )
        if assessment is None:
            return None
        return {
            "buffett_moat_type": assessment.moat_type,
            "buffett_moat_trend": assessment.moat_trend,
            "buffett_management_candor": assessment.management_candor,
            "buffett_understandable": assessment.understandable,
            "buffett_qualitative_rationale": assessment.rationale,
        }


__all__ = ["BuffettExpert"]
