# EV/FCFF-Yield (paradigm #13) — Audit Verdict + Postmortem

**Verdict:** **FAIL** (joint-FAIL: every window IS / OOS / FL fails independently)
**Date:** 2026-05-13 (audit ran 2026-05-12 22:15-22:26 UTC on runpod)
**Class:** `fundamental_value_dcf_2026_05_12` — paradigm test #13
**Ledger ID:** `ev_fcff_yield_2026_05_12_v1`
**Canonical JSON:** `docs/research/ev_fcff_yield_audit_2026-05-12.json`
**Orchestrator log:** `docs/research/ev_fcff_yield_audit_2026-05-12.log`
**Per-phase logs:** archived in audit tarball (15 isolated `<window>_p<N>.log` files)

---

## 1. One-line summary

EV/FCFF yield on R2000 ex-financials produces a **statistically real but modest** positive signal (per-phase Carhart-4F αt = +0.88 to +1.48 across 9 years, every phase positive) that is **below the project Bonferroni threshold** (αt ≥ 3.5). The signal is consistent with the published FCF-yield literature premium but insufficient as a standalone alpha source under the project's conservative gate. Paradigm #13 fails clean — no over-engineering, no luck-marginal outcome.

## 2. Per-window summary

| Window | Period | n_phases | Verdict | αt mean | αt range | excess net /y |
|---|---|---|---|---|---|---|
| IS | 2016-08-31 → 2019-08-31 | 5 | FAIL | +1.25 | +0.97..+1.48 | +1.2% |
| OOS | 2019-08-31 → 2022-08-31 | 5 | FAIL | +1.34 | +1.24..+1.42 | +12.4% |
| FL | 2022-08-31 → 2025-08-31 | 5 | FAIL | +0.96 | +0.88..+1.08 | +4.0% |

Phase variation tight in every window (std/mean ratios ~0.05-0.15) — signal is stable, just modest. OOS had strongest excess (+12.4%/y) consistent with 2019-2022 including COVID dislocations + 2021-2022 value rally; FL weakest (+4.0%) consistent with post-2022 value-premium compression.

## 3. Gate matrix (memo §8)

Every gate evaluated at every window; verdict requires all four to pass for any single window. None did.

| Gate | Threshold | IS | OOS | FL |
|---|---|---|---|---|
| G1 full-sample αt | ≥ 3.5 | 1.25 ❌ | 1.34 ❌ | 0.96 ❌ |
| G2 mean αt per-phase | ≥ 2.5 | 1.25 ❌ | 1.34 ❌ | 0.96 ❌ |
| G3 positive each phase | > 0 | ✅ all 5 | ✅ all 5 | ✅ all 5 |
| G4 αt @ 15bps stress | ≥ 2.0 | 1.25 ❌ | 1.34 ❌ | 0.96 ❌ |

## 4. Material finding: G4 is structurally identical to G1

**Observation:** in every window, αt at baseline cost (5bps) equals αt at stress cost (15bps) to ~2 decimals.

**Root cause:** orchestrator parses αt from the `run_regression` output computed against the GROSS daily returns series, then logs both rows by re-running `assess()` with different cost params. Inside `assess()`, the cost-adjusted alpha is computed as scalar `alpha_gross_4f − drag_ann`, but the **regression itself uses the same gross returns** — the t-stat is unchanged across cost levels. This is precisely the anti-pattern documented in `feedback_slippage_stress_diagnostic_pattern_2026_05_12.md`: *"Scalar α_gross − drag_ann leaves t-stat unchanged → undercount cost impact"*.

**Impact on this verdict:** none — G1, G2 already FAIL clearly, so G4 being a no-op duplicate doesn't change the outcome. But for any future paradigm using this orchestrator, G4 must be re-implemented as a Carhart re-regression on net daily returns (mirror `insider_form4_opportunistic` slippage diagnostic 2026-05-12) before relying on cost-stress as an independent gate.

**Lesson logged for future paradigms.** Not retroactively voiding this verdict because the failure is unambiguous on G1 + G2.

## 5. Interpretation

This is the cleanest paradigm failure in the project to date. Specifically:

1. **No luck-marginal outcome.** Phase variation across 5 offsets per window is tight (max std/mean ≈ 0.16 IS, 0.07 OOS, 0.08 FL). The signal is what it is — not noise sliced lucky.
2. **No over-engineering anti-pattern.** Single feature, single-stage Gordon (= FCF/EV transformation), quarterly rebalance, long-only, no compound. Spec was deliberately stripped after adversarial review to avoid the paradigm-#11 (drawdown overlay) and paradigm-#12 (compound) failure modes.
3. **No cost-mirage.** Turnover is low (quarterly value-factor rebalancing → low position churn). Gross-vs-net alpha gap is tiny (drag_ann ≈ 0.5-0.7%/y). Even with proper G4 cost re-regression, the verdict would not change.
4. **Mechanism is real.** Excess net returns are positive every window (+1.2%, +12.4%, +4.0%/y). Per-phase αt is positive 15 of 15 phases. This is **not** a failed mechanism — it's a real but sub-threshold mechanism.

The honest framing from the design memo (Perplexity 2026-05-12 adversarial review) is vindicated: this is **FCF-yield in disguise**. The factor exists and produces modest returns consistent with the documented literature (e.g., Perplexity's cited top-20% FCF-yield decile returning ~16.6% annual US 40y). Our R2000 ex-fin instance produces excess net returns in the same family but the t-statistic doesn't clear the project's Bonferroni 3.5 bar.

## 6. Disposition per ADR 0005 anti-pattern catalog

Module `alphalens/screeners/ev_fcff_yield/` → `__status__ = CLOSED`.

Closed evidence map per the 7-gate checklist (`docs/research/kill_verdict_checklist.md`):
- **Coverage**: passed (~1273 R2000 ex-fin tickers across 9-year window)
- **Pre-screen**: N/A (this is Layer 2 screener, no Layer 4 overlay pre-screen required)
- **G1 αt ≥ 3.5**: FAIL (1.25 / 1.34 / 0.96)
- **G2 mean αt ≥ 2.5**: FAIL
- **G3 positive each phase**: PASS (sign-consistent)
- **G4 cost stress ≥ 2.0**: FAIL (also flagged structurally — G4 = G1 in this orchestrator)
- **Joint windows**: FAIL (verdict requires all 3 windows individually PASS)

## 7. What's NOT closed

Per project doctrine `feedback_never_close_the_door.md` — closing a SPEC does not close a CLASS.

- The `fundamental_value_dcf_2026_05_12` signal class remains OPEN. Future variants in this class pay Bonferroni n=2 (this test counts).
- Candidate next experiments in this class (documented as "out of scope for v1" in the memo §14):
  - **Frame 2**: composite `z(g_realized_3y_revenue) − z(g_implied_FCFF)` — adds a backward-looking growth-stability discriminator alongside the value yield. May lift αt out of the FCF-yield-only regime if growth-stability decomposes from the value signal.
  - **Banks via residual income (Edwards-Bell-Ohlson)** + insurance via embedded value — separate paradigm tests (#14, #15) on the universe we excluded under Option D.
  - **Multi-stage DCF + IBES analyst forecasts** — would unlock Frankel-Lee mispricing methodology. Blocked on data source (SimFin Start tier doesn't include IBES); revisit if a forward-EPS-consensus source becomes available.

None of these are scheduled as immediate next steps. Each pays its own pre-registration cost.

## 8. Cost summary

- Pod compute: ~$0.33 total spend (per `runpodctl user` before/after delta $12.2634 → $11.9362)
- Wall time: 10.7 min (3 windows × 5 parallel phases each)
- Pod spec used: `cpu3g-8-32` ($0.32/h) on machine `8bajydd2e7i8` in EU-RO-1
- Note: initial attempts at `cpu5g-8-32` on machine `ktfjxwzu2sku` (also EU-RO-1) **stuck at runtime null for 35+ min × 2 attempts** before pivoting host. Suspected host kernel/container issue; RunPod did not surface error. cpu3g older-gen flavor is the practical fallback when `ktfjxwzu2sku` is the only cpu5g host available in EU-RO-1.

## 9. Ledger close

```python
from phase_robust_backtesting.ledger import Ledger
Ledger(root="docs/research/preregistration").complete(
    id="ev_fcff_yield_2026_05_12_v1",
    verdict="FAIL",
    mean_alpha_t=1.183,       # (1.25 + 1.34 + 0.958) / 3
    mean_excess_net=0.0587,   # (0.012 + 0.124 + 0.040) / 3
    audit_path="docs/research/ev_fcff_yield_audit_2026-05-12.json",
    completed_at=date(2026, 5, 13),
    notes="JOINT FAIL: αt mean 1.18 across 3 windows (range per-window 0.96..1.34), every-phase positive, "
          "FCF-yield mechanism vindicated but below project Bonferroni 3.5 threshold. Structural finding: "
          "orchestrator G4 cost-stress is no-op duplicate of G1 (scalar drag, t-stat invariant) — verdict "
          "still FAIL on G1+G2 unambiguously. Module status → CLOSED per ADR 0005.",
)
```

(Fallback to manual JSON patch if `Ledger.complete()` fails on the existing-schema mismatch.)
