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

Since #1404 the refusals are split by WHOSE invariant each one is, and only two
of the three kinds live here:

* **This module owns the operator's VOCABULARY** — the ``price[:alloc]`` and
  ``<N>R:pct`` mini-DSLs, the ``now@`` prefix, ``--no-tp`` against ``--tp``, the
  ``--size-pct``/``--notional`` XOR. None of it is representable in a
  ``TradeIntent``: after compilation ``alloc_pct`` is always set, so "a mix of
  bare and explicit allocations" is a fact about the command line, not the
  document.
* **This module also owns rules about the INVOCATION** — ``--ttl-days`` and
  :data:`SUPPORTED_MICS`. ``order_ttl_days == 0`` is a LEGAL document value
  (``brokers/execution.py`` resolves that sentinel to a default), so refusing the
  flag is right and refusing the document would be wrong. The venue list is Saxo
  deployment knowledge and stays with the adapter (the #1122 decision).
* **The DOCUMENT's own invariants moved to**
  :func:`broker_contract.trade_intent.validate.validate_intent` — allocations
  summing to 100, at most one immediate tier and it listed first, duplicate
  levels, the stop below the ladder, an unlevered size, take-profits above the
  planned blend. They are checked on the assembled intent at the end of
  :func:`build_manual_intent`, and their wording now lives in the contract so
  each message exists in exactly one place.

Three of those rules are ALSO checked here, and not as a duplicated policy: a
positive tier price, a non-empty ladder, and a stop below the lowest tier are
preconditions of this module's own compilation. The planned blend is computed —
and ``<N>R`` take-profits are priced off it — before the intent exists, so
without them ``min()`` would raise on an empty ladder, the blend would go
``None``, or ``risk = blend - stop`` would go non-positive and an R-form
take-profit would be silently priced BELOW the blend.
"""

from __future__ import annotations

import datetime as dt
import math

from broker_contract.failure import Failure
from broker_contract.trade_intent.schema import (
    EntryTierSpec,
    InstrumentHint,
    IntentMeta,
    TpTrancheSpec,
    TradeIntent,
    TradeSpec,
)
from broker_contract.trade_intent.validate import IntentInvalidError, validate_intent

from alphalens_pipeline.paper.sizing import planned_blended_entry_from_spec

# US venues plus GPW (#1238 PR 7) plus Xetra (#1271 PR 4) plus Euronext
# Paris (#1355 PR-C — XPAR opens after the arc landed the venue map entry
# XPAR -> PAR, the MIC-keyed EUR 2 Euronext fee card and the tracked stream
# venue window XNYS,XWAR,XETR,XPAR). XAMS stays refused until its own
# validation arc (map entry + fee card only). LIVE on a European venue
# additionally needs a verified market-data entitlement for that venue — a
# delayed quote is vetoed by the live feed and `any_delayed` is process-wide
# (Euronext Paris: MISSING as of 2026-09-07, KER/ALO delayed-15 during the
# open session; `scripts/probe_saxo_live_entitlement.py` re-reads it) — see
# the runbook notes in deploy/systemd/README.md.
SUPPORTED_MICS = ("XNYS", "XNAS", "XWAR", "XETR", "XPAR")


class ManualIntentError(ValueError):
    """A manual pick's levels/sizing cannot be compiled into a TradeIntent.

    ``failure`` carries the contract's :class:`~broker_contract.failure.Failure`
    when the refusal came from :func:`validate_intent`, so the CLI can publish
    its ``details["reason"]`` instead of handing a machine a sentence to parse.
    It is ``None`` for the vocabulary refusals this module still owns.
    """

    def __init__(self, message: str, *, failure: Failure | None = None) -> None:
        super().__init__(message)
        self.failure = failure


class UnsupportedVenueError(ManualIntentError):
    """The MIC names a venue this deployment does not trade.

    Its own class, not a message to match, because the CLI classifies by
    exception type: this is ``venue_unsupported`` while every other compile
    failure is ``intent_invalid``. The distinction is real — the document is
    well formed and the DEPLOYMENT is the thing that cannot take it, which is
    also why the venue list stayed out of ``validate_intent`` (#1404, and the
    #1122 rule that the adapter reports while the contract never decides).
    """


def ensure_supported_venue(mic: str) -> None:
    """Refuse a venue outside :data:`SUPPORTED_MICS`.

    Split out of :func:`build_manual_intent` so the raw-intent door (#1406)
    applies the identical rule and the identical message without copying the
    list — the door receives a compiled document and never calls the builder.
    """
    if mic not in SUPPORTED_MICS:
        raise UnsupportedVenueError(
            f"MIC {mic!r} is not supported (supported: {', '.join(SUPPORTED_MICS)}; "
            "XAMS awaits its own validation arc, #1238)"
        )


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
    return parsed


def parse_entry_tiers(raw_tiers: list[str] | tuple[str, ...]) -> tuple[EntryTierSpec, ...]:
    """Parse repeated ``--tier price[:alloc_pct]`` values into entry tiers.

    An ALL-BARE ladder (prices only — the WhatsApp signal shape
    ``t1:GME@17.90 t2:GME@17.00 t3:GME@16.20``) splits the allocation equally;
    the compiled-intent echo surfaces the split for verification. When any
    tier carries an explicit allocation, every tier must — that mix is a fact
    about the command line and so is refused here.

    A ``now@<cap>[:alloc_pct]`` tier (#1247) marks the immediate-entry tranche
    and participates in the allocation arithmetic exactly like any other tier.

    What this function no longer checks, because it is true of the DOCUMENT and
    is enforced by ``validate_intent`` on the assembled intent (#1404): the
    allocations summing to 100, at most one immediate tier and it listed first,
    and duplicate tier prices. A non-empty ladder and a positive price stay,
    because the blend arithmetic downstream depends on both.
    """
    if not raw_tiers:
        raise ManualIntentError("at least one --tier is required")

    parsed = [_parse_one_tier(raw) for raw in raw_tiers]
    parsed = _resolve_tier_allocations(parsed, raw_tiers)
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
    if level_raw and level_raw[-1] in ("R", "r"):
        r_multiple = _parse_float(level_raw[:-1], what=f"--tp R-multiple in {raw!r}")
        if r_multiple <= 0:
            raise ManualIntentError(f"--tp R-multiple must be positive, got {raw!r}")
        price = blend + r_multiple * risk
    else:
        price = _parse_float(level_raw, what=f"--tp price in {raw!r}")
        r_multiple = (price - blend) / risk if risk else 0.0
    return TpTrancheSpec(price=price, tranche_pct=pct, r_multiple=r_multiple, tag=f"TP{index + 1}")


def parse_tp_tranches(
    raw_tps: list[str] | tuple[str, ...], *, blend: float, stop: float
) -> tuple[TpTrancheSpec, ...]:
    """Parse repeated ``--tp price:pct`` / ``--tp <N>R:pct`` values.

    Both forms compile to an absolute price plus an ``r_multiple`` label,
    anchored on the PLANNED alloc-weighted blend entry (consistent with the
    ``atr_bracket_1p5_planned`` replay convention): one R is
    ``blend - stop``. Forms can be mixed across tranches.

    Percentages summing over 100, duplicate targets and a target at or below the
    blend are invariants of the DOCUMENT and moved to ``validate_intent`` (#1404);
    what stays here is the parse and the R-form's own positivity rule.
    """
    risk = blend - stop
    tranches = [
        _parse_one_tp(raw, index=index, blend=blend, risk=risk) for index, raw in enumerate(raw_tps)
    ]

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
    ``notional`` (account currency; divided by ``frame``) must be given.

    The resolved value must land in (0, 100] — a pick is never levered — but that
    is an invariant of the DOCUMENT, so ``validate_intent`` enforces it on the
    assembled intent (#1404) rather than this function. What stays here is the
    vocabulary: which flags may be combined, and that a notional needs a frame.
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
    generation: int = 1,
) -> TradeIntent:
    """Compile operator-provided levels into a full manual :class:`TradeIntent`.

    ``intent_id`` is ``TICKER:<arm_date>:manual`` for the first generation and
    ``TICKER:<arm_date>:manual-g<N>`` for a same-day re-arm (#1371): the picks
    fold keys on (ticker, date, generation), so a corrected pick armed after a
    `disarm` is a NEW pick with new watch crids — never a replacement the drain
    would join to the retired generation's submission and skip.
    """
    ticker = ticker.strip().upper()
    ensure_supported_venue(mic)
    if stop <= 0:
        raise ManualIntentError(f"stop must be positive, got {stop:g}")
    if no_tp and tps_raw:
        raise ManualIntentError("cannot pass --no-tp together with --tp")
    if not no_tp and not tps_raw:
        raise ManualIntentError("either --tp or --no-tp is required")
    if ttl_days is not None and ttl_days <= 0:
        raise ManualIntentError(f"ttl_days must be positive, got {ttl_days}")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise ManualIntentError(f"generation must be an int >= 1, got {generation!r}")

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
    generation_suffix = "" if generation == 1 else f"-g{generation}"
    intent = TradeIntent(
        intent_id=f"{ticker}:{arm_date.isoformat()}:manual{generation_suffix}",
        instrument=InstrumentHint(ticker=ticker, mic=mic),
        spec=spec,
        meta=IntentMeta(
            armed_ts=armed_ts,
            trade_date=arm_date.isoformat(),
            source="manual",
            generation=generation,
        ),
        exit=None,
    )
    try:
        validate_intent(intent)
    except IntentInvalidError as exc:
        # The contract owns the rule AND its wording, so the operator message
        # exists in exactly one place. `ManualIntentError` stays the CLI-facing
        # type so `broker.py` keeps reporting `intent_invalid` unchanged.
        raise ManualIntentError(exc.failure.message, failure=exc.failure) from exc
    return intent
