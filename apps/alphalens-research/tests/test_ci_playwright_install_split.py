"""A timeout-bounded retry loop in CI must not run `apt`.

The web job installs the headless browser Playwright needs. A wrapper added
2026-08-19 bounds each attempt to four minutes and retries, because the CDN
download sometimes STALLS with a dead TCP connection and no error - a stalled
attempt never triggers Playwright's own mirror fallback.

`--with-deps` makes one command do two unrelated things: an `apt-get` pass for
the OS libraries, and the CDN download. Only the download can stall, and only
the apt pass holds `/var/lib/dpkg/lock-frontend`. So a timeout that fires
during the apt pass kills `apt-get` WITHOUT releasing that lock, and every
retry then dies in about a second on

    E: Could not get lock /var/lib/dpkg/lock-frontend. It is held by process N

Measured on run 36889101798 (2026-10-01, twice in a row): attempt 1 started at
16:16:41 and was killed at 16:20:41, exactly 240s later and mid-apt; attempts
2, 3 and 4 then failed at 16:20:42, 16:20:43 and 16:20:45. The retry loop
cannot succeed once the first attempt is timed out, and the one-second attempts
are the tell. `main` passed a minute earlier only because its first attempt
finished inside the bound, so this is a slow-mirror coin flip that can block
any pull request.

The fix is to split the two concerns rather than to raise the bound: apt runs
once, unbounded (the job timeout is its backstop), and the download keeps the
wrapper it was written for. These assertions pin that split, including the
positive control that the download is still bounded and retried - dropping the
wrapper would quietly undo the 2026-08-19 decision while making this file
green.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# `playwright install-deps` is the apt half on its own; `playwright install` is
# the download half. `--with-deps` is the combined form this file forbids
# wherever an attempt is killable.
WITH_DEPS = "--with-deps"
INSTALL_DEPS = "playwright install-deps"

# Two patterns, because the two assertions below want opposite things.
#
# ANY_TIMEOUT forbids apt under every spelling of a killable command. A pattern
# that demanded the duration next (`timeout\s+\d+`) would miss
# `timeout --signal=KILL 240 ...` and `timeout -k 5 240 ...`, both of which kill
# apt exactly as the plain form does. Being broad is right here: it over-matches
# `timeout-minutes:`, which can never carry `--with-deps`.
#
# BOUNDED_CALL has to recognise a real invocation, so it keeps the duration.
ANY_TIMEOUT = re.compile(r"\btimeout\b")
BOUNDED_CALL = re.compile(r"\btimeout\s+(?:-\S+\s+)*\d+\b")


class ThePlaywrightInstallIsSplitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = CI_WORKFLOW.read_text(encoding="utf-8")
        self.lines = self.text.splitlines()

    def test_the_workflow_is_where_this_test_thinks_it_is(self) -> None:
        # Vacuity guard: every assertion below passes trivially on an empty
        # read, so a moved or renamed workflow must fail here and not silently.
        self.assertTrue(CI_WORKFLOW.is_file(), f"{CI_WORKFLOW} is gone - update this test")
        self.assertIn("playwright install", self.text)

    def test_no_killable_attempt_runs_apt(self) -> None:
        offenders = [
            f"{number}: {line.strip()}"
            for number, line in enumerate(self.lines, start=1)
            if WITH_DEPS in line and ANY_TIMEOUT.search(line)
        ]
        self.assertEqual(
            offenders,
            [],
            "a `timeout`-bounded command runs apt via --with-deps. Killing it "
            "orphans the dpkg lock and every retry then fails in a second. "
            "Install the OS dependencies in their own unbounded step with "
            f"`{INSTALL_DEPS}` and keep the bound on the download only:\n" + "\n".join(offenders),
        )

    def test_the_os_dependencies_are_installed_in_their_own_step(self) -> None:
        self.assertIn(
            INSTALL_DEPS,
            self.text,
            "the apt half has to be invoked somewhere, or the headless browser "
            "has no OS libraries on ubuntu-latest",
        )

    def test_the_download_is_still_bounded_and_retried(self) -> None:
        # The positive control. Splitting the step must not throw away the
        # stall protection the wrapper exists for; a file that only forbade
        # --with-deps would be green on a workflow that simply dropped it.
        # Comments are excluded, or the control is foolable: a workflow that
        # dropped the wrapper but left `# timeout 240 ... playwright install`
        # behind would keep this green, which is the one thing a positive
        # control must not do.
        download = [
            line
            for line in self.lines
            if BOUNDED_CALL.search(line) and not line.strip().startswith("#")
        ]
        self.assertTrue(
            any("playwright install" in line for line in download),
            "no timeout-bounded `playwright install` left: the CDN stall "
            "protection added 2026-08-19 has been dropped",
        )
        self.assertIn(
            "retrying",
            self.text,
            "the retry loop around the bounded download is gone",
        )


if __name__ == "__main__":
    unittest.main()
