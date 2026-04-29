# Layer 2d variant exploration — 2026-04-28

**RESEARCH ONLY** — Layer 2d remains CLOSED for capital deployment per
`docs/research/layer2d_validation_final.md`. This report documents an
overnight follow-up experiment using the warmed parquet cluster cache
(`~/.alphalens/insider_form4.parquet/`, 94 MB, 6.5 M (ticker, asof) rows
2011-2026). Each variant was a one-line change to the ranking function
fed to the existing `BacktestEngine`.

## Question

The closeout report (2026-04-24) attributed the kill to overfit. The
existing adapter ranks by `insider_count`. The literature
(Lakonishok-Lee 2001, Cohen-Malloy-Pomorski 2012) emphasises **dollar-
weighted** insider activity. **Did we test the wrong signal encoding?**

## Method

- Same backtest harness (`BacktestEngine`, top-N=15, holding=60d signal-only,
  weekly stride=5, PIT-union universe, SPY benchmark, FF Carhart factors).
- Six variant ranking functions, all consuming the cached `(insider_count,
  aggregate_dollar)` cluster features.
- Same IS / OOS split as the original validation: IS 2011-2022 (N=603),
  OOS 2023-2026-04-22 (N=154).
- Total runtime: ~3 minutes for all 12 backtests (vs 32h for the original
  daily run).

| Variant | Score function | Filter |
|---|---|---|
| V0_count | `insider_count` | – (baseline) |
| V1_dollar | `aggregate_dollar` | – |
| V2_log_dollar_x_count | `log10(aggregate_dollar) × insider_count` | dollar > 0 |
| V3_count_ge4 | `insider_count` | `insider_count ≥ 4` |
| V4_count_ge5 | `insider_count` | `insider_count ≥ 5` |
| V5_dollar_ge_1M | `aggregate_dollar` | `aggregate_dollar ≥ $1M` |

Script: `scripts/experiment_layer2d_variants.py`.

## OOS (2023-01-01 → 2026-04-22)

| Variant | N | mean top-N | Sharpe | Carhart α | t-stat | R² | FF5+UMD α | t-stat |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| V0_count (baseline) | 154 | 13.4 | 0.42 | 21.56% | **0.68** | 0.008 | 25.95% | 0.82 |
| V1_dollar | 154 | 13.4 | 0.15 | 4.44% | 0.12 | 0.005 | 8.87% | 0.24 |
| V2_log_dollar_x_count | 154 | 13.4 | 0.34 | 16.70% | 0.51 | 0.006 | 21.62% | 0.67 |
| V3_count_ge4 | 154 | 11.4 | 0.33 | 20.01% | 0.61 | 0.010 | 24.59% | 0.76 |
| V4_count_ge5 | 154 | 8.4 | -0.03 | 4.95% | 0.14 | 0.022 | 11.11% | 0.32 |
| V5_dollar_ge_1M | 153 | 6.5 | 0.04 | 2.85% | 0.05 | 0.003 | 10.49% | 0.21 |

V0 reproduces exactly the closeout-report numbers (Carhart α=21.56%, t=0.68) — harness verified.

## IS (2011-01-01 → 2022-12-31)

| Variant | N | mean top-N | Sharpe | Carhart α | t-stat | R² | FF5+UMD α | t-stat |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| V0_count (baseline) | 603 | 10.8 | 0.96 | 103.53% | 2.14 | 0.005 | 100.14% | 2.19 |
| V1_dollar | 603 | 10.8 | 1.00 | 98.36% | **2.37** | 0.005 | 95.08% | 2.42 |
| V2_log_dollar_x_count | 603 | 10.8 | 0.99 | 110.23% | 2.21 | 0.004 | 106.85% | 2.27 |
| V3_count_ge4 | 601 | 7.8 | 0.93 | 157.96% | 1.96 | 0.004 | 152.79% | 2.02 |
| V4_count_ge5 | 577 | 5.6 | 0.58 | 198.73% | 1.41 | 0.001 | 189.51% | 1.44 |
| V5_dollar_ge_1M | 497 | 3.9 | 0.92 | 79.17% | **2.87** | 0.005 | 78.14% | 2.86 |

## Attenuation table — IS → OOS

| Variant | IS Carhart t | OOS Carhart t | t-attenuation | IS α | OOS α | α-attenuation |
|---|---:|---:|---:|---:|---:|---:|
| V0_count | 2.14 | 0.68 | **3.15×** | 103.5% | 21.6% | 4.8× |
| V1_dollar | 2.37 | 0.12 | 19.8× | 98.4% | 4.4% | 22.4× |
| V2_log_dollar_x_count | 2.21 | 0.51 | 4.3× | 110.2% | 16.7% | 6.6× |
| V3_count_ge4 | 1.96 | 0.61 | 3.2× | 158.0% | 20.0% | 7.9× |
| V4_count_ge5 | 1.41 | 0.14 | 10.1× | 198.7% | 5.0% | 39.7× |
| V5_dollar_ge_1M | 2.87 | 0.05 | **57.4×** | 79.2% | 2.9% | 27.3× |

## Findings

1. **No variant survives OOS.** Every Carhart t-stat falls between 0.05 and 0.68 — far below Bonferroni
   (n=12 tests now, t_crit≈2.50) and below even a relaxed t>1.5 regime threshold. The signal absence
   is not specific to the original ranking.

2. **More selective filters → MORE overfit, not less.** V5 (dollar ≥ $1M) had the highest IS t-stat
   (2.87, only IS spec passing Bonferroni at n=2) but the worst attenuation (57.4×). Same pattern
   for V4 cluster ≥ 5 (IS α 199%, OOS α 5%). This is a textbook fit-to-noise signature — narrow
   filters concentrate IS-specific picks that don't recur OOS.

3. **The simplest signal (V0_count) has the lowest attenuation.** Counter to ranking-quality
   intuition; consistent with "if every variant overfits, the lowest-variance estimator overfits
   least". V0's 3.15× attenuation is itself catastrophic, but it is the smallest of the six.

4. **R² ≈ 0.005 across all specs.** The Carhart factor model explains essentially none of the
   portfolio's return variance. The "alpha" is the unexplained directional drift — not factor-driven,
   not noise-cancelled by FF5+UMD either. This is consistent with the closeout-report finding that
   FF5+UMD and Q4 OOS t-stats are also <1.0 — no factor specification rescues the signal.

5. **Dollar ranking is strictly worse than count ranking** at this universe size. V1 (pure dollar)
   collapsed hardest in OOS (4% α). Hypothesis: dollar amount is dominated by a few large-cap
   transactions which carry no marginal signal beyond what insider_count already captures, and shifts
   weight away from the small-cap concentration where the original IS alpha lived.

## Conclusion

The ranking encoding is **not** the failure mechanism. Every plausible encoding of the cached
cluster signal — count, dollar, composite, threshold-tightened on either dimension — fails OOS in
the same way: factor model attribution unanimously below significance, Sharpe < 0.5, R² ≈ 0.005.

The closeout verdict (2026-04-24, KILL) is reinforced rather than weakened by this exploration.
What changed: we now have direct evidence that the IS alpha was a property of "any subset of
PIT cluster-positive stocks during 2011-2022" rather than a property of *which* clusters were
picked or *how* they were ranked. That's a stronger overfit claim than the original — a
distributional artifact of the era, not a mis-encoded signal.

## What this rules in / out for further work

**Ruled out** (no further variant testing on cached parquet warranted):
- Reranking the existing cluster set
- Tightening cluster thresholds
- Composite scores combining count and dollar

**Still untested** (would require re-scoring from `~/.alphalens/insider_form4_backup/` raw JSON,
~4.3 GB):
- Insider role filter (CEO/CFO-only) — Form 4 records contain officer titles; Cohen-Malloy-Pomorski 2012
  found the CEO subset carries the predictive content
- Cluster sells (short side) — `filter_eligible` currently keeps only buys
- Earnings-window exclusion — would require event-date overlay
- Conditional gates (only act during stock-level drawdowns; only outside earnings windows)

These remain on the candidate list IF a future investigation has a specific hypothesis. The
2026-04-25 repositioning still applies — capital deployment based on Layer 2d derivatives is
off-table; this is research/anti-pattern-cataloguing.

## Next-direction implication

The "ranking-invariant overfit" pattern observed here generalises a methodological lesson worth
flagging in `docs/research/5_paradigm_failures_postmortem.md`: when an IS alpha is large
(>50%/y) but factor R² is near zero (<0.01), variant rankings will mostly preserve the IS alpha
and uniformly fail OOS. Future Layer 2 candidates should treat low-R² × high-α IS results as a
diagnostic flag, not a finding.
