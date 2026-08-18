# What-if: trailing TP tranches vs fixed R-multiple targets

**Status: REJECTED** (both variants; reactivation clause in §6)
**Date:** 2026-08-18
**Type:** engineering decision support — a re-cut of already-used data, NOT a pre-registered strategy test. Direction-level per cohort.
**Script:** `apps/alphalens-research/scripts/whatif_trailing_tp.py` (run on the VPS against the population-ladder store + the monitor's cached minute bars; no refetch).
**Artifacts:** VPS `~/whatif_studies/trailing_tp_2026_08_18/` (full stdout, TSV aggregate, 11,496-row per-record parquet, run copy of the script).
**Twin study:** `trailing_entry_whatif_2026_08_12.md` (#1037) — the entry-side counterpart whose +0.018R verdict shipped as the native trailing entry (first live fill 2026-08-18).

## 1. Question

The live exit model sells fixed TP tranches: at each touched R-multiple target (2R/3R/4R, ~1/3 each)
the tranche exits at the target. Closed-position charts on /edge often show price running well past
the targets, which suggests trailing could capture that tail. Two variants tested:

- **B (all-tranche):** when a TP level is touched, the tranche does not sell; it trails the running
  high from the touch and exits when price retraces `d` from that high.
- **B-last (last-tranche-only):** only the tranche at the DEEPEST target trails; earlier tranches
  bank their fixed targets exactly as today. Hypothesis: keep the collapse insurance, capture the
  post-final-target tail.

## 2. Method (mirrors the entry twin)

- Universe: every population-ladder candidate with a plannable `brief_trade_setup` and a cached
  minute-bar path. 958 tier-touches survived the funnel (exclusion accounting in §5).
- Baseline A is byte-level reuse of the repo exit walk (`ladder_replay._replay_synthetic_fill`),
  enforced by a per-touch parity guard: max |M_walk − M_repo| = 1.14e-13 over 960 touches, and a
  reconciliation against recorded parquet outcomes (100% classification match, Pearson r = 1.0000).
- Conservatism doctrine (direction flipped from the entry twin): every ambiguous minute-bar call
  cuts AGAINST the tested variant — same-bar retrace inside the touch bar fills at the worst
  plausible level `max(low, target·(1−d))`; trail triggers derive from highs through the PREVIOUS
  bar only (no look-ahead); gap-opens below the level fill at the open; SL-first tie-break; B pays
  +1 tick adverse on trail exits (A's resting-limit sells stay unpenalised). Reported deltas are
  therefore a lower bound on B.
- d grid: 0.5% / 1% / 1.5% / 2% / 3% + an ATR-scaled config (k=0.25, median effective d ≈ 1.3%).
- Methodology was adversarially reviewed BEFORE the run; three defects were found and fixed
  (same-bar fill over-crediting B, per-config outcome-based censoring of B's right tail,
  an uncounted exclusion). The last-tranche extension was verified to leave every all-tranche
  config bit-identical (36 TSV rows + 5,748 record rows reproduced exactly).

## 3. Results — all-tranche trailing (variant B)

ALL cohort (N=958), mean ΔR (own denominator, no-slippage view; adverse view within 0.001 everywhere):

| config | mean ΔR | best cohort | worst cohort |
|---|---|---|---|
| d=0.5% | −0.002 | day-1 −0.001 | E3 −0.007 |
| d=1.0% | −0.001 | day-1 +0.003 | E3 −0.011 |
| d=1.5% | −0.009 | day-1 −0.003 | E3 −0.044 |
| d=2.0% | −0.011 | day-1 −0.006 | E3 −0.047 |
| d=3.0% | −0.014 | day-1 −0.006 | E3 −0.065 |
| atr_k0.25 | −0.001 | day-1 +0.004 | E3 −0.013 |

Path-class cut (the decision-relevant view):

| baseline path class | N | baseline R | ΔR of trailing |
|---|---|---|---|
| TP_FULL (all targets hit) | 226 | 0.801 | **+0.002 … +0.008** — the /edge-chart intuition is real |
| PARTIAL_TP_THEN_SL | 25 | −0.024 | **−0.28 … −0.32** at d≥1.5% — trailing tranches ride the collapse to the stop |
| TIME_STOP | 191 | 0.318 | −0.003 … −0.024 |
| OPEN / SL_HIT (no target touched) | 338 | — | 0 (nothing arms) |

The tail capture on full runs is real but is paid for ~3× over by the collapse paths and time-stop
bleed. Fixed targets are insurance against the touch-then-collapse path; charts show the payouts,
not the premiums.

## 4. Results — last-tranche-only (variant B-last)

ALL cohort (N=958):

| config | mean ΔR | adverse | median |
|---|---|---|---|
| last:d=0.5% | −0.0002 | −0.0003 | 0 |
| last:d=1.0% | +0.0003 | +0.0001 | 0 |
| last:d=1.5% | +0.0005 | +0.0003 | 0 |
| last:d=2.0% | +0.0001 | −0.0000 | 0 |
| last:d=3.0% | −0.0005 | −0.0005 | 0 |
| **last:atr_k0.25** | **+0.0008** | +0.0006 | 0 |

Mechanics confirmed exactly: on every non-TP_FULL path class the delta is exactly 0.000 (the final
target is never touched there, so nothing ever arms — the collapse insurance is preserved 100%, not
just 2/3). The entire effect lives in TP_FULL (N=226): mean +0.0033 (ATR config) with a NEGATIVE
median (−0.01) — a lottery profile where most final tranches give back ~1% from the target and an
occasional runner pays for the rest.

## 5. Guards and caveats

- Exclusion funnel fully counted (dates 91 → candidates 783 → tier touches 960 → analysed 958);
  no outcome-based selection; two B-implausible touches (2026-05-29 MQ E1/E2, +110%/+119% at d=3%)
  dropped symmetrically across all configs — unadjudicated; if genuine they would flatter only the
  d=3% tail configs.
- pct_stop = 0 and pct_timestop ≤ 0.002 for armed tranches: the disaster-stop tail of variant B was
  essentially unexercised in this sample (the class cut exposes it instead via PARTIAL_TP_THEN_SL).
- 2,477 B tier-entries were horizon-open at path end (marked at last close) — wide-d configs'
  dispersion is understated.
- The study speaks only to touched targets: 1,207 of 2,167 tiers (56%) never reached their target.

## 6. Verdict

**REJECTED — keep fixed TP tranches (2R/3R/4R) + the trailing_atr protective stop.**

- All-tranche trailing: ~0 to clearly negative everywhere; worst exactly where the ladder earns
  most (E3 baseline 0.457R, ΔR to −0.065).
- Last-tranche-only: cost-free but benefit-free — best config +0.0008R per touched tier
  (≈ 22× smaller than the entry-trailing effect that DID ship), below implementation-complexity
  cost on money-adjacent exit code.

**Reactivation clause:** revisit last-tranche-only (ATR-scaled d) if (a) position throughput grows
enough that +0.0008R/touch compounds materially, or (b) hand-adjudication of the two dropped
+110% runners shows they are genuine (they concentrate exactly in the tail this variant monetises),
or (c) the live measurement program later shows realized full-run tails fatter than the replay's.
