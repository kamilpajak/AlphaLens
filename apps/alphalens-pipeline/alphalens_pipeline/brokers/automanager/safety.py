"""Pure-predicate portfolio safety gate for the Saxo auto-manager.

check(...) runs for every armed pick BEFORE any placement. Pure function of
inputs + two process rails read at call time (KILL file, ALLOW_ORDERS). Places,
cancels, and writes nothing: the daily-loss branch RETURNS Refuse; tripping the
KILL file is the control loop's job. Refusal order (first failing rail wins):
KILL file -> chain dead -> ALLOW_ORDERS != '1' -> MAX_OPEN cap -> portfolio
gross cap -> daily-loss limit. The cap numbers are operator policy with no
validated basis (memo risk 7) — set conservatively.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

DEFAULT_KILL_PATH = Path.home() / ".alphalens" / "broker_orders" / "KILL"

ALLOW_ORDERS_ENV = "ALPHALENS_BROKER_ALLOW_ORDERS"
MAX_OPEN_ENV = "ALPHALENS_BROKER_MAX_OPEN"
PORTFOLIO_GROSS_FRAC_ENV = "ALPHALENS_BROKER_PORTFOLIO_GROSS_FRAC"
DAILY_LOSS_LIMIT_R_ENV = "ALPHALENS_BROKER_DAILY_LOSS_LIMIT_R"

DEFAULT_MAX_OPEN = 3
DEFAULT_PORTFOLIO_GROSS_FRAC = 1.0
DEFAULT_DAILY_LOSS_LIMIT_R = 3.0


@dataclass(frozen=True)
class Allow:
    """The pick clears every rail and may be placed."""


@dataclass(frozen=True)
class Refuse:
    reason: str


Decision = Allow | Refuse


class SessionState(Protocol):
    alive: bool


@dataclass(frozen=True)
class JournalView:
    open_bracket_count: int
    gross_committed: float
    realized_r_today: float


@dataclass(frozen=True)
class BrokerView:
    open_position_count: int
    equity: float


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


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
    broker_view: BrokerView,
    session_state: SessionState,
    *,
    kill_path: Path | None = None,
) -> Decision:
    """Return Allow iff every rail clears; else the first Refuse. Pure predicate."""
    kill = kill_path or DEFAULT_KILL_PATH
    if kill.exists():
        return Refuse(f"KILL file present at {kill} — emergency stop, placement halted")
    if not session_state.alive:
        return Refuse("OAuth chain is dead — cannot place; re-run `alphalens broker auth`")
    if os.environ.get(ALLOW_ORDERS_ENV) != "1":
        return Refuse(f"{ALLOW_ORDERS_ENV} != '1' — master arm not set, placement inert")

    max_open = _int_env(MAX_OPEN_ENV, DEFAULT_MAX_OPEN)
    open_total = journal_view.open_bracket_count + broker_view.open_position_count
    if open_total >= max_open:
        return Refuse(
            f"open brackets+positions {open_total} >= MAX_OPEN {max_open} — refusing new pick"
        )

    gross_frac = _float_env(PORTFOLIO_GROSS_FRAC_ENV, DEFAULT_PORTFOLIO_GROSS_FRAC)
    gross_limit = gross_frac * broker_view.equity
    if journal_view.gross_committed > gross_limit:
        return Refuse(
            f"committed gross {journal_view.gross_committed:,.2f} exceeds portfolio cap "
            f"{gross_limit:,.2f} ({gross_frac:g} x equity {broker_view.equity:,.2f})"
        )

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
    "DEFAULT_KILL_PATH",
    "DEFAULT_MAX_OPEN",
    "DEFAULT_PORTFOLIO_GROSS_FRAC",
    "MAX_OPEN_ENV",
    "PORTFOLIO_GROSS_FRAC_ENV",
    "Allow",
    "BrokerView",
    "Decision",
    "JournalView",
    "Refuse",
    "SessionState",
    "check",
]
