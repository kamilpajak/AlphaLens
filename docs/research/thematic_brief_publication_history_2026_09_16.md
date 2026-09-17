# Thematic brief publication history (journal record)

**Status:** LOCKED (evidence record, 2026-09-16)
**Issue:** #1479 · **Related:** PR #1477 (ML label registry, §5.1 population rule)

## Why this file exists

The thematic brief for date D is rebuilt on up to six slots on D+1. Nothing in the
stores says WHEN a brief's candidate list was set: `brief_generated_at` is the last
rebuild, and the parquets are overwritten on every slot. The only record of the
timing is the systemd journal of `alphalens-thematic-build.service` on the VPS.

That journal is size-rotated (1.0 GB on 2026-09-16) and its oldest entry is
2026-05-25T13:45Z. Asof dates 2026-05-19 … 2026-05-23 already fell out of it.
This file keeps the evidence in the repo before more of it rotates away. #1479 will
stamp the publication time going forward; this record covers the past.

## Files

| File | Content |
|---|---|
| `thematic_brief_publication_history_2026_09_16_journal.log.gz` | Raw journal lines (3211), read 2026-09-16T16:05Z. `sha256` of the uncompressed text: `c44eb94551c67df4bf31e2a39232be8484c1f2b0ea0d442b90f5379c3fc02146` |
| `thematic_brief_publication_history_2026_09_16.csv` | One row per asof date (115), derived from the lines above by the script below |
| `thematic_brief_publication_history_pre_journal_2026_09_17.csv` | One row per asof date 2026-05-19 … 2026-05-23, from the brief stamps and file times on the VPS (see "Dates before the journal") |

Extraction command (read-only, on the VPS):

```bash
journalctl --user -u alphalens-thematic-build.service --no-pager --utc -o short-iso \
  | grep -E "map_themes [0-9-]+: reusing|Wrote [0-9]+ candidate rows|generate_briefs [0-9-]+: wrote [0-9]+ briefs|Starting alphalens-thematic-build|Finished alphalens-thematic-build|ailed with result"
```

## Definitions

- **Recompute:** a `Wrote N candidate rows → thematic_candidates/<asof>.parquet` line
  from a process that did NOT log `map_themes <asof>: reusing ... frozen candidate(s)`.
  A reusing run rewrites the parquet with the same list, so it is not a recompute.
- **Arrival open:** 09:30 America/New_York on `ladder_arrival_session(asof)`, in UTC.
- **`recomputed_after_open` = 1:** the LAST recompute of the date happened at or after the
  arrival open. The list that `/edge` measures is then not the list that existed before
  the open.
- `candidates_last_pre_open` / `briefs_last_pre_open`: the row count of the last write
  before the open. Empty means no write before the open.

## Result

115 asof dates; 114 have at least one recompute; **16 were recomputed after the open**:

| asof | last recompute (UTC) | arrival open (UTC) | candidates pre-open → final | briefs pre-open → final |
|---|---|---|---|---|
| 2026-05-28 | 05-29 21:23 | 05-29 13:30 | 20 → 21 | 18 → 19 |
| 2026-05-31 | 06-01 20:42 | 06-01 13:30 | 17 → 13 | 16 → 13 |
| 2026-06-01 | 06-02 20:43 | 06-02 13:30 | 10 → 6 | 10 → 6 |
| 2026-06-02 | 06-03 20:43 | 06-03 13:30 | 17 → 16 | 17 → 15 |
| 2026-06-03 | 06-04 20:44 | 06-04 13:30 | 16 → 20 | 14 → 18 |
| 2026-06-04 | 06-05 20:43 | 06-05 13:30 | 4 → 14 | 4 → 14 |
| 2026-06-07 | 06-08 20:44 | 06-08 13:30 | 14 → 11 | 12 → 10 |
| 2026-06-08 | 06-09 20:45 | 06-09 13:30 | 11 → 12 | 10 → 10 |
| 2026-06-09 | 06-10 20:44 | 06-10 13:30 | 0 → 8 | none → 7 |
| 2026-06-10 | 06-11 20:43 | 06-11 13:30 | 5 → 8 | 5 → 8 |
| 2026-06-11 | 06-12 20:42 | 06-12 13:30 | 11 → 13 | 11 → 13 |
| 2026-06-14 | 06-15 20:53 | 06-15 13:30 | 17 → 13 | 16 → 13 |
| 2026-06-15 | 06-16 16:48 | 06-16 13:30 | 16 → 12 | 16 → 11 |
| 2026-08-02 | 08-03 21:24 | 08-03 13:30 | 10 → 1 | 10 → 1 |
| 2026-08-18 | 08-19 23:13 | 08-19 13:30 | 2 → 13 | 2 → 12 |
| 2026-08-19 | 08-20 14:39 | 08-20 13:30 | 12 → 8 | 12 → 8 |

The counts are net. A list can keep its size and still swap names, so they are a
lower bound on how many names changed. No per-name history exists.

## Limits

- **Dates before 2026-05-24 are not in the journal**, and 2026-05-24 is covered only from
  2026-05-25T13:45Z. An earlier write for that date would not be visible. The older
  dates are covered by a second source, see "Dates before the journal".
- **2026-07-22** has 5 brief runs and no recompute line. The 00:30 run hit its timeout at
  01:47Z; before #1330 a timeout killed only the docker client, so the container went on
  writing with no journal output. Every later run logged `reusing 11 frozen candidate(s)`,
  the first at 07-23 05:04Z, so the list existed before the open and did not change. The
  CSV row shows `recomputed_after_open = 0`, which is correct.
- The record trusts the log text. If a future change renames these log lines, the script
  below stops matching; it reads a frozen extract, so this file is not affected.

## Dates before the journal (added 2026-09-17)

The VPS brief store starts at asof 2026-05-19; no brief exists for an earlier date.
The five dates 2026-05-19 … 2026-05-23 fell out of the journal, so they are answered
from three facts that do not depend on it. Read-only on the VPS, 2026-09-17.

- **`brief_generated_at`.** The brief writer stamps every row with the time of the
  run that wrote it (`pd.Timestamp.now`, the same in #133 and today). The largest
  stamp is the last run that wrote the list.
- **`<asof>.meta.json`.** It is written by the same run and holds the row count split
  by model. No later tool rewrites it: its mtime equals the largest stamp to the
  second, and its count equals today's row count on every date.
- **`thematic_candidates/<asof>.parquet`.** Its mtime is the last candidate
  recompute. On every date it is a few minutes before the brief run and never later.

- **Brief tickers equal candidate tickers.** On every date the set of tickers in the
  brief is exactly the set in the candidates parquet, and the candidates parquet was
  never written after the open. A later change of the brief's list alone would break
  this equality; a change of both files would move the candidates mtime.

The brief parquets of 2026-05-21 … 2026-05-23 have a later mtime (2026-05-27T09:12:17Z,
all within 0.04 s; the journal-covered 2026-05-24 has the same mtime). The most likely
writer is the one-off `alphalens thematic clean-titles` backfill, which rewrites only
`source_event_title`: its PR #271 was merged a day later, on 2026-05-28, so it was run
from the branch, and no brief title on these dates has padded punctuation today. That
attribution is not proven. It does not matter for the list: the meta files and the
candidate files were not written on 2026-05-27, and the ticker sets still match.

Why these dates become "before the open" (a frozen label) and not "unknown": two
independent files agree (the brief stamps with the meta file, and the candidate file
with the ticker sets), and nothing that can set the list wrote after the open. If a
later finding proves one of them wrong, the correction is a new `SEL_LABEL_VERSION`,
which recomputes frozen rows.

| asof | arrival open (UTC) | candidates written (UTC) | last brief stamp (UTC) | rows | after open |
|---|---|---|---|---|---|
| 2026-05-19 | 05-20 13:30 | 05-20 17:13 | 05-20 17:21 | 8 | **yes** |
| 2026-05-20 | 05-21 13:30 | 05-21 10:28 | 05-21 10:31 | 7 | no |
| 2026-05-21 | 05-22 13:30 | 05-22 09:31 | 05-22 09:42 | 13 | no |
| 2026-05-22 | 05-26 13:30 | 05-23 07:00 | 05-23 07:10 | 12 | no |
| 2026-05-23 | 05-26 13:30 | 05-24 06:57 | 05-24 07:03 | 8 | no |

`published_after_open` is 1 when the last brief stamp is at or after the arrival open.
For 2026-05-19 even the candidate list was computed after the open, so no list for that
date existed before it. For the other four dates nothing that can change the list
wrote after the open.

The schedule in git agrees but does not decide: from #157 (2026-05-19) to #315
(2026-05-30) the timer ran once a day at 06:30 UTC, before the open. The actual run times
above are irregular (07:00 to 17:21), so some runs were started by hand, and only the
stamps say when the list was written.

Limits of this source:

- It shows the LAST write, not every write. A list written before the open and replaced
  by a different list before the open looks the same as one list, which is correct for
  this question: the list that existed at the open is the stored one.
- It trusts file mtimes on the VPS. A copy that set every mtime to a new value would
  show up as meta and candidate times far from the brief stamps; on these dates they
  match the stamps within minutes, and the meta times to the second.

Extraction command (read-only, on the VPS, from `~/AlphaLens` with the host venv):

```python
import datetime as dt, json, os
import pandas as pd
from alphalens_pipeline.feedback.ladder_config import ladder_arrival_session
from alphalens_pipeline.thematic.publication import deadline_utc

home = os.path.expanduser("~/.alphalens")
iso = lambda t: pd.Timestamp(t).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")
mtime = lambda p: dt.datetime.fromtimestamp(os.path.getmtime(p), dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
for asof in ["2026-05-19", "2026-05-20", "2026-05-21", "2026-05-22", "2026-05-23"]:
    day = dt.date.fromisoformat(asof)
    brief = f"{home}/thematic_briefs/{asof}.parquet"
    meta_path = f"{home}/thematic_briefs/{asof}.meta.json"
    cand = f"{home}/thematic_candidates/{asof}.parquet"
    frame = pd.read_parquet(brief)
    stamps = pd.to_datetime(frame["brief_generated_at"], utc=True)
    same = set(frame["ticker"].astype(str)) == set(pd.read_parquet(cand)["ticker"].astype(str))
    meta = json.load(open(meta_path))
    deadline = deadline_utc(day)
    print(",".join(map(str, [
        asof, ladder_arrival_session(day), iso(deadline), len(stamps),
        meta["n_pro"] + meta["n_flash"], iso(stamps.min()), iso(stamps.max()),
        mtime(meta_path), mtime(brief), mtime(cand), int(same), int(stamps.max() >= deadline),
    ])))
```

## Derivation script

Run from the repo root with the workspace venv:
`.venv/bin/python derive.py <journal.log> <out.csv>`.

```python
import csv
import datetime as dt
import re
import sys
from zoneinfo import ZoneInfo

from alphalens_pipeline.feedback.ladder_config import ladder_arrival_session

src, dst = sys.argv[1], sys.argv[2]
line_re = re.compile(r"^(\S+) \S+ \S+\[(\d+)\]: (.*)$")
reuse, writes, briefs = set(), [], []
for line in open(src, encoding="utf-8"):
    m = line_re.match(line)
    if not m:
        continue
    ts, pid, msg = m.groups()
    t = dt.datetime.fromisoformat(ts)
    if r := re.search(r"map_themes (\d{4}-\d{2}-\d{2}): reusing", msg):
        reuse.add((pid, r.group(1)))
    if w := re.search(r"Wrote (\d+) candidate rows .*thematic_candidates/(\d{4}-\d{2}-\d{2})\.parquet", msg):
        writes.append((t, pid, w.group(2), int(w.group(1))))
    if b := re.search(r"generate_briefs (\d{4}-\d{2}-\d{2}): wrote (\d+) briefs", msg):
        briefs.append((t, b.group(1), int(b.group(2))))

ny = ZoneInfo("America/New_York")
dates = sorted({a for _, _, a, _ in writes} | {a for _, a, _ in briefs})
fields = [
    "asof", "arrival_session", "arrival_open_utc",
    "candidate_recomputes", "first_recompute_utc", "last_recompute_utc",
    "candidates_last_pre_open", "candidates_final",
    "brief_runs", "first_brief_utc", "briefs_last_pre_open", "briefs_final",
    "recomputed_after_open",
]
rows = []
for asof in dates:
    arrival = ladder_arrival_session(dt.date.fromisoformat(asof))
    open_utc = dt.datetime.combine(arrival, dt.time(9, 30), ny).astimezone(dt.timezone.utc)
    rec = sorted((t, n) for t, pid, a, n in writes if a == asof and (pid, a) not in reuse)
    br = sorted((t, n) for t, a, n in briefs if a == asof)
    pre_rec = [n for t, n in rec if t < open_utc]
    pre_br = [n for t, n in br if t < open_utc]
    iso = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ")
    rows.append({
        "asof": asof,
        "arrival_session": arrival.isoformat(),
        "arrival_open_utc": iso(open_utc),
        "candidate_recomputes": len(rec),
        "first_recompute_utc": iso(rec[0][0]) if rec else "",
        "last_recompute_utc": iso(rec[-1][0]) if rec else "",
        "candidates_last_pre_open": pre_rec[-1] if pre_rec else "",
        "candidates_final": rec[-1][1] if rec else "",
        "brief_runs": len(br),
        "first_brief_utc": iso(br[0][0]) if br else "",
        "briefs_last_pre_open": pre_br[-1] if pre_br else "",
        "briefs_final": br[-1][1] if br else "",
        "recomputed_after_open": int(bool(rec) and rec[-1][0] >= open_utc),
    })
with open(dst, "w", newline="", encoding="utf-8") as fh:
    wr = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n")
    wr.writeheader()
    wr.writerows(rows)
```
