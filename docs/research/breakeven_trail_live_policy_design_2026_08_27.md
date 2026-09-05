# `breakeven_trail` — live exit policy (break-even +0.5R · fractional-giveback trail 0.6)

**Status:** LOCKED (owner decision 2026-08-27)
**Issue:** #1162. Context: #1112 (TP anchor incident), #1115 (voided prereg — see its status note), #1160 (WHAT-IF panel labels).
**Supersedes:** `trailing_execution_design_2026_08_07.md` §3 (whose phase-2 trail was a `0.6·ATR` offset and whose SL route assumed a native `TrailingStopIfTraded` conversion — neither applies here).
**Lens ancestor:** the registered what-if lens `be_0p5r_trail0p6` ("break-even +0.5R · trail 0.6", `breakeven_lenses.py`, replay `ladder_replay.replay_ladder_breakeven`).

---

## 1. What the policy does

The live port of the `be_0p5r_trail0p6` lens. It returns exit SELLING to the brief's own
research levels and confines the policy to STOP management:

1. **Placement** — `applies_geometry=False`: the journaled `planned` lines carry the brief's
   per-tier TPs and the brief disaster stop verbatim; the journaled `tranche_plan` carries the
   brief's multi-tranche TP ladder. The live-exit engine (INC-5) sells each tranche at its
   research level. No geometry override anywhere.
2. **Risk unit** — 1R = `avg_price − plan_stop`, where `plan_stop` is the journaled brief
   disaster floor (`plan.stop_price`; under `applies_geometry=False` every tier journals the
   same `placement.disaster_stop_price`, so the MAX-across-tiers fold equals the lens's single
   `ladder.disaster_stop`). NOT an ATR multiple — `atr` is ignored by `decide_reanchor`.
3. **Arming** — dark until the tracked peak reaches `avg_price + 0.5R` (non-strict, mirroring
   the lens latch).
4. **Trail** — once armed, target = `max(avg_price, avg_price + 0.6×(peak − avg_price))`
   (`fractional_giveback_target` in `levels.py`). At the arming instant this already sits at
   entry+0.3R — the lens behaves identically; the "break-even" in the name is the lens label,
   kept for continuity.
5. **Delivery** — the existing bot-amend machinery: `_maybe_trail` → `clamp_reanchor_target`
   (anchored on the live price, never below the brief floor, `min_stop_distance_frac=0.002`)
   → `_TRAIL_STEP_EPS` ratchet vs the journal-confirmed trailed level → `AmendStop` on the
   resting `StopIfTraded`. No native trailing order is involved.

Profit therefore realizes through BOTH paths: TP tranches at research levels, and — after
arming — the trailed stop on a reversal. A full loss (−1R at the disaster stop) remains
possible only while the +0.5R trigger has never fired.

## 2. Contract facts

Registry entry `"breakeven_trail"`: `BreakevenTrailPolicy(activation_r=0.5, trail_frac=0.6,
name="breakeven_trail")`; `geometry_name=None`, `applies_geometry=False`,
`requires_amend_stop=True`, `trails=True`, `min_stop_distance_frac=0.002`, `version=1`.
`decide_reanchor` gained a kw-only `plan_stop` parameter on the Protocol and all
implementations (ignored by the ATR-family policies). This is the first policy with
`applies_geometry=False ∧ trails=True`; the wiring PR adds an end-to-end test for that
combination.

## 3. Documented live-vs-lens gaps (accepted for v1)

1. **Peak fidelity.** The lens peak is the running max of minute-bar HIGHs (trade prints);
   the live peak is the running max of ~45 s-sampled BIDs (`control_loop._update_peaks`),
   veto-dropped on stale/delayed quotes and reset on daemon restart. The live stop therefore
   sits looser and arms later; a sub-tick spike to +0.5R can be missed entirely. Follow-up
   (own issue, after the SIM soak): a 1 Hz `_running_high` accumulator mirroring
   `QuoteCache._update_running_low`, drained into `peak_tracker`.
2. **Restart.** The peak resets (arming can go dark until price re-crosses the threshold);
   the journal-folded trailed floor guarantees the placed stop never loosens.

   *Restart during a session.* Clearing the in-memory peak pauses only the UPSIDE trail —
   protection is unaffected, because the placed stop is held by the journal-folded trailed
   floor and `_maybe_trail`'s ratchet refuses to lower it. The cost depends on where price
   sits: restart with price already at or above `avg + 0.5R` and arming re-triggers on the
   same tick, so nothing is lost; restart below the threshold, or after a reversal that had
   already armed, and the trail stays dark until price advances past the threshold again —
   minutes or hours. Prefer restarting outside XNYS hours (13:30-20:00 UTC). If an
   intra-session restart cannot be avoided, check afterwards that trailing resumes on any
   position that was armed before it.
3. **Slippage/gaps.** The lens books SL fills exactly at the stop level and TP fills exactly
   at target; live pays gap-throughs and market-sell slippage.
4. **TTL.** The lens ignores entry-TTL/TIME_STOP; the live rails keep the 7-day entry TTL.
   *Closed for the entry side on 2026-09-01 (issue #1232): the TTL-honouring twin
   `be_0p5r_trail0p6_ttl7` replays the production entry-TTL fill cohort (session-OPEN
   cutoff, shared with the headline `realized_r`); this lens stays as the no-TTL
   what-if. The position TIME_STOP remains unapplied in every lens.*
5. **`avg_price` drift after partial sells.** The live anchor is the broker's netted
   `pos.avg_price`; whether Saxo's lot accounting shifts it after a TP tranche sells is to be
   verified empirically during the SIM soak (compare before/after a tranche fire). The same
   exposure already existed under `trailing_atr` (its arming anchor). If it drifts, a
   follow-up pins the entry blend at placement time.
6. **ATR guard.** `_maybe_trail` keeps its `plan.reanchor.atr` finite-positive guard even
   though this policy ignores ATR — the geometry shadow stamp is unconditional, so the guard
   is inert in practice; decoupling it is a possible follow-up, not v1.

## 4. Interaction fix shipped with the wiring PR

Multi-tranche selling under a trailing policy exposes a latent defect: the live-exit engine
amends the SL down to the PLACEMENT-TIME journaled stop when a tranche fires
(`execute_tranche_exit(stop_price=m.stop_price)`), which would reset a trailed stop back to
the disaster level. Fix: `_build_managed_exits` folds the trailed markers and uses
`max(plan stop, trailed level)`, with a GENERATION GUARD — markers older than the current
position's plan line are ignored (the fold is uic-keyed and journal-lifetime, so a new
position in a previously-traded uic must not inherit the old position's trailed level). The
same ts-gate is applied to the `trailed_stop_by_uic` fold feeding `_maybe_trail`, closing the
documented stale-floor caveat. This retroactively hardens `trailing_atr` too.

## 5. Rollout

SIM first: `deploy/systemd/alphalens-broker-manager.service.d/80-exit-policy.conf` →
`breakeven_trail`, host copy + restart, soak (journal shows the brief ladder in
`tranche_plan`, `trailed` markers under the new policy, tranche fires preserving the trailed
floor, the §3.5 `avg_price` check). The LIVE unit flip is a separate later PR on explicit
owner confirmation. The registry keeps `setup_static` / `atr_bracket_1p5` / `trailing_atr`
selectable — switching policies remains a config change, per the owner's directive to keep
the codebase able to choose.

**LIVE flip done 2026-08-28** (owner confirmation), pinning
`ALPHALENS_BROKER_EXIT_POLICY=breakeven_trail` in
`deploy/systemd/alphalens-broker-manager-live.service`. Both instances now run one policy.

What the SIM soak had actually shown at that moment, stated plainly because the flip did not
wait for the rest: the brief ladder in `tranche_plan` was confirmed — for `SAIC:2026-08-26`
the journaled targets `180.91514536839634` / `211.0057292791067` are identical to the
brief's own `brief_trade_setup.tp_tranches`. The other three observations were NOT yet
available: the SIM journal held ten `tranche_plan` lines but **zero** `tranche_fired` and no
`trailed` markers, so "tranche fires preserving the trailed floor" and the §3.5 `avg_price`
drift check remain unobserved on either instance. The `avg_price` exposure is not new — it
existed identically under `trailing_atr` (§3.5) — but it is now carried on LIVE under a
policy whose tranche-fire interaction has never been seen in a journal.

## 6. Measurement stance

No new pre-registration is created here. The `be_0p5r_trail0p6` lens keeps accruing as
display-only telemetry under its existing registration. Any future head-to-head involving
this policy is a NEW prereg with its own ledger row (the 2026-08-24 prereg's §11 item 1 void
note records that nothing is pre-authorized).

## 7. Addendum 2026-09-05 — the policy does not reach manual picks (#1325)

The memo above was written as if one daemon-wide policy governs every position. It does
not. A pick armed through `alphalens broker arm-manual` carries `exit=None`, so
`control_loop._geometry_shadow_stamp` returns `None`, so its `planned` journal line has no
`geometry` key, so `PlannedExit.reanchor` folds to `None` — and BOTH post-fill stop-move
arms refuse on exactly that. A manual pick is therefore **policy-immune**: its stop is
placed once and never moved, whatever `ALPHALENS_BROKER_EXIT_POLICY` names.

Measured, not reasoned. Running the real `_reconcile_long` on the AMBA LIVE shape of
2026-09-04 (8 @ 59.00, disaster stop 55.00 so 1R = 4.00 and the 0.5R activation sits at
61.00, session peak 62.78, well above it):

| policy | manual shape (no stamp) | same numbers WITH a stamp |
|---|---|---|
| `setup_static` | NoOp | NoOp |
| `atr_bracket_1p5` | NoOp | AmendStop |
| `trailing_atr` | NoOp | AmendStop |
| `breakeven_trail` | NoOp | AmendStop |

The right-hand column is the positive control: the check can produce the disproving
observation, and did not.

Scope of the gap, read off the two VPS journals on 2026-09-05. At the INTENT level the
split is exact: every pick recorded with `source: manual` carries `exit = None`, and every
brief pick that reached an armed position carries an exit spec (LIVE: OLN, SMG, GME). At
the JOURNAL level the same split shows on the entry-trail-era rows — on LIVE the stamped
`*-entry-tN-fire` lines are GME / OLN / SMG and the unstamped ones are AMBA / RHI.

Two things that a coarser read would get wrong, both checked:

* the `planned` rows with plain uuid `client_request_id` predate the entry-trail path and
  are mixed (some stamped, some not, on SIM), so "unstamped implies manual" is FALSE as a
  general statement about the file — it holds only within the entry-trail-era rows;
* SIM carries GME twice, once as a brief pick (exit spec set) and separately as a manual
  one (`exit = None`), so its single stamped `GME-2026-08-27-entry-t0-fire` line belongs to
  the brief arming and is not a counterexample.

So on today's population the policy governs brief picks only, and §5's "both instances now
run one policy" is true of the resolved env var, not of the positions.

**Decision (owner, 2026-09-05): manual picks keep this behaviour — the daemon holds their
disaster stop and never tightens it.** The basis is NOT the counterfactual replay (n=2
changed rows on LIVE, both worse, but the sample contains no reversal — the one scenario a
trail exists for, so it cannot settle the question). It is:

* the trail's risk unit is `avg_price - plan_stop`, and on a manual pick `plan_stop` is
  hand-set, so 0.5R is a different quantity on every pick — 6.8% of entry on AMBA (inside
  a single session's range), 29% on RHI (unreachable);
* the exit of a group-managed pick is a human decision.

Consequences recorded so the decision does not decay:

1. The `plan.reanchor is None` guard is now LOAD-BEARING for this decision, not merely a
   pre-PR-6a compatibility check. Relaxing it in isolation — e.g. to let `breakeven_trail`
   through, which is defensible on its own terms since that policy discards `atr` entirely
   — turns trailing ON for exactly the picks the decision excludes. Both arm docstrings say
   so, and `tests/brokers/automanager/test_manual_pick_no_stop_move.py` goes red on it.
2. The supported way to trail one manual pick is the per-pick policy override, issue #1236.
   Nothing else should be built for it.
3. Residual, named and NOT fixed: a BRIEF pick whose `exit_spec` fails to build (degenerate
   ATR, no usable tiers) is also silently untrailed, even though `breakeven_trail` needs no
   ATR. Measured blast radius on 2026-09-05: zero — all three LIVE brief `planned` lines
   carry the stamp. It becomes real only if brief arming starts producing exit-less intents.
4. §3's "1 Hz `_running_high`" follow-up (#1166) is not on the path to any of this: the
   sampling rate moves the AMBA outcome by ~2 bps while trail-or-not moves it by ~364 bps.

### 7.1 The second route to a moved stop, and why it is closed twice

"Policy-immune" is a claim about `_reconcile_long`. There is a second route that does not
pass through its guard: `trailed_stop_by_uic` is a JOURNAL-lifetime fold, so a level earned
by an earlier position on the same uic can outlive it — and SIM really carries that shape
(GME armed once from a brief and separately as a manual pick, same uic). Both consumers of
that map were run, not reasoned about:

* `_maybe_trail` only GATES a proposal against the floor; it never proposes the floor
  itself. With an inherited level injected, all four policies still return `NoOp` on the
  manual shape.
* `control_loop._build_managed_exits` DOES take `max(plan stop, trailed)` and place it. It
  is closed by two different mechanisms depending on the pick:
  - a manual pick WITH TP tranches journals its own `tranche_plan` under a new `pick_key`,
    so the generation reset clears the inherited marker (measured: the fold goes from
    `{uic: 61.5}` to `{}` when the manual plan line is appended);
  - a manual pick armed `--no-tp` journals NO `tranche_plan` at all
    (`_journal_tranche_plan_core` returns early on an empty ladder), so the marker survives
    the fold — but `_build_managed_exits` skips any uic with no tranche plan, so it is never
    placed.

The second bullet is the one worth remembering: that case is safe for a reason unrelated to
the reset, so a future change that starts journaling a ladder for `--no-tp` picks, or that
makes the builder tolerate a missing plan, reopens it. Both are pinned in
`test_manual_pick_no_stop_move.py`, each with a positive control showing the level DOES
come through when the plan is present.
