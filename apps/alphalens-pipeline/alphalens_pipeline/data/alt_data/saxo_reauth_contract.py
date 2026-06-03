"""Auth-death -> unmanaged-positions CONTRACT (impl lands with the order PR).

The order/exit layer is OUT of scope for this PR, but the contract it must
honor on an auth death ships now so the future order PR cannot silently
swallow a :class:`SaxoReauthRequiredError` (hard-cap Finding 3 / CRITICAL):

When a refresh hits ``invalid_grant`` while the access token still has life
AND an open position exists, the position-management path must permit EXACTLY
ONE best-effort protective-exit attempt with the still-valid token BEFORE
going dark — it must NOT blanket-raise immediately. The gauge
``alphalens_saxo_positions_unmanaged`` (named here, set by the future exit
manager) surfaces the resulting unmanaged state.

:func:`handle_reauth_required_for_positions` is the executable contract. The
real flatten / ensure-stops body is wired in by the order PR; here the
``protective_exit`` callable is injected so the contract is testable without
any broker dependency.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from alphalens_pipeline.data.alt_data.saxo_client import SaxoReauthRequiredError

logger = logging.getLogger(__name__)

# Contract gauge NAME — set to 1 by the future exit manager on auth death.
# Pinned in test_saxo_metrics_allowlist now so the name is the single source of
# truth before any emitter exists.
POSITIONS_UNMANAGED_GAUGE = "alphalens_saxo_positions_unmanaged"


def handle_reauth_required_for_positions(
    *,
    error: SaxoReauthRequiredError,
    access_token_still_valid: bool,
    has_open_position: bool,
    protective_exit: Callable[[], None],
) -> None:
    """Execute the auth-death contract, then re-raise so trading halts.

    Args:
        error: the reauth error that broke the chain.
        access_token_still_valid: whether the access token is still inside its
            ~20-min tail (a flatten can only be attempted while it is valid).
        has_open_position: whether there is a live position to protect.
        protective_exit: best-effort flatten / ensure-stops callable. Invoked
            AT MOST ONCE; any exception it raises is swallowed (the position is
            unmanaged regardless — we still re-raise the reauth error).

    Raises:
        SaxoReauthRequiredError: always — the chain is dead, trading is halted
            until a human re-auths. The single protective-exit attempt happens
            first when both preconditions hold.
    """
    if has_open_position and access_token_still_valid:
        # Exactly one best-effort attempt with the still-valid token.
        try:
            protective_exit()
        except Exception:  # best-effort flatten; never mask the reauth death
            logger.warning(
                "saxo protective-exit attempt failed on auth death; position is unmanaged"
            )
    elif has_open_position:
        logger.warning(
            "saxo auth death with an open position but no valid access token — "
            "cannot flatten; position is unmanaged"
        )
    # Re-raise so the caller's trading loop halts; the order PR also emits
    # POSITIONS_UNMANAGED_GAUGE at this point.
    raise error


__all__ = [
    "POSITIONS_UNMANAGED_GAUGE",
    "handle_reauth_required_for_positions",
]
