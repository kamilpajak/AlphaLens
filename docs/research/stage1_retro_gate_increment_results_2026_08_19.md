# Stage-1 retro gate-increment study — results

**Status:** RESULTS of a pre-registered quasi-holdout (prior-adjusting, NOT confirmatory)
**Date:** 2026-08-19
**Author:** Kamil Pajak
**Pre-registration:** `docs/research/stage1_retro_gate_increment_prereg_2026_08_19.md`
(LOCKED before labeling; Stage-A commit `18b4785b`, Phase-0 amendments `56f35d96`/`0b51a90b`,
Phase-1 start amendment `51563352`, Stage-B label-hash amendment `1bfda788`)
**Analysis code:** `apps/alphalens-research/scripts/stage1_retro_outcome_inference.py`
(pure-stats functions pinned by `apps/alphalens-research/tests/test_stage1_retro_outcome_inference.py`)
**Label artifact:** `labels.parquet`, sha256
`c8440d40a0c2751717b62f3a83ef2d245957c2a43cd55fbe3405ccdb25bfb95f` — verified equal to the
Stage-B committed value before the outcome join ran (the script refuses to run otherwise).

---

## 1. Evidential rank — read this first

This is the single pre-registered test of a **quasi-holdout** study. Per pre-reg §9, the
CLEAN-window result **adjusts priors; it cannot confirm or kill the gate**: the decision
that the mapper needed an event-conditioned gate was itself reached by observing this
cohort's aggregate profile (program-level leakage that window-splitting cannot remove).
The ISO week 40-42 forward ledger remains the only confirmatory track. One Bonferroni
slot (`stage1_retro_gate_increment_clean_kept_minus_refused_2026_08_19`) was consumed by
the outcome join; the result below is logged in the ledger and cannot be re-tried with a
tweaked rubric or re-cut windows (pre-reg §8).

## 2. Primary result (H1, CLEAN window, verdict-bearing)

**H1:** pair-cluster mean matured `market_excess_return` of KEPT themes exceeds that of
REFUSED themes (one-sided, KEPT − REFUSED > 0), CLEAN window 2026-05-19..2026-06-18 minus
the three enumerated exclusion days.

**H1 is NOT supported. The point estimate is inverted.**

| Quantity | Winsorized 1%/leg (primary) | Unwinsorized (sensitivity) |
|---|---|---|
| Δ = mean(KEPT) − mean(REFUSED), pair-cluster | **−0.0715** | −0.0704 |
| One-sided bootstrap p (Δ > 0) | **0.945** | 0.940 |
| 95% CI (two-way cluster bootstrap, percentile) | [−0.159, +0.017] | [−0.158, +0.019] |
| KEPT leg: n pairs / rows / brief days | 38 / 142 / 21 | same |
| KEPT leg: pair-cluster mean (sd) | −0.0383 (0.154) | −0.0377 (0.155) |
| REFUSED leg: n pairs / rows / brief days | 49 / 145 / 23 | same |
| REFUSED leg: pair-cluster mean (sd) | +0.0333 (0.145) | +0.0327 (0.147) |

Inference: two-way (pair × brief-day) cluster bootstrap, 10,000 resamples, seed 20260819,
zero resamples skipped for an empty leg. Outcomes are stored `market_excess_return` from
the population-ladder store; matured (`terminal`) rows only.

Reading, at the pre-registered rank: on survivors of the old pipeline, the themes the
frozen Stage-1 gate keeps did **not** outperform the themes it refuses — the matured-row
point estimate runs ≈ 7 excess points the other way, though the 95% CI still grazes zero.
Per pre-reg §8 the study was powered only for a coarse positive effect; the pre-registered
one-sided test simply fails. The inverted sign is itself only prior-adjusting evidence
(same quasi-holdout ceiling, and a sign-flipped hypothesis was never registered) — it
should lower the prior that the gate's refusals are sorting returns upward, and it removes
the retro study as a justification for committing a forward-window extension (§6).

### 2.1 Attrition (pre-registered accounting)

| Window | Rows total | In contrast pairs | Matured, used | Open positions excluded | Never-matured structural (no trade setup) | INSTRUMENT_FAILURE | NO_SOURCE_EVENT |
|---|---|---|---|---|---|---|---|
| CLEAN | 367 | 361 | **287** | 3 | 71 | 4 | 2 |
| DEV | 440 | 440 | 214 | 226 | 0 | 0 | 0 |
| Excluded CLEAN days (§3.1) | 9 | 9 | 0 (out of contrast by design) | 0 | 9 | 0 | 0 |

Notes: the 71 CLEAN never-matured rows are `plannable=False` ("no trade_setup" /
`NO_STRUCTURE`) — such rows never enter the outcome population, so the contrast measures
sorting **among rows that produced a trade setup**. The 226 DEV exclusions are genuinely
open positions (`OPEN`/`PARTIAL_TP_OPEN`, mostly July briefs). The single
INSTRUMENT_FAILURE pair (CLEAN `geopolitics` × 2026-05-19 "Putin visits China", 4 rows)
and the two `NO_SOURCE_EVENT` rows were pre-committed in the Stage-B amendment.

## 3. DEV window — exploratory descriptives only (no verdict vocabulary)

Development-data window 2026-06-19..2026-08-01; the Stage-1 rule was built from this
period's post-mortems, so these numbers describe, they never judge.

- Winsorized pair-cluster means: KEPT +0.0181 (34 pairs, 83 matured rows), REFUSED
  +0.0799 (55 pairs, 131 matured rows); difference −0.0618, descriptive bootstrap
  interval [−0.151, +0.006].
- The kept-leg pair means sit below the refused-leg pair means in this window as well —
  same direction as CLEAN, on maturity-truncated data (226 of 440 rows still open).

## 4. Ticker-level secondary and crowd-out (descriptive)

Row labels among kept themes, full cohort: **KEPT_TICKER_PROPOSED 14 rows**,
**KEPT_TICKER_ABSENT 334 rows** → crowd-out share **96.0%** (CLEAN 165/8 absent/proposed;
DEV 165/5). When the frozen gate keeps a theme, it almost never re-proposes the old
brief row's mid-cap ticker.

Where the proposals go instead: the majority proposal sets (133 distinct tickers over 95
kept pairs) are headed by XOM (14 pairs), CVX (11), COP (7), GOOGL (6), PSX (6), NVDA
(6), RTX (6) — dominated by mega-caps far above the old cohort's 500M-10B bracket,
consistent with the tech_rally 2026-08-04 observation (12/12 out of bracket) that
event-conditioning pulls proposals toward the largest involved firms. Implication for
the "widen the mcap bracket" debate: the retro replay says the gate's kept themes carry
proposal sets that mostly cannot ship under the current bracket; it does NOT say those
proposals would have earned returns (no outcome join exists for non-briefed tickers —
one-sided selection, pre-reg §1).

## 5. Refusal-reason taxonomy and fabricated-channel spot notes (descriptive)

Heuristic ordered-keyword classification (`classify_refusal_reason`, unit-tested;
categories: non-event → direction-filter → no-channel → other) over 718 decline calls,
aggregated to a per-pair majority across the 189 pairs with at least one decline:

| Bucket | Pairs | Share |
|---|---|---|
| no_channel (no transmission channel / no beneficiary / not material / not US-listed) | 111 | 58.7% |
| other (mostly "no actionable specifics" phrasings the keyword rules leave unclaimed) | 40 | 21.2% |
| non_event (commentary / forecast / round-up / vague headline) | 36 | 19.0% |
| direction_filter (adverse event; refuses to long the harmed name) | 2 | 1.1% |

Manual read of the `other` examples shows they are largely no-channel/non-event blends
("discussion with no concrete business action", "headline is a metaphor"), so the
no-channel share is a floor. The direction filter (the LYFT/Patriot anchor behavior)
exists but is rare on this cohort's survivors. This decomposition is consistent with the
2026-08-03 role-classifier finding (~65% no-channel) without re-measuring it.

**Fabricated-channel spot notes** (qualitative, from the raw kept responses; the
AVAV/KTOS failure mode — an invented transmission mechanism instead of a refusal — is
visible at low frequency):

- `AI ethics` × FBI-deepfakes explainer → VERI: "FBI agent's explanation will increase
  awareness and demand for AI porn detection tools … higher revenue from software
  licenses" — an awareness→sales chain conjured from a non-event.
- `AI_safety` × states-sue-OpenAI → BAH: "increased government demand for third-party AI
  safety audits and red-teaming → Booz Allen's AI consulting revenue grows" — plausible
  wording, no factual basis in the event.
- `Artificial Intelligence` × OpenAI-IPO commentary → NDAQ: "would likely occur on
  Nasdaq, generating listing fees" — a hypothetical listing monetized into a channel.

These stay spot notes: KEPT-vs-REFUSED returns cannot detect a right-ticker-wrong-
mechanism hold (pre-reg §1), and no rate is claimed.

## 6. Phase-3 power memo — ISO 40-42 forward window

**Measured inputs (this study):** pair-level refusal rate 142/237 = **59.9%** (CLEAN
64/111 = 57.7%); CLEAN pair-cluster spread magnitude **|Δ| = 0.0715**; pooled pair-level
sd **0.149**. Design effect for the power model = the spread **magnitude** — necessarily
so, because the observed CLEAN point estimate is *negative*: if the true effect equals
the retro estimate, a one-sided KEPT > REFUSED test never rejects at any window length.
Accrual: post-deploy candidate volume runs ~3-4 candidates/day; at the cohort's measured
3.43 rows per (theme, source_event) pair this is ≈ **1.0-1.2 pairs/day** entering a
two-leg contrast, split by the measured refusal rate (~40% kept / ~60% refused). Normal-
approximation one-sided two-sample power at α = 0.05 (helper functions unit-tested):

| Accrual | Window | n kept / refused pairs | Power, full effect (0.0715) | Power, half effect (0.0358) |
|---|---|---|---|---|
| 1.0 pair/day | ISO 40 read (~40 trading days from 08-03 deploy) | 16 / 24 | ~0.44 | ~0.19 |
| 1.0 pair/day | ISO 42 read (~54 trading days) | 22 / 32 | ~0.53 | ~0.22 |
| 1.0 pair/day | 90 trading days (~ISO 2026-W52) | 36 / 54 | 0.72 | 0.30 |
| 1.0 pair/day | 135 trading days (~ISO 2027-W07) | 54 / 81 | **0.86** | 0.39 |

Balanced-legs requirement for 80% power: **54 pairs per leg** for the full-magnitude
effect; **215 per leg** for half of it. At ~1 pair/day and a 40% kept share, the kept leg
is binding: 80% power for the full magnitude needs ≈ **135 accrued pairs ≈ 27 trading
weeks**, i.e. a read no earlier than ≈ **ISO 2027-W07** — roughly a four-month extension
past ISO 42. The half-magnitude effect needs ≈ 540 pairs ≈ 2+ years: **infeasible**.

**Instrument caveat (assumption of the model):** a forward KEPT-vs-REFUSED contrast
requires outcomes for *refused* themes, which post-deploy produce no brief rows and no
ladder entries. The forward ledger as designed reads the deployed cohort's own EDGE
profile; the two-leg power model above applies only if refusal shadow-tracking (logging
refused themes' would-be candidates into the population monitor) is built. Without it,
the ISO 40-42 read is not a KEPT-vs-REFUSED test at all.

**Recommendation:** do **not** pre-register a forward-window extension on this study's
account. The pre-reg (§12.3) conditioned an extension on chasing "the CLEAN-estimated
effect"; the CLEAN estimate came out inverted, so there is no positive retro effect to
power for, and buying 27 weeks of accrual to 80%-power a magnitude whose observed sign
is negative is not a justified spend of a forward slot. Let ISO 40-42 proceed as already
registered — the deployed cohort's own EDGE-profile first look with its counter restarted
at the 2026-08-03 deploy — and treat any future KEPT-vs-REFUSED forward test as a new
design requiring refusal shadow-tracking infrastructure first.

## 7. Reproducibility

```
python apps/alphalens-research/scripts/stage1_retro_outcome_inference.py \
    --labels <labels.parquet> \
    --outcomes <ladder_outcomes.csv> \
    --calls-log <phase1_calls.jsonl> \
    --out <results.json>
```

Outcome extract: read-only pull of `~/.alphalens/population_ladders/<brief_date>.parquet`
(brief dates 2026-05-19..2026-08-01; columns brief_date, ticker, plannable,
nonplannable_reason, terminal, matured_at, market_excess_return, …) joined 1:1 on
(brief_date, ticker) — 816/816 label rows joined. Bootstrap seed 20260819; full numeric
output in the study scratch artifact `results.json`. Labeling provenance (100% pinned
provider `Alibaba` / `served_model=deepseek/deepseek-v4-pro`) per the Stage-B amendment.
