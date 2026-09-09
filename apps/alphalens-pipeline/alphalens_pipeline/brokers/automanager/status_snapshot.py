"""One read-only operator snapshot of a broker instance (#1378).

The last piece of the LIVE status surface: `alphalens broker status` answers
"is the daemon alive, what is resting, how much room is left" in one
invocation, so an incident is not diagnosed by reading five sources by hand
(the 2026-09-08 outage).

**It recomputes nothing.** Every headroom number comes from the DAEMON'S OWN
folds, called with the candidate set to zero, so the figure the operator reads
is the figure the gate will subtract:

* gross — ``control_loop._committed_working_gross_acct`` +
  ``_filled_positions_gross_acct`` + ``entry_trails.watching_virtual_gross_acct``
  against ``GROSS_FRAC x total_value``, the exact terms of ``_check_gross_cap``;
* slots — ``_summarize_open_verdicts`` + ``_net_open_position_uics`` +
  ``_open_watch_picks_for_max_open``, the exact sum ``safety.check`` compares
  with ``MAX_OPEN``;
* cash floor — ``committed + watching`` against ``margin_available``, the
  reservation ``_check_cash_floor`` folds, and only in ``declared`` mode.

Reusing the daemon's PRIVATE folds is a deliberate coupling: refactoring the
money gates to expose a public snapshot API would touch the daemon's hot path.
Two tests hold it — one pins the names (a rename fails CI) and one probes the
real gate at the reported boundary across several states (a semantic drift
under an unchanged name fails there).

**Fail-closed is CONTENT, not an exception.** Where the gate would refuse
because it cannot value something (an unjoined working order, an unvaluable
watch tier, a position without a mark, a missing margin figure), the section
reports ``blocked`` with that reason and ``None`` numbers. The command still
exits 0: an unhealthy instance is a fact to render, not a CLI failure, and an
alert rule must not be duplicated in a status command.

**No audit fan-out.** ``reconcile_brackets`` resolves a DISAPPEARED bracket
through the ``/cs`` audit endpoint — the bucket that twice tripped 429s — one
read per bracket, unbounded on the CLI path. Only WORKING verdicts feed the
numbers here, and those resolve from ``list_open_orders`` alone, so the records
are filtered to brackets whose entry order is still open BEFORE reconciling.
The fan-out is removed by construction rather than bounded by a budget. The
cost is that closed verdicts disappear with it, so ``realized_r_today`` (the
daily-loss rail) is deliberately NOT reported.

**The snapshot is not point-in-time.** Broker HTTP, journals, textfiles and
token stores are read at different instants while the daemon writes every
~45 s. Each section carries its own ``as_of``, the journals are read ONCE and
threaded into every consumer (the daemon's own PR-T1 anti-torn-read rule), and
a journal that changes during the broker reads sets ``skewed``.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from alphalens_pipeline.brokers.automanager import entry_trails, safety, state_paths

_PROM_LINE_RE = re.compile(r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{[^}]*\})?\s+(?P<value>\S+)$")

_HEARTBEAT_GAUGE = "alphalens_broker_manager_last_tick_timestamp_seconds"
_KILL_ACTIVE_GAUGE = "alphalens_broker_manager_kill_active"
_FRAME_GAUGE = "alphalens_live_price_stream_last_frame_timestamp_seconds"


def parse_prom_gauges(path: Path) -> dict[str, float]:
    """Parse ``name value`` gauge lines; a malformed value line is skipped.

    Promoted from the CLI so ``status`` and ``stream-status`` read a textfile
    through ONE parser."""
    gauges: dict[str, float] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return gauges
    for line in text.splitlines():
        match = _PROM_LINE_RE.match(line.strip())
        if match:
            try:
                gauges[match.group("name")] = float(match.group("value"))
            except ValueError:
                continue
    return gauges


@dataclass(frozen=True)
class Exposure:
    """Gross exposure against ``GROSS_FRAC x total_value`` (the gross cap)."""

    committed: float | None
    filled: float | None
    watching: float | None
    used: float | None
    limit: float | None
    headroom: float | None
    currency: str
    unstamped_positions: int = 0
    headroom_is_upper_bound: bool = False
    blocked: list[str] = field(default_factory=list)
    as_of: str = ""


@dataclass(frozen=True)
class Slots:
    """Risk units against ``MAX_OPEN`` (the capacity rail)."""

    brackets: int
    positions: int
    watch_picks: int
    used: int
    limit: int
    free: int
    as_of: str = ""


@dataclass(frozen=True)
class CashFloor:
    """Funding reservation against ``margin_available`` (declared mode only)."""

    applies: bool
    mode: str
    reserved: float | None = None
    available: float | None = None
    headroom: float | None = None
    blocked: list[str] = field(default_factory=list)
    as_of: str = ""


@dataclass(frozen=True)
class TokenStoreHealth:
    role: str
    present: bool
    store_path: str
    access_valid: bool | None = None
    access_expires_in_s: float | None = None
    refresh_expires_in_s: float | None = None
    error: str | None = None


@dataclass(frozen=True)
class Health:
    """Everything readable WITHOUT the broker — the half an outage needs."""

    unit: str
    unit_state: str
    unit_since: str | None
    heartbeat_age_s: float | None
    kill_instance: bool
    kill_global: bool
    kill_active_gauge: float | None
    price_stream_age_s: float | None
    price_stream_source: str | None
    tokens: list[TokenStoreHealth]
    last_refusal: str | None
    as_of: str = ""


@dataclass(frozen=True)
class StatusSnapshot:
    env: str
    offline: bool
    account: Any | None
    exposure: Exposure
    slots: Slots
    cash_floor: CashFloor
    orders: list[dict[str, Any]]
    watches: list[dict[str, Any]]
    health: Health
    skewed: list[str] = field(default_factory=list)


def _iso(now: dt.datetime) -> str:
    return now.isoformat(timespec="seconds")


def _mtimes(paths: dict[str, Path]) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for name, path in paths.items():
        try:
            out[name] = path.stat().st_mtime
        except OSError:
            out[name] = None
    return out


def _filter_open_records(records: list[dict[str, Any]], open_ids: set[str]) -> list[dict[str, Any]]:
    """Records rewritten to keep ONLY brackets whose entry order is still open.

    A bracket outside ``open_ids`` can never be WORKING (``reconcile._triage_one``
    routes an open order to ``_reconcile_open`` and everything else to the audit
    path), so this drops no number the snapshot reports while removing the audit
    fan-out entirely. Filtering per BRACKET, not per record: a record with one
    open and one closed bracket would otherwise still send the closed one to the
    audit endpoint."""
    filtered: list[dict[str, Any]] = []
    for record in records:
        brackets = [
            bracket
            for bracket in (record.get("brackets") or [])
            if str(bracket.get("entry_order_id") or "") in open_ids
        ]
        if brackets:
            filtered.append({**record, "brackets": brackets})
    return filtered


def _exposure(
    *,
    account: Any,
    open_verdicts: list[Any],
    records: list[dict[str, Any]],
    positions: list[Any],
    fold: entry_trails.EntryTrailFold,
    broker: Any,
    now: dt.datetime,
) -> Exposure:
    from alphalens_pipeline.brokers.automanager import control_loop

    currency = str(getattr(account, "currency", "") or "")
    limit = safety._float_env(
        safety.PORTFOLIO_GROSS_FRAC_ENV, safety.DEFAULT_PORTFOLIO_GROSS_FRAC
    ) * float(account.total_value)
    blocked: list[str] = []

    committed, unjoined = control_loop._committed_working_gross_acct(open_verdicts, records)
    if unjoined:
        blocked.append(
            f"{unjoined} working order(s) could not be joined to a journaled entry bracket; "
            "committed gross cannot be valued, failing closed"
        )
    filled, mark_failure = control_loop._filled_positions_gross_acct(
        positions,
        None,
        account_currency=currency,
        rate_lookup=control_loop._make_position_rate_lookup(broker, currency)
        if broker is not None
        else None,
    )
    if mark_failure is not None:
        blocked.append(mark_failure)
    watching, unvaluable = entry_trails.watching_virtual_gross_acct(fold)
    if unvaluable:
        blocked.append(
            f"{unvaluable} entry-trail record(s) could not be valued (malformed or missing "
            "watch_open); the watching reservation cannot be valued, failing closed"
        )
    # A position row with no stamped currency folds RAW when there is no
    # candidate fx (control_loop._value_one_position): the daemon sizing a
    # foreign candidate would convert it, so the number here can UNDERSTATE
    # gross. Never guessed — there is no source currency to convert from —
    # but the headroom is labelled rather than left to a footnote.
    unstamped = sum(
        1 for position in positions if not (getattr(position.instrument, "currency", "") or "")
    )
    if blocked:
        return Exposure(
            committed=None,
            filled=None,
            watching=None,
            used=None,
            limit=limit,
            headroom=None,
            currency=currency,
            unstamped_positions=unstamped,
            blocked=blocked,
            as_of=_iso(now),
        )
    used = committed + filled + watching
    return Exposure(
        committed=committed,
        filled=filled,
        watching=watching,
        used=used,
        limit=limit,
        headroom=limit - used,
        currency=currency,
        unstamped_positions=unstamped,
        headroom_is_upper_bound=bool(unstamped),
        as_of=_iso(now),
    )


def _slots(
    *,
    open_verdicts: list[Any],
    positions: list[Any],
    fold: entry_trails.EntryTrailFold,
    now: dt.datetime,
) -> Slots:
    from alphalens_pipeline.brokers.automanager import control_loop

    brackets, _realized_r = control_loop._summarize_open_verdicts(
        open_verdicts, now.date().isoformat()
    )
    net_uics, unresolvable = control_loop._net_open_position_uics(positions)
    watch_picks = control_loop._open_watch_picks_for_max_open(
        fold, own_pick_key="", position_uics=net_uics
    )
    position_slots = len(net_uics) + unresolvable
    used = brackets + len(watch_picks) + position_slots
    limit = safety._int_env(safety.MAX_OPEN_ENV, safety.DEFAULT_MAX_OPEN)
    return Slots(
        brackets=brackets,
        positions=position_slots,
        watch_picks=len(watch_picks),
        used=used,
        limit=limit,
        free=max(0, limit - used),
        as_of=_iso(now),
    )


def _cash_floor(
    *,
    account: Any,
    open_verdicts: list[Any],
    records: list[dict[str, Any]],
    fold: entry_trails.EntryTrailFold,
    now: dt.datetime,
) -> CashFloor:
    import os

    from alphalens_pipeline.brokers.automanager import control_loop
    from alphalens_pipeline.brokers.automanager.live_rails import (
        SIZING_EQUITY_MODE_ENV,
        SIZING_MODE_DECLARED,
    )

    mode = (os.environ.get(SIZING_EQUITY_MODE_ENV) or "").strip().lower()
    if mode != SIZING_MODE_DECLARED:
        return CashFloor(applies=False, mode=mode or "unset", as_of=_iso(now))

    blocked: list[str] = []
    reserved, _unjoined = control_loop._committed_working_gross_acct(open_verdicts, records)
    watching, unvaluable = entry_trails.watching_virtual_gross_acct(fold)
    if unvaluable:
        blocked.append(
            f"{unvaluable} entry-trail record(s) could not be valued; the watching "
            "reservation cannot be valued, failing closed"
        )
    available = getattr(account, "margin_available", None)
    if available is None:
        blocked.append(
            "margin_available is None (SIM NoAccess or an account without the field) — "
            "the cash floor fails closed"
        )
    if blocked:
        return CashFloor(applies=True, mode=mode, blocked=blocked, as_of=_iso(now))
    total = reserved + watching
    return CashFloor(
        applies=True,
        mode=mode,
        reserved=total,
        available=float(available),
        headroom=float(available) - total,
        as_of=_iso(now),
    )


def _token_health(now: dt.datetime) -> list[TokenStoreHealth]:
    """Both stores, OFFLINE. Never a network call, never a token value.

    A read command must not refresh: rotating the chain would race the
    20-minute refresh timer that owns it. What is reported is therefore the
    state as of the LAST refresh, which is the same artefact the daemon lives
    on."""
    stores: list[TokenStoreHealth] = []
    from alphalens_pipeline.brokers.saxo.errors import SaxoAuthError
    from alphalens_pipeline.brokers.saxo.tokens import TokenStore, resolve_token_store_path

    sim_path = resolve_token_store_path()
    try:
        state = TokenStore(sim_path).load()
    except (SaxoAuthError, OSError, ValueError) as exc:
        stores.append(
            TokenStoreHealth(
                role="sim-orders", present=True, store_path=str(sim_path), error=str(exc)
            )
        )
    else:
        stores.append(
            TokenStoreHealth(
                role="sim-orders",
                present=state is not None,
                store_path=str(sim_path),
                access_valid=None if state is None else state.access_token_expires_at > now,
                access_expires_in_s=None
                if state is None
                else (state.access_token_expires_at - now).total_seconds(),
                refresh_expires_in_s=None
                if state is None
                else (state.refresh_token_expires_at - now).total_seconds(),
            )
        )

    from alphalens_pipeline.data.alt_data import saxo_marketdata_auth

    try:
        live = saxo_marketdata_auth.inspect_store()
    except (RuntimeError, OSError, ValueError) as exc:
        stores.append(
            TokenStoreHealth(
                role="live-orders-and-prices", present=True, store_path="", error=str(exc)
            )
        )
    else:
        stores.append(
            TokenStoreHealth(
                role="live-orders-and-prices",
                present=live.present,
                store_path=str(live.store_path),
                access_valid=live.access_valid,
                access_expires_in_s=None
                if live.access_expires_at is None
                else (live.access_expires_at - now).total_seconds(),
                refresh_expires_in_s=None
                if live.refresh_expires_at is None
                else (live.refresh_expires_at - now).total_seconds(),
            )
        )
    return stores


def _unit_health(env: str) -> tuple[str, str, str | None]:
    """``(unit, ActiveState, ActiveEnterTimestamp)`` — best-effort CONTENT.

    Unlike the rail composition of #1377, a missing ``systemctl`` is NOT a
    refusal here: without the rails the command cannot run at all, while
    without the unit's state it can."""
    from alphalens_pipeline.brokers.automanager import unit_env

    try:
        unit = unit_env.unit_for_env(env)
    except unit_env.UnitEnvError:
        return ("unknown", "unknown", None)
    try:
        state = unit_env._systemctl_show(unit, "ActiveState").strip() or "unknown"
        since = unit_env._systemctl_show(unit, "ActiveEnterTimestamp").strip() or None
    except (OSError, ValueError):
        return (unit, "unknown", None)
    return (unit, state, since)


def _price_stream(env: str, now: dt.datetime) -> tuple[float | None, str | None]:
    """Frame age from the shared reader first, the in-process stream second."""
    from alphalens_pipeline.data.alt_data.price_reader_server import READER_STREAM_METRICS_JOB
    from alphalens_pipeline.observability import textfile

    directory = textfile._resolve_dir()
    for job in (READER_STREAM_METRICS_JOB, state_paths.price_stream_metrics_job(env)):
        gauges = parse_prom_gauges(directory / f"alphalens_domain_{job}.prom")
        stamp = gauges.get(_FRAME_GAUGE)
        if stamp is not None:
            return (now.timestamp() - stamp, job)
    return (None, None)


def _last_refusal(env: str) -> str | None:
    from alphalens_pipeline.brokers.automanager import picks as picks_mod

    fold = picks_mod.read_pick_fold(path=state_paths.picks_path(env=env))
    for record in reversed(fold.records):
        if record.status == "refused":
            reason = record.record.get("reason") or record.record.get("note") or ""
            return f"{record.ticker} {record.trade_date.isoformat()}: {reason}".strip()
    return None


def _health(env: str, now: dt.datetime) -> Health:
    from alphalens_pipeline.observability import textfile

    gauges = parse_prom_gauges(
        textfile._resolve_dir() / f"alphalens_domain_{state_paths.metrics_job(env)}.prom"
    )
    heartbeat = gauges.get(_HEARTBEAT_GAUGE)
    stream_age, stream_source = _price_stream(env, now)
    unit, unit_state, unit_since = _unit_health(env)
    return Health(
        unit=unit,
        unit_state=unit_state,
        unit_since=unit_since,
        heartbeat_age_s=None if heartbeat is None else now.timestamp() - heartbeat,
        # The FILE is the truth right now; the gauge is the daemon's view as of
        # its last tick. A disagreement means the daemon has not noticed yet,
        # or is not ticking at all.
        kill_instance=state_paths.kill_file_path(env=env).exists(),
        kill_global=state_paths.global_kill_file_path().exists(),
        kill_active_gauge=gauges.get(_KILL_ACTIVE_GAUGE),
        price_stream_age_s=stream_age,
        price_stream_source=stream_source,
        tokens=_token_health(now),
        last_refusal=_last_refusal(env),
        as_of=_iso(now),
    )


def build_snapshot(
    *,
    broker: Any,
    env: str,
    now: dt.datetime,
    offline: bool = False,
) -> StatusSnapshot:
    """The whole snapshot: the offline half first, then the broker half.

    ``offline`` skips every broker call — during a broker outage the offline
    half is exactly what answers the operator's question, and one Saxo read can
    block for minutes under the client's retry policy (4 attempts, 30 s
    timeout, 5/15/30 s backoffs)."""
    from alphalens_pipeline.brokers.reconcile import reconcile_brackets
    from alphalens_pipeline.brokers.submission_log import iter_submission_records

    trails_path = state_paths.entry_trails_path(env=env)
    submissions_path = state_paths.submissions_path(env=env)
    watched = {"entry_trails": trails_path, "submissions": submissions_path}
    before = _mtimes(watched)

    # Read each journal ONCE and thread the same snapshot everywhere (the
    # daemon's PR-T1 anti-torn-read rule between its two money gates).
    fold = entry_trails.read_entry_trail_fold(path=trails_path)
    records = list(iter_submission_records(submissions_path))
    health = _health(env, now)
    watches = entry_trails.tier_rows(fold, include_terminal=False)

    if offline:
        return StatusSnapshot(
            env=env,
            offline=True,
            account=None,
            exposure=Exposure(
                committed=None,
                filled=None,
                watching=None,
                used=None,
                limit=None,
                headroom=None,
                currency="",
                blocked=["skipped: --offline"],
                as_of=_iso(now),
            ),
            slots=Slots(
                brackets=0, positions=0, watch_picks=0, used=0, limit=0, free=0, as_of=_iso(now)
            ),
            cash_floor=CashFloor(applies=False, mode="offline", as_of=_iso(now)),
            orders=[],
            watches=watches,
            health=health,
        )

    account = broker.get_account()
    positions = list(broker.get_positions())
    open_orders = list(broker.list_open_orders())
    open_ids = {str(state.order_id) for state in open_orders}
    open_verdicts = reconcile_brackets(_filter_open_records(records, open_ids), broker)

    exposure = _exposure(
        account=account,
        open_verdicts=open_verdicts,
        records=records,
        positions=positions,
        fold=fold,
        broker=broker,
        now=now,
    )
    slots = _slots(open_verdicts=open_verdicts, positions=positions, fold=fold, now=now)
    cash_floor = _cash_floor(
        account=account, open_verdicts=open_verdicts, records=records, fold=fold, now=now
    )
    after = _mtimes(watched)
    skewed = [name for name, stamp in before.items() if after.get(name) != stamp]
    return StatusSnapshot(
        env=env,
        offline=False,
        account=account,
        exposure=exposure,
        slots=slots,
        cash_floor=cash_floor,
        orders=list(open_orders),
        watches=watches,
        health=health,
        skewed=skewed,
    )


__all__ = [
    "CashFloor",
    "Exposure",
    "Health",
    "Slots",
    "StatusSnapshot",
    "TokenStoreHealth",
    "build_snapshot",
    "parse_prom_gauges",
]
