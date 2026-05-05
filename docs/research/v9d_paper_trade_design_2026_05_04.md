# v9D long-only paper-trade prospective replication (2026-05-04)

**Pre-reg id:** `v9d_long_only_paper_trade_2026_05_04`
**Class:** `prospective_replication_2026_05_04` (NEW class, intra-class n=1)
**Status:** registered (Phase 0 complete; Phase 1 implementation in progress)
**Frozen scorer:** `alphalens.paper_trade.scorer_v9d` (extracted from `experiment_v9_cross_sectional_residual.py`)

## Why this test

The project has now produced 11 paradigm failures across 8 signal classes on burnt holdout 2024-04-30 → 2026-04-30, but **one** finding is consistent and replicable: **+2.29 Carhart-4F αt** on v9D options-implied long-only top decile, observed across 9 evaluations (v7, v8, v9A, v9D, plus 5 v10-base phase replications) with mean exactly +2.29 and per-phase range [+1.86, +2.92]. The cross-period diagnostic (2026-05-04, STABLE verdict, range 1.52 across 4 calendar sub-periods, min +1.65) confirmed this is regime-uniform, not concentrated in 1–2 months.

The v9D L/S diagnostic was **already FAIL'd** at αt=−0.92 (HIGH-IVP30 names actually outperformed in this regime, killing the symmetric-payoff hypothesis). The honest payoff lives only in the long top-decile.

Burnt-holdout multiplicity penalty (program n=24, |t|≥3.08 unadjusted, ~3.50 escalated) is exhausted: no further refinement on the 2024-2026 window can credibly clear Bonferroni without HARKing. Per perplexity (sonar-reasoning-pro, two consultations) and zen earlier review:

> *"H+ paper-trade prospective on fresh post-2026-05-04 data is the only path that moves a skeptical reviewer's posterior from 'interesting anomaly on stale window' to 'deployable alpha factor'. All other paths diagnose the burnt finding without testing forward predictability."*

This pre-reg locks the v9D specification AS-IS (no parameter retuning) and commits to weekly forward-walk paper-trading for ≥52 weeks before any capital-deploy discussion can resume.

## Hypothesis

The v9D cross-sectional residual scorer (per-asof OLS residual of −IVP30 against reversal_1m, momentum_6m, rv_30d), applied long-only to the top decile of an iVolatility-supported PIT universe with 5-day rebalance, will exhibit **Carhart-4F αt ≥ 1.96 (unadjusted single-test) at both 26 weekly obs and 52 weekly obs** when evaluated on weekly returns accruing from 2026-05-04 onwards.

The 26-week checkpoint serves as an early-warning gate (~70% power if true αt=+2.29). The 52-week checkpoint is the deploy-eligibility gate (~95% power).

## Two-stage gate sequence

### Checkpoint #1: 26 weeks (~2026-11-04)

**Test:** Cumulative Carhart-4F αt ≥ +1.96 AND Sharpe net ≥ +0.30 on the 26 weekly portfolio returns.

**PASS:** continue tracking to 52w. Update memory; do NOT commit additional Bonferroni.

**FAIL:** archive class as `prospective_replication_failed_26w_2026_11_04`, write postmortem documenting the divergence between burnt-holdout αt=+2.29 and prospective αt < +1.96. **Do NOT auto-pivot to a refined design within the same session** (anti-HARKing per `feedback_audit_memos_post_session.md`). Pivot decision belongs to a separate planning session.

**Power note:** If true αt=+2.29 with weekly per-rebal SE ≈ 0.022 (from v10 audit), expected cumulative αt at 26 weeks ≈ +2.39, ~70% power at α=0.05. Marginal but informative.

### Checkpoint #2: 52 weeks (~2027-05-04)

**Test:** Cumulative Carhart-4F αt ≥ +1.96 AND Sharpe net ≥ +0.30 AND no single 13-week sub-period (Q1/Q2/Q3/Q4) shows αt < +0.5.

**PASS:** ELIGIBLE for capital-deploy review. Triggers a SEPARATE pre-reg cycle covering sizing, risk-management, and execution. **Deploy is NOT automatic.** Earliest possible real-money date: 2027-Q3 (allowing for sizing pre-reg + infrastructure).

**FAIL:** archive class, options-implied class fully closed, write postmortem.

**Power note:** ~95% power at α=0.05. Strong evidence either direction.

## Operational architecture

### Weekly cadence

- **Sunday 17:00 (`com.alphalens.paper-trade.refresh.plist`):** pull last 7d SMD data for current PIT universe via launchd → `~/.alphalens/ivolatility_smd/`
- **Monday 06:00 (`com.alphalens.paper-trade.score.plist`):** compute v9D residual scorer → top decile → append ledger entry → write new state.yaml

### Storage

- `~/.alphalens/paper_trade/v9d_state.yaml` — current portfolio (held tickers + scores + as-of date)
- `~/.alphalens/paper_trade/v9d_ledger.parquet` — append-only weekly entries: asof, holdings, prior_holdings, realized_return_long_net, benchmark_return_mdy, n_held
- `~/.alphalens/watchdog/paper-trade-{refresh,score}.{log,err}` — launchd stdout/stderr

### Manual entry points (`alphalens` CLI)

- `alphalens paper-trade refresh-data` — manual data pull (smoke / catch-up)
- `alphalens paper-trade score` — manual single scoring run (smoke / catch-up)
- `alphalens paper-trade verdict` — show running stats + decision-rule status

## Pre-committed kill conditions (operational, NOT performance)

1. Data refresh skip rate >5% in any week (iVol API outage; pause until recovered)
2. Ledger drift: |observed weekly return − reconstructed weekly return| > 1bp on any week (calculation bug)
3. PIT universe size <500 tickers in any week (universe collapse)
4. Realized cost drag >40bps RT in any week (cost model breakdown — pre-reg is 30bps)

If a kill condition fires for >2 consecutive weeks, abandon class and write postmortem. **Performance never triggers a kill** — that's only at checkpoints.

## Falsification asymmetry

This test cannot conjure alpha. Two checkpoints × multi-condition gates × operational integrity guards = ~6+ independent kill conditions over 12 months.

What PASS would mean: the +2.29 ceiling is genuinely real, not selection-biased, and economically meaningful on retail-accessible data going forward.

What FAIL would mean: the +2.29 was either a regime-specific artifact of 2024-2026 OR contaminated by the 9 evaluations on the same window OR our cost model under-estimates real-world execution drag. Posterior on retail-accessible options-implied alpha existence drops sharply.

## Bonferroni accounting

| Counter | Before H+ | After H+ |
|---|---|---|
| Program-level alpha-class n | 24 | 24 (prospective replication on FRESH window does not increment) |
| New class `prospective_replication_2026_05_04` n | 0 | 1 |
| Naive intra-class threshold | n/a | \|t\|≥1.96 (unadjusted single-test) |

The threshold is unadjusted because (a) this is the first test in a new class, (b) prospective replication on fresh post-burnt-holdout data is methodologically distinct from retrospective hypothesis testing, (c) standard practice for prospective replication is unadjusted single-test at α=0.05.

## What this test is NOT

- **NOT a refinement of v9D.** Scorer is FROZEN; no parameter retuning permitted.
- **NOT a multi-strategy basket.** Single hypothesis: v9D long-only top decile vs MDY.
- **NOT a sizing/risk-management test.** That's a future pre-reg cycle, after PASS.
- **NOT a capital-deploy authorization.** Deploy decision is OFF-TABLE for ≥52 weeks.

## Implementation deliverables (Phase 1-2)

- `alphalens/paper_trade/{__init__,state,ledger,scorer_v9d,verdict}.py`
- `alphalens_cli/commands/paper_trade.py`
- `scripts/refresh_ivolatility_smd_for_paper_trade.py`
- `scripts/paper_trade_score_v9d.py`
- `launchd/com.alphalens.paper-trade.{refresh,score}.plist`
- `launchd/bin/alphalens-paper-trade-{refresh,score}`
- `tests/test_paper_trade_{state,ledger,verdict,scorer_v9d}.py`
- Pre-reg ledger entry locked via `alphalens preregister add`

## Related

- v10 multi-phase audit FAIL: `docs/research/v10_drawdown_overlay/multi_phase_verdict.md`
- v10 postmortem: `docs/research/v10_drawdown_overlay_postmortem_2026_05_04.md`
- Cross-period diagnostic: `docs/research/v9d_cross_period_diagnostic_2026_05_04.md`
- Strategy validation playbook: `docs/research/strategy_validation_playbook.md`
- ADR 0007 (layer architecture): `docs/adr/0007-layer-architecture.md`
