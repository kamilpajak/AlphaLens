"""The modules production actually enters through.

Reachability is only as good as its root set, so the roots are read off the
deployment rather than recalled: every ``ExecStart`` in ``deploy/systemd/*.service``,
the ``alphalens`` console script, the pipeline image's entrypoint script, and
Django's WSGI and URL configuration.

Three shapes carry a module:

* ``.venv/bin/python apps/<app>/.../thing.py`` — a script, resolved by path.
* ``.venv/bin/alphalens <group> <command>`` — the CLI. ``alphalens_cli.main``
  imports every command module at top level, so one root covers all of it
  however it is invoked, including from inside the container.
* ``docker compose ... run --rm <service>`` — a Django management command,
  resolved through the compose file's service name.

Everything else is deployment plumbing (``docker rm``, ``env`` wrappers, shell
glue). Those shapes are NAMED in ``_KNOWN_NON_PYTHON``, never matched loosely,
so a new unit with an unfamiliar shape shows up in :func:`unresolved` instead
of silently contributing no root.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from scripts.arch.graph import module_name, repo_root

__all__ = [
    "CLI_ROOT",
    "EntryPoint",
    "ExecCommand",
    "all_entry_points",
    "exec_commands",
    "is_known_non_python",
    "repo_root",
    "resolve",
    "unit_files",
    "unresolved",
]

CLI_ROOT = "alphalens_cli.main"

_UNIT_GLOB = "deploy/systemd/*.service"
_EXEC_DIRECTIVES = ("ExecStart", "ExecStartPre", "ExecStartPost", "ExecStop", "ExecStopPost")

# Django roots. The app registry and URLconf load by dotted string, so no
# import edge reaches a view or a serializer from here.
_DJANGO_ROOTS: tuple[str, ...] = ("config.wsgi", "config.urls", "config.settings")

# Entry points that no unit file names because something else invokes them:
# the pipeline image's entrypoint is the `alphalens` CLI itself, and
# intent-replay is run by hand.
_STANDING_ROOTS: tuple[tuple[str, str], ...] = (
    (CLI_ROOT, "console-script:alphalens"),
    ("intent_replay.cli", "console-script:intent-replay"),
)

# Deployment plumbing that carries no first-party module. Matched as a prefix
# of the command, after the leading `-` (systemd's "failure is tolerated").
_KNOWN_NON_PYTHON: tuple[str, ...] = (
    "/usr/bin/docker rm",
    "/usr/bin/docker run",
    "/usr/bin/env UID=",
    "/usr/bin/true",
    "/bin/sh -c",
    "/bin/true",
    # Pure-shell metrics hook: writes a node_exporter textfile, calls no Python.
    # It is still followed as a wrapper above, which is what proves that.
    "%h/AlphaLens/deploy/systemd/bin/alphalens-emit-job-metrics",
)

# `docker compose ... run --rm <service>`: the service runs a Django management
# command. Its name is READ from the compose file rather than mapped here — a
# hardcoded map drifted once already (the service is `rebuild-ladder-outcomes`
# while the command is `rebuild_ladder_outcomes_cache`).
_COMPOSE_SERVICE = re.compile(r"docker compose\b.*?\brun\b.*?--rm\s+(?P<service>[\w-]+)")
_COMPOSE_FILE_FLAG = re.compile(r"-f\s+(?P<path>\S*docker-compose\.ya?ml)")
_DEFAULT_COMPOSE = "deploy/docker/django-prod/docker-compose.yaml"

_SCRIPT_PATH = re.compile(r"(?P<path>apps/[\w.-]+(?:/[\w.-]+)*\.py)")
# The closing quote is optional because a shell wrapper writes the binary as
# `"$HOME/AlphaLens/.venv/bin/alphalens" literature scan`.
_ALPHALENS_CLI = re.compile(r"""bin/alphalens["']?\s+(?P<group>[\w-]+)""")

# A unit may run a shell wrapper from the repo instead of a binary directly.
# The wrapper is followed: its body is scanned the same way, so a wrapper that
# calls the CLI contributes the CLI root and one that is pure shell (the
# metrics hook) contributes nothing, without either being special-cased.
_WRAPPER_PATH = re.compile(r"(?P<path>deploy/systemd/bin/[\w.-]+)")


@dataclass(frozen=True)
class ExecCommand:
    """One ``Exec*`` directive from one unit file, continuations joined."""

    unit: str
    directive: str
    command: str


@dataclass(frozen=True)
class EntryPoint:
    module: str
    origin: str


def unit_files(root: Path) -> list[Path]:
    return sorted(root.glob(_UNIT_GLOB))


def exec_commands(root: Path) -> list[ExecCommand]:
    """Every ``Exec*`` directive in every unit file, with continuations joined."""
    commands: list[ExecCommand] = []
    for unit in unit_files(root):
        for directive, command in _directives(unit.read_text(encoding="utf-8")):
            commands.append(ExecCommand(unit.name, directive, command))
    return commands


def _directives(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    pending: tuple[str, list[str]] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if pending is not None:
            continued = line.endswith("\\")
            pending[1].append(line.removesuffix("\\").strip())
            if not continued:
                out.append((pending[0], " ".join(part for part in pending[1] if part)))
                pending = None
            continue
        for directive in _EXEC_DIRECTIVES:
            if not line.startswith(f"{directive}="):
                continue
            value = line[len(directive) + 1 :].strip()
            if value.endswith("\\"):
                pending = (directive, [value.removesuffix("\\").strip()])
            else:
                out.append((directive, value))
            break
    if pending is not None:  # a unit whose last line dangles a continuation
        out.append((pending[0], " ".join(part for part in pending[1] if part)))
    return out


def is_known_non_python(command: str) -> bool:
    stripped = command.lstrip("-").strip()
    return any(stripped.startswith(prefix) for prefix in _KNOWN_NON_PYTHON)


def resolve(command: ExecCommand, root: Path) -> list[EntryPoint]:
    """Every first-party module ``command`` enters, which may be none."""
    origin = f"systemd:{command.unit}"
    found = [EntryPoint(module, origin) for module in _modules_in(command.command, root)]

    service = _COMPOSE_SERVICE.search(command.command)
    if service is not None:
        module = _compose_service_module(command.command, service.group("service"), root)
        if module is not None:
            found.append(EntryPoint(module, origin))

    return found


def _modules_in(text: str, root: Path, *, follow_wrappers: bool = True) -> list[str]:
    """Modules a command line enters directly, following repo shell wrappers."""
    modules: list[str] = []
    for match in _SCRIPT_PATH.finditer(text):
        path = root / match.group("path")
        if path.is_file():
            modules.append(module_name(path))
    if _ALPHALENS_CLI.search(text):
        modules.append(CLI_ROOT)
    if follow_wrappers:
        for match in _WRAPPER_PATH.finditer(text):
            wrapper = root / match.group("path")
            if wrapper.is_file():
                body = _without_comments(wrapper.read_text(encoding="utf-8"))
                modules.extend(_modules_in(body, root, follow_wrappers=False))
    return modules


def _without_comments(text: str) -> str:
    """Shell comments only. A wrapper's own docstring names the test that pins
    it, and reading that as an invocation made the test suite an entry point."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _compose_service_module(command: str, service: str, root: Path) -> str | None:
    """The management-command module a compose service runs, read from the file."""
    flag = _COMPOSE_FILE_FLAG.search(command)
    relpath = flag.group("path") if flag else _DEFAULT_COMPOSE
    compose = root / relpath[relpath.index("deploy/") :] if "deploy/" in relpath else root / relpath
    if not compose.is_file():
        return None
    name = _manage_command_name(compose.read_text(encoding="utf-8"), service)
    if name is None:
        return None
    matches = sorted((root / "apps/alphalens-django").glob(f"*/management/commands/{name}.py"))
    return module_name(matches[0]) if matches else None


def _manage_command_name(compose_text: str, service: str) -> str | None:
    """The token after ``manage.py`` inside ``service``'s block."""
    lines = compose_text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == f"{service}:")
    except StopIteration:
        return None
    indent = len(lines[start]) - len(lines[start].lstrip())
    seen_manage = False
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if stripped and not line.startswith(" " * (indent + 1)):
            break  # left the service block
        if not stripped.startswith("- "):
            continue
        value = stripped.removeprefix("- ").strip()
        if seen_manage:
            return value
        seen_manage = value.endswith("manage.py")
    return None


def unresolved(root: Path) -> list[ExecCommand]:
    """Directives that neither resolve to a module nor match a named shape.

    This is the completeness gate. An entry left here means the root set is
    incomplete, and an incomplete root set reports live code as dead.
    """
    out: list[ExecCommand] = []
    for command in exec_commands(root):
        if resolve(command, root):
            continue
        if is_known_non_python(command.command):
            continue
        out.append(command)
    return out


def all_entry_points(root: Path) -> list[EntryPoint]:
    """The full root set: the deployment's, plus the standing ones."""
    found: dict[tuple[str, str], EntryPoint] = {}

    def add(entry: EntryPoint) -> None:
        found.setdefault((entry.module, entry.origin), entry)

    for command in exec_commands(root):
        for entry in resolve(command, root):
            add(entry)
    for module, origin in _STANDING_ROOTS:
        add(EntryPoint(module, origin))
    for module in _DJANGO_ROOTS:
        add(EntryPoint(module, "django:settings-and-urlconf"))
    for module in _django_management_commands(root):
        add(EntryPoint(module, "django:management-command"))
    for module in _dunder_main_modules(root):
        add(EntryPoint(module, "python -m"))
    return sorted(found.values(), key=lambda e: (e.module, e.origin))


def _dunder_main_modules(root: Path) -> Iterable[str]:
    """``__main__.py`` is an entry point by definition — ``python -m pkg`` runs
    it, and nothing imports it. Counting it as unreferenced is a false
    positive, which is what it was before this existed."""
    from scripts.arch.graph import PRODUCTION_ROOTS

    return sorted(
        module_name(path)
        for relroot in PRODUCTION_ROOTS
        for path in (root / relroot).rglob("__main__.py")
    )


def _django_management_commands(root: Path) -> Iterable[str]:
    """Django loads these by dotted string, so no import edge reaches them."""
    base = root / "apps/alphalens-django"
    if not base.is_dir():
        return []
    return sorted(
        module_name(path)
        for path in base.glob("*/management/commands/*.py")
        if path.name != "__init__.py"
    )


def entry_modules(root: Path) -> list[str]:
    """Just the module names, de-duplicated, for feeding a reachability walk."""
    return sorted({entry.module for entry in all_entry_points(root)})
