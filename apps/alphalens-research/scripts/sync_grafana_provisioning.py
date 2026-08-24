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
* a container restart is spent ONLY where it buys something (see below);
* ``synced`` requires OBSERVING Grafana serve the new content, never just
  writing the file;
* every run rewrites the outcome one-hot metric family with ALL five labels
  (``in_sync``/``synced``/``fetch_failed``/``check_failed``/``reload_failed``),
  zeros included — an absent series must mean "broken emitter", never a state;
* exit 0 only on ``in_sync``/``synced``; any failure outcome exits 1 so the
  systemd unit fails and the journal carries the reason.

When a restart is spent
-----------------------
A restart is a real, if brief, outage of a SHARED Grafana (it also serves the
node-exporter dashboard), so :data:`RESTART_REQUIRING_KINDS` bounds it:

* dashboard JSON -> NO restart. MEASURED on the VPS 2026-08-24: the provider
  declares ``updateIntervalSeconds: 10`` and the cron-health dashboard's
  ``dashboard`` row was created 2026-05-30, between the 2026-05-19 and
  2026-08-01 container starts — the provisioning watcher imported it with no
  restart. Paying one for a dashboard edit would be pure downtime;
* datasource yml -> restart. Datasources are provisioned during startup only;
* dashboard PROVIDER yml -> restart. Reasoned rather than measured: Grafana
  reads the provider CONFIG in the startup provisioning pass, and the watcher
  it then starts follows the dashboards DIRECTORY, not the config that
  described it. This arm fails safe — an unnecessary restart, never a change
  that silently never applies.

How ``synced`` is verified (and why not the obvious way)
--------------------------------------------------------
The Grafana admin API is unusable: the compose file's admin password is a dead
placeholder and Grafana keeps its first-init credentials in the volume, so
``/api/datasources`` answers 401 (observed in the container log). Provisioning
also logs nothing on the happy path — the 2026-05-30 watcher import produced
zero log lines — so log scraping would verify nothing either. What remains is
read-only and sufficient:

* ``GET /api/health`` is UNAUTHENTICATED and reports ``database: ok``; polled
  after a restart as the liveness gate;
* ``grafana.db`` (streamed out with ``docker cp``, read with stdlib sqlite3 in
  read-only mode) carries ``data_source.uid`` and, for every provisioned
  dashboard, ``dashboard_provisioning.check_sum``. That checksum was MEASURED
  on 2026-08-24 to equal the md5 of the source file's bytes for both live
  dashboards, which makes it a CONTENT fingerprint, not a name-level one.

A restart that comes back without the declared uid, or a dashboard whose stored
checksum still fingerprints the old bytes, is ``reload_failed`` — the exact
2026-08-24 shape, this time caught by the instrument.

``reload_failed`` therefore covers three things, all meaning "the bytes are on
disk and Grafana was not confirmed to be serving them": a refused restart, a
container that never came back healthy, and a verification that never
converged. It also covers an unexpected error raised after the first replace,
where the live tree is left half-applied.

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
import contextlib
import datetime as dt
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
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

# Which replacements cost a container restart. A dashboard JSON is absent by
# design — the provisioning watcher picks it up (measured; see the module
# docstring), and an outage per dashboard edit would buy nothing.
RESTART_REQUIRING_KINDS = frozenset({FileKind.DATASOURCE, FileKind.DASHBOARD_PROVIDER})

BACKUP_INFIX = ".bak-autosync-"
BACKUP_KEEP = 10  # deliberately hardcoded: ~10 provisioning deploys of history;
# a knob would outlive its documentation (the no-config-drift doctrine)

CONTAINER_NAME = "grafana"
# Grafana's sqlite state lives on the named volume; `docker cp` streams it out
# in ~0.06s for ~1.2MB and needs neither root nor the network.
CONTAINER_DB_PATH = "/var/lib/grafana/grafana.db"
# Explicit non-secret columns, never `SELECT *`: data_source also holds
# password, basic_auth_password and secure_json_data.
DATASOURCE_UID_SQL = "SELECT uid FROM data_source"
DASHBOARD_PROVISIONING_SQL = "SELECT external_id, check_sum FROM dashboard_provisioning"
# The one Grafana endpoint that answers without credentials.
HEALTH_URL = "http://localhost:3000/api/health"

GIT_TIMEOUT_S = 120
DOCKER_TIMEOUT_S = 60
API_TIMEOUT_S = 10

# Convergence is asynchronous on both arms: a restart takes ~5-15s to serve
# /api/health again, and the dashboards watcher polls on its own
# updateIntervalSeconds (10 on the VPS). The budget must outlast the slower of
# the two, so 10 attempts spaced 3s covers ~30s of settling.
VERIFY_ATTEMPTS = 10
VERIFY_DELAY_S = 3.0

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


class GrafanaPort(Protocol):
    """The container-side operations. None of these raise — a ``False``/``None``
    answer is already the loud arm (``reload_failed``); an exception past the
    replace could skip the metric emit entirely."""

    def restart(self) -> bool: ...

    def health_ok(self) -> bool: ...

    def datasource_uids(self) -> set[str] | None: ...

    def provisioned_checksums(self) -> dict[str, str] | None: ...


EmitPort = Callable[[str, Mapping[str, float]], object]


# ----------------------------------------------------------------------------
# Pure helpers
# ----------------------------------------------------------------------------


def dashboard_checksums(desired: Mapping[ManagedFile, bytes]) -> dict[str, str]:
    """The dashboard convergence fingerprint: container path -> md5 of bytes.

    Grafana stores exactly this pair in ``dashboard_provisioning`` —
    ``external_id`` is the path it read the file from INSIDE the container and
    ``check_sum`` is the md5 of the file's bytes (both measured against the
    live database on 2026-08-24, matching ``md5sum`` for the two provisioned
    dashboards). Comparing content beats comparing names: a dashboard whose
    file changed but whose title did not would pass any name-level check.

    md5 is not a security choice here — Grafana picked it, and we only compare
    against what Grafana itself stored.
    """
    return {
        f"{PROVISIONED_DASHBOARD_DIR}/{managed.live_name}": hashlib.md5(
            content, usedforsecurity=False
        ).hexdigest()
        for managed, content in desired.items()
        if managed.kind is FileKind.DASHBOARD
    }


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
        # Only the modern object form is collected. Grafana's legacy form is a
        # bare string, but that string is the datasource NAME, not its uid, so
        # feeding it to a uid comparison would refuse valid dashboards. A
        # dashboard written in the legacy form is therefore not cross-checked
        # here — the shipped ones use the object form (schemaVersion 39).
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
    grafana: GrafanaPort,
    emit: EmitPort,
    now_fn: Callable[[], float] = time.time,
    sleep_fn: Callable[[float], None] = time.sleep,
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
        outcome, replaced = _sync(
            desired,
            differing=differing,
            live=live,
            grafana=grafana,
            now_fn=now_fn,
            sleep_fn=sleep_fn,
        )
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
    grafana: GrafanaPort,
    now_fn: Callable[[], float],
    sleep_fn: Callable[[float], None],
) -> tuple[Outcome, tuple[ManagedFile, ...]]:
    """The write path, in the owner-settled order: validate the whole desired
    set, then per differing file backup + prune, temp file, atomic replace,
    then — once the WHOLE tree is installed — the restart (if the change needs
    one) and the convergence verification.

    Returns the outcome together with the files actually replaced.
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

    changed = tuple(differing)
    # Restart only after every file is on disk: a restart mid-install would
    # boot Grafana against a half-applied provisioning tree.
    restarted = any(managed.kind in RESTART_REQUIRING_KINDS for managed in changed)
    if restarted:
        logger.info("restarting the %s container (provisioning config changed)", CONTAINER_NAME)
        if not grafana.restart():
            # New content on disk, container refused to come back — polling it
            # would only burn the verify budget.
            return Outcome.RELOAD_FAILED, changed

    # Verify only what THIS run changed. A datasource file that was already in
    # sync is not this run's claim to make, and demanding it would deadlock: a
    # uid missing from a container that never restarts has no remedy here.
    expected_uids: set[str] = set()
    for managed in changed:
        if managed.kind is FileKind.DATASOURCE:
            expected_uids |= parse_datasource_uids(desired[managed])
    expected_checksums = dashboard_checksums({m: desired[m] for m in changed})

    if _converged(
        grafana,
        restarted=restarted,
        expected_uids=expected_uids,
        expected_checksums=expected_checksums,
        sleep_fn=sleep_fn,
    ):
        return Outcome.SYNCED, changed
    return Outcome.RELOAD_FAILED, changed


def _converged(
    grafana: GrafanaPort,
    *,
    restarted: bool,
    expected_uids: set[str],
    expected_checksums: Mapping[str, str],
    sleep_fn: Callable[[float], None],
) -> bool:
    """Poll until Grafana is observed serving the new content, or give up.

    Every read fails CLOSED: an unreachable health endpoint or an unreadable
    database is indistinguishable from "did not converge", and guessing in
    Grafana's favour is exactly how the 2026-08-24 drift survived two months.
    """
    uids: set[str] | None = None
    checksums: dict[str, str] | None = None
    for attempt in range(VERIFY_ATTEMPTS):
        if attempt:
            sleep_fn(VERIFY_DELAY_S)
        if restarted and not grafana.health_ok():
            continue
        if expected_uids:
            uids = grafana.datasource_uids()
            if uids is None or not expected_uids <= uids:
                continue
        if expected_checksums:
            checksums = grafana.provisioned_checksums()
            if checksums is None:
                continue
            if any(checksums.get(path) != digest for path, digest in expected_checksums.items()):
                continue
        return True
    logger.error(
        "grafana never converged after %d attempts: expected datasource uid(s) %s (saw %s) "
        "and provisioned checksum(s) %s (saw %s)",
        VERIFY_ATTEMPTS,
        sorted(expected_uids),
        "unreadable" if uids is None else sorted(uids),
        dict(expected_checksums),
        "unreadable" if checksums is None else checksums,
    )
    return False


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


def _fetch_health_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=API_TIMEOUT_S) as response:
        return json.load(response)


class DockerGrafana:
    """Restart / health / provisioning-state against the grafana container.

    None of the methods raise: a ``False``/``None`` answer routes the run into
    ``reload_failed``, while an exception raised after the replace could skip
    the metric emit. The only docker verbs used are ``restart`` and a
    read-only ``cp`` of the sqlite state.
    """

    def __init__(
        self,
        *,
        container: str = CONTAINER_NAME,
        timeout: float = DOCKER_TIMEOUT_S,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        fetch_json: Callable[[str], dict] = _fetch_health_json,
        copy_db: Callable[[Path], bool] | None = None,
    ):
        self._container = container
        self._timeout = timeout
        self._runner = runner
        self._fetch_json = fetch_json
        self._copy_db = copy_db or self._docker_cp

    def restart(self) -> bool:
        # Plain `docker restart` by container name, not `docker compose`: the
        # monitoring compose file is not in this repo, and the container's own
        # `restart: unless-stopped` policy is preserved either way.
        proc = self._docker(["docker", "restart", self._container])
        if proc is None or proc.returncode != 0:
            if proc is not None:
                logger.error("restarting %s failed: %s", self._container, _proc_text(proc))
            return False
        return True

    def health_ok(self) -> bool:
        try:
            payload = self._fetch_json(HEALTH_URL)
        except Exception:
            logger.info("%s not answering yet", HEALTH_URL)
            return False
        return isinstance(payload, Mapping) and payload.get("database") == "ok"

    def datasource_uids(self) -> set[str] | None:
        rows = self._query(DATASOURCE_UID_SQL)
        if rows is None:
            return None
        return {str(row[0]) for row in rows}

    def provisioned_checksums(self) -> dict[str, str] | None:
        rows = self._query(DASHBOARD_PROVISIONING_SQL)
        if rows is None:
            return None
        return {str(row[0]): str(row[1]) for row in rows}

    def _docker_cp(self, target: Path) -> bool:
        argv = ["docker", "cp", f"{self._container}:{CONTAINER_DB_PATH}", str(target)]
        proc = self._docker(argv)
        if proc is None or proc.returncode != 0:
            if proc is not None:
                logger.error("could not copy grafana.db out: %s", _proc_text(proc))
            return False
        return True

    def _query(self, sql: str) -> list[tuple] | None:
        """Copy the live sqlite state out and read it, read-only.

        A copy rather than an in-container query because the grafana image
        ships no sqlite3 binary, and a copy rather than a host-side open
        because the docker volume directory is root-only.
        """
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "grafana.db"
            if not self._copy_db(target):
                return None
            try:
                with contextlib.closing(
                    sqlite3.connect(f"file:{target}?mode=ro", uri=True)
                ) as conn:
                    self._warn_on_unexpected_journal_mode(conn)
                    return list(conn.execute(sql))
            except Exception:
                logger.exception("could not read the copied grafana.db")
                return None

    @staticmethod
    def _warn_on_unexpected_journal_mode(conn: sqlite3.Connection) -> None:
        # Measured 'delete' on 2026-08-24, which makes a plain file copy
        # self-consistent. Were a Grafana upgrade to switch to WAL, the copy
        # could miss recent commits and verification would fail for a reason
        # nothing else would name — so name it here.
        row = conn.execute("PRAGMA journal_mode").fetchone()
        mode = str(row[0]).lower() if row else "unknown"
        if mode != "delete":
            logger.warning(
                "grafana.db journal_mode is %r, not 'delete'; a plain copy may lag "
                "recent commits and make verification flap",
                mode,
            )

    def _docker(self, argv: list[str]) -> subprocess.CompletedProcess | None:
        try:
            return self._runner(argv, capture_output=True, timeout=self._timeout)
        except Exception:
            logger.exception("`%s` did not complete", " ".join(argv))
            return None


def _proc_text(proc: subprocess.CompletedProcess) -> str:
    parts = []
    for stream in (proc.stdout, proc.stderr):
        text = stream.decode("utf-8", errors="replace") if isinstance(stream, bytes) else stream
        if text:
            parts.append(text.strip())
    return " | ".join(parts)


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
        grafana=DockerGrafana(),
        emit=default_emit,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    raise SystemExit(main())
