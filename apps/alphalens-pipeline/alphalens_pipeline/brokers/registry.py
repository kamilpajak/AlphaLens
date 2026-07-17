"""Lazy broker factory registry + process-wide default broker singleton.

Mirrors the ``get_default_*_client`` house pattern (see
``data/alt_data/polygon_client.py``): module-level lazy singleton with
double-checked locking and a ``_reset_default_broker_for_tests`` hook.

Factories are IMPORT-PATH STRINGS resolved on demand via ``importlib``, so
importing this registry costs nothing — no adapter package (and none of its
vendor deps) loads until a broker is actually requested. Adding a second
broker (IBKR) = one factory entry here + one adapter package; zero consumer
changes.
"""

from __future__ import annotations

import importlib
import os
import threading
from collections.abc import Callable

from alphalens_pipeline.brokers.contract import Broker, BrokerError

# Env var selecting the default broker name when ``get_default_broker`` is
# called without an explicit name.
BROKER_ENV = "ALPHALENS_BROKER"

_DEFAULT_BROKER_NAME = "saxo"

# broker name -> "module.path:factory_callable" (resolved lazily).
_BROKER_FACTORIES: dict[str, str] = {
    "saxo": "alphalens_pipeline.brokers.saxo.broker:create_saxo_broker_from_env",
}

_DEFAULT_BROKER: Broker | None = None
# Guards first-call construction so two threads racing the first call don't
# each build a broker. Double-checked locking (same idiom as the vendor
# client singletons).
_DEFAULT_BROKER_LOCK = threading.Lock()


def _resolve_factory(name: str) -> Callable[[], Broker]:
    spec = _BROKER_FACTORIES.get(name)
    if spec is None:
        raise ValueError(
            f"unknown broker {name!r}; registered brokers: {sorted(_BROKER_FACTORIES)}"
        )
    module_path, _, attr = spec.partition(":")
    try:
        module = importlib.import_module(module_path)
        factory: Callable[[], Broker] = getattr(module, attr)
    except (ImportError, AttributeError) as exc:
        # A mis-registered factory path must surface as a broker error the CLI
        # renders cleanly, not a raw importlib traceback.
        raise BrokerError(f"broker factory for {name!r} ({spec!r}) failed to load: {exc}") from exc
    return factory


def get_default_broker(name: str | None = None) -> Broker:
    """Return the process-wide default :class:`Broker` (lazy-initialized).

    ``name`` defaults to ``$ALPHALENS_BROKER`` (falling back to ``"saxo"``).
    Subsequent calls with the same resolved name return the same instance;
    asking for a DIFFERENT name replaces the singleton (single-broker
    process assumption — the CLI drives one broker at a time). Construction
    is thread-safe via double-checked locking.
    """
    resolved = name or os.environ.get(BROKER_ENV, _DEFAULT_BROKER_NAME)
    global _DEFAULT_BROKER  # noqa: PLW0603 — lazy singleton is the documented pattern
    current = _DEFAULT_BROKER
    if current is not None and current.name == resolved:
        return current
    with _DEFAULT_BROKER_LOCK:
        if _DEFAULT_BROKER is None or _DEFAULT_BROKER.name != resolved:
            _DEFAULT_BROKER = _resolve_factory(resolved)()
        return _DEFAULT_BROKER


def _reset_default_broker_for_tests() -> None:
    """Test-only hook: clear the cached singleton so each test starts clean."""
    global _DEFAULT_BROKER  # noqa: PLW0603 — lazy singleton is the documented pattern
    _DEFAULT_BROKER = None


__all__ = [
    "BROKER_ENV",
    "_reset_default_broker_for_tests",
    "get_default_broker",
]
