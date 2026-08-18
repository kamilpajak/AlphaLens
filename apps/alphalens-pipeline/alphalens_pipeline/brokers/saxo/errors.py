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

from broker_contract.contract import BrokerAuthError


class SaxoError(RuntimeError):
    """Non-transient Saxo failure (schema, permanent 4xx, exhausted retries)."""


class SaxoAuthError(SaxoError, BrokerAuthError):
    """401 after one token-refresh attempt, or no token configured.

    Distinct so callers can short-circuit to operator action (re-run
    ``alphalens broker auth`` — or, on the static-token fallback, regenerate
    the 24h SIM token) instead of retrying.
    """


class SaxoRateLimitError(SaxoError):
    """429 persisted after all retries. Distinct so callers can soft-fail."""


class SaxoNotFoundError(SaxoError):
    """404 on a read. Distinct because for order-status reads an absent order
    is an EXPECTED outcome (the open-orders endpoint drops filled/cancelled/
    expired orders) that the adapter maps to ``OrderStatus.UNKNOWN``."""


class SaxoLiveEnvironmentBlockedError(SaxoError):
    """The structural rail refused a LIVE base URL / environment.

    LIVE is reachable only via the ADR 0015 attended day-bound unlock or the
    ADR 0017 standing account-bound grant — this error means neither was
    validly present (it is also raised by the LIVE factory on a mismatched
    grant and by ``from_env`` on a stray ``SAXO_ENV``). Every default
    construction path stays SIM-only.
    """


__all__ = [
    "SaxoAuthError",
    "SaxoError",
    "SaxoLiveEnvironmentBlockedError",
    "SaxoNotFoundError",
    "SaxoRateLimitError",
]
