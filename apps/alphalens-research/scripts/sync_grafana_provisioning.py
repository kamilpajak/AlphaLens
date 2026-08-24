"""Converge the live Grafana provisioning tree to the ``origin/main`` blobs.

The SoT is three files in this repo — the datasource yml, the dashboard
provider yml, and the cron-health dashboard JSON. The live copies sit in
``~/monitoring/grafana/provisioning`` on the VPS, bind-mounted into the grafana
container. Nothing converged the two until now, and the drift bit on
2026-08-24: the live datasource yml never declared ``uid: prometheus`` while
every dashboard target references that uid, and the live dashboard copy was two
months stale. Every panel read "No data" while Prometheus was healthy.

The contract that shapes every decision below
---------------------------------------------
**Live converges within one cadence, and a broken sync pages.** Concretely:

* the content source is the fetched ``origin/main`` BLOB (``git show``), never
  the working tree — a stale checkout must not be able to deploy itself;
* the managed set is an explicit WHITELIST (:data:`MANAGED_FILES`), never a
  directory mirror: ``node-exporter-dashboard.json`` shares the live dashboards
  directory and belongs to another tenant, so a mirror-with-delete would erase
  a live dashboard;
* identical content is a full short-circuit: no backup, no temp file, no write
  (outcome ``in_sync``);
* on difference the WHOLE desired set is validated in-process BEFORE anything
  is replaced — YAML/JSON parse, the required ``uid: prometheus``, the provider
  path, and the cross-file check that every datasource uid a dashboard
  references is actually declared. That last one is the 2026-08-24 root cause
  caught at the gate rather than in the UI. A refusal leaves the live tree
  byte-identical (outcome ``check_failed``);
* every run rewrites the outcome one-hot metric family with ALL five labels
  (``in_sync``/``synced``/``fetch_failed``/``check_failed``/``reload_failed``),
  zeros included — an absent series must mean "broken emitter", never a state;
* exit 0 only on ``in_sync``/``synced``; any failure outcome exits 1 so the
  systemd unit fails and the journal carries the reason.

``reload_failed`` today means "at least one file was already installed when the
run failed, so the live tree is half-applied and Grafana was never confirmed to
have picked the new content up". The container restart that a datasource change
needs (dashboards hot-reload through the provisioning watcher; datasources do
not) and its verification against ``grafana.db`` are a follow-up increment —
:func:`_sync` already reports WHICH files it replaced so that decision has its
input. The Grafana admin API is deliberately not used at all: the admin
password in the compose file is a dead placeholder and the API answers 401.

Backups are ``<filename>.bak-autosync-<UTC stamp>`` beside each managed file
and pruning deletes ONLY files with that exact per-file prefix beyond the
newest 10. Backup and temp names never end in ``.json``/``.yml``/``.yaml``:
Grafana's file provider would import such a file as a duplicate dashboard or a
stale datasource.

git, the clock and the metric emitter are injected ports; the live-tree
filesystem operations are a small concrete class tested against a real
temporary directory.

Usage::

    .venv/bin/python apps/alphalens-research/scripts/sync_grafana_provisioning.py
    .venv/bin/python apps/alphalens-research/scripts/sync_grafana_provisioning.py --dry-run

Needs the ``jacoren`` user's write access to the live provisioning tree and the
checkout at ``~/AlphaLens``; no secrets are read (the repo is public, the fetch
is anonymous).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

import yaml

logger = logging.getLogger(__name__)

JOB_NAME = "grafana-provisioning-sync"

DEFAULT_REPO_DIR = Path.home() / "AlphaLens"
DEFAULT_LIVE_DIR = Path.home() / "monitoring" / "grafana" / "provisioning"


class FileKind(Enum):
    """What a managed file IS — the validation gate branches on this, and the
    restart decision will (a datasource change costs a container restart)."""

    DATASOURCE = "datasource"
    DASHBOARD_PROVIDER = "dashboard_provider"
    DASHBOARD = "dashboard"


@dataclass(frozen=True)
class ManagedFile:
    """One repo blob and where it lands in the live provisioning tree.

    The repo layout is NOT the live layout (the dashboard JSON keeps its
    historical repo home next to the monitoring rules), so the mapping is
    explicit rather than derived from a directory walk.
    """

    repo_path: str
    live_relpath: str
    kind: FileKind

    @property
    def live_name(self) -> str:
        return self.live_relpath.rsplit("/", 1)[-1]


MANAGED_FILES: tuple[ManagedFile, ...] = (
    ManagedFile(
        repo_path="deploy/monitoring/grafana/provisioning/datasources/prometheus.yml",
        live_relpath="datasources/prometheus.yml",
        kind=FileKind.DATASOURCE,
    ),
    ManagedFile(
        repo_path="deploy/monitoring/grafana/provisioning/dashboards/dashboards.yml",
        live_relpath="dashboards/dashboards.yml",
        kind=FileKind.DASHBOARD_PROVIDER,
    ),
    ManagedFile(
        repo_path="deploy/monitoring/grafana/dashboards/alphalens-cron-health.json",
        live_relpath="dashboards/alphalens-cron-health.json",
        kind=FileKind.DASHBOARD,
    ),
)

# The uid every dashboard target in this repo addresses. Its absence from the
# live datasource yml was the whole 2026-08-24 incident.
REQUIRED_DATASOURCE_UID = "prometheus"
# The dashboards directory as the container sees it; the provider yml must
# point there or the file provider watches nothing.
PROVISIONED_DASHBOARD_DIR = "/etc/grafana/provisioning/dashboards"

BACKUP_INFIX = ".bak-autosync-"
BACKUP_KEEP = 10  # deliberately hardcoded: ~10 provisioning deploys of history;
# a knob would outlive its documentation (the no-config-drift doctrine)

GIT_TIMEOUT_S = 120

OUTCOME_METRIC = "alphalens_grafana_sync_outcome"


class Outcome(Enum):
    """Exactly the five run outcomes; the metric label set mirrors this enum."""

    IN_SYNC = "in_sync"
    SYNCED = "synced"
    FETCH_FAILED = "fetch_failed"
    CHECK_FAILED = "check_failed"
    RELOAD_FAILED = "reload_failed"


SUCCESS_OUTCOMES = frozenset({Outcome.IN_SYNC, Outcome.SYNCED})


class GitPort(Protocol):
    """Reading the SoT blobs. Both operations raise :class:`GitCommandError`."""

    def fetch(self) -> None: ...

    def show(self, repo_path: str) -> bytes: ...


EmitPort = Callable[[str, Mapping[str, float]], object]


# ----------------------------------------------------------------------------
# Pure helpers
# ----------------------------------------------------------------------------


def backup_prefix(live_name: str) -> str:
    """The exact prefix pruning anchors on, per managed file.

    Per-file rather than per-directory because the dashboards directory holds
    both the provider yml and the dashboard JSONs; a shared prefix would let a
    busy dashboard's backups evict the provider's.
    """
    return f"{live_name}{BACKUP_INFIX}"


def parse_datasource_uids(content: bytes) -> set[str]:
    """Every uid declared by a datasource provisioning file.

    Returns the empty set on anything unparseable — the caller
    (:func:`validate_desired`) turns that into a refusal, which is the loud arm.
    """
    doc = _safe_yaml(content)
    if not isinstance(doc, dict):
        return set()
    uids: set[str] = set()
    for entry in doc.get("datasources") or []:
        if isinstance(entry, dict) and isinstance(entry.get("uid"), str):
            uids.add(entry["uid"])
    return uids


def referenced_datasource_uids(doc: Any) -> set[str]:
    """Every datasource uid a dashboard document addresses.

    Walks the whole document because targets, panels, annotations and template
    variables all carry their own ``datasource`` reference. ``${VAR}`` forms are
    skipped: Grafana resolves those from the dashboard's own inputs at import
    time, so cross-checking them against our provisioning tree would refuse
    valid dashboards.
    """
    found: set[str] = set()
    _collect_datasource_uids(doc, found)
    return found


def _collect_datasource_uids(node: Any, found: set[str]) -> None:
    if isinstance(node, dict):
        reference = node.get("datasource")
        if isinstance(reference, dict):
            uid = reference.get("uid")
            if isinstance(uid, str) and not uid.startswith("$"):
                found.add(uid)
        for value in node.values():
            _collect_datasource_uids(value, found)
    elif isinstance(node, list):
        for item in node:
            _collect_datasource_uids(item, found)


def validate_desired(desired: Mapping[ManagedFile, bytes]) -> str | None:
    """The promtool-equivalent gate. Returns a reason, or None when the whole
    set may be installed.

    Validated as a SET, before any replace: one bad blob must never leave a
    half-applied provisioning tree behind.
    """
    declared: set[str] = set()
    for managed, content in desired.items():
        if managed.kind is not FileKind.DATASOURCE:
            continue
        problem = _validate_datasource_file(managed, content)
        if problem:
            return problem
        declared |= parse_datasource_uids(content)

    if REQUIRED_DATASOURCE_UID not in declared:
        return (
            f'no synced datasource declares uid "{REQUIRED_DATASOURCE_UID}"; '
            "every dashboard target in this repo addresses that uid"
        )

    for managed, content in desired.items():
        if managed.kind is FileKind.DASHBOARD_PROVIDER:
            problem = _validate_provider_file(managed, content)
        elif managed.kind is FileKind.DASHBOARD:
            problem = _validate_dashboard_file(managed, content, declared)
        else:
            problem = None
        if problem:
            return problem
    return None


def _validate_datasource_file(managed: ManagedFile, content: bytes) -> str | None:
    doc = _safe_yaml(content)
    if not isinstance(doc, dict):
        return f"{managed.live_relpath}: not a YAML mapping"
    entries = doc.get("datasources")
    if not isinstance(entries, list) or not entries:
        return f"{managed.live_relpath}: declares no datasources"
    for entry in entries:
        if not isinstance(entry, dict):
            return f"{managed.live_relpath}: a datasource entry is not a mapping"
        # A datasource without an explicit uid gets a random one on every
        # provisioning run, which is exactly how the 2026-08-24 drift started.
        if not entry.get("uid"):
            return f"{managed.live_relpath}: datasource {entry.get('name')!r} declares no uid"
        if not entry.get("url"):
            return f"{managed.live_relpath}: datasource {entry.get('name')!r} declares no url"
    return None


def _validate_provider_file(managed: ManagedFile, content: bytes) -> str | None:
    doc = _safe_yaml(content)
    if not isinstance(doc, dict):
        return f"{managed.live_relpath}: not a YAML mapping"
    providers = doc.get("providers")
    if not isinstance(providers, list) or not providers:
        return f"{managed.live_relpath}: declares no providers"
    for provider in providers:
        if not isinstance(provider, dict) or provider.get("type") != "file":
            return f"{managed.live_relpath}: every provider must be of type 'file'"
        options = provider.get("options")
        path = options.get("path") if isinstance(options, dict) else None
        if path != PROVISIONED_DASHBOARD_DIR:
            return (
                f"{managed.live_relpath}: provider path {path!r} is not the "
                f"provisioned dashboards directory {PROVISIONED_DASHBOARD_DIR!r}"
            )
    return None


def _validate_dashboard_file(
    managed: ManagedFile, content: bytes, declared: set[str]
) -> str | None:
    try:
        doc = json.loads(content.decode("utf-8"))
    except Exception as exc:
        return f"{managed.live_relpath}: not valid JSON ({exc})"
    if not isinstance(doc, dict):
        return f"{managed.live_relpath}: not a JSON object"
    if not doc.get("uid") or not doc.get("title"):
        return f"{managed.live_relpath}: dashboard needs both a uid and a title"
    dangling = sorted(referenced_datasource_uids(doc) - declared)
    if dangling:
        return (
            f"{managed.live_relpath}: references datasource uid(s) {dangling} "
            "that no synced datasource declares"
        )
    return None


def _safe_yaml(content: bytes) -> Any:
    try:
        return yaml.safe_load(content.decode("utf-8"))
    except Exception:
        logger.exception("could not parse a provisioning YAML blob")
        return None


def build_metrics(outcome: Outcome) -> dict[str, float]:
    """The per-run textfile gauges: the outcome family and NOTHING else.

    ALL five outcome labels every run, zeros included — a series that
    disappears is indistinguishable from a stopped exporter, and the
    sustained-failure alert needs a clean run's 0 to clear. Job-level
    staleness deliberately comes from the unit's ``ExecStopPost``
    ``alphalens-emit-job-metrics`` hook (success-only stamp), so a second
    script-side timestamp would be an unconsumed duplicate.
    """
    return {
        f'{OUTCOME_METRIC}{{outcome="{candidate.value}"}}': (1 if candidate is outcome else 0)
        for candidate in Outcome
    }


def utc_stamp(now_ts: float) -> str:
    """Sortable UTC stamp for backup names (prune keeps the newest N by name)."""
    return dt.datetime.fromtimestamp(now_ts, tz=dt.UTC).strftime("%Y%m%dT%H%M%SZ")


# ----------------------------------------------------------------------------
# Live-tree filesystem operations (concrete; tested on a real tmpdir)
# ----------------------------------------------------------------------------


class LiveTree:
    """The ONLY writes this job may perform: the managed files, their autosync
    backups, and short-lived temp files. The live directories are SHARED —
    ``node-exporter-dashboard.json`` is another tenant's provisioned dashboard
    and must never be touched."""

    def __init__(self, path: Path):
        self.path = Path(path)
        # Phase marker for run()'s catch-all: an unexpected error BEFORE the
        # first replace maps to check_failed (live untouched), after it to
        # reload_failed (the tree is partly installed).
        self.replaced: list[str] = []

    def live_path(self, relpath: str) -> Path:
        return self.path / relpath

    def read_live(self, relpath: str) -> bytes | None:
        try:
            return self.live_path(relpath).read_bytes()
        except (FileNotFoundError, NotADirectoryError):
            return None

    def write_temp(self, relpath: str, content: bytes) -> str:
        """Write the candidate content next to its target; return its name.

        Same directory as the target so the eventual ``os.replace`` is a single
        ``rename(2)``. chmod 644 because grafana reads the bind-mounted files as
        its own in-container user; a 0600 file would be silently skipped. The
        name is a dotfile ending in ``.tmp`` so Grafana's file provider (which
        globs ``*.json`` / ``*.yml``) never picks it up.
        """
        directory = self.live_path(relpath).parent
        directory.mkdir(parents=True, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(
            dir=directory, prefix=f".{Path(relpath).name}.sync-", suffix=".tmp"
        )
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.chmod(temp_path, 0o644)
        return Path(temp_path).name

    def remove_temp(self, relpath: str, name: str) -> None:
        (self.live_path(relpath).parent / name).unlink(missing_ok=True)

    def backup_live(self, relpath: str, stamp: str) -> Path | None:
        """Copy the current file aside; no-op when it does not exist yet."""
        source = self.live_path(relpath)
        if not source.exists():
            return None
        target = source.parent / f"{backup_prefix(source.name)}{stamp}"
        shutil.copy2(source, target)
        return target

    def prune_autosync_backups(self, relpath: str, keep: int = BACKUP_KEEP) -> None:
        """Delete this file's autosync backups beyond the newest ``keep``.

        Anchored on the EXACT per-file autosync prefix: the directories hold a
        foreign tenant's dashboard, the other managed files' backups, and
        operator backups in other naming styles — none of those are ours to
        delete. The UTC stamp is lexically sortable, so name order is age order.
        """
        target = self.live_path(relpath)
        directory = target.parent
        if not directory.is_dir():
            return
        prefix = backup_prefix(target.name)
        backups = sorted(
            (entry for entry in directory.iterdir() if entry.name.startswith(prefix)),
            key=lambda entry: entry.name,
            reverse=True,
        )
        for stale in backups[keep:]:
            stale.unlink()

    def replace_live_with_temp(self, relpath: str, name: str) -> None:
        """Atomic install: ``rename(2)``, so Grafana's provisioning watcher
        sees either the old complete file or the new complete file."""
        target = self.live_path(relpath)
        os.replace(target.parent / name, target)
        self.replaced.append(relpath)


# ----------------------------------------------------------------------------
# Run loop
# ----------------------------------------------------------------------------


def run(
    *,
    git: GitPort,
    live: LiveTree,
    emit: EmitPort,
    now_fn: Callable[[], float] = time.time,
    dry_run: bool = False,
) -> int:
    """One pass. Returns the process exit code (0 = live matches SoT now)."""
    try:
        git.fetch()
        desired = {managed: git.show(managed.repo_path) for managed in MANAGED_FILES}
    except Exception:  # GitCommandError, a hung-subprocess timeout, anything
        logger.exception("could not read the origin/main provisioning blobs")
        if not dry_run:
            _emit(emit, Outcome.FETCH_FAILED)
        return 1

    differing = [
        managed
        for managed, content in desired.items()
        if live.read_live(managed.live_relpath) != content
    ]
    if not differing:
        if dry_run:
            print("in_sync: the live provisioning tree matches the origin/main blobs")
            return 0
        _emit(emit, Outcome.IN_SYNC)
        return 0

    if dry_run:
        names = ", ".join(managed.live_relpath for managed in differing)
        print(f"would sync: {len(differing)} file(s) differ from the blobs ({names})")
        return 0

    try:
        outcome, replaced = _sync(desired, differing=differing, live=live, now_fn=now_fn)
        if replaced:
            logger.info("installed %s", ", ".join(m.live_relpath for m in replaced))
    except Exception:
        # An unexpected error (disk-full OSError, a port-contract violation)
        # must still emit the one-hot family — a run with no metrics is
        # indistinguishable from a stopped exporter. Phase mapping: nothing
        # installed -> check_failed; partly installed -> reload_failed.
        logger.exception("unexpected error in the sync write path")
        outcome = Outcome.RELOAD_FAILED if live.replaced else Outcome.CHECK_FAILED
    _emit(emit, outcome)
    return 0 if outcome in SUCCESS_OUTCOMES else 1


def _sync(
    desired: Mapping[ManagedFile, bytes],
    *,
    differing: list[ManagedFile],
    live: LiveTree,
    now_fn: Callable[[], float],
) -> tuple[Outcome, tuple[ManagedFile, ...]]:
    """The write path, in the owner-settled order: validate the whole desired
    set, then per differing file backup + prune, temp file, atomic replace.

    Returns the outcome together with the files actually replaced — the input
    the restart decision needs, since only a datasource change costs a Grafana
    container restart.
    """
    problem = validate_desired(desired)
    if problem:
        # Nothing has been touched yet, by construction: the gate runs before
        # the first backup.
        logger.error("refusing the origin/main provisioning set: %s", problem)
        return Outcome.CHECK_FAILED, ()

    stamp = utc_stamp(now_fn())
    for managed in differing:
        relpath = managed.live_relpath
        live.backup_live(relpath, stamp)
        live.prune_autosync_backups(relpath, BACKUP_KEEP)

        temp_name = live.write_temp(relpath, desired[managed])
        installed = False
        try:
            live.replace_live_with_temp(relpath, temp_name)
            installed = True
        finally:
            # The replace consumes the temp on the happy path; every other exit
            # must not leave hidden .tmp files accumulating in a shared dir.
            if not installed:
                live.remove_temp(relpath, temp_name)
    return Outcome.SYNCED, tuple(differing)


def _emit(emit: EmitPort, outcome: Outcome) -> None:
    """Swallow-all: the sync already happened (or loudly failed via the exit
    code); a broken textfile dir is observability debt, not a second failure."""
    try:
        emit(JOB_NAME, build_metrics(outcome))
    except Exception:
        logger.exception("outcome metric emit failed; continuing")


# ----------------------------------------------------------------------------
# Default adapters (the only code that talks to the outside world)
# ----------------------------------------------------------------------------


class GitCommandError(RuntimeError):
    """A git invocation failed, carrying git's own explanation."""


class GitCli:
    """git via argv lists (never a shell string), always ``-C <repo>``.

    Reads the ``origin/main`` BLOB (``git show origin/main:<path>``), never the
    working tree — the fetch is read-only for the checkout, and a stale or
    dirty working tree cannot leak into the deployed content.
    """

    def __init__(
        self,
        repo_dir: str | Path,
        *,
        timeout: float = GIT_TIMEOUT_S,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ):
        self.repo_dir = Path(repo_dir)
        self._timeout = timeout
        self._runner = runner

    def fetch(self) -> None:
        self._git("fetch", "origin", "main")

    def show(self, repo_path: str) -> bytes:
        return self._git("show", f"origin/main:{repo_path}")

    def _git(self, *args: str) -> bytes:
        argv = ["git", "-C", str(self.repo_dir), *args]
        try:
            proc = self._runner(argv, capture_output=True, timeout=self._timeout)
        except subprocess.TimeoutExpired as exc:
            # A hung git (dead network, wedged lock) must land in the
            # fetch_failed arm WITH metrics, not escape past the emit.
            raise GitCommandError(
                f"`{' '.join(argv)}` timed out after {self._timeout:.0f}s"
            ) from exc
        if proc.returncode != 0:
            stderr = proc.stderr
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            # NOT check=True: CalledProcessError's message is the exit code
            # alone; git's own explanation is the diagnostic value in journald.
            raise GitCommandError(
                f"`{' '.join(argv)}` exited {proc.returncode}: {(stderr or '').strip()}"
            )
        return proc.stdout


def default_emit(job: str, metrics: Mapping[str, float]) -> object:
    """The canonical textfile emitter (atomic write into the scraped dir).

    Lazy import so ``--dry-run`` and ``--help`` work without the pipeline
    package on path.
    """
    from alphalens_pipeline.observability.textfile import emit_domain_metrics

    return emit_domain_metrics(job=job, metrics=metrics)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--repo-dir",
        default=str(DEFAULT_REPO_DIR),
        help=f"checkout whose origin/main carries the SoT (default {DEFAULT_REPO_DIR})",
    )
    parser.add_argument(
        "--live-dir",
        default=str(DEFAULT_LIVE_DIR),
        help=f"live Grafana provisioning tree (default {DEFAULT_LIVE_DIR})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report in_sync / would-sync; write nothing (not even metrics)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    return run(
        git=GitCli(args.repo_dir),
        live=LiveTree(Path(args.live_dir)),
        emit=default_emit,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    raise SystemExit(main())
