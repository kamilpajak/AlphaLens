# Layer 2d random-subset null distribution — 2026-04-28

**RESEARCH ONLY.** Empirically tests whether the V0_count baseline IS α (103.5%/y, t=2.14)
is rank-driven or set-distributional. Per-rebalance, instead of selecting top-15 by
``insider_count``, sample 15 names uniformly at random from the cluster-positive
candidate pool. Repeat K=100 times → null distribution of Carhart α and t-stat.

If V0 sits near the null median, ranking added no information beyond "this stock
appeared in the cluster-positive set". If V0 lives in the upper tail, ranking
genuinely picked above-average names.

- Top-N: 15
- Rebalance stride: 5 (weekly)
- N trials: 100 (seed 17, uniform without replacement per rebalance)
- Universe: PIT union (1403 IS / 1536 OOS)
- Script: `scripts/experiment_layer2d_random_null.py`


## Null distribution — IS_2011_2022

- N trials: 100
- Top-N per rebalance: 15
- Rebalance count: 603
- Sampling: uniform without replacement from cluster-positive set per rebalance

### Distribution quantiles

| Metric | min | p05 | p25 | median | p75 | p95 | max | mean | std |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Carhart α (ann) | 64.95% | 70.18% | 75.42% | 79.43% | 82.46% | 89.97% | 95.62% | 79.48% | 5.90% |
| Carhart t-stat | 2.06 | 2.19 | 2.29 | 2.34 | 2.40 | 2.50 | 2.59 | 2.34 | 0.10 |
| Sharpe | 0.81 | 0.86 | 0.91 | 0.94 | 0.97 | 1.03 | 1.09 | 0.94 | 0.05 |
| R² | 0.0045 | 0.0057 | 0.0068 | 0.0079 | 0.0090 | 0.0108 | 0.0162 | 0.0080 | 0.0018 |

### V0 baseline percentile within null

| Metric | V0 baseline | Null median | Null p95 | V0 percentile in null |
|---|---:|---:|---:|---:|
| Carhart α (ann) | 103.53% | 79.43% | 89.97% | 100th |
| Carhart t-stat | 2.14 | 2.34 | 2.50 | 3th |
| Sharpe | 0.96 | 0.94 | 1.03 | 64th |


## Null distribution — OOS_2023_2026

- N trials: 100
- Top-N per rebalance: 15
- Rebalance count: 165
- Sampling: uniform without replacement from cluster-positive set per rebalance

### Distribution quantiles

| Metric | min | p05 | p25 | median | p75 | p95 | max | mean | std |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Carhart α (ann) | -10.70% | -2.17% | 6.54% | 13.09% | 18.28% | 26.28% | 34.43% | 12.40% | 8.78% |
| Carhart t-stat | -0.33 | -0.07 | 0.21 | 0.40 | 0.57 | 0.82 | 1.15 | 0.39 | 0.28 |
| Sharpe | 0.02 | 0.14 | 0.29 | 0.40 | 0.49 | 0.61 | 0.76 | 0.39 | 0.15 |
| R² | 0.0018 | 0.0028 | 0.0046 | 0.0058 | 0.0078 | 0.0107 | 0.0146 | 0.0064 | 0.0027 |

### V0 baseline percentile within null

| Metric | V0 baseline | Null median | Null p95 | V0 percentile in null |
|---|---:|---:|---:|---:|
| Carhart α (ann) | 21.56% | 13.09% | 26.28% | 88th |
| Carhart t-stat | 0.68 | 0.40 | 0.82 | 88th |
| Sharpe | 0.42 | 0.40 | 0.61 | 57th |

## Synthesis

| Metric | IS null median | IS V0 percentile | OOS null median | OOS V0 percentile | IS→OOS attenuation (null median) |
|---|---:|---:|---:|---:|---:|
| Carhart α (ann) | 79.4% | 100th | 13.1% | 88th | 6.1× |
| Carhart t-stat | 2.34 | **3rd** | 0.40 | 88th | 5.85× |
| Sharpe | 0.94 | 64th | 0.40 | 57th | 2.35× |

### Findings

1. **The IS α was set-distributional, not rank-driven.** Random picks from the
   cluster-positive set delivered Carhart α between 65% and 96% (median 79%) with
   t-stat between 2.06 and 2.59 (median 2.34) — virtually all 100 trials nominally
   significant at t>2. Membership in the cluster-positive set during 2011-2022 was
   itself a ~80%/y unexplained-α signature, irrespective of how a portfolio of
   15 names was chosen from it.

2. **V0_count ranking was statistically WORSE than random.** V0's t=2.14 sits at
   the 3rd percentile of the null t-distribution — 97% of random subsets gave
   higher t-stats. V0 produced higher α (103.5% > null max 95.6%) but also higher
   variance, dragging the t-stat below the random average. Ranking by
   `insider_count` concentrated the portfolio into volatile names without
   improving directionality.

3. **The "set effect" collapsed OOS too.** Null median α dropped from 79% (IS)
   to 13% (OOS), and null median t from 2.34 to 0.40 — a 5.85× attenuation
   nearly identical to V0's own 3.15× attenuation. The set-level effect did not
   generalize past 2022. V0's higher OOS percentile (88th α and t) is luck of
   the variance-direction, not signal.

4. **R² stays near zero across both nulls and V0** (median 0.008 IS, 0.006 OOS).
   The "α" is purely residual directional drift of the candidate-positive set;
   no factor specification absorbs or explains it. Consistent with the diagnostic
   flag from `layer2d_variants.md` §next-direction.

### Implications

- **Layer 2d KILL verdict reinforced one more level.** The closeout report
  attributed failure to overfit at the ranking level. The variant exploration
  showed every ranking failed identically. This null test demonstrates the
  set-membership signal itself failed OOS — there is no salvageable layer in
  the cluster-positive pool, regardless of selection scheme.

- **Methodological lesson, formalised.** Before declaring a screener significant,
  run the random-subset null on its candidate pool. If the null median t-stat
  is ≥ 2 (i.e., the candidate set itself "passes" significance), the ranking
  is providing zero edge — the apparent α is set-driven and likely won't
  generalize. This was previously a pattern hypothesis; this experiment confirms
  it empirically for Layer 2d.

- **Generalises beyond Layer 2d.** The diagnostic test is universe-agnostic:
  given a candidate pool and 1-day forward returns, K=100 random-subset trials
  cost minutes. Should be added to the standard Phase 3 backtest workflow as
  a pre-OOS gate. If V0 sits below the 95th percentile of the null t-stat,
  the screener provides no rank-quality value and should not proceed to OOS.

### Caveat — tail risk in null distribution

Even though IS null median t=2.34 looked nominally significant, when applied
OOS the median dropped to 0.40 — meaning the 2.34 IS finding was itself a
distributional artifact of the era, not evidence of a stable cross-section.
The lesson: a "passing" null distribution test on IS does not guarantee any
OOS robustness; it only certifies that ranking adds no value. Set-level
artifacts still have to be ruled out independently (e.g., by checking factor
R², regime breakdown, sample-period stability).
