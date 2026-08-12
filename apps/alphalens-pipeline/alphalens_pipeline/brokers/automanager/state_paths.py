"""The ONE broker-state path seam (ADR 0016, design memo D2-D4).

Instance identity is one env var, ``ALPHALENS_BROKER_ENVIRONMENT ∈
{"sim", "live"}`` (default ``"sim"``): the future LIVE daemon and the
existing SIM daemon are the same binary, distinguished only by this knob.
Every mutable broker-state path — journals, pick inbox, KILL gate,
execution-quality telemetry, Prometheus job labels — funnels through this
module so a future state move (repo split, transport reimpl) touches one
file instead of the six scattered ``Path.home()`` joins this module
replaces (``control_loop.py``, ``submission_log.py``, ``picks.py``,
``safety.py``, ``exec_quality.py``).

All functions resolve ``Path.home()`` and the env var AT CALL TIME — never
as import-time ``Path`` constants — so tests can patch either freely and a
long-lived process (the daemon) always re-reads the current environment
rather than freezing it at import.

Every env-scoped function accepts ``env: str | None = None``: ``None``
resolves via :func:`broker_environment`; an explicit value lets a caller
(e.g. the CLI's ``--env live`` flag) target the OTHER instance and is
validated identically to the env var.
"""

from __future__ import annotations

import os
from pathlib import Path

# --- Instance identity -------------------------------------------------------

BROKER_ENVIRONMENT_ENV = "ALPHALENS_BROKER_ENVIRONMENT"

ENV_SIM = "sim"
ENV_LIVE = "live"

_VALID_ENVIRONMENTS = (ENV_SIM, ENV_LIVE)

# --- Filesystem layout constants (named so the literals are not duplicated
# across the per-env path builders below) -------------------------------------

_ALPHALENS_HOME_DIRNAME = ".alphalens"
_BROKER_ORDERS_DIRNAME = "broker_orders"
_EXEC_QUALITY_DIRNAME = "exec_quality"

_KILL_FILENAME = "KILL"
_SUBMISSIONS_FILENAME = "submissions.jsonl"
_PICKS_FILENAME = "picks.jsonl"
_STANDALONE_STOPS_FILENAME = "standalone_stops.jsonl"
_ENTRY_TRAILS_FILENAME = "entry_trails.jsonl"
_TRANCHE_FILLS_FILENAME = "tranche_fills.parquet"

# The pre-migration flat layout (D4): these three journals used to live
# directly under broker_orders/ with no per-env subdirectory. A KILL file at
# that same flat level is NOT legacy — it is the current, still-valid GLOBAL
# kill (D3) — so it is deliberately excluded from this tuple. The entry-trails
# journal (PR-T0) was born INTO the per-env layout and never existed flat, so
# it is deliberately excluded too.
LEGACY_FLAT_STATE_FILENAMES = (
    _SUBMISSIONS_FILENAME,
    _PICKS_FILENAME,
    _STANDALONE_STOPS_FILENAME,
)

# --- Prometheus job-name components ------------------------------------------

_METRICS_JOB_PREFIX = "broker-manager"
_STREAM_METRICS_JOB_SUFFIX = "stream"
_PRICE_STREAM_JOB_PREFIX = "live-price-stream"


class BrokerStateLayoutError(RuntimeError):
    """Raised when durable broker state is still in the pre-migration flat layout.

    Fail-loud, not fail-empty (D4): a daemon started against an empty
    per-env root while the broker still holds positions from the OLD flat
    journals would reconcile against nothing and silently degrade its
    protection logic to the adopt/alert paths — worse than refusing to
    start. Migrating the three flat journals into their per-env
    subdirectory (see :data:`LEGACY_FLAT_STATE_FILENAMES` and
    ``docs/research/broker_env_state_separation_design_2026_08_10.md`` §6)
    is a one-time operator step, not something this module should paper
    over.
    """


def _alphalens_home() -> Path:
    return Path.home() / _ALPHALENS_HOME_DIRNAME


def _validate_environment(value: str) -> str:
    if value not in _VALID_ENVIRONMENTS:
        raise ValueError(
            f"{BROKER_ENVIRONMENT_ENV}={value!r} is not a supported broker "
            f"environment; expected one of {_VALID_ENVIRONMENTS}"
        )
    return value


def broker_environment() -> str:
    """Return the resolved instance identity: ``"sim"`` or ``"live"``.

    Reads ``$ALPHALENS_BROKER_ENVIRONMENT`` at call time, defaulting to
    :data:`ENV_SIM`. Any other value fails loud with a ``ValueError`` naming
    both the env var and the offending value (D1).
    """
    return _validate_environment(os.environ.get(BROKER_ENVIRONMENT_ENV, ENV_SIM))


def _resolve_env(env: str | None) -> str:
    """Resolve an optional explicit ``env`` override to a validated value.

    ``None`` resolves via :func:`broker_environment`; an explicit value is
    validated identically (same rejection message), so a caller targeting
    the other instance gets the same fail-loud behavior as the env var.
    """
    if env is None:
        return broker_environment()
    return _validate_environment(env)


# --- Per-instance journal + KILL paths ---------------------------------------


def broker_orders_root(env: str | None = None) -> Path:
    """``~/.alphalens/broker_orders/<env>`` — the per-instance state root."""
    return _alphalens_home() / _BROKER_ORDERS_DIRNAME / _resolve_env(env)


def submissions_path(env: str | None = None) -> Path:
    """``<broker_orders_root>/submissions.jsonl`` for the resolved instance."""
    return broker_orders_root(env) / _SUBMISSIONS_FILENAME


def picks_path(env: str | None = None) -> Path:
    """``<broker_orders_root>/picks.jsonl`` for the resolved instance."""
    return broker_orders_root(env) / _PICKS_FILENAME


def standalone_stops_path(env: str | None = None) -> Path:
    """``<broker_orders_root>/standalone_stops.jsonl`` for the resolved instance."""
    return broker_orders_root(env) / _STANDALONE_STOPS_FILENAME


def entry_trails_path(env: str | None = None) -> Path:
    """``<broker_orders_root>/entry_trails.jsonl`` for the resolved instance.

    Per-tier entry-trailing state journal (entry-trailing design memo §5,
    PR-T0 scaffolding) — sibling of ``standalone_stops.jsonl``, per-env from
    day one (never part of the legacy flat layout)."""
    return broker_orders_root(env) / _ENTRY_TRAILS_FILENAME


def kill_file_path(env: str | None = None) -> Path:
    """``<broker_orders_root>/KILL`` — stops ONLY the resolved instance (D3)."""
    return broker_orders_root(env) / _KILL_FILENAME


def global_kill_file_path() -> Path:
    """``~/.alphalens/broker_orders/KILL`` — the legacy, still-honored GLOBAL
    kill (D3): halts placement in EVERY instance, env-independent by design.
    """
    return _alphalens_home() / _BROKER_ORDERS_DIRNAME / _KILL_FILENAME


def exec_quality_parquet(env: str | None = None) -> Path:
    """``~/.alphalens/exec_quality/<env>/tranche_fills.parquet``.

    SIM and LIVE fills are distinct measurement sources (T8 no-pooling) and
    must never share a file.
    """
    return _alphalens_home() / _EXEC_QUALITY_DIRNAME / _resolve_env(env) / _TRANCHE_FILLS_FILENAME


# --- Prometheus job-name derivations -----------------------------------------


def metrics_job(env: str | None = None) -> str:
    """``"broker-manager-<env>"`` — the daemon's heartbeat/kill-active job label."""
    return f"{_METRICS_JOB_PREFIX}-{_resolve_env(env)}"


def stream_metrics_job(env: str | None = None) -> str:
    """``"broker-manager-<env>-stream"`` — the streaming-liveness gauge domain."""
    return f"{metrics_job(env)}-{_STREAM_METRICS_JOB_SUFFIX}"


def price_stream_metrics_job(env: str | None = None) -> str:
    """``"live-price-stream-<env>"`` — the ``SaxoLivePriceStream`` gauge job label."""
    return f"{_PRICE_STREAM_JOB_PREFIX}-{_resolve_env(env)}"


# --- Legacy-layout guard (D4) -------------------------------------------------


def assert_no_legacy_flat_state() -> None:
    """Raise :class:`BrokerStateLayoutError` if the pre-migration flat layout
    is still present: any of :data:`LEGACY_FLAT_STATE_FILENAMES` existing
    directly under ``~/.alphalens/broker_orders/`` (not under a per-env
    subdirectory). A missing/empty ``broker_orders/`` directory, a clean
    per-env nested layout, or a flat ``KILL`` file (the current GLOBAL kill,
    D3) all pass without raising.
    """
    root = _alphalens_home() / _BROKER_ORDERS_DIRNAME
    legacy_present = [name for name in LEGACY_FLAT_STATE_FILENAMES if (root / name).is_file()]
    if legacy_present:
        raise BrokerStateLayoutError(
            f"legacy flat broker state found under {root}: {legacy_present}. "
            f"Migrate into the per-environment layout before starting, e.g. "
            f"move these files into {root / ENV_SIM}/ "
            f"(see docs/research/broker_env_state_separation_design_2026_08_10.md §6)."
        )
