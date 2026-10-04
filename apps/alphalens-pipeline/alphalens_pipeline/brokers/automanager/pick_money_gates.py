"""The money gates a pick must clear before any order is placed.

Step 5 of the ``control_loop`` partition. Four gates live here, each answering
one question about whether a pick may spend money:

* **Size** — ``_check_pick_size``: is the declared notional inside the boot
  rail, and in the account currency?
* **Sizing** — ``_resolve_and_size``: what instrument, and how many units?
* **Fee floor** — ``_check_fee_floor`` with ``_estimate_round_trip_fee_bps``:
  would the round trip cost more than the edge the pick claims?
* **Exposure** — ``_check_gross_cap`` and ``_check_cash_floor``, with the
  valuation helpers ``_committed_working_gross_acct``,
  ``_filled_positions_gross_acct``, ``_value_one_position`` and
  ``_make_position_rate_lookup``: does the account have the room?

What moved is the MAXIMAL set of ``control_loop`` definitions the placement
wiring root reaches EXCLUSIVELY, restricted to these four gates, and CLOSED
under every module-level name it uses. Nothing here calls, or names, anything
left behind in ``control_loop``, so there is no back-edge and no import cycle —
measured, not judged.

``control_loop`` reaches everything here through the module prefix
(``pick_money_gates.<name>``) and never by a ``from`` import: a ``from`` import
would give ``control_loop`` its own binding, and a test patching this module
would not reach it.
"""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from broker_contract.contract import Broker

from alphalens_pipeline.brokers.automanager import entry_trails
from alphalens_pipeline.brokers.automanager.costs import (
    COMMISSION_RATE,
    FX_ROUND_TRIP_RATE,
    MIN_COMMISSION_USD,
    US_FEE_CARD,
    fee_card_for,
    round_trip_fee_bps,
)

logger = logging.getLogger(__name__)


def _check_pick_size(size: Any, *, account_currency: str, ticker: str) -> tuple[str, str] | None:
    """``None`` iff the document's amount can be spent here, else
    ``(violation, alert_key)`` for a terminal refusal (#1467).

    Currency first: an amount in another currency cannot be compared with the
    cap, which is in account currency. The cap is
    ``ALPHALENS_BROKER_MAX_PICK_NOTIONAL``: unset means no cap (SIM, the
    ``MAX_FEE_BPS`` precedent; LIVE boots only with it pinned, see
    ``live_rails``), and a value that is not a positive number refuses every
    pick until the unit is fixed (fail closed, like the fee floor)."""
    from alphalens_pipeline.brokers.automanager.live_rails import MAX_PICK_NOTIONAL_ENV

    if size.currency != account_currency:
        return (
            f"{ticker}: the pick is sized in {size.currency} but the account is "
            f"{account_currency} — refused; re-arm it in {account_currency}",
            f"pick-currency:{ticker}",
        )
    raw = os.environ.get(MAX_PICK_NOTIONAL_ENV)
    if raw is None or not raw.strip():
        return None
    try:
        cap = float(raw)
    except ValueError:
        cap = math.nan
    if not math.isfinite(cap) or cap <= 0:
        return (
            f"{MAX_PICK_NOTIONAL_ENV}={raw!r} is not a positive number — {ticker} refused "
            "(fail-closed until the cap is fixed)",
            f"pick-cap:{ticker}",
        )
    if size.notional_acct > cap:
        return (
            f"{ticker}: the pick's amount {size.notional_acct:,.2f} {size.currency} exceeds "
            f"{MAX_PICK_NOTIONAL_ENV}={cap:,.2f} — refused, never shrunk",
            f"pick-cap:{ticker}",
        )
    return None


def _resolve_and_size(
    broker: Broker,
    ticker: str,
    account: Any,
    spec: Any,
    hint_mic: str | None = None,
) -> tuple[Any, Any, Any] | None:
    """Resolve the instrument, build any needed FX conversion, and size the
    already-parsed :class:`~broker_contract.trade_intent.schema.TradeSpec`.
    Returns ``(instrument, fx, plan)`` or ``None`` on any resolve/size failure
    (logged) — one bad pick must never crash a tick.

    ``hint_mic`` is the intent's ``InstrumentHint.mic`` (#1238):
    ``explicit_mic_from_hint`` keeps US hints on the probe path (a legacy brief
    pick hints XNYS while its real venue may be XNAS) and turns a non-US hint
    (an operator's venue on a manual document, e.g. XWAR) into an explicit
    single-venue resolve.

    PR-7 (broker-manager extraction memo §5): the brief-side parse and the
    exit-geometry build moved to arm time, client-side (#1552 later removed the
    brief producer; every pick is a hand-written document) — this helper runs
    only the money half
    (``compute_setup_plan``) on the already-parsed ``spec`` the daemon received
    on the drained ``TradeIntent``. #1414 then retired that builder outright, so
    nothing on this path computes a bracket at all: the document declares how
    its stop is managed and may supply levels. The caller reads ``intent.exit``
    directly for the (possibly ``None``) exit-geometry spec; this helper never
    touches a brief."""
    from broker_contract.contract import BrokerError
    from broker_contract.sizing import TradeSetupNotPlannableError, compute_setup_plan

    from alphalens_pipeline.brokers.execution import build_fx_conversion
    from alphalens_pipeline.brokers.routing import explicit_mic_from_hint, resolve_us_instrument

    try:
        instrument = resolve_us_instrument(
            broker, ticker, exchange_mic=explicit_mic_from_hint(hint_mic)
        )
        if not instrument.currency:
            logger.warning("place_pick %s: resolved with no instrument currency", ticker)
            return None
        fx = None
        if instrument.currency != account.currency:
            get_fx_rate = getattr(broker, "get_fx_rate", None)
            if get_fx_rate is None:
                logger.warning(
                    "place_pick %s: %s vs account %s but broker has no get_fx_rate",
                    ticker,
                    instrument.currency,
                    account.currency,
                )
                return None
            fx = build_fx_conversion(get_fx_rate(account.currency, instrument.currency))
        # #1467: the document states the amount, so nothing here reads a frame.
        plan = compute_setup_plan(spec, fx=fx)
    except (BrokerError, TradeSetupNotPlannableError) as exc:
        logger.warning("place_pick %s: resolve/size failed: %s", ticker, exc)
        return None

    return instrument, fx, plan


def _check_fee_floor(
    plan: Any,
    fx: Any,
    *,
    ticker: str,
    instrument_currency: str = "USD",
    exchange_mic: str | None = None,
) -> str | None:
    """``None`` iff the pick clears the round-trip fee floor OR
    ``ALPHALENS_BROKER_MAX_FEE_BPS`` is unset (SIM — no fee floor, byte-
    identical to pre-fee-floor behavior). Else a refusal message naming the
    ticker, the estimated fee, and the cap (design memo §4) — never feeds
    back into selection (R2), just a fee fact reported to the operator.

    Prices the plan with ``_estimate_round_trip_fee_bps`` — the SAME per-tier
    model already journaled as ``est_round_trip_fee_bps`` on every placement,
    so the gate and the journal can never disagree about the cost of one plan
    (#1123). Every commission minimum below roughly $1,250 per order is a flat
    $1, so at our notionals the estimate is a COUNT of chargeable orders; the
    older aggregate model counted exactly two however deep the ladder was, and
    understated a real 3-tier SMG ladder by 120 bps (110.2 vs 230.7 journaled).

    Falls back to the aggregate ``round_trip_fee_bps`` over
    ``setup_plan_gross_notional`` when the per-tier model returns an honest
    ``None`` (no sized plan / no tiers / zero gross) — the floor must always
    answer, never crash the tick on a degenerate plan. The refusal message
    names which model produced the number so the operator is never guessing.

    NOTE (#1123): the per-tier model assumes ONE chargeable order per tier.
    Saxo charges the minimum per order per EXECUTION DAY, so a tier resting as
    GTD and filling across two days pays twice — neither model expresses that.
    The mirrored exit is likewise an assumption, not a derivation: a
    geometry-policy pick currently places a single 100% tranche."""
    from alphalens_pipeline.brokers.automanager.live_rails import MAX_FEE_BPS_ENV

    max_fee_bps_raw = os.environ.get(MAX_FEE_BPS_ENV)
    if max_fee_bps_raw is None or not max_fee_bps_raw.strip():
        return None
    try:
        max_fee_bps = float(max_fee_bps_raw)
    except ValueError:
        # FAIL-CLOSED: a typo'd cap must never crash the tick and must never
        # silently disable the floor (fail-open). Refuse the pick with a
        # message naming the env var — the operator fixes the unit.
        return (
            f"fee floor: {MAX_FEE_BPS_ENV}={max_fee_bps_raw!r} is not a number — "
            f"{ticker} refused (fail-closed until the cap is fixed)"
        )

    from broker_contract.sizing import setup_plan_gross_notional

    notional = setup_plan_gross_notional(plan)
    fee_bps = _estimate_round_trip_fee_bps(
        plan, fx, instrument_currency=instrument_currency, exchange_mic=exchange_mic
    )
    model = "per-tier"
    if fee_bps is None:
        # FAIL-OPEN, deliberately — and NOT the same class as the malformed-cap
        # branch above, which fails CLOSED. That one is an operator typo in
        # configuration: the floor is live but unreadable, so refusing is the
        # only safe answer. THIS one is a plan that honestly prices to nothing
        # — the only shape that reaches here is an all-zero-qty tier set (gross
        # 0), because `compute_setup_plan` raises TradeSetupNotPlannableError
        # for anything less. Such a plan places NO order: `classify` yields no
        # tiers and `_place_pick` returns before `_place_tiers` ("every entry
        # tier sized to zero shares"). So passing it here costs nothing, and
        # refusing it would attribute the refusal to the fee floor instead of
        # to the sizing that actually produced it. Logged so a LIVE rail never
        # takes this path silently.
        model = "aggregate"
        logger.warning(
            "fee floor: %s — per-tier model could not price the plan (gross %.2f), "
            "falling back to the aggregate model",
            ticker,
            notional,
        )
        fallback_card = fee_card_for(instrument_currency, exchange_mic=exchange_mic)
        fee_bps = round_trip_fee_bps(
            notional,
            fx_applies=fx is not None,
            min_commission_applies=fallback_card is not None or instrument_currency == "USD",
            card=fallback_card if fallback_card is not None else US_FEE_CARD,
        )
    if fee_bps <= max_fee_bps:
        return None
    return (
        f"fee floor: {ticker} round-trip {fee_bps:.1f} bps > cap {max_fee_bps:.1f} bps "
        f"({model} model, notional {notional:,.2f}) — pick refused"
    )


def _estimate_round_trip_fee_bps(
    plan: Any, fx: Any, *, instrument_currency: str = "USD", exchange_mic: str | None = None
) -> float | None:
    """The HONEST per-tier round-trip fee estimate in bps of the plan's gross
    (broker sizing memo §4.5, amended by operator decision §7.3) — journaled
    on every placement as the calibration series for path B's 150 bps target,
    and since #1123 also the number the fee FLOOR (``_check_fee_floor``) gates
    on, so the gate and the journal price one plan the same way. The aggregate
    ``round_trip_fee_bps`` survives only as the floor's fallback for when this
    returns ``None``.

    - ``entry_fees``: each non-zero tier pays its own commission
      ``max($1, 0.08% x qty x limit)`` — zero-qty tiers are never POSTed
      (``_ZERO_QTY_TIER_POLICY``), so they pay nothing. The $1 minimum is a
      USD figure, gated on ``instrument_currency`` exactly like
      ``round_trip_fee_bps``.
    - ``exit_fees``: the same shape over the TP tranches, with tranche qtys
      derived at placement as ``tranche_frac x total entry qty``
      (the brief-shaped ``TpTrancheSpec.tranche_pct`` is a PERCENTAGE 0-100;
      ``compute_setup_plan`` converts it ONCE into the plan's fraction, so
      nothing downstream divides by 100 again — this function used to, and
      priced the whole exit leg at 1% of the position). When the
      plan carries NO tranches (geometry-policy picks express the exit in
      ``exit_spec``, not static tranches) the estimate MIRRORS the entry fees
      — a symmetric single-exit assumption, deliberately simple over falsely
      precise.
    - ``fx_cost``: the 0.50% FX round trip on the gross when a conversion
      applies.

    ``None`` (an honest "not estimable", journaled as a real null) when there
    is no sized plan / no tiers / zero gross — mirrors the inert stance of
    ``round_trip_fee_bps`` on a non-positive notional."""
    # Two separate refusals, not one compound expression: a reader (and a type
    # checker) can then see that everything below this point has a real plan.
    # The compound form left ``plan`` optional for the whole body while the
    # ``gross`` call below requires one.
    if plan is None:
        return None
    entry_tiers = getattr(plan, "entry_tiers", None)
    if not entry_tiers:
        return None

    from broker_contract.sizing import setup_plan_gross_notional

    gross = setup_plan_gross_notional(plan)
    if gross <= 0:
        return None
    # #1238 PR 3: price the venue's own card when one exists (WSE 0.12% min
    # PLN 10); MIC-first since #1271 (Xetra and Euronext are both EUR with
    # different minimums); a currency with no verified card keeps the legacy
    # shape (US rate, minimum only for USD).
    card = fee_card_for(instrument_currency, exchange_mic=exchange_mic)
    commission_rate = card.commission_rate if card is not None else COMMISSION_RATE
    min_commission = card.min_commission if card is not None else MIN_COMMISSION_USD
    min_commission_applies = card is not None or instrument_currency == "USD"

    def _fill_fee(qty: float, price: float) -> float:
        ad_valorem = commission_rate * qty * price
        if min_commission_applies:
            return max(min_commission, ad_valorem)
        return ad_valorem

    entry_fees = sum(_fill_fee(t.qty, t.limit_price) for t in entry_tiers if t.qty > 0)
    tranches = getattr(plan, "tp_tranches", None) or ()
    if tranches:
        # Same `qty > 0` filter as entry_fees above. Zero-qty tiers add zero
        # either way; keeping the two sums written the same way stops a reader
        # hunting for a difference that is not there.
        total_qty = sum(t.qty for t in entry_tiers if t.qty > 0)
        exit_fees = sum(_fill_fee(total_qty * t.tranche_frac, t.target_price) for t in tranches)
    else:
        exit_fees = entry_fees
    fx_cost = FX_ROUND_TRIP_RATE * gross if fx is not None else 0.0
    return (entry_fees + exit_fees + fx_cost) / gross * 10000.0


def _committed_working_gross_acct(
    open_verdicts: Iterable[Any], records: Iterable[Mapping[str, Any]]
) -> tuple[float, int]:
    """``(total, unjoined)`` — the still-working journaled entry gross folded
    into ACCOUNT currency, plus the count of working verdicts that could NOT
    be joined back to a journaled entry bracket.

    WORKING/PARTIALLY_FILLED verdicts joined back to their journaled entry
    bracket — the join the removed pre-sizing rail used to do untyped — but
    each bracket's ``entry x qty`` — INSTRUMENT currency — is
    converted through that record's OWN journaled ``fx_rate`` (submission_log
    schema 2: the account-ccy -> instrument-ccy Mid the sizing used), so
    mixed-vintage rates never revalue each other. ``fx_rate`` null
    (same-currency / schema-1 era) folds as-is.

    ``unjoined`` counts working verdicts whose ``client_request_id`` matches
    no journaled bracket (or a bracket missing entry/qty) — exposure that
    EXISTS at the broker but cannot be valued from the journal. The caller
    fails CLOSED on it (zen pre-merge finding): silently skipping would
    understate committed gross and let a pick through over the true cap.
    ``_summarize_open_verdicts`` no longer folds gross at all (#1192) — it
    counts slots and today's realized R; THIS fold is the only gross valuation."""
    entry_fx_by_request_id: dict[str, tuple[Mapping[str, Any], Any]] = {
        str(bracket.get("client_request_id")): (bracket, record.get("fx_rate"))
        for record in records
        for bracket in record.get("brackets") or []
    }
    total = 0.0
    unjoined = 0
    for verdict in open_verdicts:
        if verdict.status not in {"WORKING", "PARTIALLY_FILLED"}:
            continue
        joined = entry_fx_by_request_id.get(str(verdict.details.get("client_request_id") or ""))
        if joined is None:
            unjoined += 1
            continue
        bracket, fx_rate = joined
        if bracket.get("entry") is None or bracket.get("qty") is None:
            unjoined += 1
            continue
        notional = float(bracket["entry"]) * float(bracket["qty"])
        if fx_rate is not None:
            # rate is instrument-ccy per 1 account-ccy -> acct = instr / rate.
            notional /= float(fx_rate)
        total += notional
    return total, unjoined


def _filled_positions_gross_acct(
    positions: Iterable[Any],
    fx: Any,
    *,
    account_currency: str = "",
    rate_lookup: Callable[[str], float | None] | None = None,
) -> tuple[float, str | None]:
    """``(total, None)`` — the broker positions' mark-to-market gross in
    ACCOUNT currency — or ``(0.0, failure)`` when any position carries no
    usable mark or a currency that cannot be converted.

    Valuation choice: ``Position.market_value`` (Saxo
    ``PositionView.MarketValue`` — qty x current market price, INSTRUMENT
    currency) is the one current-price field the position row carries;
    ``avg_price`` is the stale open price and would mis-state exposure after
    any move. A ``None`` mark (SIM NoAccess) cannot be valued conservatively
    HIGH without a price, so it FAILS CLOSED — the caller refuses the pick
    with an alert rather than silently skipping the position. ``abs`` because
    gross exposure ignores position sign.

    Mixed-currency book (#1238 PR 4 — pre-#1238 this failed closed the moment
    ANY stamped currency differed from the candidate's fx, so the first GPW
    position alongside USD ones would have refused every placement). Per
    position, by its stamped ``instrument.currency``:

    - ``""`` (not stamped — best-effort reverse lookup rows,
      ``InstrumentRef`` docstring): today's path byte-identical — the
      candidate ``fx.rate`` when present, else raw. Absent is not wrong.
    - equal to the candidate fx's instrument currency: the candidate rate.
    - equal to ``account_currency``: already account currency, folds raw.
    - anything else: converted through ``rate_lookup`` (ONE lookup per
      distinct currency per attempt — it is broker I/O inside a money gate);
      no lookup available or no rate producible fails CLOSED, exactly like a
      missing mark."""
    expected_ccy = getattr(fx, "instrument_currency", "") if fx is not None else ""
    rates: dict[str, float] = {}
    total = 0.0
    for position in positions:
        value_acct, failure = _value_one_position(
            position,
            fx,
            expected_ccy=expected_ccy,
            account_currency=account_currency,
            rates=rates,
            rate_lookup=rate_lookup,
        )
        if failure is not None:
            return 0.0, failure
        total += value_acct
    return total, None


def _value_one_position(
    position: Any,
    fx: Any,
    *,
    expected_ccy: str,
    account_currency: str,
    rates: dict[str, float],
    rate_lookup: Callable[[str], float | None] | None,
) -> tuple[float, str | None]:
    """``(value in account currency, None)`` for one position, or
    ``(0.0, failure)`` — the fail-closed cases and per-currency branch order of
    :func:`_filled_positions_gross_acct` verbatim. ``rates`` memoizes lookups
    across positions (ONE lookup per distinct currency per attempt)."""
    position_ccy = getattr(position.instrument, "currency", "") or ""
    if position.market_value is None:
        return 0.0, (
            f"position {position.instrument.ticker} has no broker mark "
            "(market_value=None) — cannot value gross exposure, failing closed"
        )
    value_instr = abs(float(position.market_value))
    # Legacy path first, byte-identical: a candidate fx with no stamped
    # instrument currency (or an unstamped/matching position) converts
    # through the candidate rate exactly as before #1238; fx=None with an
    # unstamped position folds raw.
    if fx is not None and (not expected_ccy or position_ccy in ("", expected_ccy)):
        return value_instr / float(fx.rate), None
    if fx is None and not position_ccy:
        return value_instr, None
    if account_currency and position_ccy == account_currency:
        return value_instr, None
    rate = rates.get(position_ccy)
    if rate is None and rate_lookup is not None:
        looked_up = rate_lookup(position_ccy)
        if looked_up is not None and looked_up > 0.0:
            rate = float(looked_up)
            rates[position_ccy] = rate
    if rate is None:
        return 0.0, (
            f"position {position.instrument.ticker} trades in {position_ccy} and no "
            f"conversion into the account currency is available — cannot value gross "
            "exposure through a foreign rate, failing closed"
        )
    return value_instr / rate, None


def _make_position_rate_lookup(broker: Any, account_currency: str) -> Callable[[str], float | None]:
    """A policy-checked ``instrument-per-account`` rate per foreign currency
    for :func:`_filled_positions_gross_acct` (#1238 PR 4). Reuses the SAME
    quote source and acceptance policy as candidate sizing
    (``broker.get_fx_rate`` -> ``build_fx_conversion``); any failure —
    missing capability, a broker error, a policy-rejected quote — returns
    ``None`` and the fold fails closed."""

    def _lookup(currency: str) -> float | None:
        get_fx_rate = getattr(broker, "get_fx_rate", None)
        if get_fx_rate is None or not account_currency:
            return None
        from broker_contract.contract import BrokerError

        from alphalens_pipeline.brokers.execution import build_fx_conversion

        try:
            return float(build_fx_conversion(get_fx_rate(account_currency, currency)).rate)
        except (BrokerError, TypeError, ValueError) as exc:
            logger.warning(
                "gross cap: FX lookup %s->%s failed (%s) — the fold fails closed",
                account_currency,
                currency,
                exc,
            )
            return None

    return _lookup


def _check_gross_cap(
    plan: Any,
    fx: Any,
    *,
    account: Any,
    open_verdicts: Iterable[Any],
    records: Iterable[Mapping[str, Any]],
    positions: Iterable[Any],
    ticker: str,
    entry_trail_fold: entry_trails.EntryTrailFold | None = None,
    broker: Any = None,
) -> str | None:
    """``None`` iff the pick keeps total gross exposure — still-working
    journaled entries + THIS candidate + filled positions + WATCHING trail
    tiers, all in ACCOUNT currency — within ``GROSS_FRAC x
    account.total_value``; else a terminal refusal message naming the total,
    its components, the limit, GROSS_FRAC and total_value.

    ``GROSS_FRAC`` is read THROUGH ``safety.PORTFOLIO_GROSS_FRAC_ENV`` and
    ``safety.DEFAULT_PORTFOLIO_GROSS_FRAC`` with the same ``_float_env``
    fallback, so this rail can never drift from the configured name or
    default even though ``safety.check`` no longer reads them itself.
    (``safety`` still OWNS the env contract; #1192 removed only its rail.) The candidate
    folds its RAW planned gross (``setup_plan_gross_notional``) — explicitly
    NO cash/fee buffer: the cap measures EXPOSURE, not funding.

    The watching term (entry-trailing memo G5) folds the limit-valued virtual
    reservation of NON-terminal entry-trail tiers from ``entry_trails.jsonl``
    — those tiers have NO broker order yet, so they are invisible to the
    committed-working fold. It applies in BOTH sizing modes (the cash floor
    is inert outside declared mode, so THIS rail must carry the virtual fold
    everywhere); no/empty journal folds to exactly 0.0, and the refusal text
    only names the component when it is non-zero (PR-T0 inertness)."""
    from broker_contract.sizing import setup_plan_gross_notional

    from alphalens_pipeline.brokers.automanager import safety

    gross_frac = safety._float_env(
        safety.PORTFOLIO_GROSS_FRAC_ENV, safety.DEFAULT_PORTFOLIO_GROSS_FRAC
    )

    candidate_acct = setup_plan_gross_notional(plan)
    if fx is not None:
        # rate is instrument-ccy per 1 account-ccy -> acct = instr / rate.
        candidate_acct /= float(fx.rate)

    committed_acct, unjoined = _committed_working_gross_acct(open_verdicts, records)
    if unjoined:
        # Fail CLOSED on journal join-skew (zen pre-merge finding): a working
        # verdict we cannot value means real broker exposure the cap cannot
        # see — refusing beats silently under-counting on a money rail.
        return (
            f"gross cap: {ticker} refused — {unjoined} working order(s) could not be "
            "joined to a journaled entry bracket; committed gross cannot be valued, "
            "failing closed"
        )
    account_ccy = str(getattr(account, "currency", "") or "")
    filled_acct, mark_failure = _filled_positions_gross_acct(
        positions,
        fx,
        account_currency=account_ccy,
        rate_lookup=_make_position_rate_lookup(broker, account_ccy) if broker is not None else None,
    )
    if mark_failure is not None:
        return f"gross cap: {ticker} refused — {mark_failure}"

    # PR-T1: read the fold ONCE in _place_pick and thread it into BOTH money
    # gates so a mid-attempt append (a watch opening on another pick this tick)
    # can never tear the read between them. A None fold (direct unit tests) reads
    # its own snapshot, as before.
    fold = (
        entry_trail_fold if entry_trail_fold is not None else entry_trails.read_entry_trail_fold()
    )
    watching_acct, unvaluable = entry_trails.watching_virtual_gross_acct(fold)
    if unvaluable:
        # Fail CLOSED exactly like the unjoined-working-orders path above: a
        # malformed/unvaluable entry-trail record may be a virtual reservation
        # the cap cannot see — refusing beats silently under-counting.
        return (
            f"gross cap: {ticker} refused — {unvaluable} entry-trail record(s) could not "
            "be valued (malformed or missing watch_open); the watching reservation "
            "cannot be valued, failing closed"
        )

    total_acct = committed_acct + candidate_acct + filled_acct + watching_acct
    limit_acct = gross_frac * account.total_value
    if total_acct <= limit_acct:
        return None
    # Named only when non-zero so the pre-trailing refusal text stays
    # byte-identical while no watch is open (PR-T0 inertness proof).
    watching_component = f" + watching {watching_acct:,.2f}" if watching_acct else ""
    return (
        f"gross cap: {ticker} total gross {total_acct:,.2f} {account.currency} "
        f"(working {committed_acct:,.2f} + candidate {candidate_acct:,.2f} "
        f"+ filled {filled_acct:,.2f}{watching_component}) exceeds limit {limit_acct:,.2f} "
        f"({gross_frac:g} x total_value {account.total_value:,.2f}) — pick refused"
    )


_CASH_FLOOR_BUFFER_PCT = 4.0


def _check_cash_floor(
    plan: Any,
    fx: Any,
    *,
    account: Any,
    open_verdicts: Iterable[Any],
    records: Iterable[Mapping[str, Any]],
    ticker: str,
    entry_trail_fold: entry_trails.EntryTrailFold | None = None,
) -> str | None:
    """``None`` iff the pick's buffered funding need fits the account's real
    ``margin_available`` (or the sizing mode is not ``declared`` — clamped /
    unset stays byte-identical to pre-cash-floor behavior; the min-clamp
    already bounds sizing by the snapshot there). Else a terminal refusal
    message naming the buffered candidate, the resting reservation, the
    available figure and the account currency (memo §4.2/§4.3):

        candidate_buffered + reserved_resting > available -> refuse

    ``reserved_resting`` folds the committed-working entry gross from the
    journal via the PR-0 ``_committed_working_gross_acct`` fold because the
    broker reserves NOTHING for a resting buy limit (P1 probe, 2026-08-12
    SIM: CashBalance, MarginAvailableForTrading and TotalValue all UNCHANGED
    after placement and after cancel) — without this ledger two armed picks
    would double-spend the same cash. The watching virtual reservation
    (entry-trailing memo G5) joins the same sum: a watching trail tier has NO
    broker order at all, so its future fire is cash the floor must reserve;
    no/empty journal adds exactly 0.0 (PR-T0 inertness), and an unvaluable
    watching record fails CLOSED here too — independent of the gross cap
    running first, so no caller ordering can silently under-reserve. The
    committed fold's ``unjoined`` count is deliberately ignored HERE: the
    gross cap (which runs FIRST in ``_place_pick``, same verdicts+records)
    already fails closed on any unjoined working verdict, so this code path
    only ever sees ``unjoined`` when called outside that ordering (direct
    unit tests).

    ``available`` is ``margin_available`` — never ``cash``, which ignores
    margin impact and lags under EOD netting; ``None`` (SIM NoAccess or an
    account double without the field) fails CLOSED."""
    from alphalens_pipeline.brokers.automanager.live_rails import (
        SIZING_EQUITY_MODE_ENV,
        SIZING_MODE_DECLARED,
    )

    mode = (os.environ.get(SIZING_EQUITY_MODE_ENV) or "").strip().lower()
    if mode != SIZING_MODE_DECLARED:
        return None

    from broker_contract.sizing import setup_plan_gross_notional

    candidate_acct = setup_plan_gross_notional(plan)
    if candidate_acct <= 0:
        # An unplannable/zero-tier pick funds nothing — stay inert (before the
        # margin read); the zero-tiers refusal downstream owns such a pick.
        return None
    if fx is not None:
        # rate is instrument-ccy per 1 account-ccy -> acct = instr / rate.
        candidate_acct /= float(fx.rate)
    candidate_buffered = candidate_acct * (1.0 + _CASH_FLOOR_BUFFER_PCT / 100.0)

    reserved_resting, _unjoined = _committed_working_gross_acct(open_verdicts, records)
    # PR-T1 torn-read fix: _place_pick reads the fold ONCE and threads the SAME
    # snapshot into this gate and _check_gross_cap, so a mid-attempt watch_open
    # append on another pick this tick can never tear the read between the two
    # money gates. A None fold (direct unit tests) reads its own snapshot.
    fold = (
        entry_trail_fold if entry_trail_fold is not None else entry_trails.read_entry_trail_fold()
    )
    watching_acct, unvaluable = entry_trails.watching_virtual_gross_acct(fold)
    if unvaluable:
        # Fail CLOSED independent of the gross cap running first: a direct or
        # future caller outside the _place_pick ordering must never silently
        # under-reserve on a watching record it cannot value.
        return (
            f"cash floor: {ticker} refused — {unvaluable} entry-trail record(s) could not "
            "be valued (malformed or missing watch_open); the watching reservation "
            "cannot be valued, failing closed"
        )
    reserved_resting += watching_acct

    available = getattr(account, "margin_available", None)
    if available is None:
        return (
            f"cash floor: {ticker} refused — account margin_available is None, the "
            "real balance cannot be read; failing closed"
        )
    if candidate_buffered + reserved_resting <= available:
        return None
    return (
        f"cash floor: {ticker} needs {candidate_buffered:,.2f} {account.currency} "
        f"(incl. {_CASH_FLOOR_BUFFER_PCT:g}% buffer) + {reserved_resting:,.2f} already "
        f"reserved by resting entries, but only {available:,.2f} {account.currency} is "
        "available — deposit and re-arm"
    )
