"""The wake-date notifier: parser, triage, and the one-message run loop.

The job's whole value is that SILENCE MEANS "nothing due". Every failure mode
below therefore has to be either loud (a message, a non-zero exit) or provably
impossible — a Wake line the parser cannot read must never degrade into "no
issue was due today", because that is indistinguishable from a healthy quiet
day and nobody would ever look.

Nothing here touches the network: the GitHub reads/writes and the Telegram send
are ports, and every test passes a fake.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import unittest

from scripts import wake_due_issues as wake

TODAY = dt.date(2026, 8, 23)


def _issue(number: int, body: str, *, title: str = "Some issue") -> wake.Issue:
    return wake.Issue(
        number=number,
        title=title,
        url=f"https://github.com/kamilpajak/AlphaLens/issues/{number}",
        body=body,
    )


class TestParseWakeDate(unittest.TestCase):
    """``parse_wake_date`` is pure: body text in, a classified result out."""

    def test_body_without_a_wake_line_is_missing_not_malformed(self) -> None:
        parsed = wake.parse_wake_date("Blocked on the October options read.\n")

        self.assertEqual(parsed.status, wake.WakeStatus.MISSING)
        self.assertIsNone(parsed.date)

    def test_wake_line_yields_the_date(self) -> None:
        parsed = wake.parse_wake_date("Blocked.\n\nWake: 2026-09-01\n")

        self.assertEqual(parsed.status, wake.WakeStatus.OK)
        self.assertEqual(parsed.date, dt.date(2026, 9, 1))

    def test_leading_and_trailing_whitespace_is_tolerated(self) -> None:
        # GitHub bodies arrive with CRLF and indentation from list items.
        parsed = wake.parse_wake_date("intro\r\n   Wake:   2026-09-01  \r\n")

        self.assertEqual(parsed.status, wake.WakeStatus.OK)
        self.assertEqual(parsed.date, dt.date(2026, 9, 1))

    def test_unparseable_value_is_reported_as_malformed_with_its_raw_text(self) -> None:
        # The whole point: never invent a date, never skip silently. The raw
        # text has to survive so the Telegram message can quote it back.
        parsed = wake.parse_wake_date("Wake: soon\n")

        self.assertEqual(parsed.status, wake.WakeStatus.MALFORMED)
        self.assertIsNone(parsed.date)
        self.assertEqual(parsed.raw, "soon")

    def test_impossible_calendar_date_is_malformed_not_a_crash(self) -> None:
        # Shape-correct, calendar-wrong. A regex-only check would accept it and
        # then blow up in fromisoformat halfway through the run.
        parsed = wake.parse_wake_date("Wake: 2026-13-45\n")

        self.assertEqual(parsed.status, wake.WakeStatus.MALFORMED)
        self.assertEqual(parsed.raw, "2026-13-45")

    def test_non_padded_date_is_malformed(self) -> None:
        # The contract is exactly YYYY-MM-DD, so 2026-9-1 is a typo to report,
        # not a date to accept in a format the docs never promised.
        parsed = wake.parse_wake_date("Wake: 2026-9-1\n")

        self.assertEqual(parsed.status, wake.WakeStatus.MALFORMED)
        self.assertEqual(parsed.raw, "2026-9-1")

    def test_compact_iso_form_is_malformed(self) -> None:
        # date.fromisoformat ALSO accepts the basic form (20260901). Leaning on
        # it alone would silently honour a spelling the convention never
        # promised, so the YYYY-MM-DD shape is checked before parsing.
        parsed = wake.parse_wake_date("Wake: 20260901\n")

        self.assertEqual(parsed.status, wake.WakeStatus.MALFORMED)
        self.assertEqual(parsed.raw, "20260901")

    def test_iso_week_date_is_malformed_rather_than_a_different_date(self) -> None:
        # The sharpest case for the shape check, and the reason it exists:
        # date.fromisoformat("2026-W01-1") parses CLEANLY to 2025-12-29 — a
        # date in a DIFFERENT YEAR from the one the text appears to say. That
        # is the invented-date failure exactly, arriving through a valid parse
        # rather than an error, so only an up-front shape check can stop it.
        parsed = wake.parse_wake_date("Wake: 2026-W01-1\n")

        self.assertEqual(parsed.status, wake.WakeStatus.MALFORMED)
        self.assertEqual(parsed.raw, "2026-W01-1")

    def test_first_wake_line_wins_when_two_are_present(self) -> None:
        # Pinned behaviour: the FIRST line is authoritative. An issue edited
        # over time appends; taking the last would let a stale "Wake:" pasted
        # into a comment-style edit move the date forward or backward without
        # anyone deciding to.
        parsed = wake.parse_wake_date("Wake: 2026-09-01\nmore text\nWake: 2026-12-31\n")

        self.assertEqual(parsed.date, dt.date(2026, 9, 1))

    def test_first_wake_line_wins_even_when_it_is_the_malformed_one(self) -> None:
        # The FIRST-line rule is not "first PARSEABLE line" — that would let a
        # typo be silently repaired by a later line, which is exactly the
        # invented-date failure the job must not have.
        parsed = wake.parse_wake_date("Wake: whenever\nWake: 2026-09-01\n")

        self.assertEqual(parsed.status, wake.WakeStatus.MALFORMED)
        self.assertEqual(parsed.raw, "whenever")

    def test_wake_inside_a_fenced_code_block_is_ignored(self) -> None:
        # DECISION (pinned): fenced blocks are documentation, not instruction.
        # Issue templates paste `Wake: YYYY-MM-DD` inside a fence to show the
        # format; honouring that would wake every issue carrying the template.
        body = "How to use:\n\n```\nWake: 2026-09-01\n```\n\nStill blocked.\n"

        parsed = wake.parse_wake_date(body)

        self.assertEqual(parsed.status, wake.WakeStatus.MISSING)

    def test_wake_outside_a_fence_is_read_even_when_a_fence_is_present(self) -> None:
        # The other half of the fence rule — stripping must not swallow the
        # real line that follows the block.
        body = "```\nWake: 2026-01-01\n```\n\nWake: 2026-09-01\n"

        parsed = wake.parse_wake_date(body)

        self.assertEqual(parsed.date, dt.date(2026, 9, 1))

    def test_tilde_fences_are_stripped_too(self) -> None:
        body = "~~~text\nWake: 2026-09-01\n~~~\n"

        self.assertEqual(parse_status(body), wake.WakeStatus.MISSING)

    def test_unterminated_fence_swallows_the_rest_of_the_body(self) -> None:
        # GitHub renders an unterminated fence as code to end-of-body, so the
        # parser agrees with what the author actually sees. Ignoring is the
        # conservative arm: the issue stays labelled and keeps waiting.
        body = "```\nWake: 2026-09-01\n"

        self.assertEqual(parse_status(body), wake.WakeStatus.MISSING)

    def test_inline_mention_is_not_a_wake_line(self) -> None:
        # Only a whole line counts. Prose that mentions the convention
        # ("set Wake: 2026-09-01 when you know") must not arm the timer.
        body = "Please set Wake: 2026-09-01 once the vendor confirms.\n"

        self.assertEqual(parse_status(body), wake.WakeStatus.MISSING)

    def test_empty_body_is_missing(self) -> None:
        # gh returns "" (not null) for an issue opened with no description.
        self.assertEqual(parse_status(""), wake.WakeStatus.MISSING)


def parse_status(body: str) -> wake.WakeStatus:
    return wake.parse_wake_date(body).status


class TestDueClassification(unittest.TestCase):
    """today-or-earlier is due; strictly-later is not."""

    def test_past_date_is_due(self) -> None:
        self.assertTrue(wake.is_due(dt.date(2026, 8, 1), TODAY))

    def test_today_is_due(self) -> None:
        # Boundary: "today or earlier" includes today. Off-by-one here delays
        # every wake by a day, silently.
        self.assertTrue(wake.is_due(TODAY, TODAY))

    def test_tomorrow_is_not_due(self) -> None:
        self.assertFalse(wake.is_due(dt.date(2026, 8, 24), TODAY))


class TestTriage(unittest.TestCase):
    """The whole issue list, split into the buckets the run loop acts on."""

    def test_splits_due_waiting_malformed_and_missing(self) -> None:
        issues = [
            _issue(1, "Wake: 2026-08-01\n"),  # past -> due
            _issue(2, f"Wake: {TODAY.isoformat()}\n"),  # today -> due
            _issue(3, "Wake: 2026-12-31\n"),  # future -> still waiting
            _issue(4, "Wake: someday\n"),  # malformed -> reported
            _issue(5, "no wake line here\n"),  # missing -> untouched
        ]

        triage = wake.triage_issues(issues, TODAY)

        self.assertEqual([i.number for i, _ in triage.due], [1, 2])
        self.assertEqual([i.number for i in triage.waiting], [3])
        self.assertEqual([i.number for i, _ in triage.malformed], [4])
        self.assertEqual([i.number for i in triage.no_wake_line], [5])

    def test_due_carries_the_parsed_date_for_the_message(self) -> None:
        triage = wake.triage_issues([_issue(1, "Wake: 2026-08-01\n")], TODAY)

        self.assertEqual(triage.due[0][1], dt.date(2026, 8, 1))

    def test_malformed_carries_the_raw_text(self) -> None:
        triage = wake.triage_issues([_issue(4, "Wake: q3-ish\n")], TODAY)

        self.assertEqual(triage.malformed[0][1], "q3-ish")

    def test_actionable_is_true_only_when_something_must_be_said(self) -> None:
        quiet = wake.triage_issues([_issue(3, "Wake: 2026-12-31\n")], TODAY)
        loud = wake.triage_issues([_issue(4, "Wake: nope\n")], TODAY)

        self.assertFalse(quiet.actionable)
        self.assertTrue(loud.actionable)


class FakeGitHub:
    """In-memory stand-in for the gh CLI. Records every mutation."""

    def __init__(self, issues: list[wake.Issue], *, fail_on: set[int] | None = None):
        self._issues = issues
        self._fail_on = fail_on or set()
        self.removed: list[tuple[int, str]] = []
        self.listed_labels: list[str] = []

    def list_labelled_issues(self, label: str) -> list[wake.Issue]:
        self.listed_labels.append(label)
        return list(self._issues)

    def remove_label(self, number: int, label: str) -> None:
        if number in self._fail_on:
            raise RuntimeError(f"gh failed on #{number}")
        self.removed.append((number, label))


class RecordingNotifier:
    def __init__(self, *, ok: bool = True):
        self.ok = ok
        self.messages: list[str] = []

    def __call__(self, text: str) -> bool:
        self.messages.append(text)
        return self.ok


class TestRun(unittest.TestCase):
    def test_nothing_due_sends_nothing_and_exits_zero(self) -> None:
        # The load-bearing contract: silence == nothing due.
        github = FakeGitHub([_issue(3, "Wake: 2026-12-31\n"), _issue(5, "no line\n")])
        notify = RecordingNotifier()

        rc = wake.run(github=github, notify=notify, today=TODAY)

        self.assertEqual(rc, 0)
        self.assertEqual(notify.messages, [])
        self.assertEqual(github.removed, [])

    def test_empty_issue_list_sends_nothing_and_exits_zero(self) -> None:
        github = FakeGitHub([])
        notify = RecordingNotifier()

        self.assertEqual(wake.run(github=github, notify=notify, today=TODAY), 0)
        self.assertEqual(notify.messages, [])

    def test_due_issues_are_unlabelled_and_announced_in_one_message(self) -> None:
        github = FakeGitHub(
            [
                _issue(1, "Wake: 2026-08-01\n", title="Options telemetry read"),
                _issue(2, f"Wake: {TODAY.isoformat()}\n", title="ML floor-25 re-measure"),
                _issue(3, "Wake: 2026-12-31\n"),
            ]
        )
        notify = RecordingNotifier()

        rc = wake.run(github=github, notify=notify, today=TODAY)

        self.assertEqual(rc, 0)
        self.assertEqual(github.removed, [(1, wake.WAITING_LABEL), (2, wake.WAITING_LABEL)])
        self.assertEqual(len(notify.messages), 1)
        body = notify.messages[0]
        self.assertIn("#1", body)
        self.assertIn("Options telemetry read", body)
        self.assertIn("#2", body)
        self.assertNotIn("#3", body)

    def test_message_is_sent_before_the_label_is_removed(self) -> None:
        # Ordering is deliberate. If the send fails after the label is gone the
        # wake is lost for good; sending first makes the run at-least-once —
        # a failed send leaves the label on and tomorrow retries.
        order: list[str] = []
        github = FakeGitHub([_issue(1, "Wake: 2026-08-01\n")])
        original_remove = github.remove_label

        def _track_remove(number: int, label: str) -> None:
            order.append("remove")
            original_remove(number, label)

        github.remove_label = _track_remove  # type: ignore[method-assign]

        def _track_notify(text: str) -> bool:
            order.append("notify")
            return True

        wake.run(github=github, notify=_track_notify, today=TODAY)

        self.assertEqual(order, ["notify", "remove"])

    def test_failed_send_leaves_the_label_on_and_exits_non_zero(self) -> None:
        github = FakeGitHub([_issue(1, "Wake: 2026-08-01\n")])
        notify = RecordingNotifier(ok=False)

        rc = wake.run(github=github, notify=notify, today=TODAY)

        self.assertNotEqual(rc, 0)
        self.assertEqual(github.removed, [])

    def test_malformed_wake_line_is_reported_and_never_unlabelled(self) -> None:
        # A typo must be LOUD (it is a human error that stalls the issue
        # forever) but must not itself wake anything — we do not know when.
        github = FakeGitHub([_issue(7, "Wake: after the audit\n", title="Bad line")])
        notify = RecordingNotifier()

        rc = wake.run(github=github, notify=notify, today=TODAY)

        self.assertEqual(rc, 0)
        self.assertEqual(github.removed, [])
        self.assertEqual(len(notify.messages), 1)
        self.assertIn("#7", notify.messages[0])
        self.assertIn("after the audit", notify.messages[0])

    def test_one_failed_label_removal_does_not_stop_the_rest(self) -> None:
        github = FakeGitHub(
            [_issue(1, "Wake: 2026-08-01\n"), _issue(2, "Wake: 2026-08-02\n")],
            fail_on={1},
        )
        notify = RecordingNotifier()

        rc = wake.run(github=github, notify=notify, today=TODAY)

        self.assertNotEqual(rc, 0)  # loud: the operator must fix #1 by hand
        self.assertEqual(github.removed, [(2, wake.WAITING_LABEL)])

    def test_dry_run_mutates_nothing_and_sends_nothing(self) -> None:
        github = FakeGitHub([_issue(1, "Wake: 2026-08-01\n")])
        notify = RecordingNotifier()

        rc = wake.run(github=github, notify=notify, today=TODAY, dry_run=True)

        self.assertEqual(rc, 0)
        self.assertEqual(github.removed, [])
        self.assertEqual(notify.messages, [])

    def test_the_waiting_label_is_what_gets_queried(self) -> None:
        github = FakeGitHub([])

        wake.run(github=github, notify=RecordingNotifier(), today=TODAY)

        self.assertEqual(github.listed_labels, ["waiting:data"])


class TestMessage(unittest.TestCase):
    def test_message_quotes_the_wake_date_and_the_url(self) -> None:
        triage = wake.triage_issues([_issue(1, "Wake: 2026-08-01\n", title="T")], TODAY)

        text = wake.build_message(triage, TODAY)

        self.assertIn("2026-08-01", text)
        self.assertIn("https://github.com/kamilpajak/AlphaLens/issues/1", text)

    def test_message_separates_due_from_malformed(self) -> None:
        triage = wake.triage_issues(
            [_issue(1, "Wake: 2026-08-01\n"), _issue(2, "Wake: dunno\n")], TODAY
        )

        text = wake.build_message(triage, TODAY)

        self.assertIn("due", text.lower())
        self.assertIn("unreadable", text.lower())


class FakeRunner:
    """Stands in for ``subprocess.run``. Records argv; returns canned output."""

    def __init__(self, stdout: str = "[]", *, returncode: int = 0, stderr: str = ""):
        self._stdout = stdout
        self._returncode = returncode
        self._stderr = stderr
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(
            args=argv, returncode=self._returncode, stdout=self._stdout, stderr=self._stderr
        )


class TestGhCliAdapter(unittest.TestCase):
    """The argv the adapter builds, and what it does with gh's answer.

    A wrong flag here is the classic silent bug: gh exits non-zero, the run
    dies, and nothing distinguishes it from a network blip. These tests pin the
    argv shape without ever launching gh.
    """

    def test_list_argv_scopes_repo_state_and_label(self) -> None:
        runner = FakeRunner("[]")

        wake.GhCliGitHub("owner/repo", runner=runner).list_labelled_issues("waiting:data")

        argv = runner.calls[0]
        self.assertEqual(argv[:2], ["gh", "issue"])
        self.assertIn("list", argv)
        # --repo is always explicit: an ambiguous repo context silently targets
        # the wrong repository (project rule for every gh invocation).
        self.assertIn("--repo", argv)
        self.assertIn("owner/repo", argv)
        self.assertIn("--label", argv)
        self.assertIn("waiting:data", argv)
        # Closed issues must not be woken — they are already done.
        self.assertIn("--state", argv)
        self.assertIn("open", argv)

    def test_list_requests_the_fields_the_parser_needs(self) -> None:
        # gh omits any field not named in --json, so a dropped field would make
        # every body empty and every issue silently "no Wake line".
        runner = FakeRunner("[]")

        wake.GhCliGitHub("owner/repo", runner=runner).list_labelled_issues("waiting:data")

        argv = runner.calls[0]
        json_fields = argv[argv.index("--json") + 1].split(",")
        self.assertEqual(set(json_fields), {"number", "title", "url", "body"})

    def test_list_maps_gh_json_onto_issues(self) -> None:
        payload = json.dumps(
            [{"number": 12, "title": "T", "url": "https://x/12", "body": "Wake: 2026-09-01"}]
        )

        issues = wake.GhCliGitHub("owner/repo", runner=FakeRunner(payload)).list_labelled_issues(
            "waiting:data"
        )

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].number, 12)
        self.assertEqual(issues[0].body, "Wake: 2026-09-01")

    def test_null_body_becomes_empty_string_not_none(self) -> None:
        # An issue opened with no description. `None` here would crash the
        # regex search on a body the parser should simply call "missing".
        payload = json.dumps([{"number": 12, "title": "T", "url": "u", "body": None}])

        issues = wake.GhCliGitHub("owner/repo", runner=FakeRunner(payload)).list_labelled_issues(
            "waiting:data"
        )

        self.assertEqual(issues[0].body, "")
        self.assertEqual(wake.parse_wake_date(issues[0].body).status, wake.WakeStatus.MISSING)

    def test_remove_label_argv(self) -> None:
        runner = FakeRunner("")

        wake.GhCliGitHub("owner/repo", runner=runner).remove_label(12, "waiting:data")

        argv = runner.calls[0]
        self.assertEqual(argv[:3], ["gh", "issue", "edit"])
        self.assertIn("12", argv)
        self.assertIn("--remove-label", argv)
        self.assertIn("waiting:data", argv)

    def test_gh_failure_message_carries_stderr(self) -> None:
        # Diagnosis happens in journald hours later. CalledProcessError's repr
        # shows only the exit code, so gh's own explanation ("could not add
        # label: not found") would be lost exactly when it is needed.
        runner = FakeRunner("", returncode=1, stderr="gh: label not found")

        with self.assertRaises(wake.GitHubCommandError) as caught:
            wake.GhCliGitHub("owner/repo", runner=runner).remove_label(12, "waiting:data")

        self.assertIn("gh: label not found", str(caught.exception))


class FakeTelegramClient:
    def __init__(self, *, ok: bool = True):
        self.ok = ok
        self.calls: list[dict] = []

    def send_message(self, chat_id: str, text: str, **kwargs) -> bool:
        self.calls.append({"chat_id": chat_id, "text": text, **kwargs})
        return self.ok


class TestTelegramSink(unittest.TestCase):
    def test_sink_sends_plain_text_not_markdown(self) -> None:
        # Issue titles carry `_`, `*`, `[` freely. Under the client's default
        # parse_mode="Markdown" those return a Telegram 400 and the alert is
        # SILENTLY dropped — the one outcome this job cannot have.
        client = FakeTelegramClient()

        sink = wake.telegram_sink(client, "chat-1")
        ok = sink("hello _world_ [x]")

        self.assertTrue(ok)
        self.assertEqual(client.calls[0]["chat_id"], "chat-1")
        self.assertEqual(client.calls[0]["parse_mode"], "")

    def test_sink_propagates_failure_so_the_run_can_exit_non_zero(self) -> None:
        # send_message never raises; it returns False. A sink that dropped
        # that boolean would turn a lost alert into a silent success.
        sink = wake.telegram_sink(FakeTelegramClient(ok=False), "chat-1")

        self.assertFalse(sink("anything"))


if __name__ == "__main__":
    unittest.main()
