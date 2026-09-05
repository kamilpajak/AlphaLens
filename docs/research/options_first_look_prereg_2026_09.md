# Options telemetry × EDGE — pre-registration of the cluster-19 first look

**Status:** REGISTERED (frozen 2026-09-05, before any feature-vs-outcome statistic
was computed on this panel)
**Cluster:** 19 (`options_*`), §3 of [`edge_hypothesis_budget_2026_07.md`](edge_hypothesis_budget_2026_07.md)
**Looks:** 0 used, 1 charged by this study
**Run trigger:** first of (a) primary panel ≥ 60 arrival-session clusters, or
(b) 2026-12-15. See §2.
**Sunset:** 2027-03

This document upgrades the one-paragraph method note registered 2026-07-16 (§3
cluster-19 entry) into a full analysis plan, in the shape cluster 15 received
([`experts_last_look_2026_09.md`](experts_last_look_2026_09.md)). The 07-16
decisions — un-gate from `chain_quality=OK`, stratify by ATM spread, ticker-episode
unit, one look — are carried forward unchanged. Everything the paragraph left
undefined is decided here.

## 1. Why this study, why now

`options_*` has been stamped on briefs since 2026-07-06 and has never been looked
at. The registration exists because the obvious design — gate on
`chain_quality == "OK"` — was diagnosed as a trap: `OK` fires on ~3% of rows
(88 of 496 stamped as of 2026-09-05), so an OK-gated study would over-sample a
few liquid names and would not reach power until well into 2027. The 07-16
decision was to keep `chain_quality` descriptive, run across the full stamped
panel, and make the spread↔noise trade-off visible by reporting stratified by ATM
spread.

Why now: the paragraph deferred the powered look until "matured-outcome N ≥ 30 on
the full panel". That floor is met several times over (§3). Why NOT immediately:
see §2.

## 2. Timing: register now, run at the trigger

The floor being met is not the same as the study being worth its single look. At
today's 30-31 arrival-session clusters, an effect would have to be very large to
clear the program bar (α = 1.76e-4), and the realistic outcome is "not B-clear,
monitor" — where 12 of the 16 original clusters already sit. Spending the one
first look to land there is a poor trade while the panel is still accruing.

Accrual is ~1 arrival-session cluster per trading session (options are stamped on
every daily run, so essentially every session contributes one cluster once its
outcome window closes).

**Trigger, frozen:** run at the FIRST of

- the primary panel (§4) reaching **≥ 60 arrival-session clusters**, or
- **2026-12-15**, whichever comes first.

The run script must refuse to execute before the trigger without an explicit
override flag, and an override is a logged protocol deviation. Checking the
cluster count is outcome-blind and may be repeated freely.

## 3. Outcome-blind sample measurements (2026-09-05, VPS stores synced 09:18Z)

These reads happened BEFORE this registration and are disclosed here in full.
They are counts, completeness and bucket sizes only. **No feature-vs-outcome
statistic was computed.** `car_10` was evaluated solely as a computability gate
(an episode whose outcome cannot be computed is not an episode); no value was
aggregated, ranked or correlated.

Panel = `population_ladders` plannable rows, per `(brief_date, ticker)`,
arrival = `session_on_or_after(brief_date, XNYS)`, ticker-episode dedup.
Maturity frontier = newest `grouped_daily_history` session = 2026-09-03.

| slice | episodes | arrival clusters |
|---|---|---|
| all plannable, matured | 385 | 58 |
| with `options_spread_pct_atm` | 154 | 31 |
| — under the day-0-excluded anchor (§4) | 152 | 30 |

Registered spread buckets, on the matured panel:

| bucket | episodes | clusters |
|---|---|---|
| tight ≤ 30% | 73 | 27 |
| wide 30-50% | 28 | 17 |
| very wide > 50% | 53 | 24 |

Member completeness (matured, spread-present):

| member | episodes | clusters |
|---|---|---|
| `options_term_slope` | 150 | 31 |
| `options_vrp_ratio` | 149 | 30 |
| `options_skew_xzz` | 124 | 30 |

`chain_quality` across all stamped rows: THIN 405 / OK 88 / NONE 3.

## 4. Design (canonical text to be mirrored in the script docstring)

**Unit.** Ticker-episode (`ticker_episode_dedup`, chained 5-session collapse).
Clusters = arrival sessions, everywhere (OLS CR2, wild cluster bootstrap,
bootstrap CIs).

**Panel.** Plannable rows joined to `thematic_briefs` on `(brief_date, ticker)`;
`brief_date >= 2026-07-06` is automatic (first options stamp is 2026-07-06, the
discovery freeze is 2026-07-05 — the panel is held-out by construction, verified:
zero stamped rows before the freeze). Split guard [0.55, 1.8] on day-over-day
closes across the outcome window. Episodes whose window has not closed by the run
date are excluded.

**PRIMARY outcome — `car_10_ex0`, the day-0-excluded anchor.** Anchor =
`close(arrival)`, terminal = `close(arrival + 10 sessions)`, SPY leg β = 1. This
is the single most important decision in this plan, and it is specific to this
cluster.

Options are snapshotted in the post-close window of the asof session, while the
ledger's standard `car_10` anchors at `close(arrival − 1)` and its window
therefore CONTAINS the arrival session. The July 2026 adversarial review measured
day-0 overlap at ~14% of `car_10` variance and flagged every positive
momentum-like finding from this panel as suspect until the anchor moves. For
implied-volatility features the contamination is not merely additive but
mechanical and directional: an IV or skew move on day D is largely a RESPONSE to
day D's price move, which under the standard anchor sits inside the outcome. A
naive positive result here is more likely to be day-0 feedback than signal.

Measured cost of the shift: 152 episodes / 30 clusters versus 154 / 31. Two
episodes. The defence is essentially free and is therefore mandatory, not
optional.

**SECONDARY outcome — standard `car_10`** (anchor `close(arrival − 1)`, terminal
`close(arrival + 9)`), reported for comparability with every other cluster in the
ledger.

**Pre-committed artifact rule.** A member significant on the SECONDARY but not on
the PRIMARY is recorded as a **day-0 artifact**, never as a finding, and confers
no promotion. This rule is frozen here so it cannot be re-argued after seeing the
two numbers.

**Members — FAMILY = 3, frozen, never shrinks post-hoc.** The 07-16 registration
names term-slope, VRP and skew; those three are the family.

1. `options_term_slope`
2. `options_vrp_ratio`
3. `options_skew_xzz`

`options_ivx30` is **deliberately NOT a member**: it is the IV level, it is not
named in the registration, and it is the single most day-0-coupled column in the
family. It enters only as a control (below). Admitting it later requires an
amendment dated before that run, and it raises the family to 4.

**Model.** Per member: `cluster_ols` of the outcome on
`[const, member, technical_atr_pct, spread_bucket fixed effects]`, clustered on
arrival session; restricted wild cluster bootstrap two-sided p, B = 10,000,
complete-case per member. ATR is included because it is the anchor separator and
every prior sweep has found unconditioned effects to be ATR repackaging.
`options_ivx30` enters as an additional control in a pre-specified sensitivity
run, not in the primary.

**Bar.** Within-cluster family bar 0.05 / 3 ≈ **0.0167**. Program B-clear status
additionally requires raw p < **1.76e-4** on the PRIMARY outcome. Both are
reported; they are different claims and must never be conflated.

**Stratification is a REPORT, not a test family.** The three spread buckets are
reported as point estimates with cluster-bootstrap CIs per bucket, so the
spread↔noise trade-off is visible as the 07-16 decision intended. **No per-bucket
p-value is claimed and no bucket contributes to the family count.** Testing three
members × three buckets would be nine tests for a question the registration
framed as descriptive.

**Under-powered bucket rule (mirrors cluster 15's infeasible-member rule).** A
bucket below **50 episodes AND 15 clusters** is printed with its estimate and CI
under an explicit `NOT INTERPRETABLE` label. It is not merged, not dropped, and
its absence redistributes nothing. On today's counts the `wide 30-50%` bucket
(28 episodes) would carry that label; it may clear the floor by the run date.

**Scale guard — frozen, and it must be a test, not a comment.**
`options_spread_pct_atm` holds a **FRACTION** despite the `_pct` suffix (observed
range 0.03-1.96, i.e. 3%-196%), while the 07-16 paragraph writes the buckets in
percent ("≤30% / 30-50% / >50%"). Implementing the paragraph literally cuts at 30
and 50 and silently collapses every row into one bucket, producing a
single-celled "stratification" with no error and no warning. This was hit once
during the preflight for this memo. The script therefore:

- cuts at **0.30 and 0.50**;
- asserts `spread.max() <= 3.0` — fails loudly if the column ever switches to a
  percent scale;
- asserts all three buckets are non-empty before any estimate is produced.

**Population statement, frozen wording.** 154 of 385 matured episodes carry a
spread value. This study describes **candidates that have a usable options
chain**, roughly 40% of the brief population, selected on optionability. No
result may be restated as a claim about the brief population as a whole.

## 5. Verdict language (frozen, three-way)

- **CLEAR** — a member with p < 0.0167 on the PRIMARY outcome, the same sign on
  the SECONDARY, and sign stability under drop-3-clusters. Promotes cluster 19 to
  a monitored candidate. B-clear is claimed only at p < 1.76e-4 on the PRIMARY.
- **DAY-0 ARTIFACT** — significant on the SECONDARY but not the PRIMARY. Recorded
  as an artifact. The look is spent; nothing is promoted.
- **NULL** — no member clears on the PRIMARY. The cluster keeps its slot with
  1 look used, and is entitled to exactly ONE further look, at sunset (2027-03)
  or under a dated amendment; failing that it retires.

Equivalence is not claimed at this N; a null is absence of evidence, and the
results section must use that wording rather than "no effect".

## 6. Results

Placeholder — filled by a results commit that must not touch executable code.

## 7. Known limitations (frozen wording)

- **Conditional on optionability** (§4). Not a statement about all briefs.
- **THIN dominates.** 405 of 496 stamped rows are `THIN`; the study deliberately
  includes them, so mid-quote noise is inside the estimates by design. That is the
  point of the spread stratification, not a defect — but it caps how sharp any
  estimate can be.
- **Pre-registration is weaker than cluster 15's, in two ways.** First, that plan
  was frozen before any read of its panel; this one is written after two rounds of
  outcome-blind counting on the same panel (§3, fully disclosed). No outcome
  statistic was seen, but the ordering is worse. Second, cluster 15 froze an
  executable script alongside its memo, so its analysis is deterministic text;
  **this registration is prose only.** The script
  (`apps/alphalens-research/scripts/ml/2026_XX_options_first_look.py`) must be
  written and merged BEFORE the trigger fires, mirroring §4 verbatim in its
  docstring, with the §4 scale guard and bucket assertions as real assertions.
  Until it exists this plan is binding on intent but not machine-enforced, and
  that gap is the main reason the run is deferred rather than executed on the
  first day the N floor is met.
- **The day-0 defence is a shift, not a proof.** Moving the anchor removes the
  arrival session from the outcome window. It does not remove information the
  post-close snapshot may carry about the following session's open.
- **Power.** Even at the 60-cluster trigger, clearing 1.76e-4 under cluster-robust
  inference requires a large effect. The expected outcome is a NULL or a
  sub-B-clear CLEAR; the study is worth its look because the cluster is currently
  unmeasured, not because a B-clear result is likely.

## 8. Reproduce

```bash
rsync -a vault.kamilpajak.pl:.alphalens/population_ladders/ "$HOME/.alphalens/population_ladders/" \
  --exclude 'grouped/' --exclude 'bars/'
rsync -a vault.kamilpajak.pl:.alphalens/grouped_daily_history/ "$HOME/.alphalens/grouped_daily_history/"
rsync -a --exclude '_backup_pre_pr185' \
  vault.kamilpajak.pl:.alphalens/thematic_briefs/ "$HOME/.alphalens/thematic_briefs/"
```

`population_ladders/` holds price subdirectories (`grouped/`, `bars/`) beside the
top-level outcome parquets; `load_store`'s glob is non-recursive, so excluding
them is a transfer saving, not a correctness fix. Check the newest grouped session
by FILENAME — `ls -t` sorts by mtime and is misleading after `rsync -a`.

The grouped store runs structurally ~2 sessions behind the calendar: the 03:30 UTC
top-up asks Polygon for the previous session and receives
`403 NOT_AUTHORIZED "Attempted to request today's data before end of day"` on the
free tier, leaves a gap and fills it the next day. This is expected, not a stall,
and it makes the maturity frontier — and therefore the cluster count — slightly
conservative.
