"""Where the account cash book lives on disk (#1689, memo section 3).

```
~/.alphalens/account_cash/<env>/cash_events.jsonl
~/.alphalens/account_cash/<env>/trade_rows.jsonl
~/.alphalens/account_cash/<env>/balances.jsonl
~/.alphalens/account_cash/<env>/sweeps.jsonl
~/.alphalens/account_cash/<env>/.sweep.lock
```

A NEW root, deliberately not inside ``broker_orders/<env>/``: that root is the
daemon's live money-path state, and a derived record sitting among four queue
journals is eventually read as queue state. Per-env, never pooled, the way
``exec_quality_parquet`` already keeps SIM and LIVE fills apart.

**Declared here and not in** ``brokers/automanager/state_paths.py``. That module
is the one BROKER-STATE seam (ADR 0016) and it travels with the Stage-1 keeper
split, while this layer's semantics stay in AlphaLens. Declaring the path there
would invert the split argument and create exactly the import edge this layer's
own dependency rule forbids (``tests/test_module_dependencies.py``, rule 2).

:func:`_alphalens_home` is the ONE ``Path.home()`` caller in the whole package,
and every public builder goes through it. That is ADR 0016's call-time rule and
the only seam the test suite's operator-state guard can wrap: its predicate
matches ``call.func.attr == "home"``, so a ``Path("~/.alphalens").expanduser()``
form would be both unguarded and invisible to the gate that reads the guarded
set off this source. A test redirects the whole store by patching
``pathlib.Path.home``.

``env`` is a REQUIRED positional on every builder, validated, and this module
reads no environment variable. Two reasons: a write must never be steered by an
ambient variable (#1377, stated at ``alphalens_cli/commands/broker.py``), and a
second spelling of ``ALPHALENS_BROKER_ENVIRONMENT`` here would mean importing
the keeper's state seam, which rule 2 forbids. There is deliberately NO
``root=`` keyword either: an override that never calls :func:`_alphalens_home`
proves nothing about the test that uses it.
"""

from __future__ import annotations

from pathlib import Path

ENV_SIM = "sim"
ENV_LIVE = "live"

#: The accepted environments, in the keeper's own order. Pinned against
#: ``state_paths.{ENV_SIM, ENV_LIVE}`` by
#: ``tests/account_cash/test_env_vocabulary_parity.py``, because rule 2 stops
#: the two modules sharing the strings.
ENVIRONMENTS: tuple[str, ...] = (ENV_SIM, ENV_LIVE)

_ALPHALENS_HOME_DIRNAME = ".alphalens"
_ACCOUNT_CASH_DIRNAME = "account_cash"

_CASH_EVENTS_FILENAME = "cash_events.jsonl"
_TRADE_ROWS_FILENAME = "trade_rows.jsonl"
_BALANCES_FILENAME = "balances.jsonl"
_SWEEPS_FILENAME = "sweeps.jsonl"
_SWEEP_LOCK_FILENAME = ".sweep.lock"

__all__ = [
    "ENVIRONMENTS",
    "ENV_LIVE",
    "ENV_SIM",
    "account_cash_root",
    "balances_path",
    "cash_events_path",
    "sweep_lock_path",
    "sweeps_path",
    "trade_rows_path",
    "validate_environment",
]


def _alphalens_home() -> Path:
    """``~/.alphalens``, resolved AT CALL TIME. The package's one home seam."""
    return Path.home() / _ALPHALENS_HOME_DIRNAME


def validate_environment(value: str) -> str:
    """``value`` itself when it is a known environment, else ``ValueError``.

    Fail-loud and naming the offending value, the ``state_paths`` shape: a path
    built from an unvalidated string would silently create a third store root
    that nothing ever reads.
    """
    if value not in ENVIRONMENTS:
        raise ValueError(
            f"{value!r} is not a supported account-cash environment; expected one of {ENVIRONMENTS}"
        )
    return value


def account_cash_root(env: str) -> Path:
    """``~/.alphalens/account_cash/<env>`` — the per-environment store root."""
    return _alphalens_home() / _ACCOUNT_CASH_DIRNAME / validate_environment(env)


def cash_events_path(env: str) -> Path:
    """``<root>/cash_events.jsonl`` — one line per booking row, keyed ``BkAmountId``."""
    return account_cash_root(env) / _CASH_EVENTS_FILENAME


def trade_rows_path(env: str) -> Path:
    """``<root>/trade_rows.jsonl`` — one line per trades-report row, keyed ``TradeId``."""
    return account_cash_root(env) / _TRADE_ROWS_FILENAME


def balances_path(env: str) -> Path:
    """``<root>/balances.jsonl`` — one line per sweep: the balance snapshot."""
    return account_cash_root(env) / _BALANCES_FILENAME


def sweeps_path(env: str) -> Path:
    """``<root>/sweeps.jsonl`` — one line per sweep: asked for, and what came back."""
    return account_cash_root(env) / _SWEEPS_FILENAME


def sweep_lock_path(env: str) -> Path:
    """``<root>/.sweep.lock`` — the flock'd file one sweep holds at a time."""
    return account_cash_root(env) / _SWEEP_LOCK_FILENAME
