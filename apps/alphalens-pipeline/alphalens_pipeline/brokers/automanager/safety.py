"""Pure-predicate portfolio safety gate for the Saxo auto-manager.

check(...) runs for every armed pick BEFORE any placement. Pure function of
inputs + three process rails read at call time (instance KILL file, GLOBAL
KILL file, ALLOW_ORDERS). Places, cancels, and writes nothing: the daily-loss
branch RETURNS Refuse; tripping a KILL file is the control loop's job.
Refusal order (first failing rail wins): instance KILL file -> GLOBAL KILL
file -> chain dead -> ALLOW_ORDERS != '1' -> daily-loss limit. Every one of
these is TRANSIENT: the pick stays armed and places once the rail clears.

No rail here counts picks, positions or watches (#1732, owner decision
2026-10-05): free capital is the only limit on how many picks the daemon
takes. The money rails that bound it (per-pick amount, fee floor, gross cap,
cash floor, all in ``pick_money_gates``) run later in the drain (the last three
after sizing) and refuse terminally there.

The portfolio-gross cap is deliberately NOT here (#1192): it needs the
post-sizing plan and the FX conversion, neither of which exists this early,
so it lives in ``pick_money_gates._check_gross_cap``. This module still owns
``PORTFOLIO_GROSS_FRAC_ENV`` and its default, which that rail reads through.

Both KILL paths default through the ONE broker-state path seam
(``state_paths.kill_file_path`` / ``state_paths.global_kill_file_path``,
ADR 0016 D3): the per-instance kill stops only this instance, the GLOBAL kill
(the legacy parent-level ``broker_orders/KILL``) stops every instance —
defense in depth.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from alphalens_pipeline.brokers.automanager import state_paths

ALLOW_ORDERS_ENV = "ALPHALENS_BROKER_ALLOW_ORDERS"
PORTFOLIO_GROSS_FRAC_ENV = "ALPHALENS_BROKER_PORTFOLIO_GROSS_FRAC"
DAILY_LOSS_LIMIT_R_ENV = "ALPHALENS_BROKER_DAILY_LOSS_LIMIT_R"

DEFAULT_PORTFOLIO_GROSS_FRAC = 1.0
DEFAULT_DAILY_LOSS_LIMIT_R = 3.0


@dataclass(frozen=True)
class Allow:
    """The pick clears every rail and may be placed."""


@dataclass(frozen=True)
class Refuse:
    """A refused placement. Always TRANSIENT: KILL file, dead chain,
    ALLOW_ORDERS master arm and the daily-loss lockout keep the pick armed, and
    it places once the rail clears. An inert or paused daemon must never
    destroy the armed queue.

    The terminal refusals (a refused line in picks.jsonl) belong to the
    post-sizing money gates, which journal their own through
    ``control_loop._refuse_pick_terminal``."""

    reason: str


Decision = Allow | Refuse


class SessionState(Protocol):
    """Read-only view of chain aliveness — check() only ever reads .alive, so
    the protocol declares it as a read-only property. Declaring it as a plain
    mutable attribute would reject frozen-dataclass implementers (their
    attribute is read-only by construction) under pyright's protocol variance
    check."""

    @property
    def alive(self) -> bool: ...


@dataclass(frozen=True)
class JournalView:
    """Today's realized R from closed pairs — the daily-loss rail's only input.
    Losses on still-open positions never reach it."""

    realized_r_today: float


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def check(
    pick,
    journal_view: JournalView,
    session_state: SessionState,
    *,
    kill_path: Path | None = None,
    global_kill_path: Path | None = None,
) -> Decision:
    """Return Allow iff every rail clears; else the first Refuse. Pure predicate."""
    kill = kill_path or state_paths.kill_file_path()
    if kill.exists():
        return Refuse(f"KILL file present at {kill} — emergency stop, placement halted")
    global_kill = global_kill_path or state_paths.global_kill_file_path()
    if global_kill.exists():
        return Refuse(
            f"GLOBAL KILL file present at {global_kill} — emergency stop, placement halted"
        )
    if not session_state.alive:
        return Refuse("OAuth chain is dead — cannot place; re-run `alphalens broker auth`")
    if os.environ.get(ALLOW_ORDERS_ENV) != "1":
        return Refuse(f"{ALLOW_ORDERS_ENV} != '1' — master arm not set, placement inert")

    # No portfolio-gross rail here. It lived here until #1192 and could not
    # work: it compared a journal sum in INSTRUMENT currency against a limit in
    # ACCOUNT currency (~3.7x looser than it read on a PLN account holding USD
    # instruments), it ran before sizing so the candidate never counted, and it
    # saw only WORKING verdicts so filled exposure dropped out. The rail is
    # `control_loop._check_gross_cap`, which runs post-sizing with `fx` in hand
    # and folds working + candidate + filled + watching in one currency.
    #
    # Repairing the FULL rail in place was not possible, though the committed
    # term alone was: each journaled record carries its own `fx_rate`, so that
    # one term could have been valued correctly here. The candidate has no rate
    # and no size until `_resolve_and_size` runs (after this function), and
    # filled exposure needs a rate too. A currency-correct but candidate-blind
    # and filled-blind rail still cannot bound exposure, so it was removed
    # rather than kept half-correct. `PORTFOLIO_GROSS_FRAC_ENV` and its default stay
    # exported — `_check_gross_cap` reads the env THROUGH them so the two can
    # never disagree on the limit, and `live_rails` pins the name at boot.

    loss_limit_r = abs(_float_env(DAILY_LOSS_LIMIT_R_ENV, DEFAULT_DAILY_LOSS_LIMIT_R))
    if journal_view.realized_r_today <= -loss_limit_r:
        return Refuse(
            f"daily realized r {journal_view.realized_r_today:+.2f} <= "
            f"-{loss_limit_r:.2f} daily-loss limit — the day is closed to new picks"
        )

    return Allow()


__all__ = [
    "ALLOW_ORDERS_ENV",
    "DAILY_LOSS_LIMIT_R_ENV",
    "DEFAULT_DAILY_LOSS_LIMIT_R",
    "DEFAULT_PORTFOLIO_GROSS_FRAC",
    "PORTFOLIO_GROSS_FRAC_ENV",
    "Allow",
    "Decision",
    "JournalView",
    "Refuse",
    "SessionState",
    "check",
]
