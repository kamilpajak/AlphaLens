# proposal_funnel first read — market-cap composition of mapper proposals

Status: RESULT, 2026-08-21. Issue #1002 (part of #974).
Contract: [`proposal_funnel_first_read_contract_2026_08_21.md`](proposal_funnel_first_read_contract_2026_08_21.md),
committed before any row was read.
Script: `apps/alphalens-research/scripts/read_proposal_funnel.py` (deterministic —
two runs produce byte-identical output).

## Denominator, first

Everything below rests on:

| | |
|---|---|
| proposals | **524** |
| of which the cap resolved | **488** (36 `no_mcap`) |
| days | **15**, `2026-08-06` .. `2026-08-20`, none missing |
| theme-days | **94** |
| distinct themes | **39** in total; 34 with at least 5 proposals, 33 with at least 5 resolved |
| distinct tickers | **205** |

Integrity passed on every check the script makes: no null verdict, no unknown
verdict, no `asof` disagreeing with its file name, no cap present on a
`no_mcap` row and none missing on a resolved one.

The caps themselves were checked separately, because every number below rests on
them. Of the 58 tickers proposed on 3 or more days, 57 stay within a 1.5× range
across the window. The single exception is `MRNA` at 3.2× ($21.5B .. $69.6B),
and that turned out to be **real**: its close went $62.96 (08-18) → $174.38
(08-19) → $133.32 (08-20). The classifier's caps track prices, and no
implausible value was found.

## 1. Was 78% a single-day artefact? No.

**Primary estimand: `too_big` = 370/488 = 75.8%** (Wilson 95%: 71.8% .. 79.4%).

The 2026-08-06 anecdote that opened #1002 measured 78.3%. Fifteen days later
the pooled figure is 75.8%, and 08-06 sits near the middle of the daily range,
not at an edge. The single day was representative.

On all 524 proposals, including the ones whose cap never resolved:

| verdict | n | share |
|---|---:|---:|
| `too_big` | 370 | 70.6% |
| `in_bracket` | 87 | 16.6% |
| `too_small` | 31 | 5.9% |
| `no_mcap` | 36 | 6.9% |

## 2. Is the share stable day to day? The two pre-committed instruments disagree, and the band is the one at fault

| instrument | pre-committed in | result |
|---|---|---|
| ±10 pp band around the pooled share (§8) | contract | **VARIES** — observed range 57.8% .. 95.5%, worst deviation 19.6 pp |
| Pearson chi-square of homogeneity (§7) | contract | chi2 = 22.18, p = 0.075 over 15 days — **does not reject** one constant rate |
| permutation version of the same statistic | contract §7's own remedy for a small expected cell (min expected 2.2) | p = 0.0744 — same answer without the assumption |

Both were fixed before the data and both are reported. They point opposite ways,
and the honest reading is that **the band is the weaker instrument, for a reason
that did not depend on the numbers**: the days range from 9 to 96 proposals, and
a 9-proposal day swings ±25 pp on sampling noise alone. A fixed percentage-point
band cannot be met by days of such different sizes. The day-level intervals show
this directly — every daily interval overlaps the pooled 75.8% except 2026-08-16
(95.5%, interval 78.2% .. 99.2%), which is one day out of fifteen.

**Recorded as a contract-design lesson**, not as a reason to ignore the band:
a stability threshold must be stated on an estimate whose precision is roughly
constant, or stated as an interval-overlap rule. This one was not, and the fix
belongs in the next contract that needs a stability clause.

Conclusion carried forward: **no day-to-day movement is established.** The
composition behaves like one rate near 76% with the spread that daily sample
sizes imply.

| asof | prop | resolved | themes | too_big | in | small | no_mcap | too_big share (95%) |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| 2026-08-06 | 23 | 23 | 7 | 18 | 3 | 2 | 0 | 78.3% (58.1 .. 90.3) |
| 2026-08-07 | 31 | 29 | 8 | 24 | 3 | 2 | 2 | 82.8% (65.5 .. 92.4) |
| 2026-08-08 | 10 | 10 | 5 | 6 | 4 | 0 | 0 | 60.0% (31.3 .. 83.2) |
| 2026-08-09 | 20 | 19 | 6 | 12 | 7 | 0 | 1 | 63.2% (41.0 .. 80.9) |
| 2026-08-10 | 32 | 31 | 8 | 21 | 8 | 2 | 1 | 67.7% (50.1 .. 81.4) |
| 2026-08-11 | 26 | 23 | 7 | 19 | 4 | 0 | 3 | 82.6% (62.9 .. 93.0) |
| 2026-08-12 | 23 | 23 | 6 | 19 | 3 | 1 | 0 | 82.6% (62.9 .. 93.0) |
| 2026-08-13 | 21 | 20 | 6 | 17 | 2 | 1 | 1 | 85.0% (64.0 .. 94.8) |
| 2026-08-14 | 26 | 25 | 5 | 18 | 6 | 1 | 1 | 72.0% (52.4 .. 85.7) |
| 2026-08-15 | 19 | 19 | 4 | 17 | 2 | 0 | 0 | 89.5% (68.6 .. 97.1) |
| 2026-08-16 | 22 | 22 | 4 | 21 | 1 | 0 | 0 | 95.5% (78.2 .. 99.2) |
| 2026-08-17 | 9 | 9 | 3 | 7 | 2 | 0 | 0 | 77.8% (45.3 .. 93.7) |
| 2026-08-18 | 102 | 96 | 10 | 75 | 13 | 8 | 6 | 78.1% (68.9 .. 85.2) |
| 2026-08-19 | 53 | 45 | 5 | 26 | 13 | 6 | 8 | 57.8% (43.3 .. 71.0) |
| 2026-08-20 | 107 | 94 | 10 | 70 | 16 | 8 | 13 | 74.5% (64.8 .. 82.2) |

## 3. Is it theme-dependent? Overwhelmingly

This is where the variance actually lives. Post-hoc (the contract fixed a test
across DAYS and only a structural flag for themes), the same statistic across
the 33 themes with at least 5 resolved proposals (the same cut-off the day-level
test uses) gives **chi2 = 169.6,
permutation p < 0.0001**. Labelled post-hoc and not quoted as a confirmation —
but the raw extremes need no test:

| always outside the bracket | | reliably inside | |
|---|---:|---|---:|
| `pentagon` | 28/28 too_big | `box_office` | 0/15 too_big, 13 in bracket |
| `debt_offering` | 15/15 | `minerals` | 5/14 too_big, 7 in bracket |
| `tech_rally` | 10/10 | `ammunition_shortage` | 10/21 too_big, 7 in bracket |
| `consumer_sentiment` | 26/27 | `cannabis` | 0/8 too_big (4 too_small) |
| `earnings_reaction` | 7/7, median cap $2 961B | `police_technology` | 2/10 (5 too_small, median $0.09B) |
| `revenue_forecast` | 6/6, median cap $909B | `content_authentication` | 3/7 (median $0.64B) |

Three themes meet the contract's pre-committed **structurally outside the
bracket** condition (≥10 proposals, ≥3 distinct days, zero in bracket):

| theme | proposals | days | in bracket | median cap |
|---|---:|---:|---:|---:|
| `pentagon` | 28 | 7 | 0 | $136.8B |
| `consumer_sentiment` | 27 | 3 | 0 | $168.1B |
| `mrna_technology` | 10 | 8 | 0 | $23.6B |

The pattern the table shows — index-level and macro themes (`jobs_report`,
`retail_sales`, `cpi`, `consumer_sentiment`, `tech_rally`, `debt_offering`,
`currency_intervention`, `yen_support`) returning mega caps, product- and
sector-specific themes (`box_office`, `minerals`, `ammunition_shortage`,
`cannabis`) returning small ones — is an **observation about which themes are
selected**, not a measured mechanism. See §7 for why the funnel cannot settle
that.

## 4. The full cap distribution

n = 488 resolved. The bracket is $0.5B .. $10B inclusive.

```
min $0.002B     median $41.21B     max $5 428B

p10  $1.37B   p20  $7.75B   p30 $16.87B   p40 $24.15B   p50 $41.21B
p60 $73.43B   p70 $131.66B  p80 $194.45B  p90 $434.77B
```

| bucket | n |
|---|---:|
| $1M .. $10M | 3 |
| $10M .. $100M | 7 |
| $100M .. $1B | 29 |
| $1B .. $10B | 79 |
| $10B .. $100B | **202** |
| $100B .. $1T | **145** |
| above $1T | 23 |

The median proposal is **four times the top of the bracket**. The mass is not a
tail of a few giants — 370 of 488 resolved proposals sit above $10B (which is
the `too_big` count exactly, as it must be), and the modal decade is
$10B .. $100B. At the other end, only 8 of the 87 in-bracket names fall in
$0.5B .. $1B.

## 5. Which themes spend calls and return nothing usable

**49 of 94 theme-days (52.1%) returned zero in-bracket names.** The median
theme-day produces 5 proposals and 0 usable ones.

The three structurally-outside themes in §3 account for 18 of those theme-days
(pentagon 7, mrna_technology 8, consumer_sentiment 3) and 65 proposals, none of
which could ever ship.

## 6. Two byproducts worth their own follow-up

**`no_mcap` is 6.9%** — 36 proposals, 18 tickers, on 9 of 15 days. The list is
dominated by names that no longer trade under that symbol: `CVAC` (9),
`GRTS` (5), `RDFN` (3), `PLL`, `BIG`, `DGLY`, `CTLT` (2 each). This is the
concrete number behind **#1074** (delisted-ticker pre-check before yfinance):
the mapper repeatedly proposes tickers that cannot resolve, and each one costs a
yfinance call and a funnel row.

**The proposal rate quadrupled on 2026-08-18 and the composition did not move.**
The mapper's own `prompt_sha` changed on that day:

| prompt_sha | days | proposals | per day | too_big | in_bracket |
|---|---:|---:|---:|---:|---:|
| `52b12550f344` | 12 (08-06 .. 08-17) | 262 | 21.8 | 78.7% | 17.2% |
| `fdcbf59d0720` | 3 (08-18 .. 08-20) | 262 | 87.3 | 72.8% | 16.0% |

Four times the proposals, essentially the same mix. **This is descriptive only**
— three days, and the prompt change is confounded with everything else that
moved in that window (the `mapper-freeze-v3`/`v4` schema bump and the channel
assessor's own v1→v2 token). It is recorded because it bears directly on the
dead hypothesis in #1002 that a cap on the number of names was what limited the
answer: raising volume by 4× bought proportionally more in-bracket names and
changed nothing about the shape.

## 7. What this read can and cannot settle

**Can:**
- the pooled composition and its denominator;
- whether the composition moves day to day (it does not, measurably);
- that theme identity dominates the variance, and which specific themes never
  produce an in-bracket name;
- the full cap distribution rather than the three-bucket summary;
- how many theme-days are spent for zero usable output;
- the `no_mcap` rate and which tickers cause it;
- how concentrated the proposals are (205 distinct tickers behind 524
  proposals; `MRNA` proposed 24 times across 10 days, `too_big` every time;
  58% of proposals come from tickers seen on 3 or more days).

**Cannot:**
- **The refusal rate.** A theme whose mapper returned nothing writes no funnel
  row at all, so refusals are invisible in this file by construction. That
  question belongs to the theme-decision artifact and is blocked on **#991**.
- **Whether the skew is a defect.** Per #1002 itself, separating a model
  artefact from a true property of news-to-company mapping requires an
  independently built universe of companies exposed to each theme. This read
  has no such universe and makes no such claim.
- **Whether theme SELECTION or within-theme mapper preference drives it.** The
  funnel only sees themes that were both selected and produced at least one
  proposal. `~/.alphalens/theme_rollup/<asof>.parquet` holds every scored theme
  including the unselected ones and makes any selection policy replayable from
  disk with zero LLM calls — that is the natural next question, and it is a
  different one.
- **Any causal reading of the 08-18 prompt change** (§6): three days, several
  things moved together.

## 8. Follow-ups this read earns

1. **#1074 gets a number** — 6.9% of proposals cannot resolve a cap, and the top
   offenders are delisted or renamed symbols.
2. **A theme-level question, properly posed** — whether the mega-cap skew comes
   from theme selection or from the mapper's behaviour inside a theme. Replayable
   from `theme_rollup` without spending a call. Needs its own contract.
3. **The stability-threshold lesson** in §2 belongs in the next contract that
   states one.
