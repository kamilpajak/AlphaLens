"""Registry of exit-stop WHAT-IF lenses + grid replay.

Each lens is an alternative EXIT-STOP policy re-applied to the SAME picks and the
SAME retained price paths — entry tiers, TP ladder, and the pick held fixed, so a
difference in realized R is the exit-stop effect, not the selection. The result is
a display-only counterfactual map ``{lens_id: realized_r}`` stamped exactly like
``grid_realized_r_json``; it NEVER overrides the headline ``realized_r``.

Two lens KINDS today (dispatched by ``BreakevenLens.kind``):

* ``"breakeven"`` — an MFE-triggered break-even / trailing stop
  (:func:`replay_ladder_breakeven`, PR #722). ``mfe_trigger_r`` / ``trail_frac``.
* ``"fill_anchored"`` — a stop sized to the ACTUAL fill rather than the planned
  deep ladder (:func:`realized_r_fill_anchored`, exit-geometry path b). The tight
  fill-anchored stop collapses the ladder to a single-tier E1 entry. ``stop_atr_mult``.

The registry is data-driven: adding a lens is one entry here, no schema or UI
change (the stamped column is a JSON map and the ``/edge`` selector reads the
registry). ``status`` is ``"in_sample"`` until forward N crosses the validation
gate, then ``"validated"`` — a flag flip, not a code change. The design doctrine
(default-realized, in-sample labelling, registry-driven selector) lives in
``docs/research/edge_whatif_lens_registry_plan_2026_06_30.md``.

NAMING NOTE: the historical module / ``BREAKEVEN_LENSES`` / ``breakeven_grid`` /
``breakeven_realized_r_json`` names predate the second (fill-anchored) kind and are
kept to avoid a data migration on the live stamped column. Read them as the general
"exit-stop what-if" registry.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from alphalens_pipeline.feedback.ladder_replay import (
    realized_r_fill_anchored,
    replay_ladder_breakeven,
)

_DEFAULT_STOP_ATR_MULT = 0.5


@dataclass(frozen=True)
class BreakevenLens:
    """One registered exit-stop what-if policy.

    ``status`` ∈ {``"in_sample"``, ``"validated"``}: a lens read from the same
    sample it was tuned on is ``in_sample`` (optimistic, not proof); it graduates
    to ``validated`` only once a clean forward sample crosses the N-gate.

    ``kind`` selects the replay: ``"breakeven"`` uses ``mfe_trigger_r`` /
    ``trail_frac``; ``"fill_anchored"`` uses ``stop_atr_mult``. Kind-irrelevant
    params stay ``None``.
    """

    lens_id: str
    label: str
    category: str
    status: str
    kind: str = "breakeven"
    mfe_trigger_r: float | None = None  # kind="breakeven"
    trail_frac: float | None = None  # kind="breakeven"
    stop_atr_mult: float | None = None  # kind="fill_anchored"


# Adding another lens is a single entry here — the JSON-map column and the
# registry-driven selector absorb it with no schema or UI change. Both current
# lenses are exit-stop counterfactuals, display-only, in_sample.
BREAKEVEN_LENSES: tuple[BreakevenLens, ...] = (
    BreakevenLens(
        lens_id="be_0p5r",
        label="break-even +0.5R",
        category="exit-stop",
        status="in_sample",
        kind="breakeven",
        mfe_trigger_r=0.5,
        trail_frac=None,
    ),
    BreakevenLens(
        lens_id="fill_anchored_0p5atr",
        label="fill-anchored stop (0.5·ATR)",
        category="exit-stop",
        status="in_sample",
        kind="fill_anchored",
        stop_atr_mult=0.5,
    ),
)


def _lens_realized_r(
    lens: BreakevenLens,
    trade_setup: Mapping[str, Any] | None,
    bars: Sequence[Mapping[str, Any]],
) -> float | None:
    """Dispatch one lens to its replay, returning realized R (or ``None``)."""
    if lens.kind == "fill_anchored":
        mult = lens.stop_atr_mult if lens.stop_atr_mult is not None else _DEFAULT_STOP_ATR_MULT
        return realized_r_fill_anchored(trade_setup, bars, stop_atr_mult=mult)
    # default: MFE-triggered break-even / trailing. A missing trigger reduces to a
    # static disaster-stop walk (baseline parity), never an error.
    trigger = lens.mfe_trigger_r if lens.mfe_trigger_r is not None else float("inf")
    return replay_ladder_breakeven(
        trade_setup, bars, mfe_trigger_r=trigger, trail_frac=lens.trail_frac
    )


def breakeven_grid(
    trade_setup: Mapping[str, Any] | None,
    bars: Sequence[Mapping[str, Any]],
) -> dict[str, float | None]:
    """Realized R under each registered exit-stop lens, keyed by ``lens_id``.

    Re-replays the SAME bars under each lens's exit-stop policy (break-even or
    fill-anchored), reusing the pure replay engine. A lens that cannot resolve
    (unparseable setup / no bars / no fill / risk <= 0 / missing ATR for a
    fill-anchored lens) maps to ``None``. Display-only; never feeds the headline
    ``realized_r``.
    """
    return {lens.lens_id: _lens_realized_r(lens, trade_setup, bars) for lens in BREAKEVEN_LENSES}
