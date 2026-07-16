"""Broker-free ladder-outcome replay.

For a matured candidate we already know its deterministic trade setup (entry
tiers E1/E2/E3, take-profit tranches TP1/TP2/TP3, one disaster stop SL) and we
can fetch the intraday price path (Polygon minute bars) over the hold horizon.
This module REPLAYS that path against the ladder and records, in order, which
levels the price crossed -- a clean "did the setup work, and how" feedback
signal that needs NO broker, NO resting orders, NO always-on process.

Fill model (the only modelling assumption): a level is "executed at that level"
the first time price touches it -- entry on ``low <= limit`` (a dip), TP on
``high >= target`` (a rally), SL on ``low <= disaster_stop``. This is the exact
price the strategy's geometry assumes (resting-limit price-improvement), so the
realized R it produces is the clean, slippage-free number -- which is precisely
why a price-path replay is a BETTER feedback source than a real (or paper)
broker, whose market-order fills would smear the geometry.

Three measurement layers (design memo §5.0), in priority order:

1. **Substrate (policy-free):** the ordered crossing sequence, plus MFE / MAE
   (max favourable / adverse excursion, measured over IN-TRADE bars only and
   anchored to the blended entry) and ``forward_return`` (the close-to-close
   move over the horizon, independent of any fill). Re-derivable later without
   touching Polygon again.
2. **As-specified (headline):** the ladder replayed EXACTLY as ``brief_trade_setup``
   specifies it -- static stop, fixed TP targets. ``classification`` +
   ``blended_entry`` + ``realized_r``. The only number that maps 1:1 to what the
   tool emits.
3. **Ratchet what-if (optional, namespaced ``ratchet_realized_r``):** the same
   path under a break-even-after-TP1 / lock-in-after-TP2 ratcheting stop. NEVER
   overrides the layer-2 headline -- we do not ship ratchet management, so a
   ratchet P&L would flatter / penalise the tool for a policy it never executes.

Pure + deterministic: this module imports nothing from the store / Polygon /
broker. It takes a parsed trade-setup dict + a list of OHLC bars and returns a
:class:`LadderOutcome`. Brief enumeration, the Polygon fetch and the parquet
write live in the caller (``population_ladder_monitor``).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

# Within one bar we cannot order an SL touch vs a TP touch (minute granularity
# hides intra-bar sequence). We resolve it CONSERVATIVELY -- assume the adverse
# (SL) leg happened first -- and flag the bar so the bias is auditable. Finer
# resolution would need second/tick aggregates (still broker-free), not a live
# feed.
TIE_BREAK_SL_FIRST = "sl_first"

_ENTRY = "ENTRY"
_TP = "TP"
_SL = "SL"
_TIME = "TIME_STOP"


@dataclass(frozen=True)
class LevelCrossing:
    """One level touched by the price path, recorded in crossing order."""

    level_id: str  # "E1".."E3", "TP1".."TP3", "SL"
    kind: str  # _ENTRY | _TP | _SL
    price: float  # the level price (touch = fill-at-level)
    bar_ts_ms: int  # bar-start epoch ms when the touch occurred
    same_bar_ambiguous: bool = False  # SL and a TP both crossable in this bar


@dataclass(frozen=True)
class LadderOutcome:
    status: str  # "OK" | "NO_STRUCTURE" | "NO_DATA"
    sequence: tuple[LevelCrossing, ...] = ()
    entries_filled: tuple[str, ...] = ()
    tps_hit: tuple[str, ...] = ()
    sl_hit: bool = False
    classification: str = "NO_FILL"
    blended_entry: float | None = None
    realized_r: float | None = None
    # Fraction of the FULL intended position that actually filled (alloc-weighted,
    # bounded (0, 1]). ``None`` when nothing filled. Exposed so the population
    # monitor can derive the realized gross weight (= suggested_size × this) WITHOUT
    # re-implementing the alloc-weighting logic. Pure geometry — NOT a size field.
    filled_fraction: float | None = None
    horizon_open: bool = False  # position still open at the last bar
    ambiguous_bars: int = 0
    # Substrate (layer 1) -- policy-free path statistics.
    mfe: float | None = None  # max favourable excursion in R (in-trade, vs blended)
    mae: float | None = None  # max adverse excursion in R (in-trade, vs blended)
    mfe_pct: float | None = None  # same, as a fraction of blended entry
    mae_pct: float | None = None
    forward_return: float | None = None  # (last close - reference_close)/reference_close
    # Ratchet what-if (layer 3) -- never overrides realized_r.
    ratchet_realized_r: float | None = None

    def sequence_str(self) -> str:
        """Compact human form, e.g. ``E1->E2->TP1->SL``."""
        return "->".join(c.level_id for c in self.sequence)


@dataclass
class _Level:
    level_id: str
    price: float
    weight: float  # alloc_pct (entries) or tranche_pct (TPs)


@dataclass
class _ParsedLadder:
    ok: bool
    entries: list[_Level] = field(default_factory=list)  # descending price (E1>E2>E3)
    tps: list[_Level] = field(default_factory=list)  # ascending price (TP1<TP2<TP3)
    disaster_stop: float | None = None
    total_entry_alloc: float = 0.0  # sum of entry alloc_pct over ALL intended tiers
    atr: float | None = None  # ATR from the setup, if present (ratchet runner is OUT for now)


def parse_ladder(trade_setup: Mapping[str, Any] | None) -> _ParsedLadder:
    """Pull the levels out of a ``brief_trade_setup`` dict.

    Returns ok=False (-> NO_STRUCTURE) when the setup is absent, not "OK",
    missing a disaster stop, or has no entry tiers -- the same conditions under
    which the live exit_manager falls back to no structured ladder.
    """
    if not trade_setup:
        return _ParsedLadder(ok=False)
    if trade_setup.get("status") != "OK":
        return _ParsedLadder(ok=False)
    stop = trade_setup.get("disaster_stop")
    raw_entries = trade_setup.get("entry_tiers") or []
    if stop is None or not raw_entries:
        return _ParsedLadder(ok=False)

    entries = [
        _Level(level_id=f"E{i + 1}", price=float(t["limit"]), weight=float(t.get("alloc_pct", 0.0)))
        for i, t in enumerate(raw_entries)
    ]
    raw_tps = trade_setup.get("tp_tranches") or []
    tps = [
        _Level(
            level_id=f"TP{i + 1}", price=float(t["target"]), weight=float(t.get("tranche_pct", 0.0))
        )
        for i, t in enumerate(raw_tps)
    ]
    total_entry_alloc = sum(lvl.weight for lvl in entries)
    raw_atr = trade_setup.get("atr")
    try:
        atr = float(raw_atr) if raw_atr is not None else None
    except (ValueError, TypeError):
        atr = None
    # Guard: a malformed disaster_stop (non-numeric string, wrong type) must not raise;
    # treat it as a missing stop -> not a valid ladder.
    try:
        stop_f = float(stop)
    except (ValueError, TypeError):
        return _ParsedLadder(ok=False)
    return _ParsedLadder(
        ok=True,
        entries=entries,
        tps=tps,
        disaster_stop=stop_f,
        total_entry_alloc=total_entry_alloc,
        atr=atr,
    )


def _bar_lhc(bar: Mapping[str, Any]) -> tuple[int, float, float, float]:
    return int(bar["t"]), float(bar["l"]), float(bar["h"]), float(bar["c"])


def _blended_entry(filled: list[_Level]) -> float:
    """Qty-weighted blended entry over FILLED tiers (alloc_pct weights).

    Equal-weight fallback when allocs are absent / zero.
    """
    wsum = sum(lvl.weight for lvl in filled)
    if wsum > 0:
        return sum(lvl.price * lvl.weight for lvl in filled) / wsum
    return sum(lvl.price for lvl in filled) / len(filled)


def _filled_frac(ladder: _ParsedLadder, filled: list[_Level]) -> float:
    """Fraction of the FULL intended position that actually filled.

    ``= sum(alloc_pct of filled tiers) / sum(alloc_pct of ALL intended tiers)``.
    Falls back to ``len(filled)/len(entries)`` when allocs are absent / zero so a
    setup without alloc weights still re-bases TP shares sensibly. Bounded to
    (0, 1].
    """
    total = ladder.total_entry_alloc
    if total > 0:
        frac = sum(lvl.weight for lvl in filled) / total
    elif ladder.entries:
        frac = len(filled) / len(ladder.entries)
    else:
        frac = 1.0
    return min(max(frac, 0.0), 1.0)


def replay_ladder(
    trade_setup: Mapping[str, Any] | None,
    bars: Sequence[Mapping[str, Any]],
    *,
    reference_close: float | None = None,
    entry_expiry_ms: int | None = None,
    position_expiry_ms: int | None = None,
) -> LadderOutcome:
    """Replay an OHLC bar list against the ladder.

    bars: dicts with at least ``t`` (epoch ms), ``l``, ``h``, ``c``. They are
    sorted ascending by ``t`` defensively (bug #4) so the crossing sequence is
    correct even if the source returns them out of order.

    Single full-horizon pass: the walk records the as-specified exit marker (the
    first SL or the full-TP scale-out) but does NOT return early -- it keeps
    iterating so the MFE / MAE substrate covers the WHOLE in-trade window, not
    just the bars up to the exit. ``reference_close`` anchors ``forward_return``
    (computed independently of any fill); ``None`` leaves it ``None``.

    Time-awareness (PR-1): both cutoffs are ABSOLUTE epoch-ms scalars (the
    weeks/sessions -> ms conversion belongs to the future driver, NOT this pure
    engine). Both default ``None`` -> byte-identical legacy behaviour.

    * ``entry_expiry_ms`` -- an entry tier fills only when its limit is touched
      on a bar with ``ts < entry_expiry_ms``. A limit touched at/after the cutoff
      does NOT fill (stale entry-TTL). It only blocks NEW fills; TP/SL resolution
      on an already-open position continues. If nothing filled before the cutoff
      the existing NO_FILL path applies.
    * ``position_expiry_ms`` -- if an OPEN position has not exited via SL/TP by
      the FIRST bar with ``ts >= position_expiry_ms``, the remainder is
      time-stopped and marked to THAT bar's close (terminal ``TIME_STOP``). A
      real SL/TP firing on the same bar as the cutoff WINS over the synthetic
      time-stop (resolved before the time-stop check).
    """
    ladder = parse_ladder(trade_setup)
    if not ladder.ok:
        return LadderOutcome(status="NO_STRUCTURE")
    if not bars:
        return LadderOutcome(status="NO_DATA")

    # Bug #4: sort ascending by timestamp defensively. Polygon returns asc, but a
    # mis-ordered input would corrupt the crossing sequence + MFE/MAE windows.
    ordered = sorted(bars, key=lambda b: int(b["t"]))

    stop = ladder.disaster_stop
    assert stop is not None  # ok=True guarantees it

    walk = _LadderWalk(
        ladder, stop, entry_expiry_ms=entry_expiry_ms, position_expiry_ms=position_expiry_ms
    )
    for bar in ordered:
        walk.step(bar)

    blended = _blended_entry(walk.filled) if walk.filled else None
    ratchet_r: float | None = None
    if walk.filled and blended is not None:
        risk_per_share = (blended - stop) if blended else 0.0
        ratchet_r = _replay_ratchet(ladder, ordered, walk.filled, blended, risk_per_share)

    return _finalize(walk, blended=blended, reference_close=reference_close, ratchet_r=ratchet_r)


# The fixed EXIT-policy grid (PR-2). Each config re-replays the SAME bars under a
# different take-profit ladder while holding the candidate (price path), the entry
# tiers, and the disaster stop FIXED -- so a difference in realized R is
# attributable to trade-management, not to the pick. The entry-side counterfactual
# (full-fill blended entry) is a SEPARATE measure (see realized_r_full_fill).
GRID_CONFIGS: tuple[str, ...] = ("single_tp_first", "single_tp_last", "no_tp_ride")


def _with_tp_tranches(
    trade_setup: Mapping[str, Any], tps: list[Mapping[str, Any]]
) -> dict[str, Any]:
    """Shallow-copy the setup with its TP ladder replaced (entries/stop untouched).

    Swaps the ``tp_tranches`` key with a NEW list; never mutates the original
    setup or its nested entry/tranche dicts. Callers pass freshly-built tranche
    dicts, so the shallow copy depth is sufficient.
    """
    swapped = dict(trade_setup)
    swapped["tp_tranches"] = tps
    return swapped


def _grid_realized_r(
    trade_setup: Mapping[str, Any],
    bars: Sequence[Mapping[str, Any]],
    *,
    entry_expiry_ms: int | None,
    position_expiry_ms: int | None,
) -> float | None:
    """Terminal realized R of one alternate-exit replay (``None`` if it never
    exited / could not be built). ``reference_close`` is irrelevant here -- the
    grid measures realized R (fill-anchored), not the substrate forward return."""
    return replay_ladder(
        trade_setup,
        bars,
        entry_expiry_ms=entry_expiry_ms,
        position_expiry_ms=position_expiry_ms,
    ).realized_r


def replay_ladder_grid(
    trade_setup: Mapping[str, Any] | None,
    bars: Sequence[Mapping[str, Any]],
    *,
    reference_close: float | None = None,
    entry_expiry_ms: int | None = None,
    position_expiry_ms: int | None = None,
) -> dict[str, float | None]:
    """Re-replay the SAME cached bars under a fixed grid of alternate EXIT ladders.

    Returns ``{config_name: realized_r}`` for each entry in :data:`GRID_CONFIGS`.
    This is the within-decision lever that separates ladder-capture quality from
    selection quality: the bars, entry tiers, and stop are identical across the
    grid, so the spread in realized R is the trade-management effect. It reuses
    the pure :func:`replay_ladder` over the already-fetched bars (the same
    zero-extra-cost pattern as the ratchet what-if pass), so there is no new walk
    logic and no new Polygon call.

    * ``single_tp_first`` -- collapse the TP ladder to a single 100% tranche at
      the NEAREST target (bank everything at TP1).
    * ``single_tp_last`` -- a single 100% tranche at the FARTHEST target (hold for
      the top target only).
    * ``no_tp_ride`` -- drop all take-profits; ride to the stop or the position
      time-stop (no profit-taking).

    A config that cannot be built (no TP tranches for the single-TP variants, an
    unparseable setup, or no bars) maps to ``None``.

    ``reference_close`` is intentionally unused -- the grid measures realized R
    (fill-anchored), not the substrate forward return, which is the only thing the
    arrival anchor feeds. It is kept only for call-signature symmetry with
    :func:`replay_ladder`.
    """
    none_grid: dict[str, float | None] = dict.fromkeys(GRID_CONFIGS, None)
    if trade_setup is None or not bars or not parse_ladder(trade_setup).ok:
        return none_grid

    raw_tps = list(trade_setup.get("tp_tranches") or [])
    grid = dict(none_grid)
    # next(iter(...), None) / next(reversed(...), None) instead of [0] / [-1]: no
    # subscript means no IndexError to reason about (static analysis cannot narrow
    # the `if raw_tps` truthiness guard on its own).
    first_tp = next(iter(raw_tps), None)
    last_tp = next(reversed(raw_tps), None)
    if first_tp is not None and last_tp is not None:
        grid["single_tp_first"] = _grid_realized_r(
            _with_tp_tranches(trade_setup, [{**first_tp, "tranche_pct": 100.0}]),
            bars,
            entry_expiry_ms=entry_expiry_ms,
            position_expiry_ms=position_expiry_ms,
        )
        grid["single_tp_last"] = _grid_realized_r(
            _with_tp_tranches(trade_setup, [{**last_tp, "tranche_pct": 100.0}]),
            bars,
            entry_expiry_ms=entry_expiry_ms,
            position_expiry_ms=position_expiry_ms,
        )
    grid["no_tp_ride"] = _grid_realized_r(
        _with_tp_tranches(trade_setup, []),
        bars,
        entry_expiry_ms=entry_expiry_ms,
        position_expiry_ms=position_expiry_ms,
    )
    return grid


def _with_entry_tiers(
    trade_setup: Mapping[str, Any], entries: list[Mapping[str, Any]]
) -> dict[str, Any]:
    """Shallow-copy the setup with its entry ladder replaced (tps/stop untouched).

    Mirror of :func:`_with_tp_tranches`: swaps the ``entry_tiers`` key with a NEW
    list of freshly-built tier dicts; never mutates the original setup.
    """
    swapped = dict(trade_setup)
    swapped["entry_tiers"] = entries
    return swapped


def _with_disaster_stop(trade_setup: Mapping[str, Any], stop: float) -> dict[str, Any]:
    """Shallow-copy the setup with its disaster stop replaced (entries/tps untouched).

    Mirror of :func:`_with_tp_tranches` / :func:`_with_entry_tiers`: swaps ONLY
    the ``disaster_stop`` key; never mutates the source dict and leaves
    ``entry_tiers`` / ``tp_tranches`` (and all other keys) intact as the same
    objects (shallow copy depth is sufficient because this function does not
    create new nested structures).
    """
    swapped = dict(trade_setup)
    swapped["disaster_stop"] = stop
    return swapped


def _replay_synthetic_fill(
    trade_setup: Mapping[str, Any],
    bars: Sequence[Mapping[str, Any]],
    *,
    fill_price: float,
    fill_ts_ms: int,
    own_stop: float,
    position_expiry_ms: int | None = None,
) -> LadderOutcome:
    """Replay the EXIT walk for a non-touch arm whose entry is force-filled.

    The entry-side fill primitives (open / VWAP) bypass the ``low <= limit``
    touch gate that :func:`replay_ladder` uses.  This helper pre-seeds a single
    synthetic fill at ``fill_price`` / ``fill_ts_ms`` into a :class:`_LadderWalk`
    and then runs the SAME per-bar exit walk (TP targets + ``own_stop`` + optional
    ``position_expiry_ms`` time-stop) over the bars that follow, returning a
    :class:`LadderOutcome` whose terminal state yields the exit mark.

    Key invariants
    --------------
    * The touch gate is **bypassed**: ``fill_price`` need not be touched by any
      bar's low.  The synthetic fill is pre-seeded directly into the walk state
      before the bar loop starts.
    * ``own_stop`` is the disaster stop used for this arm (NOT ``trade_setup``'s
      ``disaster_stop``).  The two may differ, e.g. because the entry-grid engine
      sets a per-arm stop based on the fill price.
    * TP targets come from ``trade_setup["tp_tranches"]`` unchanged.
    * No new entry fills happen during the exit walk: ``entry_expiry_ms`` is set
      to ``fill_ts_ms`` so all bars (ts >= fill_ts_ms) block new entry fills.
    * Bars before ``fill_ts_ms`` are excluded (the position does not exist yet).

    Implementation notes
    --------------------
    Reuses :class:`_LadderWalk` by constructing it with a modified
    :class:`_ParsedLadder` (``disaster_stop=own_stop``) and directly seeding the
    ``filled``, ``filled_ids``, and ``seq`` attributes to represent the synthetic
    entry.  The walk then iterates ``step(bar)`` normally over the post-fill bars
    and :func:`_finalize` computes the outcome.
    """
    # Fix 2: guard against a stale/missing fill price or stop passed by the caller.
    # Mirrors how replay_ladder returns a degenerate outcome on bad inputs.
    if not math.isfinite(fill_price) or not math.isfinite(own_stop):
        return LadderOutcome(status="NO_DATA")

    # Swap the disaster stop so the parsed ladder and _finalize both use own_stop.
    modified_setup = _with_disaster_stop(trade_setup, own_stop)
    ladder = parse_ladder(modified_setup)
    if not ladder.ok:
        return LadderOutcome(status="NO_STRUCTURE")

    if not bars:
        return LadderOutcome(status="NO_DATA")

    # Intentional duplication of parse_ladder + sorted(bars, ...) + empty-bar guards:
    # must track replay_ladder's defensive bar sort (bug #4) and degenerate-input guards.
    ordered = sorted(bars, key=lambda b: int(b["t"]))

    # Exclude bars that predate the fill: the position does not exist yet.
    post_fill = [b for b in ordered if int(b["t"]) >= fill_ts_ms]
    if not post_fill:
        return LadderOutcome(status="NO_DATA")

    # Construct the walk.  entry_expiry_ms=fill_ts_ms blocks all new entry fills
    # for bars at ts >= fill_ts_ms (the only bars we iterate), so the pre-seeded
    # synthetic level is the only fill that ever appears.
    walk = _LadderWalk(
        ladder,
        own_stop,
        entry_expiry_ms=fill_ts_ms,
        position_expiry_ms=position_expiry_ms,
    )

    # Pre-seed the synthetic entry -- bypass the low<=limit touch gate entirely.
    # level_id "E_SYNTH" is deliberately NOT in ladder.entries, so it cannot be
    # accidentally re-added by _fill_entries.
    # Fix 1: weight = total_entry_alloc so _filled_frac returns exactly 1.0 by
    # construction (synthetic fill is by definition a FULL fill of the position).
    synth_level = _Level(
        level_id="E_SYNTH", price=fill_price, weight=ladder.total_entry_alloc or 100.0
    )
    walk.filled.append(synth_level)
    walk.filled_ids.add("E_SYNTH")
    walk.seq.append(LevelCrossing("E_SYNTH", _ENTRY, fill_price, fill_ts_ms))

    # Run the per-bar exit walk over post-fill bars.
    for bar in post_fill:
        walk.step(bar)

    blended = fill_price  # single synthetic fill -> blended = fill_price
    return _finalize(walk, blended=blended, reference_close=None, ratchet_r=None)


def realized_r_full_fill(
    trade_setup: Mapping[str, Any] | None,
    bars: Sequence[Mapping[str, Any]],
    *,
    entry_expiry_ms: int | None = None,
    position_expiry_ms: int | None = None,
) -> float | None:
    """Realized R if the position had been entered at the FULL-FILL blended price.

    The entry-side counterfactual paired with the as-specified ``realized_r``: it
    replays the SAME exit ladder over the SAME bars, but from a single entry tier
    placed at the all-tier alloc-weighted blended entry (the price the ladder
    would have averaged if every tier had filled). The gap
    ``realized_r - realized_r_full_fill`` shows the entry-tier-spacing effect by
    SIGN: POSITIVE means laddering the entry HELPED (the actual composite entry
    achieved a higher R than a single fill at the blend would have); NEGATIVE
    means laddering HURT (a partial / shallow fill left R on the table that the
    deeper full-blend entry would have captured).

    Like the exit grid, this is a pure transform over the already-fetched bars
    (zero extra Polygon cost). It returns ``None`` for an unparseable setup or no
    bars; ``None`` realized R when the single full-blend limit never fills (price
    never dipped to the blended depth) -- which is itself the honest answer that a
    full-ladder fill never triggered on this path.
    """
    ladder = parse_ladder(trade_setup)
    if trade_setup is None or not bars or not ladder.ok:
        return None
    # Filter non-finite limits to match the monitor's _full_ladder_blended_entry
    # robustness (a corrupted NaN limit would otherwise poison the blend).
    finite_entries = [lvl for lvl in ladder.entries if math.isfinite(lvl.price)]
    if not finite_entries:
        return None
    full_blend = _blended_entry(finite_entries)
    setup_full = _with_entry_tiers(trade_setup, [{"limit": full_blend, "alloc_pct": 100.0}])
    return replay_ladder(
        setup_full,
        bars,
        entry_expiry_ms=entry_expiry_ms,
        position_expiry_ms=position_expiry_ms,
    ).realized_r


def realized_r_fill_anchored(
    trade_setup: Mapping[str, Any] | None,
    bars: Sequence[Mapping[str, Any]],
    *,
    stop_atr_mult: float = 0.5,
) -> float | None:
    """What-if realized R under a FILL-ANCHORED disaster stop (exit-geometry path b).

    "Size the stop to the ACTUAL fill, not the planned deep ladder." A stop placed
    ``stop_atr_mult*ATR`` below the shallowest tier E1 collapses the averaging-down
    ladder — that tight stop is pierced (SL-first) before a deeper dip can fill
    E2/E3 — so the faithful model is a SINGLE-tier entry at E1 with a
    ``stop_atr_mult*ATR`` disaster stop and the SAME TP targets, replayed over the
    SAME bars. Because it re-runs the walk (changing fills + stop-outs) it is a
    display-only counterfactual like :func:`replay_ladder_breakeven` / the exit grid
    — NEVER the headline ``realized_r``, and NOT a validated rule (in_sample). It
    isolates the winner-R compression the far stop imposes on shallow fills
    (``docs/research/exit_geometry_reward_risk_2026_06_30.md``).

    Returns ``None`` for an unparseable setup, no bars, a missing / non-finite /
    non-positive ATR, no finite entry tier, a non-positive risk
    (``stop_atr_mult <= 0``), or when the single E1 limit never fills on this path.
    """
    ladder = parse_ladder(trade_setup)
    if trade_setup is None or not bars or not ladder.ok:
        return None
    atr = ladder.atr
    if atr is None or not math.isfinite(atr) or atr <= 0:
        return None
    finite_entries = [lvl for lvl in ladder.entries if math.isfinite(lvl.price)]
    if not finite_entries:
        return None
    # Shallowest tier = highest price (E1); max() is robust to input ordering.
    e1 = max(lvl.price for lvl in finite_entries)
    anchored_stop = e1 - stop_atr_mult * atr
    if e1 - anchored_stop <= 0:  # stop_atr_mult <= 0 -> risk undefined
        return None
    setup_single = _with_entry_tiers(trade_setup, [{"limit": e1, "alloc_pct": 100.0}])
    setup_single = _with_disaster_stop(setup_single, anchored_stop)
    return replay_ladder(setup_single, bars).realized_r


def replay_ladder_atr_bracket(
    trade_setup: Mapping[str, Any] | None,
    bars: Sequence[Mapping[str, Any]],
    *,
    stop_atr_mult: float = 1.5,
    tp_atr_mult: float = 1.5,
    tp_floor_frac: float = 0.006,
    ceiling_price: float | None = None,
) -> float | None:
    """What-if realized R under a fixed ATR BRACKET exit (bezpazery v1).

    Source: the betlejem5 EMM executor bracket doctrine — INSPIRATION, not a
    replication (``docs/research/bezpazery_lens_design_2026_07_16.md``). The
    production entry tiers are KEPT (unlike :func:`realized_r_fill_anchored`'s
    single-E1 collapse); the exit is replaced with a symmetric bracket anchored to
    the walk-1 blended entry:

    * stop  = ``blended - stop_atr_mult * ATR`` (static — no ratchet, no trail);
    * TP    = ``min(ceiling_price, max(blended * (1 + tp_floor_frac),
      blended + tp_atr_mult * ATR))`` — a single 100% tranche. A ``None`` /
      non-finite ``ceiling_price`` leaves the TP uncapped (missing 52w-high data
      is coverage, not a null — memo §4.2); a ceiling at/below the cost floor
      makes the bracket degenerate and returns ``None`` (memo §4.1).

    ATR is ``trade_setup["atr"]`` (absolute, asof_close-anchored — the
    fill-anchored precedent) with the identical null-guard. Like the other
    what-if lenses this pass honours NO entry-TTL / position TIME_STOP: walk-1
    derives the fills with no expiries (:func:`replay_ladder_breakeven`
    contract) and walk-2 replays the modified setup the same way, so a position
    neither stopped nor TP'd by the last bar is marked to the last close
    (horizon-open remainder). DELIBERATE deviation from the betlejem5 source's
    same-day flatten — every registered lens must see the identical bar window
    (memo §3.1).

    Note on the R denominator: walk-2 re-derives fills under the bracket stop,
    so on multi-tier paths its realized blend (and hence risk) can drift
    slightly from the walk-1 anchor blend used to place the bracket — accepted
    for the ``replay_ladder`` reuse (SL-first ambiguity, filled-frac re-basing,
    NO_FILL->None inherited for free; memo §4.3). The bracket itself stays
    frozen at the walk-1 blend: later tier fills never move the stop/TP.

    Returns ``None`` for an unparseable setup, no bars, a missing / non-finite /
    non-positive ATR, a non-positive risk (``stop_atr_mult <= 0``), a
    non-constructible bracket (ceiling at/below the cost floor, or a bracket
    stop at/below zero), or when nothing fills in walk-1. Display-only; never
    the headline ``realized_r``.
    """
    ladder = parse_ladder(trade_setup)
    if trade_setup is None or not bars or not ladder.ok:
        return None
    atr = ladder.atr
    if atr is None or not math.isfinite(atr) or atr <= 0:
        return None
    if stop_atr_mult <= 0:  # risk = stop_atr_mult * atr would be <= 0
        return None
    ordered = sorted(bars, key=lambda b: int(b["t"]))
    stop = ladder.disaster_stop
    assert stop is not None  # ok=True guarantees it
    # Walk-1: derive the fill set under the family contract (no expiries) to
    # anchor the bracket at the alloc-weighted blended entry.
    walk = _LadderWalk(ladder, stop, entry_expiry_ms=None, position_expiry_ms=None)
    for bar in ordered:
        walk.step(bar)
    if not walk.filled:
        return None
    blended = _blended_entry(walk.filled)
    bracket_stop = blended - stop_atr_mult * atr
    if bracket_stop <= 0:
        # A stop at a non-positive price cannot be placed in reality (ATR wider
        # than ~1/stop_atr_mult of the entry) — degenerate bracket, same class
        # as the ceiling-below-floor case: None, not a never-hit stop that
        # would silently dilute R.
        return None
    tp_floor = blended * (1.0 + tp_floor_frac)
    tp = max(tp_floor, blended + tp_atr_mult * atr)
    if ceiling_price is not None and math.isfinite(ceiling_price):
        if ceiling_price <= tp_floor:
            return None  # bracket not constructible (memo section 4.1)
        tp = min(tp, ceiling_price)
    # Walk-2: same entries, single 100% TP tranche + the bracket stop, replayed
    # with no expiries (the modified-setup reuse pattern of the exit grid).
    setup_bracket = _with_tp_tranches(trade_setup, [{"target": tp, "tranche_pct": 100.0}])
    setup_bracket = _with_disaster_stop(setup_bracket, bracket_stop)
    return replay_ladder(setup_bracket, bars).realized_r


class _LadderWalk:
    """Single full-horizon pass state for the as-specified ladder replay.

    Encapsulates the per-bar mutation that :func:`replay_ladder` drives so the
    crossing sequence, fills, excursion watermarks, and exit flags live in one
    place. Behaviour is byte-identical to the original inline loop.
    """

    def __init__(
        self,
        ladder: _ParsedLadder,
        stop: float,
        *,
        entry_expiry_ms: int | None,
        position_expiry_ms: int | None,
    ) -> None:
        self.ladder = ladder
        self.stop = stop
        self.entry_expiry_ms = entry_expiry_ms
        self.position_expiry_ms = position_expiry_ms
        self.seq: list[LevelCrossing] = []
        self.filled: list[_Level] = []
        self.filled_ids: set[str] = set()
        self.hit_tp_ids: set[str] = set()
        self.ambiguous_bars = 0
        self.sl_hit = False
        self.time_stop = False  # synthetic position time-stop fired (terminal TIME_STOP)
        self.expiry_close: float | None = None  # close of the bar the time-stop fired on
        self.exit_reached = False  # as-specified exit (first SL, full TP, or time-stop) fired
        self.last_close: float | None = None
        self.in_trade_high: float | None = None  # highest high since first fill
        self.in_trade_low: float | None = None  # lowest low since first fill

    def step(self, bar: Mapping[str, Any]) -> None:
        ts, low, high, close = _bar_lhc(bar)
        self.last_close = close  # advances EVERY bar (whole-horizon forward_return)

        # Once the as-specified position has exited (full SL, or all TPs taken) it is
        # FLAT: no new entry fills, no in-trade excursion, no further exit resolution.
        # ``last_close`` still advances above so ``forward_return`` spans the whole
        # horizon. (zen HIGH: a post-exit dip must NOT fill an unused deeper tier and
        # retroactively change blended entry / filled_frac / realized_r, nor extend
        # the MFE/MAE window past the actual holding period.)
        if self.exit_reached:
            return

        self._fill_entries(ts, low)
        if not self.filled:
            return  # no position yet -> TP/SL/excursion cannot trigger

        # In-trade excursion over every HELD bar (first fill until the as-specified
        # exit). MFE/MAE measure only what happened while the position was open.
        self.in_trade_high = high if self.in_trade_high is None else max(self.in_trade_high, high)
        self.in_trade_low = low if self.in_trade_low is None else min(self.in_trade_low, low)

        if self._resolve_stop(ts, low, high):
            return
        self._take_tps(ts, high)
        self._maybe_time_stop(ts, close)

    def _fill_entries(self, ts: int, low: float) -> None:
        """Entries first (you cannot exit before entering). Multiple tiers can fill
        in one bar (a gap-down through several limits)."""
        for lvl in self.ladder.entries:
            # Entry-TTL: a limit touched at/after the cutoff is stale and does NOT
            # fill (only blocks NEW fills; TP/SL still resolves an open position).
            if self.entry_expiry_ms is not None and ts >= self.entry_expiry_ms:
                break
            if lvl.level_id not in self.filled_ids and low <= lvl.price:
                self.filled.append(lvl)
                self.filled_ids.add(lvl.level_id)
                self.seq.append(LevelCrossing(lvl.level_id, _ENTRY, lvl.price, ts))

    def _resolve_stop(self, ts: int, low: float, high: float) -> bool:
        """Resolve a disaster-stop pierce. Returns True when the position exited.

        SL-first on ambiguity: a TP also crossable this bar (Bug #3), OR a fresh
        entry that filled THIS bar AND a stop pierced THIS bar (intra-bar order
        unknown), both flag the SL ``same_bar_ambiguous`` + bump the counter.
        """
        if low > self.stop:
            return False
        tp_crossable = any(
            t.level_id not in self.hit_tp_ids and high >= t.price for t in self.ladder.tps
        )
        entered_this_bar = any(c.bar_ts_ms == ts and c.kind == _ENTRY for c in self.seq)
        ambiguous = tp_crossable or entered_this_bar
        if ambiguous:
            self.ambiguous_bars += 1
        self.seq.append(LevelCrossing("SL", _SL, self.stop, ts, same_bar_ambiguous=ambiguous))
        self.sl_hit = True
        self.exit_reached = True
        return True

    def _take_tps(self, ts: int, high: float) -> None:
        """Record each TP tranche hit this bar (ascending); mark exit on full scale-out."""
        for t in self.ladder.tps:
            if t.level_id in self.hit_tp_ids or high < t.price:
                continue
            self.hit_tp_ids.add(t.level_id)
            self.seq.append(LevelCrossing(t.level_id, _TP, t.price, ts))
        if self.ladder.tps and len(self.hit_tp_ids) == len(self.ladder.tps):
            self.exit_reached = True  # fully scaled out via TPs

    def _maybe_time_stop(self, ts: int, close: float) -> None:
        """Synthetic position time-stop LAST: only when the as-specified exit did NOT
        fire on this bar (a real SL/TP on the cutoff bar WINS over the time-stop)."""
        if self.exit_reached or self.position_expiry_ms is None or ts < self.position_expiry_ms:
            return
        # TIME_STOP records the BAR's CLOSE as ``price`` (not a fill level — the
        # remainder is marked to close at expiry). Consumers should read the
        # realized_r / classification, not interpret this cross's price as a trigger.
        self.seq.append(LevelCrossing("TIME_STOP", _TIME, close, ts))
        self.expiry_close = close
        self.time_stop = True
        self.exit_reached = True


def _finalize(
    walk: _LadderWalk,
    *,
    blended: float | None,
    reference_close: float | None,
    ratchet_r: float | None,
) -> LadderOutcome:
    # Unpack the walk's terminal state into the local names the body uses.
    ladder = walk.ladder
    seq = walk.seq
    filled = walk.filled
    hit_tp_ids = walk.hit_tp_ids
    sl_hit = walk.sl_hit
    ambiguous_bars = walk.ambiguous_bars
    last_close = walk.last_close
    in_trade_high = walk.in_trade_high
    in_trade_low = walk.in_trade_low
    time_stop = walk.time_stop
    expiry_close = walk.expiry_close

    entries_filled = tuple(lvl.level_id for lvl in filled)
    tps_hit = tuple(t.level_id for t in ladder.tps if t.level_id in hit_tp_ids)

    forward_return = _forward_return(reference_close, last_close)

    if not filled or blended is None:
        return LadderOutcome(
            status="OK",
            sequence=tuple(seq),
            classification="NO_FILL",
            ambiguous_bars=ambiguous_bars,
            forward_return=forward_return,
        )

    stop = ladder.disaster_stop
    assert stop is not None
    risk = blended - stop  # R unit per share

    # ``filled_frac`` is well-defined regardless of geometry (it is the alloc-
    # weighted fill ratio), so expose it on every filled outcome — the size layer
    # needs it even for a BAD_GEOMETRY row (a real position was opened).
    filled_frac = _filled_frac(ladder, filled)

    # Bug #2: degenerate geometry (stop at/above the blended entry) makes R-units
    # undefined. Classify BAD_GEOMETRY with realized_r EXPLICITLY None rather than
    # silently returning a 0 or a NaN.
    if risk <= 0:
        mfe, mae, mfe_pct, mae_pct = _excursions(blended, risk, in_trade_high, in_trade_low)
        return LadderOutcome(
            status="OK",
            sequence=tuple(seq),
            entries_filled=entries_filled,
            tps_hit=tps_hit,
            sl_hit=sl_hit,
            classification="BAD_GEOMETRY",
            blended_entry=blended,
            realized_r=None,
            filled_fraction=filled_frac,
            ambiguous_bars=ambiguous_bars,
            mfe=None,  # R-units undefined when risk <= 0
            mae=None,
            mfe_pct=mfe_pct,
            mae_pct=mae_pct,
            forward_return=forward_return,
            ratchet_realized_r=None,
        )

    realized_r, horizon_open = _realized_r_with_frac(
        ladder, hit_tp_ids, blended, stop, risk, sl_hit, last_close, filled_frac, expiry_close
    )
    mfe, mae, mfe_pct, mae_pct = _excursions(blended, risk, in_trade_high, in_trade_low)

    classification = _classify(
        bool(tps_hit),
        sl_hit,
        len(hit_tp_ids) == len(ladder.tps) and bool(ladder.tps),
        horizon_open,
        time_stop,
    )
    return LadderOutcome(
        status="OK",
        sequence=tuple(seq),
        entries_filled=entries_filled,
        tps_hit=tps_hit,
        sl_hit=sl_hit,
        classification=classification,
        blended_entry=blended,
        realized_r=realized_r,
        filled_fraction=filled_frac,
        horizon_open=horizon_open,
        ambiguous_bars=ambiguous_bars,
        mfe=mfe,
        mae=mae,
        mfe_pct=mfe_pct,
        mae_pct=mae_pct,
        forward_return=forward_return,
        ratchet_realized_r=ratchet_r,
    )


def _realized_r_with_frac(
    ladder: _ParsedLadder,
    hit_tp_ids: set[str],
    blended: float,
    stop: float,
    risk: float,
    sl_hit: bool,
    last_close: float | None,
    filled_frac: float,
    expiry_close: float | None = None,
) -> tuple[float, bool]:
    """Realized R over the FILLED position with TP shares re-based to the fill.

    Bug #1: a TP tranche's ``tranche_pct`` is defined over the FULL intended
    position, but when only some entry tiers fill each tranche must be re-based
    to its share of the FILLED position::

        share_of_filled = (tranche_pct / 100) / filled_frac

    capped so the cumulative TP shares never exceed 1.0; the remainder is closed
    at the ``stop`` (if SL hit) or marked to the last close (horizon-open). When
    ALL tiers fill (``filled_frac == 1``) the re-based share reduces exactly to
    the old full-position weighting. ``stop`` is the disaster stop for the
    headline pass and the EFFECTIVE (ratcheted) stop for the what-if pass.
    """
    tp_wsum = sum(t.weight for t in ladder.tps)
    contrib = 0.0
    cumulative_share = 0.0
    horizon_open = False
    for t in ladder.tps:
        if tp_wsum > 0:
            full_share = t.weight / tp_wsum
        else:
            full_share = 1.0 / max(len(ladder.tps), 1)
        # Re-base the tranche over the FILLED fraction, capping cumulative at 1.0.
        share = full_share / filled_frac if filled_frac > 0 else full_share
        share = min(share, 1.0 - cumulative_share)
        if share <= 0:
            continue
        if t.level_id in hit_tp_ids:
            contrib += share * (t.price - blended) / risk
            cumulative_share += share
    remaining = max(0.0, 1.0 - cumulative_share)
    if remaining > 1e-9:
        if sl_hit:
            contrib += remaining * (stop - blended) / risk  # = -remaining
        elif expiry_close is not None:
            # Time-stop: mark the remainder at the close of the expiry bar. Terminal
            # (TIME_STOP), so horizon_open stays False.
            contrib += remaining * (expiry_close - blended) / risk
        elif last_close is not None:
            contrib += remaining * (last_close - blended) / risk
            horizon_open = True
    return contrib, horizon_open


def _excursions(
    blended: float,
    risk: float,
    in_trade_high: float | None,
    in_trade_low: float | None,
) -> tuple[float | None, float | None, float | None, float | None]:
    """MFE / MAE in R-units and as fractions of the blended entry.

    Anchored EX-POST to the FINAL blended entry, computed over IN-TRADE bars only
    (the caller updates the high/low watermarks from the first fill until the
    as-specified exit). Note the anchor is the final blend: an excursion bar
    between the first and a later tier fill is measured against a blend that
    already includes the later tier — a deliberate substrate approximation, not
    a running-blend reconstruction. ``None`` when there is no in-trade bar or
    ``risk <= 0`` (R-units undefined).
    """
    if in_trade_high is None or in_trade_low is None:
        return None, None, None, None
    mfe_pct = (in_trade_high - blended) / blended if blended else None
    mae_pct = (in_trade_low - blended) / blended if blended else None
    if risk <= 0:
        return None, None, mfe_pct, mae_pct
    mfe = (in_trade_high - blended) / risk
    mae = (in_trade_low - blended) / risk
    return mfe, mae, mfe_pct, mae_pct


def _forward_return(reference_close: float | None, last_close: float | None) -> float | None:
    """Close-to-close horizon return, independent of any ladder fill."""
    if reference_close is None or last_close is None or reference_close == 0:
        return None
    return (last_close - reference_close) / reference_close


def _fill_entry_ids(ladder: _ParsedLadder, filled_ids: set[str], low: float) -> None:
    """Add any entry tier whose limit is touched this bar to ``filled_ids`` (in place)."""
    for lvl in ladder.entries:
        if lvl.level_id not in filled_ids and low <= lvl.price:
            filled_ids.add(lvl.level_id)


def _ratchet_eff_stop(
    eff_stop: float, hit_tp_ids: set[str], blended: float, tp1_price: float
) -> float:
    """Raise the effective stop per the ratchet rule: TP1 -> break-even+, TP2 -> lock-in."""
    if "TP1" in hit_tp_ids:
        eff_stop = max(eff_stop, blended)  # break-even+
    if "TP2" in hit_tp_ids:
        eff_stop = max(eff_stop, tp1_price)  # lock-in
    return eff_stop


def _replay_ratchet(
    ladder: _ParsedLadder,
    ordered_bars: Sequence[Mapping[str, Any]],
    filled: list[_Level],
    blended: float,
    risk: float,
) -> float | None:
    """SECOND walk over the SAME bars with a ratcheting effective stop.

    Separate state from the as-specified pass; never touches ``realized_r``.
    Ratchet rule (design memo §2 simplified, NO ATR trailing runner -- that is a
    follow-up): start at the disaster stop; on TP1 hit raise the stop to the
    blended entry (break-even+); on TP2 hit raise it to the TP1 price (lock-in).
    The remainder exits when ``low <= eff_stop``; if it survives the horizon it is
    marked to the last close. Same SL-first ambiguity + same bug-#1 filled-frac
    re-basing as the headline pass. The position TIME_STOP is intentionally NOT
    applied to the ratchet pass -- the time-stop is a layer-2 headline concern;
    the ratchet what-if terminates on SL / full-TP / horizon only.

    Returns ``ratchet_realized_r`` (R-units) or ``None`` when risk <= 0 (geometry
    undefined -- matches the headline BAD_GEOMETRY guard).
    """
    if risk <= 0 or not ladder.tps:
        return None

    stop = ladder.disaster_stop
    assert stop is not None
    eff_stop = stop
    tp1_price = ladder.tps[0].price
    hit_tp_ids: set[str] = set()
    sl_hit = False
    last_close: float | None = None
    filled_ids: set[str] = set()

    for bar in ordered_bars:
        _ts, low, high, close = _bar_lhc(bar)
        last_close = close
        # Re-derive position state on the SAME fill model so the ratchet walk is
        # self-contained (no shared mutable state with the headline pass).
        _fill_entry_ids(ladder, filled_ids, low)
        if not filled_ids:
            continue

        # SL-first on ambiguity (a TP also crossable this bar): the disaster /
        # ratcheted stop wins, so a pierce ends the walk before taking any TP.
        if low <= eff_stop:
            sl_hit = True
            break
        for t in ladder.tps:
            if t.level_id not in hit_tp_ids and high >= t.price:
                hit_tp_ids.add(t.level_id)
        # Ratchet the effective stop AFTER recording TP hits this bar.
        eff_stop = _ratchet_eff_stop(eff_stop, hit_tp_ids, blended, tp1_price)
        if len(hit_tp_ids) == len(ladder.tps):
            break  # fully scaled out

    filled_frac = _filled_frac(ladder, filled)
    # The remainder exits at the EFFECTIVE stop (not the disaster stop) on an SL.
    contrib, _open = _realized_r_with_frac(
        ladder, hit_tp_ids, blended, eff_stop, risk, sl_hit, last_close, filled_frac
    )
    return contrib


def replay_ladder_breakeven(
    trade_setup: Mapping[str, Any] | None,
    bars: Sequence[Mapping[str, Any]],
    *,
    mfe_trigger_r: float,
    trail_frac: float | None = None,
) -> float | None:
    """What-if realized R under an MFE-triggered break-even / trailing stop.

    Distinct from :func:`_replay_ratchet` (which raises the stop only on a TP-target
    HIT): this pass raises the effective stop to the blended entry (break-even) once
    the running in-trade MFE first reaches ``mfe_trigger_r`` R. That threshold fires
    far more often than a TP target, because losers in this book peak around +0.6R
    MFE before reversing to the full -1R disaster stop (the exit-geometry diagnosis,
    ``docs/research/exit_geometry_reward_risk_2026_06_30.md``). When ``trail_frac`` is
    set, once triggered the effective stop trails at
    ``blended + trail_frac * (peak_high - blended)`` — locking in a fraction of the
    best price reached (a winner-trailing runner).

    Returns realized R (fill-anchored, like ``realized_r``) or ``None`` when the
    setup is unparseable, no bars are given, nothing filled, or risk <= 0. Pure: it
    never mutates the setup and never touches the headline ``realized_r``.
    ``mfe_trigger_r = inf`` with ``trail_frac=None`` reduces to a static
    disaster-stop walk (baseline parity with :func:`replay_ladder`'s ``realized_r``).

    Like :func:`_replay_ratchet`, this what-if honours NO entry-TTL / position
    TIME_STOP — it terminates on SL / full-TP / horizon only. It therefore takes no
    ``entry_expiry_ms`` / ``position_expiry_ms``: exposing them would invite a
    biased what-if where the fill set (and ``filled_frac``) is determined under an
    expiry the second walk's re-derivation ignores. Both walks here re-derive fills
    without an expiry, so they stay internally consistent (zen review, PR #722).
    """
    ladder = parse_ladder(trade_setup)
    if not ladder.ok or not bars:
        return None
    ordered = sorted(bars, key=lambda b: int(b["t"]))
    stop = ladder.disaster_stop
    assert stop is not None  # ok=True guarantees it
    # No entry-TTL / time-stop in the what-if (see docstring): both walks re-derive
    # fills without an expiry, keeping filled_frac and the second walk consistent.
    walk = _LadderWalk(ladder, stop, entry_expiry_ms=None, position_expiry_ms=None)
    for bar in ordered:
        walk.step(bar)
    if not walk.filled:
        return None
    blended = _blended_entry(walk.filled)
    risk = blended - stop
    if risk <= 0:
        return None
    filled_frac = _filled_frac(ladder, walk.filled)
    return _replay_breakeven(
        ladder, ordered, blended, risk, stop, filled_frac, mfe_trigger_r, trail_frac
    )


def _scan_tp_hits(ladder: _ParsedLadder, hit_tp_ids: set[str], high: float) -> None:
    """Record (in place) any TP level newly pierced by ``high``."""
    for t in ladder.tps:
        if t.level_id not in hit_tp_ids and high >= t.price:
            hit_tp_ids.add(t.level_id)


def _replay_breakeven(
    ladder: _ParsedLadder,
    ordered_bars: Sequence[Mapping[str, Any]],
    blended: float,
    risk: float,
    stop: float,
    filled_frac: float,
    mfe_trigger_r: float,
    trail_frac: float | None,
) -> float:
    """Second walk with an MFE-triggered break-even / trailing effective stop.

    Mirrors :func:`_replay_ratchet`'s self-contained re-derivation (SL-first on
    ambiguity, same filled-frac re-basing of TP shares) but swaps the TP-hit ratchet
    for an MFE-R-threshold break-even (optionally trailing). The running MFE is
    measured against the FINAL ``blended`` (same anchor as the stored ``mfe``). The
    position TIME_STOP is intentionally NOT applied — the what-if terminates on
    SL / full-TP / horizon only, matching the ratchet pass.
    """
    eff_stop = stop
    hit_tp_ids: set[str] = set()
    sl_hit = False
    last_close: float | None = None
    filled_ids: set[str] = set()
    peak_high: float | None = None
    triggered = False
    for bar in ordered_bars:
        _ts, low, high, close = _bar_lhc(bar)
        last_close = close
        _fill_entry_ids(ladder, filled_ids, low)
        if not filled_ids:
            continue
        # SL-first on ambiguity: a pierce of the (possibly ratcheted) stop ends the
        # walk before any new TP / break-even raise recorded on the SAME bar.
        if low <= eff_stop:
            sl_hit = True
            break
        _scan_tp_hits(ladder, hit_tp_ids, high)
        if peak_high is None or high > peak_high:
            peak_high = high
        if (peak_high - blended) / risk >= mfe_trigger_r:
            triggered = True
        if triggered:
            eff_stop = max(eff_stop, _breakeven_eff_stop(blended, peak_high, trail_frac))
        if len(hit_tp_ids) == len(ladder.tps):
            break  # fully scaled out
    contrib, _open = _realized_r_with_frac(
        ladder, hit_tp_ids, blended, eff_stop, risk, sl_hit, last_close, filled_frac
    )
    return contrib


def _breakeven_eff_stop(blended: float, peak_high: float, trail_frac: float | None) -> float:
    """Break-even (``blended``), or a trailing lock-in at ``trail_frac`` of the peak gain."""
    if trail_frac is None:
        return blended
    return max(blended, blended + trail_frac * (peak_high - blended))


def _classify(
    any_tp: bool, sl_hit: bool, all_tp: bool, horizon_open: bool, time_stop: bool = False
) -> str:
    if all_tp:
        return "TP_FULL"
    if any_tp and sl_hit:
        return "PARTIAL_TP_THEN_SL"
    if sl_hit:
        return "SL_HIT"
    if time_stop:
        # Terminal: covers partial-TP-then-timestop AND no-TP-timestop. Outranks
        # PARTIAL_TP_OPEN; a real SL above outranks it.
        return "TIME_STOP"
    if any_tp:
        return "PARTIAL_TP_OPEN"
    if horizon_open:
        return "OPEN"
    return "OPEN"


ENTRY_GRID_ARMS: tuple[str, ...] = (
    "baseline",
    "narrow_tiers",
    "single_at_close",
    "market_at_arrival",
    "vwap_arrival",
)
"""Canonical ordered arm names for the 5-arm entry-grid counterfactual.

Touch arms (resting-limit dip-buy): baseline, narrow_tiers, single_at_close.
Non-touch arms (always-fill): market_at_arrival, vwap_arrival.
"""


@dataclass(frozen=True)
class _ArmExcessContext:
    """Finalization inputs shared by every entry-grid arm.

    Computed once per event in :func:`replay_entry_grid` and threaded into the
    touch / non-touch reward helpers and :func:`_arm_excess_from_outcome`,
    keeping each helper's parameter list small. All fields are arm-independent;
    the per-arm ``arm_name`` is passed alongside (it varies per call).
    """

    benchmark_window_return: float
    market_cap: float | None
    first_rth_bar: Mapping[str, Any] | None
    apply_haircut_fn: Any
    implausible_threshold: float
    cash_reward: float


def _arm_excess_from_outcome(
    outcome: LadderOutcome,
    *,
    own_stop: float,
    arm_name: str,
    ctx: _ArmExcessContext,
    check_outcome_status: bool = False,
) -> float | None:
    """Shared reward-finalization step for all five entry-grid arms.

    Called after the arm-specific replay (touch or non-touch) has produced a
    :class:`LadderOutcome`.  Both paths share identical finalization logic:
    cash-classification check, ``exit_mark`` derivation, implausible-return guard,
    raw-excess computation, and haircut application.

    Parameters
    ----------
    outcome:
        The :class:`LadderOutcome` from the arm's replay pass.
    own_stop:
        The disaster-stop price used in the arm's replay (may differ from the
        source setup's ``disaster_stop`` for the alternative-geometry arms).
    arm_name:
        The arm identifier string passed through to ``ctx.apply_haircut_fn``.
    ctx:
        The arm-independent finalization inputs (benchmark return, market cap,
        first RTH bar, haircut fn, implausible threshold, cash reward) — see
        :class:`_ArmExcessContext`.
    check_outcome_status:
        When ``True``, also maps ``outcome.status in {"NO_DATA", "NO_STRUCTURE"}``
        to ``ctx.cash_reward``.  Set for non-touch arms whose synthetic-fill
        replay can produce these status codes on degenerate bar inputs.
    """
    if outcome.classification in ("NO_FILL", "BAD_GEOMETRY"):
        return ctx.cash_reward
    if check_outcome_status and outcome.status in ("NO_DATA", "NO_STRUCTURE"):
        return ctx.cash_reward

    b = outcome.blended_entry
    r = outcome.realized_r
    if b is None or r is None or not math.isfinite(b) or not math.isfinite(r):
        return ctx.cash_reward

    exit_mark = b + r * (b - own_stop)

    if not math.isfinite(exit_mark) or abs(exit_mark / b - 1.0) > ctx.implausible_threshold:
        return None

    raw_excess = (exit_mark - b) / b - ctx.benchmark_window_return
    return ctx.apply_haircut_fn(
        raw_excess,
        arm=arm_name,
        market_cap=ctx.market_cap,
        first_rth_bar=ctx.first_rth_bar,
    )


def _touch_arm_reward(
    arm_name: str,
    arm_setup: Any,
    own_stop: float,
    *,
    trade_setup: Mapping[str, Any],
    ordered_bars: Sequence[Mapping[str, Any]],
    entry_expiry_ms: int | None,
    position_expiry_ms: int | None,
    ctx: _ArmExcessContext,
) -> float | None:
    """Replay a touch arm and return its net haircut-adjusted excess (or None).

    Distinguishes two failure modes:

    - ``NO_STRUCTURE``: arm could not be built (uncomputable) → ``None``,
      so the common-support filter in the offline script can drop the event.
    - ``BAD_GEOMETRY``: arm was built but geometry collapsed → ``cash_reward``.

    Non-finite ``own_stop`` also signals an uncomputable arm → ``None``.
    """
    if arm_setup.status == "NO_STRUCTURE":
        # Arm could not be built (e.g. NaN atr, zero close) -> uncomputable.
        return None
    if arm_setup.status != "OK":
        # BAD_GEOMETRY (or any other non-OK non-NO_STRUCTURE status) -> cash.
        return ctx.cash_reward

    # Guard: a non-finite own_stop means the arm's stop geometry is undefined
    # (e.g. arm_disaster_stop returned NaN because atr was missing/NaN).
    # This is an uncomputable arm, not a resting-arm that hit cash.
    if not math.isfinite(own_stop):
        return None

    # Build the modified setup: swap entry tiers + disaster stop.
    modified = _with_entry_tiers(trade_setup, list(arm_setup.entry_tiers))  # type: ignore[arg-type]
    modified = _with_disaster_stop(modified, own_stop)

    outcome = replay_ladder(
        modified,
        ordered_bars,
        entry_expiry_ms=entry_expiry_ms,
        position_expiry_ms=position_expiry_ms,
    )

    return _arm_excess_from_outcome(
        outcome,
        own_stop=own_stop,
        arm_name=arm_name,
        ctx=ctx,
    )


def _notouch_arm_reward(
    arm_name: str,
    arm_fill: Any,
    *,
    trade_setup: Mapping[str, Any],
    ordered_bars: Sequence[Mapping[str, Any]],
    arrival_open_ms: int,
    position_expiry_ms: int | None,
    atr: float,
    close: float,
    ctx: _ArmExcessContext,
) -> float | None:
    """Process a non-touch fill result and return net excess (or None).

    ``NO_FILL`` or a non-finite fill price → cash (resting-cash alternative).
    Non-finite ``own_stop`` (NaN atr or close) → ``None`` (uncomputable arm).
    """
    if arm_fill.status == "NO_FILL" or arm_fill.fill_price is None:
        return ctx.cash_reward

    fill_price: float = arm_fill.fill_price
    fill_ts_ms: int = arm_fill.fill_ts_ms if arm_fill.fill_ts_ms is not None else arrival_open_ms

    if not math.isfinite(fill_price):
        return ctx.cash_reward

    from alphalens_pipeline.thematic.trade_setup.entry_primitives import arm_disaster_stop

    own_stop = arm_disaster_stop(fill_price, atr, close)
    if not math.isfinite(own_stop):
        # own_stop is NaN (atr or close is NaN/non-positive) — the arm's stop
        # geometry is undefined, so it is uncomputable, NOT a cash position.
        return None

    outcome = _replay_synthetic_fill(
        trade_setup,
        ordered_bars,
        fill_price=fill_price,
        fill_ts_ms=fill_ts_ms,
        own_stop=own_stop,
        position_expiry_ms=position_expiry_ms,
    )

    return _arm_excess_from_outcome(
        outcome,
        own_stop=own_stop,
        arm_name=arm_name,
        ctx=ctx,
        check_outcome_status=True,
    )


def replay_entry_grid(
    trade_setup: Mapping[str, Any] | None,
    bars: Sequence[Mapping[str, Any]],
    *,
    arrival_open_ms: int,
    arrival_close_ms: int,
    benchmark_window_return: float | None,
    market_cap: float | None,
    entry_expiry_ms: int | None = None,
    position_expiry_ms: int | None = None,
) -> dict[str, float | None]:
    """Replay five entry-arm counterfactuals and return per-arm net excess rewards.

    For each arm in :data:`ENTRY_GRID_ARMS` the function:

    1. Builds or resolves the arm's entry geometry.
    2. Replays the price path (same ``bars``) with that geometry, holding the
       TP targets and baseline disaster-stop structure **fixed** across arms.
    3. Derives ``arm_blended`` (the arm's fill denominator) and ``exit_mark``
       (the terminal mark — TP price, own disaster-stop, or time-stop close).
    4. Computes ``raw_excess = (exit_mark − arm_blended) / arm_blended − benchmark_window_return``.
    5. Applies :func:`alphalens_pipeline.feedback.execution_cost.apply_haircut_to_excess`
       (0 bps for resting-limit touch arms; half-spread + market-impact for always-fill arms).

    Returns ``{arm_name: net_reward_or_None}`` for all five arms.

    Shared unevaluability — ALL arms return ``None`` when:
    * ``bars`` is empty,
    * ``trade_setup`` is ``None`` or unparseable (status != "OK"),
    * ``benchmark_window_return`` is ``None``.

    Per-arm cash handling — ``NO_FILL`` or ``BAD_GEOMETRY`` maps to
    ``−benchmark_window_return`` (cash = 0 raw return, so excess = minus the
    benchmark). These two conditions are treated identically.

    Per-arm implausible-return guard — when ``|(exit_mark / arm_blended) − 1|``
    exceeds :data:`alphalens_pipeline.feedback.bar_window.IMPLAUSIBLE_RETURN_THRESHOLD`
    (0.60) the arm returns ``None`` individually (a split or bad data on that
    arm's path; other arms are unaffected).

    The exit is held FIXED across arms by absolute TP/SL prices.  Only the
    entry denominator and per-arm disaster stop vary; a difference in reward
    across arms is therefore attributable to entry execution, not to exit
    management.

    Parameters
    ----------
    trade_setup:
        The parsed ``brief_trade_setup`` dict.  Must contain ``status``,
        ``entry_tiers``, ``tp_tranches``, ``disaster_stop``, ``atr``, and
        ``asof_close`` (the latter two feed the alternative arm builders).
    bars:
        Minute OHLCV bars with keys ``t``, ``o``, ``h``, ``l``, ``c``, ``v``,
        covering the full position horizon.
    arrival_open_ms:
        Epoch ms of the session open (RTH start for the arrival date).
    arrival_close_ms:
        Epoch ms of the session close (RTH end for the arrival date).
    benchmark_window_return:
        The market benchmark return over the same holding window.  ``None``
        renders the whole grid unevaluable.
    market_cap:
        Issuer market cap in USD; passed through to the haircut model.
        ``None`` uses the conservative default impact bps.
    entry_expiry_ms:
        Epoch ms after which touch-gate entry fills are blocked (touch arms).
        Defaults to ``None`` (no TTL).
    position_expiry_ms:
        Epoch ms at which an open position is synthetically time-stopped and
        marked to the expiry-bar close.  Defaults to ``None`` (horizon-open
        positions are marked to the last bar close).
    """
    # --- Lazy import: entry_primitives and execution_cost are in sibling modules;
    # keeping them as function-local imports avoids circular-import risk and mirrors
    # the pattern used by other ladder_replay helpers.
    from alphalens_pipeline.feedback.bar_window import IMPLAUSIBLE_RETURN_THRESHOLD
    from alphalens_pipeline.feedback.execution_cost import apply_haircut_to_excess
    from alphalens_pipeline.thematic.trade_setup.entry_primitives import (
        build_baseline_arm,
        build_narrow_tiers_arm,
        build_single_at_close_arm,
        market_at_arrival_fill,
        vwap_arrival_fill,
    )
    from alphalens_pipeline.thematic.trade_setup.ladder import (
        _MIN_SPACING_MULT,
        _MIN_STOP_DIST_MULT,
    )

    none_grid: dict[str, float | None] = dict.fromkeys(ENTRY_GRID_ARMS, None)

    # Shared unevaluability checks.
    if benchmark_window_return is None:
        return dict(none_grid)
    if not math.isfinite(benchmark_window_return):
        return dict(none_grid)
    if not bars:
        return dict(none_grid)
    parsed = parse_ladder(trade_setup)
    if not parsed.ok:
        return dict(none_grid)
    assert trade_setup is not None  # parse_ladder(None).ok is False; ok=True => not None

    # Extract scalar inputs needed by the alternative arm builders.
    # Guard: malformed (non-numeric) values fall back to NaN so the downstream
    # math.isfinite checks route the affected arms to None without raising.
    raw_atr = trade_setup.get("atr")
    raw_close = trade_setup.get("asof_close")
    try:
        atr: float = float(raw_atr) if raw_atr is not None else float("nan")
    except (ValueError, TypeError):
        atr = float("nan")
    try:
        close: float = float(raw_close) if raw_close is not None else float("nan")
    except (ValueError, TypeError):
        close = float("nan")

    # Sort bars ascending once; all arm replays share this ordering.
    ordered_bars = sorted(bars, key=lambda b: int(b["t"]))

    # First in-arrival-window bar — used by the haircut model for the spread estimate.
    first_rth_bar: Mapping[str, Any] | None = next(
        (b for b in ordered_bars if arrival_open_ms <= int(b["t"]) <= arrival_close_ms),
        None,
    )

    cash_reward = -benchmark_window_return

    # Arm-independent finalization inputs, computed once and shared by all arms.
    excess_ctx = _ArmExcessContext(
        benchmark_window_return=benchmark_window_return,
        market_cap=market_cap,
        first_rth_bar=first_rth_bar,
        apply_haircut_fn=apply_haircut_to_excess,
        implausible_threshold=IMPLAUSIBLE_RETURN_THRESHOLD,
        cash_reward=cash_reward,
    )

    # Shared keyword arguments forwarded to both arm-reward helpers.
    _arm_kwargs: dict[str, Any] = {
        "trade_setup": trade_setup,
        "ordered_bars": ordered_bars,
        "ctx": excess_ctx,
    }

    result: dict[str, float | None] = dict(none_grid)

    # ------------------------------------------------------------------
    # Touch arms: baseline, narrow_tiers, single_at_close
    # ------------------------------------------------------------------
    # Each touch arm is replayed via replay_ladder with its own entry tiers and
    # own disaster stop.  The TP targets from trade_setup are kept unchanged so
    # the exit mark is the same absolute TP price across all arms.
    #
    # exit_mark derivation:
    #   Given blended_entry (b) and realized_r (R) from the outcome, and the stop
    #   used in the replay (s):
    #       R = (exit_mark − b) / (b − s)
    #       exit_mark = b + R × (b − s)
    #
    # This algebraically recovers the absolute exit price:
    #   - TP hit: exit_mark = TP price (independent of s)
    #   - SL hit: exit_mark = s (the arm's own stop)
    #   - Time-stop: exit_mark = expiry bar close (independent of s)
    #   - Horizon-open: exit_mark = last close (independent of s)

    # baseline: pass-through control arm; its own stop is the source setup's stop.
    # Guard: a non-numeric disaster_stop falls back to NaN so the isfinite check
    # in _touch_arm_reward returns None (uncomputable) instead of raising.
    baseline_setup = build_baseline_arm(trade_setup)
    try:
        baseline_stop = float(trade_setup.get("disaster_stop", float("nan")))
    except (ValueError, TypeError):
        baseline_stop = float("nan")
    result["baseline"] = _touch_arm_reward(
        "baseline",
        baseline_setup,
        baseline_stop,
        entry_expiry_ms=entry_expiry_ms,
        position_expiry_ms=position_expiry_ms,
        **_arm_kwargs,
    )

    # narrow_tiers: compact dip-buy arm built from close + atr.
    narrow_setup = build_narrow_tiers_arm(
        close=close,
        atr=atr,
        min_spacing_mult=_MIN_SPACING_MULT,
        min_stop_dist_mult=_MIN_STOP_DIST_MULT,
    )
    # narrow_tiers own_stop is embedded in the ArmSetup; extract it.
    narrow_stop = (
        narrow_setup.disaster_stop if narrow_setup.disaster_stop is not None else float("nan")
    )
    result["narrow_tiers"] = _touch_arm_reward(
        "narrow_tiers",
        narrow_setup,
        narrow_stop,
        entry_expiry_ms=entry_expiry_ms,
        position_expiry_ms=position_expiry_ms,
        **_arm_kwargs,
    )

    # single_at_close: single entry at or just below close.
    sac_setup = build_single_at_close_arm(close=close, atr=atr)
    sac_stop = sac_setup.disaster_stop if sac_setup.disaster_stop is not None else float("nan")
    result["single_at_close"] = _touch_arm_reward(
        "single_at_close",
        sac_setup,
        sac_stop,
        entry_expiry_ms=entry_expiry_ms,
        position_expiry_ms=position_expiry_ms,
        **_arm_kwargs,
    )

    # ------------------------------------------------------------------
    # Non-touch arms: market_at_arrival, vwap_arrival
    # ------------------------------------------------------------------
    # Fill primitives bypass the low<=limit touch gate.  On a successful fill
    # the exit walk is driven by _replay_synthetic_fill with the arm's own stop.
    # On NO_FILL -> cash.

    maa_fill = market_at_arrival_fill(
        list(ordered_bars),
        arrival_open_ms=arrival_open_ms,
        arrival_close_ms=arrival_close_ms,
    )
    result["market_at_arrival"] = _notouch_arm_reward(
        "market_at_arrival",
        maa_fill,
        arrival_open_ms=arrival_open_ms,
        position_expiry_ms=position_expiry_ms,
        atr=atr,
        close=close,
        **_arm_kwargs,
    )

    vwap_fill = vwap_arrival_fill(
        list(ordered_bars),
        arrival_open_ms=arrival_open_ms,
    )
    result["vwap_arrival"] = _notouch_arm_reward(
        "vwap_arrival",
        vwap_fill,
        arrival_open_ms=arrival_open_ms,
        position_expiry_ms=position_expiry_ms,
        atr=atr,
        close=close,
        **_arm_kwargs,
    )

    return result


__all__ = [
    "ENTRY_GRID_ARMS",
    "TIE_BREAK_SL_FIRST",
    "LadderOutcome",
    "LevelCrossing",
    "parse_ladder",
    "replay_entry_grid",
    "replay_ladder",
]
