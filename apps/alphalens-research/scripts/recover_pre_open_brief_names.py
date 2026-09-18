"""Recover, per as-of date, the brief list that existed BEFORE the arrival open (#1479).

Until #1482 the thematic build rewrote a date's brief up to six times a day, including
after the open of the session a reader could first trade. On 17 as-of dates the stored
list is therefore not the list that existed at that open, and `/edge` and the ML labels
measure the stored one. `thematic/publication.py` names those dates; this script recovers
the NAMES on them, from the only record that still holds them: the build journal.

What the journal gives us, per run:

- ``generate_briefs <asof>: wrote N briefs`` — the moment a list became the stored one.
- the score-stage table printed just before it, whose first column is the ticker.

The brief is that table minus unverified rows and minus duplicates of a ticker that sits
in two themes, so the UNIQUE names of the table are the brief's names, and their count
must equal ``N``. Where it does not, the table was cut short (``_print_score_preview``
stops at 25 rows) and the date is recorded as ``partial``, never silently completed.

Rules, in order, for one as-of date:

1. the deadline is ``publication.deadline_utc(asof)``, the arrival open;
2. of the brief writes for that date, keep the LAST one strictly before the deadline;
3. read the table printed by the same pid; that pid must belong to exactly one run in the
   extract, or the run cannot be identified and the date is refused;
4. the names are the unique tickers of the table, in the order printed;
5. the status is ``exact`` when their count equals the logged brief count, else
   ``partial``; a date with no pre-open write is ``no_pre_open_list`` and yields no names.

The table is read twice, by two parsers that fail differently — one keyed on the runs of
whitespace between columns, one on the fixed column widths of the printer's format string
— and a date whose two readings disagree is refused rather than reported. They do NOT fail
differently on a first token longer than the ticker column: both drop it, which the count
check then reports as ``partial``. That is the backstop, not the cross-check.

Read-only. It touches no store: the input is a frozen journal extract committed beside the
CSV it produces (``docs/research/pre_open_brief_names_2026_09_18*``).

Usage:
    python scripts/recover_pre_open_brief_names.py <journal-extract> <out.csv>

The extract may be plain text or gzip. With no arguments it reads the committed extract
and writes the committed CSV.
"""

from __future__ import annotations

import csv
import datetime as dt
import gzip
import re
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from alphalens_pipeline.thematic.publication import PUBLISHED_AFTER_OPEN_HISTORY, deadline_utc

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_EXTRACT = REPO_ROOT / "docs/research/pre_open_brief_names_2026_09_18_journal.log.gz"
DEFAULT_CSV = REPO_ROOT / "docs/research/pre_open_brief_names_2026_09_18.csv"

RECOVERY_EXACT = "exact"
RECOVERY_PARTIAL = "partial"
RECOVERY_NO_LIST = "no_pre_open_list"

CSV_COLUMNS = (
    "asof",
    "ticker",
    "position",
    "n_briefs_logged",
    "n_scored_logged",
    "n_names_recovered",
    "recovery_status",
    "source_run_utc",
    "source_pid",
)

_LINE = re.compile(r"^(\S+) \S+ \S+\[(\d+)\]: (.*)$")
_WROTE = re.compile(r"generate_briefs (\d{4}-\d{2}-\d{2}): wrote (\d+) briefs")
_SCORED = re.compile(r"Generating briefs for (\d+) scored rows .*asof=(\d{4}-\d{2}-\d{2})")
_TABLE_HEADER = re.compile(r"^ticker\s+industry\s+score\b")
_STAGE_MARKER = re.compile(r"^\[\d{4}-\d{2}-\d{2}T[\d:]+Z\] thematic\b")
# The printer writes "{ticker:8s} {industry:20s} ...", so a row's ticker is both the first
# whitespace-delimited token AND the first eight characters.
_TICKER = re.compile(r"^[A-Z][A-Z0-9.\-]{0,6}$")
_TICKER_FIELD_WIDTH = 8


@dataclass(frozen=True)
class DateRecovery:
    """What the journal says the brief for ``asof`` was at the arrival open."""

    asof: dt.date
    status: str
    n_briefs_logged: int | None
    names: list[str] = field(default_factory=list)
    source_run_utc: dt.datetime | None = None
    source_pid: str | None = None
    n_scored_logged: int | None = None

    @property
    def n_names_recovered(self) -> int:
        return len(self.names)


def iter_events(text: str) -> Iterator[tuple[dt.datetime, str, str]]:
    """Yield ``(timestamp, pid, message)`` for every journal line that has that shape."""
    for line in text.splitlines():
        match = _LINE.match(line)
        if match:
            stamp, pid, message = match.groups()
            yield dt.datetime.fromisoformat(stamp), pid, message


def _names_by_whitespace(rows: Sequence[str]) -> list[str]:
    """Read the ticker as the first whitespace-delimited token of the row."""
    names = []
    for row in rows:
        head = row.split(maxsplit=1)[0] if row.split() else ""
        if _TICKER.match(head):
            names.append(head)
    return names


def _names_by_column(rows: Sequence[str]) -> list[str]:
    """Read the ticker out of the printer's fixed 8-character first column."""
    names = []
    for row in rows:
        head = row[:_TICKER_FIELD_WIDTH].strip()
        if _TICKER.match(head) and row[_TICKER_FIELD_WIDTH:].strip():
            names.append(head)
    return names


def _table_for_pid(text: str, pid: str) -> list[str]:
    """The tickers of the score table printed by ``pid``, in order, with repeats kept.

    Two readings that fail differently: a row whose columns are not where the printer puts
    them is a ticker to the first reading and not to the second, so the mismatch is loud.
    """
    rows: list[str] = []
    inside = False
    for _, line_pid, message in iter_events(text):
        if line_pid != pid:
            continue
        if _TABLE_HEADER.match(message):
            inside, rows = True, []
            continue
        if not inside:
            continue
        if _STAGE_MARKER.match(message) or _SCORED.search(message):
            inside = False
            continue
        rows.append(message)
    if inside:
        raise ValueError(
            f"pid {pid}: the score table never ends in the extract, so the lines after it "
            "cannot be told apart from its rows"
        )
    by_whitespace = _names_by_whitespace(rows)
    by_column = _names_by_column(rows)
    if by_whitespace != by_column:
        raise ValueError(
            f"pid {pid}: the two readings of the score table disagree: "
            f"{by_whitespace} vs {by_column}"
        )
    return by_whitespace


def _unique(names: Iterable[str]) -> list[str]:
    seen: dict[str, None] = {}
    for name in names:
        seen.setdefault(name, None)
    return list(seen)


def recover_date(text: str, asof: dt.date) -> DateRecovery:
    """Recover the pre-open brief names for one as-of date (see the module docstring)."""
    deadline = deadline_utc(asof)
    writes: list[tuple[dt.datetime, str, int]] = []
    scored: dict[str, int] = {}
    pid_runs: dict[str, int] = {}
    for stamp, pid, message in iter_events(text):
        if (match := _WROTE.search(message)) and match.group(1) == asof.isoformat():
            writes.append((stamp, pid, int(match.group(2))))
        if (match := _SCORED.search(message)) and match.group(2) == asof.isoformat():
            scored[pid] = int(match.group(1))
        if _TABLE_HEADER.match(message):
            pid_runs[pid] = pid_runs.get(pid, 0) + 1

    before_open = [write for write in writes if write[0] < deadline]
    if not before_open:
        return DateRecovery(asof=asof, status=RECOVERY_NO_LIST, n_briefs_logged=None)

    stamp, pid, n_briefs = before_open[-1]
    if pid_runs.get(pid, 0) > 1:
        raise ValueError(
            f"{asof}: pid {pid} printed {pid_runs[pid]} score tables, so the run that wrote "
            "the brief cannot be identified"
        )
    names = _unique(_table_for_pid(text, pid))
    return DateRecovery(
        asof=asof,
        status=RECOVERY_EXACT if len(names) == n_briefs else RECOVERY_PARTIAL,
        n_briefs_logged=n_briefs,
        names=names,
        source_run_utc=stamp,
        source_pid=pid,
        n_scored_logged=scored.get(pid),
    )


def recovery_dates(text: str) -> list[dt.date]:
    """The affected as-of dates this extract can speak about.

    A date qualifies when its arrival open is after the extract's first line: only then
    could the write that set the stored list be inside the extract. Reading the bound off
    the text keeps a re-cut extract honest instead of trusting a constant.
    """
    first_event = next((stamp for stamp, _, _ in iter_events(text)), None)
    if first_event is None:
        raise ValueError("the extract holds no journal line")
    return sorted(asof for asof in PUBLISHED_AFTER_OPEN_HISTORY if deadline_utc(asof) > first_event)


def recover_all(text: str, dates: Iterable[dt.date]) -> list[DateRecovery]:
    return [recover_date(text, asof) for asof in sorted(dates)]


def _number(value: int | None) -> str:
    """An absent count is an empty cell, never the string "None"."""
    return "" if value is None else str(value)


def write_csv(recoveries: Sequence[DateRecovery], path: Path) -> None:
    """One row per recovered name; a date without names writes nothing."""
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, lineterminator="\n")
        writer.writeheader()
        for recovery in recoveries:
            for position, ticker in enumerate(recovery.names, start=1):
                writer.writerow(
                    {
                        "asof": recovery.asof.isoformat(),
                        "ticker": ticker,
                        "position": position,
                        "n_briefs_logged": _number(recovery.n_briefs_logged),
                        "n_scored_logged": _number(recovery.n_scored_logged),
                        "n_names_recovered": recovery.n_names_recovered,
                        "recovery_status": recovery.status,
                        "source_run_utc": (
                            recovery.source_run_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
                            if recovery.source_run_utc
                            else ""
                        ),
                        "source_pid": recovery.source_pid or "",
                    }
                )


def read_extract(path: Path) -> str:
    raw = path.read_bytes()
    if path.suffix == ".gz":
        raw = gzip.decompress(raw)
    return raw.decode("utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    extract = Path(args[0]) if args else DEFAULT_EXTRACT
    out = Path(args[1]) if len(args) > 1 else DEFAULT_CSV
    text = read_extract(extract)
    recoveries = recover_all(text, recovery_dates(text))
    write_csv(recoveries, out)
    for recovery in recoveries:
        print(
            f"{recovery.asof}  {recovery.status:18s} "
            f"names={recovery.n_names_recovered:>3} "
            f"briefs={recovery.n_briefs_logged if recovery.n_briefs_logged is not None else '-':>3} "
            f"scored={recovery.n_scored_logged if recovery.n_scored_logged is not None else '-':>3}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
