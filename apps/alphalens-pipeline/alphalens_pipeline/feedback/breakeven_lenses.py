"""Registry of break-even / trailing exit-stop WHAT-IF lenses + grid replay.

Each lens is an alternative EXIT-STOP policy re-applied to the SAME picks and the
SAME retained price paths via :func:`replay_ladder_breakeven` (PR #722) — entry
tiers, TP ladder, and the pick held fixed, so a difference in realized R is the
exit-stop effect, not the selection. The result is a display-only counterfactual
map ``{lens_id: realized_r}`` stamped exactly like ``grid_realized_r_json``; it
NEVER overrides the headline ``realized_r``.

The registry is data-driven: adding a lens is one entry here, no schema or UI
change (the stamped column is a JSON map and the ``/edge`` selector reads the
registry). ``status`` is ``"in_sample"`` until forward N crosses the validation
gate, then ``"validated"`` — a flag flip, not a code change. The design doctrine
(default-realized, in-sample labelling, registry-driven selector) lives in
``docs/research/edge_whatif_lens_registry_plan_2026_06_30.md``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from alphalens_pipeline.feedback.ladder_replay import replay_ladder_breakeven


@dataclass(frozen=True)
class BreakevenLens:
    """One registered exit-stop what-if policy.

    ``status`` ∈ {``"in_sample"``, ``"validated"``}: a lens read from the same
    sample it was tuned on is ``in_sample`` (optimistic, not proof); it graduates
    to ``validated`` only once a clean forward sample crosses the N-gate.
    """

    lens_id: str
    label: str
    mfe_trigger_r: float
    trail_frac: float | None
    category: str
    status: str


# MVP: one registered lens. Adding another (e.g. a trailing variant) is a single
# entry here — the JSON-map column and the registry-driven selector absorb it with
# no schema or UI change.
BREAKEVEN_LENSES: tuple[BreakevenLens, ...] = (
    BreakevenLens(
        lens_id="be_0p5r",
        label="break-even +0.5R",
        mfe_trigger_r=0.5,
        trail_frac=None,
        category="exit-stop",
        status="in_sample",
    ),
)


def breakeven_grid(
    trade_setup: Mapping[str, Any] | None,
    bars: Sequence[Mapping[str, Any]],
) -> dict[str, float | None]:
    """Realized R under each registered break-even lens, keyed by ``lens_id``.

    Re-replays the SAME bars under each lens's MFE-triggered break-even / trailing
    stop, reusing the pure :func:`replay_ladder_breakeven`. A lens that cannot
    resolve (unparseable setup / no bars / no fill / risk <= 0) maps to ``None``.
    Display-only; never feeds the headline ``realized_r``.
    """
    return {
        lens.lens_id: replay_ladder_breakeven(
            trade_setup,
            bars,
            mfe_trigger_r=lens.mfe_trigger_r,
            trail_frac=lens.trail_frac,
        )
        for lens in BREAKEVEN_LENSES
    }
