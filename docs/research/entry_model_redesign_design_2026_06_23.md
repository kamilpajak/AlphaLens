# Entry-model redesign — counterfactual entry-grid + gated learned policy

**Status:** DRAFT (awaiting user review of this spec, then writing-plans for Faza 0)
**Date:** 2026-06-23
**Author:** research session (adversarially hardened via 5-lens Workflow review, verdict GO_WITH_CHANGES)
**Doctrine:** telemetry-only, forward-only, no capital deployment (ADR 0012); pre-registration + program-level Bonferroni (phase-robust-backtesting ledger).

---

## 1. Motivation (and its honest limit)

`diagnose_selection.py` (2026-06-23, n=316 plannable, fixed-horizon market-adjusted BHAR vs SPY) found the current dip-buy entry ladder behaves like an **anti-signal**:

| horizon | filled | unfilled | gap |
|---|--:|--:|--:|
| k=5 | −3.2% [−4.0,−2.3] | +5.2% [+3.7,+6.8] | ~8.4pp |
| k=10 | −6.0% [−7.5,−4.5] | +6.1% [+4.2,+8.2] | ~12pp |

Fill-rate 92.8%, 54% fill same session. Reading: the dip-buy ladder fills falling knives and misses gappers.

**HARD CAVEAT (this reframes everything — 3 reviewers independently):** this diagnostic **motivates but does not prove** the anti-signal. Two biases:

1. **Selection-on-outcome.** "Filled" vs "unfilled" is a deterministic function of the early price path — you fill exactly the names that dipped. Conditioning realized return on fill is conditioning on the outcome.
2. **Anchor bias.** CAR is anchored to the **prior-session close**. "Unfilled" means price never returned below the brief close (the name ran up). So the +5%/+6% unfilled edge is measured from an anchor that the proposed remedy (`market_at_arrival`) **never pays** — it buys the gapped-up open. The realizable edge is `+5% − overnight/opening gap`, plausibly a small fraction or even negative.

**Therefore the program opens with a HALT gate (Faza −1), not with building.**

---

## 2. Faza −1 — HALT gate (cheapest possible test, do this first)

Re-run `diagnose_selection.py` with the CAR anchor moved from prior-close to the **arrival-session 30-min VWAP** (`bar_window.ARRIVAL_VWAP_WINDOW_MIN`, already used by `benchmark_excess`) — the price an arrival-entry actually pays. Recompute filled-vs-unfilled excess from that anchor.

- **If the unfilled edge collapses toward ~0 once the gap is subtracted → STOP.** The program premise is gone; Faza 0 is not launched. Document and close.
- **If it survives → proceed to Faza 0.**

This is ~50 lines (anchor swap in the existing script) and is decisive. It is the minimal viable experiment; everything below is gated on it.

---

## 3. Architecture (4 layers; build order re-scoped)

```
[A] Entry-grid substrate   [B] Counterfactual rewards    [C] Learned policy        [D] Shadow + gate
 replay N entry PRIMITIVES   reward(arm,event) for ALL    per-arm within-event      log policy choice +
 over the SAME cached bars,  arms (full feedback) =       difference / cost-        counterfactual reward;
 each arm with its OWN fill   raw market-excess on the    sensitive classifier;     drives NOTHING live until
 model + OWN stop;            arm's own blended fill,      argmax; auto-retrain      the OVERFIT STACK (§7)
 EXIT held fixed (abs prices) k=10, exec-cost haircut,     rolling-window, versioned passes + power-derived N
                              NO_FILL = cash 0-excess
```

**Build order (corrected from the original "everything in Faza 0"):**
- **Faza 0** = layer [A] substrate + an **offline research SCRIPT** computing per-arm cost-adjusted market-excess with day-blocked bootstrap CIs (grouped print like `diagnose_selection`). **No stamped parquet column, no `entry_grid_config_version`, no `entry_policy` package, no nightly refit, no Bonferroni-charged model yet.**
- **Faza 1** = offline GO/NO-GO: does any static arm beat baseline OOS **after execution cost + multiplicity**, and is there residual context-dependence?
- **Faza 2** = learned policy [C]/[D] — designed in full here, **built only behind the §7 gate**.
- **Faza 3** = product surface change (user-owned), deterministic only (§9).

---

## 4. Arms (entry primitives)

Five arms. Each is deterministically replayable on cached minute bars, carries its **own stop** (`arm_blended − k·ATR`, re-run the geometry floor), and reports a per-arm `BAD_GEOMETRY` / geometry-collapse rate as a diagnostic (never a silent `None`-drop).

| arm | fill model | notes |
|---|---|---|
| `baseline` | current dip-buy 3-tier (touch-fill) | control |
| `narrow_tiers` | 3 shallow tiers (~0.1–0.25·ATR below close), touch-fill | extends `build_entry_tiers`; touch path |
| `single_at_close` | one tier at/just-below close, touch-fill | touch path |
| `market_at_arrival` | **NEW primitive** — fill at first-RTH-bar open of arrival session | handle gap / halt / empty-first-bar; flag `late_open`; pays execution cost (§6) |
| `vwap_arrival` | **NEW primitive** — fill at VWAP over first 30 min (`_window_vwap`, single synthetic tier, mirrors `realized_r_full_fill`) | lowest-variance always-fill arm |

**Correction vs the original sketch:** `market_at_arrival` / `vwap_arrival` / `single_at_close` are **NOT expressible as `entry_tiers`** — the "`_with_entry_tiers` reuse, no new walk logic" premise was false. The two always-fill arms need new fill primitives in `entry_primitives.py`, each verified against hand-computed gap-up / gap-down / halted test cases.

---

## 5. Reward (layer [B])

**reward(arm, event) = raw market-excess of the actually-held position, measured on the ARM'S OWN blended entry**, over the actual hold window:

```
reward = (exit_mark − arm_blended) / arm_blended  −  SPY_return(same window)
```

- **Drop `realized_r` from the arm reward.** `realized_r` is entry-coupled (`risk = blended − stop` differs per arm, so the same TP price maps to a different R) — comparing arms on R conflates entry-price quality with stop-distance rescaling. `realized_r` stays reserved for the *exit* grid.
- **EXIT held fixed by absolute target PRICES** (not by R), so the comparison varies only entry.
- **Equal size** across arms (isolates the entry channel from sizing) — necessary but not sufficient; the stop-distance leak is closed by the raw-return reward above, not by equal-size alone.
- **k = 10** sessions (locked). Stamp `k` so events at different `k` are never pooled. k=20 deferred (systematically truncated for recent events by the forward-only grouped store; revisit ~mid-July when candidate history is old enough).
- **NO_FILL = cash, 0 excess** (locked): an unfilled arm earns `−SPY_return(window)` (uninvested capital = 0 raw return ⇒ excess is minus the benchmark). Handle `NO_FILL` and `BAD_GEOMETRY` **identically across all arms** (never per-arm drop-vs-zero inconsistency). Also log opportunity/regret vs the best-filling arm as a separate diagnostic (free under full feedback).
- **Headline comparison restricted to the common-support subset** (events where all arms are evaluable) so fill-rate differences cannot masquerade as selection edge. Include a simulation proof: two arms with identical fill-conditional reward but different fill rates must get equal expected reward.

---

## 6. Execution-cost haircut (layer [B], before Faza 1)

The replay fill model is slippage-free price-improvement (resting-limit touch). That is fair for the three resting-below-close arms but **wrong for the always-fill arms**, which pay spread + opening impact and get no price improvement. Without a haircut, the grid structurally over-credits exactly the arms it is meant to promote.

- Resting-limit arms (`baseline`, `narrow_tiers`, `single_at_close`): keep the touch price.
- `market_at_arrival` / `vwap_arrival`: charge open/VWAP **+ half-spread + impact**, sized from the candidate's mcap bucket and a minute-bar spread proxy.
- The Faza 1 GO/NO-GO is evaluated **after** the haircut ("beats best static arm after realistic execution cost").
- Document explicitly: telemetry-clean ≠ execution-clean for always-fill arms; **no real fills exist post-ADR-0012** to validate them.

---

## 7. Learned policy (layer [C]) + the overfit stack (layer [D])

**Designed now; built only behind the gate below.** Approach: per-arm **within-event reward-difference / cost-sensitive classification** (refines the originally-approved "per-arm regression + argmax" — the within-event difference cancels the per-event common term and targets only the decision boundary, far fewer effective parameters). Default model = pooled penalised-linear with arm-as-feature + monotone constraints; GBT only at N ≫ 200. Explicit static-arm fallback when predicted inter-arm spread < bootstrap SE.

**The overfit defense is a STACK, not a single "static arm beats baseline" check** (that check is a *signal-existence* gate, not an overfit safeguard):

1. **Lift measured vs best-static-arm frozen on TRAIN** (not vs baseline): `lift = mean_x[ reward(π(x),x) − reward(a*_train(x), x) ]`. Beating baseline is trivial; the policy must beat the best context-free rule.
2. **Purged + embargoed walk-forward keyed on event MATURITY date**, embargo ≥ the 42-session horizon; refit only on TERMINAL events.
3. **Block bootstrap (resample DAYS, not rows)** for the lift CI — cross-sectional same-day correlation makes row-bootstrap CIs far too tight.
4. **Within-day permutation null** (shuffle rewards within day, retrain, measure lift) = the **overfit floor**; real lift must exceed it. This is the tractability null.
5. **Capacity matched to N** + within-event-difference estimator (above).
6. **Program-level multiplicity / PBO** — Bonferroni over every arm + model class + feature set + window variant ("data inputs unchanged ⇒ same family"); report Probability of Backtest Overfitting.
7. **Power analysis sets the N gate** — compute the minimum detectable lift at the chosen α; do **not** use a folkloric N≥200 if that N has no power to detect a realistic 1–2% lift.

**Activation gate (all must hold):** lower-CI-bound(lift) > 0 after multiplicity correction **AND** lift exceeds the permutation null **AND** N ≥ power-derived threshold **AND** span ≥ 2 distinct vol regimes.

**Auto-retrain:** standalone research script on its own systemd timer (like `diagnose_selection` / literature scans) — **never imported by an `alphalens_pipeline` module**. `entry_policy_config_version` = hash of {data-snapshot digest, window bounds, seed, feature list, hyperparams}, not just code.

---

## 8. Features (PIT, zero leakage)

Known strictly **at arrival, before the entry window**: momentum/RS percentile (O'Neil R exists), overnight gap = `arrival_open / prior_close − 1` (opening print only), realized vol / ATR% (setup-time ATR), distance to nearest support, theme, novelty, days-to-earnings, mcap bucket. **Leakage unit test** asserts no feature reads a bar with `t ≥ arrival cutoff` (mirror existing PIT tests).

This is a small numeric model on our own PIT data — it does **not** violate the "LLM blind on numerical data" doctrine (that bans asking LLMs for numbers, not ML on our data).

---

## 9. Product safety (Faza 3, user-owned)

Briefs are user-facing (the WhatsApp group acts on the surfaced `trade_setup`), so a live entry change is a real, non-deterministic product change — not pure telemetry.

- The **surfaced** entry stays **deterministic**. If a static arm wins, change the one transparent geometry rule the group can reason about (e.g. nearest tier 0.5·ATR → 0.1·ATR, or add a market-at-open tier), behind a **forward-only version stamp** on the brief `trade_setup` (mirror `insider_signal_version`), with explicit group communication — never a silent deploy.
- A **learned policy is reserved for research/telemetry ranking only — never the surfaced setup.** Whether [C] ever reaches the product surface is itself a deferred decision.

---

## 10. Code surface (corrected)

- `apps/alphalens-pipeline/alphalens_pipeline/thematic/trade_setup/entry_primitives.py` (new) — 5 arm builders + the 2 new non-touch fill primitives, each with hand-computed test cases.
- `apps/alphalens-pipeline/alphalens_pipeline/feedback/ladder_replay.py` — `replay_entry_grid` mirroring `replay_ladder_grid`, with per-arm fill + own-stop + raw-market-excess reward + execution-cost haircut.
- **Faza 0 deliverable** = `apps/alphalens-research/scripts/diagnose_entry_grid.py` (research script; reuses `benchmark_excess` + `fixed_horizon` + the `diagnose_selection` grouped-print template). **No parquet column / no `entry_grid_config_version` / no package** until a static arm proves out.
- Graduation (only if Faza 1 GOs): `entry_grid_json` stamped on `population_ladders` parquet as a **canonical sorted-key JSON token** (like `ladder_config_version`, NOT an opaque hash), NaN-safe + atomic-write, **parquet-only, NO Django migration** (kept off `/edge` ingest and off the user surface until Faza 3); then `alphalens_research/entry_policy/` (train / walk-forward / evaluate) per ADR 0011.

---

## 11. Phasing summary

| Faza | Deliverable | Gate to next |
|---|---|---|
| −1 | anchor-corrected `diagnose_selection` re-run | unfilled edge survives the gap subtraction |
| 0 | entry-grid substrate + `diagnose_entry_grid.py` offline script | — |
| 1 | offline per-arm cost-adjusted comparison (day-block bootstrap) | a static arm beats baseline OOS after cost + multiplicity **AND** residual context-dependence |
| 2 | learned policy [C]/[D], shadow-logged, auto-retrain | the §7 overfit stack passes (incl. power-derived N) |
| 3 | deterministic product surface change (user-owned) | OOS edge held, group communicated |

---

## 12. Decision log (locked with user)

- Approach **A** (per-arm, refined to within-event difference / cost-sensitive classification) — confirmed.
- AI: **design now, build behind the §7 hardened gate** (not the weaker "static beats baseline" gate alone).
- Reward horizon **k=10**.
- NO_FILL = **cash, 0 excess**.
- Arms v1 = **baseline + market_at_arrival + vwap_arrival + narrow_tiers + single_at_close** (all 4 non-baseline).
- Faza 0 ships as a **research script, no persisted parquet column / no config_version** until a static arm proves out.

## 13. Known risks / open items

- The whole program dies cheaply at Faza −1 if the anchor-corrected edge is ~0 — this is by design.
- Execution-cost model is a proxy (no real fills post-ADR-0012); the always-fill arms' ranking is only as good as the haircut.
- At n~few-hundred, the learned policy will likely show no significant OOS lift for a long time; the power analysis (§7.7) makes this explicit rather than hidden.
- k=20 coverage waits on calendar time (~mid-July 2026), not backfill.
