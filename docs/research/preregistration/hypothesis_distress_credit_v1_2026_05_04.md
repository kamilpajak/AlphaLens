# Hypothesis: distress_credit_v1_2026_05_04

**Class:** `distress_credit_search_2026_05_04` (NEW, first in class)

**Hypothesis text (frozen):**

> A long-only equal-weighted bottom-quintile Merton-PD portfolio drawn from S&P 1500 PIT (excluding top-50 mega-caps and excluding the top-quintile-distress names always), with portfolio dollar exposure modulated by HY OAS z-score (defensive sizing during stress regimes per Frazzini-Pedersen 2014 BAB convention; floor 0.5, ceiling 1.0, linear interp between z = +1 and z = -1), produces mean Carhart-4F α t-stat ≥ 3.50 across a 5-phase OOS audit on the burnt holdout 2024-04-30 → 2026-04-30, with every-phase α t-stat ≥ 0, α t-stat dispersion ≤ 0.5 across phases, Sharpe-improvement ≥ 0.50 over a Carhart-4F-residualized SP1500 buy-hold baseline, and excess_net_ann dispersion ≤ 50pp.

**Pre-committed contingency (Phase A auto-pivot):**

If Phase A check A4-extended (correlation between HY OAS z-score and forward-21d market return on TRAIN, rolling 60-month windows) reveals that the overlay would invert (mean correlation positive OR decade-window sign drift > 0.4), then **drop Layer 4 from PRIMARY** and run pure long-only safe-decile (Layer 2 only) under the same Bonferroni threshold |t| ≥ 3.50, with Sharpe-improvement gate downgraded to "secondary descriptive" (no Layer 4 → no overlay claim).

**Bonferroni accounting:**

- In-class n = 1 (first in class), in-class threshold from function = 1.96
- Program-level n = 24 (23 prior entries + this one), naive Bonferroni = 3.08
- **PRIMARY threshold |t| ≥ 3.50** = escalated above naive program-level for meta-multiplicity (parity with event_drift_v4 escalation 3.34 → 3.50)

**Capital deployment clause:** OFF-TABLE on this burnt holdout regardless of verdict. PASS triggers prospective walk-forward replication on accruing post-2026-04-30 data at unadjusted p<0.05 single-test before any escalation.

**Design provenance:** `docs/research/distress_credit_v1_design_2026_05_04.md`
