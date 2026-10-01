"""Byte snapshots of a journal, taken before boot compaction rewrites it (#1648).

Boot compaction (``control_loop._compact_standalone_stop_journal``,
``entry_trails.compact_entry_trail_journal``) keeps only the lines the daemon's
folds still need. Some removed lines exist nowhere else, for example a closed
position's ``tranche_fired`` with the decision-side bid/ask/spread at the moment
a take-profit fired. Each compactor therefore snapshots the exact bytes it
compacted from before it replaces the journal, and :func:`iter_journal_history`
rebuilds the full record history from those snapshots plus the current journal.

A snapshot is a whole-file copy rather than "the dropped lines": the standalone
compactor returns re-serialized records, so deciding which lines were dropped
would take a second matching rule that could itself be wrong.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
import os
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, NamedTuple

logger = logging.getLogger(__name__)

SNAPSHOT_DIRNAME = "compaction_snapshots"
_SNAPSHOT_SUFFIX = ".jsonl"
_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
_FILE_MODE = 0o600
_DIR_MODE = 0o700

STATUS_COMPACTED = "compacted"
STATUS_UNCHANGED = "unchanged"
STATUS_SKIPPED = "skipped"


class CompactionOutcome(NamedTuple):
    """What one boot compaction did to one journal. ``reason`` is set only
    when it was skipped, and is safe to show the operator."""

    journal: str
    status: str
    reason: str = ""


def snapshot_dir(journal: Path) -> Path:
    """Where ``journal``'s snapshots live: a sibling directory of the journal,
    derived from its path, so a test that points the journal at a temporary
    directory keeps its snapshots there too."""
    return journal.parent / SNAPSHOT_DIRNAME


def snapshot_bytes(
    journal: Path,
    data: bytes,
    *,
    clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
) -> Path:
    """Write ``data`` (the bytes a compactor read from ``journal``) as a new
    snapshot and make it durable. Returns the snapshot path.

    The name is ``<journal stem>.<UTC stamp>.<pid>.jsonl``, so snapshots of one
    journal sort oldest first. The file is created with ``O_EXCL`` (never
    overwritten) and mode 0600, like the journals; the directory is 0700. Raises
    ``OSError`` on any failure, after removing a partly written file."""
    directory = snapshot_dir(journal)
    directory.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
    stamp = clock().astimezone(dt.UTC).strftime(_STAMP_FORMAT)
    path = directory / f"{journal.stem}.{stamp}.{os.getpid()}{_SNAPSHOT_SUFFIX}"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _FILE_MODE)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        path.unlink(missing_ok=True)
        raise
    os.close(fd)
    _fsync_directory(directory)
    return path


def _fsync_directory(directory: Path) -> None:
    """Make the new directory entry itself durable (POSIX)."""
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def replace_compacted(
    journal: Path,
    original: bytes,
    compacted: bytes,
    *,
    before: os.stat_result,
    tmp_prefix: str,
) -> CompactionOutcome:
    """Replace ``journal`` with ``compacted`` without losing a byte of
    ``original`` (the bytes the caller read, ``before`` being the journal's
    stat taken just before that read).

    - Byte-identical output: nothing to do, no snapshot, no rewrite.
    - Otherwise snapshot ``original`` first. If that fails, the journal is
      left as it is: losing lines is exactly what this exists to stop.
    - Write the compacted bytes to a temp file in the journal's directory.
    - Just before replacing, re-check the journal's size and mtime. If another
      process appended meanwhile (the CLI can), give up and keep the journal:
      the appended line is in no snapshot, so replacing would lose it.
    The window between that check and ``os.replace`` remains; it is far
    smaller than the read-to-replace window it closes."""
    name = journal.name
    if compacted == original:
        return CompactionOutcome(name, STATUS_UNCHANGED)
    try:
        snapshot_bytes(journal, original)
    except OSError as exc:
        logger.warning("journal compaction skipped for %s: snapshot failed (%s)", name, exc)
        return CompactionOutcome(name, STATUS_SKIPPED, f"snapshot failed ({exc})")
    fd, tmp_name = tempfile.mkstemp(dir=str(journal.parent), prefix=tmp_prefix, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(compacted)
            fh.flush()
            os.fsync(fh.fileno())
        now = journal.stat()
        if (now.st_size, now.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            os.unlink(tmp_name)
            logger.warning("journal compaction skipped for %s: it changed during compaction", name)
            return CompactionOutcome(name, STATUS_SKIPPED, "the journal changed during compaction")
        os.replace(tmp_name, str(journal))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    return CompactionOutcome(name, STATUS_COMPACTED)


def _snapshots_of(journal: Path) -> list[Path]:
    directory = snapshot_dir(journal)
    if not directory.is_dir():
        return []
    return sorted(directory.glob(f"{journal.stem}.*{_SNAPSHOT_SUFFIX}"))


def _records_in(path: Path) -> Iterator[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            yield record


def iter_journal_history(journal: Path) -> Iterator[dict[str, Any]]:
    """Every record ``journal`` ever held that a snapshot or the current file
    still has: the snapshots oldest first, then the current journal. Each record
    is yielded once, keyed on its canonical JSON (``sort_keys=True``, which is
    how ``brokers.journal.append_json_line`` writes every line). Malformed lines
    are skipped."""
    seen: set[str] = set()
    for path in [*_snapshots_of(journal), journal]:
        for record in _records_in(path):
            key = json.dumps(record, sort_keys=True, default=str)
            if key in seen:
                continue
            seen.add(key)
            yield record


__all__ = [
    "SNAPSHOT_DIRNAME",
    "STATUS_COMPACTED",
    "STATUS_SKIPPED",
    "STATUS_UNCHANGED",
    "CompactionOutcome",
    "iter_journal_history",
    "replace_compacted",
    "snapshot_bytes",
    "snapshot_dir",
]
