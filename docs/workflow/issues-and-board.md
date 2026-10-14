# Issues and the board (Projects v2)

Every task is a GitHub ISSUE. The board at `github.com/users/kamilpajak/projects/1` is a VIEW over
issues, never a second registry — the wake timer reads the issue BODY, not a board field, so
deleting the board breaks nothing.

## Creating a task

- Create the issue: `gh issue create --repo kamilpajak/AlphaLens ...`. Always pass `--repo` — a
  sibling worktree makes the ambient repo context ambiguous.
- Add it to the board by hand; new issues are NOT auto-added:
  `gh project item-add 1 --owner kamilpajak --url <issue-url>`.
- A card from `item-add` starts with NO status, not `Todo`. Set it, mirroring the label.
- Status options: `Todo` / `In progress` / `Waiting: decision` / `Waiting: data` / `Blocked` /
  `Done`.

## State labels

Default is NO label, meaning actionable now. Over-labelling trains the reader to ignore labels.

- `waiting:decision` — blocked on a judgement call by the owner. The issue MUST NAME the decision,
  with options and costs, or the label is noise. The 2026-08-23 triage refuted 5 of 12 waiting
  labels, one of them for naming no decision at all.
- `waiting:data` — paused until a date or a data/sample threshold. May carry a `Wake:` line
  (below). With no date, state the unblocking CONDITION in the body instead.
- `blocked` — blocked on something external: a vendor, an upstream, another person.

## Area labels

A SECOND axis beside the state labels, not a replacement. Every open issue carries EXACTLY ONE
area label plus whatever state label applies (`gh issue edit --add-label` adds; it never drops
`waiting:*`). The label names the PROGRAM that owns the issue's semantics, not the repo holding
the file today.

- `area:bracket-keeper` — the executor under `brokers/**` AND its `alphalens broker` CLI surface,
  even though `alphalens_cli/commands/broker.py` stays in AlphaLens through Stage 1 of the split
  (`docs/research/bracket_keeper_repo_split_stage1_design_2026_08_02.md` §5; #1424 is the
  precedent).
- `area:contract` — the shared `broker_contract` leaf and the tools built on it. intent-replay
  (#1571) is a THIRD codebase, neither AlphaLens nor the keeper.
- `area:alphalens` — everything else: thematic, feedback and `/edge`, Django/SPA, research
  scripts, VPS infra and monitoring, docs.

Rules: a cross-cutting issue gets ONE primary label where the fix lands, plus a comment starting
`Cross-cutting:` that names the other side — never two area labels, because the split PR will move
issues by label filter. An issue whose follow-ups fall in two programs is split first
(#1368 -> #1583). An operator or account decision with no code in either program gets NO area
label (#1359).

## The `Wake:` line

A `waiting:data` issue that should resurface on a date carries `Wake: YYYY-MM-DD` on its own line
in the body. `alphalens-issue-wake.{service,timer}` drops the label and sends ONE Telegram
message.

- **The timer fires daily at 05:30 UTC** (`OnCalendar=*-*-* 05:30:00 UTC` in
  `deploy/systemd/alphalens-issue-wake.timer`). That is 07:30 only while CEST is in force; in CET
  it is 06:30. The due test is `date <= today` against `utc_today()`
  (`apps/alphalens-research/scripts/wake_due_issues.py:451`), so the comparison is UTC too.
- State the date only when the issue's own facts give it, or plain arithmetic derives it. NEVER
  estimate — invented dates fire notifications that teach the owner to ignore notifications.
- One `Wake:` line per issue: the FIRST one wins, not the first PARSEABLE one
  (`wake_due_issues.py:226`).
- A decorated key is accepted (`- Wake:`, `**Wake:**`, `> Wake:`, lowercase). A `Wake:` line
  inside a fenced block is REPORTED as malformed and the label is left on — a bad date is never
  guessed at.
- A wake threshold on a SAMPLE SIZE has no watcher: the timer only reads dates. Give it a date or
  accept that nothing will wake it.
- Mirror the date into the board's `Wake` field for the Schedule view; that field is a sort-key
  copy (confirmed present in `gh project field-list 1 --owner kamilpajak`, 2026-10-14).
- The script sends BEFORE unlabelling, so a failed send leaves the labels on and the next run
  repeats the whole set. Read the verdict from the journal, not from the absence of a message:
  `journalctl --user -u alphalens-issue-wake.service --since today`.
- Dry run: `.venv/bin/python apps/alphalens-research/scripts/wake_due_issues.py --dry-run` prints
  the message it would send and mutates nothing.

## Moving cards

The board mirrors reality at EVERY transition, not only at creation.

- Starting work -> `In progress`. Adding or removing a waiting label -> the matching `Waiting:`
  column.
- ONLY close -> `Done` is automated, by the `Item closed` workflow enabled in the project UI on
  2026-08-24 (before that, #1093 sat in `In progress` after its merge). VERIFY it fired after each
  close.
- Every other move is a manual `gh project item-edit`. Re-derive the field and option IDs rather
  than pasting them: `gh project field-list 1 --owner kamilpajak --format json`.
- Run board writes from the Mac. The VPS `GH_TOKEN` is a fine-grained PAT that cannot see the
  board (`user.projectV2` refuses, measured 2026-08-23 and not re-checked since); the `project`
  scope sits on the Mac's `gh` token.

## Closing

- PRs carry `Closes #N`. Grep the whole PR body before merging — `fixes`/`closes #N` anywhere in
  any sentence closes that issue.
- Obsolete work closes with `--reason "not planned"` plus a comment naming the ADR or decision
  that obsoleted it (pattern: #422 under ADR 0012).
- No epics for one-PR scope. An epic is for genuinely multi-PR work with sub-issues; a deferred
  follow-up carved out of a review gets its own small issue with a `Wake:` line, not a bullet in a
  comment.
- Prefer FIXING to filing. File an issue only when someone has to decide, or when the work must
  wait.

## Board API gotchas

- **`gh project item-list` defaults to `--limit 30`.** Measured 2026-10-14: the default page
  returned 30 rows against 282 items on the board. A card you cannot find is usually below the
  page, not missing — pass `--limit` above the item count before concluding anything.
- **Aggregates lag your own writes.** `totalCount` read 39 -> 38 -> 36 during one burst against a
  true 36 (2026-08-23). Verify after the writes settle, never mid-burst. On 2026-10-14, with
  nothing in flight, `item-list --limit 1000` and `gh project view`'s `items.totalCount` agreed at
  282.
- **The built-in `Auto-add sub-issues` workflow drags CLOSED sub-issues onto the board with no
  Status.** It cannot be disabled via API; remove those cards by hand.
- **Views cannot be created via API.** The Status board and the Wake table are click-only in the
  UI.
- Field and option IDs, plus the #1314 history (the VPS token lacked `Issues: write`, so label
  removal never worked from 2026-08-25 until the token was fixed; issue closed 2026-09-06):
  machine-local auto-memory note `ref_issue_wake_260823`, which is not in this repo.
