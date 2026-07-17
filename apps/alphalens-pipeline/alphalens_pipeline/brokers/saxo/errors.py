"""Saxo exception taxonomy.

Kept in its own leaf module (rather than ``client.py``) so ``tokens.py`` can
raise :class:`SaxoAuthError` without importing the client — preserving the
one-way ``client -> tokens -> errors`` layering. ``client.py`` re-exports the
whole taxonomy, so consumers inside ``brokers/saxo/`` keep a single import
site; NOTHING outside ``brokers/saxo/`` may catch these — ``broker.py``
translates them to the ``contract.Broker*Error`` taxonomy at the adapter
boundary.
"""

from __future__ import annotations


class SaxoError(RuntimeError):
    """Non-transient Saxo failure (schema, permanent 4xx, exhausted retries)."""


class SaxoAuthError(SaxoError):
    """401 after one token-refresh attempt, or no token configured.

    Distinct so callers can short-circuit to operator action (regenerate the
    24h SIM token) instead of retrying.
    """


class SaxoRateLimitError(SaxoError):
    """429 persisted after all retries. Distinct so callers can soft-fail."""


class SaxoNotFoundError(SaxoError):
    """404 on a read. Distinct because for order-status reads an absent order
    is an EXPECTED outcome (the open-orders endpoint drops filled/cancelled/
    expired orders) that the adapter maps to ``OrderStatus.UNKNOWN``."""


class SaxoLiveEnvironmentBlockedError(SaxoError):
    """The SIM-only structural rail refused a LIVE base URL / environment.

    Lifting the rail requires its own future ADR (see ADR 0014) — there is
    deliberately no env-var or constructor switch that reaches LIVE.
    """


__all__ = [
    "SaxoAuthError",
    "SaxoError",
    "SaxoLiveEnvironmentBlockedError",
    "SaxoNotFoundError",
    "SaxoRateLimitError",
]
