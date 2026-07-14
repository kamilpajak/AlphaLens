# ADR 0013 — Trade-side layer architecture (live tool)

- **Status:** Accepted
- **Date:** 2026-07-14
- **Supersedes:** —
- **Sibling:** ADR 0007 (research-lab layer architecture). This ADR is the
  symmetric decomposition for the LIVE thematic tool's trade side.

## Context

ADR 0007 gave the research lab an explicit 5-layer decomposition so that every
failure attributes to exactly one layer. The live thematic tool grew its own
trade-side stack (brief → trade setup → broker-free replay → EDGE dashboard)
without an equivalent canonical decomposition. Three concrete events forced
this ADR now:

1. **The `tp1_r > 1.5` collider** (evidence record:
   `docs/research/tail_filter_tp1r_collider_2026_07_14.md`). A covariate
   computed from the ladder's own geometry looked like a selection signal on
   the fill-dependent excess outcome (p ≈ .038) and evaporated on the
   fill-independent `car_10` with controls (p ≈ .93); the mechanism was pure
   conditioning — P(SL_HIT | tp1_r > 1.5) = 93.9% vs 6.8% otherwise. Without a
   named layer boundary between SETUP output and SELECTION input, that
   covariate was one review away from entering the funnel.
2. **An unnamed IN-FLIGHT layer.** Counterfactual exit lenses
   (`BREAKEVEN_LENSES` registry, `ratchet_realized_r`) are accumulating on
   `/edge` with no stated contract for what they may and may not do.
3. **A missing version key on the setup geometry.** The geometry constants
   spread across `trade_setup/` (`builder.py:35-41` — `_MIN_BARS`,
   `_SWING_THRESHOLD_MULT`, `_STOP_ATR_BUFFER`, `_SHALLOW_PULLBACK_MULT`,
   `_DEEP_FALLBACK_MULT`, `_DISASTER_FLOOR_FRAC`, `_DEFAULT_RISK_BUDGET_PCT`;
   `ladder.py:13-15` — tier spacing, stop distance, TP R-multiple fallbacks;
   `levels.py:14`; `sizing.py:21`) carry no poolability key; `SCHEMA_VERSION`
   tracks JSON structure, not geometry. The planned September exit-geometry
   pre-registration (`exit_geometry_reward_risk_2026_06_30.md` §7, not yet
   filed) would silently pool incomparable ladders.

The closest existing decomposition is
`docs/research/edge_tuning_attribution_data_readiness_2026_06_11.md`
(§3.1 three measure layers, §4.1 the 2x2 selection-vs-ladder firebreak,
**§4.2 the limits that override both**). This ADR **canonizes** that memo —
including §4.2 — as architecture; it does not duplicate its analysis.

## Decision

Adopt an **8-layer architecture** for the live tool's trade side, numbered
**T1-T8** to avoid collision with the two legacy numbering schemes already in
the codebase (the mapping pipeline's "Layer 1-5" docstrings and ADR 0007's lab
layers; note the legacy names `layer4_weighted_score` and `selection_score`
belong to T3 ORDERING). Each layer has a single responsibility, a causality
contract, and a poolability/config-version key. Failures attribute to exactly
one layer.

| # | Layer | Operates on | Modifies | Causality contract | Version key | Measured at |
|---|---|---|---|---|---|---|
| T1 | **SIGNAL** | news events (RSS/GDELT/Polygon/EDGAR) → themes → LLM proposal | which tickers are *proposed* | LLM reasons over injected pre-computed facts only; no numeric brackets in prompts; catalyst source-gate on the anchor event | `mapper_config_version` (`mapping/orchestrator.py:334`; currently SHARED with T2 — a noted gap, mirror of the T5 key gap) | proposal-level head-to-head (`proposal_shadow`, hypothesis-budget cluster 19) |
| T2 | **SELECTION** | proposed candidates | which tickers enter the brief (mcap filter → 3 OR verification gates → cap=3/theme, budget=5) | consumes ONLY pre-setup, fill-independent inputs (R1, R2) | `mapper_config_version` (shared with T1, see above) | substrate outcome: `market_excess_return` / `car_k` anchored to `reference_close` (`brief_date` is T-1 of the run day) |
| T3 | **ORDERING** | selected candidates | rank within the brief: `selection_score = layer4_weighted_score − atr_penalty` (`screening/scorer.py:609-614`) | orders the already-selected set; never re-admits or drops | `scorer_config_version` (`scorer.py:375`) | rank-vs-substrate correlation |
| T4 | **DISPLAY** | anything (experts, insider, options, market state, chips) | pixels only — **never** the score or the set | strictly read-only w.r.t. T1-T3; stamped now for forward calibration | `insider_signal_version`, `MARKET_STATE_CONFIG_VERSION`, `OPTIONS_CONFIG_VERSION`, `panel_config_version` | the hypothesis-budget ledger (held-out confirmation, fixed cluster family, α-spending; N≥30 ticker-episodes is the *minimum*, not the gate — `edge_hypothesis_budget_2026_07.md`) |
| T5 | **SETUP / LADDER** | one selected ticker's price history | the frozen `trade_setup` JSON on the brief: levels → disaster stop → entry tiers → equal-risk sizing → TP tranches → order TTL (`thematic_trade_setup_v1_design_2026_05_27.md`) | deterministic; built strictly AFTER selection; output feeds only downstream layers | `SCHEMA_VERSION` (structure) + **`setup_builder_config_version` (geometry — MISSING, see Consequences)** | as-specified `realized_r` **plus NO_FILL rate + `tiers_filled_count`** (a NO_FILL is invisible to `realized_r` yet attributes to T5 geometry) |
| T6 | **IN-FLIGHT** | an open position between entry and terminal | today: **nothing** — no live mutation; terminals are frozen (`population_ladder_monitor.py:1369`, "frozen — no new pricing") | all in-flight policies exist ONLY as counterfactual what-if lenses (`BREAKEVEN_LENSES`, `ratchet_realized_r`) until graduated (R4) | `lens_id` + `status="in_sample"` per registry entry | what-if counterfactual outcome per lens |
| T7 | **EXIT** | the stop/TP policy embedded in the setup | when an open position terminates (disaster stop, TP tranches, TP-hit ratchet, 42-session time-stop) | changes ship only through a pre-registered purged+embargoed walk-forward (planned ~2026-09, exit-geometry memo §7), behind a version stamp | `ladder_config_version` (`feedback/ladder_config.py:44`) + the future `setup_builder_config_version` | as-specified `realized_r` vs the what-if grid |
| T8 | **MEASUREMENT** | matured outcomes (broker-free replay per ADR 0012) | nothing upstream — telemetry/firebreak only | three measure layers per attribution memo §3.1: **substrate** (fill-independent), **as-specified** (`realized_r`), **what-if**; the 2x2 firebreak (§4.1) attributes fault to SELECTION vs LADDER — **subject to §4.2 limits**: raw counts only until each off-diagonal cell clears N≥30, anchors are not orthogonal, entry misses need their own category | `ladder_config_version` on every outcome row | — (it *is* the measurement) |

Dependency direction along the trade path is strictly downward:
**T1 → T2 → T3 → T5 → T7 → T8**. DISPLAY (T4) and IN-FLIGHT (T6) are read-only
side branches: T4 reads T1-T3 plus T8 outcomes and feeds nothing; T6 is
computed *by* T8 over T5's frozen setup, and its only edge into T7 is
graduation per R4 — a policy promotion, not dataflow. T8 reads everything and
writes nothing upstream.

**Termination ownership:** entry-side termination belongs to T5
(`order_ttl_days` is a frozen setup field; a NO_FILL — never engaged within
TTL — attributes to T5 geometry). Position-side termination (disaster stop /
TP / 42-session time-stop, `paper/constants.py:55`) belongs to T7. Both sides
are keyed by `ladder_config_version`, whose token deliberately spans T5 and T7
(`time_stop_days` + per-row `order_ttl_days`).

**Measurement boundary:** T8's source of truth is the parquet store
(`~/.alphalens/population_ladders/`). The Postgres `edge_ladderoutcome` mirror
and the `/edge` SPA are transport of T8 output — mirror staleness is an ops
fault (cf. the frozen-`/edge` incidents of 2026-06/07), never a measurement
fault.

### Hard rules

- **R1 — Substrate-first for selection covariates.** A SELECTION covariate must
  show signal on the FILL-INDEPENDENT substrate outcome before it may be
  considered. Evidence: the `tp1_r > 1.5` collider
  (`tail_filter_tp1r_collider_2026_07_14.md` — p .038 fill-dependent, p .93 on
  `car_10` with controls; P(SL_HIT | tp1_r>1.5) = 93.9% vs 6.8%).
- **R2 — SETUP outputs never feed SELECTION.** The setup is built AFTER
  selection; a filter on any setup output (fill depth, tier prices, stop
  distance, `tp1_r`) inverts the DAG, so an exit-rule change would silently
  shift which tickers get selected. Evidence: same collider — `tp1_r` is a
  deterministic function of ladder geometry, not of the pick.
- **R3 — Every layer carries its own poolability/config-version key, and a key
  bump is a cohort boundary:** analyses never pool across it and existing rows
  are never restamped (forward-only; terminals stay frozen). The sole
  retroactive surface is the what-if layer — T6/T8 lenses may be recomputed
  over already-cached bars at any time. Existing keys: `scorer_config_version`,
  `ladder_config_version`, `insider_signal_version`, `novelty_config_version`,
  `MARKET_STATE_CONFIG_VERSION`, `OPTIONS_CONFIG_VERSION`,
  `mapper_config_version`. Known gaps: `setup_builder_config_version` missing
  (T5), and T1/T2 share one key.
- **R4 — IN-FLIGHT policies live as display-only lenses until graduated.**
  Requirements per registered lens: `in_sample` label, winners-harmed
  accounting first-class, a registry cap of **5** concurrent lenses (constant
  `MAX_REGISTERED_LENSES` to be added beside `BREAKEVEN_LENSES`), and every
  registered lens — including retired ones — counts toward the walk-forward's
  multiplicity budget. Graduation = pre-registered forward validation, nothing
  less. Evidence: the break-even lens flipped mean R −0.371 → +0.069 in-sample
  (`exit_geometry_reward_risk_2026_06_30.md` §4) and was still held to
  display-only, correctly — §5 lists why in-sample counterfactuals overstate.
- **R5 — Failures attribute to exactly ONE layer.** ADR 0007's founding
  principle, transplanted; the 2x2 firebreak (attribution memo §4.1,
  `sign(market_excess)` × `sign(realized_r)`) is the instrument, applied under
  the §4.2 limits (raw counts until off-diagonal N≥30; anchor
  non-orthogonality; NO_FILL as its own category). Evidence: the exit-geometry
  diagnosis (payoff 0.22:1) attributed cleanly to T7 while selection was
  simultaneously +14% — two independent verdicts on one dataset.

## Consequences

**Positive:**

- The tp1_r-class error is now structurally nameable: "that covariate is a T5
  output offered to T2" is a one-line rejection.
- IN-FLIGHT has a name and a contract, so lens accretion on `/edge` is bounded
  (cap, label, multiplicity charge) instead of ambient.
- The September exit change has a defined shipping path: pre-registered
  walk-forward + version stamp, with poolability keys on both sides.
- Attribution memo §3.1/§4.1/§4.2 is promoted from "a memo we remember" to
  architecture that new sessions load from `docs/adr/`.

**Negative / cost:**

- One more table to keep in sync with code. Mitigated: the version keys are
  code constants, and the enforcement tests below fail loudly on drift.
- R1 cannot be fully machine-enforced (it constrains analysis practice, not
  imports); it lives as doctrine plus review checklist.

**Action items:**

1. **Ship `setup_builder_config_version` BEFORE the September exit change.**
   A single derived token over ALL `trade_setup/` geometry constants —
   `builder.py:35-41` AND `ladder.py:13-15`, `levels.py:14`, `sizing.py:21`
   (a builder-only token would still let a TP-R-multiple or spacing change
   pool silently) — mirroring the `ladder_config_version(...)` pattern,
   stamped into `trade_setup` next to `SCHEMA_VERSION` and mirrored onto
   outcome rows.
2. **Tests to enforce:**
   - Dependency direction: no module in `thematic/{mapping,screening}` imports
     from `thematic/trade_setup` or `feedback/` (mirror the
     `test_module_dependencies.py` AST-walk pattern; verified 2026-07-14 to
     pass today with no allowlist).
   - Version-key presence: each layer's key constant exists and is stamped on
     its artifact (candidate parquet / trade_setup / outcome rows).
   - Lens registry: every `BREAKEVEN_LENSES` entry carries
     `status="in_sample"` until a graduation record exists; registry length
     ≤ `MAX_REGISTERED_LENSES` (5).
   - Existing sort-lock tests (`_NON_EXPERT_SORT_ALLOWLIST`) already pin R2's
     DISPLAY-side twin (display fields never enter ordering); keep them.

## What this ADR does NOT cover

- **The research lab** — screener/gate/engine/overlay/attribution remain
  ADR 0007's domain. The two stacks meet only at shared data infrastructure
  (ADR 0011 DAG).
- **Portfolio-level sizing** — none exists; sizing is per-name equal-risk
  inside T5 (`suggested_size_pct`, 1% risk budget). There is no trade-side
  analogue of ADR 0007's risk overlay.
- **The planned September pre-registrations** (exit walk-forward, V2A scorer
  calibration, expert×EDGE) — those remain their own design docs; this ADR
  only fixes which layer each one is allowed to change.
- **Broker execution** — there is none; all outcomes are broker-free
  price-path replays per ADR 0012.

## References

- ADR 0007 — research-side layer architecture (sibling).
- ADR 0011 — pipeline/research workspace split (dependency DAG).
- ADR 0012 — broker chain decommissioned; outcomes are broker-free replays.
- `docs/research/edge_tuning_attribution_data_readiness_2026_06_11.md` —
  §3.1 three measure layers, §4.1 the 2x2 firebreak, §4.2 the overriding
  limits (all canonized here).
- `docs/research/tail_filter_tp1r_collider_2026_07_14.md` — the founding
  evidence for R1/R2 (sweep + refutation, full numbers).
- `docs/research/thematic_trade_setup_v1_design_2026_05_27.md` — the
  deterministic setup builder.
- `docs/research/exit_geometry_reward_risk_2026_06_30.md` — §4 counterfactual,
  §5 caveats, §7 the planned walk-forward path (~2026-09).
- `docs/research/edge_hypothesis_budget_2026_07.md` — SELECTION / ORDERING /
  DISPLAY enforced-disjoint split; the hypothesis-budget ledger (fixed cluster
  family, α-spending, promotion mechanics).
- Code anchors: `thematic/trade_setup/builder.py:35-41`, `ladder.py:13-15`,
  `levels.py:14`, `sizing.py:21` (keyless geometry constants),
  `thematic/screening/scorer.py:375,609-614`, `feedback/ladder_config.py:44`,
  `feedback/breakeven_lenses.py:70`, `feedback/population_ladder_monitor.py:1369`,
  `mapping/orchestrator.py:331,334`, `market/market_state.py:66`,
  `thematic/options_telemetry/features.py:18`,
  `thematic/screening/insider_signal.py:60`, `paper/constants.py:55,66`.
