"""Registry of exit-stop WHAT-IF lenses + grid replay.

Each lens is an alternative EXIT-STOP policy re-applied to the SAME picks and the
SAME retained price paths — entry tiers, TP ladder, and the pick held fixed, so a
difference in realized R is the exit-stop effect, not the selection. The result is
a display-only counterfactual map ``{lens_id: realized_r}`` stamped exactly like
``grid_realized_r_json``; it NEVER overrides the headline ``realized_r``.

Three lens KINDS today (dispatched by ``BreakevenLens.kind``):

* ``"breakeven"`` — an MFE-triggered break-even / trailing stop
  (:func:`replay_ladder_breakeven`, PR #722). ``mfe_trigger_r`` / ``trail_frac``.
* ``"fill_anchored"`` — a stop sized to the ACTUAL fill rather than the planned
  deep ladder (:func:`realized_r_fill_anchored`, exit-geometry path b). The tight
  fill-anchored stop collapses the ladder to a single-tier E1 entry. ``stop_atr_mult``.
* ``"atr_bracket"`` — a fixed symmetric ATR bracket around the blended entry
  (:func:`replay_ladder_atr_bracket`, bezpazery v1): static stop
  ``stop_atr_mult`` ATRs below, single 100% TP at
  ``min(52w ceiling, max(cost floor, tp_atr_mult ATRs above))``. The 52w ceiling
  is NOT in the trade setup — the caller threads the brief's
  ``technical_pct_off_52w_high`` into :func:`breakeven_grid`, which reconstructs
  the ceiling from ``asof_close``. ``stop_atr_mult`` / ``tp_atr_mult`` /
  ``tp_floor_frac`` / ``anchor_mode``. The ``anchor_mode`` says WHICH entry blend
  the bracket is placed around and is mandatory (issue #1114): ``"planned"``
  (all intended tiers — what the live rail places against) or ``"realised"``
  (tiers that touched in the bar walk). Both are registered, so the pair is
  measurable side by side; they differ on every partial fill.

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

from broker_contract.exit_geometry.levels import ceiling_from_52w_high

from alphalens_pipeline.feedback.ladder_replay import (
    realized_r_fill_anchored,
    replay_ladder_atr_bracket,
    replay_ladder_breakeven,
)

_DEFAULT_STOP_ATR_MULT = 0.5
# atr_bracket-kind defaults (bezpazery v1 pinned values — memo §2).
_DEFAULT_BRACKET_STOP_ATR_MULT = 1.5
_DEFAULT_BRACKET_TP_ATR_MULT = 1.5
_DEFAULT_BRACKET_TP_FLOOR_FRAC = 0.006


@dataclass(frozen=True)
class BreakevenLens:
    """One registered exit-stop what-if policy.

    ``status`` ∈ {``"in_sample"``, ``"validated"``}: a lens read from the same
    sample it was tuned on is ``in_sample`` (optimistic, not proof); it graduates
    to ``validated`` only once a clean forward sample crosses the N-gate.

    ``kind`` selects the replay: ``"breakeven"`` uses ``mfe_trigger_r`` /
    ``trail_frac``; ``"fill_anchored"`` uses ``stop_atr_mult``; ``"atr_bracket"``
    uses ``stop_atr_mult`` / ``tp_atr_mult`` / ``tp_floor_frac``. Kind-irrelevant
    params stay ``None``.

    ``preregistered_ref`` is the provenance pointer for a lens whose parameters
    were written down BEFORE it was added here (design memo section), so its
    forward read is not an in-sample pick. ``None`` for lenses tuned on the same
    sample they are read from. The slim Django image cannot import this registry,
    so ``edge/api/summary.py`` mirrors the non-None refs in
    ``_LENS_PREREGISTERED_REF`` (drift pinned by a research-side parity test).
    """

    lens_id: str
    label: str
    category: str
    status: str
    kind: str = "breakeven"
    mfe_trigger_r: float | None = None  # breakeven-kind replay param
    trail_frac: float | None = None  # breakeven-kind replay param
    stop_atr_mult: float | None = None  # fill_anchored- + atr_bracket-kind replay param
    tp_atr_mult: float | None = None  # atr_bracket-kind replay param
    tp_floor_frac: float | None = None  # atr_bracket-kind replay param
    # atr_bracket-kind ENTRY ANCHOR (issue #1114): "planned" (all intended
    # tiers, what the live rail places against) or "realised" (tiers that
    # touched in the bar walk). Unlike the three params above this one has NO
    # module-level default -- an atr_bracket lens that leaves it None is
    # REFUSED at dispatch, because a what-if figure must name the policy it
    # replays. ``None`` on every other kind.
    anchor_mode: str | None = None
    preregistered_ref: str | None = None  # provenance (design-memo section), display-only


# ADR 0013 R4: the registry is bounded — at most this many concurrently
# registered lenses (one-in-one-out beyond it); every registered lens, retired
# ones included, counts toward the walk-forward multiplicity budget.
MAX_REGISTERED_LENSES = 5

# Adding another lens is a single entry here — the JSON-map column and the
# registry-driven selector absorb it with no schema or UI change. All current
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
    # Pre-registered trailing variant of be_0p5r (exit-geometry memo §7 row
    # "be@0.5R + trail0.6"): once the +0.5R trigger arms, the effective stop
    # trails at 0.6 of the peak gain instead of sitting flat at break-even.
    # Parameters were fixed in the memo BEFORE registration, so its forward
    # sample is a clean read — but it is still in_sample until that forward
    # N crosses the gate. Populates FORWARD-ONLY (frozen terminal rows keep
    # their stamped grid; PR #747).
    BreakevenLens(
        lens_id="be_0p5r_trail0p6",
        label="break-even +0.5R · trail 0.6",
        category="exit-stop",
        status="in_sample",
        kind="breakeven",
        mfe_trigger_r=0.5,
        trail_frac=0.6,
        preregistered_ref="exit_geometry_2026_06_30 s7 be0.5/trail0.6",
    ),
    # Pre-registered fixed ATR bracket (bezpazery v1, memo
    # docs/research/bezpazery_lens_design_2026_07_16.md §2 — source: the
    # betlejem5 EMM executor bracket, inspiration NOT replication): static stop
    # 1.5xATR below the blended entry, single 100% TP at min(52w-high ceiling,
    # max(cost floor +0.6%, +1.5xATR)). No day-flatten (deliberate deviation —
    # every lens shares the same bar window). Parameters were fixed in the memo
    # BEFORE registration, so its forward sample is a clean read — but it is
    # still in_sample until that forward N crosses the gate. Populates
    # FORWARD-ONLY (frozen terminal rows keep their stamped grid; PR #747).
    BreakevenLens(
        lens_id="atr_bracket_1p5",
        label="ATR bracket 1.5 (bezpazery)",
        category="exit-stop",
        status="in_sample",
        kind="atr_bracket",
        stop_atr_mult=1.5,
        tp_atr_mult=1.5,
        tp_floor_frac=0.006,
        anchor_mode="realised",
        preregistered_ref=(
            "betlejem5_comparative bezpazery v1 (bracket 1.5xATR, floor 0.6%, ceiling 52w-high)"
        ),
    ),
)


def _lens_realized_r(
    lens: BreakevenLens,
    trade_setup: Mapping[str, Any] | None,
    bars: Sequence[Mapping[str, Any]],
    *,
    pct_off_52w_high: float | None = None,
) -> float | None:
    """Dispatch one lens to its replay, returning realized R (or ``None``).

    ``pct_off_52w_high`` is the brief-row 52w-high distance (only the
    ``atr_bracket`` kind consumes it — as the reconstructed TP ceiling).

    Raises ``ValueError`` on an unregistered ``kind`` — a new lens kind must be a
    conscious two-step change (register + add a branch here), never a silent
    fall-through to the break-even path that would produce wrong R.
    """
    if lens.kind == "fill_anchored":
        mult = lens.stop_atr_mult if lens.stop_atr_mult is not None else _DEFAULT_STOP_ATR_MULT
        return realized_r_fill_anchored(trade_setup, bars, stop_atr_mult=mult)
    if lens.kind == "atr_bracket":
        # DELIBERATELY NOT the "X if X is not None else _DEFAULT_X" pattern the
        # other three params use (issue #1114). A default here would let a lens
        # measure an anchor nobody chose, which is the exact defect: the lens
        # replayed the realised-fill anchor while the live rail placed against
        # the planned blend, and nothing said so.
        if lens.anchor_mode is None:
            raise ValueError(f"atr_bracket lens {lens.lens_id!r} must declare an anchor_mode")
        return replay_ladder_atr_bracket(
            trade_setup,
            bars,
            anchor=lens.anchor_mode,  # type: ignore[arg-type]
            stop_atr_mult=(
                lens.stop_atr_mult
                if lens.stop_atr_mult is not None
                else _DEFAULT_BRACKET_STOP_ATR_MULT
            ),
            tp_atr_mult=(
                lens.tp_atr_mult if lens.tp_atr_mult is not None else _DEFAULT_BRACKET_TP_ATR_MULT
            ),
            tp_floor_frac=(
                lens.tp_floor_frac
                if lens.tp_floor_frac is not None
                else _DEFAULT_BRACKET_TP_FLOOR_FRAC
            ),
            ceiling_price=ceiling_from_52w_high(trade_setup, pct_off_52w_high),
        )
    if lens.kind == "breakeven":
        # MFE-triggered break-even / trailing. A missing trigger reduces to a static
        # disaster-stop walk (mfe_trigger_r=inf never arms it -> baseline parity).
        trigger = lens.mfe_trigger_r if lens.mfe_trigger_r is not None else float("inf")
        return replay_ladder_breakeven(
            trade_setup, bars, mfe_trigger_r=trigger, trail_frac=lens.trail_frac
        )
    raise ValueError(f"unknown exit-lens kind: {lens.kind!r}")


def breakeven_grid(
    trade_setup: Mapping[str, Any] | None,
    bars: Sequence[Mapping[str, Any]],
    *,
    pct_off_52w_high: float | None = None,
) -> dict[str, float | None]:
    """Realized R under each registered exit-stop lens, keyed by ``lens_id``.

    Re-replays the SAME bars under each lens's exit-stop policy (break-even,
    fill-anchored, or ATR bracket), reusing the pure replay engine. A lens that
    cannot resolve (unparseable setup / no bars / no fill / risk <= 0 / missing
    ATR for an ATR-parametrized lens / non-constructible bracket) maps to
    ``None``. ``pct_off_52w_high`` is the brief row's
    ``technical_pct_off_52w_high`` (the trade setup does not carry it) — the
    ``atr_bracket`` kind reconstructs its TP ceiling from it; ``None`` leaves
    that TP uncapped. Display-only; never feeds the headline ``realized_r``.
    """
    return {
        lens.lens_id: _lens_realized_r(lens, trade_setup, bars, pct_off_52w_high=pct_off_52w_high)
        for lens in BREAKEVEN_LENSES
    }
