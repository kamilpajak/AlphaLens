# Catalyst extraction confidence is source-biased — measurement memo

**Date:** 2026-07-16
**Status:** DRAFT — no decision now; options deferred to the August catalyst re-verification.
**Scope:** descriptive measurement of LLM extraction `confidence` by news source, and how it leaks into `compute_catalyst_strength` / `catalyst_floor`. No formula change, no config change, no new hypothesis-budget look is taken by this memo.

---

## 1. Finding — confidence is systematically source-dependent

LLM extraction confidence (flash model only) over the window 2026-06-15..2026-07-14, from the VPS thematic stores (4,994 news items / 6,410 extracted events):

| Source | Mean confidence | Median confidence |
|--------|-----------------|-------------------|
| polygon | 0.879 | 0.90 |
| perplexity | 0.845 | 0.90 |
| edgar_press_release | 0.804 | 0.935 |
| rss | 0.754 | 0.85 |
| gdelt | 0.541 | 0.60 |

Two additional facts:

- The deterministic template path stamps `confidence = 1.0` and is effectively EDGAR-only — it covers 27% of EDGAR events. So the EDGAR distribution is a mixture of a hard-coded 1.0 mass and a flash-scored remainder.
- Body length does NOT explain the ordering: EDGAR full text scores below the 451-char Polygon description. The real driver is title-only vs snippet input (GDELT's deficit of −0.2..−0.3 is the title-only penalty) plus content genre.

## 2. Why it matters

`compute_catalyst_strength` weights `confidence` at 40%, and `catalyst_floor` converts strength into a 0/1/2 scoring bonus at thresholds 0.45 / 0.70. With a ~0.34 mean-confidence gap between Polygon and GDELT, the catalyst bonus partially measures WHICH FEED an item arrived from, not catalyst quality. Two tickers with the same underlying catalyst can land on different sides of the 0.45 floor purely because one was picked up by GDELT and the other by Polygon.

The bias is not confined to the confidence term: `second_order_implications` (20% of strength) also tracks genre — Polygon items average 0.92 SOI per event vs 0.36 for EDGAR flash-scored events. So roughly 60% of the strength formula carries some source signature.

## 3. Options for August (no decision now)

1. **Per-source confidence normalization** — z-score or rank `confidence` within each source before it enters the strength formula, so the term measures within-feed relative confidence instead of the feed identity.
2. **Explicit source prior** — keep raw confidence but add a per-source calibration offset (or multiplier) as a named, versioned config, making the feed effect visible and tunable rather than implicit.
3. **Keep the formula unchanged** — rely on the new `catalyst_config_version` stamp (separate PR in flight) to segment cohorts, and let the forward EDGE panel answer whether the source bias correlates with outcomes before touching anything.

## 4. Constraint

Any formula change is a tuning decision against outcomes and therefore MUST be paid for as a pre-registered look in the hypothesis-budget ledger (`edge_hypothesis_budget_2026_07.md`) AND accompanied by a `catalyst_config_version` bump so pre/post cohorts never pool silently. Today's evidence for acting is suggestive only: confidence vs car_10 rho +0.14, sub-Bonferroni. This memo records the measurement so the August decision starts from stamped numbers, not memory.
