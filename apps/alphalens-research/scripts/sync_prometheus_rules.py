"""Converge the live Prometheus rules file to the ``origin/main`` blob.

The SoT is ``deploy/monitoring/prometheus/rules/alphalens.yaml`` in the repo
(promtool-checked and unit-tested in CI). The live copy is
``~/monitoring/prometheus/alphalens.rules`` on the VPS, bind-mounted into the
SHARED prometheus container (which also serves gunbot + node alerts — this job
must NEVER touch anything in that directory except ``alphalens.rules`` and its
own backups). Drift between the two bit three times: missing rules
(2026-05-31), new alerts absent (2026-08-19), and a metric RENAME
(2026-08-20) where every alert NAME matched while live expressions summed
absent series — invisible to a name-level diff.

The contract that shapes every decision below
---------------------------------------------
**Live converges within one cadence, and a broken sync pages.** Concretely:

* the content source is the fetched ``origin/main`` BLOB (``git show``),
  never the working tree — a stale checkout nearly shipped stale rules on
  2026-08-23;
* identical content is a full short-circuit: no backup, no temp file, no HUP
  (outcome ``in_sync``);
* on difference the new content is promtool-checked INSIDE the container
  BEFORE the atomic replace — a refusal leaves live byte-identical (outcome
  ``check_failed``);
* the reload is verified against a FINGERPRINT of the new content: every
  alert name parsed from the new YAML must appear in ``api/v1/rules``. A bare
  "reload succeeded" flag is insufficient — the 2026-05-31 incident was a HUP
  that reloaded a stale file and looked like success;
* every run rewrites the outcome one-hot metric family with ALL five labels
  (``in_sync``/``synced``/``fetch_failed``/``check_failed``/``reload_failed``),
  zeros included — an absent series must mean "broken emitter", never a
  state — and stamps the success timestamp only on ``in_sync``/``synced`` so
  a failing sync stalls the staleness clock;
* exit 0 only on ``in_sync``/``synced``; any failure outcome exits 1 so the
  systemd unit fails and the journal carries the reason.

Backups are ``alphalens.rules.bak-autosync-<UTC stamp>`` and pruning deletes
ONLY files with that exact prefix beyond the newest 10 — the operator's manual
backups use other naming styles and must survive.

git, docker, the rules API, the clock and the metric emitter are injected
ports; the live-directory filesystem operations are a small concrete class
tested against a real temporary directory.

Usage::

    .venv/bin/python apps/alphalens-research/scripts/sync_prometheus_rules.py
    .venv/bin/python apps/alphalens-research/scripts/sync_prometheus_rules.py --dry-run

Runs on the VPS as ``alphalens-prometheus-rules-sync.timer`` (hourly). Needs
the ``jacoren`` user's docker access and the checkout at ``~/AlphaLens``; no
secrets are read (the repo is public, the fetch is anonymous).
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
import urllib.request
from collections.abc import Callable, Mapping
from enum import Enum
from pathlib import Path
from typing import Protocol

import yaml

logger = logging.getLogger(__name__)

JOB_NAME = "prometheus-rules-sync"

DEFAULT_REPO_DIR = Path.home() / "AlphaLens"
DEFAULT_LIVE_DIR = Path.home() / "monitoring" / "prometheus"
RULES_REPO_PATH = "deploy/monitoring/prometheus/rules/alphalens.yaml"
LIVE_RULES_FILENAME = "alphalens.rules"

BACKUP_PREFIX = "alphalens.rules.bak-autosync-"
BACKUP_KEEP = 10

CONTAINER_NAME = "prometheus"
# The live dir is bind-mounted here inside the container; promtool must check
# the file at its container path.
CONTAINER_RULES_DIR = "/etc/prometheus"
RULES_API_URL = "http://localhost:9090/api/v1/rules"

GIT_TIMEOUT_S = 120
DOCKER_TIMEOUT_S = 60
API_TIMEOUT_S = 10

# A HUP reload is asynchronous: prometheus acknowledges the signal and swaps
# rule groups on its own schedule, so the first api/v1/rules poll can race it.
VERIFY_ATTEMPTS = 5
VERIFY_DELAY_S = 2.0

OUTCOME_METRIC = "alphalens_rules_sync_outcome"
SUCCESS_METRIC = "alphalens_rules_sync_last_success_timestamp_seconds"


class Outcome(Enum):
    """Exactly the five run outcomes; the metric label set mirrors this enum."""

    IN_SYNC = "in_sync"
    SYNCED = "synced"
    FETCH_FAILED = "fetch_failed"
    CHECK_FAILED = "check_failed"
    RELOAD_FAILED = "reload_failed"


SUCCESS_OUTCOMES = frozenset({Outcome.IN_SYNC, Outcome.SYNCED})


class GitPort(Protocol):
    """Reading the SoT blob. Both operations raise :class:`GitCommandError`."""

    def fetch(self) -> None: ...

    def show_rules(self) -> bytes: ...


class PrometheusPort(Protocol):
    """The container-side operations. None of these raise — a False/None answer
    is already the loud arm (``check_failed`` / ``reload_failed``)."""

    def promtool_check(self, temp_name: str) -> bool: ...

    def reload(self) -> bool: ...

    def active_alert_names(self) -> set[str] | None: ...


EmitPort = Callable[[str, Mapping[str, float]], object]


# ----------------------------------------------------------------------------
# Pure helpers
# ----------------------------------------------------------------------------


def parse_alert_names(content: bytes) -> set[str]:
    """Every alert name in a rules YAML — the reload-verification fingerprint.

    Never raises: by the time this runs promtool has already accepted the
    content, so a parse failure here means a promtool/PyYAML disagreement.
    The empty set makes verification fail loudly (``reload_failed``) instead
    of crashing past the metric emit.
    """
    try:
        doc = yaml.safe_load(content.decode("utf-8"))
    except Exception:
        logger.exception("could not parse the new rules YAML for the fingerprint")
        return set()
    if not isinstance(doc, dict):
        return set()
    names: set[str] = set()
    for group in doc.get("groups") or []:
        if not isinstance(group, dict):
            continue
        for rule in group.get("rules") or []:
            if isinstance(rule, dict) and "alert" in rule:
                names.add(str(rule["alert"]))
    return names


def parse_rules_payload(payload: Mapping) -> set[str]:
    """Alert names in an ``api/v1/rules`` response, across ALL groups.

    The container is shared, so foreign groups (gunbot, node) appear too;
    the fingerprint check is a subset test and extra names are harmless.
    """
    names: set[str] = set()
    data = payload.get("data") or {}
    for group in data.get("groups") or []:
        for rule in group.get("rules") or []:
            if rule.get("type") == "alerting":
                names.add(str(rule.get("name")))
    return names


def build_metrics(outcome: Outcome, now_ts: float) -> dict[str, float]:
    """The per-run textfile gauges.

    ALL five outcome labels every run, zeros included — a series that
    disappears is indistinguishable from a stopped exporter, and the
    sustained-failure alert needs a clean run's 0 to clear. The success
    timestamp appears only on success outcomes (mirroring the bash
    ``alphalens-emit-job-metrics`` hook) so a failing sync stalls the
    staleness clock instead of quietly advancing it.
    """
    metrics: dict[str, float] = {
        f'{OUTCOME_METRIC}{{outcome="{candidate.value}"}}': (1 if candidate is outcome else 0)
        for candidate in Outcome
    }
    if outcome in SUCCESS_OUTCOMES:
        metrics[SUCCESS_METRIC] = int(now_ts)
    return metrics


def utc_stamp(now_ts: float) -> str:
    """Sortable UTC stamp for backup names (prune keeps the newest N by name)."""
    return dt.datetime.fromtimestamp(now_ts, tz=dt.UTC).strftime("%Y%m%dT%H%M%SZ")


# ----------------------------------------------------------------------------
# Live-directory filesystem operations (concrete; tested on a real tmpdir)
# ----------------------------------------------------------------------------


class LiveDir:
    """The ONLY writes this job may perform: ``alphalens.rules``, its autosync
    backups, and a short-lived temp file — all inside the live dir. The dir is
    shared with foreign tenants' config (``prometheus.yml``, ``alert.rules``)
    which must never be touched."""

    def __init__(self, path: Path):
        self.path = Path(path)

    @property
    def live_path(self) -> Path:
        return self.path / LIVE_RULES_FILENAME

    def read_live(self) -> bytes | None:
        try:
            return self.live_path.read_bytes()
        except FileNotFoundError:
            return None

    def write_temp(self, content: bytes) -> str:
        """Write the candidate content next to the live file; return its name.

        Same directory as the target so the eventual ``os.replace`` is a
        single ``rename(2)``. chmod 644 because promtool runs inside the
        container as a different user — an unreadable temp file would report
        ``check_failed`` for content that is actually fine.
        """
        fd, temp_path = tempfile.mkstemp(
            dir=self.path, prefix=".alphalens.rules.sync-", suffix=".tmp"
        )
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.chmod(temp_path, 0o644)
        return Path(temp_path).name

    def remove_temp(self, name: str) -> None:
        (self.path / name).unlink(missing_ok=True)

    def backup_live(self, stamp: str) -> Path | None:
        """Copy the current live file aside; no-op when live does not exist yet."""
        if not self.live_path.exists():
            return None
        target = self.path / f"{BACKUP_PREFIX}{stamp}"
        shutil.copy2(self.live_path, target)
        return target

    def prune_autosync_backups(self, keep: int = BACKUP_KEEP) -> None:
        """Delete autosync backups beyond the newest ``keep``.

        Anchored on the EXACT autosync prefix: the operator keeps manual
        backups in other naming styles (``.bak-<slug>-<ts>``, ``.bak.<ts>Z``)
        and the dir holds foreign tenants' files — none of those are ours to
        delete. The UTC stamp is lexically sortable, so name order is age
        order.
        """
        backups = sorted(
            (entry for entry in self.path.iterdir() if entry.name.startswith(BACKUP_PREFIX)),
            key=lambda entry: entry.name,
            reverse=True,
        )
        for stale in backups[keep:]:
            stale.unlink()

    def replace_live_with_temp(self, name: str) -> None:
        """Atomic install: ``rename(2)``, so a scrape or reload mid-replace
        sees either the old complete file or the new complete file."""
        os.replace(self.path / name, self.live_path)


# ----------------------------------------------------------------------------
# Run loop
# ----------------------------------------------------------------------------


def run(
    *,
    git: GitPort,
    live: LiveDir,
    prom: PrometheusPort,
    emit: EmitPort,
    now_fn: Callable[[], float] = time.time,
    sleep_fn: Callable[[float], None] = time.sleep,
    dry_run: bool = False,
) -> int:
    """One pass. Returns the process exit code (0 = live matches SoT now)."""
    try:
        git.fetch()
        desired = git.show_rules()
    except GitCommandError:
        logger.exception("could not read the origin/main rules blob")
        if not dry_run:
            _emit(emit, Outcome.FETCH_FAILED, now_fn())
        return 1

    current = live.read_live()
    if current == desired:
        if dry_run:
            print("in_sync: live rules match the origin/main blob; nothing to do")
            return 0
        _emit(emit, Outcome.IN_SYNC, now_fn())
        return 0

    if dry_run:
        live_size = "absent" if current is None else f"{len(current)} bytes"
        print(f"would sync: live rules ({live_size}) differ from the blob ({len(desired)} bytes)")
        return 0

    outcome = _sync(desired, live=live, prom=prom, now_fn=now_fn, sleep_fn=sleep_fn)
    _emit(emit, outcome, now_fn())
    return 0 if outcome in SUCCESS_OUTCOMES else 1


def _sync(
    desired: bytes,
    *,
    live: LiveDir,
    prom: PrometheusPort,
    now_fn: Callable[[], float],
    sleep_fn: Callable[[float], None],
) -> Outcome:
    """The write path, in the owner-settled order: backup + prune, temp file,
    promtool check BEFORE the replace, atomic replace, HUP, fingerprint
    verification."""
    live.backup_live(utc_stamp(now_fn()))
    live.prune_autosync_backups(BACKUP_KEEP)

    temp_name = live.write_temp(desired)
    if not prom.promtool_check(temp_name):
        # Live is untouched; the backup made above is of the unchanged file
        # and harmless. The bad content never replaces the running rules.
        live.remove_temp(temp_name)
        return Outcome.CHECK_FAILED

    live.replace_live_with_temp(temp_name)

    if not prom.reload():
        # New file on disk, but prometheus may still run the old rules —
        # the outcome says exactly that.
        return Outcome.RELOAD_FAILED

    expected = parse_alert_names(desired)
    if not expected:
        # No alert names means no fingerprint; "verified" would be a guess.
        logger.error("new rules content contains no alert names; cannot verify the reload")
        return Outcome.RELOAD_FAILED

    for attempt in range(VERIFY_ATTEMPTS):
        if attempt:
            sleep_fn(VERIFY_DELAY_S)
        active = prom.active_alert_names()
        if active is not None and expected <= active:
            return Outcome.SYNCED
    logger.error(
        "reload verification failed: %s not all present in api/v1/rules after %d attempts",
        sorted(expected),
        VERIFY_ATTEMPTS,
    )
    return Outcome.RELOAD_FAILED


def _emit(emit: EmitPort, outcome: Outcome, now_ts: float) -> None:
    """Swallow-all: the sync already happened (or loudly failed via the exit
    code); a broken textfile dir is observability debt, not a second failure."""
    try:
        emit(JOB_NAME, build_metrics(outcome, now_ts))
    except Exception:
        logger.exception("outcome metric emit failed; continuing")


# ----------------------------------------------------------------------------
# Default adapters (the only code that talks to the outside world)
# ----------------------------------------------------------------------------


class GitCommandError(RuntimeError):
    """A git invocation failed, carrying git's own explanation."""


class GitCli:
    """git via argv lists (never a shell string), always ``-C <repo>``.

    Reads the ``origin/main`` BLOB (``git show origin/main:<path>``), never
    the working tree — the fetch is read-only for the checkout, and a stale
    or dirty working tree cannot leak into the deployed content.
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

    def show_rules(self) -> bytes:
        return self._git("show", f"origin/main:{RULES_REPO_PATH}")

    def _git(self, *args: str) -> bytes:
        argv = ["git", "-C", str(self.repo_dir), *args]
        proc = self._runner(argv, capture_output=True, timeout=self._timeout)
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


def _fetch_rules_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=API_TIMEOUT_S) as response:
        return json.load(response)


class DockerPrometheus:
    """promtool / HUP / rules-API against the shared prometheus container.

    None of the methods raise: a False/None answer routes the run into the
    matching loud outcome, while an exception past the promtool gate could
    skip the metric emit. The only docker verbs used are ``exec promtool``
    (read-only over the bind mount) and the HUP.
    """

    def __init__(
        self,
        *,
        container: str = CONTAINER_NAME,
        timeout: float = DOCKER_TIMEOUT_S,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        fetch_json: Callable[[str], dict] = _fetch_rules_json,
    ):
        self._container = container
        self._timeout = timeout
        self._runner = runner
        self._fetch_json = fetch_json

    def promtool_check(self, temp_name: str) -> bool:
        # The live dir is bind-mounted at CONTAINER_RULES_DIR, so the host
        # temp file is visible inside the container under its basename.
        argv = [
            "docker",
            "exec",
            self._container,
            "promtool",
            "check",
            "rules",
            f"{CONTAINER_RULES_DIR}/{temp_name}",
        ]
        proc = self._docker(argv)
        if proc is None or proc.returncode != 0:
            if proc is not None:
                logger.error("promtool refused the new rules: %s", _proc_text(proc))
            return False
        return True

    def reload(self) -> bool:
        argv = ["docker", "exec", self._container, "kill", "-HUP", "1"]
        proc = self._docker(argv)
        if proc is None or proc.returncode != 0:
            if proc is not None:
                logger.error("HUP reload failed: %s", _proc_text(proc))
            return False
        return True

    def active_alert_names(self) -> set[str] | None:
        try:
            payload = self._fetch_json(RULES_API_URL)
        except Exception:
            logger.exception("could not read %s", RULES_API_URL)
            return None
        return parse_rules_payload(payload)

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
        help=f"directory holding the live {LIVE_RULES_FILENAME} (default {DEFAULT_LIVE_DIR})",
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
        live=LiveDir(Path(args.live_dir)),
        prom=DockerPrometheus(),
        emit=default_emit,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    raise SystemExit(main())
