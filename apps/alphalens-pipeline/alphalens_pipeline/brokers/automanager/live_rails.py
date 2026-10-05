"""LIVE boot-assert — design memo §3 point 2 / ADR 0017 point 4.

**The most dangerous verified fact this guards against:** the code defaults
of the safety rails are permissive — ``safety.DEFAULT_PORTFOLIO_GROSS_FRAC =
1.0`` (100% gross) and ``safety.DEFAULT_DAILY_LOSS_LIMIT_R = 3.0``. The per-pick
amount ceiling and the round-trip fee floor have no live-safe default either
(an unset ceiling admits any amount a document states, and an unset fee floor
admits any cost), and an unset or ``clamped`` sizing mode switches the cash
floor off. A LIVE unit missing one pin would trade 100% gross of the real
balance instead of failing to boot.

``assert_live_rails`` refuses to let a LIVE instance start unless ALL SIX of
``ALPHALENS_BROKER_PORTFOLIO_GROSS_FRAC``, ``ALPHALENS_BROKER_DAILY_LOSS_LIMIT_R``,
``ALPHALENS_BROKER_SIZING_EQUITY_MODE`` (the cash-floor switch, which must be
``declared``), ``ALPHALENS_BROKER_MAX_FEE_BPS``,
``ALPHALENS_BROKER_MAX_PICK_NOTIONAL`` (the per-pick amount ceiling, #1467) and
``ALPHALENS_BROKER_ENTRY_TRAIL_BPS`` (the entry-trailing distance, memo
``docs/research/entry_trailing_design_2026_08_12.md`` §6 — an operator must
explicitly state ``0`` = trailing off rather than inherit it) are EXPLICITLY
set AND within the live bounds table below (:data:`LIVE_RAIL_ENVS`). Every
violation is collected and reported TOGETHER (not fail-fast on the first one)
so an operator with a unit file missing several pins fixes it in one edit
instead of one restart per missing pin.

Removed pins, kept here so a reader does not go looking for them:

* ``ALPHALENS_BROKER_EXIT_POLICY`` until #1414. It selected whether a document's
  own exit levels were placed; the document says that itself now.
* ``ALPHALENS_BROKER_MAX_OPEN`` and ``ALPHALENS_BROKER_ENTRY_WATCH_MAX_PICKS``
  until #1732 (owner decision 2026-10-05): free capital is the only limit on
  how many picks LIVE takes. Neither count ever bounded money; the account is
  bounded by the gross cap (``PORTFOLIO_GROSS_FRAC x total_value``), the cash
  floor (candidate x 1.04 + resting entries + watching reservations <=
  ``margin_available``), the per-pick amount and the fee floor. That is why
  ``declared`` is now the only sizing mode LIVE may boot with: with no count
  left, the cash floor is the rail that bounds how much LIVE funds at once.

The numeric bounds (PORTFOLIO_GROSS_FRAC <= 1.0, DAILY_LOSS_LIMIT_R <= 2.0,
MAX_FEE_BPS <= 1000, MAX_PICK_NOTIONAL <= 15000, ENTRY_TRAIL_BPS <= 150) are the
operator-decided §8 caps for the soak — NOT a mechanism for widening risk later
without also widening this assert. PORTFOLIO_GROSS_FRAC widened 0.5 -> 1.0 on
2026-09-08 (operator decision, memo
``docs/research/broker_live_daemon_arm_design_2026_08_10.md`` §8 point 3): the
operator bounds risk by DIVERSIFICATION across names, not by a fraction of
equity held back; at 1.0 the whole account may be committed, never more (1.0 is
the no-leverage line — the account carries no margin agreement).

The sizing frame (ALPHALENS_BROKER_SIZING_EQUITY) and MAX_FEE_BPS carried a
floor but no CEILING until issue #1121: the frame was the direct multiplier on
position size, so a typo of 150000 for 15000 passed every check. #1467 removed
the frame — a pick states its own amount — and MAX_PICK_NOTIONAL carries the
frame's 15000 ceiling over as the bound on one pick's amount. Every ceiling here
was the value the LIVE unit already ran on the day it was added, so widening one
is a reviewed code edit instead of a silent host edit.

Env-var NAMES are imported from their owning modules (``safety.py`` for the
two portfolio rails ``safety`` and ``pick_money_gates`` read,
``entry_trails.py`` for the trailing distance) — never re-declared as string
literals, so the boot-assert and the runtime reader can never drift onto
different env var names. ``MAX_FEE_BPS_ENV``, ``MAX_PICK_NOTIONAL_ENV`` and
``SIZING_EQUITY_MODE_ENV`` are owned here; their runtime readers are in
``pick_money_gates``.
"""

from __future__ import annotations

import os

from broker_contract.contract import BrokerCapabilityError

from alphalens_pipeline.brokers.automanager.entry_trails import (
    ENTRY_TRAIL_BPS_ENV,
    ENTRY_TRAIL_BPS_MAX,
)
from alphalens_pipeline.brokers.automanager.safety import (
    DAILY_LOSS_LIMIT_R_ENV,
    PORTFOLIO_GROSS_FRAC_ENV,
)

# The round-trip fee floor, read by the placement drain.
MAX_FEE_BPS_ENV = "ALPHALENS_BROKER_MAX_FEE_BPS"
# The largest account-currency amount one pick may state (#1467). Read by the
# placement drain, which refuses a larger pick before the day-1 gate.
MAX_PICK_NOTIONAL_ENV = "ALPHALENS_BROKER_MAX_PICK_NOTIONAL"

# The cash-floor switch. Its name is historical: it used to select how the
# sizing frame was resolved (memo broker_sizing_declared_frame_design §4.1), and
# since #1467 removed the frame, ``declared`` means one thing only — the cash
# floor runs (pick_money_gates._check_cash_floor, status_snapshot._cash_floor).
# Renaming the variable is a unit change on the VPS, left for a separate step.
SIZING_EQUITY_MODE_ENV = "ALPHALENS_BROKER_SIZING_EQUITY_MODE"
SIZING_MODE_CLAMPED = "clamped"  # the cash floor is off
SIZING_MODE_DECLARED = "declared"  # the cash floor is on; the only mode LIVE boots with

# Operator-decided §8 soak bounds (design memo §3 table). Widening risk later
# is a design-memo decision, not a silent constant edit here.
_PORTFOLIO_GROSS_FRAC_UPPER = 1.0  # 0.5 -> 1.0 on 2026-09-08, see the module docstring
_DAILY_LOSS_LIMIT_R_UPPER = 2.0

# Operator-locked §8 soak bounds exactly like the two above — PRESCRIPTIVE,
# not a snapshot of what the host happens to run. They are equal to the running
# values because that is the point: the ceiling is the last deliberate decision,
# so widening one is a design-memo decision here, never a silent host edit.
#
# The fee floor was checked for POSITIVITY only until issue #1121. Each ceiling
# is the value the LIVE unit already runs, so bounding it changed nothing on the
# day it shipped; every future widening is a reviewed code change.
_MAX_FEE_BPS_UPPER = 1_000.0
# #1467 replaced "percent x frame" with an amount the document states. The frame
# ceiling (15000) had been the only bound on one pick's size; this carries the
# same number over, so the day it shipped nothing widened.
_MAX_PICK_NOTIONAL_UPPER = 15_000.0


LIVE_RAIL_ENVS: tuple[str, ...] = (
    PORTFOLIO_GROSS_FRAC_ENV,
    DAILY_LOSS_LIMIT_R_ENV,
    SIZING_EQUITY_MODE_ENV,
    MAX_FEE_BPS_ENV,
    MAX_PICK_NOTIONAL_ENV,
    ENTRY_TRAIL_BPS_ENV,
)
"""Every env var :func:`assert_live_rails` pins, in its check order."""


def _missing_or_blank(raw: str | None) -> bool:
    return raw is None or not raw.strip()


def _check_int_bounded(
    var: str,
    *,
    lo: int,
    hi: int,
    unset_reason: str = "the code default is permissive",
) -> str | None:
    """``None`` if ``var`` is set to an int in ``[lo, hi]``, else a violation.

    ``unset_reason`` tailors the unset-violation wording: the default fits
    the rails whose code default is dangerous; a pin whose
    unset default is SAFE (entry trailing: unset = off) must say so instead —
    the generic wording would nudge an operator toward a nonzero value for
    the wrong reason."""
    raw = os.environ.get(var)
    if _missing_or_blank(raw):
        return f"{var}: must be explicitly set (unset — {unset_reason})"
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


def _check_sizing_mode(var: str) -> str | None:
    """``None`` iff ``var`` is explicitly set AND (case-insensitively)
    ``declared`` — the only mode that runs the cash floor. ``clamped`` (or any
    other value) switches the floor off, and since #1732 removed the count
    limits the floor is what bounds how much LIVE funds at once, so LIVE
    refuses to boot without it. A blank value fails the explicit-set check."""
    raw = os.environ.get(var)
    if _missing_or_blank(raw):
        return f"{var}: must be explicitly set (unset — the cash floor must be switched on)"
    mode = raw.strip().lower()  # type: ignore[union-attr]  # raw is non-None past the blank check
    if mode != SIZING_MODE_DECLARED:
        return (
            f"{var}: must be {SIZING_MODE_DECLARED!r} on LIVE (any other value switches "
            f"the cash floor off), got {raw!r}"
        )
    return None


def assert_live_rails() -> None:
    """Refuse to let a LIVE instance boot unless all six safety-rail env vars
    (:data:`LIVE_RAIL_ENVS`) are explicitly set and within the live-soak bounds
    (design memo §3 point 2 / ADR 0017 point 4; the entry-trailing distance
    follows the entry-trailing design memo §6 — explicit ``"0"`` = trailing
    off). No count of picks, positions or watches is a rail (#1732).

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
            _check_sizing_mode(SIZING_EQUITY_MODE_ENV),
            _check_float_bounded(
                MAX_FEE_BPS_ENV, exclusive_lo=0.0, inclusive_hi=_MAX_FEE_BPS_UPPER
            ),
            _check_float_bounded(
                MAX_PICK_NOTIONAL_ENV,
                exclusive_lo=0.0,
                inclusive_hi=_MAX_PICK_NOTIONAL_UPPER,
            ),
            # Entry-trailing distance (memo §6): [0, 150] — the bound and the
            # env-var name are OWNED by entry_trails.py; explicit "0" (feature
            # off) is valid, unset fails like every other pin. Custom unset
            # wording: unlike the rails above, this pin's unset code
            # default is SAFE (off) — the operator states a value, not a fix.
            _check_int_bounded(
                ENTRY_TRAIL_BPS_ENV,
                lo=0,
                hi=ENTRY_TRAIL_BPS_MAX,
                unset_reason="explicit 0 = trailing off; the pin must still be stated",
            ),
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
    "ENTRY_TRAIL_BPS_ENV",
    "LIVE_RAIL_ENVS",
    "MAX_FEE_BPS_ENV",
    "MAX_PICK_NOTIONAL_ENV",
    "PORTFOLIO_GROSS_FRAC_ENV",
    "SIZING_EQUITY_MODE_ENV",
    "SIZING_MODE_CLAMPED",
    "SIZING_MODE_DECLARED",
    "assert_live_rails",
]
