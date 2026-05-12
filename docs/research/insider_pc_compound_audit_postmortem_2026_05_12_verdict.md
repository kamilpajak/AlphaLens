# Postmortem — insider_pc_compound audit verdict 2026-05-12

**Status:** LOCKED, JOINT FAIL.
**Pre-reg ledger:** entry `insider_pc_compound_2026_05_10`, outcome appended 2026-05-12.
**Module status change:** `alphalens/screeners/compound_insider_pc/__status__` → CLOSED per ADR 0005 (anti-pattern catalog).

---

## Verdict (per memo §5.1)

| Window | mean αt | excess_net/y | dispersion | RW bounds | Gate trip | Verdict |
|---|---:|---:|---:|---|---|---|
| OOS 2018-2023 | **-0.034** | +3.68% | 3.0pp | [-2.18, +1.47] | G1 (αt < 2.50) | **FAIL** |
| Final-Lock 2024-2026 | +0.674 | **-0.28%** | 3.8pp | [-1.40, +3.80] | G3 (excess_net < 0); G1 also trips | **FAIL** |

Memo §5.1 explicit rule *"FAIL on either window"* → **JOINT FAIL**. Both windows reject independently — the strongest available rejection class in the matrix. Capital-deploy stays off-table; this verdict cements what project policy already prohibited.

---

## Per-phase detail

**OOS** (5 phases, monthly stride 21d, 72 rebalances/phase):
```
phase  alpha_t  excess_net  Sharpe_net
    0   +0.36     +4.9%       +0.38
    1   +0.46     +4.8%       +0.35
    2   -0.52     +2.3%       +0.27
    3   +0.34     +4.5%       +0.35
    4   -0.81     +1.9%       +0.27
mean  -0.034     +3.68%       +0.32
```
Per-phase signs FLIP across phases (3 positive, 2 negative). Phases 2 and 4 deliver outright negative residual α. No phase comes within 0.6σ of the 2.5 PASS_MARGINAL floor, let alone the 2.974 Bonferroni floor.

**Final-Lock** (5 phases, monthly stride 21d, 27 rebalances/phase):
```
phase  alpha_t  excess_net  Sharpe_net
    0   +0.81    -0.9%       +0.43
    1   +0.20    -2.8%       +0.40
    2   +0.53    +0.5%       +0.53
    3   +0.85    +0.8%       +0.55
    4   +0.98    +1.0%       +0.52
mean  +0.674    -0.28%       +0.49
```
All phases positive αt — but all below 1.5 (the PASS floor) and well below 2.974 (Bonferroni). G3 fails because tiny gross alpha is consumed by 5bps transaction costs. Romano-Wolf CI [-1.40, +3.80] straddles 0 — even bootstrap-adjusted inference cannot reject αt=0.

---

## Why it failed (mechanism)

Memo §7 §1 and §5 pre-predicted both modes empirically:

### §7 risk #1 — Time-domain correlation despite cross-sectional orthogonality

Pre-screen #1 (memo §3.5) measured cross-sectional Spearman ρ = -0.000035 between component scores on IS 2014-2017 — decisively orthogonal at the signal level. The naive expectation: ~√2 Sharpe-lift from two independent signals.

Memo §7 #1 already flagged the failure of that naive expectation:
> *"Cross-sectional ρ ≈ 0 (verified) does NOT translate to time-series portfolio return independence. Portfolio RETURNS may be highly positively correlated even when SCORES are orthogonal, because both portfolios are FAT in the same vol regimes (Q4-Q5) and FLAT in others (Q1-Q2)."*

Both component bases are EXTREME counter-cyclical: insider_form4 (PR #88, Q5/Q1 dispersion +95%/y), pc_abnormal (memo §3.5 pre-screen #2, R_sharpe=-2.34). They fire alpha in the SAME market-vol regime. Equal-weighted z-score average of two regime-coincident portfolios isn't √2-better — it's the same portfolio.

The audit's observed mean Sharpe_net (~0.32 OOS, ~0.49 FL) is in line with the components' standalone Sharpe (~0.4 form4, ~0.4 P/C). No lift.

### §7 risk #5 — Selection-bias amplification per Blume-Easley 2018

Components had standalone αt clustered just under Bonferroni: form4=+2.71 (PASS_MARGINAL), P/C=+2.65 (INCONCLUSIVE). Per Blume-Easley 2018, signals clustered at the marginal threshold are 60-70% signal / 30-40% luck. Compounding two luck-marginal signals does NOT yield a √2 jump — the luck components can be REALIZATION-CORRELATED in-sample (both got lucky in the same historical realization) without being structurally correlated.

Memo §7 #5 set the mitigation explicitly:
> *"pre-registered minimum combined OOS αt floor of 2.5 (PASS_MARGINAL boundary in §5.1). If compound misses αt ≥ 2.5 on final lock, file each component separately and archive the compound design as a research artifact."*

The audit missed αt ≥ 2.5 on BOTH windows (OOS by 2.53σ, FL by 1.83σ). The pre-registered mitigation path is engaged.

---

## Verification (8-angle + zen external sign-off)

| Angle | Source | Result |
|---|---|---|
| Arithmetic recompute (mean αt, excess_net, dispersion) | JSON `per_phase` → `gates` | ✓ exact match to 6 decimals |
| Phase wall consistency stddev (%/mean) | JSON `per_phase.wall_seconds` | ✓ OOS 0.8%, FL 1.5% — no silent stalls |
| Per-phase α-variance (signs differ) | JSON `per_phase.alpha_t` | ✓ rules out empty-universe / constant-zero bugs |
| Bootstrap αt directional agreement (parsed vs daily-cont) | JSON `bootstrap.alpha_t_per_phase_observed` | ✓ all 10 phases sign-consistent |
| Code identity at audit time | git: `compound_insider_pc/zscore_compound.py` last touched ebf87f2 = memo lock date | ✓ no drift |
| Hash guard (components SHA256) | PR #95 guard logged "GUARD OK" at audit start | ✓ verified |
| Compound formula matches memo §3.1 | source review (`_xsec_zscore` ddof=1, strict intersection, equal-weight) | ✓ |
| Memo §A0 coverage ≥30%×≥50 tickers | indirect (memo §3.5 pre-verified on IS 100% mean 154; post-cliff OOS/FL ≥ IS; wall+variance rule out degenerate cases) | ✓ inferred |
| Memo §7 #1 + #5 predicted this failure mode | memo source | ✓ explicit pre-registration |
| External validation | zen (gemini-3-pro-preview) 2026-05-12 | ✓ "finalize-able verdict; system worked exactly as designed" |

---

## Operational learnings (lock today)

### What worked

1. **Custom orchestrator + hard-locked rebalance stride** (PR #98) caught and prevented two methodology drifts (generic CLI driver conflating phase-count with day-step; missing synchronous block-bootstrap). Cost prevented: ~$8/audit recurrence + invalid verdict.
2. **Component hash guard** (PR #95) eliminated mid-audit silent code-drift risk on `opportunistic_form4.py` + `pc_abnormal_volume.py`.
3. **Pre-audit smoke framework** (PR #97) caught environmental data-coverage failure modes in <2 min vs ~30 min wasted compute on pod.
4. **Memo §7 risk catalog** functioned as the verdict-classification's safety net. Section #5 explicitly stated the FAIL path AND the disposition rule. The audit didn't surprise us — it confirmed pre-registered risk.

### What broke (and was fixed mid-cycle)

| Launch attempt | Cost | Failure | Fix shipped |
|---|---|---|---|
| #1 (08:10 UTC) | ~$0.13 | precheck guard data gap (pod had no pre-2018 iVol) | PR #96 (`--skip-precheck` in launcher) |
| #2 (08:37) | ~$0.58 | stride-5 conflation in `alphalens audit` generic CLI | PR #98 (custom orchestrator + hard-lock) |
| #3 (12:53) | <$0.01 | preaudit framework gate caught coverage threshold | PR #99 (prices threshold tune) |
| #4 (12:59) | ~$0.002 | artifact-root collision OOS vs FL | PR #100 (per-window artifact paths) |
| #5 (13:00) | ~$0.43 | OOM-killed phases on 16 GB pod | upgrade to cpu5m-8-64 (no code) |
| #6 (20:15) | $2.20 | **SUCCESS** | — |

Total audit-cycle compute: ~$2.84. Memory baseline: $10-13/cycle.

### Methodology lessons (for project's "Workflow conventions")

1. **Pre-reg LOCKED audits MUST use strategy-specific orchestrators**, never the generic `alphalens audit` CLI. The generic driver conflates phase-count with rebalance-stride and omits the synchronous block-bootstrap that memo §5.4 requires.
2. **Constant-lock tests must check EFFECTIVE values, not just module-level constants.** PR #95 originally tested `_REBALANCE_STRIDE_LOCK == 21` (the constant). PR #98 added subprocess invocation of `main()` with `--rebalance-stride 5` and asserts exit code 9 + PRE-REG VIOLATION stderr. The latter catches CLI override drift.
3. **Memory budget for 5-phase × 2-window concurrent audits is ~50 GB.** 16 GB OOM-kills. cpu5m-8-64 ($0.52/h) is the right pod class.
4. **Pre-existing artifact paths must be distinct per window** when running concurrent orchestrators. The form4 launcher (one window) didn't need this; compound (two windows) does. PR #100 fixed the bug; orchestrator's `--artifact-root` is now passed explicitly per tmux session.
5. **§A0 coverage gate is observable via per-phase report.md ticker-count lines.** Orchestrator's regex doesn't currently capture them — only αt/Sharpe/excess. Future improvement: extend the regex to capture `n=X topN=Y turn=Z%` for end-to-end coverage verification without container-disk dependency.

---

## Forward path

Per memo §7 #5 mitigation, both components remain registered separately:

- **insider_form4_opportunistic_2026_05_08_v2** — PASS_MARGINAL on both windows, paper-trade active per `project_insider_form4_opportunistic_locked_2026_05_05.md`.
- **pc_abnormal_volume_retrospective_pre_2018_2026_05_05** — INCONCLUSIVE retrospective, paper-trade active per `project_pc_abnormal_retrospective_INCONCLUSIVE_2026_05_05.md`.

The compound design is **archived as a research artifact**, not deleted:
- `alphalens/screeners/compound_insider_pc/__status__` → CLOSED (ADR 0005 anti-pattern catalog).
- Module + tests remain in repo for reproduction + future reference.
- `alphalens_cli/commands/audit._SCRIPTS["insider_pc_compound"]` stays — replay still works.

Per project doctrine ("Keep searching screeners — never close the door"), the FAIL doesn't close the search space. Next compound candidates remain on table per `project_compound_experiments_roadmap.md`:
- insider × distress_credit (cross-source, similar Layer 1 fusion pattern)
- pc_abnormal × IV-skew (within iVolatility, but different signal axis)
- Layer 4 overlay tests on insider_form4 base — all overlay candidates REJECTED per `project_v10_drawdown_overlay_FAIL_2026_05_04.md` cyclicality pre-screen; reconsider only with new theoretical motivation.

Bonferroni accounting: program-level n stays at 34 (this audit was already counted at registration; failed tests still count). Next signal-class registration must clear |t|≥2.974 with the n=34+1 incremented threshold.

---

## Cross-reference

- Design memo: `docs/research/insider_pc_compound_design_2026_05_10.md`
- Launch postmortem (operational): `docs/research/insider_pc_compound_audit_launch_postmortem_2026_05_11.md`
- Audit JSONs (verdict-bearing): `docs/research/insider_pc_compound_oos_2026-05-11.json`, `docs/research/insider_pc_compound_finallock_2026-05-11.json`
- Pre-reg ledger entry: `docs/research/preregistration/ledger.json::entries[32]` outcome appended
- Memory: `~/.claude/projects/.../project_insider_pc_compound_audit_complete_2026_05_11.md`
