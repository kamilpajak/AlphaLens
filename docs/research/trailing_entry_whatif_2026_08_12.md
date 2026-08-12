# Trailing (bounce-confirmed) entries vs hardcoded limit entries — what-if replay

**Date:** 2026-08-12
**Status:** COMPLETE — direction-level diagnostic (a re-cut of already-used data, NOT a pre-registered strategy test; no Bonferroni claim is made)
**Question (operator):** on our historical data, would trailing entries (enter only after price bounces d% off its running low) beat the current limit-at-touch entries?
**Method:** what-if replay over the population-ladder parquets (85 days, 769 plannable candidates) on cached Polygon minute bars; both variants share ONE exit function (the repo `ladder_replay` engine); slippage stressed both ways; independent verifier re-derived 4 cases by hand (6-decimal match, cases picked by deterministic rule, never by outcome) and reproduced the recorded parquet outcomes with Pearson r = 1.0000. Script: `apps/alphalens-research/scripts` authoring copy of `whatif_trailing_entry.py` (run from `/tmp` on the VPS); records parquet `/tmp/whatif_trailing_entry_records.parquet` (11,292 rows).

## Verdict

**A tight trail (d = 0.5–1%) beats limit-at-touch in EVERY cohort, under BOTH slippage assumptions — and it enters CHEAPER, not dearer.** The edge decays monotonically with d and crosses to negative around d ≈ 2% (day-1 cohort flips hardest: d = 3% is clearly worse than A).

Policy view (missed tier = 0R, fixed risk denominator = A's risk unit — the view that cannot flatter B by re-denominating):

| Cohort | N | A (none/adv) | B d=0.5% (none/adv) | B d=1% (none/adv) | B d=3% (none/adv) |
|---|---|---|---|---|---|
| ALL | 946 | 0.215 / 0.209 | **0.233 / 0.230** | 0.225 / 0.223 | 0.198 / 0.196 |
| day-1 touch | 357 | 0.177 / 0.175 | **0.202 / 0.200** | 0.194 / 0.192 | 0.140 / 0.139 |
| day-2+ touch | 589 | 0.238 / 0.229 | **0.251 / 0.249** | 0.244 / 0.241 | 0.233 / 0.230 |
| E1 | 628 | 0.211 / 0.209 | **0.223 / 0.221** | 0.218 / 0.215 | 0.183 / 0.181 |
| E2 | 243 | 0.153 / 0.135 | **0.174 / 0.172** | — | — |
| E3 | 75 | (in log) | (in log) | — | — |

Full grids incl. d = 1.5/2%, medians, win rates, own-denominator view: rerun log `/tmp/whatif_rerun.log` on the VPS (regenerate any time from the records parquet).

## Why the trail wins (mechanics, from the data)

1. **The concession is NEGATIVE at small d** (ALL cohort: −0.3% average entry price vs the limit). After price touches the limit it usually keeps sliding; the trail follows the falling price down and triggers off a LOWER low — so "waiting for confirmation" gets paid instead of paying. The intuition "trailing always buys dearer" is wrong at small d on our paths.
2. **Fill-rate cost is negligible at small d**: 99.5% at 0.5%, 98.5% at 1% (vs 100% for A). By d = 3% it drops to 93.8% and the missed winners eat the edge.
3. **Day-1 touches benefit MOST** (+0.025R at d = 0.5%, win rate 68.1% → 70.0%) — consistent with the day-1 adverse-selection finding (`reference_day1_gap_gate_and_adverse_selection_2026_08_11`): day-1 dips are the most likely to be falling knives, so bounce confirmation filters exactly where filtering pays. Note the day-1 gap GATE only covers the open-below-E1 subclass; the trail helps the remaining day-1 touches too.
4. Effect size honesty: +0.018R mean on ALL is ~8% relative — real but modest; N = 946 tier-touches from 85 days, third-order cut of the same data. Direction, not calibration: {0.5%, 1%} beat A robustly, the exact optimum inside that range is not resolvable at this N.

## Caveats

- **Execution realism**: variant A assumed touch-fill (adverse variant demands trade-through — changed almost nothing: 938/946 touches traded through on the same bar); variant B assumed stop-buy at trigger +1 tick adverse. Real stop-buy slippage on thin small-caps can exceed 1 tick; the ~+0.02R edge could absorb ~2-3 extra ticks on a $3 stock before flipping, less on dearer names.
- **60 "implausible" B rows dropped** (0.6%, mirror of the monitor's split guard) without a per-variant bias quantification — flagged by the verifier, direction unknown, small.
- 197 candidates had entry windows truncated by the data horizon; 4,937 tier-entries were still horizon-open and marked at last close — identical treatment for A and B, so comparisons stand, but absolute R levels are conservative.
- Implementing trailing ENTRIES live was previously REJECTED (INC-4) on execution-complexity grounds (bot-managed stop-buys trailed per tick, restart-safety, off-tick amend limits — see `trailing_execution_design_2026_08_07.md`). This result is evidence to REOPEN that decision with a concrete payoff estimate (~+0.02R/entry), not a green light to build.

## Follow-ups (not scheduled)

1. Reopen the trailing-entry execution design (V-variant selection) with this payoff estimate; weigh against the amend-rate limits and restart-safety cost.
2. If built: ship behind a per-env flag, validate on SIM with the same replay as the acceptance oracle.
3. The per-variant implausible-drop breakdown (verifier issue 3) — one-line script change if the study is ever re-run.
