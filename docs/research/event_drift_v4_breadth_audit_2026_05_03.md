# event_drift v4 — Breadth Audit Verdict (Phase 2)

**Date pre-reg locked**: 2026-05-03
**Date executed**: 2026-05-04 (runpod RTX A4500 / EU-RO-1, post-parquet refactor)
**Pre-reg id**: `event_drift_v4_pead_quality_sp1500`
**Class**: `event_drift_search_2026_05_03` (in-class extension; 2/2 attempts after v3 ABANDONED)
**Verdict**: **FAIL — class CLOSED 2/2 ABANDONED**

## Pre-reg gates

```
min_daily_portfolio_breadth: 10 (mean across audit window)
min_daily_portfolio_breadth_p10: 5
```

## Primary attempt — locked params

Top-quintile SUE × below-median accruals × Day-1 sign confirmation,
trailing-90d cohort quantile, single-active-window invariant, ex Fin/Util,
ADV ≥ $5M, 60d holding window. Universe: **S&P 1500 PIT (FALLBACK proxy)** — pivot from
v3's R2000 per pre-committed contingency in v3 breadth-audit postmortem L121-125.

| Metric | Value | Gate | Pass |
|--------|------:|-----:|:---:|
| n_asofs (Friday strides) | 104 | — | — |
| mean_daily_breadth | 6.97 | ≥10 | ❌ |
| median | 8.0 | — | — |
| p10 | 2.0 | ≥5 | ❌ |
| p25 | 3.75 | — | — |
| p75 | 9.0 | — | — |
| p90 | 11.7 | — | — |
| min | 1 | — | — |
| max | 13 | — | — |
| n_zero_days | 0 | — | — |

Wall: 145s. Run id: `20260504-121525-ae80ed3`. Both gates fail (AND).

## Pre-reg-permitted retry (only if primary FAILs)

```
day1_sign_confirmation: DISABLED   # single-axis relaxation, --no-day1 flag
```

| Metric | Value | Gate | Pass |
|--------|------:|-----:|:---:|
| n_asofs | 104 | — | — |
| mean_daily_breadth | 15.19 | ≥10 | ✅ |
| median | 18.0 | — | — |
| p10 | 4.0 | ≥5 | ❌ |
| p25 | 9.75 | — | — |
| p75 | 21.0 | — | — |
| p90 | 22.0 | — | — |
| min | 2 | — | — |
| max | 23 | — | — |
| n_zero_days | 0 | — | — |

Wall: 149s. Run id: `20260504-122716-ae80ed3`. **mean PASSES, p10 still FAILS** — AND gate logic closes class.

## Pipeline funnel diagnosis

| Stage | Count | Drop |
|-------|------:|-----:|
| S&P 1500 PIT (FALLBACK union) | 1507 | — |
| With cached OHLCV | 1505 | 2 |
| Tickers with ≥1 announcement in window | 1482 | 23 (no companyfacts EPS) |
| Total announcements in audit window | 72856 | — |
| Skipped: no Foster SUE | — | 472 |
| Skipped: no Sloan accruals | — | 7815 (11% — vs v3 R2000 71.9%) |
| Skipped: excluded sector | — | 0 |
| Event windows built | 1487 | 8287 |
| After single-active-window invariant | 1350 | 137 |
| After Day-1 sign confirmation (primary) | 664 | 686 (~50% drop, normal) |

**Key comparison vs v3:** SP1500 accruals attrition is 11% (vs R2000 71.9%) — v3 hypothesis (universe-induced coverage gap) ✅ confirmed-and-fixed by SP1500 pivot. But cardinality gate still fails — root cause was not coverage alone, but **funnel restrictiveness** at the AND of all filters. p10=2 (primary) / p10=4 (retry) shows that even with adequate coverage, the SUE×accruals×Day-1 funnel produces sub-5-name weeks 10% of the time.

## Decision

Per pre-reg `fail_classification.breadth_collapse`:
> "retry once with sue_quartile or no Day-1 gate; if still <10 close class"

**Outcome: BOTH ATTEMPTS FAIL on AND-gate (mean ≥10 AND p10 ≥5).**

Primary: mean=6.97 ❌, p10=2 ❌ — both gates fail.
Retry (`--no-day1`): mean=15.19 ✅, p10=4 ❌ — mean clears, p10 fails. AND gate logic forces FAIL.

Strict reading of the spec: "still <10 close class" was written for v3's expected failure mode (mean axis), but pre-reg specifies BOTH `min_daily_portfolio_breadth >= 10 (mean)` AND `min_daily_portfolio_breadth_p10 >= 5`. The verdict logic in `experiment_event_drift_v4.py` ANDs them; this audit follows that contract.

**Decision**: class `event_drift_search_2026_05_03` closes 2/2 ABANDONED. Ledger updated 2026-05-04. No Bonferroni budget burned (Phase 2 GO/NO-GO triggered abort).

## Survivorship caveat

S&P 1500 PIT FALLBACK proxy uses CURRENT iShares ETF holdings (IVV/IJH/IJR) labeled with
backdated as_of. Companies that left the index between snapshot date and historical asofs
are MISSING from the universe. Estimated bias: ~150-300 bps/y on the 2-year holdout.
Documented in v4 design memo + this verdict. If holdout PASS, prospective replication
on accruing data post-2026-04-30 is not affected by this caveat.

## Diagnostic output files

Run artifacts (on runpod network volume `xymjkwj580`, EU-RO-1, persisted across pod terminations):

- `/network/results/20260504-121525-ae80ed3/` — primary attempt (manifest.json, run.log, artifacts/breadth.json)
- `/network/results/20260504-122716-ae80ed3/` — retry (`--no-day1`)

To re-fetch locally:
```sh
# Spin up a temporary pod with the volume attached, then:
scp -P <pod-port> root@<pod-ip>:/network/results /local/path -r
```

## Adversarial-review reminder

zen DISSENTED on universe (recommended S&P 600 small-cap-only over S&P 1500 — PEAD
arbitraged away in S&P 500 dilutes signal). User elected S&P 1500 for safer breadth at
cost of expected lower αt ceiling. If breadth PASS but holdout αt in [1.0, 1.8], v5 =
S&P 600 isolated to validate dilution hypothesis (per v4 design `dilution_diagnostic`).

## Outcome (2026-05-04)

VERDICT = FAIL — actions taken:

1. ✅ Marked v4 ABANDONED in ledger (`docs/research/preregistration/ledger.json`); class closes 2/2.
2. Holdout stub at `experiment_event_drift_v4.py:533` left as-is — no implementation needed (gate didn't pass).
3. Pivot to alt-data v2 holdout — pending; remains pre-registered.
4. iVolatility decision 2026-05-07: re-evaluate per `docs/research/options_provider_evaluation_2026_05_01.md`. v4 closure means no surviving Layer-1 base to compound with iVolatility ivx30 features.

## Infrastructure note (first runpod execution)

This was the first AlphaLens experiment executed on runpod (post-parquet refactor). End-to-end validation:

| Concern | Outcome |
|---|---|
| Image pull from ghcr.io (private) | OK via container registry auth |
| `bootstrap.sh` clone+uv sync | OK (after manual scp of deploy key — env-var bug to fix per task #13) |
| Parquet I/O | OK — built 1487 windows in 127s on 12 vCPU pod (Mac OOM'd before refactor) |
| `verify_data.py` | PASS on all 4 datasets |
| Wall sanity-check | Initial 0.3s headline was mis-read; actual wall 145-149s. Phase breakdown matches expected work. |
| Cost | ~$0.04 total (10 min × $0.25/h RTX A4500) for 2 audits |
