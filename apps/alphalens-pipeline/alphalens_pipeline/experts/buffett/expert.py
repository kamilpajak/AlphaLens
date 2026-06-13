"""The Buffett expert — value / quality lens adapter over the existing machinery.

Wraps (does NOT duplicate) the moved Buffett functions behind the
:class:`~alphalens_pipeline.experts.base.Expert` Protocol: the numeric panel comes
from the existing ``build_default_panel_fn`` + ``compute_panel``, the 0-100 score
from ``compute_quality_score``, and the qualitative classification from
``assess_panel_qualitative``.

The eager qualitative enrich (``enrich_brief_frame``, the ``QualEnrichExpert``
capability) is driven by the registry via ``experts.enrich.enrich_briefs`` (PR-2).
The numeric score-stage stamping still calls ``quant_enrichment.enrich`` directly;
O'Neil (numeric-only, no qual layer) follows in PR-7.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, cast

from alphalens_pipeline.experts.base import Assessment, Panel
from alphalens_pipeline.experts.buffett.comparison import BuffettPanel
from alphalens_pipeline.experts.buffett.quality_score import compute_quality_score
from alphalens_pipeline.experts.buffett.quant_enrichment import (
    BUFFETT_COLUMNS,
    PanelFn,
    build_default_panel_fn,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pandas as pd

    from alphalens_pipeline.experts.buffett.qual_enrichment import AssessOne

# The five qualitative CONTENT columns the assessment yields.
_QUAL_CONTENT_COLUMNS: tuple[str, ...] = (
    "buffett_moat_type",
    "buffett_moat_trend",
    "buffett_management_candor",
    "buffett_understandable",
    "buffett_qualitative_rationale",
)

# The three provenance columns the qual-enrich pass stamps alongside the content
# (folded into the expert's declared surface in PR-2; their VALUES still come from
# QualRecord via the unchanged qual_enrichment.stamp_columns map).
_QUAL_PROVENANCE_COLUMNS: tuple[str, ...] = (
    "buffett_used_scuttlebutt",
    "buffett_qual_computed_at",
    "buffett_qual_config_version",
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
    column_names = BUFFETT_COLUMNS + _QUAL_CONTENT_COLUMNS + _QUAL_PROVENANCE_COLUMNS

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

    def enrich_brief_frame(
        self,
        df: pd.DataFrame,
        brief_date: dt.date,
        *,
        briefs_dir: Path | None = None,
        store,
        mcap_fn,
        dividends_fn,
        exec_comp_fn=None,
        scuttlebutt: bool = False,
        cache_dir: Path | None = None,
        assess_one: AssessOne | None = None,
    ) -> tuple[pd.DataFrame, int]:
        """Stamp the eight Buffett qualitative columns into ``df`` (the eager,
        cached qual layer — the :class:`QualEnrichExpert` capability).

        Delegates verbatim to the moved Buffett machinery: ``build_comparison``
        builds the panels, ``enrich_qualitative`` computes the records against the
        immutable per-(config_version, date, ticker, scuttlebutt) cache (PR-0),
        and ``stamp_columns`` merges the eight columns by ticker. Returns the
        (stamped) frame plus the count of names that resolved a real
        classification. ``assess_one`` is injectable for tests. The single atomic
        write is the driver's responsibility (read-once / write-once).
        """
        from alphalens_pipeline.experts.buffett.comparison import build_comparison
        from alphalens_pipeline.experts.buffett.qual_enrichment import (
            DEFAULT_QUAL_CACHE_DIR,
            _is_real,
            enrich_qualitative,
            stamp_columns,
        )

        panels = build_comparison(
            brief_date,
            briefs_dir=briefs_dir,
            store=store,
            mcap_fn=mcap_fn,
            dividends_fn=dividends_fn,
            exec_comp_fn=exec_comp_fn,
        )
        records = enrich_qualitative(
            panels,
            asof=brief_date,
            scuttlebutt=scuttlebutt,
            cache_dir=cache_dir if cache_dir is not None else DEFAULT_QUAL_CACHE_DIR,
            assess_one=assess_one,
        )
        by_ticker = {panel.ticker.upper(): rec for panel, rec in zip(panels, records, strict=True)}
        out = stamp_columns(df, by_ticker)
        n_real = sum(1 for rec in records if rec is not None and _is_real(rec))
        return out, n_real

    def migrate_qual_cache(self, cache_dir: Path | None = None) -> int:
        """Move this expert's legacy untagged cache into the version-tiered layout."""
        from alphalens_pipeline.experts.buffett.qual_enrichment import (
            DEFAULT_QUAL_CACHE_DIR,
            migrate_legacy_qual_cache,
        )

        return migrate_legacy_qual_cache(
            cache_dir if cache_dir is not None else DEFAULT_QUAL_CACHE_DIR
        )


__all__ = ["BuffettExpert"]
