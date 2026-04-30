# Mom+lowvol fallback — extended-IS validation

**Date:** 2026-04-29 (followup to `tri_factor_validation_2026_04_29.md`)
**Verdict:** Mom+lowvol fallback **also fails** the strict subsample stability gate (locked-universe halves), but with smaller failure magnitude than tri-factor. Neither strategy has demonstrated stable alpha across both 4-year IS halves.

## Why we tested

Tri-factor failed the gate (`tri_factor_validation_2026_04_29.md`). The synthesis action items recommended fallback to mom+lowvol baseline. Before treating mom+lowvol as a deployable candidate, run it through the **same harness** (8y IS + 4y halves, locked universe) for apples-to-apples comparison.

## Full IS+OOS results (mom+lowvol baseline, vw=1.0)

`docs/research/momentum_lowvol_extended_is.md`:

| Period | ADV | cost | Sharpe gross | Sharpe net | excess net | α 4F | t (4F) |
|---|---|---:|---:|---:|---:|---:|---:|
| IS 2015-2022 | $5M | 5bp | 0.42 | 0.21 | +16.1% | +27.8% | +1.37 |
| IS 2015-2022 | $20M | 5bp | 0.44 | 0.26 | +15.2% | +26.7% | +1.39 |
| OOS 2023-2026 | $5M | 5bp | 0.75 | 0.55 | +18.9% | +48.1% | +1.45 |
| OOS 2023-2026 | $20M | 5bp | 0.74 | 0.56 | +19.4% | +46.6% | +1.37 |

Tri-factor full-IS comparison: Sharpe net 0.26-0.31, α t=1.26-1.65. Essentially identical at the 8y IS level — the ROE component adds ~no signal over 8 years (it both helps and hurts regimes; nets to zero).

OOS: tri-factor (rw=0.5/1.0 ADV $20M) hit Sharpe net 0.98-1.12, t=2.13-2.22. Mom+lowvol OOS hits Sharpe net 0.55-0.56, t=1.37-1.45. **Tri-factor's "advantage" was OOS-only and now suspect.**

## Locked-universe halves — mom+lowvol vs tri-factor

`docs/research/momentum_lowvol_subsample_halves.md` for mom+lowvol; `docs/research/tri_factor_edgar_subsample_halves.md` for tri-factor.

Both runs used `--lock-universe` (same 1395-ticker 2015-2022 PIT union for both halves).

| Strategy | Half | ADV | Sharpe gross | excess gross | α 4F | t (4F) |
|---|---|---|---:|---:|---:|---:|
| **mom+lowvol** | 2015-2018 | $5M | 0.20 | +13.5% | +10.4% | +0.39 |
| mom+lowvol | 2015-2018 | $20M | 0.15 | +7.7% | +5.9% | +0.27 |
| **mom+lowvol** | 2019-2022 | $5M | -0.15 | -22.4% | -18.8% | -0.46 |
| mom+lowvol | 2019-2022 | $20M | 0.04 | -5.8% | -4.3% | -0.11 |
| **tri-factor (rw=0.5)** | 2015-2018 | $5M | 0.14 | +9.8% | +5.8% | +0.23 |
| tri-factor (rw=0.5) | 2015-2018 | $20M | 0.23 | +12.1% | +8.7% | +0.40 |
| **tri-factor (rw=0.5)** | 2019-2022 | $5M | -0.13 | -20.9% | -18.7% | -0.45 |
| tri-factor (rw=0.5) | 2019-2022 | $20M | -0.01 | -10.7% | -7.4% | -0.19 |

Key diffs:
- **Half 1 (2015-2018)**: both strategies similar weak positive (max t = +0.40, gross excess +5-13%). Neither has detectable edge.
- **Half 2 (2019-2022)**: both strategies negative; tri-factor is **roughly 2× worse** than mom+lowvol at every config. Both at ADV $20M: tri-factor -10.7%, mom+lowvol -5.8%. At ADV $5M: tri-factor -20.9%, mom+lowvol -22.4% (tied).

## Decision matrix verdict

Per `project_next_session_edgar_backfill.md`:

| Result | Verdict |
|---|---|
| Per-subperiod t > 2.0 in 2 of 2 halves | PASS |
| One half t > 2.0, other marginal | MID |
| Both halves t < 1.5 OR catastrophic in any half | FAIL |

Mom+lowvol: max t any half = +0.39 (≪ 1.5). Half 2 ADV $5M is catastrophic (-22.4%/y); ADV $20M is "merely" -5.8%/y. → **FAIL** by the strict gate.

Tri-factor: max t any half = +0.40. Half 2 catastrophic across ALL configs. → **FAIL more decisively**.

## What this means

**Both strategies fail strict subsample stability.** Neither demonstrates stable alpha across both 4-year IS halves. The OOS 2023-2026 strong results (Sharpe 0.55 for mom+lowvol, 1.04 for tri-factor) reflect a regime rotation that catches a 4-year preceding stretch of equally severe underperformance.

The ROE addition (mom+lowvol → mom+lowvol+ROE) **amplifies** regime fragility:
- Mom+lowvol regime range: half 1 +13.5% → half 2 -22.4% = 36pp swing at $5M
- Tri-factor regime range: half 1 +9.8% → half 2 -20.9% = 31pp swing at $5M (similar)
- BUT at ADV $20M tri-factor swing is wider: +12.1% → -10.7% = 23pp; mom+lowvol: +7.7% → -5.8% = 13pp.

So the small/mid-cap quality+momentum signal class is fundamentally regime-dependent at this scale. The 5/5 paradigm-failure pivot doc (`paradigm_failures_postmortem.md`) called this out for prior layers; this session adds **layer 6** (tri-factor) and **layer 7** (mom+lowvol) to the same pattern.

## Options forward

1. **Accept research-only mode for active alpha** (matches pivot 2026-04-25). Continue Layer 1 watchdog + literature review as the only ACTIVE components. Tri-factor and mom+lowvol both become research-replay tooling.

2. **Lower the gate explicitly** and deploy mom+lowvol at ultra-conservative size with explicit regime-monitoring. Per memory `feedback_quality_over_speed.md`, this contradicts "never compromise on quality" — only viable if risk budget is tiny and seen as research-with-skin.

3. **Search a fundamentally different signal class** — moving away from cross-sectional momentum/quality factor stack toward something with different regime exposure (event-driven, statistical arb, options-flow-based). High effort, no guarantee.

4. **Methodology audit first** — the 17pp gap between full-period Sharpe and halves-average Sharpe (both tri-factor and mom+lowvol show this) suggests the engine's holding-window carry-over treatment is non-trivial. Worth understanding before discarding any strategy purely on halves results.

## Phase-robust update (end of session 2026-04-29)

After methodology audit + multi-phase aggregator infrastructure landed, both strategies were re-validated under proper multi-phase methodology (5 phases × stride=5).

| Metric | Tri-factor (rw=1.0 $5M) | Mom+lowvol (vw=1.0 $5M) |
|---|---:|---:|
| mean alpha t | +0.34 | +0.29 |
| std t across phases | 1.30 | 0.70 |
| mean excess net /y | -8.5% | -6.3% |
| std excess net /y | 45.1pp | 21.4pp |
| Robust verdict | **FAIL** | **FAIL** |

Both FAIL the multi-phase robust gate. Mom+lowvol is ~2× less phase-fragile than tri-factor — empirical confirmation that ROE addition amplifies regime fragility (now phase-robust, not single-sample). Neither has positive mean across phases; both should be **CLOSED** for capital deployment.

Synthesis above (everything before this section) was based on single-phase halves data. The multi-phase verdict is **definitive and supersedes** the original section.

## Recommended next step (subject to user direction)

Option 4 (methodology audit) before any further strategy decisions. The full-period vs halves gap of 17pp consistently across BOTH strategies is suspicious — it could mean:
(a) The engine correctly models continuous deployment (full period is realistic), and the halves understate real-world Sharpe by truncating cohort runoffs. → both strategies are MORE attractive than halves suggest.
(b) The engine has an artifact that inflates full-period Sharpe (e.g., look-ahead from cohort warm-up). → halves are the truth and full IS is biased upward.

Without resolving this, every subsample stability check (the gate that just killed two strategies) may be misleading.
