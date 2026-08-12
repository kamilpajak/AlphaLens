# Broker sizing — two paths: declared frame (A, now) and risk-unit re-denomination (B, target)

**Date:** 2026-08-12
**Status:** LOCKED 2026-08-12 — operator accepted §7 verdicts incl. both amendments (MAX_FEE_BPS=1000 backstop semantics; NVAX knife-edge consequence at X=150)
**Method:** 3-lens design workflow (risk/ops, product/target, code-fit) + 2 code-verifying critics (facts, adversarial "what breaks money") + zen `deepseek/deepseek-v4-pro` adversarial pass (9 findings; 6 applied, 1 rejected as false positive — see §9); every claim below carries a verified `file:line` anchor or is marked as requiring a probe.
**Supersedes:** the `min(pin, snapshot)` sizing doctrine of `broker_live_daemon_arm_design_2026_08_10.md` §3 (PR-B #1022) once Path A ships.

## 1. Problem

Sizing today: `suggested_size_pct` (brief) × `min(ALPHALENS_BROKER_SIZING_EQUITY, live snapshot)` (`control_loop.py:2320-2352`). On a 1 984 PLN account with a 16 000 PLN frame the clamp produces a ~$20 executable position → fee floor 1037 bps → terminal refuse. The operator wants: **declare "1% = X PLN" and have the program size proportionally, erroring loudly only when the position needs more money than the account has** — no silent shrinking.

## 2. Load-bearing discovery: the system is already fixed-fractional-risk in disguise

`thematic/trade_setup/sizing.py:56-80` derives `suggested_size_pct = risk_budget_pct × Σ qᵢEᵢ/(Eᵢ−S)` (capped at `_MAX_EXPOSURE_PCT = 25`), with `_DEFAULT_RISK_BUDGET_PCT = 1.0` (`builder.py:45`) and `alloc_pctᵢ ∝ qᵢEᵢ/(Eᵢ−S)`. Substituted through `compute_setup_plan` (`broker_contract/sizing.py:246-266`):

```
qty_i = floor( (risk_budget_pct/100 × equity × q_i) / (E_i − S) )
```

That is van-Tharp fixed-fractional risk per tier, laundered through two notional percentages. **"1% = X PLN" is therefore not a hack — it names the risk unit the pipeline already computes.** Path B makes the unit explicit; Path A ships the declared denominator now. The algebraic identity holds except where the 25% exposure cap binds (risk silently truncated in notional form — goldens on capped picks will NOT be byte-comparable; verified critic finding).

## 3. Pre-requisite: the gross-cap backstop is broken three ways (must fix BEFORE removing the clamp)

Under Path A the gross rail becomes the only notional-vs-real-equity link. Verified defects (`critic:money` B4, `risk-ops` F10/F11):

1. **Currency mismatch** — `gross_committed` sums `entry × qty` in **USD** (`control_loop.py:2314-2316`, `entry` journaled from `bracket.entry_limit` at `:2700`) against `gross_frac × total_value` in **PLN** (`safety.py:139-140`) → the cap is ~USDPLN (~3.7×) looser than intended. Today's "25% of equity" actually permits ~54% NVAX exposure. `broker_contract/sizing.py:307-322` (`setup_plan_gross_guard_limit`) already solves one-currency comparison but only the CLI consumes it.
2. **Candidate excluded** — `safety.check` runs pre-sizing (`control_loop.py:3089` before `:3123`) and compares only already-committed gross (`safety.py:141`); the first pick of ANY size always passes.
3. **Filled positions vanish** — only WORKING/PARTIALLY_FILLED verdicts are counted (`control_loop.py:2312`); a filled position's exposure drops out of gross entirely.

**Fix (PR-0):** candidate-inclusive, account-currency gross check at the post-sizing site: `(Σ committed WORKING gross converted via each record's journaled fx [:2710, :2764] + candidate_gross_pln + filled-position value marked-to-market) ≤ GROSS_FRAC × total_value`, where `candidate_gross_pln = setup_plan_gross_notional(plan) / fx.rate` — the RAW entry notional, explicitly WITHOUT the §4.2 cash buffer. The gross rail measures exposure (entry × qty); the buffer covers funding frictions (fees, FX markup, settlement-window drift) and belongs only to the cash floor. Using the buffered figure here would double-count funding as exposure and tighten the cap ~4% for no risk reason. No `safety.py` reshape needed; the existing pre-sizing check stays as the cheap early exit. Zen finding applied: the FILLED leg must be valued at CURRENT price × qty × CURRENT fx (or Saxo's own account-ccy exposure field from the `get_positions()` payload [:3079] if present) — journaled placement-time fx is correct only for the resting WORKING leg; using it for filled positions understates exposure whenever USDPLN has risen since fill.

## 4. Path A — declared frame + cash floor (ship now)

### 4.1 Mode seam

- New env `ALPHALENS_BROKER_SIZING_EQUITY_MODE` ∈ {`clamped`, `declared`}, name declared in `live_rails.py` next to `SIZING_EQUITY_ENV` (`live_rails.py:57` doctrine: names live in owning modules).
- `_resolve_sizing_equity` gains the mode branch **inside** the function (three call sites `:2399/:2709/:2763` stay byte-identical; journal `sizing_equity` stamps automatically record the declared frame — audit doctrine pinned by `test_control_loop.py:1151`).
- `declared` → return the pin, NO `min()`. Unset/`clamped` → today's behavior byte-identical (SIM unaffected by default).
- **Fail-closed everywhere** (critic B8): unknown mode → 0.0; `declared` with unset/blank pin → 0.0 (never raw snapshot — the one behavior the docstring forbids, `:2328-2332`).
- Boot-assert grows to SEVEN pins: `_check_sizing_mode` mirrors `_check_exit_policy` (`live_rails.py:117-131`); a LIVE unit missing the mode line fails boot loudly. `SIZING_EQUITY` keeps its name — the mode var makes semantics explicit per unit, and the 7-pin assert prevents silent reversion on a stale unit file. (Path B renames to `CAPITAL_FRAME`; doing the rename twice is churn.)

### 4.2 Cash floor — `_check_cash_floor`, sibling of `_check_fee_floor`

Placement in `_place_pick` (verified order `control_loop.py:3025-3169`): after the fee floor (`:3128-3147`), before `classify` (`:3149`). Inputs already in scope — `plan` + `fx` from `_resolve_and_size` (`:3123-3126`), `account` from `:3078`; zero extra I/O.

```
required_acct_ccy = setup_plan_gross_notional(plan) / fx.rate      # whole ladder, sizing.py:298-304
                    × (1 + _CASH_FLOOR_BUFFER_PCT/100)
refuse when required_acct_ccy > available
```

- **Whole-ladder-or-nothing by construction**: `setup_plan_gross_notional` sums ALL tiers, so a pass means every tier is funded.
- **Buffer = 4%** (own named constant; NOT `sizing_buffer_pct`, which shrinks the USD sizing notional — the wrong direction for a cash check, `risk-ops` F3): covers entry commissions (0.08% min $1/tier), one-way FX markup (≤0.25%), and USDPLN drift over the full GTD-7d + T+2 window (`execution.py:106-112`, `:167-172`). Zen finding applied: routine weekly moves are 1-2% but the 7-trading-day + T+2 TAIL exceeds 3% in stress regimes (2020/2022-class), so 3% was still thin; 4% is not a percentile-calibrated figure either — the CONSEQUENCE of an underfunded fill (reject vs forced action) is what P2 (§6) must establish, and the buffer is sized to make that event rare, not impossible. A mid-rest funding degradation re-check (cancel resting tiers when the buffer erodes) is Path-B reservation-ledger scope, not Path A.
- **`available` field**: `margin_available` (`AccountSnapshot`, `contract.py:148-155`; Saxo `MarginAvailableForTrading`, `broker.py:294-308`), fail-closed refuse on `None` — never `CashBalance`, which ignores resting-buy reservations and lags under EOD netting. **Neither `cash` nor `margin_available` has any consumer today** (grep-verified) — this is the first; semantics MUST be probe-verified first (§6 P1).
- **Scope**: entry buys + entry-side fees only. Disaster stop is a post-fill SELL StopIfTraded (`placement_planner.py:15-17`, `broker.py:518-534`), TP exits are market sells — no cash reserved (exit fees come from proceeds).
- **Cross-check (optional, cheap)**: sum per-tier precheck `EstimatedCashRequired` (`broker.py:500-516`) against local math; >2% divergence → refuse (mirrors `_FX_PRECHECK_RATE_DIVERGENCE_MAX_PCT`, `execution.py:165`). Precheck alone cannot replace the aggregate check — "Precheck reserves nothing" (`broker.py:476-479`) and three individually-passing tiers can be jointly unaffordable.

### 4.3 Refuse semantics: TERMINAL (`mark_refused`)

Follows the repo's own split (`risk-ops` F5): fee floor is terminal because retry-per-tick would self-place a stale brief signal once conditions change (queue-semantics fix, `control_loop.py:3101-3111`); the day-1 gap gate defers because it self-resolves within one session. Cash shortage is defer-shaped ("deposit arrives later") but **unbounded** — `iter_picks` has no armed-pick expiry (`picks.py:114-162`), so a pick deferred weeks would fire on a dead signal. Terminal + loud alert naming the shortfall ("need ~X PLN, available Y — deposit and re-arm"); `alphalens broker arm` after the deposit re-validates freshness by construction. Alert key `cash-floor:<ticker>`, mirroring `fee-floor:`/`day1-gap:`. Residual accepted: EOD-netting staleness can refuse a pick that would clear at EOD (critic B6) — mitigated by using `margin_available`, and re-arm is one command.

### 4.4 Ladder atomicity + crash window (critic B1/B2 — both HIGH)

- **B1 rollback**: today a mid-ladder `BrokerError` (e.g. Saxo insufficient-funds on tier 2/3) stops the loop, journals a note-only record that retires the pick, and **leaves the placed tiers live** (`control_loop.py:2740-2770`, `:300-313`). Highly probable at a near-boundary account. Fix: on an insufficient-funds-classified error, `cancel_order` this pick's just-placed unfilled tiers (cancel is deliberately ungated, `broker.py:76-80`), then `mark_refused` + alert. Converts "partial ladder" into the promised "nothing" — safe, entries are resting limits.
- **B2 write-ahead line**: a crash between POST and `append_submission_record` (`:2744` vs `:2688`) re-places the whole frame-sized ladder on restart (dedup key absent; under declared mode the duplicate is no longer balance-bounded). Fix: append a note record (`brackets=[]`, `note="placement attempt"`) BEFORE the first POST — registers the dedup key with zero schema change; a crash then strands an alertable non-retried attempt instead of a double order. Dedup mechanics verified: `_submitted_pick_keys` keys on top-level ticker+brief_date and already treats `brackets=[]` note-only records as submitted (`control_loop.py:300-313`) — no dedup-path change needed, only the append moves earlier.

### 4.5 Fee floor under multi-tier fills (critic B5)

`_round_trip_fee_bps` (`control_loop.py:2426-2450`) models ONE entry + ONE exit fill on the aggregate. Reality: 3 GTD tiers each pay `max($1, 0.08%×tier)` on entry, plus up to 3 TP-tranche sells + stop on exit. At frame 16k, NVAX computes ≈119 bps < 150 → passes, while a realistic multi-tier round trip is ~220-290 bps — the declared cap silently violated. Under the min-clamp the same pick refused at >1000 bps, so this leak is Path-A-opened. Fix in the same PR: per-tier entry commissions `Σ max($1, rate×qty×limit)` from `plan.entry_tiers` + symmetric per-tranche exit estimate. The per-tier model ships in PR-2 even with the cap at backstop level (§7 decision 3): journal the honest round-trip estimate (`est_round_trip_fee_bps`) on EVERY placement record, not only on refusals — this is the calibration series for path B's 150 bps target.

### 4.6 Daily-loss breaker denomination note (critic B7 — memo'd, small guard)

`compute_realized_r` is price-geometry, qty/currency-blind (`reconcile.py:145-160`). Under declared frame, 1R ≈ `risk_unit` PLN of the FRAME, not of the balance — at frame 16k vs balance 2k, `DAILY_LOSS_LIMIT_R=1.0` tolerates an ~8%-of-real-capital day. Mitigant already present: per-tier R summation triple-counts a full 3-tier stop-out, so the breaker trips early (conservative). Guard added in Path A: a boot-time + daily observability line "frame/balance = N.N; one 1R loss = X% of real capital" (critic B9). A PLN-denominated companion loss rail is Path B's heat-cap work, not Path A scope. Breaker's non-terminal reset-at-midnight behavior (`safety.py:149-153`, `:3087`) is pre-existing and unchanged.

### 4.7 SIM soak coverage (critic B8)

The SIM unit runs `declared` mode too (with a SIM-appropriate frame) so the cash floor and rollback paths soak on SIM before the LIVE flip — mode unset on SIM would leave the new code path LIVE-only, untested where it matters.

## 5. Path B — target model: "Declared Frame + Risk Unit + Refuse-Only Gates"

Full lens output preserved in the workflow transcript; the locked shape:

- **Unit re-denomination, not redesign** (§2 identity): `CapitalFrame` (declared, per-account), `RISK_UNIT_PCT` (default 1.0), `qty_i = floor(risk_unit_instr × q_i/(E_i−S))`. Operator states the whole risk posture in one sentence: *"frame 10 000 zł, 1R = 100 zł, max 2 open, heat 2R, day stops at −1R."*
- **Gate stack (refuse-only, never shrink):** per-pick exposure cap (descendant of the T5 25% truncation — moving it execution-side edits brief-generation content and needs its own decision) → portfolio heat cap `Σ open risk ≤ HEAT_CAP_R` → gross cap vs REAL equity (as repaired in PR-0) → fee floor (schedule-driven) → cash sufficiency with a reservation ledger folded from the journal.
- **Leaf placement** (`broker_contract`, dep-free): `CapitalFrame`, `RiskBudget`, `compute_risk_setup_plan`, `required_cash`, and `FeeSchedule` + `round_trip_fee_bps` **promoted from the private `_FEE_FLOOR_*` constants** (`control_loop.py:2421-2423`) — per-broker/venue fee schedules are a product requirement. No env reads in the leaf; control loop keeps policy, ordering, refusal semantics.
- **Per-account config record** `AccountRiskConfig` (frozen dataclass): frame, risk unit, MAX_OPEN, heat cap, gross frac, daily-loss, fee cap, exit policy, fee schedule, currency — env-constructed today, client-config-store later; SIM/LIVE/client-N share one code path (ADR 0016 "same binary, instance identity via config" extended to money).
- **Retire on the daemon path**: `compute_daily_scale_factor` / `STEADY_STATE_GROSS_FRAC` — the daemon hardcodes `scale_factor=1.0` (`control_loop.py:2400`); the Little's-Law scaler is paper-harness heritage.
- **Rejected**: ATR/percent-vol layer (stop already ATR-anchored — double vol-normalization), Kelly (no validated edge; `capital_deploy_clause`), portfolio vol-targeting (degenerate at MAX_OPEN ≤ 2; Layer-4 research object), risk-%-of-live-balance compounding (couples sizing to EOD-netting balance noise).
- **MAX_OPEN re-denomination prerequisite**: `open_bracket_count` counts TIERS, not picks (`control_loop.py:2296-2317`) — one 3-tier ladder occupies 3 slots. Any MAX_OPEN raise and the heat cap both need per-pick denomination first. Cross-pick cash races (same-drain double-spend) are structurally masked by MAX_OPEN=1 today and un-mask with it.

### Migration A → B (zero-rework contract)

| Path A element | Becomes in B | Rework |
|---|---|---|
| Declared frame, no min-clamp | `CapitalFrame` verbatim; "1% = X" becomes `risk_unit` | none |
| Cash floor (refuse whole pick) | Gate 5 + reservation ledger | additive |
| Terminal refuse semantics | unchanged contract | none |
| Notional `compute_setup_plan` | `compute_risk_setup_plan` (identity §2) | mechanical code-wise; goldens differ where the 25% cap binds, and capped picks CHANGE size — an operator-visible semantics shift, not zero-rework (zen finding applied) |
| 7-pin boot-assert | 7-pin with renamed frame + `RISK_UNIT_PCT` | rename PR + coordinated unit-file edits on BOTH instances in the same deploy window (a stale unit fails boot loudly — safe, but it is operator rework, not zero) |

**What A must NOT do** (keeps the table true): no `min(frame, balance)` fallback anywhere; no shrink-to-fit on any gate; no env reads or snapshots in `broker_contract`; no second FX conversion point; no `TradeSpec` wire-schema change (risk weights are recoverable leaf-side: `q_i ∝ alloc_pct_i(E_i−S)/E_i`).

## 6. Probes required before the LIVE flip (SIM-verifiable, mandatory)

- **P1 — reservation semantics AND update timing**: place a small resting buy on SIM, re-read `/port/v1/balances`; confirm which field decrements (`MarginAvailableForTrading` vs `CashBalance`) and by how much. Zen finding applied — also probe WHEN the field updates: after a sell fill (intraday proceeds under EOD netting), and note that deposit-visibility latency on LIVE cannot be SIM-probed — after the first real deposit, check how quickly it appears in `margin_available` before assuming same-day re-arm works. Adjudicates the §4.2 field choice. (Precedent: the trailing-mechanics probe.)
- **P2 — unaffordable resting fill**: make a resting buy unaffordable (consume cash), force the fill; observe Saxo behavior (reject-at-fill / partial / fill-then-forced-action). Not determinable from code; sizes the §4.2 buffer.

## 7. Operator decisions

The frame, the repaired gross cap, and the fee cap interlock (zen finding applied): the repaired cap limits candidate gross to `GROSS_FRAC × real equity` = 992 PLN at today's balance, so a pick passes only when `suggested_size_pct ≤ 992/frame`. A larger frame buys bigger positions for LOW-size_pct picks but gross-refuses everything above the ratio; a smaller frame passes more picks at smaller notionals, where the honest per-tier fee model (§4.5) charges more bps. Historical brief distribution (554 OK setups, 2026-06-13→08-11): size_pct median 4.62%, p75 7.75%, p90 11.74%, max 25%. Soak configs at the current balance (pass rate = fraction of historical candidates clearing the repaired gross cap on RAW gross):

| | Frame (X) | Passes size_pct ≤ | Historical pass rate | NVAX (6.7%) |
|---|---|---|---|---|
| **Config 3 (operator, LOCKED)** | 15 000 (X=150) | ~6.6% | ~70% | raw gross ~1 005 zł planned (957 zł after qty-flooring) vs 992 zł budget — sub-4% margin, pass/refuse decided by rounding luck; treat as **refused** under the headroom rule below |
| Config 1 | 14 000 (X=140) | ~7.1% | ~73% | ~957 zł, 3.6% headroom — also below the rule |
| Config 2 | 8 000 (X=80) | ~12.4% | ~94% | ~540 zł |

Standing rule adopted with the frame choice: demand **≥5% headroom** between candidate gross and the gross budget — the budget is 0.5 × mark-to-market `total_value`, which drifts daily (EOD netting, FX, open-position marks); a sub-2% margin makes pass/refuse a coin flip decided by the fx sizing buffer and per-tier qty flooring, and a refuse at drain time is terminal (§4.3).

| # | Decision | Operator verdict (2026-08-12) |
|---|---|---|
| 1 | Frame value X ("1% = X PLN") | **X = 150 zł, fixed regardless of candidate** (operator). Consequence at today's balance: NVAX-class (~6.7%) picks are knife-edge/refused; ~70% of historical candidates pass — the natural path is arming the next fresh pick (median size_pct 4.6% clears with wide margin) or a small deposit (`total_value` ≥ ~2 110 PLN restores 5% headroom for NVAX-class) |
| 2 | `GROSS_FRAC` pin | 0.25 → 0.5 (boot-assert max; the repaired cap makes 0.5 mean a REAL 50%). Mode-independent: `safety.check` runs before sizing for every armed pick (`control_loop.py:3089`) with no knowledge of the sizing mode — binds identically in `clamped` and `declared`, and survives into Path B as gate 3 of the §5 stack, always denominated against REAL equity, never the frame |
| 3 | `MAX_FEE_BPS` for the soak | 150 → **1000** — re-labeled from cost cap to DEGENERATE-CLASS BACKSTOP: mode-A soak accepts realistic fees (~290-470 bps honest per-tier estimate, §4.5) as validation cost, but the fee floor stays the ONLY rail catching the tiny-notional class (the original 1037 bps $20 fiasco; the cash floor and gross cap pass tiny notionals trivially, the zero-qty check misses low-priced stocks). Never unset on LIVE (boot-assert requires explicit finite > 0, `live_rails.py:101-114`); no 'off' token is added to the boot-assert (a permanent off-switch to serve a temporary soak preference contradicts the pins doctrine). Restore ≤150 (path-B target, operator-confirmed) after funding to a full frame |
| 4 | Refuse semantics | terminal + re-arm (§4.3) — operator confirmed |
| 5 | Cash-floor buffer | 4% (§4.2) |
| 6 | Path B start | after ≥1 clean LIVE round-trip on Path A; path-B fee cap target = 150 bps (operator) |

## 8. Ship order

1. **PR-0** — gross-cap repair (§3): candidate-inclusive, account-ccy, filled positions included. Prerequisite; independently valuable (today's cap is fiction even in clamped mode).
2. **P1/P2 probes** on SIM (§6) — parallel with PR-0 review.
3. **PR-1** — mode env + `_resolve_sizing_equity` branch + 7-pin boot-assert + fail-closed paths (§4.1).
4. **PR-2** — cash floor + ladder rollback + write-ahead line + per-tier fee model + observability line (§4.2-4.6).
5. Unit pins: LIVE `SIZING_EQUITY=15000`, `SIZING_EQUITY_MODE=declared`, `GROSS_FRAC=0.5`, `MAX_FEE_BPS=1000`; SIM gets `declared` with a SIM frame (§4.7). Arm the next fresh pick with size_pct clearing the ≥5% headroom rule (NVAX at 6.7% is knife-edge at X=150 — §7).

Each PR: TDD (unittest.TestCase — pytest silently skipped), zen `deepseek/deepseek-v4-pro` pre-merge, DCO sign-off, S3776 ≤15 (extract `_refuse_terminal` helper shared with the fee floor), diff-coverage 80% (test list per branch in the code-fit lens output).

## 9. Zen adversarial pass — adjudication record

9 findings from `deepseek/deepseek-v4-pro` (thinking=high) on the draft. Applied: FX-tail buffer thinness (→ §4.2, 4% + P2-arbitrated consequence), stale-FX filled leg in the gross repair (→ §3, mark-to-market), P1 update-timing scope (→ §6), frame↔gross-cap↔fee-cap interlock with the ≤7.1%-size_pct consequence (→ §7), migration zero-rework overstatement (→ §5 table), write-ahead dedup mechanics detail (→ §4.4). **Rejected as false positive:** "MAX_OPEN=1 counting tiers makes any 3-tier ladder unplaceable" — `safety.check` runs ONCE per pick BEFORE placement (`safety.py:131-137` compares the pre-existing book; empty book → 0 < 1 passes) and all tiers then place in a single `_place_tiers` call; empirically the NVAX ladder passed safety on 2026-08-11 and reached the fee floor. Per-tier counting only blocks a SECOND pick while a ladder rests — intended under MAX_OPEN=1, and already §5's re-denomination prerequisite. Declined (scope): hybrid defer-then-terminal cash refuse (session-bounded defer adds a state machine for a marginal window; terminal + re-arm stands per §4.3), percentile-calibrated buffer (P2 first — consequence severity decides how much calibration the buffer deserves).
