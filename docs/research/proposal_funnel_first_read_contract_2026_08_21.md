# proposal_funnel first read — analysis contract

Written: 2026-08-21 20:20 CEST, before any row of the funnel was read.

Contract template: `docs/research/experiment_analysis_contract_2026_08_07.md`.
Issue: #1002 (part of #974).

## What was known before writing this

Deliberately recorded, so the plan below can be checked against what it could
have been fitted to. Before writing this file I read:

- the funnel writer (`orchestrator.py::_funnel_row`, `_PROPOSAL_FUNNEL_COLUMNS`)
  and the bracket classifier (`verification/mcap_filter.py`) — column names and
  the verdict vocabulary, no values;
- the file NAMES on the VPS: 15 funnel days, `2026-08-06` .. `2026-08-20`;
  16 rollup days, `2026-08-05` .. `2026-08-20`;
- the bracket bounds: `DEFAULT_MCAP_RANGE = (500_000_000, 10_000_000_000)`,
  both ends inclusive;
- the single-day numbers already quoted in #1002 itself (2026-08-06: 23
  proposals, 78% `too_big` / 13% `in_bracket` / 9% `too_small`).

No cell of any parquet was read. The 2026-08-06 numbers are the anecdote this
read exists to replace, and are excluded from every "was it stable" comparison
below by being the day the hypothesis came from.

---

1. **QUESTION**
   What is the market-cap composition of the mapper's proposals — pooled, by
   day, and by theme — over the whole funnel window?

   This is a description, not a comparison. There is no treatment and nothing
   is being tested for an effect.

2. **UNIT**
   One **proposal**: one row of `proposal_funnel/<asof>.parquet`, i.e. one
   ticker proposed for one theme on one `asof`. The same ticker proposed for
   two themes on one day is two proposals.

   Secondary unit, named now: the **theme-day** (one theme's mapper call on one
   `asof`), used only for the per-theme yield question.

3. **SAMPLING**
   Census. Every row of every funnel file present on the VPS at read time,
   `2026-08-06` .. `2026-08-20`, no filtering.

   *Was any case selected because of an arm's output?* **No.** There are no
   arms, and no day, theme or ticker is dropped for what it contains. The one
   exception runs the other way: 2026-08-06 is the day that generated the
   hypothesis, so it is reported but never used as evidence that the split is
   stable.

4. **ARMS**
   None. Descriptive read of accumulated telemetry, zero LLM calls.

   The window is not homogeneous in configuration, which is recorded here
   rather than discovered later: `mapper_config_version` and
   `channel_config_version` both changed inside it (cohort 2 of the channel
   pre-registration starts 2026-08-20 13:51 UTC). Every headline number is
   therefore reported **stratified by `mapper_config_version`** as well as
   pooled, and no cross-version difference is claimed as an effect — the
   versions differ in more than one thing at once.

5. **PRIMARY**
   The share of proposals with `bracket_verdict == "too_big"`, pooled over the
   window, with the denominator being proposals whose market cap resolved
   (`bracket_verdict != "no_mcap"`).

   One estimand. Everything below is secondary and may not be promoted.

6. **SECONDARY** (all fixed now)
   - the same share per `asof`, with an exact binomial 95% interval per day;
   - the four-way verdict split (`too_big` / `in_bracket` / `too_small` /
     `no_mcap`) pooled and per day, on the ALL-proposals denominator;
   - the full market-cap distribution: min, deciles, max, plus a histogram in
     powers of ten — not the three-bucket summary;
   - the same, per theme, for every theme with at least 5 proposals;
   - per-theme yield: theme-days, proposals, in-bracket proposals, and the
     count of theme-days returning zero in-bracket names;
   - `no_mcap` rate per day (a yfinance-outage detector, per the classifier's
     own docstring);
   - duplication: distinct tickers vs proposals, and the most frequently
     proposed tickers;
   - the share of proposals whose ticker recurs on 3 or more distinct days.

7. **TEST**
   For the primary the answer is a number and an interval, not a test.

   For the single stated yes/no question — "is the mega-cap share stable day to
   day" — the test is fixed here as a **Pearson chi-square test of homogeneity**
   on the `day × {too_big, not-too_big}` table over days with at least 5
   resolved proposals, with the observed per-day range reported beside it. If
   any expected cell is below 5, the chi-square is reported as unreliable and
   the range alone is used.

8. **THRESHOLD**
   Operationally, "stable" means: every day's `too_big` share sits within
   **±10 percentage points** of the pooled share. This is the width at which
   the composition would change what a reader expects from tomorrow's run.

   A theme is called "structurally outside the bracket" only if it has at least
   **10 proposals** across at least **3 distinct days** and **zero** of them are
   in bracket.

9. **POWER**
   Fixed by what exists: 15 days. The expected order of magnitude is 20-40
   proposals per day, so a few hundred proposals pooled.

   The binding limitation is not N, it is **independence**. Each `asof` file is
   overwritten by whichever of the six daily slots ran last, so a day is one
   draw, not a replicate — the pipeline cannot be asked to repeat a day without
   changing it. Themes also repeat across days, so proposals are clustered by
   theme and the naive binomial interval understates the true day-to-day
   spread. Both intervals below are therefore reported as *descriptive*, not as
   inference about a future day.

   This design has no power to detect a small day-to-day drift, and none is
   claimed.

10. **FAILURE MODES** (kept separate, never collapsed into one denominator)
    - **Refusal / zero proposals** — a theme whose mapper returned nothing
      writes NO funnel row (`_write_proposal_funnel_best_effort` returns early
      on an empty list). Refusals are therefore **invisible in this file** and
      the funnel cannot measure the refusal rate. That question belongs to the
      theme-decision / rollup artifacts and to #991, and is out of scope here.
    - **`no_mcap`** — the classifier could not resolve a cap. Reported as its
      own bucket, never merged into `too_small`.
    - **Duplicate ticker** — same ticker, several themes, same day. Kept as
      separate proposals in the primary; reported separately in secondary.
    - **Repeated theme across days** — clustering, see POWER.
    - **Missing day** — a day with no funnel file at all is reported by name,
      not silently skipped.

11. **CONTROLS**
    Read-only. No model, no temperature, no token budget — nothing is
    generated. The analysis script is committed with the result so the numbers
    can be reproduced from the same parquets.

    Market caps are whatever the classifier stamped at run time; this read does
    not re-fetch them, so it inherits any staleness in `mcap_cache.json`
    (max 14 days by `_MCAP_CACHE_MAX_STALE_DAYS`).

12. **STOPPING**
    The window is fixed at "every funnel file that exists when the script
    runs". No day is added or removed after seeing a number.

13. **VERDICTS**
    Only descriptive labels are permitted:
    - **stable** — the ±10 pp condition in THRESHOLD holds on every day;
    - **varies** — it does not, reported with the observed range;
    - **theme-dependent** — at least one theme meets the
      structurally-outside-the-bracket condition in THRESHOLD.

    The words **defect**, **bug** and **broken** may not appear in the result.
    #1002 states the reason: separating a model artefact from a true property
    of news-to-company mapping needs an independently constructed universe per
    theme, which this read does not have.

14. **ARCHIVE**
    - inputs: `~/.alphalens/thematic_candidates/proposal_funnel/*.parquet` on
      the VPS (`vault.kamilpajak.pl`), copied to the Mac for the read;
    - script: `apps/alphalens-research/scripts/read_proposal_funnel.py`,
      committed in the same PR as this contract;
    - result: `docs/research/proposal_funnel_first_read_2026_08_21.md`,
      committed AFTER this file, in a later commit on the same branch;
    - the git history of this branch is the timestamp evidence: contract
      commit strictly precedes the result commit.
