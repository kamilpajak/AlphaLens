"""Pure builder behind `alphalens broker arm-manual` (#1235).

Compiles operator vocabularies — the ``price[:alloc]`` tier mini-DSL, TP
tranches given as absolute prices or R-multiples, and sizing given as either a
percent of the declared frame or an account-currency notional — into the one
normal form the wire contract already speaks (`TradeSpec` with absolute prices
and 0-100 percentages). No I/O, no typer: the CLI command stays a thin shell
and every rule here is testable without a runner.

The intent arms with ``exit=None`` on purpose, and that choice does MORE than
pick the placed levels — issue #1325. The ``exit`` leaf feeds two consumers:
the geometry actually placed (only for an ``applies_geometry`` policy), and the
``geometry`` shadow stamp on the ``planned`` journal line, which is the only
source of ``PlannedExit.reanchor``. Both post-fill stop-move arms
(``position_manager._maybe_reanchor`` and ``_maybe_trail``) refuse when that is
``None``, so a manual pick is POLICY-IMMUNE: whatever
``ALPHALENS_BROKER_EXIT_POLICY`` names, the daemon places the intent's own
static disaster stop and tranche TP levels and never moves the stop again.

One route does NOT pass that guard, so it is worth naming here rather than
leaving for someone to rediscover: ``ProtectionView.trailed_stop_by_uic`` is a
journal-lifetime fold, and ``control_loop._build_managed_exits`` takes
``max(plan stop, trailed)`` and PLACES it. A level earned by an earlier
position on the same uic can therefore outlive it. It still cannot reach a
manual pick, for two different reasons: a pick with TP tranches journals its
own ``tranche_plan`` under a new ``pick_key``, which resets the fold; and a
pick armed ``--no-tp`` journals no ``tranche_plan`` at all, so the builder
skips its uic. Both are pinned in the test module named below.

That is the intended behaviour, decided 2026-09-05 on #1325: the exit of a
group-managed pick is a human decision, and the trail's 0.5R activation is
meaningless across manual picks anyway because ``1R = avg_price - plan_stop``
is a hand-set number (6.8% of entry on AMBA, 29% on RHI). Pinned end-to-end by
``tests/brokers/automanager/test_manual_pick_no_stop_move.py`` — if this module
ever starts building an exit spec, that suite goes red BEFORE real money starts
trailing. Opting a single pick INTO a trailing policy is #1236, not this module.

Every refusal raises :class:`ManualIntentError` with an operator-readable
message. This command arms real money: a malformed level must explode loudly,
never be silently normalized (no alloc rescaling, no tolerance beyond float
noise).
"""

from __future__ import annotations

import datetime as dt
import math

from broker_contract.trade_intent.schema import (
    EntryTierSpec,
    InstrumentHint,
    IntentMeta,
    TpTrancheSpec,
    TradeIntent,
    TradeSpec,
)

from alphalens_pipeline.paper.sizing import planned_blended_entry_from_spec

# US venues plus GPW (#1238 PR 7) plus Xetra (#1271 PR 4 — XETR opens after
# the arc landed the venue map + RHM alias, the MIC-keyed fee cards with the
# Xetra EUR 3 minimum, and the tracked stream venue window XNYS,XWAR,XETR).
# XAMS stays refused until its own validation arc (map entry + fee card only).
# LIVE on a European venue additionally needs a verified market-data
# entitlement for that venue — a delayed quote is vetoed by the live feed and
# `any_delayed` is process-wide — see the runbook notes in
# deploy/systemd/README.md.
SUPPORTED_MICS = ("XNYS", "XNAS", "XWAR", "XETR")

# Percentage sums are validated against float noise only (33.3+33.3+33.4 !=
# 100.0 exactly) — NEVER against sloppy input; 60+30 is a refusal, not a
# rescale.
_PCT_SUM_TOL = 1e-6


class ManualIntentError(ValueError):
    """A manual pick's levels/sizing cannot be compiled into a TradeIntent."""


# Immediate-entry tier prefix (#1247): ``now@<cap>[:alloc_pct]``. The cap is
# the operator's max acceptable fill — the daemon places a capped LIMIT at
# drain instead of a resting pullback rung.
_NOW_PREFIX = "now@"


def _parse_float(raw: str, *, what: str) -> float:
    try:
        value = float(raw)
    except ValueError:
        raise ManualIntentError(f"cannot parse {what}: {raw!r}") from None
    if not math.isfinite(value):
        raise ManualIntentError(f"cannot parse {what}: {raw!r}")
    return value


def _parse_one_tier(raw: str) -> tuple[float, float | None, bool]:
    """Parse one ``price[:alloc_pct]`` / ``now@<cap>[:alloc_pct]`` value into
    ``(price, alloc_pct-or-None, is_now)``."""
    parts = raw.split(":")
    if len(parts) == 1:
        price_raw, alloc_raw = parts[0], None
    elif len(parts) == 2:
        price_raw, alloc_raw = parts
    else:
        raise ManualIntentError(f"cannot parse --tier: {raw!r} (expected price[:alloc_pct])")
    is_now = price_raw.startswith(_NOW_PREFIX)
    if is_now:
        price_raw = price_raw[len(_NOW_PREFIX) :]
        if not price_raw:
            raise ManualIntentError(
                f"a now tier needs a cap price, got {raw!r} (expected now@<cap>[:alloc_pct])"
            )
    price = _parse_float(price_raw, what=f"--tier price in {raw!r}")
    if price <= 0:
        raise ManualIntentError(f"--tier price must be positive, got {raw!r}")
    alloc: float | None = None
    if alloc_raw is not None:
        alloc = _parse_float(alloc_raw, what=f"--tier alloc_pct in {raw!r}")
        if alloc <= 0:
            raise ManualIntentError(f"--tier alloc_pct must be positive, got {raw!r}")
    return price, alloc, is_now


def _resolve_tier_allocations(
    parsed: list[tuple[float, float | None, bool]], raw_tiers: list[str] | tuple[str, ...]
) -> list[tuple[float, float | None, bool]]:
    """Apply the all-bare equal split, or validate explicit allocations."""
    n_bare = sum(1 for _, alloc, _ in parsed if alloc is None)
    if n_bare == len(parsed):
        return [(price, 100.0 / len(parsed), is_now) for price, _, is_now in parsed]
    if n_bare:
        raise ManualIntentError(
            "either every --tier carries an explicit alloc_pct or none does "
            f"(equal split) — got a mix in {list(raw_tiers)!r}"
        )
    alloc_sum = sum(alloc for _, alloc, _ in parsed if alloc is not None)
    if abs(alloc_sum - 100.0) > _PCT_SUM_TOL:
        raise ManualIntentError(
            f"--tier allocations must sum to 100, got {alloc_sum:g} — no silent rescaling"
        )
    return parsed


def parse_entry_tiers(raw_tiers: list[str] | tuple[str, ...]) -> tuple[EntryTierSpec, ...]:
    """Parse repeated ``--tier price[:alloc_pct]`` values into entry tiers.

    An ALL-BARE ladder (prices only — the WhatsApp signal shape
    ``t1:GME@17.90 t2:GME@17.00 t3:GME@16.20``) splits the allocation equally;
    the compiled-intent echo surfaces the split for verification. When any
    tier carries an explicit allocation, every tier must, and the allocations
    must sum to 100 (float-noise tolerance only — no silent rescaling).

    A ``now@<cap>[:alloc_pct]`` tier (#1247) marks the immediate-entry
    tranche: at most one per pick, and it must be listed FIRST (the day-1
    gate and the watch router key on the first PULLBACK tier — a now tier
    hiding mid-ladder would corrupt both). It participates in the allocation
    arithmetic exactly like any other tier.
    """
    if not raw_tiers:
        raise ManualIntentError("at least one --tier is required")

    parsed = [_parse_one_tier(raw) for raw in raw_tiers]

    now_indexes = [index for index, (_, _, is_now) in enumerate(parsed) if is_now]
    if len(now_indexes) > 1:
        raise ManualIntentError(
            "at most one now tier per pick — a second 'now' is a new signal, re-arm"
        )
    if now_indexes and now_indexes[0] != 0:
        raise ManualIntentError("the now tier must be listed first")

    parsed = _resolve_tier_allocations(parsed, raw_tiers)

    prices = [price for price, _, _ in parsed]
    if len(set(prices)) != len(prices):
        # The same price twice is almost certainly a pasted-twice typo — the
        # deeper rung of such a ladder can never fill separately (and a now
        # cap colliding with a pullback rung is the same typo class).
        raise ManualIntentError(f"duplicate --tier price: {prices}")
    return tuple(
        EntryTierSpec(
            limit_price=price,
            alloc_pct=alloc,
            tag=f"T{index + 1}",
            entry_mode="immediate" if is_now else "pullback",
        )
        for index, (price, alloc, is_now) in enumerate(parsed)
        if alloc is not None
    )


def _parse_one_tp(raw: str, *, index: int, blend: float, risk: float) -> TpTrancheSpec:
    """Parse one ``price:pct`` / ``<N>R:pct`` value into a tranche.

    Both forms compile to an absolute price plus an ``r_multiple`` label
    anchored on the planned blend entry (one R is ``blend - stop``)."""
    parts = raw.split(":")
    if len(parts) != 2:
        raise ManualIntentError(f"cannot parse --tp: {raw!r} (expected price:pct or <N>R:pct)")
    level_raw, pct_raw = parts
    pct = _parse_float(pct_raw, what=f"--tp tranche_pct in {raw!r}")
    if pct <= 0:
        raise ManualIntentError(f"--tp tranche_pct must be positive, got {raw!r}")
    if level_raw and level_raw[-1] in ("R", "r"):
        r_multiple = _parse_float(level_raw[:-1], what=f"--tp R-multiple in {raw!r}")
        if r_multiple <= 0:
            raise ManualIntentError(f"--tp R-multiple must be positive, got {raw!r}")
        price = blend + r_multiple * risk
    else:
        price = _parse_float(level_raw, what=f"--tp price in {raw!r}")
        if price <= blend:
            raise ManualIntentError(
                f"--tp price must be above the planned blend entry {blend:g}, got {raw!r}"
            )
        r_multiple = (price - blend) / risk
    return TpTrancheSpec(price=price, tranche_pct=pct, r_multiple=r_multiple, tag=f"TP{index + 1}")


def parse_tp_tranches(
    raw_tps: list[str] | tuple[str, ...], *, blend: float, stop: float
) -> tuple[TpTrancheSpec, ...]:
    """Parse repeated ``--tp price:pct`` / ``--tp <N>R:pct`` values.

    Both forms compile to an absolute price plus an ``r_multiple`` label,
    anchored on the PLANNED alloc-weighted blend entry (consistent with the
    ``atr_bracket_1p5_planned`` replay convention): one R is
    ``blend - stop``. Forms can be mixed across tranches.
    """
    risk = blend - stop
    tranches = [
        _parse_one_tp(raw, index=index, blend=blend, risk=risk) for index, raw in enumerate(raw_tps)
    ]

    pct_sum = sum(t.tranche_pct for t in tranches)
    if pct_sum - 100.0 > _PCT_SUM_TOL:
        raise ManualIntentError(f"--tp tranche percentages exceed 100, got {pct_sum:g}")
    prices = [t.price for t in tranches]
    if len(set(prices)) != len(prices):
        # Two tranches at one target are one bigger tranche at best and a
        # pasted-twice typo at worst (an R-form can land exactly on a given
        # absolute target) — refuse either way.
        raise ManualIntentError(f"duplicate --tp price: {prices}")
    return tuple(tranches)


def planned_blended_entry_of(tiers: tuple[EntryTierSpec, ...], *, disaster_stop: float) -> float:
    """Alloc-weighted planned blend over the manual tiers.

    Delegates to :func:`alphalens_pipeline.paper.sizing.
    planned_blended_entry_from_spec` (via a provisional spec) so the manual
    path can never drift from the blend the daemon's geometry shadow and the
    replay lenses compute — blend divergence is exactly the SMG class of bug
    (issue #1114).

    An immediate ("now") tier's cap participates in the blend like any other
    tier: the cap is that allocation's worst-case PLANNED entry, the same
    epistemic status as a pullback rung's limit (#1247 memo D2). Excluding it
    would fork the blend arithmetic and leave a now-only pick with no blend
    for R-form take-profits.
    """
    provisional = TradeSpec(
        entry_tiers=tiers,
        disaster_stop=disaster_stop,
        tp_tranches=(),
        suggested_size_pct=1.0,
    )
    blend = planned_blended_entry_from_spec(provisional)
    if blend is None:  # unreachable: parse_entry_tiers guarantees priced tiers
        raise ManualIntentError("entry tiers yield no planned blend")
    return blend


def resolve_size_pct(
    *, size_pct: float | None, notional: float | None, frame: float | None
) -> float:
    """Resolve the two sizing vocabularies into one ``suggested_size_pct``.

    Exactly one of ``size_pct`` (percent of the declared frame) or
    ``notional`` (account currency; divided by ``frame``) must be given. The
    result must land in (0, 100] — a manual pick is never levered.
    """
    if (size_pct is None) == (notional is None):
        raise ManualIntentError("exactly one of --size-pct or --notional is required")
    if size_pct is None:
        assert notional is not None  # the XOR check above guarantees it
        if notional <= 0:
            raise ManualIntentError(f"--notional must be positive, got {notional:g}")
        if frame is None:
            raise ManualIntentError(
                "--notional needs the declared frame (pass --frame or set the sizing-equity env)"
            )
        if frame <= 0:
            raise ManualIntentError(f"--frame must be positive, got {frame:g}")
        size_pct = 100.0 * notional / frame
    if not 0.0 < size_pct <= 100.0:
        raise ManualIntentError(
            f"resolved sizing must satisfy 0 < size_pct <= 100, got {size_pct:g}"
        )
    return size_pct


def build_manual_intent(
    *,
    ticker: str,
    mic: str,
    tiers_raw: list[str] | tuple[str, ...],
    stop: float,
    tps_raw: list[str] | tuple[str, ...],
    no_tp: bool,
    size_pct: float | None,
    notional: float | None,
    frame: float | None,
    ttl_days: int | None,
    arm_date: dt.date,
    armed_ts: str,
) -> TradeIntent:
    """Compile operator-provided levels into a full manual :class:`TradeIntent`.

    ``intent_id`` is ``TICKER:<arm_date>:manual`` — the picks fold keys on
    (ticker, date) with latest-wins, so re-arming the same ticker the same day
    REPLACES the pending intent (the typo-fix path), it never duplicates it.
    """
    ticker = ticker.strip().upper()
    if not ticker:
        raise ManualIntentError("ticker must be non-empty")
    if mic not in SUPPORTED_MICS:
        raise ManualIntentError(
            f"MIC {mic!r} is not supported (supported: {', '.join(SUPPORTED_MICS)}; "
            "XAMS awaits its own validation arc, #1238)"
        )
    if stop <= 0:
        raise ManualIntentError(f"stop must be positive, got {stop:g}")
    if no_tp and tps_raw:
        raise ManualIntentError("cannot pass --no-tp together with --tp")
    if not no_tp and not tps_raw:
        raise ManualIntentError("either --tp or --no-tp is required")
    if ttl_days is not None and ttl_days <= 0:
        raise ManualIntentError(f"ttl_days must be positive, got {ttl_days}")

    tiers = parse_entry_tiers(tiers_raw)
    lowest_tier = min(t.limit_price for t in tiers)
    if stop >= lowest_tier:
        raise ManualIntentError(
            f"stop {stop:g} must sit below every entry tier (lowest tier {lowest_tier:g})"
        )

    blend = planned_blended_entry_of(tiers, disaster_stop=stop)
    tranches = () if no_tp else parse_tp_tranches(tps_raw, blend=blend, stop=stop)

    resolved_size_pct = resolve_size_pct(size_pct=size_pct, notional=notional, frame=frame)

    spec_kwargs: dict = {}
    if ttl_days is not None:
        spec_kwargs["order_ttl_days"] = ttl_days
    spec = TradeSpec(
        entry_tiers=tiers,
        disaster_stop=stop,
        tp_tranches=tranches,
        suggested_size_pct=resolved_size_pct,
        **spec_kwargs,
    )
    return TradeIntent(
        intent_id=f"{ticker}:{arm_date.isoformat()}:manual",
        instrument=InstrumentHint(ticker=ticker, mic=mic),
        spec=spec,
        meta=IntentMeta(armed_ts=armed_ts, trade_date=arm_date.isoformat(), source="manual"),
        exit=None,
    )
