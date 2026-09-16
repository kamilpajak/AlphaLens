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

- **Dates before 2026-05-24 are not covered**, and 2026-05-24 is covered only from
  2026-05-25T13:45Z. An earlier write for that date would not be visible.
- **2026-07-22** has 5 brief runs and no recompute line. The 00:30 run hit its timeout at
  01:47Z; before #1330 a timeout killed only the docker client, so the container went on
  writing with no journal output. Every later run logged `reusing 11 frozen candidate(s)`,
  the first at 07-23 05:04Z, so the list existed before the open and did not change. The
  CSV row shows `recomputed_after_open = 0`, which is correct.
- The record trusts the log text. If a future change renames these log lines, the script
  below stops matching; it reads a frozen extract, so this file is not affected.

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
