# insider_form4_opportunistic — final_lock_2024_2026 verdict

**Verdict:** `PASS_MARGINAL` — αt=2.69 in [2.50, 3.1237); other gates pass

**Window:** 2024-01-01 → 2026-03-31 (R2000 PIT, 5 phases, stride=21d)
**Wall total:** 201.6 min (max single phase 201.5 min — parallel)

## Gates (R2000 primary, pre-reg lock)

| Gate | Metric | Threshold | Result |
|---|---|---|---|
| G1 pooled αt | +2.692 | ≥ 3.1237 (Bonferroni n=28) | ❌ |
| G2 per-phase αt floor | min=+2.39 max=+2.98 | every ≥ 1.5 | ✅ |
| G3 excess_net mean | +24.36% | ≥ 0.0% | ✅ |
| G4 dispersion (excess_net) | 7.0pp | ≤ 70.0pp | ✅ |
| G5 bounds αt lower (block-boot) | +1.371 | > 0 | ✅ |

## Per-phase results

| Phase | αt | Sh gross | Sh net | excess gross | excess net |
|---:|---:|---:|---:|---:|---:|
| 0 | +2.92 | +1.20 | +1.14 | +22.3% | +20.9% |
| 1 | +2.98 | +1.39 | +1.34 | +26.7% | +25.2% |
| 2 | +2.39 | +1.34 | +1.29 | +25.0% | +23.4% |
| 3 | +2.57 | +1.43 | +1.38 | +29.4% | +27.9% |
| 4 | +2.60 | +1.29 | +1.24 | +26.0% | +24.4% |

## Bootstrap detail (G5)

- Method: stationary block bootstrap (Politis-Romano), single-strategy CI on mean αt
- Block size: 126 trading days (daily-cadence input, native unit per v2 ledger lock)
- Resampling: synchronous_across_phases
- Reps: 1000
- Observed mean αt across phases: +2.682
- 2.5% / 97.5% bounds: +1.371 / +5.181
- Per-phase observed αt (Carhart 4F, HAC=126): +2.88, +2.96, +2.38, +2.59, +2.60

## References

- Phase A canonical: `docs/research/insider_form4_opportunistic_phase_a_2026_05_08.json`
- Pre-reg ledger: `docs/research/preregistration/ledger.json` entry `insider_form4_opportunistic_2026_05_08_v2`
