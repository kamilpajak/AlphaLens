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
:class:`LadderOutcome`. Enumeration, Polygon fetch and the feedback-ledger write
live in the caller (``ladder_backfill``).
"""

from __future__ import annotations

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
    atr = float(raw_atr) if raw_atr is not None else None
    return _ParsedLadder(
        ok=True,
        entries=entries,
        tps=tps,
        disaster_stop=float(stop),
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
    seq: list[LevelCrossing] = []
    filled: list[_Level] = []
    filled_ids: set[str] = set()
    hit_tp_ids: set[str] = set()
    ambiguous_bars = 0
    sl_hit = False
    time_stop = False  # synthetic position time-stop fired (terminal TIME_STOP)
    expiry_close: float | None = None  # close of the bar the time-stop fired on
    exit_reached = False  # the as-specified exit (first SL, full TP, or time-stop) has fired
    last_close: float | None = None
    in_trade_high: float | None = None  # highest high since first fill
    in_trade_low: float | None = None  # lowest low since first fill

    for bar in ordered:
        ts, low, high, close = _bar_lhc(bar)
        last_close = close  # advances EVERY bar (whole-horizon forward_return)

        # Once the as-specified position has exited (full SL, or all TPs taken)
        # it is FLAT: no new entry fills, no in-trade excursion, no further exit
        # resolution. ``last_close`` still advances above so ``forward_return``
        # spans the whole horizon. (zen HIGH: a post-exit dip must NOT fill an
        # unused deeper tier and retroactively change the blended entry /
        # filled_frac / realized_r of an already-closed position, nor extend the
        # MFE/MAE window past the actual holding period.)
        if exit_reached:
            continue

        # 1) Entries first (you cannot exit before entering). Multiple tiers can
        #    fill in one bar (a gap-down through several limits).
        for lvl in ladder.entries:
            # Entry-TTL: a limit touched at/after the cutoff is a stale entry and
            # does NOT fill (it only blocks NEW fills; the TP/SL block below still
            # resolves an already-open position).
            if entry_expiry_ms is not None and ts >= entry_expiry_ms:
                break
            if lvl.level_id not in filled_ids and low <= lvl.price:
                filled.append(lvl)
                filled_ids.add(lvl.level_id)
                seq.append(LevelCrossing(lvl.level_id, _ENTRY, lvl.price, ts))

        if not filled:
            continue  # no position yet -> TP/SL/excursion cannot trigger

        # Track in-trade excursion over every HELD bar (first fill until the
        # as-specified exit). forward_return carries the whole-horizon directional
        # signal; MFE/MAE measure only what happened while the position was open.
        in_trade_high = high if in_trade_high is None else max(in_trade_high, high)
        in_trade_low = low if in_trade_low is None else min(in_trade_low, low)

        # 2) Resolve the as-specified exit for this bar.
        sl_cross = low <= stop
        tp_crosses = [t for t in ladder.tps if t.level_id not in hit_tp_ids and high >= t.price]

        if sl_cross and tp_crosses:
            # Ambiguous: conservative SL-first. Record SL, mark exit.
            ambiguous_bars += 1
            seq.append(LevelCrossing("SL", _SL, stop, ts, same_bar_ambiguous=True))
            sl_hit = True
            exit_reached = True
            continue
        if sl_cross:
            # Bug #3: an entry that filled THIS bar AND a stop pierced THIS bar is
            # ambiguous (we cannot order entry-then-stop vs stop intra-bar). Flag
            # it SL-first when a fresh entry landed in the same bar.
            entered_this_bar = any(c.bar_ts_ms == ts and c.kind == _ENTRY for c in seq)
            if entered_this_bar:
                ambiguous_bars += 1
                seq.append(LevelCrossing("SL", _SL, stop, ts, same_bar_ambiguous=True))
            else:
                seq.append(LevelCrossing("SL", _SL, stop, ts))
            sl_hit = True
            exit_reached = True
            continue
        for t in tp_crosses:  # ascending; record each tranche hit this bar
            hit_tp_ids.add(t.level_id)
            seq.append(LevelCrossing(t.level_id, _TP, t.price, ts))
        if len(hit_tp_ids) == len(ladder.tps) and ladder.tps:
            exit_reached = True  # fully scaled out via TPs

        # 3) Time-stop LAST: only when the as-specified exit did NOT fire on this
        #    bar (a real SL/TP on the cutoff bar WINS over the synthetic stop).
        #    Marks the remainder to THIS bar's close and ends the in-trade window.
        # ``filled`` is guaranteed non-empty here (the ``if not filled: continue``
        # guard above), so it is not re-checked.
        if not exit_reached and position_expiry_ms is not None and ts >= position_expiry_ms:
            # TIME_STOP records the BAR's CLOSE as ``price`` (not a fill level — the
            # remainder is marked to close at expiry). Consumers should read the
            # realized_r / classification, not interpret this cross's price as a
            # trigger level.
            seq.append(LevelCrossing("TIME_STOP", _TIME, close, ts))
            expiry_close = close
            time_stop = True
            exit_reached = True

    blended = _blended_entry(filled) if filled else None
    ratchet_r = (
        _replay_ratchet(ladder, ordered, filled, blended, (blended - stop) if blended else 0.0)
        if filled and blended is not None
        else None
    )

    return _finalize(
        ladder,
        seq,
        filled,
        hit_tp_ids,
        sl_hit,
        ambiguous_bars,
        last_close,
        blended,
        in_trade_high,
        in_trade_low,
        reference_close,
        ratchet_r,
        time_stop=time_stop,
        expiry_close=expiry_close,
    )


def _finalize(
    ladder: _ParsedLadder,
    seq: list[LevelCrossing],
    filled: list[_Level],
    hit_tp_ids: set[str],
    sl_hit: bool,
    ambiguous_bars: int,
    last_close: float | None,
    blended: float | None,
    in_trade_high: float | None,
    in_trade_low: float | None,
    reference_close: float | None,
    ratchet_r: float | None,
    *,
    time_stop: bool = False,
    expiry_close: float | None = None,
) -> LadderOutcome:
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
    have_position = False

    for bar in ordered_bars:
        _ts, low, high, close = _bar_lhc(bar)
        last_close = close
        # Re-derive position state on the SAME fill model so the ratchet walk is
        # self-contained (no shared mutable state with the headline pass).
        for lvl in ladder.entries:
            if lvl.level_id not in filled_ids and low <= lvl.price:
                filled_ids.add(lvl.level_id)
                have_position = True
        if not have_position:
            continue

        sl_cross = low <= eff_stop
        tp_crosses = [t for t in ladder.tps if t.level_id not in hit_tp_ids and high >= t.price]

        if sl_cross:
            # SL-first on ambiguity (a TP also crossable this bar).
            sl_hit = True
            break
        for t in tp_crosses:
            hit_tp_ids.add(t.level_id)
        # Ratchet the effective stop AFTER recording TP hits this bar.
        if "TP1" in hit_tp_ids:
            eff_stop = max(eff_stop, blended)  # break-even+
        if "TP2" in hit_tp_ids:
            eff_stop = max(eff_stop, tp1_price)  # lock-in
        if len(hit_tp_ids) == len(ladder.tps):
            break  # fully scaled out

    filled_frac = _filled_frac(ladder, filled)
    # The remainder exits at the EFFECTIVE stop (not the disaster stop) on an SL.
    contrib, _open = _realized_r_with_frac(
        ladder, hit_tp_ids, blended, eff_stop, risk, sl_hit, last_close, filled_frac
    )
    return contrib


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


__all__ = [
    "TIE_BREAK_SL_FIRST",
    "LadderOutcome",
    "LevelCrossing",
    "parse_ladder",
    "replay_ladder",
]
