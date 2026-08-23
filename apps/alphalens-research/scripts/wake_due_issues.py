"""Wake the GitHub issues whose ``Wake:`` date has arrived.

An issue parked on external data ("re-measure the ML floor once 25 clusters
mature", "first options-vs-EDGE look at N>=30") carries the ``waiting:data``
label and one line in its body::

    Wake: 2026-09-27

Once a day this script finds the labelled issues whose wake date is TODAY OR
EARLIER, drops the label from each, and sends ONE Telegram message listing
them. Nothing due means nothing sent.

The contract that shapes every decision below
---------------------------------------------
**Silence must mean "nothing was due", never "the job died".** That cuts two
ways, and both are load-bearing:

* a quiet run is a real answer, so the job must not chatter on days with
  nothing to report (otherwise the operator filters it out and the one message
  that matters is lost in the noise); and
* every way this can fail must be LOUD — a non-zero exit (which the
  ``ExecStopPost`` metrics hook turns into ``alphalens_job_last_exit_code`` and
  a stalled ``last_success`` clock), a message, or both.

Consequences, each pinned by a test:

* a ``Wake:`` line the parser cannot read is REPORTED in the message, never
  skipped. A typo'd date is a human error that would otherwise park an issue
  forever, and its signature ("nothing due today") is identical to a healthy
  day. The parser never guesses what the author meant.
* the message is sent BEFORE any label is removed. If the send fails after the
  label is gone, the wake is lost for good; sending first makes the run
  at-least-once — a failed send leaves the labels on and tomorrow retries the
  whole set. The cost is a possible duplicate message, which is visible and
  harmless.
* a label removal that fails does not abort the rest, but does force a non-zero
  exit so the operator finishes it by hand.

GitHub and Telegram are injected ports (:class:`GitHubPort` / a
``Callable[[str], bool]`` sink), so the tests never touch the network. The
default adapters are the ``gh`` CLI and the canonical
:class:`~alphalens_pipeline.data.alt_data.telegram_client.TelegramClient` —
never a hand-rolled HTTP call (``tests/test_no_raw_telegram_http.py`` scans
this directory and enforces that).

Usage::

    .venv/bin/python apps/alphalens-research/scripts/wake_due_issues.py
    .venv/bin/python apps/alphalens-research/scripts/wake_due_issues.py --dry-run

Runs on the VPS as ``alphalens-issue-wake.timer`` (daily, 05:30 UTC). Needs
``gh`` on PATH with ``GH_TOKEN``, plus ``TELEGRAM_BOT_TOKEN`` /
``TELEGRAM_CHAT_ID`` — all three already live in ``/etc/alphalens/env``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

logger = logging.getLogger(__name__)

REPO = "kamilpajak/AlphaLens"
WAITING_LABEL = "waiting:data"

# One WHOLE line, nothing else on it. Prose that merely mentions the convention
# ("please set Wake: 2026-09-01 when the vendor confirms") must not arm the
# timer, so the pattern is anchored at both ends. Leading indentation and a
# trailing CR (GitHub bodies arrive CRLF) are tolerated.
# The KEY may carry ordinary Markdown decoration — a list bullet, a checkbox, a
# blockquote marker, bold, any case. An adversarial review found `- Wake: ...`
# in a bullet list produced NO match, which sent the issue to the silent
# `no_wake_line` bucket forever, on an issue whose very label asserts it carries
# one. The bullet form is the most likely thing a human types.
#
# The VALUE may be empty, and deliberately so: `Wake:` with the date not yet
# filled in must be REPORTED, not treated as "no wake line". That is the
# sharpest silent case, because the author believes the issue is armed.
#
# Still anchored at the start of a line, so prose that merely mentions the
# convention ("please set Wake: 2026-09-01 when the vendor confirms") does not
# arm the timer.
WAKE_LINE_RE = re.compile(
    r"^[ \t]*(?:[-*+>][ \t]*)*(?:\[[ xX]\][ \t]*)?"
    r"\*{0,2}wake\*{0,2}[ \t]*:\*{0,2}[ \t]*(?P<value>.*?)[ \t\r]*$",
    re.MULTILINE | re.IGNORECASE,
)

# The date format is exactly YYYY-MM-DD. `date.fromisoformat` also accepts
# `20260901` and other ISO spellings; accepting those would silently honour a
# format the issue convention never promised, so the shape is checked first and
# anything else is reported as malformed.
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# A fenced block is documentation, not instruction: issue templates paste
# `Wake: YYYY-MM-DD` inside a fence to show the format. Honouring that would
# wake every issue carrying the template.
FENCE_RE = re.compile(r"^[ \t]*(```|~~~)")
# GitHub renders three more things as code, and a Wake line inside any of them
# is documentation. `<pre>` and a 4-space indent are the two the fence rule
# missed; an issue template showing the format as an indented sample would have
# woken every issue carrying it AND stripped the label permanently.
PRE_OPEN_RE = re.compile(r"^[ \t]*<pre\b", re.IGNORECASE)
PRE_CLOSE_RE = re.compile(r"</pre>", re.IGNORECASE)
INDENTED_CODE_RE = re.compile(r"^(?: {4}|\t)")

# gh caps --limit; 200 is far above the realistic number of parked issues and
# keeps one page per run.
LIST_LIMIT = 200
GH_TIMEOUT_S = 60


class WakeStatus(Enum):
    """What the body said about its wake date."""

    OK = "ok"
    MISSING = "missing"
    MALFORMED = "malformed"


@dataclass(frozen=True)
class WakeParse:
    """Outcome of reading one issue body.

    ``raw`` carries the unparseable text verbatim so the Telegram message can
    quote it back at the author — reporting "issue #7 has a bad Wake line"
    without saying WHAT it says would send the operator hunting.
    """

    status: WakeStatus
    date: dt.date | None = None
    raw: str | None = None


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    url: str
    body: str


@dataclass
class Triage:
    """Every labelled issue, sorted into the buckets the run loop acts on."""

    due: list[tuple[Issue, dt.date]] = field(default_factory=list)
    waiting: list[Issue] = field(default_factory=list)
    malformed: list[tuple[Issue, str]] = field(default_factory=list)
    no_wake_line: list[Issue] = field(default_factory=list)

    @property
    def actionable(self) -> bool:
        """True when the run has something it must say.

        ``waiting`` and ``no_wake_line`` are the normal quiet state and never
        produce a message; ``malformed`` does, because it is a fault.
        """
        return bool(self.due or self.malformed)


class GitHubPort(Protocol):
    """The two GitHub operations this job needs. Injected so tests stay local."""

    def list_labelled_issues(self, label: str) -> list[Issue]: ...

    def remove_label(self, number: int, label: str) -> None: ...


NotifyPort = Callable[[str], bool]


# ----------------------------------------------------------------------------
# Pure parsing / triage
# ----------------------------------------------------------------------------


def strip_fenced_blocks(body: str) -> tuple[str, list[str]]:
    """Blank out every code block; return the body plus any Wake lines removed.

    An UNTERMINATED fence swallows the rest of the body, which is what GitHub
    itself renders — so the parser agrees with what the author sees. That is
    the conservative arm: the issue keeps waiting rather than waking on a line
    the author believes is inside a code sample.
    """
    out: list[str] = []
    removed: list[str] = []
    inside_fence = False
    inside_pre = False
    for line in body.splitlines():
        if FENCE_RE.match(line):
            inside_fence = not inside_fence
            out.append("")
            continue
        if PRE_OPEN_RE.match(line):
            inside_pre = True
        code = inside_fence or inside_pre or INDENTED_CODE_RE.match(line) is not None
        if PRE_CLOSE_RE.search(line):
            inside_pre = False
        if code:
            # Kept, not discarded: a Wake line in here must be REPORTED rather
            # than vanish. Blanking it made the issue MISSING, which is the same
            # forever-parked failure the fence rule exists to prevent, reached
            # by the other door.
            if WAKE_LINE_RE.match(line):
                removed.append(line.strip())
            out.append("")
            continue
        out.append(line)
    return "\n".join(out), removed


def parse_wake_date(body: str) -> WakeParse:
    """Read the wake date out of an issue body. Pure; never raises.

    The FIRST ``Wake:`` line wins — not the first PARSEABLE one. Issue bodies
    get appended to over time, and "first parseable" would let a later line
    silently repair a typo above it, which is the invented-date failure this
    job must not have.
    """
    prose, in_code = strip_fenced_blocks(body)
    match = WAKE_LINE_RE.search(prose)
    if match is None:
        if in_code:
            # A Wake line exists but only inside a code sample. Never honour it;
            # never stay quiet about it either.
            return WakeParse(status=WakeStatus.MALFORMED, raw=f"{in_code[0]} (inside a code block)")
        return WakeParse(status=WakeStatus.MISSING)

    raw = match.group("value").strip()
    if not ISO_DATE_RE.match(raw):
        return WakeParse(status=WakeStatus.MALFORMED, raw=raw)
    try:
        parsed = dt.date.fromisoformat(raw)
    except ValueError:
        # Shape-correct, calendar-wrong (2026-13-45).
        return WakeParse(status=WakeStatus.MALFORMED, raw=raw)
    return WakeParse(status=WakeStatus.OK, date=parsed)


def is_due(wake_date: dt.date, today: dt.date) -> bool:
    """Today or earlier is due. Strictly later keeps waiting."""
    return wake_date <= today


def triage_issues(issues: Iterable[Issue], today: dt.date) -> Triage:
    triage = Triage()
    for issue in issues:
        parsed = parse_wake_date(issue.body)
        if parsed.status is WakeStatus.MISSING:
            triage.no_wake_line.append(issue)
        elif parsed.status is WakeStatus.MALFORMED or parsed.date is None:
            # `date is None` can only mean MALFORMED — an OK parse always
            # carries a date. The extra clause states that invariant (and
            # proves it to the type checker); it is not a second failure mode.
            triage.malformed.append((issue, parsed.raw or ""))
        elif is_due(parsed.date, today):
            triage.due.append((issue, parsed.date))
        else:
            triage.waiting.append(issue)
    return triage


def build_message(triage: Triage, today: dt.date) -> str:
    """The single Telegram body. Plain text — see :func:`telegram_sink`."""
    lines = [f"AlphaLens wake check — {today.isoformat()}"]
    if triage.due:
        lines.append("")
        # "waking now", not "label removed": the send happens BEFORE the
        # removals, so at the moment this text is written the removals have not
        # been attempted yet and one of them may still fail.
        lines.append(f"Due ({len(triage.due)}) — waking now, {WAITING_LABEL} comes off:")
        lines.extend(
            f"• #{issue.number} {issue.title} (wake {wake_date.isoformat()})\n  {issue.url}"
            for issue, wake_date in triage.due
        )
    if triage.malformed:
        lines.append("")
        lines.append(f"Unreadable Wake line ({len(triage.malformed)}) — label left in place:")
        lines.extend(
            f"• #{issue.number} {issue.title} (Wake: {raw!r})\n  {issue.url}"
            for issue, raw in triage.malformed
        )
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Run loop
# ----------------------------------------------------------------------------


def run(
    *,
    github: GitHubPort,
    notify: NotifyPort,
    today: dt.date,
    label: str = WAITING_LABEL,
    dry_run: bool = False,
) -> int:
    """One pass. Returns the process exit code (0 ok, 1 something must be fixed)."""
    issues = github.list_labelled_issues(label)
    triage = triage_issues(issues, today)
    logger.info(
        "%d labelled: %d due, %d waiting, %d malformed, %d without a Wake line",
        len(issues),
        len(triage.due),
        len(triage.waiting),
        len(triage.malformed),
        len(triage.no_wake_line),
    )

    if not triage.actionable:
        return 0

    message = build_message(triage, today)
    if dry_run:
        print(message)
        return 0

    # Announce first, unlabel second — see the module docstring. A send that
    # fails must leave the labels ON so tomorrow's run repeats the whole set.
    if not notify(message):
        logger.error("Telegram send failed; labels left in place for the next run:\n%s", message)
        return 1

    exit_code = 0
    for issue, _ in triage.due:
        try:
            github.remove_label(issue.number, label)
        except Exception:
            # Keep going: one broken issue must not strand the others, but the
            # run still exits non-zero so the operator finishes it by hand.
            logger.exception("could not remove %s from #%d", label, issue.number)
            exit_code = 1
    return exit_code


# ----------------------------------------------------------------------------
# Default adapters (the only code that talks to the outside world)
# ----------------------------------------------------------------------------


class GitHubCommandError(RuntimeError):
    """A ``gh`` invocation failed, carrying gh's own explanation."""


class GhCliGitHub:
    """GitHub via the ``gh`` CLI, invoked with an argv list (never a shell string).

    ``gh`` is the project's GitHub surface (it already carries auth on the VPS
    via ``GH_TOKEN``), and ``--repo`` is always passed explicitly: an ambiguous
    repo context silently targets the wrong repository.

    ``runner`` is the ``subprocess.run`` seam, injected so the tests can pin the
    argv this builds without launching gh.
    """

    def __init__(
        self,
        repo: str = REPO,
        *,
        timeout: float = GH_TIMEOUT_S,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ):
        self._repo = repo
        self._timeout = timeout
        self._runner = runner

    def list_labelled_issues(self, label: str) -> list[Issue]:
        payload = self._gh(
            "issue",
            "list",
            "--label",
            label,
            "--state",
            "open",
            "--limit",
            str(LIST_LIMIT),
            "--json",
            "number,title,url,body",
        )
        rows = json.loads(payload)
        return [
            Issue(
                number=int(row["number"]),
                title=str(row.get("title") or ""),
                url=str(row.get("url") or ""),
                # gh returns "" for an empty body, but be defensive about null.
                body=str(row.get("body") or ""),
            )
            for row in rows
        ]

    def remove_label(self, number: int, label: str) -> None:
        self._gh("issue", "edit", str(number), "--remove-label", label)

    def _gh(self, *args: str) -> str:
        # Fixed argv list, never a shell string — the label and repo reach gh as
        # separate arguments, so nothing in them can be interpreted as syntax.
        argv = ["gh", *args, "--repo", self._repo]
        proc = self._runner(argv, capture_output=True, text=True, timeout=self._timeout)
        if proc.returncode != 0:
            # NOT check=True: CalledProcessError's message is the exit code
            # alone, and diagnosis happens in journald hours later. gh's own
            # explanation ("could not add label: not found", "HTTP 403") is the
            # entire diagnostic value, so carry it into the message.
            raise GitHubCommandError(
                f"`{' '.join(argv)}` exited {proc.returncode}: {(proc.stderr or '').strip()}"
            )
        return proc.stdout


class _TelegramSender(Protocol):
    def send_message(self, chat_id: str, text: str, **kwargs: object) -> bool: ...


def telegram_sink(client: _TelegramSender, chat_id: str) -> NotifyPort:
    """Wrap the canonical Telegram client as a ``NotifyPort``.

    ``parse_mode=""`` disables entity parsing. Issue titles carry ``_``, ``*``
    and ``[`` freely; under the client's default Markdown mode those return a
    Telegram 400 and the alert is SILENTLY dropped — the one outcome this job
    cannot have. The boolean is propagated so a failed send becomes a non-zero
    exit rather than a quiet success.
    """

    def _send(text: str) -> bool:
        return client.send_message(chat_id, text, parse_mode="")

    return _send


def default_notify() -> NotifyPort:
    from alphalens_pipeline.data.alt_data.telegram_client import TelegramClient

    return telegram_sink(
        TelegramClient(os.environ["TELEGRAM_BOT_TOKEN"]), os.environ["TELEGRAM_CHAT_ID"]
    )


def utc_today() -> dt.date:
    """Today in UTC.

    NOT ``date.today()``. The timer fires in UTC but the VPS runs Europe/Berlin,
    so on a Persistent=true catch-up firing at 22:30 UTC the local date is
    already tomorrow — and an issue would wake a day before its date arrived.
    """
    return dt.datetime.now(dt.UTC).date()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--repo", default=REPO, help=f"GitHub repo (default {REPO})")
    parser.add_argument(
        "--label", default=WAITING_LABEL, help=f"label to scan (default {WAITING_LABEL})"
    )
    parser.add_argument(
        "--today",
        default=None,
        help="override today's date (YYYY-MM-DD) — for operator replay, not production",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the message that WOULD be sent; remove no labels, send nothing",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    if args.today is not None and not ISO_DATE_RE.match(args.today):
        # `date.fromisoformat` also accepts 20260901 and 2026-W01-1, which would
        # silently replay a different day than the operator typed.
        parser.error(f"--today must be YYYY-MM-DD, got {args.today!r}")
    today = dt.date.fromisoformat(args.today) if args.today else utc_today()

    return run(
        github=GhCliGitHub(args.repo),
        # Built lazily: --dry-run must work on a machine with no Telegram creds.
        notify=(lambda _text: True) if args.dry_run else default_notify(),
        today=today,
        label=args.label,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    raise SystemExit(main())
