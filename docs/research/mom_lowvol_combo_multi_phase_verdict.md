# Mom+lowvol multi-phase audit — FAIL phase-robust (2026-04-29)

**Pre-registered:** `mom_lowvol_combo_2026_04_29` in signal class `price_factor_search_2026_04_29` (`docs/research/preregistration/ledger.json`).

**Bonferroni gate at registration time:** mean αt ≥ 2.39 (n=3 tests in class, α=0.05). Plus PASS rule: every-phase αt ≥ 1.5 AND every-phase excess net ≥ 0.

**Verdict: FAIL** on every dimension.

## Headline numbers

| Period | mean αt (±std) | min..max αt | mean excess net (±std) | majority sign |
|---|---:|---:|---:|---|
| IS 2011-2022 | +0.67 (±0.90) | −0.63..+1.65 | −1.1% (±17.6pp) | 3/5 negative |
| OOS 2023-2026 | +0.49 (±1.05) | −0.53..+1.66 | −5.7% (±44.5pp) | 2/5 negative |

OOS dispersion of **44.5pp** is the largest phase-aliasing magnitude observed across any AlphaLens validation to date — far exceeding the tri-factor 30-77pp range that motivated the methodology audit.

## Per-phase OOS

| Phase | αt | excess net | Sharpe net |
|---:|---:|---:|---:|
| 0 | +1.55 | +21.8% | +0.61 |
| 1 | +1.66 | +44.0% | +0.69 |
| 2 | −0.39 | −33.9% | −0.46 |
| 3 | −0.53 | −67.0% | −0.45 |
| 4 | +0.17 |  +6.5% | +0.03 |

The original 2026-04-29 strategy search reported "Sharpe net 0.55, αt 1.45" — that report sampled phase 0 only. Phases 1 looked even better (+1.66), but phases 2-3 reveal catastrophic excess returns (−33.9% and −67.0%/y net) that would dominate any practical deployment averaging across rebalance days.

## How the pre-registration framework caught this

Without pre-registration:
- Single-phase OOS Sharpe 0.55 from `momentum_lowvol_synthesis.md` looked promising.
- The natural temptation: pick parameters that look best on this one phase, declare MID candidate, move to forward-walk.

With pre-registration + multi-phase audit:
- Hypothesis was frozen 2026-04-29 with explicit Bonferroni gate (n=3 → t≥2.39).
- Audit ran all 5 phase offsets at the lock-universe + identical config.
- 3/5 phases produced excess net ≤ 0; mean αt fell from headline 1.45 to phase-distributed 0.49.
- One-shot completion via `alphalens preregister complete --verdict FAIL` cements the result; reopening with same id is rejected by the ledger.

## Class-wide picture

`price_factor_search_2026_04_29` signal class is **3/3 FAIL** after this audit:

| id | scorer | verdict | mean αt (or single) | mean excess net |
|---|---|---:|---:|---:|
| pure_momentum | 12-1m JT | FAIL | −0.50 (single) | −47% to −89% |
| pure_contrarian | 60d-DD + 5d bounce | FAIL | −0.88 (single) | −52% |
| mom_lowvol_combo | z(mom)−z(vol) | FAIL | +0.49 (5-phase mean) | −5.7% |

Reinforces the strategic pivot (`project_research_infrastructure_pivot.md` 2026-04-25): retail price-only factor strategies on AlphaLens R2000-like PIT universe do not survive phase-robust validation.

## What's NOT closed

- **mom+lowvol+ROE tri-factor variant**: separately invalidated 2026-04-29 (`tri_factor_multi_phase_verdict.md`) — also phase-robust FAIL. Both surface forms of the mom+lowvol family are now closed.
- The EDGAR companyfacts PIT TTM ROE store remains valuable infrastructure regardless.

## Action

- Pre-registration ledger updated (`alphalens preregister complete mom_lowvol_combo_2026_04_29 --verdict FAIL ...`).
- No `__status__` change needed — mom+lowvol lives as a script, not a packaged scorer.
- Memory entry: see `project_mom_lowvol_combo_failed_2026_04_29.md`.

## Audit artifact

`docs/research/mom_lowvol_combo_multi_phase_audit.json` — raw per-phase outputs + summary + verdict from `scripts/audit_multi_phase.py`.
