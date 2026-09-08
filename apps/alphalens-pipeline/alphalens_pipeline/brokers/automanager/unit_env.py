"""Compose a one-off broker environment from the INSTALLED systemd unit (#1377).

A LIVE read command (`alphalens broker account|positions|orders|reconcile|cancel
--env live`) needs the daemon's whole boot surface before
``create_saxo_broker_live_from_env`` will construct anything: the nine risk
rails plus the account-bound grant from the unit's ``Environment=``, and the
``SAXO_LIVE_APP_KEY`` / ``_APP_SECRET`` / ``_AUTH_REDIRECT_URL`` credentials
that ``LiveAuthConfig.from_env`` reads from the shared ``EnvironmentFile=``.
Before this module the operator had to assemble both by hand
(``set -a; . /etc/alphalens/env; set +a`` plus
``env $(systemctl --user show … -p Environment --value) …``), which is exactly
what slowed the 2026-09-08 outage diagnosis down.

**Precedence mirrors systemd, it does not invent one.** ``Environment=`` is
applied FIRST and the ``EnvironmentFile=`` SECOND, so the file wins — verified
on the host 2026-08-11 and recorded in the unit file, because that ordering is
what makes the rail ban on ``/etc/alphalens/env`` (#1209) load-bearing rather
than cosmetic. A composition is therefore what the daemon gets at its NEXT
start; when the file defines a banned key the caller is warned by NAME (never
by value) instead of the divergence being smoothed over.

**What this describes.** ``systemctl show`` reports the LOADED unit
configuration with every drop-in merged (measured: the production payload
carries ``ALPHALENS_BROKER_ALLOW_ORDERS=1`` from ``10-allow-orders.conf``,
overriding the base unit's ``0``). It does NOT describe the environment of the
RUNNING process: after a ``daemon-reload`` without a ``restart`` the two
differ, and only the hourly drift check compares them. ``NeedDaemonReload`` is
surfaced as the cheap half of that signal (unit files edited, never reloaded).

**Refusal, not best effort.** Every failure raises :class:`UnitEnvError` naming
the manual recipe: no ``systemctl`` on the host, a unit whose ``LoadState`` is
not ``loaded`` (measured: a non-existent unit answers with an EMPTY
``Environment`` payload and exit status 0, so emptiness cannot be the test), an
empty payload on a loaded unit, or a mandatory environment file this process
cannot read. A silent fallback onto the caller's shell would report one
instance's numbers under the other one's name.

Values are never logged or echoed by this module; callers print key NAMES and
counts only (the drift check reports variable names by the same rule — the
secret is the value).
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from alphalens_pipeline.brokers.automanager.state_paths import ENV_LIVE, ENV_SIM

SIM_UNIT = "alphalens-broker-manager.service"
LIVE_UNIT = "alphalens-broker-manager-live.service"

_UNITS = {ENV_SIM: SIM_UNIT, ENV_LIVE: LIVE_UNIT}

# The names that must never live in the shared environment file (#1209): the
# rails, the placement arm and the account-bound grant. Kept in sync with
# ``check_systemd_drift.BANNED_ENV_NAME_PREFIX`` + ``LIVE_GRANT_VARS`` by the
# same reasoning, not by import — that script is a research-side tool and the
# pipeline must not depend on it (ADR 0011).
_BANNED_FILE_PREFIX = "ALPHALENS_BROKER_"
_BANNED_FILE_NAMES = frozenset({"ALPHALENS_SAXO_LIVE_STANDING", "SAXO_LIVE_ACCOUNT_KEY"})

_MANUAL_RECIPE = (
    "compose it by hand instead: "
    "`set -a; . /etc/alphalens/env; set +a` then "
    "`env $(systemctl --user show {unit} -p Environment --value) "
    ".venv/bin/alphalens broker …`"
)


class UnitEnvError(RuntimeError):
    """The installed unit's environment could not be read honestly."""


@dataclass(frozen=True)
class ComposedEnv:
    """One composed environment plus what it was composed FROM.

    ``values`` is the environment; ``unit`` / ``env_file`` / ``dropins`` are
    what the caller echoes so the operator can see the sources (names and
    counts only). ``warnings`` are ready-to-print lines about divergences the
    composition preserved rather than hid.
    """

    values: dict[str, str]
    unit: str
    env_file: Path | None
    dropins: int
    needs_daemon_reload: bool
    warnings: list[str]


def unit_for_env(env: str) -> str:
    """The systemd unit that runs the named broker instance."""
    try:
        return _UNITS[env]
    except KeyError:
        raise UnitEnvError(
            f"no broker unit for environment {env!r} (expected {ENV_SIM!r} or {ENV_LIVE!r})"
        ) from None


def parse_environment_payload(payload: str) -> dict[str, str]:
    """The assignments in a ``systemctl show -p Environment --value`` payload.

    systemd renders one space-separated list and QUOTES any value containing a
    space, so ``shlex.split`` is the honest reader — a plain ``str.split``
    would shred such a value into a phantom variable. Only the FIRST ``=``
    splits (a redirect URL carries more). A token without ``=`` is refused:
    misreading the loaded environment is the one failure this module exists to
    prevent.
    """
    values: dict[str, str] = {}
    for token in shlex.split(payload.strip()):
        key, separator, value = token.partition("=")
        if not separator or not key:
            raise UnitEnvError(
                f"systemctl rendered {token!r}, which is not a KEY=VALUE assignment — "
                "refusing to guess what the unit sets"
            )
        values[key] = value
    return values


def parse_environment_file(text: str) -> dict[str, str]:
    """The assignments in an ``EnvironmentFile=`` body.

    The subset systemd documents and this repo's file actually uses: one
    assignment per line, ``#`` comment lines, blank lines, an optional
    ``export`` prefix, and optionally quoted values (the production file has
    one such line). A line that is not an assignment is SKIPPED, matching
    systemd, which logs and carries on rather than refusing to start.
    """
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, separator, value = line.partition("=")
        if not separator or not key.strip():
            continue
        values[key.strip()] = _unquote(value.strip())
    return values


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_environment_files_property(value: str) -> list[tuple[Path, bool]]:
    """``(path, ignore_errors)`` per entry of ``systemctl show -p EnvironmentFiles``.

    systemd renders each entry as ``/path (ignore_errors=<yes|no>)`` (measured
    on the host). ``ignore_errors`` decides whether an unreadable file is fatal,
    exactly as it does for the daemon.
    """
    entries: list[tuple[Path, bool]] = []
    for line in value.strip().splitlines():
        entry = line.strip()
        if not entry:
            continue
        path_text, _, flag = entry.partition(" (ignore_errors=")
        entries.append((Path(path_text.strip()), flag.rstrip(")").strip() == "yes"))
    return entries


def _systemctl_show(unit: str, prop: str) -> str:
    """One ``systemctl --user show`` property value (the process seam).

    A thin named function so tests patch ONE attribute (the pattern
    ``entry_trails._entry_trail_journal_path`` uses). ``check=False``: systemctl
    answers a non-existent unit with exit status 0 and empty values, so the
    caller decides via ``LoadState`` rather than via a return code.
    """
    completed = subprocess.run(
        ["systemctl", "--user", "show", unit, "-p", prop, "--value"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return completed.stdout


def _read_text(path: Path) -> str:
    """Read one environment file (the filesystem seam)."""
    return path.read_text(encoding="utf-8")


def compose_live_environment(
    *,
    env: str = ENV_LIVE,
    run=None,
    read_text=None,
) -> ComposedEnv:
    """The environment the named instance's unit would give its daemon.

    ``run(unit, prop)`` and ``read_text(path)`` are the injected process and
    filesystem seams (defaults: :func:`_systemctl_show`, :func:`_read_text`),
    so this composition is testable without a systemd host.
    """
    run = run if run is not None else _systemctl_show
    read_text = read_text if read_text is not None else _read_text
    unit = unit_for_env(env)
    recipe = _MANUAL_RECIPE.format(unit=unit)

    try:
        load_state = run(unit, "LoadState").strip()
        payload = run(unit, "Environment")
        env_files_property = run(unit, "EnvironmentFiles")
        dropin_property = run(unit, "DropInPaths")
        needs_reload = run(unit, "NeedDaemonReload").strip() == "yes"
    except (OSError, subprocess.SubprocessError) as exc:
        raise UnitEnvError(f"cannot read {unit} through systemctl ({exc}) — {recipe}") from exc

    if load_state != "loaded":
        raise UnitEnvError(
            f"{unit} has LoadState={load_state!r}, not 'loaded' — the unit is not "
            f"installed on this host, so its rails cannot be read; {recipe}"
        )

    values = parse_environment_payload(payload)
    if not values:
        raise UnitEnvError(
            f"{unit} is loaded but defines no Environment= assignments — the LIVE "
            f"rails cannot come from it; {recipe}"
        )

    warnings: list[str] = []
    env_file: Path | None = None
    for path, ignore_errors in parse_environment_files_property(env_files_property):
        try:
            text = read_text(path)
        except OSError as exc:
            if ignore_errors:
                continue
            raise UnitEnvError(
                f"cannot read {path} ({exc}) — this is a CLI-side read failure, not a "
                "statement about the daemon, which read the file at its own start and "
                f"may still be running fine; {recipe}"
            ) from exc
        env_file = path
        file_values = parse_environment_file(text)
        # systemd applies the file AFTER Environment=, so the file wins. A
        # banned key here is preserved (it IS what the daemon boots with next)
        # and reported by NAME, never by value.
        for key in file_values:
            if key.startswith(_BANNED_FILE_PREFIX) or key in _BANNED_FILE_NAMES:
                warnings.append(
                    f"WARN {key}: {path} overrides the unit pin — a banned key in the "
                    "shared environment file (#1209); the composed value is the FILE's"
                )
        values.update(file_values)

    if needs_reload:
        warnings.append(
            f"WARN {unit}: unit files changed since the last daemon-reload — this "
            "composition may match no running process"
        )

    return ComposedEnv(
        values=values,
        unit=unit,
        env_file=env_file,
        dropins=len([line for line in dropin_property.split() if line.strip()]),
        needs_daemon_reload=needs_reload,
        warnings=warnings,
    )


__all__ = [
    "LIVE_UNIT",
    "SIM_UNIT",
    "ComposedEnv",
    "UnitEnvError",
    "compose_live_environment",
    "parse_environment_file",
    "parse_environment_files_property",
    "parse_environment_payload",
    "unit_for_env",
]
