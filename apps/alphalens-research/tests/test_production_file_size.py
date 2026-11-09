"""Ratchet the size of production files, so the next 11 000-line module cannot grow unnoticed.

Finding 3 of the 2026-10-02 architecture audit: nothing in CI measures file or
function size. ``ruff`` ignores ``PLR0915`` / ``PLR0912`` / ``PLR0911``
repo-wide (see the root ``pyproject.toml``), SonarCloud runs with
``continue-on-error: true`` and excludes ``**/tests/**`` and both
``scripts/**``, and no workflow counts lines.

The audit also measured why re-enabling the ruff rules would NOT have caught
the file that prompted this: ``control_loop.py`` held 301 functions and only
**4** above cyclomatic complexity 15. A file of 11 130 individually compliant
functions passes every per-function rule there is. The gate that was missing
is per FILE.

This is a RATCHET, not a limit, and it is red in BOTH directions:

* a file may not grow past its baseline entry (or past ``CEILING`` when it has
  no entry) — that is what stops a big file from growing back;
* a file that has shrunk more than ``SHRINK_SLACK`` below its entry must have
  the entry lowered — that is what stops the baseline from recording a size the
  file left behind. Without this half the baseline silently stops meaning
  anything, the way a dead allowlist entry does.

Raising an entry is allowed and is meant to be visible: it is one line in the
diff, in the PR that needs the file to grow.

The corpus is the production corpus, held in place on TWO independent axes,
because either one alone leaves a way in:

* against ``sonar.sources`` — a root Sonar scans but this gate does not;
* against ``[tool.uv.workspace] members`` — a member added to the workspace
  but to neither list. The Sonar check alone cannot see this one: both lists
  stay unchanged, both stay equal, and the new member's files are never walked.

What is left out is listed by DIRECTORY (``EXCLUDED_DIRS``), not matched on the
name ``tests`` wherever it appears. Name matching would mean a large file could
hide in any new directory called ``tests``; a declared list makes an
undeclared one red. Django is the only member whose tests and migrations sit
inside its source root — everywhere else the suite is a sibling of the package.
"""

from __future__ import annotations

import unittest
from pathlib import Path

# Workspace root = repo top dir (three levels above this test file:
# tests/foo.py -> tests/ -> apps/alphalens-research/ -> apps/ -> repo)
WORKSPACE_ROOT = Path(__file__).resolve().parents[3]

# A file above this many lines must carry a baseline entry below. 1 000 is the
# point at which a module stops fitting in one reading; 17 of the 544
# production files are above it today (measured 2026-10-27). The corpus half of
# that pair was stale at 527 before #1805 and the count half was 18: splitting
# `account_cash/report.py` took one file off the list and put three small ones
# into the corpus.
CEILING = 1000

# How far below its entry a file may sit before the entry is stale. A file that
# legitimately lost a few lines should not force a baseline edit; one that lost
# a tenth of itself has been restructured, and the entry must record that.
SHRINK_SLACK = 0.10

# The production roots. Mirrors ``sonar.sources`` (minus the one non-Python
# entry) and is held to it by a test below.
PRODUCTION_ROOTS: tuple[str, ...] = (
    "apps/alphalens-broker-contract/broker_contract",
    "apps/alphalens-feedback/alphalens_feedback",
    "apps/intent-replay/intent_replay",
    "apps/alphalens-pipeline/alphalens_pipeline",
    "apps/alphalens-pipeline/alphalens_cli",
    "apps/alphalens-research/alphalens_research",
    "apps/alphalens-django",
)

# Directories inside a production root that are not production code, each with
# what it holds. Declared one by one rather than matched by name, so a large
# file cannot hide in a new directory called ``tests``: an undeclared one is red
# (``test_no_undeclared_tests_or_migrations_directory_exists``), and a declared
# one that disappears is red too.
EXCLUDED_DIRS: dict[str, str] = {
    "apps/alphalens-django/auth_cf/tests": "django app test suite",
    "apps/alphalens-django/briefs/migrations": "django schema migrations",
    "apps/alphalens-django/briefs/tests": "django app test suite",
    "apps/alphalens-django/config/tests": "django project test suite",
    "apps/alphalens-django/core/tests": "django app test suite",
    "apps/alphalens-django/edge/migrations": "django schema migrations",
    "apps/alphalens-django/edge/tests": "django app test suite",
    "apps/alphalens-django/market/tests": "django app test suite",
}

# Names that must not appear as a directory inside a production root unless
# EXCLUDED_DIRS declares that directory.
MUST_BE_DECLARED_DIR_NAMES = frozenset({"tests", "migrations"})

# What a file inside a declared excluded directory is allowed to be called.
# Without this, EXCLUDED_DIRS is a hole rather than a scope: a file of any size
# dropped into `edge/tests/` is outside the corpus and so invisible to the
# ratchet, whatever it contains. 8 432 lines live in those eight directories
# today and every one of their files matches one of these shapes, so the rule
# costs nothing now and makes an odd one out red.
ALLOWED_EXCLUDED_FILE_SHAPES = ("__init__.py", "conftest.py")
ALLOWED_EXCLUDED_FILE_PREFIXES = ("test_",)

# path (repo-relative, posix) -> the line count this file may not exceed.
#
# Most entries are files the audit left too big. An entry is also how a later
# PR records a file it deliberately took OVER ``CEILING`` -- the room has to be
# asked for in the diff. Lower one when the file shrinks; raise one
# deliberately, in the PR that needs the room.
BASELINE: dict[str, int] = {
    # Lowered from 3507 in #1689: the JSON envelope and the failure contract
    # moved to `commands/_cli_envelope.py`, which took 345 lines out of this
    # file. The old entry left it 6 lines above the stale floor (3507 x 0.9 =
    # 3156.3), so the next small deletion anywhere in the file would have gone
    # red for a reason that had nothing to do with it.
    # Raised by 6 in #1689's review: the re-export comment above that import
    # block said every test patching one of those names HERE keeps resolving,
    # which is true of resolving and false of patching. Six lines buy the
    # distinction, the mechanism (a function's globals are its DEFINING module)
    # and the measurement. Asked for rather than paid for by deleting six lines
    # of someone else's explanation, which is how a ratchet gets gamed.
    #
    # Raised by 13 in #1811: `broker_auth` is one code over five causes, and the
    # five sites that know their cause now pass the bootstrap for their own rail
    # (`broker auth` for SIM, `broker marketdata-auth` for LIVE market data).
    # Each of the five calls was a one-liner that the formatter rewraps onto
    # four lines once it carries `suggestions=`. The constants themselves live
    # in `_cli_envelope.py` beside the table they override, so this file pays
    # for the call sites only.
    #
    # Raised by 26 in #1788: `broker picks` now publishes `placement_withheld`
    # and renders it, because the drain withholds every placement while a pick
    # line cannot be read. Before this the view asked the GATED reader which
    # keys decode, so one unrelated torn line made every armed pick render
    # UNREADABLE -- a readable pick labelled unreadable. The room is 9 lines of
    # published `--help` (the old text sent the operator to re-arm, which does
    # not clear this condition), 7 for the rendering (above the empty-queue
    # early return, or a journal whose only line is unreadable renders as an
    # EMPTY queue) and 10 of comment saying which reader each surface asks.
    #
    # Raised again, by 16, in #1822: `broker status` now reports the same
    # withheld condition. #1788 left it visible only in `broker picks` and in
    # journald, so a daemon that could place nothing read green on every gauge
    # and on the one command an operator runs during an incident. The room is 5
    # lines of published `--help` (the enumeration of what status reports is a
    # claim about the command; the stale `slot /` came out in the same edit,
    # #1732 having removed that section), 7 for the rendered line beside
    # `refused` / `expired` and 4 of comment saying why the line is guarded on a
    # truthy count rather than on the field being present.
    # Raised by 65: `broker picks` now performs BOTH submission joins, because
    # the state answers "has anything reached the broker" while the wait line
    # and the window answer "has the drain finished". Only 2 lines are code (the
    # shared journal read plus the second fold); the rest is published `--help`
    # and the comments that keep it honest. The help carried three claims that
    # became false when the state changed meaning, and the adversarial review
    # found a fourth that was false when written: it sent the operator to
    # `broker reconcile` for "what rests at the venue", which reads the same
    # submissions journal and cannot see an entry-trail order either. PLACED now
    # states all three ways it is not a venue claim, because a one-line hedge is
    # what produced the wrong instruction.
    # Plus 7 for the v3 bump: `picks` re-partitioned an existing state value, so
    # the envelope version is the only thing that tells two stored payloads they
    # mean different things by the word PLACED. The room is the note recording
    # WHY, beside the v2 note it mirrors — a bare version bump with no reason
    # written next to it is the shape that makes the next one a guess.
    # Raised by 6 for #1832, measured with `wc -l` on the merged file (3319).
    # The drain now retires a placed immediate-only pick, so the published help
    # could no longer explain `window_end` and `overdue` by "that pick is never
    # retired" -- that sentence became false on the drain's side the day this
    # landed, while the fields stayed, because the view keeps calling the narrow
    # fold with no `immediate_only_keys`. The room says which of the two answers
    # the view gives and that an overdue PLACED row may now be a pick the drain
    # has finished with.
    # +20 for the graceful stop of `broker manage`: the command installs the
    # SIGTERM/SIGINT handlers (with the price-reader's off-main-thread suppress),
    # hands run_daemon the stop-aware sleep and loop check, and restores the
    # previous handlers in a nested finally, so a stream stop that raises still
    # restores them. The room is 15 lines of code, 1 blank and 6 of comment saying
    # why the handlers go in AFTER the deps are built and why ``--once`` ignores a
    # stop until its one tick ran. Counted with ``wc -l`` after ruff format (3319
    # before, 3341 after).
    # +10 more, from the review of #1864: the stream reader stop runs through
    # ``graceful_stop.run_within`` with a 10 s budget. It now runs on a real stop,
    # and its DELETE (Saxo retry ladder, 429 Retry-After up to 120 s, no DNS bound)
    # could otherwise hold the exit until SIGKILL. Counted with ``wc -l``
    # (3341 before, 3351 after).
    "apps/alphalens-pipeline/alphalens_cli/commands/broker.py": 3351,
    # Raised by 8: the score stage now also stamps the three raw broad-index
    # 5-session returns beside the market-state label. The room is 1 line of
    # lazy call and 7 of comment saying why the family is separate from the
    # label it sits next to — display data, version-free, out of
    # MARKET_STATE_COLUMNS and out of the sort — because the two calls are
    # adjacent and a reader would otherwise assume one family.
    "apps/alphalens-pipeline/alphalens_cli/commands/thematic.py": 1716,
    # Raised again, by 8, to say that the raise page's ticker fallback is
    # unreachable by construction. It sits beside the stop-PLACED page's
    # fallback, which looks identical and IS reachable and tested, so without
    # the note a reader comparing the two concludes both are exercised.
    #
    # Raised again, by 22, to put the governing plan's disaster stop in the
    # stop-raised key. A ticker exited and re-armed the same session was hidden
    # by the old position's page, which is the opposite of what
    # ``_build_managed_exits`` intends when it clears the announce entry on the
    # way out. The field's docstring says why it is not ``previous``.
    #
    # Raised again, by 13, to evict the throttle's dead dedup keys. The
    # stop-raised key is the first keyed on a continuously varying NUMBER
    # rather than an identifier, so the map grew at the trail-amend rate for
    # the daemon's whole lifetime; the comment says why dropping a key older
    # than the interval changes no decision.
    #
    # Raised again, by 4, for the ``SL`` leg label the stop-FILL page was
    # missing while the two pages where nothing moved carried it.
    #
    # Raised again, by 25, for the stop-placed key's fallback. The ref alone
    # COLLAPSED two genuinely distinct placements -- the first stop and a
    # supersede re-place at a raised level -- which is the hiding failure the
    # key exists to avoid, reintroduced by its own fallback. It is a named
    # function now, carrying the level and the placed quantity, with the
    # collapse written down so the next reader does not simplify it back.
    #
    # Raised again, by 21, for two more review findings. The stop-placed page
    # moved ahead of the supersede-cancel loop (a cancel that re-raises used to
    # cost the page for good, since the stop is already resting and no later
    # tick re-places), and the "from" level of a raise became ONE comparison
    # whose excluded case is now reported rather than relabelled -- a stop
    # driven DOWN from the announced level read as a raise off the disaster
    # stop. Both comments say what the old shape got wrong, because that is the
    # part a reader cannot recover from the code.
    #
    # Raised again, by 89, for the review of #1621 part 3's money-path finding.
    # The two pages were synchronous sink calls on protective paths, and the
    # sink's own bound is 34 s when a read hangs, 64 s when connect and read
    # each burn the 10 s window, and unbounded when DNS hangs. One page sat
    # INSIDE the serial per-action protection loop, so it delayed every later
    # uic's stop; the other sat ahead of the exits engine and the whole
    # protection pass. They are now computed where their facts are and SENT
    # after the tick's protective work: ``_queue_page`` (which carries the
    # measurement, because a bound nobody states is a bound nobody checks),
    # ``_drain_pages``, the two ``_send_*_page`` builders that let a render
    # failure be contained with the delivery failure, a ``finally`` in
    # ``run_once`` and one in the #1402 fill-wake pass, which has its own report
    # and so its own queue. Measured with ``wc -l`` after ``ruff format``.
    #
    # Raised again, by 6, for the review of #1621 part 3: the stop-raised key's
    # docstring claimed the level in the key dedups a daemon RESTART. It cannot
    # -- the throttle is built per process, so its last-sent map dies with the
    # announce map -- and the correction has to say why the page is worded as
    # STATE instead, which is the decision that replaced persisting the map.
    #
    # Raised from 7559 for #1621 part 3, the two alerts the daemon never sent.
    # The lines are the design, not decoration. STOP_RAISED needs an
    # out-parameter (``StopRaise`` plus a ``raised`` argument threaded through
    # two functions) because the pure build holds no alert sink and knows no
    # tickers, so the page belongs to the caller (``_announce_stop_raises``) --
    # the same split as the take-profit page. STOP_PLACED needs
    # ``_page_stop_placed`` for the same reason: ``PlaceStop`` carries no
    # ticker. Both need ``_page_best_effort``, and that is the half that earns
    # its lines on the money path: these two pages sit BEFORE the live-exits
    # engine and BETWEEN a confirmed place and the protection pass's remaining
    # actions, and a sink exception is not a ``BrokerError``, so uncontained it
    # would abort the tick mid-protection. The rest is the two throttle-key
    # docstrings, which say what each key discriminates -- a collapsed key
    # HIDES the second, higher raise, the page the operator most wants.
    # Measured with ``wc -l`` after ``ruff format``, not added up from the diff.
    # The previous entry's #1621-part-4 and #1525 history follows.
    #
    # Raised from 7254 for #1621 part 4. Routing the orphan-sweep, orphan and
    # protection-failure alerts through the throttle turns three one-line
    # ``deps.alert`` calls into three ``if deps.alert_throttled(...)`` blocks
    # and adds the key helper plus the comment that says what each key
    # discriminates -- which is the design, not decoration: a collapsed key
    # HIDES an orphan. 51 lines, of which the last 14 record a MEASURED
    # correction to #1621's own premise: the orphan sweep runs only on a
    # process's first tick, so only the protection arm removes a flood. A
    # comment that stops the next reader re-deriving that is worth its lines.
    # 7 more from the pre-merge review round: why the protection key's uic
    # can read None, and which test keeps that from hiding a failure.
    # The previous entry's #1525 history follows.
    #
    # Raised from 7227 for the #1525 extraction. Pulling the journaled stop
    # floor out of ``_build_managed_exits`` cannot be line-neutral -- the
    # signature, the call line and the moved block cost 13 lines -- and the
    # docstring that records why the ``>`` chain is safe without the shared
    # ratchet helper costs 18 more. The function it exists to shrink went from
    # cognitive complexity 34 to 12, which is the trade this entry pays for.
    #
    # Raised from 7254 for the #1402 instrumentation of the unprotected window
    # between an entry fill and its disaster stop. The room goes to two frozen
    # dataclasses on the probe's memo (``ArmedEntryOrder``,
    # ``EntryFillObservation`` -- the memo was a bare tuple and now has to carry
    # the crid and the uic the fold already states), the named wake rule the
    # journal record shares with it, and
    # ``_journal_entry_fill_protection_gap``. Both records are read by nothing
    # in the daemon, so most of those lines are the docstrings that say why they
    # can never touch the money path. They belong HERE and not in
    # ``entry_trails``: the probe and the protection pass are the two observers,
    # ``entry_trails`` owns the kinds and the field spellings they write.
    #
    # The number is the file's ACTUAL length, checked against ``wc -l`` rather
    # than counted from the diff: the first attempt at this entry read 7439
    # against a 7444-line file and shipped the gate red. The last 22 lines are
    # the pre-merge review round: grouping the gap observations per uic instead
    # of electing one, and the two comments that say what is NOT recorded.
    #
    # Re-counted after merging #1766 into this branch: that change and this
    # one both grew the file from 7254, so the merged length is 7554
    # rather than either branch's own number. Counted with ``wc -l`` on the
    # merged file, not added up from the two diffs.
    #
    # Raised from 7945 for the edge-triggered token-chain page. The 2026-10-23
    # incident sent 531 pages from that one site in 7.5 hours; the lines are
    # the holder, the emitter and the docstring that records why the fix is an
    # EDGE and not a throttle -- a throttle would still repeat every interval
    # and would still say nothing when the chain came back.
    #
    # Raised again from 7977 for the chain-alive gauge the review asked for.
    # Edge-triggering the page removed the only thing that contradicted a green
    # monitoring picture, and the refresh-unit job rules do NOT replace it: they
    # watch that timer's success clock and resolve on re-auth, while the LIVE
    # token provider latches dead and keeps refusing to place. The lines are the
    # metric builder, the third key in the one emit call, and the docstrings
    # recording that correction.
    #
    # Raised again, by 74, for the placements-withheld gauge (#1822). It is the
    # FOURTH key in that same emit call -- a second emit_domain_metrics call to
    # this domain would atomically erase the other three and the daemon would
    # read as dead rather than as withholding. The room is the metric name, the
    # builder, the fourth key, and `_placements_withheld`, which is the one
    # value this function READS rather than being handed: the drain throws the
    # count away and is skipped entirely under KILL or a dead chain, so a
    # forwarded count would be stale exactly when it matters. Most of the lines
    # are that docstring, and they are the measurement (the extra fold costs
    # 0.251 ms on a 75-line journal, 0.00056% of a 45 s tick) plus the reason
    # the chain gauge's "never a second read" argument does not carry over.
    # Raised by 15, all of it comment and none of it code: the entry-trail
    # watcher section claimed "STRICTLY DRY-RUN: the pass NEVER places, amends,
    # or cancels a broker order" while `_arm_native_trail` POSTs a native Saxo
    # trailing-limit entry order from inside it. The room is the corrected
    # statement, the measurement that settles it (MSFT filled 2026-10-30 at
    # 520.92, above its own 518.42 limit, which a resting buy limit cannot do),
    # and a line saying the DRY-RUN wording belongs to the pure engine in
    # `entry_trail_watcher.py` so it is not reintroduced here.
    # Raised by 49 for #1832, measured with `wc -l` on the merged file (8185),
    # not counted from the diff. Two things bought the room. The drain now hands
    # the retirement fold the keys whose entry ladder is nothing but a now
    # tranche, so a placed all-immediate pick leaves the queue instead of being
    # written `expired` / "unplaced at window end" over a filled order (ten SIM
    # keys, three LIVE picks). Most of those lines are the comment saying why
    # the LEGACY refusal above keeps the unwidened set. The rest is
    # `_an_order_exists_for`, which reads submissions.jsonl inside the expiry
    # branch only so the alert can tell "nothing was placed" from "an order is
    # already at the broker" — the second message must not invite a re-arm,
    # because the arming door does not refuse one.
    # Plus 8 after the adversarial review: the tick now takes `list(...)` of its
    # own journal snapshot instead of the injected callable's word for it. This
    # tick folds that snapshot THREE times and the placement gate is no longer
    # the first of them, so a one-shot iterator left the LAST fold empty and
    # every already-retired pick was placed AGAIN — reproduced against the real
    # drain, safe on the base commit, unsafe on the first version of this one.
    # The room is the comment saying why, because the next reader will otherwise
    # see a redundant `list()` over something already annotated `-> list`.
    # +141 for the standalone-stop compactor's account of what it DROPS: which
    # reader recovers each dropped number (the pre-compaction snapshots, not this
    # file), why no retention slot is added for one, the per-uic growth bound the
    # compactor had never stated, and ``STANDALONE_STOP_JOURNAL_KINDS``, the
    # per-kind compaction decision an AST gate holds against the writers. All of
    # it docstring and two constants, in the function a future author would widen.
    # Counted with ``wc -l`` on the file (8192 before, 8333 after) — the entry it
    # replaces read 8193, one line above the file it recorded, so the entry-to-entry
    # delta is 140 while the file grew 141.
    # +34 for #1621 point 11 on the persistent-OCO-lag page (#1855). Scope, because
    # an earlier wording over-claimed: counted with ``grep -o 'uic {'`` on
    # ``origin/main``, 27 strings name an instrument that way across this file (22)
    # and ``position_manager.py`` (5). Four are conditional fallbacks for a failed
    # ticker lookup; the other 23 say ``uic <n>`` on every send. This page is the one
    # of those 23 that fires on the NORMAL protection path — every other one fires
    # only on a failure or a degraded path — so it is the one the operator actually
    # reads. Point 11 stays open for the rest. The room buys ``_instrument_label`` and
    # its docstring (the policy: the ticker when a position names one, the uic always,
    # because the uic is the only link to the Saxo app), the uic -> ticker read of the
    # tick's ``long_positions`` at the one caller, and the paragraph in
    # ``_track_oco_lag`` saying why the new argument is required rather than defaulted
    # and why the lookup cannot miss.
    # +79 for ``_exit_result_r`` (#1621, #1856), which decides whether a stop-fill
    # page may state the trade's R at all. Roughly a quarter of it is the decision —
    # six refusals, the two local reads and the call, plus three lines at the call
    # site; the rest is the docstring, and that is what the room buys. Each refusal
    # exists because ``alphalens broker trades`` publishes a NULL r_multiple for that
    # same shape, and a page printing a number against one of those nulls is worse
    # than a page printing none: the operator cannot tell which of the two surfaces is
    # wrong. Without the list a later author reads six early returns as defensiveness
    # and deletes one. Two of the six were measured, not imagined: a COMPLETE stop
    # fill covering less than the pick is ``not_closed`` to the command, and the page
    # printed ``+0.0935R`` for shares that lost 0.1645R; and a take-profit whose
    # attribution had been reset by a later keyless plan line or a retraction made the
    # page print ``+0.3834R`` where the command published a null.
    # The two changes touch disjoint regions and their growth adds exactly. Counted
    # with ``wc -l`` on the MERGED file after ruff format: 8333 before either, 8367
    # after #1855, 8446 after both (8333 + 34 + 79).
    # +31 for #1621 on BRACKET picks: their stop ref is ``<uuid>-stop-<n>``, so the
    # stop-fill and stop-placed pages named ``uic <n>`` for every
    # ``entry_mode: "immediate"`` pick (three LIVE stop fills out of three in the week
    # of 2026-11-02). The room buys ``_stop_page_ticker``, which both pages now call:
    # the entry-trail parse, then one ``submissions.jsonl`` read for a bracket ref,
    # the fall-back to the uic when that read raises ANYTHING (the fill page is sent
    # after ``stop_filled`` is journaled, so a raise would lose it), and the docstring
    # that says why that read may sit in both callers (once per filled stop; the
    # DEFERRED send, never the protection loop). Counted with ``wc -l`` after ruff
    # format: 8446 before, 8477 after.
    # +12 more, from the review of #1862: the ``_stop_page_ticker`` docstring now says
    # what each caller does with a defect (the placed page's drain contains it, the
    # fill page's tick does not), that a BaseException propagates, and that the
    # fill-path read is SYNCHRONOUS before the protection pass, at about 4 us a record.
    # The earlier text said the read was "never the protection loop itself", which is
    # true of the placed page only. Counted with ``wc -l`` (8477 before, 8489 after).
    # +14 for the graceful stop of `broker manage`: the #1402 fill-probe wait takes
    # ``is_running`` and stops reading the book once a stop is requested (a woken
    # sleep returns at once, so without it each chunk would hit the broker until the
    # deadline), and the ``run_daemon`` docstring says that a caller stopping on a
    # signal must make the sleep and the wake Event wake too. Counted with ``wc -l``
    # after ruff format (8489 before, 8503 after).
    # +24 more, from the review of #1864: the placement drain checks the stop flag
    # before each pick (``keep_placing``, threaded from ``run_daemon`` through
    # ``run_once``), so a stop never STARTS a placement that systemd's SIGKILL at
    # 90 s could cut between its broker POST and its journal record. Main never
    # began one after the signal. Counted with ``wc -l`` (8503 before, 8527 after).
    # +111 to move the operator pages that were still sent inline BEFORE the protection
    # pass onto the ``_queue_page`` / ``_drain_pages`` path: the stop fill, the #1198
    # sibling retire (its three pages), the entry fill with its ceiling breach, and the
    # take-profit sell. Measured on ``run_once`` with a 2 s sink, a stop fill with an
    # open sibling delayed the protection pass by 4 s, an entry fill and a take-profit
    # by 2 s each. The room buys the ``try`` of ``run_once`` opening at the entry-trail
    # reconcile, the first pass that queues, with the comment that says why (the trade
    # pages and the "retired N" page follow a journal latch, so a raise in a later pass
    # would lose them for good, and only a FIRST signal lets the tick finish); most of
    # that diff is the pass block re-indented one level. The rest is the queue calls,
    # ``_queue_retire_page`` and ``_send_trade_page`` (a ``functools.partial`` target,
    # so a loop of tranches cannot share one closure), and the docstrings that now say
    # the pages wait and in which order: ``_queue_page``, ``_reconcile_one_standing_stop``
    # (why the fill event is built at the fill) and ``_announce_fired_tranches``.
    # Counted with ``wc -l`` after ruff format, on top of #1864 (8527 before, 8638 after).
    # +26 more, from the review of #1867: the four live-exits failure pages go
    # through ``_queue_alert_page`` too. One of them sat where a position is NAKED:
    # a full-close take-profit cancels the stop before its market sell, and when
    # the sell raised, the inline page held the protection pass that puts the stop
    # back for the sink's 34-64 s. Counted with ``wc -l`` (8638 before, 8664 after).
    "apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/control_loop.py": 8664,
    # +1 to correct the comment beside ``_OCO_LAG_HOLD_REASON``. It claimed a rename
    # of that literal "fails the control-loop tests loudly"; measured, renaming it to
    # "oco-lag-held" leaves all 552 control-loop tests green, because they read the
    # symbol by name. Only this package's own two test modules assert the literal.
    # Counted with ``wc -l`` (1406 before, 1407 after), after ruff format.
    "apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/position_manager.py": 1407,
    # +24 for PR #1840, to take Sonar python:S3776 off ``select_stop_move_lines``: the
    # four skip guards move into ``_stop_move_window_ts`` with the docstring that names
    # every reason a line is skipped and the note that their ORDER is load-bearing.
    # Counted with ``wc -l`` on the merged file, not from the diff — the comment on the
    # ``control_loop.py`` entry above records an earlier attempt that counted from a
    # diff and shipped this gate red.
    # +2: ``_journal_envelope_clamped`` said the boot compactor drops the record
    # and stopped there; it now names the snapshot the record then lives in.
    # +52 for ``fired_tranche_tags_by_pick`` (#1621), which attributes a fired
    # take-profit to the pick that was GOVERNING when the line was written and never
    # forgets it. The existing fold answers the executor's question — may this uic
    # still fire tier N — and so RESETS on every new plan generation. An exit page
    # asks whether THIS pick ever took a take-profit, because the command nulls such
    # a pick's whole exit family for good. Measured, the two answers disagree on a
    # keyless plan line appended after the fire, on a retraction, and on a later pick
    # taking over the uic. The docstring is most of the room: a reader who sees two
    # folds over the same lines will merge them unless it says why they differ.
    # Counted with ``wc -l`` (1409 before, 1461 after), after ruff format.
    # +65 for #1621 on BRACKET picks. Two parts. The two stop-ref patterns move here
    # from ``trades.py`` (which now imports them), so the stop pages and ``alphalens
    # broker trades`` PARSE a stop ref with one rule, plus the comment that says the
    # page's join is stricter than the command's on purpose, and
    # ``_bracket_request_from_stop_ref`` that tries the entry-trail shape first. And
    # ``_bracket_ticker`` with ``_submission_uic``: the exact join of a bracket stop
    # ref onto ``submissions.jsonl`` (one bracket in one record, same uic, the uic
    # read as int OR string, never a bool), whose docstring names every shape that
    # refuses. Counted with ``wc -l`` after ruff format: 1461 before, 1526 after.
    # +6 more, from the review of #1862: ``_submission_uic`` takes a string only in
    # the writer's own ``str(int)`` form. ``int()`` also accepted '0641', ' 641 ',
    # '+641', '6_41' and non-ASCII digits, and each joined uic 641. Counted with
    # ``wc -l`` (1526 before, 1532 after).
    "apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/stop_journal.py": 1532,
    # +188 for the first half of this file's SonarCloud complexity cleanup: eight of
    # its sixteen python:S3776 functions, cleared by naming the block each one's
    # surplus sat in. Nineteen new module-level helpers and their docstrings; the
    # logic is moved, not added, and two of the helpers REPLACE duplication rather
    # than adding any (`_absorb_instrument_fields` served two readers that carried
    # the same four lines, `_TERMINAL_BY_AUDIT_STATUS` two inline dicts that had
    # drifted apart). Counted with ``wc -l`` on the merged file, not from the diff.
    #
    # Worth stating plainly: at 3490 lines against a 1000 ceiling this is the largest
    # file in the repo and the growth is going the wrong way. It has a clear seam -
    # the design memo it implements is sectioned, and the module follows those
    # sections - but splitting it moves names that six test modules import directly,
    # so it is its own issue and not a rider on a complexity pass.
    "apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/trades.py": 3490,
    # Raised from 2416 for #1689's account-cash capability. The room goes to
    # ONE method, ``list_account_cash``: the SIM refusal, the two key
    # resolutions and the delegate. The read body itself is a separate module
    # (``saxo/account_cash_read.py``) precisely so this file stays on a
    # handful of lines, and the row shapes live in ``brokers/account_cash.py``
    # beside the capability they belong to.
    #
    # Counted with ``wc -l`` on the MERGED file, not added up from the diff:
    # the comment on the ``control_loop.py`` entry above records an earlier
    # attempt that counted from a diff and shipped the gate red.
    "apps/alphalens-pipeline/alphalens_pipeline/brokers/saxo/broker.py": 2456,
    "apps/alphalens-pipeline/alphalens_pipeline/brokers/saxo/client.py": 1141,
    "apps/alphalens-pipeline/alphalens_pipeline/data/alt_data/saxo_price_stream.py": 1395,
    "apps/alphalens-pipeline/alphalens_pipeline/feedback/ladder_chart.py": 1329,
    # +7 for the deprecation marker on layer 3 and the sentence that fences off
    # layers 1-2, so retiring the what-if layer cannot take the realized measure
    # with it (owner decision 2026-10-13, epic #1526).
    "apps/alphalens-pipeline/alphalens_pipeline/feedback/ladder_replay.py": 1917,
    "apps/alphalens-pipeline/alphalens_pipeline/feedback/population_ladder_monitor.py": 3347,
    # +3 for PR #1837, to spell the `~/.alphalens` root ONCE (Sonar python:S1192
    # flagged it four times over): one line for the `_ALPHALENS_HOME` constant and two
    # for the note that it is module-private on purpose. Measured two ways (a grep and
    # an AST walk over string constants, agreeing): 142 non-test modules carry that
    # literal and 137 build the root with `Path.home()`, sharing no constant, so a
    # repo-wide one is its own change and not a rider on this. A named constant cannot
    # be introduced for free, so this entry moves rather than the fix being dropped.
    #
    # +37 more for PR #1844, taking Sonar python:S3776 off the file's last two
    # functions. The room goes to two helpers and their docstrings, not to new logic:
    # `_tagged_tickers` (the BRK.B fold and the isinstance rationale move onto it) and
    # `_read_session_rows` (which states why the file read is a free function rather
    # than a second method — it must not be able to reach `_bars` or `_settled`).
    # Counted with `wc -l` on the merged file, not from the diff.
    "apps/alphalens-pipeline/alphalens_pipeline/feedback/selection_label.py": 1394,
    "apps/alphalens-pipeline/alphalens_pipeline/thematic/mapping/channel_assessor.py": 1267,
    # Raised by 3 in the news-lake change: one import of
    # `mapping.news_source` in the existing import block, and one
    # `news_source=news_source.news_source_token()` line at each of the two
    # `mapper_config_version` call sites. The token must record which news store
    # the catalyst was resolved against, and passing it UNCONDITIONALLY at both
    # sites is what makes the cohort key move on its own when the flag flips —
    # a conditional would be a convention to remember. The lake reader, the
    # union rule and the flag went to their own module instead of growing this
    # one; these three lines are the whole footprint here.
    "apps/alphalens-pipeline/alphalens_pipeline/thematic/mapping/orchestrator.py": 1629,
    "apps/alphalens-research/alphalens_research/eval/faithfulness.py": 1034,
    # Was EXACTLY at the 1000 ceiling, so publishing the walked sub-window
    # needed the room: the walk is the only side that knows which bars the
    # loop read. The obvious split is the take-profit ladder block, which
    # carries its own collateral (four private names imported by name in
    # ``test_walk``, and a new module owing an environment-scan row), so it is
    # its own issue rather than a rider on a three-field publishing change.
    "apps/intent-replay/intent_replay/walk.py": 1021,
}


def iter_production_files(root: Path) -> list[str]:
    """Every production ``.py`` file under ``root``, as repo-relative posix paths."""
    found: list[str] = []
    for top in PRODUCTION_ROOTS:
        base = root / top
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            rel = path.relative_to(root)
            posix = rel.as_posix()
            if any(part.startswith(".") or part == "__pycache__" for part in rel.parts):
                continue
            if any(posix.startswith(excluded + "/") for excluded in EXCLUDED_DIRS):
                continue
            found.append(posix)
    return sorted(found)


def line_count(root: Path, rel: str) -> int:
    """Newline bytes in a file — exactly what ``wc -l`` reports.

    Counted this way on purpose rather than by iterating lines: iteration counts
    a final line with no trailing newline, ``wc -l`` does not, and the baseline
    below was generated with ``wc -l``. Today the two agree on every production
    file (``end-of-file-fixer`` in pre-commit), so this only removes a way for
    them to disagree later.
    """
    with (root / rel).open("rb") as handle:
        return sum(chunk.count(b"\n") for chunk in iter(lambda: handle.read(1 << 20), b""))


def measure(root: Path) -> dict[str, int]:
    """The production corpus as path -> line count."""
    return {rel: line_count(root, rel) for rel in iter_production_files(root)}


def oddly_named_files_in_excluded_dirs(root: Path) -> list[str]:
    """Files inside a declared excluded directory that are not named like tests or migrations.

    The excluded directories are a SCOPE boundary, and this is what keeps the
    boundary honest. A file called anything else sitting in one of them is
    either production code in the wrong place, or a file that needs its own
    declaration — both of which a reader should see rather than have skipped.
    """
    odd: list[str] = []
    for excluded in sorted(EXCLUDED_DIRS):
        base = root / excluded
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            name = path.name
            if name in ALLOWED_EXCLUDED_FILE_SHAPES:
                continue
            if name.startswith(ALLOWED_EXCLUDED_FILE_PREFIXES):
                continue
            # a Django migration: 0001_initial.py and friends
            stem = name.removesuffix(".py")
            if stem[:4].isdigit() and stem[4:5] == "_":
                continue
            odd.append(path.relative_to(root).as_posix())
    return sorted(odd)


def undeclared_test_or_migration_dirs(root: Path) -> list[str]:
    """Directories named like a test or migration tree that EXCLUDED_DIRS does not declare.

    Only a directory that actually holds a module counts. See the comment
    inside: this walks the filesystem, not the git index.
    """
    undeclared: list[str] = []
    for top in PRODUCTION_ROOTS:
        base = root / top
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_dir() or path.name not in MUST_BE_DECLARED_DIR_NAMES:
                continue
            # A directory holding no .py file hides nothing, so it is not a
            # finding. This rule walks the FILESYSTEM, not the index, so it
            # also sees directories git does not track: an empty one left
            # behind by a deleted app survives locally whenever a gitignored
            # `__pycache__` keeps it alive, and reporting that as repo drift
            # is a gate that cries wolf on a developer machine while staying
            # green in CI's fresh checkout. Measured on 2026-10-04:
            # apps/alphalens-django/feedback/tests, left by #465 in June.
            if not any("__pycache__" not in py.parts for py in path.rglob("*.py")):
                continue
            rel = path.relative_to(root).as_posix()
            if rel not in EXCLUDED_DIRS:
                undeclared.append(rel)
    return sorted(undeclared)


def files_over_their_limit(
    sizes: dict[str, int], baseline: dict[str, int]
) -> list[tuple[str, int, int]]:
    """Files above their effective limit, as (path, lines, limit).

    The effective limit is the baseline entry when there is one, and
    ``CEILING`` otherwise — so a file with no entry may grow up to the ceiling
    and must then be split or given an entry.
    """
    over: list[tuple[str, int, int]] = []
    for rel, lines in sorted(sizes.items()):
        limit = baseline.get(rel, CEILING)
        if lines > limit:
            over.append((rel, lines, limit))
    return over


def stale_baseline_entries(
    sizes: dict[str, int], baseline: dict[str, int]
) -> list[tuple[str, int, int]]:
    """Entries the file has outgrown DOWNWARD, as (path, lines, entry).

    Three shapes, all of them an entry that no longer guards anything: the file
    is gone, the file has fallen back under the ceiling, or it sits more than
    ``SHRINK_SLACK`` below what the entry claims.
    """
    stale: list[tuple[str, int, int]] = []
    for rel, entry in sorted(baseline.items()):
        lines = sizes.get(rel)
        if lines is None:
            stale.append((rel, 0, entry))
        elif lines <= CEILING or lines < entry * (1 - SHRINK_SLACK):
            stale.append((rel, lines, entry))
    return stale


class ProductionFilesStayWithinTheirBaseline(unittest.TestCase):
    """The ratchet itself, measured over the real tree."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.sizes = measure(WORKSPACE_ROOT)

    def test_no_production_file_is_over_its_limit(self) -> None:
        over = files_over_their_limit(self.sizes, BASELINE)
        self.assertEqual(
            over,
            [],
            "These production files are over their limit. Split the file, or — if the "
            "change genuinely needs the room — raise its entry in BASELINE in this same "
            f"PR so the growth is visible in review:\n{self._render(over)}",
        )

    def test_no_baseline_entry_records_a_size_the_file_has_left_behind(self) -> None:
        stale = stale_baseline_entries(self.sizes, BASELINE)
        self.assertEqual(
            stale,
            [],
            "These BASELINE entries no longer describe their file. Lower each entry to "
            "the count shown (or drop it, if the file is now at or under the "
            f"{CEILING}-line ceiling), so the baseline keeps ratcheting "
            f"down:\n{self._render(stale)}",
        )

    @staticmethod
    def _render(rows: list[tuple[str, int, int]]) -> str:
        return "\n".join(
            f"  {path}: {lines} lines, entry/limit {limit}" for path, lines, limit in rows
        )


class TheCorpusIsTheProductionCorpus(unittest.TestCase):
    """What the gate measures, pinned — a gate over an empty corpus tests nothing."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.files = iter_production_files(WORKSPACE_ROOT)

    def test_the_corpus_covers_every_python_root_sonar_scans(self) -> None:
        """A new workspace member added to Sonar must also come under this gate."""
        text = (WORKSPACE_ROOT / "sonar-project.properties").read_text(encoding="utf-8")
        declared = [
            line.split("=", 1)[1].strip()
            for line in text.splitlines()
            if line.startswith("sonar.sources=")
        ]
        self.assertEqual(len(declared), 1, "expected exactly one sonar.sources line")
        self.assertFalse(
            declared[0].endswith("\\"),
            "sonar.sources continues onto the next line. A .properties continuation would "
            "leave this test comparing a truncated list, which can pass while roots are "
            "missing — keep the value on one line.",
        )
        sonar_roots = [entry.strip() for entry in declared[0].split(",")]
        python_roots = [root for root in sonar_roots if not root.startswith("apps/web/")]
        self.assertEqual(
            sorted(python_roots),
            sorted(PRODUCTION_ROOTS),
            "sonar.sources and PRODUCTION_ROOTS have drifted apart. A Python root Sonar "
            "scans but this gate does not is a member where the next oversized file can "
            "grow unseen.",
        )

    def test_the_corpus_holds_a_file_from_every_root(self) -> None:
        for root in PRODUCTION_ROOTS:
            with self.subTest(root=root):
                self.assertTrue(
                    any(rel.startswith(root + "/") for rel in self.files),
                    f"no production file found under {root} — the walk is broken or the root moved",
                )

    def test_the_corpus_excludes_every_declared_directory(self) -> None:
        leaked = [
            rel
            for rel in self.files
            if any(rel.startswith(excluded + "/") for excluded in EXCLUDED_DIRS)
        ]
        self.assertEqual(leaked, [], f"non-production files leaked into the corpus: {leaked[:10]}")

    def test_every_declared_excluded_directory_still_exists(self) -> None:
        """An entry for a directory that is gone guards nothing — drop it."""
        gone = sorted(d for d in EXCLUDED_DIRS if not (WORKSPACE_ROOT / d).is_dir())
        self.assertEqual(gone, [], f"EXCLUDED_DIRS names directories that no longer exist: {gone}")

    def test_no_undeclared_tests_or_migrations_directory_exists(self) -> None:
        """A new such directory must be declared, so a big file cannot hide in one."""
        undeclared = undeclared_test_or_migration_dirs(WORKSPACE_ROOT)
        self.assertEqual(
            sorted(undeclared),
            [],
            "These directories sit inside a production root and are named like test or "
            "migration trees, but EXCLUDED_DIRS does not declare them. Either add each "
            "one with what it holds, or — if it does hold production code — rename it, "
            f"because the name is what makes a reader skip it:\n  {sorted(undeclared)}",
        )

    def test_no_oddly_named_file_hides_inside_a_declared_excluded_directory(self) -> None:
        """Closes the hole the exclusion would otherwise be.

        A file inside `edge/tests/` is outside the corpus whatever its size, so
        without this the eight declared directories are eight places an
        oversized module can sit unseen. Found by adversarially reviewing this
        gate after it shipped: a planted 2 500-line file in `edge/tests/` passed
        every other rule here.
        """
        odd = oddly_named_files_in_excluded_dirs(WORKSPACE_ROOT)
        self.assertEqual(
            odd,
            [],
            "These files sit in a directory this gate skips, but are not named like a test, "
            "a conftest, a package marker or a migration. Either rename the file so a reader "
            "can tell what it is, move it out of the skipped directory, or widen "
            f"ALLOWED_EXCLUDED_FILE_* with the reason:\n  {odd}",
        )

    def test_every_workspace_member_contributes_a_production_root(self) -> None:
        """A member added to the workspace but to neither list would escape the gate.

        The Sonar parity test cannot see this: both lists stay unchanged and equal.
        """
        import tomllib

        config = tomllib.loads((WORKSPACE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        members = config["tool"]["uv"]["workspace"]["members"]
        self.assertTrue(members, "no uv workspace members declared")
        uncovered = sorted(
            member
            for member in members
            if not any(root == member or root.startswith(member + "/") for root in PRODUCTION_ROOTS)
        )
        self.assertEqual(
            uncovered,
            [],
            "These uv workspace members have no production root in this gate, so files "
            f"inside them are never measured: {uncovered}",
        )

    def test_every_production_root_lives_under_a_workspace_member(self) -> None:
        import tomllib

        config = tomllib.loads((WORKSPACE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        members = config["tool"]["uv"]["workspace"]["members"]
        orphaned = sorted(
            root
            for root in PRODUCTION_ROOTS
            if not any(root == member or root.startswith(member + "/") for member in members)
        )
        self.assertEqual(
            orphaned, [], f"PRODUCTION_ROOTS entries outside every workspace member: {orphaned}"
        )

    def test_the_corpus_is_the_size_the_audit_measured(self) -> None:
        """A walk that silently narrowed to a handful of files would pass every rule above."""
        self.assertGreater(len(self.files), 400, f"only {len(self.files)} production files found")

    def test_every_baseline_entry_names_a_file_in_the_corpus(self) -> None:
        missing = sorted(set(BASELINE) - set(self.files))
        self.assertEqual(
            missing,
            [],
            f"BASELINE names files that are not in the production corpus: {missing}",
        )


class TheGateCatchesWhatItIsFor(unittest.TestCase):
    """Positive controls — synthetic corpora driven through the REAL predicates.

    Without these the two rules above could rot to "nothing to check" and still
    report green, which is the failure mode the audit kept finding: a check
    with no power to produce the observation that would refute it.
    """

    def test_a_file_growing_past_its_entry_is_caught(self) -> None:
        sizes = {"a/b.py": 2001}
        self.assertEqual(files_over_their_limit(sizes, {"a/b.py": 2000}), [("a/b.py", 2001, 2000)])

    def test_a_file_exactly_at_its_entry_is_allowed(self) -> None:
        self.assertEqual(files_over_their_limit({"a/b.py": 2000}, {"a/b.py": 2000}), [])

    def test_a_new_file_over_the_ceiling_with_no_entry_is_caught(self) -> None:
        sizes = {"a/new.py": CEILING + 1}
        self.assertEqual(
            files_over_their_limit(sizes, {}),
            [("a/new.py", CEILING + 1, CEILING)],
        )

    def test_a_small_file_with_no_entry_is_allowed(self) -> None:
        self.assertEqual(files_over_their_limit({"a/small.py": CEILING}, {}), [])

    def test_a_file_that_shrank_past_the_slack_forces_its_entry_down(self) -> None:
        sizes = {"a/b.py": 1799}  # 2000 * (1 - 0.10) = 1800
        self.assertEqual(stale_baseline_entries(sizes, {"a/b.py": 2000}), [("a/b.py", 1799, 2000)])

    def test_a_file_that_shrank_within_the_slack_keeps_its_entry(self) -> None:
        self.assertEqual(stale_baseline_entries({"a/b.py": 1800}, {"a/b.py": 2000}), [])

    def test_an_entry_for_a_file_that_fell_under_the_ceiling_is_stale(self) -> None:
        sizes = {"a/b.py": CEILING}
        self.assertEqual(
            stale_baseline_entries(sizes, {"a/b.py": 1100}), [("a/b.py", CEILING, 1100)]
        )

    def test_an_entry_for_a_deleted_file_is_stale(self) -> None:
        self.assertEqual(stale_baseline_entries({}, {"a/gone.py": 1200}), [("a/gone.py", 0, 1200)])

    def test_an_undeclared_directory_is_caught_only_when_it_holds_python(self) -> None:
        """Both halves: a directory with a module is a finding, an empty leftover is not.

        The second half is the false positive this control exists for. The walk
        reads the filesystem, so a directory git does not track -- one left by a
        deleted app and kept alive by a gitignored `__pycache__` -- would
        otherwise read as repo drift on a developer machine and stay green in
        CI, which is the shape that teaches a reader to ignore the gate.
        """
        import tempfile

        root = PRODUCTION_ROOTS[0]
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / root
            (base / "holds_code" / "tests").mkdir(parents=True)
            (base / "holds_code" / "tests" / "test_thing.py").write_text(
                "x = 1\n", encoding="utf-8"
            )
            (base / "leftover" / "tests" / "__pycache__").mkdir(parents=True)
            (base / "leftover" / "tests" / "__pycache__" / "stale.py").write_text(
                "x = 1\n", encoding="utf-8"
            )

            self.assertEqual(
                undeclared_test_or_migration_dirs(Path(tmp)),
                [f"{root}/holds_code/tests"],
            )

    def test_an_oddly_named_file_in_an_excluded_directory_is_caught(self) -> None:
        import tempfile

        declared = next(iter(EXCLUDED_DIRS))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / declared).mkdir(parents=True)
            for name in ("__init__.py", "conftest.py", "test_thing.py", "0001_initial.py"):
                (root / declared / name).write_text("x = 1\n", encoding="utf-8")
            (root / declared / "zz_production_code.py").write_text("x = 1\n", encoding="utf-8")

            self.assertEqual(
                oddly_named_files_in_excluded_dirs(root),
                [f"{declared}/zz_production_code.py"],
            )

    def test_an_excluded_directory_of_only_well_named_files_is_allowed(self) -> None:
        import tempfile

        declared = next(iter(EXCLUDED_DIRS))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / declared).mkdir(parents=True)
            (root / declared / "test_only.py").write_text("x = 1\n", encoding="utf-8")
            self.assertEqual(oddly_named_files_in_excluded_dirs(root), [])

    def test_the_walk_finds_a_planted_file_and_skips_a_planted_test(self) -> None:
        """The discovery half, driven over a synthetic tree by the real function."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            declared = next(iter(EXCLUDED_DIRS))
            member = root / PRODUCTION_ROOTS[0]
            (member / "sub").mkdir(parents=True)
            (member / "sub" / "kept.py").write_text("x = 1\n", encoding="utf-8")
            (member / "sub" / "__pycache__").mkdir()
            (member / "sub" / "__pycache__" / "cached.py").write_text("x = 1\n", encoding="utf-8")
            (member / "sub" / "notpython.txt").write_text("x\n", encoding="utf-8")
            # An undeclared directory called `tests` is production until declared:
            # that is the point of the declared list, so this file IS measured.
            (member / "tests").mkdir()
            (member / "tests" / "measured.py").write_text("x = 1\n", encoding="utf-8")
            # A declared one is skipped.
            (root / declared).mkdir(parents=True)
            (root / declared / "skipped.py").write_text("x = 1\n", encoding="utf-8")

            found = iter_production_files(root)

            self.assertEqual(
                found,
                [f"{PRODUCTION_ROOTS[0]}/sub/kept.py", f"{PRODUCTION_ROOTS[0]}/tests/measured.py"],
            )
            self.assertEqual(measure(root)[f"{PRODUCTION_ROOTS[0]}/sub/kept.py"], 1)


if __name__ == "__main__":
    unittest.main()
