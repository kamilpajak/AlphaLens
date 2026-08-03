# Non-deterministic (ML-ready) exit-policy architecture — design

**Status:** DRAFT (2026-08-03) — awaiting implementation plan.
**Type:** design memo (architecture / refactor, deterministic-first).
**Scope:** the Saxo SIM broker auto-manager exit-logic layer (`apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/` + `broker_contract/exit_geometry/`). SIM-only. No live-money change.

One-line summary: restructure the exit-logic layer into a named `ExitPolicy` registry behind a stateful safety envelope, shipped **deterministic-first with zero ML**, so a future non-deterministic (ML-driven) exit policy plugs in as one more registry entry without touching the protection engine or the loop.

---

## 1. How it works today (problem)

The bot decides a position's exit orders — the disaster stop-loss and the take-profit — in two passes of the same ~45s daemon tick:

- **Placement-time geometry** (once per fresh entry). At arm time `paper/sizing.py::build_exit_geometry_spec` reads ATR off the brief, reconstructs a 52-week-high ceiling, resolves the **hardcoded** `"atr_bracket_1p5"` policy from `broker_contract/exit_geometry/registry.py`, and computes stop = blend − 1.5·ATR, TP = max(blend·1.006, blend + 1.5·ATR) capped at the ceiling.
- **Fill-complete reanchor** (every tick, in `position_manager.py::_maybe_reanchor`). When a position is downside-covered by a lone clean standalone stop, it PATCHes that stop to `avg_price − 1.5·ATR` (an `AmendStop(reason="reanchor-on-fill")`).

Problems with the current shape:

1. **Magic-string switch.** Policy selection is `os.environ["ALPHALENS_BROKER_EXIT_POLICY"]` read live in `_exit_policy()`, branched on with a **binary not-equal test** (`!= "setup_static"`). `"setup_static"` is a sentinel branched on directly, not a registry entry. A third policy would need surgery at two separate call-sites (`_journal_tier`'s `use_geometry` test **and** the reanchor gate).
2. **Reanchor is a bolted-on arm, not part of the policy.** Placement geometry and its reanchor are co-gated by the same flag deliberately (geometry never goes live without its reanchor) — but that coupling lives in flag logic, not in one object.
3. **The safety envelope bounds form, not economics.** Today's geometry checks that levels are constructible and under the 52w ceiling, but there is **no minimum stop-distance floor** and **no monotone-tighten rule**. A future policy could, entirely within the interface, hair-trigger-liquidate (stop too close) or **loosen a live disaster stop** below the brief-sized level.
4. ~~Missing version key.~~ **Already shipped (verified 2026-08-03).** ADR 0013 R3 lists `setup_builder_config_version` (T5) as a known gap, but the key is in fact implemented (`thematic/trade_setup/config_version.py::setup_builder_config_version`) and stamped onto the frozen setup (`model.py::builder_config_version`, emitted by `to_dict()`; `builder.py:211`). The ADR R3 text is stale. No INC-0 work is needed; a one-line ADR correction is optional.

## 2. Goal (why)

Make the exit-logic layer able to accept a **future non-deterministic ML policy** — one whose decisions are probabilistic, not always identical — **without weakening any safety guarantee and without a rewrite when that policy arrives.** The ML is not data-ready until ~Aug–Oct 2026 (see §7), so the deliverable now is the **safe skeleton + the one hard rail that must be battle-tested before any stochastic policy can go live**, not the ML machinery itself.

## 3. Recommended architecture (hybrid, deterministic-first)

A **named `ExitPolicy` registry behind a stateful deterministic safety envelope.**

- The control loop holds only the `ExitPolicy` Protocol plus a single `govern()` envelope call-site. It **never branches on a policy name** and never touches sampling. One policy object owns both placement and reanchor, so the co-gating that today lives in flag logic holds by construction.
- Today's deterministic policies **are** the abstraction (a point-mass / degenerate special case), so there is no parallel deterministic branch to keep in sync — but implemented as a policy that simply returns fixed levels, **without** any distributional wrapper (that wrapper is deferred until a non-point-mass consumer exists).
- The policy is a **pure price/plan oracle**: it proposes stop/TP prices and a reanchor plan. It **never emits an order Action.** All never-naked / never-oversell / realized-quantity guarantees stay 100% inside the untouched `reconcile_protection` diff engine (see §6).

### 3.1 The safety envelope (the key element)

`clamp_to_envelope` becomes **stateful** — it takes the prior/initial stop **plus the live market price** and enforces the **economic content** of any proposed exit, not just its form:

- **(a) minimum stop-distance floor > 0** — the stop can never sit so close to price that ordinary noise liquidates the position.
- **(b) monotone-tighten** — a reanchor may only move the stop **toward less risk** (tighter): never looser than the brief disaster level, never above the live price.
- **(c) degrade-to-safe** — on any non-finite / ≤ 0 / degenerate value, fall back to a safe standalone stop or `None` (no live change), never to no protection.

This is the rail that makes the containment story **true**: a future ML policy can propose anything; the envelope clamps it into a safe range. Because it must be trustworthy before any unpredictable policy moves stops, it ships and is exercised **now**, on the deterministic path, where it costs almost nothing.

Companion telemetry: journal a **raw-vs-clamped divergence** stamp whenever the envelope had to correct a proposal, so a broken future policy is visible rather than silently masked.

## 4. What ships now (deterministic-first, zero ML, zero RNG)

A single acceptance-gated PR sequence:

- **INC-0 — version key. ALREADY SHIPPED** (`setup_builder_config_version` is implemented and stamped, verified 2026-08-03 — see §1.4). ADR 0013's ordering precondition is already satisfied; no work here.
- **INC-1 — registry + envelope (byte-identical live, modulo Decision 1).**
  - Introduce the `ExitPolicy` Protocol + a named registry (`broker_contract/exit_geometry/registry.py`).
  - `SetupStaticPolicy` becomes a real inert/null registry entry replacing the magic-string sentinel: `decide_placement_geometry` returns inert levels (static brief disaster-stop/TP still journals; nothing goes live), `decide_reanchor` returns `None`.
  - `AtrBracket1p5Policy.decide_placement_geometry` wraps `atr_bracket_levels(blend, atr, ceiling)`; `AtrBracket1p5Policy.decide_reanchor` returns `avg_price − 1.5·ATR` — the current `_maybe_reanchor` body verbatim, so reanchor is that one policy's method, not a third policy.
  - `_exit_policy()`, `_journal_tier`'s `use_geometry` test, and the reanchor gate all switch on policy **name via the registry**, not a not-equal check.
  - `clamp_to_envelope` made stateful per §3.1.
  - No `rng` parameter, no per-decision record, no parquet widening, no `maybe_exit_now`.
  - Every safety acceptance test (`test_every_position_protected`, `test_no_oversell`, `test_safety_rails`) stays byte-green.

## 5. Decisions (the three forks)

Resolved by the operator on 2026-08-03:

1. **Economic clamps live now, not inert.** The min-distance floor + monotone-tighten rule are enforced **live on the deterministic reanchor now**, accepting that they may diverge from today's `atr_bracket_1p5` reanchor in edge cases (e.g. avg fill above the planned blend), which breaks the strict byte-identical-refactor guarantee. Rationale: the one rail that gates live ML risk must be exercised before the risky moment; SIM makes any divergence observable and cheap.
2. **`maybe_exit_now` deferred entirely.** No reserved placeholder method (extract-on-second-use). `close_now` has no valid wire form under the current Action union anyway; add it only when a real in-flight-exit consumer exists.
3. **Replay-first, not live randomization.** To compare two exit geometries, use **dual-deterministic what-if replay over every cached episode** (zero confounding, no N-halving, no Bonferroni slot burned) before spending a live registry slot on randomization. Live randomization is justified only if resting-order path-dependence genuinely escapes replay.

## 6. Invariants that stay untouched

The policy proposes prices; the diff engine (`reconcile_protection` / `_reconcile_long`) enforces safety. These must stay byte-green through INC-0/INC-1 (verified in `acceptance/`):

- **INV-1 never-naked** — every owned long > 0.5 sh has resting SELL commitment ≥ owned by tick end; new stop placed before stale stop cancelled.
- **INV-2 never-oversell** — resting SELL commitment never exceeds netted owned (OCO pair counted once).
- **INV-3 protection pass unconditional every tick** — KILL halts only new placement, never protection of open positions.
- **INV-4 fault isolation** — a BrokerError on one uic never aborts the tick or starves another uic.
- **INV-5 capability degrades to a strictly-safer primitive, never to no protection.**
- **INV-6 realized-qty-only sizing** — every stop/OCO/amend sizes to the live broker-reported quantity, never a planned tier quantity.
- **INV-7 standalone-stop is a hard startup gate** (`SupportsStandaloneStop`).
- **INV-8 geometry/AmendStop co-gating** — dynamic geometry requires `SupportsAmendStop` (fail-fast at startup). Preserved by construction once reanchor is the policy's own method.
- **INV-9 placement rails gate only new picks**, never protection of open positions.
- **INV-10 idempotent retries** — deterministic request-id stamps; genuine resize gets a distinct ref.

## 7. Deferred to ML-ready

Everything stochastic and everything that only serves a stochastic consumer: the seedable RNG + seed derivation, the per-decision record / sampled-action shadow stamp, the distributional output, the nested `breakeven_realized_r_json` widening (prefer a companion column when it ships), the replay seed-threading + idempotence self-check, and the `maybe_exit_now` in-flight-exit capability (added only when its first consumer exists).

When that apparatus is built it must inherit the critique fixes: inline feature-snapshot **values** (not a pointer into a compacted store), a numpy/library-version fingerprint in the poolability key, a content-addressed policy artifact (not a bare version string), and a defined PIT-safe per-tick seed coordinate (or a ban on per-tick sampling).

Timeline (data-readiness audits 2026-07-14 / 07-30): SIGNAL T1/T2 MARGINAL (full ~27-feature model ~early Oct); LADDER T5 MARGINAL; IN-FLIGHT T6 NOT_ENOUGH (46 ep / 24 clusters, below the 25–30 grouped-CV floor — single-split tree ~mid-Sep, real policy learning ~Oct–Nov). Options/market-state arms mature later (ivx30 matured floor N=25 ~08-21 / N=30 ~08-28; strict `chain_quality=OK` cluster floor ~early Oct). ML re-run window ~2026-08-21..28.

**Graduation path:** a future learned or hand-specified stochastic policy ships **display-only** as one of the ≤5 registry lenses (`feedback/breakeven_lenses.py`, `MAX_REGISTERED_LENSES=5`), counts toward the Bonferroni / walk-forward budget, and becomes live-affecting **only** via the ADR 0013 R4 pre-registered forward walk-forward — never before.

## 8. Extension seams (reference for the implementation plan)

- **Registry seam:** `broker_contract/exit_geometry/registry.py::EXIT_GEOMETRY_POLICIES` + `resolve_policy(name)`. Note `sizing.build_exit_geometry_spec` **hardcodes** `resolve_policy("atr_bracket_1p5")` — a policy-name parameter must be threaded through the spec/brief for a new policy to be selectable.
- **Reanchor seam:** `PlannedExit.reanchor` + `ReanchorFacts(k_atr, atr)` + `_maybe_reanchor`. Minimal by design (stop-only). A TP-reanchor or trailing policy needs a new facts dataclass and a new arm parallel to `_maybe_reanchor` inside `_reconcile_long`; `_reanchor_facts_from_governing` / `ReanchorFacts` must generalize past `k_atr·atr` if the formula differs.
- **Geometry-stamp wire format:** `_journal_tier`'s `geometry_stamp` dict (`policy_name/policy_version/k_atr/atr/ceiling_price/applied`) is parsed back by `_reanchor_facts_from_governing`. This namespaced additive dict (never read by `_fold_planned_exits`, so additions cannot silently change live behavior) is the natural place to later add `rng_seed / sampled_action / feature_snapshot_ref`, following the PR-6a pattern that stamped deterministic geometry dark before any flip.
- **Lens seam (what-if):** `feedback/breakeven_lenses.py::BreakevenLens` — add a new `kind` alongside `breakeven/fill_anchored/atr_bracket`; registry dispatch + the JSON-map column absorb any `lens_id` with no schema/UI change. A stochastic replay needs an explicit RNG-seed parameter threaded into a new `ladder_replay.py::replay_ladder_<policy>` and the scalar column widened to persist seed + sampled_action.
- **Capability-gate seam:** `control_loop.build_default_deps` (~738) — add a fail-fast for any new capability a future policy's reanchor needs, mirroring `SupportsStandaloneStop / SupportsAmendStop / SupportsOcoExit`.
- **Verdict-routing seam:** any auto-cancel/auto-close addition MUST preserve the no-automated-time/divergence-close design (42-session TIME_STOP stays research-only).

## 9. Risks / open notes

- **Byte-identical break (accepted).** Decision 1 makes INC-1's live output potentially diverge from today's reanchor in edge cases. Mitigation: the raw-vs-clamped divergence telemetry surfaces every clamp; SIM watch confirms the divergence is only the intended safety correction.
- **Interface-freeze risk.** Fixing the `ExitPolicy` method surface before the ML shape is known (scorer vs distributional vs RL) could freeze the wrong contract. Mitigation: keep the surface to the two live decisions (`decide_placement_geometry`, `decide_reanchor`); do **not** add `maybe_exit_now` or a distributional return until a real consumer exists.
- **Workflow coverage gap.** The design fan-out evaluated only two of four architectures (the continuous-scorer and RL-action-space variants failed on a schema-retry cap). The recommendation deliberately defers the ML-shape choice, so this does not affect the near-term deliverable; re-run those two when the ML consumer is data-ready.

## 10. Test plan

- **INC-0:** unit test that the T5 `setup_builder_config_version` key is stamped and stable; no behavior-change assertion (existing acceptance suite stays green).
- **INC-1 registry:** unit tests that `resolve_policy("setup_static")` returns the inert policy (placement journals static levels, `decide_reanchor` → `None`) and `resolve_policy("atr_bracket_1p5")` reproduces today's placement + reanchor; a structural test that the loop selects on policy name, not a not-equal string test.
- **INC-1 envelope:** unit tests for the stateful clamp — min-distance floor rejects a too-close stop; monotone-tighten rejects a looser-than-prior reanchor and accepts a tighter one; degenerate (NaN / ≤ 0) input degrades to safe/None; a divergence stamp is journaled when a proposal is corrected.
- **Safety regression:** `test_every_position_protected`, `test_no_oversell`, `test_safety_rails` stay byte-green.
- **Known gap:** no stochastic-path test now (that path is deferred); the divergence-telemetry read-side and /edge widening are out of scope until the ML consumer ships.
