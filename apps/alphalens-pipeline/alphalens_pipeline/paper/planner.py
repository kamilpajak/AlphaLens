"""Daily paper-trade planner.

Reads the day's verified candidates from the thematic brief parquet,
applies the locked sizing math (see
``docs/research/paper_trading_capital_sizing_2026_05_28.md``), and
persists either a PLANNED row to the SQLite ledger or a shadow-log entry
for any candidate that is skipped or blocked.

The planner is intentionally idempotent at the (brief_date, ticker) key —
the ``UNIQUE(brief_date, ticker)`` constraint on ``plans`` plus a
pre-check on ``shadow_log`` means re-running ``alphalens paper plan
--date <D>`` on the same day is a no-op rather than a doubling of the
record. Re-planning after the brief parquet changes is supported via
``--force`` (deletes previous rows for that date first).
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from alphalens_pipeline.paper.brief_loader import CandidateBrief, load_brief
from alphalens_pipeline.paper.constants import (
    DEFAULT_ORDER_TTL_DAYS,
    DEFAULT_PAPER_EQUITY_USD,
    GROSS_SAFETY_FRAC,
)
from alphalens_pipeline.paper.ledger import (
    insert_planned,
    insert_shadow,
    open_ledger,
)
from alphalens_pipeline.paper.sizing import (
    SetupPlan,
    TradeSetupNotPlannableError,
    compute_daily_scale_factor,
    compute_setup_plan,
    setup_plan_gross_notional,
    validate_trade_setup,
)

logger = logging.getLogger(__name__)


class PositionChecker(Protocol):
    """Reports whether a ticker has an open paper position right now.

    The default impl wraps :class:`AlpacaClient`; tests + dry-runs pass a
    set-backed stub. The Protocol keeps the planner free of an Alpaca SDK
    import so it can be unit-tested without ``alpaca-py`` installed.
    """

    def has_open_position(self, symbol: str) -> bool:  # pragma: no cover - signature only
        ...


class EquityProvider(Protocol):
    """Returns the current paper equity in USD."""

    def get_paper_equity(self) -> float:  # pragma: no cover - signature only
        ...


class _AlpacaPositionChecker:
    """Production :class:`PositionChecker` backed by a live AlpacaClient."""

    def __init__(self, client) -> None:
        self._client = client

    def has_open_position(self, symbol: str) -> bool:
        return self._client.get_position(symbol) is not None


class _AlpacaEquityProvider:
    """Production :class:`EquityProvider` reading live equity from Alpaca."""

    def __init__(self, client) -> None:
        self._client = client

    def get_paper_equity(self) -> float:
        return float(self._client.get_account().equity)


class _NullPositionChecker:
    """Dry-run / no-Alpaca fallback — never reports a position open. Used
    by ``--no-alpaca`` to enable offline planning against the same brief."""

    def has_open_position(self, symbol: str) -> bool:
        return False


class _FixedEquityProvider:
    def __init__(self, equity: float) -> None:
        self._equity = float(equity)

    def get_paper_equity(self) -> float:
        return self._equity


@dataclass(frozen=True)
class PlanOutcome:
    """One candidate's outcome from a planning run."""

    ticker: str
    theme: str
    status: str  # 'PLANNED' / 'SHADOWED'
    reason: str | None = None  # populated when status == 'SHADOWED'


@dataclass(frozen=True)
class PlanReport:
    """Aggregate report of one ``alphalens paper plan`` invocation."""

    brief_date: dt.date
    paper_equity: float
    n_planned: int
    n_shadowed: int
    total_gross_notional: float
    outcomes: tuple[PlanOutcome, ...]


def _shadow(
    conn,
    *,
    candidate: CandidateBrief,
    reason: str,
    details: dict | None = None,
) -> PlanOutcome:
    insert_shadow(
        conn,
        brief_date=candidate.brief_date,
        ticker=candidate.ticker,
        theme=candidate.theme,
        reason=reason,
        details=details,
    )
    return PlanOutcome(
        ticker=candidate.ticker,
        theme=candidate.theme,
        status="SHADOWED",
        reason=reason,
    )


def _resolve_position_checker(
    explicit: PositionChecker | None,
    alpaca_client,
) -> PositionChecker:
    if explicit is not None:
        return explicit
    if alpaca_client is not None:
        return _AlpacaPositionChecker(alpaca_client)
    return _NullPositionChecker()


def _resolve_equity_provider(
    explicit: EquityProvider | None,
    alpaca_client,
    fallback_equity: float,
) -> EquityProvider:
    if explicit is not None:
        return explicit
    if alpaca_client is not None:
        return _AlpacaEquityProvider(alpaca_client)
    return _FixedEquityProvider(fallback_equity)


def _delete_existing_for_date(conn, brief_date: dt.date, account: str) -> None:
    """Idempotency helper for ``--force`` reruns. Scoped to one account
    so ``--force`` on a TEST replan doesn't blow away MAIN history (or
    vice versa) when both share the same ledger file. The shadow_log
    table doesn't carry an account column today — same brief_date can
    only ever be planned by one account in practice (plan-per-day
    cadence), so the unscoped DELETE is fine for now.
    """
    conn.execute(
        "DELETE FROM plans WHERE brief_date = ? AND account = ?",
        (brief_date.isoformat(), account),
    )
    conn.execute("DELETE FROM shadow_log WHERE brief_date = ?", (brief_date.isoformat(),))


def _process_candidate(
    conn,
    *,
    candidate: CandidateBrief,
    setup_plan: SetupPlan | None,
    unplannable_reason: str | None,
    position_checker: PositionChecker,
    cumulative_gross: float,
    gross_cap: float,
    planned_at: dt.datetime,
    account: str,
) -> tuple[PlanOutcome, float]:
    """Plan one candidate. Returns ``(outcome, new_cumulative_gross)``."""
    if not candidate.verified:
        return _shadow(conn, candidate=candidate, reason="not_verified"), cumulative_gross

    if candidate.trade_setup is None:
        return _shadow(conn, candidate=candidate, reason="no_trade_setup"), cumulative_gross

    if setup_plan is None:
        return (
            _shadow(
                conn,
                candidate=candidate,
                reason="unplannable_setup",
                details={"explain": unplannable_reason},
            ),
            cumulative_gross,
        )

    if position_checker.has_open_position(candidate.ticker):
        return (
            _shadow(
                conn,
                candidate=candidate,
                reason="same_ticker_open",
                details={
                    "theme": candidate.theme,
                    "would_have_planned_notional": setup_plan_gross_notional(setup_plan),
                },
            ),
            cumulative_gross,
        )

    gross_notional = setup_plan_gross_notional(setup_plan)
    if cumulative_gross + gross_notional > gross_cap:
        return (
            _shadow(
                conn,
                candidate=candidate,
                reason="gross_cap_block",
                details={
                    "current_gross": cumulative_gross,
                    "would_add": gross_notional,
                    "gross_cap": gross_cap,
                },
            ),
            cumulative_gross,
        )

    tiers = [
        (t.tier_index, t.limit_price, t.qty, t.alloc_pct, t.tag) for t in setup_plan.entry_tiers
    ]
    tp_rows = [
        (t.tranche_index, t.target_price, t.tranche_pct, t.r_multiple, t.tag)
        for t in setup_plan.tp_tranches
    ]
    order_ttl_days = setup_plan.order_ttl_days or DEFAULT_ORDER_TTL_DAYS
    insert_planned(
        conn,
        brief_date=candidate.brief_date,
        ticker=candidate.ticker,
        theme=candidate.theme,
        planned_at=planned_at,
        suggested_size_pct=setup_plan.suggested_size_pct,
        scale_factor=setup_plan.scale_factor,
        final_size_pct=setup_plan.final_size_pct,
        paper_equity=setup_plan.paper_equity,
        total_notional=setup_plan.total_notional,
        gross_notional=gross_notional,
        disaster_stop=setup_plan.disaster_stop,
        order_ttl_days=order_ttl_days,
        tiers=tiers,
        tp_tranches=tp_rows,
        account=account,
    )
    return (
        PlanOutcome(ticker=candidate.ticker, theme=candidate.theme, status="PLANNED"),
        cumulative_gross + gross_notional,
    )


def _try_compute(
    candidate: CandidateBrief, paper_equity: float, scale_factor: float
) -> tuple[SetupPlan | None, str | None]:
    """Wrap :func:`compute_setup_plan` to return either a plan or a reason."""
    if candidate.trade_setup is None:
        return None, None
    try:
        plan = compute_setup_plan(
            brief_trade_setup=candidate.trade_setup,
            paper_equity=paper_equity,
            scale_factor=scale_factor,
        )
    except TradeSetupNotPlannableError as exc:
        return None, str(exc)
    return plan, None


def _collect_plannable_suggested(
    candidates: list[CandidateBrief],
) -> list[float]:
    """First pass: extract ``suggested_size_pct`` from candidates that would
    be plannable (verified + has a setup that passes validation). Skips
    unverified candidates and unparseable setups silently — the second pass
    handles those with structured shadow_log reasons. This pass exists only
    to feed :func:`compute_daily_scale_factor`."""
    out: list[float] = []
    for cand in candidates:
        if not cand.verified or cand.trade_setup is None:
            continue
        try:
            out.append(validate_trade_setup(cand.trade_setup))
        except TradeSetupNotPlannableError:
            continue
    return out


def plan_for_date(
    *,
    brief_date: dt.date,
    briefs_dir: Path,
    ledger_path: Path,
    alpaca_client=None,
    position_checker: PositionChecker | None = None,
    equity_provider: EquityProvider | None = None,
    fallback_equity: float = DEFAULT_PAPER_EQUITY_USD,
    force: bool = False,
    candidates: Iterable[CandidateBrief] | None = None,
    account: str = "main",
) -> PlanReport:
    """Plan one day's verified candidates and persist to the SQLite ledger.

    ``candidates`` is an optional pre-loaded list — exposed so tests can drive
    the planner without writing a parquet to disk.

    ``alpaca_client`` is the canonical :class:`AlpacaClient`. When provided,
    the planner uses it for live equity + the same-ticker dedup check. When
    omitted, the planner falls back to ``fallback_equity`` and treats every
    ticker as having no open position (offline mode). Tests inject custom
    :class:`PositionChecker` / :class:`EquityProvider` via the explicit args.

    Single-writer assumption: the SQLite ledger uses WAL journal mode, which
    allows concurrent readers but serialises writers. A second writer
    (e.g. an operator running ``alphalens paper plan`` while the daily cron
    is mid-execution, or PR 3's reconciler firing concurrently) will see
    ``database is locked`` errors. The planner is invoked sequentially today
    — daily systemd timer + occasional manual operator runs — so the
    assumption holds in practice. Before PR 3 adds the reconciler cron, add
    an advisory file lock (``fcntl.flock`` on a sibling ``.lock`` file) so
    the two write surfaces cannot race.
    """
    candidates_list = (
        list(candidates) if candidates is not None else load_brief(brief_date, briefs_dir)
    )
    pos_checker = _resolve_position_checker(position_checker, alpaca_client)
    equity_provider_resolved = _resolve_equity_provider(
        equity_provider, alpaca_client, fallback_equity
    )
    paper_equity = equity_provider_resolved.get_paper_equity()
    gross_cap = GROSS_SAFETY_FRAC * paper_equity
    planned_at = dt.datetime.now(dt.UTC)

    # First pass: compute the daily global scale factor over the
    # plannable cohort. v2 sizing per memo §2.3 — preserves
    # inter-candidate ratios while bounding aggregate steady-state gross.
    # Candidates that fail validation here are simply skipped from the
    # aggregate; the second pass re-runs validation per candidate and
    # produces the structured shadow_log entry.
    scale_factor = compute_daily_scale_factor(
        _collect_plannable_suggested(candidates_list),
        paper_equity,
    )
    logger.info(
        "paper plan %s: scale_factor=%.4f (equity=$%.0f)",
        brief_date.isoformat(),
        scale_factor,
        paper_equity,
    )

    outcomes: list[PlanOutcome] = []
    cumulative_gross = 0.0
    # Track tickers that already produced a PLANNED row in THIS run. The
    # ``UNIQUE(brief_date, ticker)`` constraint on ``plans`` would crash the
    # whole batch with IntegrityError on the second occurrence of a ticker
    # within one brief (same ticker can appear under different themes —
    # e.g. NVDA in 'ai-infra' AND 'datacenter-buildout'). Shadow-log the
    # duplicate cleanly and continue processing the remaining candidates.
    planned_tickers_in_run: set[str] = set()

    with open_ledger(ledger_path) as conn:
        if force:
            _delete_existing_for_date(conn, brief_date, account)

        for candidate in candidates_list:
            if candidate.ticker in planned_tickers_in_run:
                outcomes.append(
                    _shadow(
                        conn,
                        candidate=candidate,
                        reason="duplicate_ticker_in_brief",
                        details={
                            "theme": candidate.theme,
                            "note": "already planned earlier in this brief run under a different theme",
                        },
                    )
                )
                continue

            setup_plan, unplannable_reason = _try_compute(candidate, paper_equity, scale_factor)
            outcome, cumulative_gross = _process_candidate(
                conn,
                candidate=candidate,
                setup_plan=setup_plan,
                unplannable_reason=unplannable_reason,
                position_checker=pos_checker,
                cumulative_gross=cumulative_gross,
                gross_cap=gross_cap,
                planned_at=planned_at,
                account=account,
            )
            outcomes.append(outcome)
            if outcome.status == "PLANNED":
                planned_tickers_in_run.add(candidate.ticker)

    n_planned = sum(1 for o in outcomes if o.status == "PLANNED")
    n_shadowed = sum(1 for o in outcomes if o.status == "SHADOWED")
    logger.info(
        "paper plan %s: %d planned, %d shadowed, gross_notional=$%.0f / cap=$%.0f",
        brief_date.isoformat(),
        n_planned,
        n_shadowed,
        cumulative_gross,
        gross_cap,
    )

    return PlanReport(
        brief_date=brief_date,
        paper_equity=paper_equity,
        n_planned=n_planned,
        n_shadowed=n_shadowed,
        total_gross_notional=cumulative_gross,
        outcomes=tuple(outcomes),
    )


__all__ = [
    "EquityProvider",
    "PlanOutcome",
    "PlanReport",
    "PositionChecker",
    "plan_for_date",
]
