"""LIVE boot-assert — design memo §3 point 2 / ADR 0017 point 4.

**The most dangerous verified fact this guards against:** the code defaults
of the safety rails are permissive — ``safety.DEFAULT_MAX_OPEN = 3``,
``safety.DEFAULT_PORTFOLIO_GROSS_FRAC = 1.0`` (100% gross), and
``safety.DEFAULT_DAILY_LOSS_LIMIT_R = 3.0``. Sizing equity, the exit policy,
and the round-trip fee floor have no live-safe default either (raw account
snapshot sizing, the silently-inert ``setup_static`` exit geometry, and an
unset fee floor respectively). A LIVE unit missing one pin would trade 100%
gross of the real balance instead of failing to boot.

``assert_live_rails`` refuses to let a LIVE instance start unless ALL SIX of
``ALPHALENS_BROKER_MAX_OPEN``, ``ALPHALENS_BROKER_PORTFOLIO_GROSS_FRAC``,
``ALPHALENS_BROKER_DAILY_LOSS_LIMIT_R``, ``ALPHALENS_BROKER_SIZING_EQUITY``,
``ALPHALENS_BROKER_EXIT_POLICY``, and ``ALPHALENS_BROKER_MAX_FEE_BPS`` are
EXPLICITLY set AND within the live bounds table below. Every violation is
collected and reported TOGETHER (not fail-fast on the first one) so an
operator with a unit file missing several pins fixes it in one edit instead
of one restart per missing pin. ``EXIT_POLICY`` is additionally checked
against the exit-policy registry HERE, at boot — a copy-paste unit with a
typo'd policy name must never reach the per-tick protection pass, where a
``ValueError`` would starve every position that tick.

The four numeric bounds (MAX_OPEN <= 2, PORTFOLIO_GROSS_FRAC <= 0.5,
DAILY_LOSS_LIMIT_R <= 2.0, and the strictly-positive SIZING_EQUITY /
MAX_FEE_BPS floors) are the operator-decided §8 caps for the soak — NOT a
mechanism for widening risk later without also widening this assert.

Env-var NAMES are imported from their owning modules (``safety.py`` for the
three portfolio rails already used by ``safety.check``,
``position_manager.py`` for the exit-policy flag already used by
``control_loop.build_default_deps``) — never re-declared as string literals,
so the boot-assert and the runtime reader can never drift onto different env
var names. ``SIZING_EQUITY_ENV`` and ``MAX_FEE_BPS_ENV`` are new (no PR-A
consumer reads sizing equity or the fee floor yet — PR-B wires them).
"""

from __future__ import annotations

import math
import os

from broker_contract.contract import BrokerCapabilityError
from broker_contract.exit_geometry.registry import resolve_exit_policy

from alphalens_pipeline.brokers.automanager.position_manager import _EXIT_POLICY_ENV
from alphalens_pipeline.brokers.automanager.safety import (
    DAILY_LOSS_LIMIT_R_ENV,
    MAX_OPEN_ENV,
    PORTFOLIO_GROSS_FRAC_ENV,
)

EXIT_POLICY_ENV = _EXIT_POLICY_ENV

# New env-var names — no PR-A consumer reads either yet (PR-B wires the
# min(pinned, snapshot) sizing and the round-trip fee floor).
SIZING_EQUITY_ENV = "ALPHALENS_BROKER_SIZING_EQUITY"
MAX_FEE_BPS_ENV = "ALPHALENS_BROKER_MAX_FEE_BPS"

# Operator-decided §8 soak bounds (design memo §3 table). Widening risk later
# is a design-memo decision, not a silent constant edit here.
_MAX_OPEN_LOWER = 1
_MAX_OPEN_UPPER = 2
_PORTFOLIO_GROSS_FRAC_UPPER = 0.5
_DAILY_LOSS_LIMIT_R_UPPER = 2.0


def _missing_or_blank(raw: str | None) -> bool:
    return raw is None or not raw.strip()


def _check_int_bounded(var: str, *, lo: int, hi: int) -> str | None:
    """``None`` if ``var`` is set to an int in ``[lo, hi]``, else a violation."""
    raw = os.environ.get(var)
    if _missing_or_blank(raw):
        return f"{var}: must be explicitly set (unset — the code default is permissive)"
    try:
        value = int(raw)  # type: ignore[arg-type]  # raw is non-None past the blank check
    except ValueError:
        return f"{var}: must be an integer, got {raw!r}"
    if not lo <= value <= hi:
        return f"{var}: must be in [{lo}, {hi}] for the live soak, got {value}"
    return None


def _check_float_bounded(var: str, *, exclusive_lo: float, inclusive_hi: float) -> str | None:
    """``None`` if ``var`` is set to a float in ``(exclusive_lo, inclusive_hi]``,
    else a violation."""
    raw = os.environ.get(var)
    if _missing_or_blank(raw):
        return f"{var}: must be explicitly set (unset — the code default is permissive)"
    try:
        value = float(raw)  # type: ignore[arg-type]  # raw is non-None past the blank check
    except ValueError:
        return f"{var}: must be a number, got {raw!r}"
    if not exclusive_lo < value <= inclusive_hi:
        return f"{var}: must be in ({exclusive_lo}, {inclusive_hi}] for the live soak, got {value}"
    return None


def _check_float_positive(var: str) -> str | None:
    """``None`` if ``var`` is set to a strictly-positive float, else a violation."""
    raw = os.environ.get(var)
    if _missing_or_blank(raw):
        return f"{var}: must be explicitly set (unset — the code default is permissive)"
    try:
        value = float(raw)  # type: ignore[arg-type]  # raw is non-None past the blank check
    except ValueError:
        return f"{var}: must be a number, got {raw!r}"
    # math.isfinite: inf > 0 and nan fails every comparison, so a bare
    # `<= 0` check would boot a live daemon with an unbounded frame/floor.
    if not math.isfinite(value) or value <= 0:
        return f"{var}: must be a finite number > 0, got {value}"
    return None


def _check_exit_policy(var: str) -> str | None:
    """``None`` iff ``var`` is explicitly set AND resolves against the
    exit-policy registry. A blank value fails the explicit-set check (never
    silently falls back to ``position_manager``'s own ``setup_static``
    default — a copy-paste unit missing this pin must fail loud here, at
    boot, not silently run the wrong exit mechanism)."""
    raw = os.environ.get(var)
    if _missing_or_blank(raw):
        return f"{var}: must be explicitly set (unset would silently run setup_static)"
    name = raw.strip()  # type: ignore[union-attr]  # raw is non-None past the blank check
    try:
        resolve_exit_policy(name)
    except ValueError as exc:
        return f"{var}: {exc}"
    return None


def assert_live_rails() -> None:
    """Refuse to let a LIVE instance boot unless all six safety-rail env vars
    are explicitly set and within the live-soak bounds (design memo §3 point
    2 / ADR 0017 point 4).

    Call ONCE, at LIVE composition-root time, BEFORE any broker/network I/O —
    mirrors the two ADR 0016 state-safety guards already run first in
    ``control_loop.build_default_deps`` (D7/D4). Collects EVERY violation and
    raises exactly one :class:`BrokerCapabilityError` naming all of them,
    rather than failing fast on the first — an operator with several missing
    pins fixes the unit file once.
    """
    violations = [
        v
        for v in (
            _check_int_bounded(MAX_OPEN_ENV, lo=_MAX_OPEN_LOWER, hi=_MAX_OPEN_UPPER),
            _check_float_bounded(
                PORTFOLIO_GROSS_FRAC_ENV,
                exclusive_lo=0.0,
                inclusive_hi=_PORTFOLIO_GROSS_FRAC_UPPER,
            ),
            _check_float_bounded(
                DAILY_LOSS_LIMIT_R_ENV,
                exclusive_lo=0.0,
                inclusive_hi=_DAILY_LOSS_LIMIT_R_UPPER,
            ),
            _check_float_positive(SIZING_EQUITY_ENV),
            _check_exit_policy(EXIT_POLICY_ENV),
            _check_float_positive(MAX_FEE_BPS_ENV),
        )
        if v is not None
    ]
    if violations:
        raise BrokerCapabilityError(
            "LIVE boot-assert failed (design memo §3 point 2 / ADR 0017 point 4) — "
            f"{len(violations)} rail(s) missing or out of live-soak bounds:\n"
            + "\n".join(f"  - {line}" for line in violations)
        )


__all__ = [
    "DAILY_LOSS_LIMIT_R_ENV",
    "EXIT_POLICY_ENV",
    "MAX_FEE_BPS_ENV",
    "MAX_OPEN_ENV",
    "PORTFOLIO_GROSS_FRAC_ENV",
    "SIZING_EQUITY_ENV",
    "assert_live_rails",
]
