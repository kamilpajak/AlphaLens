"""Net-cash replay of the two pre-registered exit policies (#1115, memo §10.1).

Governing document: ``docs/research/exit_policy_comparison_prereg_2026_08_24.md``
(LOCKED). Where a clause and this module disagree, the clause wins.

For one candidate, over the SAME cached RTH minute path, this computes the net
USD outcome at a common notional and a fixed horizon for an explicit,
defaultless ``arm``:

* **Arm A — brief ladder** (§5.2): the brief's ``tp_tranches`` in order, each
  selling its ``tranche_pct`` of the held position; the brief's disaster stop,
  static. Replayed by the PRODUCTION engine
  (:func:`alphalens_pipeline.feedback.ladder_replay.replay_ladder`) and
  rendered into cash — one engine, two renderings, so the fill convention
  cannot drift from the ``/edge`` store.
* **Arm B — live operational policy** (§5.2/§5.3): the planned-anchor ATR
  bracket with the #1112 step-3 take-profit clamp and the ``ReanchorOnFill``
  dynamic stop, all REUSED from the live composition
  (``resolve_exit_policy("atr_bracket_1p5")`` +
  :func:`broker_contract.exit_geometry.levels.clamp_reanchor_target` +
  :func:`alphalens_pipeline.paper.sizing.first_brief_tp_target`). When the
  bracket is not constructible the declared fallback is the classic per-tier
  bracket of ``brokers/execution.py`` — each filled tier exits at
  ``tp_tranches[min(tier_index, len-1)]`` with the shared disaster stop.

Economic scale (§5.4): the same gross notional both arms, split across intended
tiers by ``alloc_pct`` (share count per fill identical across arms); commission
and FX charged PER FILL via the declared constants of
``alphalens_pipeline.brokers.automanager.costs`` (a buy plus an equal-notional
sell sums exactly to ``round_trip_fee_bps``); slippage ``+S`` bps on buys,
``-S`` on sells; a position open at the horizon is MARKED at that session's
close — a mark is not a fill and carries no fee or slippage.

Known limitation, stated per §5.2/§11: the #1112 arm-time disarm gate
(``arms_inside_exit_region``) is NOT modelled. The measured contrast is the
exit-geometry contrast holding the entry rule fixed.

This module performs no file IO whatsoever — in particular it can never touch
the production ladder store (memo §10.1). Pure functions over the inputs.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from alphalens_pipeline.brokers.automanager.costs import (
    COMMISSION_RATE,
    FX_ROUND_TRIP_RATE,
    MIN_COMMISSION_USD,
)
from alphalens_pipeline.feedback.ladder_replay import (
    TIE_BREAK_SL_FIRST,
    LadderOutcome,
    parse_ladder,
    replay_ladder,
)
from alphalens_pipeline.paper.sizing import first_brief_tp_target, planned_blended_entry
from broker_contract.exit_geometry.levels import ceiling_from_52w_high, clamp_reanchor_target
from broker_contract.exit_geometry.registry import resolve_exit_policy

__status__ = "RESEARCH_ONLY"

ARM_A = "brief_tranches"
"""Arm A — the brief's staged take-profit ladder with its static disaster stop."""

ARM_B = "live_atr_bracket"
"""Arm B — the live planned-anchor ATR bracket policy with the step-3 clamp,
the reanchor-on-fill stop, and the declared per-tier-bracket fallback."""

Arm = Literal["brief_tranches", "live_atr_bracket"]

_ARMS: frozenset[str] = frozenset({ARM_A, ARM_B})

# The live behavioral policy arm B mirrors. Resolved from the registry so the
# bracket parameters (stop/tp ATR multiples, cost floor) and the reanchor
# semantics (decide_reanchor + min_stop_distance_frac) are the LIVE objects,
# never constants retyped here. Parity with build_exit_geometry_spec is pinned
# by a test that is a HALT tripwire during accrual (memo §11 item 4). Resolved
# once at import: the registry is static configuration, and the parity test
# breaks loudly if the resolved object ever drifts from the live composition.
_LIVE_POLICY = resolve_exit_policy("atr_bracket_1p5")

_BPS = 10_000.0


@dataclass(frozen=True)
class Levels:
    stop: float
    tp: float
    # Placement-time fact for §8.1 item 6: True when the 52w-high ceiling made
    # the FINAL tp (after the step-3 clamp) lower than it would have been with
    # no ceiling. A present-but-not-binding ceiling is False.
    ceiling_capped: bool = False


@dataclass(frozen=True)
class ArmOutcome:
    """One arm's net USD outcome for one candidate (memo §5.4)."""

    net_cash: float
    gross_cash: float
    total_fees: float
    chargeable_fills: int
    classification: str
    used_fallback: bool = False
    exit_levels: Levels | None = None
    first_fill_ts_ms: int | None = None
    exit_ts_ms: int | None = None
    mae_pct: float | None = None
    # USD paid on entry fills (slipped) — the absolute-MAE basis of §8.1
    # item 4: mae_usd = mae_pct * entry_cost. Full fill == the notional under
    # the share-proportional sizing.
    entry_cost: float = 0.0
    ceiling_capped: bool = False


# --------------------------------------------------------------------------
# §5.1 common feasibility — functions of the brief row and bar store alone.
# --------------------------------------------------------------------------


def infeasibility_reason(
    trade_setup: Mapping[str, Any] | None, *, bars_cover_window: bool
) -> str | None:
    """The §5.1 rule that excludes this candidate from BOTH arms, or ``None``.

    Evaluated before either policy runs, so neither arm can select the sample.
    Reasons, in rule order: ``setup_not_ok``, ``atr_missing_or_nonpositive``,
    ``no_tp_tranche``, ``bars_missing``.
    """
    ladder = parse_ladder(trade_setup)
    if not ladder.ok:
        return "setup_not_ok"
    if ladder.atr is None or not math.isfinite(ladder.atr) or ladder.atr <= 0:
        return "atr_missing_or_nonpositive"
    has_tp = any(math.isfinite(t.price) and t.price > 0 for t in ladder.tps)
    if not has_tp:
        return "no_tp_tranche"
    if not bars_cover_window:
        return "bars_missing"
    return None


# --------------------------------------------------------------------------
# Shared sizing arithmetic (§5.4): one notional, split by alloc_pct, so the
# share count on each fill is identical across arms.
# --------------------------------------------------------------------------


def _tier_quantities(trade_setup: Mapping[str, Any], *, notional: float) -> dict[str, float]:
    """Shares per entry tier id at the common notional.

    ``qty(E_i) = notional * w_i / sum_j(w_j * p_j)`` — shares PROPORTIONAL to
    the alloc weights, scaled so a full fill costs exactly ``notional``. Chosen
    over a per-tier dollar split deliberately: under this convention the
    position's average cost equals the alloc-weighted blend of the filled tier
    limits — the very quantity §5.2 declares as the "realised average fill"
    and the engine uses as ``blended_entry`` — so the arm-A cash rendering is
    an exact affine image of the engine's realized R (pinned by a test). A
    dollar split would make the cost basis the harmonic mean instead and
    silently detach the replay's cash from its own anchor definitions. Equal
    weights when allocs are absent.
    """
    ladder = parse_ladder(trade_setup)
    if not ladder.ok or not ladder.entries:
        return {}
    weights = [lvl.weight if ladder.total_entry_alloc > 0 else 1.0 for lvl in ladder.entries]
    denom = sum(w * lvl.price for w, lvl in zip(weights, ladder.entries, strict=True))
    if denom <= 0:
        return {}
    return {
        lvl.level_id: notional * w / denom for w, lvl in zip(weights, ladder.entries, strict=True)
    }


def filled_shares(
    trade_setup: Mapping[str, Any], entries_filled: Sequence[str], *, notional: float
) -> float:
    """Total shares held after the given tier ids filled, at the common notional."""
    quantities = _tier_quantities(trade_setup, notional=notional)
    return sum(quantities.get(level_id, 0.0) for level_id in entries_filled)


def planned_blend(trade_setup: Mapping[str, Any]) -> float | None:
    """The live rail's planned alloc-weighted blend over ALL intended tiers."""
    return planned_blended_entry(trade_setup)


def _per_fill_fee(fill_notional: float) -> float:
    """Commission + the FX leg for ONE fill (§5.4).

    A buy plus an equal-notional sell sums exactly to
    ``costs.round_trip_fee_bps``: per-fill commission is
    ``max(MIN_COMMISSION_USD, COMMISSION_RATE * n)`` and the round-trip FX rate
    splits evenly over its two conversions.
    """
    return max(MIN_COMMISSION_USD, COMMISSION_RATE * fill_notional) + (
        0.5 * FX_ROUND_TRIP_RATE * fill_notional
    )


# --------------------------------------------------------------------------
# Arm B geometry — the live composition, reused not retyped.
# --------------------------------------------------------------------------


def arm_b_initial_levels(
    trade_setup: Mapping[str, Any],
    *,
    pct_off_52w_high: float | None,
    anchor_blend: float | None = None,
    apply_clamp: bool = True,
) -> Levels | None:
    """The placement-time (stop, tp) of the live policy, WITH the step-3 clamp.

    ``None`` means the bracket is not constructible and arm B takes the §5.3
    fallback. Mirrors ``build_exit_geometry_spec`` exactly (pinned by the SMG
    parity test): bracket levels off the planned blend, then
    ``tp = max(tp, first_brief_tp_target)`` — the floor outranks the ceiling.
    """
    blended = anchor_blend if anchor_blend is not None else planned_blended_entry(trade_setup)
    if blended is None or not math.isfinite(blended) or blended <= 0:
        return None
    ladder = parse_ladder(trade_setup)
    atr = ladder.atr
    if atr is None or not math.isfinite(atr) or atr <= 0:
        return None
    ceiling = ceiling_from_52w_high(trade_setup, pct_off_52w_high)
    levels = _LIVE_POLICY.decide_placement_geometry(blended, atr, ceiling_price=ceiling)
    if levels is None:
        return None
    stop, tp = levels
    uncapped = _LIVE_POLICY.decide_placement_geometry(blended, atr, ceiling_price=None)
    tp_uncapped = uncapped[1] if uncapped is not None else tp
    if apply_clamp:
        first_target = first_brief_tp_target(trade_setup)
        if first_target is not None:
            tp = max(tp, first_target)
            tp_uncapped = max(tp_uncapped, first_target)
    return Levels(stop=stop, tp=tp, ceiling_capped=tp < tp_uncapped)


def arm_b_reanchored_stop(
    fill_blend: float, atr: float, *, brief_disaster_stop: float
) -> float | None:
    """The reanchor-on-fill stop target, exactly as the live arm composes it.

    ``policy.decide_reanchor(fill_blend, atr)`` clamped by
    ``clamp_reanchor_target`` against the BRIEF disaster floor with the
    policy's own ``min_stop_distance_frac`` — the same call shape as
    ``position_manager._maybe_reanchor``. ``None`` = do not re-anchor.
    """
    proposed = _LIVE_POLICY.decide_reanchor(fill_blend, atr)
    if proposed is None:
        return None
    return clamp_reanchor_target(
        brief_disaster_stop,
        proposed,
        anchor_price=fill_blend,
        min_distance_frac=_LIVE_POLICY.min_stop_distance_frac,
    )


# --------------------------------------------------------------------------
# Cash rendering of a production-engine outcome (arm A + the arm-B fallback).
# --------------------------------------------------------------------------


def _tranche_sold_shares(
    ladder_tps: list[Any], hit_tp_ids: set[str], filled_frac: float
) -> tuple[list[tuple[str, float, float]], float]:
    """Per-tranche (tp_id, price, share_of_filled) using the ENGINE's re-basing.

    Byte-for-byte the ``_realized_r_with_frac`` share math: tranche weight
    normalised over the tp weight sum, re-based by the filled fraction, capped
    so cumulative shares never exceed 1. Returns the sold list and the residual
    share.
    """
    tp_wsum = sum(t.weight for t in ladder_tps)
    sold: list[tuple[str, float, float]] = []
    cumulative = 0.0
    for t in ladder_tps:
        full_share = (t.weight / tp_wsum) if tp_wsum > 0 else 1.0 / max(len(ladder_tps), 1)
        share = full_share / filled_frac if filled_frac > 0 else full_share
        share = min(share, 1.0 - cumulative)
        if share <= 0:
            continue  # cumulative already 1.0; later tranches contribute nothing (engine pattern)
        if t.level_id in hit_tp_ids:
            sold.append((t.level_id, t.price, share))
            cumulative += share
    return sold, 1.0 - cumulative


def _cash_from_engine_outcome(
    trade_setup: Mapping[str, Any],
    outcome: LadderOutcome,
    *,
    notional: float,
    slippage_bps: float,
    charge_fees: bool,
    last_close: float | None,
    used_fallback: bool = False,
    exit_levels: Levels | None = None,
) -> ArmOutcome:
    """Render one ``replay_ladder`` outcome into net USD at the common notional."""
    slip = slippage_bps / _BPS
    ladder = parse_ladder(trade_setup)
    quantities = _tier_quantities(trade_setup, notional=notional)

    if not outcome.entries_filled:
        return ArmOutcome(
            net_cash=0.0,
            gross_cash=0.0,
            total_fees=0.0,
            chargeable_fills=0,
            classification=outcome.classification,
            used_fallback=used_fallback,
            exit_levels=exit_levels,
        )

    gross = 0.0
    fees = 0.0
    fills = 0
    entry_cost = 0.0
    first_fill_ts: int | None = None
    exit_ts: int | None = None

    by_id = {lvl.level_id: lvl for lvl in ladder.entries}
    for crossing in outcome.sequence:
        if crossing.kind == "ENTRY" and crossing.level_id in outcome.entries_filled:
            lvl = by_id[crossing.level_id]
            qty = quantities.get(lvl.level_id, 0.0)
            if qty <= 0:
                continue  # execution.py skips zero-qty tiers; no phantom $1 minimum
            price = lvl.price * (1.0 + slip)
            gross -= qty * price
            entry_cost += qty * price
            fees += _per_fill_fee(qty * price)
            fills += 1
            if first_fill_ts is None:
                first_fill_ts = crossing.bar_ts_ms
        elif crossing.kind in ("SL", "TP", "TIME_STOP") and exit_ts is None:
            exit_ts = crossing.bar_ts_ms

    shares = filled_shares(trade_setup, outcome.entries_filled, notional=notional)
    filled_frac = outcome.filled_fraction or 0.0
    sold, residual = _tranche_sold_shares(ladder.tps, set(outcome.tps_hit), filled_frac)
    for _tp_id, price, share in sold:
        qty = share * shares
        if qty <= 0:
            continue
        eff = price * (1.0 - slip)
        gross += qty * eff
        fees += _per_fill_fee(qty * eff)
        fills += 1

    if residual > 1e-12:
        qty = residual * shares
        if outcome.sl_hit:
            assert ladder.disaster_stop is not None
            eff = ladder.disaster_stop * (1.0 - slip)
            gross += qty * eff
            fees += _per_fill_fee(qty * eff)
            fills += 1
        else:
            mark = _mark_price(outcome, last_close)
            if mark is not None:
                # A mark is a valuation, not a fill: no fee, no slippage (§5.4).
                gross += qty * mark

    net = gross - fees if charge_fees else gross
    return ArmOutcome(
        net_cash=net,
        gross_cash=gross,
        total_fees=fees if charge_fees else 0.0,
        chargeable_fills=fills,
        classification=outcome.classification,
        used_fallback=used_fallback,
        exit_levels=exit_levels,
        first_fill_ts_ms=first_fill_ts,
        exit_ts_ms=exit_ts,
        mae_pct=outcome.mae_pct,
        entry_cost=entry_cost,
    )


def _last_close(bars: Sequence[Mapping[str, Any]]) -> float | None:
    """Close of the time-latest bar — the engine's own OPEN-remainder mark."""
    if not bars:
        return None
    return float(max(bars, key=lambda b: int(b["t"]))["c"])


def _mark_price(outcome: LadderOutcome, last_close: float | None) -> float | None:
    """The §5.4 mark for an open remainder: the time-stop bar's close when the
    horizon fired, else the last close of the replayed path (the engine marks
    an OPEN remainder to its last close in exactly the same way)."""
    for crossing in outcome.sequence:
        if crossing.kind == "TIME_STOP":
            return crossing.price
    return last_close


# --------------------------------------------------------------------------
# Arm B main walk — single 100% tranche, DYNAMIC stop. Mirrors the engine's
# bar conventions (fill at limit on bar_low <= limit, TP on bar_high, SL on
# bar_low, SL-first on ambiguity, time-stop marks the cutoff bar's close).
# --------------------------------------------------------------------------


def _arm_b_bracket_walk(
    trade_setup: Mapping[str, Any],
    bars: Sequence[Mapping[str, Any]],
    levels: Levels,
    *,
    notional: float,
    slippage_bps: float,
    charge_fees: bool,
    entry_expiry_ms: int | None,
    position_expiry_ms: int | None,
    reanchor: bool = True,
) -> ArmOutcome:
    slip = slippage_bps / _BPS
    ladder = parse_ladder(trade_setup)
    assert ladder.ok and ladder.disaster_stop is not None and ladder.atr is not None
    quantities = _tier_quantities(trade_setup, notional=notional)

    stop = levels.stop
    tp = levels.tp
    filled: list[Any] = []
    filled_ids: set[str] = set()
    gross = 0.0
    fees = 0.0
    fills = 0
    entry_cost = 0.0
    first_fill_ts: int | None = None
    exit_ts: int | None = None
    classification = "NO_FILL"
    in_trade_low: float | None = None
    fill_blend_slipped: float | None = None

    ordered = sorted(bars, key=lambda b: int(b["t"]))
    assert TIE_BREAK_SL_FIRST == "sl_first"  # convention documented at the engine

    for bar in ordered:
        ts, low, high, close = int(bar["t"]), float(bar["l"]), float(bar["h"]), float(bar["c"])

        newly_filled = False
        for lvl in ladder.entries:
            if entry_expiry_ms is not None and ts >= entry_expiry_ms:
                break
            if lvl.level_id not in filled_ids and low <= lvl.price:
                filled.append(lvl)
                filled_ids.add(lvl.level_id)
                qty = quantities.get(lvl.level_id, 0.0)
                if qty > 0:  # execution.py skips zero-qty tiers
                    price = lvl.price * (1.0 + slip)
                    gross -= qty * price
                    entry_cost += qty * price
                    fees += _per_fill_fee(qty * price)
                    fills += 1
                newly_filled = True
                if first_fill_ts is None:
                    first_fill_ts = ts

        if not filled:
            continue

        if newly_filled and reanchor:
            # ReanchorOnFill (§5.2): the realised average fill in the replay is
            # the alloc-weighted blend of the tier LIMITS that filled, plus the
            # declared entry slippage. Re-fires on every NEW blend, mirroring
            # the live per-avg_price latch.
            wsum = sum(lvl.weight for lvl in filled)
            blend = (
                sum(lvl.price * lvl.weight for lvl in filled) / wsum
                if wsum > 0
                else sum(lvl.price for lvl in filled) / len(filled)
            )
            fill_blend_slipped = blend * (1.0 + slip)
            reanchored = arm_b_reanchored_stop(
                fill_blend_slipped, ladder.atr, brief_disaster_stop=ladder.disaster_stop
            )
            if reanchored is not None:
                stop = reanchored

        in_trade_low = low if in_trade_low is None else min(in_trade_low, low)

        held = filled_shares(trade_setup, tuple(filled_ids), notional=notional)
        if held <= 0:
            continue
        if low <= stop:
            # SL-first on the same-bar ambiguity, as at the engine.
            eff = stop * (1.0 - slip)
            gross += held * eff
            fees += _per_fill_fee(held * eff)
            fills += 1
            classification = "SL_HIT"
            exit_ts = ts
            break
        if high >= tp:
            eff = tp * (1.0 - slip)
            gross += held * eff
            fees += _per_fill_fee(held * eff)
            fills += 1
            classification = "TP_FULL"
            exit_ts = ts
            break
        if position_expiry_ms is not None and ts >= position_expiry_ms:
            gross += held * close  # mark, not a fill (§5.4)
            classification = "TIME_STOP"
            exit_ts = ts
            break
    else:
        if filled and classification == "NO_FILL":
            # Bars ended before any exit event: the position is OPEN and the
            # remainder is MARKED at the last close (engine semantics; the
            # driver's §5.1 rule 4 makes this unreachable in the primary, but
            # the variant paths and ad-hoc calls must not report NO_FILL cash
            # while holding shares).
            held = filled_shares(trade_setup, tuple(filled_ids), notional=notional)
            last_close = _last_close(ordered)
            if held > 0 and last_close is not None:
                gross += held * last_close
            classification = "OPEN"

    mae_pct: float | None = None
    if fill_blend_slipped is not None and in_trade_low is not None and fill_blend_slipped > 0:
        mae_pct = (in_trade_low - fill_blend_slipped) / fill_blend_slipped

    net = gross - fees if charge_fees else gross
    return ArmOutcome(
        net_cash=net if filled else 0.0,
        gross_cash=gross if filled else 0.0,
        total_fees=(fees if charge_fees else 0.0) if filled else 0.0,
        chargeable_fills=fills,
        classification=classification,
        exit_levels=Levels(stop=stop, tp=tp, ceiling_capped=levels.ceiling_capped),
        first_fill_ts_ms=first_fill_ts,
        exit_ts_ms=exit_ts,
        mae_pct=mae_pct,
        entry_cost=entry_cost if filled else 0.0,
        ceiling_capped=levels.ceiling_capped,
    )


def _arm_b_fallback(
    trade_setup: Mapping[str, Any],
    bars: Sequence[Mapping[str, Any]],
    *,
    notional: float,
    slippage_bps: float,
    charge_fees: bool,
    entry_expiry_ms: int | None,
    position_expiry_ms: int | None,
) -> ArmOutcome:
    """§5.3: the classic per-tier bracket — tier i exits 100% of ITS shares at
    ``tp_tranches[min(i, len-1)]`` with the shared disaster stop. Each tier is
    an independent single-tranche ladder replayed by the production engine."""
    ladder = parse_ladder(trade_setup)
    assert ladder.ok and ladder.disaster_stop is not None
    raw_entries = list(trade_setup.get("entry_tiers") or [])
    raw_tps = list(trade_setup.get("tp_tranches") or [])

    quantities = _tier_quantities(trade_setup, notional=notional)
    gross = 0.0
    fees = 0.0
    fills = 0
    entry_cost = 0.0
    first_fill_ts: int | None = None
    exit_ts: int | None = None
    classifications: list[str] = []

    for index, tier in enumerate(raw_entries):
        tranche = raw_tps[min(index, len(raw_tps) - 1)] if raw_tps else None
        sub_setup = dict(trade_setup)
        sub_setup["entry_tiers"] = [dict(tier, alloc_pct=100.0)]
        sub_setup["tp_tranches"] = [dict(tranche, tranche_pct=100.0)] if tranche else []
        # The tier keeps EXACTLY its main-convention share count: sub notional
        # = qty_i * limit_i, so the fallback and the bracket path size fills
        # identically (§5.4 "the share count on each fill is the same").
        tier_qty = quantities.get(f"E{index + 1}", 0.0)
        sub = _cash_from_engine_outcome(
            sub_setup,
            replay_ladder(
                sub_setup,
                bars,
                entry_expiry_ms=entry_expiry_ms,
                position_expiry_ms=position_expiry_ms,
            ),
            notional=tier_qty * float(tier["limit"]),
            slippage_bps=slippage_bps,
            charge_fees=charge_fees,
            last_close=_last_close(bars),
        )
        gross += sub.gross_cash
        fees += sub.total_fees
        fills += sub.chargeable_fills
        entry_cost += sub.entry_cost
        classifications.append(sub.classification)
        if sub.first_fill_ts_ms is not None:
            first_fill_ts = (
                sub.first_fill_ts_ms
                if first_fill_ts is None
                else min(first_fill_ts, sub.first_fill_ts_ms)
            )
        if sub.exit_ts_ms is not None:
            exit_ts = sub.exit_ts_ms if exit_ts is None else max(exit_ts, sub.exit_ts_ms)

    classification = (
        "NO_FILL"
        if all(c == "NO_FILL" for c in classifications)
        else "+".join(sorted({c for c in classifications if c != "NO_FILL"}))
    )
    return ArmOutcome(
        net_cash=gross - fees if charge_fees else gross,
        gross_cash=gross,
        total_fees=fees if charge_fees else 0.0,
        chargeable_fills=fills,
        classification=classification,
        used_fallback=True,
        first_fill_ts_ms=first_fill_ts,
        exit_ts_ms=exit_ts,
        entry_cost=entry_cost,
    )


# --------------------------------------------------------------------------
# The entry point.
# --------------------------------------------------------------------------


def replay_arm(
    trade_setup: Mapping[str, Any],
    bars: Sequence[Mapping[str, Any]],
    *,
    arm: Arm,
    notional: float,
    slippage_bps: float,
    position_expiry_ms: int | None,
    entry_expiry_ms: int | None = None,
    pct_off_52w_high: float | None = None,
    charge_fees: bool = True,
    arm_b_anchor: Literal["planned", "realised"] = "planned",
    arm_b_apply_clamp: bool = True,
    arm_b_reanchor: bool = True,
) -> ArmOutcome:
    """Replay ONE candidate under ONE policy arm, in net USD (memo §10.1).

    ``arm`` is explicit and has no default — the same discipline #1114 imposed
    on the lens anchor. Feasibility (§5.1) is the caller's gate; this function
    refuses a setup ``parse_ladder`` rejects rather than guessing.
    """
    if arm not in _ARMS:
        raise ValueError(f"unknown arm {arm!r}; expected one of {sorted(_ARMS)}")
    ladder = parse_ladder(trade_setup)
    if not ladder.ok:
        raise ValueError("trade_setup failed parse_ladder — run infeasibility_reason first")

    if arm == ARM_A:
        outcome = replay_ladder(
            trade_setup,
            bars,
            entry_expiry_ms=entry_expiry_ms,
            position_expiry_ms=position_expiry_ms,
        )
        return _cash_from_engine_outcome(
            trade_setup,
            outcome,
            notional=notional,
            slippage_bps=slippage_bps,
            charge_fees=charge_fees,
            last_close=_last_close(bars),
        )

    # §8.3 variants (analysis-script sensitivities ONLY — the §5.2 live arm B
    # is the default): "realised" anchors the bracket on the blend of the
    # tiers that TOUCH in a pre-walk (the lens's two-walk trick, under the
    # same cutoffs); apply_clamp=False drops the step-3 tp floor;
    # reanchor=False freezes the stop at placement (the registered
    # atr_bracket_1p5_planned lens geometry when both are off).
    anchor_blend: float | None = None
    if arm_b_anchor == "realised":
        pre_walk = replay_ladder(
            trade_setup,
            bars,
            entry_expiry_ms=entry_expiry_ms,
            position_expiry_ms=position_expiry_ms,
        )
        if not pre_walk.entries_filled or pre_walk.blended_entry is None:
            return ArmOutcome(
                net_cash=0.0,
                gross_cash=0.0,
                total_fees=0.0,
                chargeable_fills=0,
                classification="NO_FILL",
            )
        anchor_blend = pre_walk.blended_entry

    levels = arm_b_initial_levels(
        trade_setup,
        pct_off_52w_high=pct_off_52w_high,
        anchor_blend=anchor_blend,
        apply_clamp=arm_b_apply_clamp,
    )
    if levels is None:
        return _arm_b_fallback(
            trade_setup,
            bars,
            notional=notional,
            slippage_bps=slippage_bps,
            charge_fees=charge_fees,
            entry_expiry_ms=entry_expiry_ms,
            position_expiry_ms=position_expiry_ms,
        )
    return _arm_b_bracket_walk(
        trade_setup,
        bars,
        levels,
        notional=notional,
        slippage_bps=slippage_bps,
        charge_fees=charge_fees,
        entry_expiry_ms=entry_expiry_ms,
        position_expiry_ms=position_expiry_ms,
        reanchor=arm_b_reanchor,
    )
