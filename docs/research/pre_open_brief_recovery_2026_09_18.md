# Pre-open brief names, recovered from the build journal

**Status:** LOCKED (evidence record + pre-registration, 2026-09-18)
**Issue:** #1479 · **Related:** #1482 (publish once), #1489 (ML labels), #1493 (dates before the journal)

## Why this file exists

`/edge` and the ML label store measure the brief for a date as it is STORED. On 17 as-of dates
that is not the list that existed at the arrival open: until #1482 the thematic build rewrote a
date's brief up to six times a day, including after the open. `thematic_brief_publication_history_2026_09_16.md`
records WHICH dates, from the build journal, but only as counts.

Those dates are 186 of 1104 rows in `~/.alphalens/population_ladders` (16.8%), and some changed
completely: 2026-08-02 had ten names before the open and holds one today (`KLIC`), which was not
on the pre-open list at all.

The journal also prints, for every run, the score-stage table of candidates BY NAME. This file
freezes those names before the journal rotates them away (it is size-rotated at 1.0 GB and its
oldest entry is 2026-05-25), and pre-registers what the recompute may do with them.

## Files

| File | Content |
|---|---|
| `pre_open_brief_names_2026_09_18_journal.log.gz` | The frozen journal lines (11894), read 2026-09-18. `sha256` of the uncompressed text: `a3578fb4f725e4389708bf9038e49b9cd7ad73eba9c72d0029a820a6139f7c3d` |
| `pre_open_brief_names_2026_09_18.csv` | One row per recovered name (172), derived from the file above by the committed script |
| `apps/alphalens-research/scripts/recover_pre_open_brief_names.py` | The script. Re-running it over the extract must reproduce the CSV byte for byte; a test asserts it |

Extraction command (read-only, on the VPS). `journalctl` reads `--since` / `--until` in LOCAL
time while `--utc` only changes the OUTPUT format, so the window is stated in local time:

```bash
journalctl --user -u alphalens-thematic-build.service --no-pager --utc -o short-iso \
  --since "2026-05-25" --until "2026-09-18" | awk '
  /ticker   industry/ { intable=1; print; next }
  intable && /thematic brief/ { intable=0; print; next }
  intable && /Generating briefs for/ { intable=0; print; next }
  intable { print; next }
  /generate_briefs [0-9-]+: wrote [0-9]+ briefs/ { print; next }
  /Generating briefs for [0-9]+ scored rows/ { print; next }
  /Wrote [0-9]+ candidate rows/ { print; next }
'
```

## How a name list is recovered

For one as-of date, in order:

1. the deadline is the arrival open, `publication.deadline_utc(asof)`;
2. of that date's `generate_briefs <asof>: wrote N briefs` lines, the LAST one strictly before the
   deadline names the list that was stored at the open;
3. the score table printed by the SAME pid holds the names; that pid must belong to exactly one
   run in the extract, or the date is refused rather than guessed;
4. the names are the unique tickers of the table, in the order printed;
5. the status is `exact` when their count equals `N`, else `partial`; a date with no pre-open
   write is `no_pre_open_list` and yields no names.

Why the unique names of a SCORED table are the BRIEF's names: `generate_briefs` keeps the rows with
`verified=True`, deduplicates a ticker that appears under two themes, and then merges the LLM prose
with `how="left"`, so a failed prose call empties columns but never drops a name. The equality in
step 5 is the check that this held on the date in question.

## Result

| asof | arrival open (UTC) | last pre-open write | briefs | scored | names | status |
|---|---|---|---|---|---|---|
| 2026-05-28 | 05-29 13:30 | 05-29 07:11:43Z | 18 | 26 | 17 | **partial** |
| 2026-05-31 | 06-01 13:30 | 06-01 12:55:41Z | 16 | 19 | 16 | exact |
| 2026-06-01 | 06-02 13:30 | 06-02 12:49:32Z | 10 | 10 | 10 | exact |
| 2026-06-02 | 06-03 13:30 | 06-03 12:53:26Z | 17 | 17 | 17 | exact |
| 2026-06-03 | 06-04 13:30 | 06-04 12:57:37Z | 14 | 20 | 14 | exact |
| 2026-06-04 | 06-05 13:30 | 06-05 12:49:24Z | 4 | 4 | 4 | exact |
| 2026-06-07 | 06-08 13:30 | 06-08 12:56:18Z | 12 | 18 | 12 | exact |
| 2026-06-08 | 06-09 13:30 | 06-09 12:55:43Z | 10 | 13 | 10 | exact |
| 2026-06-09 | 06-10 13:30 | — | — | — | 0 | **no_pre_open_list** |
| 2026-06-10 | 06-11 13:30 | 06-11 12:49:25Z | 5 | 5 | 5 | exact |
| 2026-06-11 | 06-12 13:30 | 06-12 12:57:23Z | 11 | 11 | 11 | exact |
| 2026-06-14 | 06-15 13:30 | 06-15 13:26:19Z | 16 | 19 | 16 | exact |
| 2026-06-15 | 06-16 13:30 | 06-16 13:17:22Z | 16 | 16 | 16 | exact |
| 2026-08-02 | 08-03 13:30 | 08-03 09:02:40Z | 10 | 10 | 10 | exact |
| 2026-08-18 | 08-19 13:30 | 08-19 12:54:52Z | 2 | 2 | 2 | exact |
| 2026-08-19 | 08-20 13:30 | 08-20 13:03:13Z | 12 | 12 | 12 | exact |

14 dates recovered exactly, 155 names. 2026-05-28 is partial and 2026-06-09 has no list.
2026-05-19, the 17th affected date, is older than the journal; it had no list before the open
either (its candidates were written at 17:13Z against a 13:30Z open, see #1493).

How different the stored lists are: of the 172 recovered names, 75 are also in the brief stored
today for the same date. Two dates share no name with what is stored (2026-08-02 and 2026-08-18).

## What was checked

- **Every recovered ticker has a daily bar on its arrival session** in `grouped_daily_history`
  (checked on the VPS on 2026-09-18, zero misses). A parser that had picked up log noise
  (`RSI`, `INFO`, `HTTP`) or a cut-off symbol would fail here.
- **The table is read twice**, once on the whitespace between columns and once on the printer's
  fixed 8-character first column, and a date whose readings disagree is refused.
- **Three dates were read by eye** out of the frozen log text and pinned as test data, so a parser
  change cannot quietly rewrite its own ground truth.
- **No pid is reused**: over the whole journal window, 671 pids, none appearing on two days, none
  carrying two as-of dates, none printing two score tables.
- **No run wrote a brief across the open**: the last pre-open writes finished 3 to 40 minutes
  before it, so the June-era non-atomic `to_parquet` cannot have left a torn file at the open.

## Limits

- **`_print_score_preview` prints at most 25 rows** (`enriched.head(25)`). That is why 2026-05-28,
  whose run scored 26 rows, is `partial`: one of its 18 names was never printed. The missing name
  cannot be identified from the journal.
- **The names are a set, not a brief.** The THEME of each name is not in the table (it prints the
  industry), and neither are the trade levels or the prose. A consumer that needs those cannot be
  served from this record.
- **The last write before the open is the list at the open, not every list of that morning.** An
  earlier list replaced before the open is not recorded, which is correct for the question asked.
- **The journal is size-rotated.** Dates before 2026-05-25 have already gone; a future rotation
  will take more. This file is the copy that survives that.

## Pre-registration for the recompute (step 2)

Fixed here, before any outcome is recomputed, so that the recompute cannot be steered by what it
produces:

- Only the `exact` dates are rebuilt. `partial` (2026-05-28) and `no_pre_open_list` (2026-06-09,
  2026-05-19) are excluded from the rebuilt population and stay excluded in the summaries.
- For a rebuilt date the population is exactly the recovered name set, in the recovered order.
  A name stored today but absent from that set is dropped; a name in the set but missing today is
  added.
- Outcomes come from the unchanged code paths on the same price sources and windows as today:
  `population_ladder_monitor.replay_population_ladders` for `/edge`, and
  `selection_label.enrich_selection_labels` for the labels. No new fallback is introduced for a
  name whose prices are missing; it takes the status the existing code gives it.
- The labels for rebuilt dates recompute under a new `SEL_LABEL_VERSION`, because the population
  rule changed for them.
- The recompute reports, per date, how many rows were added, dropped and kept. It does not report,
  and must not be steered by, whether the rebuilt numbers are better or worse.
