"""One append for every broker journal — integrity and durability in one place.

The journals (`picks.jsonl`, `submissions.jsonl`, `entry_trails.jsonl`, the
standalone-stop journal) are append-only JSONL and the source of truth between
the CLI that arms picks and the daemon that places real orders. Two failure
modes used to be handled unevenly across the four writers, and they are
different things:

**Integrity.** A partial write — ENOSPC is the realistic cause — leaves a line
with no trailing newline. The NEXT append then concatenates onto it, both
records merge into one unparseable line, and BOTH are lost. Measured on
2026-09-11 (#1421): arm a pick, tear the line, arm a second — the CLI reports
success for the second and the fold never sees it, while the malformed counter
reads 1 rather than 2. On `submissions.jsonl` the same shape escalates: the
`"attempt"` record is a write-ahead, so losing it with the following `"placed"`
record takes the key out of the join and the drain re-places a pick that already
rests at the broker.

**Durability.** A buffered write lost to a crash or a SIGKILL drops the record
outright. `entry_trails` and the standalone-stop journal already flushed and
fsynced for exactly this reason; `picks` and `submissions` did not — the two
that decide whether a pick is placed and whether it is re-placed.

Stdlib only, and no import from `automanager`, so every journal writer can reach
it without a cycle.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["JournalWriteError", "append_json_line"]


class JournalWriteError(OSError):
    """A journal append failed; nothing was recorded for this call.

    Subclasses ``OSError`` ON PURPOSE. The daemon already catches ``OSError``
    around one journal write (``control_loop`` sibling-retire), alerts the
    operator and keeps the tick alive so the protection pass is not starved —
    a class outside that hierarchy would silently convert a handled condition
    into an aborted tick.
    """


def _ends_without_newline(path: Path) -> bool:
    """Does the file end mid-line, i.e. did a previous write not finish?

    ONE open, with the emptiness case handled by the exception rather than by a
    preceding ``stat``: the two calls are not atomic, and ``seek(-1, SEEK_END)``
    on an empty file raises ``OSError`` (errno 22) rather than returning 0 — so
    a check-then-open would turn an ordinary state into a crash. Ordinary is the
    right word: both compactors "create or truncate a file that has nothing to
    compact", so an empty journal happens in normal operation.

    The last BYTE is what is read, never a character. A UTF-8 continuation byte
    is always >= 0x80, so 0x0A cannot appear inside a multi-byte character and a
    write torn in the middle of one cannot masquerade as a finished line.
    """
    try:
        with path.open("rb") as fh:
            try:
                fh.seek(-1, os.SEEK_END)
            except OSError:
                return False  # empty file
            return fh.read(1) not in (b"\n", b"")
    except FileNotFoundError:
        return False


def append_json_line(
    path: Path,
    record: Mapping[str, Any],
    *,
    default: Callable[[Any], Any] | None = None,
) -> None:
    """Append ``record`` as one JSON line, repairing a torn predecessor first.

    ``default`` stays a per-caller parameter rather than a constant. Three
    journals serialize with ``default=str``; ``picks`` deliberately does not,
    because an unrepresentable value in a ``TradeIntent`` must refuse loudly
    instead of becoming a silent string on the money path.

    Raises :class:`JournalWriteError` when the write fails. A serialization
    failure is NOT wrapped — a ``TypeError`` there is a programming error in the
    caller's payload, not an I/O condition a retry could clear.
    """
    # Serialisation stays OUTSIDE the guard: a payload that cannot be rendered
    # is a programming error in the caller, not an I/O condition a retry clears,
    # and it must not wear a retryable I/O code.
    line = json.dumps(record, sort_keys=True, default=default)
    try:
        # `mkdir` is part of the append, not a preamble: a directory that cannot
        # be created fails for the same reason a write fails — the queue did not
        # take the record — so it carries the same type.
        path.parent.mkdir(parents=True, exist_ok=True)
        # The PROBE is inside the try on purpose: a journal we cannot read is a
        # journal we cannot safely append to, so a failure here is the same
        # failure as a failed write and must carry the same type.
        separator = ""
        if _ends_without_newline(path):
            # The one thing that observes this failure mode as it happens: the
            # malformed counter is visible only to a human running `broker
            # picks`, and the drain never reads it at all.
            logger.warning(
                "journal %s ended mid-line — repairing the separator before appending; "
                "a previous write was torn (disk full, or a crash mid-append)",
                path,
            )
            separator = "\n"
        with path.open("a", encoding="utf-8") as fh:
            # ONE write. Two calls collapse into a single syscall under default
            # buffering but not under line buffering, where the separator would
            # reach the file alone and open a window for another appender. One
            # string makes the property structural rather than a side effect of
            # a setting this call site does not choose.
            fh.write(separator + line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    except OSError as exc:
        raise JournalWriteError(f"cannot append to {path}: {exc}") from exc
