# Ladder reward:risk asymmetry — diagnosis + break-even-stop counterfactual

**Date:** 2026-06-30
**Status:** DIAGNOSIS COMPLETE + counterfactual replay built (TDD). **Production change NOT yet made** — this is descriptive evidence for a pre-registration, not a shipped rule.
**Trigger:** user noticed on `/edge` that most terminals are correct signals (+EXCESS_RETURN) but the realized `%book` is tiny — one SL ≈ −1% book, a TP ≈ +0.05% book, "you need ~10 TPs to recover one SL".
**Code:** `replay_ladder_breakeven` in `alphalens_pipeline/feedback/ladder_replay.py` (+ tests in `apps/alphalens-research/tests/test_feedback_ladder_replay.py::TestBreakevenPass`); script `apps/alphalens-research/scripts/diagnose_exit_geometry.py`.

---

## 1. The defect is real and size-independent

The strategy bleeds in R despite correct selection (terminal-with-`realized_r`, N=42):

| Metric | Value |
|---|---|
| Win-rate | 50% |
| avg_win | **+0.213R** |
| avg_loss | **−0.955R** (all 20 SL_HIT = −1.000R) |
| Payoff | **0.22 : 1** |
| Breakeven win-rate needed | **82%** (have 50%) |
| Mean realized_r | **−0.371R** |
| TP_FULL to offset one SL | **~10.4** |

This is a pure realized-R fact, not a sizing artifact (size-weighted `%book` was demoted in #717/#720 for unlimited-budget / per-member sizing; the R-geometry is independent of all that).

## 2. Root cause — two compounding mechanisms

The puzzle: TP targets sit at mean **2.62R** / median **2.10R** (873 tranches) yet a full take-profit realizes only **+0.21R**.

**(A) Exit geometry — stop placed for the DEEP ladder, but only the SHALLOW tier fills.** `disaster_stop` is set below the deepest tier (`trade_setup/builder.py`), but in practice only T1 fills (20/21 TP_FULL filled exactly 1 tier). `realized_r = (exit − blended_filled_entry)/(blended_filled_entry − disaster_stop)`: a shallow entry over a deep stop inflates the risk-per-share denominator (~1.2×), and the TP target's R-multiple — computed against the full-ladder blend — shrinks when the fill is shallow. So winners realize a small R while losers run to the far stop for the full −1R.

**(B) Entry selection-on-fill — deep tiers select falling knives.** `tiers_filled_count` correlates **−0.67** with market_excess: 1 tier → +7.8% market_excess / 17% SL; 2 tiers → −17.5% / 92% SL; 3 tiers → −20.3% / 83% SL. Deep fills are breakdowns that run to −1R. (Corroborates `edge_driven_ladder_feasibility_2026_06_30`: deep-fill 89% SL.)

**Split:** ~half of −0.371R is genuinely bad names (deep-fill, −20% market_excess = a selection miss the exit fix cannot recover); ~half is a geometry tax on CORRECT names (shallow fill, +7.8% market_excess, capped at +0.21R). The exit fix recovers the second half without touching selection.

## 3. The lever — MFE-triggered break-even (NOT the existing TP-hit ratchet)

The store already carries `ratchet_realized_r` (raise stop to break-even on a **TP1 HIT**). It barely helps (Δ +0.012R, rescues 0/20 SL) because the losers never reach a TP target. But they DO peak in MFE: **SL losers' MFE median = 0.618R; 70% (14/20) reach ≥ +0.5R MFE** before reversing to −1R. So a break-even triggered by **MFE crossing +0.5R** (not a TP hit) rescues them.

## 4. Counterfactual replay result

`replay_ladder_breakeven` re-walks the RETAINED minute bars (RTH-filtered) with an MFE-R-triggered break-even / trailing stop, holding the pick + entry tiers + TP ladder fixed. Baseline fidelity: the replayed static baseline reproduces the stored `realized_r` on **42/42 names to 0.0000** (so the deltas are trustworthy).

| Stop policy | mean_R | median_R | win% | payoff | winners_worse |
|---|---|---|---|---|---|
| **baseline (current)** | **−0.371** | −0.006 | 50 | 0.22 | — |
| **be@0.5R (break-even)** | **+0.069** | +0.044 | 52 | 2.23 | **0** |
| **be@0.5R + trail0.6** | **+0.367** | +0.322 | 95 | 0.44 | **0** |
| be@0.3R | +0.051 | +0.022 | 50 | 2.08 | 1 (too tight) |
| be@0.75R | −0.050 | +0.044 | 52 | 0.64 | 0 (too loose) |
| be@0.5R + trail0.4 | +0.270 | +0.235 | 95 | 0.33 | 0 |

**The +0.5R break-even flips expectancy from −0.371R (bleeding) to +0.069R (positive) with ZERO winners harmed** (`winners_worse=0` — it only arms after +0.5R MFE, so it never cuts a position that is not already in profit; this is the empirical floor-interaction co-validation). Adding a 0.6 trail lifts mean R to +0.367 but changes the profile (95% small wins, big winners capped) — a different risk choice, not strictly dominant.

## 5. Caveats (hard)

- **N=42, conditioned-on-fill, ~5 weeks, same sample the hypothesis was read from** → descriptive evidence for a pre-registration, NOT a validated edge. No CI, no holdout here.
- **Exit and entry are coupled** — tightening the stop raises SL frequency on falling knives; a real change should be a JOINT entry×exit replay, not a single knob.
- **`trade_setup` is user-facing** (the WhatsApp group acts on it) → any production stop-rule change is a non-deterministic product change requiring a **forward-only version stamp** + a note to the group, not a silent deploy.
- Selection (deep-fill = −20% market_excess) is a separate, already-identified track (`edge_signal_attribution`: deprioritize high-ATR / popped names).

## 6. What was built (this PR)

- `replay_ladder_breakeven(trade_setup, bars, *, mfe_trigger_r, trail_frac=None, ...)` — pure what-if replay, mirrors `_replay_ratchet`'s walk model but swaps the TP-hit ratchet for an MFE-R-threshold break-even (optional trail). Pure addition: zero change to the production replay path. Tested (`TestBreakevenPass`, 6 cases incl. baseline parity at `mfe_trigger_r=inf`).
- `diagnose_exit_geometry.py` — read-only research script: replays the store under the policy grid, prints the table above + the 42/42 fidelity guard.

## 7. Next step (NOT done here)

Pre-register a **joint entry×exit** counterfactual (small grid: break-even trigger ∈ {0.5}, trail ∈ {none, 0.6} × the deprioritize-deep-fill entry rule), held to a purged+embargoed walk-forward once N per config crosses the gate (~2026-09+). Only then consider a production stop-rule change behind a `trade_setup` version stamp + group communication. Priority: this exit-geometry fix ranks ABOVE further selection work, because at 0.22:1 even perfect selection loses.
