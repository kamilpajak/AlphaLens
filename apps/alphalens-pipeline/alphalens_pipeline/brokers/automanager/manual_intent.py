"""Pure builder behind `alphalens broker arm-manual` (#1235).

Compiles operator vocabularies — the ``price[:alloc]`` tier mini-DSL, TP
tranches given as absolute prices or R-multiples, and sizing given as either a
percent of the declared frame or an account-currency notional — into the one
normal form the wire contract already speaks (`TradeSpec` with absolute prices
and 0-100 percentages). No I/O, no typer: the CLI command stays a thin shell
and every rule here is testable without a runner.

The intent arms with ``exit=None`` on purpose: the daemon's stop management is
a daemon-wide policy (``ALPHALENS_BROKER_EXIT_POLICY``, `breakeven_trail`
since #1183), and the ``exit`` geometry leaf only feeds geometry-applying
policies. Placement falls back to the intent's own static disaster stop and
tranche TP levels — exactly the manual pick's meaning. Per-pick policy
override is #1236, not this module.

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

# v1 supports US venues only (#1235 decision 2026-09-02). XWAR / XAMS are
# planned follow-ups: the calendar helper is already MIC-parametrized, but the
# daemon's routing (`resolve_us_instrument`) and price feed are US-pinned, so
# an unsupported MIC must refuse here rather than arm an intent the daemon
# cannot route.
SUPPORTED_MICS = ("XNYS", "XNAS")

# Percentage sums are validated against float noise only (33.3+33.3+33.4 !=
# 100.0 exactly) — NEVER against sloppy input; 60+30 is a refusal, not a
# rescale.
_PCT_SUM_TOL = 1e-6


class ManualIntentError(ValueError):
    """A manual pick's levels/sizing cannot be compiled into a TradeIntent."""


def _parse_float(raw: str, *, what: str) -> float:
    try:
        value = float(raw)
    except ValueError:
        raise ManualIntentError(f"cannot parse {what}: {raw!r}") from None
    if not math.isfinite(value):
        raise ManualIntentError(f"cannot parse {what}: {raw!r}")
    return value


def parse_entry_tiers(raw_tiers: list[str] | tuple[str, ...]) -> tuple[EntryTierSpec, ...]:
    """Parse repeated ``--tier price[:alloc_pct]`` values into entry tiers.

    A single bare ``price`` means 100% allocation. With multiple tiers every
    one must carry an explicit allocation, and the allocations must sum to
    100 (float-noise tolerance only).
    """
    if not raw_tiers:
        raise ManualIntentError("at least one --tier is required")

    tiers: list[EntryTierSpec] = []
    for index, raw in enumerate(raw_tiers):
        parts = raw.split(":")
        if len(parts) == 1:
            if len(raw_tiers) > 1:
                raise ManualIntentError(
                    f"every --tier needs price:alloc_pct when more than one is given, got {raw!r}"
                )
            price_raw, alloc_raw = parts[0], "100"
        elif len(parts) == 2:
            price_raw, alloc_raw = parts
        else:
            raise ManualIntentError(f"cannot parse --tier: {raw!r} (expected price[:alloc_pct])")
        price = _parse_float(price_raw, what="--tier")
        alloc = _parse_float(alloc_raw, what="--tier")
        if price <= 0:
            raise ManualIntentError(f"--tier price must be positive, got {raw!r}")
        if alloc <= 0:
            raise ManualIntentError(f"--tier alloc_pct must be positive, got {raw!r}")
        tiers.append(EntryTierSpec(limit_price=price, alloc_pct=alloc, tag=f"T{index + 1}"))

    alloc_sum = sum(t.alloc_pct for t in tiers)
    if abs(alloc_sum - 100.0) > _PCT_SUM_TOL:
        raise ManualIntentError(
            f"--tier allocations must sum to 100, got {alloc_sum:g} — no silent rescaling"
        )
    return tuple(tiers)


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
    tranches: list[TpTrancheSpec] = []
    for index, raw in enumerate(raw_tps):
        parts = raw.split(":")
        if len(parts) != 2:
            raise ManualIntentError(f"cannot parse --tp: {raw!r} (expected price:pct or <N>R:pct)")
        level_raw, pct_raw = parts
        pct = _parse_float(pct_raw, what="--tp")
        if pct <= 0:
            raise ManualIntentError(f"--tp tranche_pct must be positive, got {raw!r}")
        if level_raw and level_raw[-1] in ("R", "r"):
            r_multiple = _parse_float(level_raw[:-1], what="--tp")
            if r_multiple <= 0:
                raise ManualIntentError(f"--tp R-multiple must be positive, got {raw!r}")
            price = blend + r_multiple * risk
        else:
            price = _parse_float(level_raw, what="--tp")
            if price <= blend:
                raise ManualIntentError(
                    f"--tp price must be above the planned blend entry {blend:g}, got {raw!r}"
                )
            r_multiple = (price - blend) / risk
        tranches.append(
            TpTrancheSpec(price=price, tranche_pct=pct, r_multiple=r_multiple, tag=f"TP{index + 1}")
        )

    pct_sum = sum(t.tranche_pct for t in tranches)
    if pct_sum - 100.0 > _PCT_SUM_TOL:
        raise ManualIntentError(f"--tp tranche percentages exceed 100, got {pct_sum:g}")
    return tuple(tranches)


def planned_blended_entry_of(tiers: tuple[EntryTierSpec, ...], *, disaster_stop: float) -> float:
    """Alloc-weighted planned blend over the manual tiers.

    Delegates to :func:`alphalens_pipeline.paper.sizing.
    planned_blended_entry_from_spec` (via a provisional spec) so the manual
    path can never drift from the blend the daemon's geometry shadow and the
    replay lenses compute — blend divergence is exactly the SMG class of bug
    (issue #1114).
    """
    from alphalens_pipeline.paper.sizing import planned_blended_entry_from_spec

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
            f"MIC {mic!r} is not supported in v1 (supported: {', '.join(SUPPORTED_MICS)}; "
            "XWAR / XAMS are planned follow-ups, #1235)"
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
        meta=IntentMeta(armed_ts=armed_ts, brief_date=arm_date.isoformat(), source="manual"),
        exit=None,
    )
