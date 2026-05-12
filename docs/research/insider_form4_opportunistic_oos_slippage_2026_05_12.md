# insider_form4_opportunistic — oos_slippage_2026_05_12 verdict

**Verdict:** `PASS_MARGINAL` — αt=2.71 in [2.50, 3.1237); other gates pass

**Window:** 2018-01-01 → 2023-12-31 (R2000 PIT, 5 phases, stride=21d)
**Wall total:** 84.0 min (max single phase 83.7 min — parallel)

## Gates (R2000 primary, pre-reg lock)

| Gate | Metric | Threshold | Result |
|---|---|---|---|
| G1 pooled αt | +2.710 | ≥ 3.1237 (Bonferroni n=28) | ❌ |
| G2 per-phase αt floor | min=+2.48 max=+2.93 | every ≥ 1.5 | ✅ |
| G3 excess_net mean | +17.68% | ≥ 0.0% | ✅ |
| G4 dispersion (excess_net) | 1.3pp | ≤ 70.0pp | ✅ |
| G5 bounds αt lower (block-boot) | +1.539 | > 0 | ✅ |

## Per-phase results

| Phase | αt | Sh gross | Sh net | excess gross | excess net |
|---:|---:|---:|---:|---:|---:|
| 0 | +2.61 | +0.87 | +0.82 | +18.6% | +17.1% |
| 1 | +2.81 | +0.93 | +0.88 | +19.9% | +18.4% |
| 2 | +2.72 | +0.90 | +0.85 | +18.6% | +17.2% |
| 3 | +2.93 | +0.94 | +0.89 | +19.6% | +18.2% |
| 4 | +2.48 | +0.92 | +0.87 | +18.9% | +17.5% |

## Bootstrap detail (G5)

- Method: stationary block bootstrap (Politis-Romano), single-strategy CI on mean αt
- Block size: 126 trading days (daily-cadence input, native unit per v2 ledger lock)
- Resampling: synchronous_across_phases
- Reps: 1000
- Observed mean αt across phases: +2.676
- 2.5% / 97.5% bounds: +1.539 / +4.198
- Per-phase observed αt (Carhart 4F, HAC=126): +2.55, +2.72, +2.70, +2.92, +2.48

## References

- Phase A canonical: `docs/research/insider_form4_opportunistic_phase_a_2026_05_08.json`
- Pre-reg ledger: `docs/research/preregistration/ledger.json` entry `insider_form4_opportunistic_2026_05_08_v2`
