# Sector-percentile nulls: where they come from, and what that means for #170

**Status:** LOCKED (measurement record)
**Date:** 2026-09-05
**Closes:** #170 (theme-conditional sector cohort) — acceptance criterion 1 executed, premise refuted
**Store read at:** 2026-09-05 08:32 UTC

## 1. Question

Issue #170 (Track 2, opened 2026-05-20) asked for a theme-conditional peer
cohort. Its first acceptance criterion is a measurement:

> Collect data: log fraction of briefs where `sector_percentile=nan` per ticker
> over a week of daily runs. If material (e.g. >30% of thematic briefs lose the
> signal), draft a design memo comparing the three options.

This memo runs that measurement over 102 brief-dates instead of one week, and
asks the question the criterion does not: **when the percentile is absent, is
the peer cohort the reason?**

## 2. Method

Source: `~/.alphalens/thematic_briefs/` and `~/.alphalens/thematic_scored/`
rsync'd from the VPS. 112 brief parquets; 10 predate the `peer_cohort_level`
column (added by Django migration `0002`) and are excluded. Remaining: **955
brief rows over 102 dates, 2026-05-26 … 2026-09-04**.

Brief rows are the shortlist survivors, which is the unit the criterion names
("thematic briefs"). Brief dates follow the T-1 convention.

## 3. The criterion is met

| column | null rows | null rate |
|---|---|---|
| `insider_score_sector_percentile` | 744 / 955 | 77.9% |
| `fcff_yield_sector_percentile` | 296 / 955 | 31.0% |
| `valuation_composite_sector_percentile` | 150 / 955 | 15.7% |

Two of the three clear the 30% bar the issue names.

## 4. The cohort is not the reason

Cohort resolution succeeds almost always. `iter_peers_fallback` returns:

| level | rows | share |
|---|---|---|
| `sic4` | 828 | 86.7% |
| `ff48` | 63 | 6.6% |
| `sic3` | 57 | 6.0% |
| `thin` | 7 | **0.7%** |

`thin` is the only level at which cohort width zeroes a percentile:
`screening/scorer.py:353` sets all three signals to `None` when
`peer_cohort_level == "thin"`, and only then.

By month, so the pooled figure is not hiding a mid-window fix (the FF-48
overlay, #198, landed inside this window):

| month | `sic4` | `sic3` | `ff48` | `thin` | rows |
|---|---|---|---|---|---|
| 2026-05 | 83.5% | 8.8% | 5.5% | 2.2% | 91 |
| 2026-06 | 84.7% | 10.0% | 5.0% | 0.3% | 300 |
| 2026-07 | 90.3% | 4.0% | 4.9% | 0.9% | 350 |
| 2026-08 | 82.5% | 2.7% | 14.2% | 0.5% | 183 |
| 2026-09 | 100.0% | 0.0% | 0.0% | 0.0% | 31 |

`thin` never exceeds 2.2% in any month.

### 4.1 Positive control on the null mechanism

Reading the code says FCFF returns a null percentile only when the *candidate*
has no yield (`screening/fcff_signal.py:105-107`); peers cannot cause it. The
store agrees:

- 296 null percentiles; **295** of them also have a null `fcff_yield_pct`.
- The 1 remaining row (value present, percentile absent) sits in a `thin`
  cohort. Rows with a value, a healthy cohort and no percentile: **0**.

For the insider signal the same control needed a correction. A first pass
compared "percentile null" against "`insider_score_usd` null" and found 666
rows with a score but no rank, which looks like a large cohort failure. It is
not: `insider_score_usd` is **0**, not null, when the candidate has no
opportunistic buying.

| `insider_score_usd` | rows |
|---|---|
| null | 78 |
| `== 0` | 849 |
| `> 0` | **13** |

Only 13 of 955 rows carry actual opportunistic insider buying. 3 of those have
no percentile, and 2 are in a non-thin cohort — the genuine "lone buyer, no
peer buyers" case (`screening/insider_signal.py:251-254`). Two rows.

**Conclusion: the peer cohort explains ~0.7% of the missing percentiles.** The
rest is candidate-level data absence — no EDGAR fundamentals (FCFF), or no
insider buying to rank (insider). All three options in #170 act on the cohort,
so none of them can move these numbers.

## 5. The proposed options are also refuted on cardinality

Option 1 is "peers = today's other candidates for the same theme". Measured on
`~/.alphalens/thematic_scored/` (1251 rows, 112 dates), candidates per
`(date, theme)`:

- mean **2.4**, median **2**, max **7**.

A percentile over a median of one other name is not a statistic. #170 itself
anticipated "cohort 5-20, small sample = noisy"; the realised cardinality is
below that range. Option 2 (`SIC ∪ same-theme`) adds ~1 name to a cohort that
already cleared `min_cohort = 8`, so it changes nothing. Option 3 (an extra LLM
call to enumerate a theme universe) pays money and latency for an unvalidated
peer set, and each enumerated name then needs its own fundamentals fetch to
participate in the percentile at all.

## 6. What the measurement found instead

### 6.1 Missing data is scored as a negative vote

`screening/scorer.py:67-71`:

```python
def fcff_is_positive(*, sector_percentile: float | None) -> bool:
    """Positive when FCFF yield is at or above the sector median."""
    if sector_percentile is None:
        return False
    return sector_percentile >= 50.0
```

That boolean reaches `compose_weighted_score` → `layer4_weighted_score` →
`selection_score`, the cross-candidate ordering key. It is worth one point on a
1-5 scale. So on 31% of brief rows (48.6% in August, §6.3), "we have no
fundamentals for this company" is scored identically to "this company is below
its sector median". The docstring does not claim that reading; nothing in the
call site records it as a decision.

Rows without FCFF data score lower:

| | fcff missing (n=295) | fcff present (n=660) | gap |
|---|---|---|---|
| `layer4_weighted_score` | 2.034 | 2.665 | +0.631 |
| `selection_score` | 1.474 | 2.445 | +0.972 |

Among rows that *do* have the data, the bit fires 53.8% of the time (355/660).

**Both figures are bounded, not established** — see §7.

### 6.2 Same-theme percentiles come from different cohorts

The comparability problem #170 was really about (QUBT/IONQ under one theme,
different 4-digit SIC codes) is not rare. Over `(date, theme)` groups holding
≥2 brief candidates:

- 310 groups; **233 (75.2%)** span more than one `industry_id`.
- Restricting to groups where ≥2 candidates actually carry a valuation
  percentile (264 groups): **201 (76.1%)** draw those percentiles from
  different cohorts.

Since the derived boolean enters a cross-candidate ranking, percentiles
measured against different reference sets are being compared. This is the part
of #170 that survives — but its remedy is on the *consumer* of the percentile,
not on the width of the cohort.

### 6.3 FCFF coverage degraded in August

| month | `fcff_yield_sector_percentile` null | rows |
|---|---|---|
| 2026-05 | 29.7% | 91 |
| 2026-06 | 25.7% | 300 |
| 2026-07 | 26.0% | 350 |
| 2026-08 | **48.6%** | 183 |
| 2026-09 | 38.7% | 31 |

Since §4.1 shows this null is a candidate-data null, the jump is a coverage
change in the EDGAR fundamentals store, unrelated to anything in #170. The
September figure rests on 4 dates and is provisional.

Three explanations were tested and all three fail:

- **Whole-day fetch failure.** `scorer.py::_build_feature_fetcher` returns
  `lambda ticker, asof: None` for every ticker when
  `EdgarFundamentalsStore.preload` raises, which would blank an entire run.
  That would show as dates at 100% null. Across all 102 dates there are
  **zero**; the per-date distribution is continuous (15 dates at 0%, 29 in
  (0,25], 45 in (25,50], 10 in (50,75], 3 in (75,100)). The absence is
  per-ticker, not per-run.
- **Missing means small-cap.** Median `market_cap` is not systematically lower
  for missing rows: 3.85bn vs 4.32bn in August, and in July the missing rows
  were *larger* (6.91bn vs 4.56bn).
- **An influx of never-seen tickers.** August's new-ticker share (60.7%) sits
  between June (77.2%) and July (51.9%).

What remains is a timing observation, not a cause: the sustained elevation
begins around 2026-08-19/20, when the market-cap bracket became the binding
candidate filter (#1075). A different candidate mix can carry different EDGAR
coverage without differing in median market cap — foreign private issuers
filing 20-F rather than 10-K, recent listings. Tracked in #1335.

## 7. What this measurement cannot support

- **The 53.8% base rate does not transfer to the missing rows.** It assumes
  names without EDGAR fundamentals have the same FCFF-yield distribution as
  covered names, which is unverified. Treat 53.8% as an upper bound on the
  share of missing rows wrongly denied. The defect in §6.1 stands on the
  mechanism (absence can only subtract), not on this number.
  An earlier draft of this memo justified the caveat by asserting that missing
  coverage correlates with earlier-stage companies. **The store does not
  support that**: median `market_cap` for missing rows is not systematically
  lower, and in July it was higher (§6.3). The caveat survives as "the
  distribution is unverified"; the mechanism offered for it does not.
- **The 0.972 `selection_score` gap is not the bit's contribution.** Names
  without fundamentals plausibly score worse on other components too. It is an
  upper bound.
- **Cohort *quality* was not measured, only cohort *presence*.** The mcap /
  price filter runs before the `min_cohort` check, so a cohort can clear the
  floor and still be economically incoherent (the DFIN case widened into a
  ~300-name "Business Services" FF-48 bucket). Nothing here refutes that
  concern; it was not tested.
- **Level-to-level null rates must not be read as a width effect.** Rows reach
  `ff48` only because `sic4` and `sic3` failed the floor, so comparing null
  rates across levels compares different tickers, not the same ticker at
  different widths.

## 8. Verdict

**#170 is closed.** Acceptance criterion 1 is executed and recorded here. The
criterion is met numerically and mis-specified: it measures signal absence,
while the issue's three remedies address cohort width, which accounts for ~0.7%
of that absence. The remedies are additionally refuted on realised theme
cardinality (§5).

Two findings are carried out as their own issues:

- **#1334** — `fcff_is_positive(None) → False` puts missing data into
  `selection_score` as a negative vote (§6.1), together with the 76.1%
  cross-cohort comparability result (§6.2), because both are fixed by changing
  how the percentile is consumed. This is a SELECTION change: it needs its own
  design memo, a hypothesis-budget entry, and a `SCORER_CONFIG_VERSION` bump —
  never a smuggled ordering tweak. The same score already documents the
  opposite default one term away: `selection_score.py:43` pins `atr_penalty` as
  "never punish unknown ATR".
- **#1335** — the late-August FCFF coverage regression (§6.3).

## 9. Reproduce

```bash
rsync -a --exclude '_backup_pre_pr185' \
  vault.kamilpajak.pl:.alphalens/thematic_briefs/ "$HOME/.alphalens/thematic_briefs/"
rsync -a vault.kamilpajak.pl:.alphalens/thematic_scored/ "$HOME/.alphalens/thematic_scored/"
```

Every figure is a pandas aggregation over those two stores: concatenate the
per-date parquets (skipping files without `peer_cohort_level`), then group as
described in each section. No tracked code was touched.
