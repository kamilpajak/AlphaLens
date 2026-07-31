# Broker-manager extraction + exit-geometry wiring

**Status: LOCKED (zen-reviewed + operator-decided 2026-07-31)** · 2026-07-31

> **Adversarial review applied (zen deepseek-v4-pro, 2026-07-31):** one P0 surfaced — the planned-vs-realized blend anchor places a wrong-distance stop and is fixed by a MANDATORY `avg_price` re-anchor in PR-6, not deferred (§4.3, Risk 2a). Three calibrated honesty notes added: persistence is a genuine reimpl across the network boundary, not a "file-for-socket swap" (§5.2); service-internal deps (auth token store, `TimeSource` clock, A2 market-data) named so "standalone" ≠ "dependency-free" (§5.2); the acceptance-DSL-green-per-PR is the coherence gate that removes any half-ported-interval risk (§6). Two claims held firm: the unsized-spec refinement (preserves current per-drain re-sizing) and the two-CLIENT-ports discipline.

> **Revision R2 — operator decisions 2026-07-31 (broker-manager = pure executor; SUPERSEDES the sections noted):**
> 1. **Earnings gate leaves the manager entirely (supersedes §2.4, §3-V3, EarningsPort).** Deciding whether to send an order — earnings-window avoidance included — is CLIENT responsibility; the manager never knows about earnings. The `brokers→thematic` coupling is removed by **DELETION, not a port** — `earnings_gate` moves client-side. **EarningsPort dropped.**
> 2. **Package `broker_contract`; A2 infra as a separate pinned module (confirms §2.1).**
> 3. **Bring-your-own CONCRETE levels now; the manager resolves NO policies (supersedes §2.3 ExitGeometrySpec, §4.1).** The client computes exit levels (using the SHARED `exit_geometry` leaf — single-source-of-truth preserved) and sends explicit `{stop, tp}`. `kind="policy"` (manager-side resolution) dropped; the wire carries concrete levels called CLIENT-side.
>
> **The mechanical/geometric line this draws:**
> - **Manager (autonomous) = MECHANICAL safety only:** never-naked, keep the protective order sized to the netted position (grow-amend qty), no oversell, OCO commit-once, clean terminals. Protects at whatever LEVELS the client last set.
> - **Client (owns the loop) = ALL geometry:** initial levels, the realized-blend RE-ANCHOR (the former P0 — now client work: watch fills via events → recompute → amend), and future trail0.6 / ML.
> - **New manager verb `amend_exit(position, new_stop, new_tp)`** — the client's channel to move levels; the manager executes it with no naked window.
>
> **Simplifications:** **zero client-facing ports** (EarningsPort deleted; NotificationPort folds into `stream_events` — the manager EMITS events, the client routes them). Client contract = the API alone: `submit_intent` + `amend_exit` + `cancel` + `query_state` + `stream_events`. The P0 anchor risk is unchanged in substance but **moves to the client**; the manager only executes amends safely. **AlphaLens-the-client grows an exit-management loop** (watch fills → re-anchor/trail → amend) — new client scope the autonomous daemon used to absorb. Questions 4–6 deferred by the operator.

> **Revision R3 — declarative reaction-plan (operator-confirmed 2026-07-31; refines R2's "client loop"):** the intent carries INITIAL levels **plus an optional declarative REACTION PLAN** — a bounded, deterministic primitive vocabulary the EXECUTOR evaluates each tick, NOT a client watch-loop. The executor stays client-logic-agnostic: it knows the PRIMITIVES, never the strategy (the client compiles "bezpazery"/"trail0.6" into primitive params).
>
> **Primitive vocabulary (ship two, both cover a real policy with NO client loop and NO `amend_exit`):**
> - `reanchor_on_fill(k_atr, …)` — on fill-complete, set stop/TP to `avg_price ± k·ATR`. **Covers bezpazery** (a static bracket re-anchored to the realized entry). Uses `Position.avg_price` (already live in `ProtectionView`) → **this primitive IS the §4.3 P0 fix**, evaluated executor-side where `avg_price` lives. Cheap: no new executor capability.
> - `trailing_stop(arm_trigger_r, trail_frac)` — arm break-even at `+arm_trigger_r` MFE, then trail at `trail_frac` of peak. **Covers `be_0p5r_trail0p6`.** Requires the executor to gain **per-position peak-tracking** (a `ProtectionView` peak field fed by the already-wired streaming price) — bounded/deterministic, not arbitrary client code. Ships WITH trail0.6, later than bezpazery.
>
> **Mechanical safety is orthogonal:** a reaction primitive produces a LEVEL; the level is applied through the existing never-naked `AmendStop` path (position_manager.py:240). A bad rule → a bad level, NEVER a naked window or oversell.
>
> **`amend_exit` (client push) is reserved for `kind="model"`/ML** — where the next level comes from a model call, not a closed-form primitive — and any future bespoke client logic outside the vocabulary. It is the escape hatch, NOT the path for bezpazery or trail0.6.
>
> **Supersedes R2's "AlphaLens grows an exit-management loop":** bezpazery + trail0.6 need NO client loop (executor-evaluated primitives, robust to client downtime); only ML uses the push channel. `ExitGeometrySpec` (R2 concrete-levels) becomes `initial_levels + optional reaction_plan[]`; the `kind` discriminant maps to primitive types (`reanchor_on_fill` / `trailing_stop` / `model`→push). The manager remains a pure executor: it runs the client's compiled reaction rules, it does not author or name them.

One design, two deliverables: (a) wire exit-geometry into the SIM Saxo auto-manager **through the future service boundary** so extraction is a transport swap, not a rewrite; (b) map the exact coupling cut-line for extracting the broker-manager into a standalone, multi-client service. Produced via a design workflow (coupling map + dependency DAG + proto-API readers → minimal-cut vs clean-ports proposals → synthesis); every load-bearing seam verified against source.

---

## 1. Context, the two-boundary model, the core insight

### 1.1 What exists today
AlphaLens **is** the broker-manager, in-process. The live path:
`brief parquet → load_brief (control_loop.py:1609) → _place_pick (1593) → _resolve_and_size (1468) → compute_setup_plan(brief_trade_setup, paper_equity, fx) (paper/sizing.py:241) → decompose → placement → daemon`.
The daemon (`alphalens broker manage`) ticks every ~45s: drains armed picks, places brackets + standalone disaster stops, reconciles against live broker truth, manages ladder exits / OCO upgrade to terminal. SIM-only, gated on `ALPHALENS_BROKER_ALLOW_ORDERS=1` (ADR 0014).

### 1.2 The two boundaries
- **Boundary 1 (manager ↔ broker vendor) — ALREADY CLEAN.** `brokers/contract.py`: the `Broker` Protocol + capability protocols (`SupportsStandaloneStop/OcoExit/AmendStop/OrderResolution/FillCrossCheck`) + frozen types (`InstrumentRef`, `Position`, `OrderState`, `BracketOrderRequest`, `PlacedOrder`). Saxo reached only via `registry`, rule-enforced. Nothing to do here.
- **Boundary 2 (client ↔ manager) — DOES NOT EXIST as a formal contract.** This is the boundary to formalize and extract along.

### 1.3 The core insight (verified, and refined)
`TradeIntent ≈ SetupPlan + ExitGeometrySpec`, and the **only** brief-coupling on the live path is `compute_setup_plan` reading the brief dict inside `_resolve_and_size`. Extraction = move `brief → spec` to the **client** side; the service consumes an agnostic spec, never a brief.

**Refinement (adopted): the boundary carries the UNSIZED spec, not the sized plan.** `compute_setup_plan` needs `paper_equity` (`broker.get_account().total_value`) and `fx` (`broker.get_fx_rate`) — both **service-side money-truth**. If the intent carried a sized `SetupPlan`, the client would need a `query_account` round-trip and would freeze share quantities at arm-time, going stale across the multi-day entry-retry loop. So `SetupPlan` is a service-internal artifact; the wire carries a pre-sized `TradeSpec` and the **service sizes at each placement attempt** against fresh equity.

`compute_setup_plan` splits at its natural internal seam:
- `parse_brief_to_spec(brief_dict) -> TradeSpec` (brief parsing + `validate_trade_setup`) → **client**.
- `compute_setup_plan(TradeSpec, equity, fx) -> SetupPlan` (money-math half, unchanged) → **shared package, called service-side**.

---

## 2. Target architecture

### 2.1 Three-way split
- **A — shared package (`broker_contract`, pinned by version in both `pyproject.toml`s; ADR 0006 `phase-robust-backtesting` extraction precedent).** Pure leaf, stdlib + value types only:
  - `contract.py` — Boundary-1 Protocol + frozen types (moved verbatim).
  - `intent.py` — `TradeIntent` / `TradeSpec` / `ExitGeometrySpec` (§2.3).
  - `sizing.py` — `SetupPlan/TierPlan/TpTranchePlan` + `TradeSpec` + pure `compute_setup_plan(spec, equity, fx)`.
  - `fx.py`, `constants.py`, `calendar.py` — moved verbatim (already MIC-parametrized).
  - `exit_geometry/{levels,registry}.py` — pure leaf (§4).
  - `ports.py` — **exactly two** injected Protocols: `EarningsPort` + `NotificationPort` (§2.4). No `ClockPort`/`MarketCalendarPort` — deliberate (§7 Risk 3).
- **B — service repo (the extracted broker-manager).** `brokers/{execution,reconcile,submission_log,registry,routing}.py`, `brokers/automanager/*`, `brokers/saxo/*`, the `~/.alphalens/broker_orders/*.jsonl` journals + `KILL` gate. Consumes `TradeIntent` + the two ports; never sees a brief.
- **C — client (stays in AlphaLens).** `paper/brief_loader.py`, the new `parse_brief_to_spec` + `validate_trade_setup`, `thematic/*`, `feedback/*`, Django, `/edge`, the thin `alphalens broker arm` CLI shim that builds a `TradeIntent`.
- **A2 — thin shared infra shelf** (pinned by the service repo, no client coupling): `observability/textfile` (Prometheus emit) + `data.alt_data.polygon_client` (quotes/streaming/fill-cross-check — market data the manager legitimately needs to manage exits, key-scoped per client).

### 2.2 In-tree first, extract later
Pre-extraction, ship A as in-tree leaf packages plus the relocated sizing types. The extraction epic pulls them + `contract.py` + `fx/constants/calendar` into the standalone `broker_contract` package. **The whole cut-line is proven green in one repo before any physical split** — the epic's diff is transport + tenancy, not logic.

### 2.3 Schema (frozen dataclasses)

```
TradeIntent                       # the Boundary-2 wire type
  intent_id: str                  # client-authored idempotency key; supersedes synthesized (ticker, brief_date)
  account_id: str                 # tenant dimension — reserved, single value today (§7 Risk 3)
  instrument: InstrumentHint      # {ticker, mic}; service resolves to InstrumentRef
  spec: TradeSpec                 # UNSIZED thesis (client-authored)
  exit: ExitGeometrySpec          # policy name+version (client-authored)
  meta: {armed_ts, brief_date, schema_version}

TradeSpec                         # formalizes compute_setup_plan's current dict input, UNSIZED
  entry_tiers: tuple[EntryTierSpec, ...]   # (limit_price, alloc_pct) — no share qty
  disaster_stop: float
  tp_tranches: tuple[TpTrancheSpec, ...]   # (price, alloc_pct)
  suggested_size_pct: float
  order_ttl_days: int             # default DEFAULT_ORDER_TTL_DAYS
  side: Literal["long"]           # guard the side — the "Sell"→Buy footgun is client-side now
  schema_version: str

ExitGeometrySpec                  # discriminated union
  kind: Literal["policy", "levels"]
  # kind="policy" (SHIP NOW):
  name: str                       # "atr_bracket_1p5" | "bezpazery"
  version: str                    # policy version key (ADR 0013 R3 / 0014 poolability)
  anchor_facts: Mapping[str,float]# client-carried market snapshot: {atr_14, high_52w, ...}
  # kind="levels" (RESERVED, bring-your-own): explicit_stop, explicit_tps
```

Policy *parameters* (the 1.5×ATR multiple, TP multiples, ceiling rule) live in `exit_geometry/registry.py` keyed by `(name, version)` — client and service resolve identical params from the same leaf. The spec carries only `name + version + anchor_facts`; the formula is shared code. Today's armed pick (`picks.py Pick(ticker, date)`) is a **degenerate `TradeIntent`**.

### 2.4 Where facts come from — the merge line between the two proposals

The one decision the proposals genuinely split on is **the earnings gate: carried intent-fact vs injected port.** The merge adopts a single rule:

> **Port a fact iff it (i) gates a SAFETY rail AND (ii) can change materially between arm-time and fill. Otherwise carry it in the intent.**

| Fact | Gates a safety rail? | Changes arm→fill? | Verdict |
|---|---|---|---|
| account equity + fx | no (sizing) | yes | **service-side** (`BrokerPort`), never carried (§1.3) |
| **next earnings date** | **yes** (earnings-window refusal) | **yes** (can be *newly scheduled* after arm) | **`EarningsPort`** |
| geometry anchor facts (ATR, 52w-high) | no (sets initial bracket price; rails run regardless) | yes, but divergence is *measured* by the dark shadow, and path-dependent policies recompute live | **carried in `anchor_facts`** |
| alert routing | no | n/a | `NotificationPort` |
| time / market calendar | no (deterministic) | no | **shared-A pure function, NOT a port** (YAGNI) |

**Earnings gate = port, not carried fact.** The gate is a per-tick safety invariant, class-equal to never-naked; the manager is stateless-per-tick across a multi-day retry loop. A frozen `next_earnings_date` cannot see an earnings date *scheduled after arm-time* — the gate then fails open on a stale `None` and silently lets a placement land into an earnings window. So formalize the **existing** `lookup=` seam (`earnings_gate.py:82`) into a constructor-injected `EarningsPort` whose `(ticker, today)`-keyed cache self-heals daily *because the source is a live port, not a frozen field*. Gate **logic** (`earnings_window_refusal`, window math, fail-open, opt-out) stays service-side, unchanged.

**Everywhere the rule does not fire, take the lean-wire pragmatism.** Geometry `anchor_facts` are carried (not a rail; the dark shadow measures divergence pre-flip). Time/calendar stay pure shared functions — not ports. Net: **exactly two ports** (`EarningsPort`, `NotificationPort`), each load-bearing because each cuts a real coupling (`thematic`, `telegram`).

### 2.5 The dark flag
`ALPHALENS_BROKER_EXIT_POLICY`, default `"setup_static"` (mirrors `_oco_enabled`). Until flipped, the `TradeSpec`'s static `disaster_stop`/`tp_tranches` are used and geometry is inert. Ship stamp-only shadow → measure anchor divergence → flip to `atr_bracket_1p5`/`bezpazery`.

---

## 3. Coupling cut-table — every automanager → AlphaLens coupling and its resolution

Tiers: **A** shared · **A2** shared infra · **B** service · **C** client. All six leaks are lazy imports → tripwires use `top_level_only=False`.

| # | Coupling (verified site) | Tier after cut | Resolution |
|---|---|---|---|
| V1 | `_place_pick → paper.brief_loader.load_brief` (control_loop.py:1609) | brief → **C** | `arm_pick` builds the full `TradeIntent` client-side at arm-time; daemon drains intents, never briefs. |
| V2 | `_resolve_and_size → compute_setup_plan(brief dict)` (1468→sizing.py:241) | split: parse **C**, sizing **A** | `parse_brief_to_spec` (client) emits `TradeSpec`; `compute_setup_plan(spec, equity, fx)` (shared A, pure) called service-side. |
| V3 | `earnings_gate._fetch_next_earnings → thematic.sources.earnings_calendar` (earnings_gate.py:57) — **sharpest `brokers→thematic` violation** | gate logic **B**, feed via port | Formalize `lookup=` seam into `EarningsPort`. Feed-less client gets `NullEarningsPort` → inert gate (loud, §7 Risk 1). AlphaLens wires the thematic-backed adapter at the composition root. Service imports zero `thematic`. |
| V4 | `_default_alert → telegram` (1394) + `saxo/tokens._send_chain_loss_telegram → TelegramClient` (tokens.py:109) | **B** consumes port | Inject one `NotificationPort`; chain-loss alert routes through the same sink (already injectable via `self._alert`, tokens.py:359). Service default = journald/no-op. |
| V5 | `control_loop → observability.textfile` (Prometheus) | **A2** | Generic emit → shared infra shelf. No injection. |
| V6 | `saxo/{client,tokens} → data.alt_data.{polygon,telegram}` | polygon **A2**, telegram port | Polygon (market data / fill-cross-check) → A2, key-scoped. Telegram → `NotificationPort`. |
| — | `paper.sizing` types, `fx`, `constants`, `calendar` | **A** | Generic value/calendar math; travel with the shared package. |
| — | `brokers/saxo/*` | **B (internal)** | Vendor adapter, reached only via `registry`; already rule-enforced. |

**Pre-extraction tripwires** (add to `test_module_dependencies.py`, each with a positive control, `top_level_only=False`): `brokers ↛ thematic`; `brokers ↛ feedback`; `brokers ↛ paper.brief_loader`; `brokers ↛ data.alt_data.telegram`; `brokers ↛` the brief-parse module. Green including function-scope = the A/B/C cut holds while still one repo.

---

## 4. Exit-geometry wiring through the intent

Wiring the geometry now and extracting later are the **same seam**, because `ExitGeometrySpec` is a field on `TradeIntent` in shared package A. Both sides compute from the same `exit_geometry` leaf, so the repo split changes only *how `TradeIntent` arrives*, not one line of geometry math.

### 4.1 The single chokepoint (static policies: bezpazery / atr_bracket_1p5 first)
- **Client (C):** at `arm`, `parse_brief_to_spec` emits the `TradeSpec` (setup-static levels) **and** an `ExitGeometrySpec(kind="policy", name="atr_bracket_1p5", version=…, anchor_facts={atr_14, high_52w})`. The client owns the brief's market snapshot; the service never fetches market data to compute geometry.
- **Service (B) — the lone `PlannedExit` producer:** at `_build_planned_line` (control_loop.py:897, folded by `_fold_planned_exits` at :1012), when `exit.kind == "policy"` AND the dark flag is not `setup_static`, override `stop_price`/`take_profit` with `exit_geometry.levels.atr_bracket_levels(entry, anchor_facts["atr_14"], registry[name,version])` + `ceiling_from_52w_high(...)` instead of `placement.disaster_stop_price` / `tier.tp`.
- **All safety machinery consumes the folded levels UNCHANGED:** `reconcile_protection`, B0 OCO, grow-amend, never-naked, OCO-too-far-TTL, additive-B1. The override happens *before* the fold, so no rail knows whether the stop came from the brief or a policy. This is the "reuse the whole safety core untouched" property.

### 4.2 `kind` discriminants — future-proofing for trail0.6 + ML
- **`kind="policy"` static** (bezpazery, atr_bracket_1p5): ship now, via the placement chokepoint above.
- **Path-dependent policies (`trail0.6`, `kind="model"` ML): Seam B, reserved.** These do NOT touch the placement chokepoint. They cross the boundary as a policy *name* and drive a **gated arm in `position_manager._reconcile_long`** (position_manager.py:526) emitting the **existing `AmendStop` rail** (position_manager.py:240). No new transport verb, no new safety machinery. They recompute live each tick — which is *why* anchor staleness (§7 Risk 2) is a non-issue for them.
- **`kind="levels"` (bring-your-own explicit stop/TP): reserved, not implemented** — same "reserve the escape hatch, ship the minimal path" discipline as the `EarningsPort` null adapter and the reserved `account_id`.

### 4.3 Anchor-divergence — P0 geometry BLOCKER (not a stampable divergence)

**Zen adversarial review 2026-07-31 elevated this to P0.** The subtlety: today's `disaster_stop` is a STATIC brief price (sizing.py:284), so anchoring never mattered. bezpazery's bracket is `blended − 1.5·ATR` — anchored to the ENTRY blend. At placement the override can only see the PLANNED blend (alloc-weighted tier limits). But `grow-amend` amends stop QTY and keeps the stop PRICE literal (position_manager passes `plan.stop_price`, never re-anchors). So when the REALIZED blend drifts from planned (partial fills, gap-down fills), the live risk distance `realized_blend − stop` is no longer `1.5·ATR` — the stop is silently **too tight** (shallow fill) or **too loose** (deep gap-fill), with no rail catching it (never-naked/reconcile run on whatever price they hold). This is a per-position risk-geometry fault, and it breaks the "live == replay via shared formula" premise (replay anchors at realized touch-blend).

**Required, NOT deferrable: the realized-`avg_price` recompute ships WITH the geometry-live PR (PR-6), before any flip.** `Position.avg_price` (the netted realized blend) is already live in `ProtectionView.long_positions`. On the tick the fill-set completes, recompute the stop/TP off `avg_price` using the same leaf so the invariant `avg_price − stop = k·ATR` holds; refresh ATR at recompute to avoid stale-vol. This re-anchor rides the existing `AmendStop` price-moving rail (position_manager.py:240) — no new order path. (Alternative considered: **stop-as-delta** — store the stop as a distance from realized blend so grow-amend auto-re-lifts. Bigger change, moves the stop for the owner's benefit; needs an explicit risk-rule sign-off. Recommend the recompute for the first cut.)

**Gating artifact:** a fail-case test — a 2-tier ladder with a gapped deep fill — must demonstrate the planned-vs-realized ATR-multiple drift, and PR-6 must show it corrected by the recompute. The `planned` journal record still stamps **both** anchors + `(name, version)` (T8 poolability, ADR 0013 R3 / 0014), and the dark shadow still measures divergence — but the shadow is a *monitor*, not the fix; the recompute is the fix.

---

## 5. Transport-agnostic service API + acceptance-DSL as the cross-transport contract

### 5.1 The `ManagerService` Protocol
The three file/side-effect faces that already exist become three methods; the file journals become one *implementation* of persistence.

```
submit_intent(TradeIntent)      -> IntentAck{intent_id, status: armed|refused, reason?}
query_state(intent_ids?)        -> list[PositionState]
stream_events()                 -> Iterator[ManagerEvent]
```

- **`submit_intent`** = append to `picks.jsonl`, formalized. The existing queue contract becomes the submit contract verbatim: latest-status-line-per-key wins, ARMED-only drain, terminal `mark_refused` retires the key, cross-tick dedup on `(ticker, brief_date)` → now `intent_id`. Places nothing itself; the daemon drains.
- **`query_state`** = the reconcile projection surfaced as a read. Per managed position: `{symbol, owned_qty, protection:{covered_qty, rung, stop_price, tp_price}, plan_ref, terminal?}` from `build_protection_view` + `reconcile_brackets`. **State is a pure function computed on read from live broker truth — no stored status field.** That statelessness is *why* a network boundary is safe to drop in front: a dropped connection loses nothing; the next tick re-derives.
- **`stream_events`** = a typed union over today's three sinks: `alert` | `tick_report` (`TickReport`) | `order_outcome` (`ReconcileVerdict`) | `liveness{heartbeat_ts, kill_active, stream_age}`. Telegram / journald / Prometheus become downstream **`NotificationPort` subscribers**, not the primary interface.

### 5.2 Why the LOGIC is transport-agnostic — and the honest caveat on persistence
`LoopDeps` (frozen, control_loop.py:131) is the injection seam — every collaborator arrives as an injected callable; `build_default_deps` (:656) is the single wiring site. **The management LOGIC is a new `LoopDeps` wiring, nothing more** — `run_once` is pure over its injected collaborators.

**But "transport swap not a rewrite" is only true of the logic, NOT the persistence layer (zen review 2026-07-31).** The file journals (`picks.jsonl`, `broker_orders/*.jsonl`) are a shared-filesystem contract that works in-process because of local-FS properties the design silently leans on: `O_APPEND` line-atomicity, latest-line-per-key wins, one daemon writer + the `arm` CLI appender. **None of those survive a network boundary** (NFS `O_APPEND` is not atomic; two processes across a socket have no shared inode). So the extraction epic **reimplements the persistence/transport behind the `ManagerService` Protocol** — a real ordered/atomic channel (a queue or DB for submit + a durable event log for `stream_events`), with sequence/version vectors replacing latest-line-wins. That is a genuine build, not a file-for-socket swap. The claim that survives: the *Protocol and the logic behind it* are stable across transports (the acceptance DSL proves it); the file journal is merely the *in-process implementation* of that Protocol. State the scope honestly so the epic is budgeted for a persistence reimpl, not a rename.

**Service-internal dependencies (travel WITH the service; they are NOT client-facing ports, so they do not count against "two ports" — but "standalone" ≠ "dependency-free"):** the Saxo OAuth token store (auth, inside the vendor adapter, Boundary 1), a `TimeSource` clock (GTD/TTL/earnings-window math — inject a `TimeSource` interface defaulting to system UTC, testable with a fake, never a bare `datetime.now()`), and the A2 market-data feed (Polygon fill-cross-check / quotes). These are the service's own infra, key-scoped per deployment.

### 5.3 Acceptance DSL = the cross-transport SLA suite
`tests/brokers/automanager/acceptance/` drives the **real** manager against `fake_broker.py` via a single `world.run_tick()` WHEN seam, asserting on **observable outcomes** (broker state + emitted events), never on file paths. Re-point `run_tick()` to "drive one management cycle **through the transport under test**" and the identical GIVEN/THEN becomes the cross-transport contract. `world` wires `FakeEarnings` + `CapturingNotifier` — making guarantee #5 (never-silent) a **direct assertion on `NotificationPort.publish`** instead of scraping journald. The six guarantee files ARE the service SLA:

1. `test_safety_rails` — submit refuses under orders-off / `MAX_OPEN` / gross-cap / daily-loss / KILL (KILL still protects held).
2. `test_every_position_protected` — never-naked from broker truth (even positions the service never placed) → the `query_state.protection` invariant.
3. `test_no_oversell` — resting SELL ≤ owned; OCO counts once.
4. `test_resilience` — per-symbol `BrokerError` isolation; OCO-refused degrades to plain stop, never to nothing.
5. `test_never_silent` — every degrade emits on `stream_events`; healthy tick quiet. Now includes: a null-port inert earnings gate surfaces its inert state.
6. `test_terminal` — filled entry ends protected; cancelled entry's children cleaned (no orphans).

The fake-not-Saxo property already proves no vendor coupling.

---

## 6. Phased plan (PR-by-PR, each smallest-safe + SIM-gated)

Two tracks interleave on the shared schema. Every PR keeps SIM-only placement gating and lands green on the acceptance DSL. Steps 1–8 ship in-tree, one repo, dark/null-defaulted — the cut-line is proven green *before* the physical split. **Each PR is independently behavior-preserving** (a refactor extracting a port with its default wired at the composition root, or a dark-flagged addition), so there is no known-broken interval between PR-3 (EarningsPort) and PR-7 (intent carrier) — the gate works identically via injection throughout. **The acceptance DSL green on every PR IS the coherence gate** (zen review): it drives the real manager and asserts the six SLA guarantees, so a half-refactored safety feature cannot merge green.

**Foundation (shared A):**
- **PR-1 (geometry) — `exit_geometry/` leaf.** `levels.py` (`atr_bracket_levels`, `ceiling_from_52w_high`) + `registry.py` (`ExitGeometryPolicy`, register `atr_bracket_1p5`/`bezpazery`). Pure, unwired. Tests + repoint `feedback/` to call the leaf (L3 golden-replay byte-identical).
- **PR-2 (both) — `trade_intent/schema.py` + `ports.py` leaves.** `TradeSpec`, `ExitGeometrySpec`, `TradeIntent`, `EarningsPort`, `NotificationPort`. Export `paper.sizing` types from A. No behavior change.

**Kill the non-brief couplings (cheap, independent, do early):**
- **PR-3 (extraction) — `EarningsPort`, break `brokers→thematic` (V3).** `earnings_gate` takes the injected port; default wired at the composition root as the thematic-backed adapter (no live behavior change). Add the `brokers ↛ thematic` tripwire. Sharpest violation, fixed first.
- **PR-4 (extraction) — `NotificationPort` + shelve infra (V4/V5/V6).** Formalize `_default_alert` as injected `NotificationPort`; route the chain-loss alert through it; `observability/textfile` + `polygon_client` → A2. Add `brokers ↛ feedback` and `brokers ↛ data.alt_data.telegram` rules.

**Split sizing + go geometry-live in-process:**
- **PR-5 (both) — split `compute_setup_plan`.** `parse_brief_to_spec` + `validate_trade_setup` → client module (`thematic/intent_builder.py`, emits `TradeSpec`); `compute_setup_plan(TradeSpec, equity, fx)` stays pure in shared A. Behavior-preserving.
- **PR-6 (geometry) — wire override at `_build_planned_line` + the realized-blend re-anchor (P0, §4.3).** `parse_brief_to_spec` also emits the `ExitGeometrySpec`, threaded through `_place_pick` in-process. Override stop/tp from the leaf when policy active; **stamp both anchors + `(name,version)`** on the `planned` line. **MUST also include the `avg_price` re-anchor** (recompute stop/TP off `Position.avg_price` via the `AmendStop` rail once the fill-set completes, so live risk distance = k·ATR) and the 2-tier gapped-fill drift fail-case as the gate — the planned-blend anchor alone places a wrong-distance stop (§4.3). Dark flag default `setup_static`. Ship stamp-only shadow → confirm the recompute holds the invariant → flip. **Geometry value delivered here, before the carrier changes.**

**Formalize the carrier (kills the brief read):**
- **PR-7 (extraction) — `arm_pick` carries `TradeIntent`.** `arm_command` (already loads the brief) persists the full `TradeIntent` into `picks.jsonl`; `iter_picks` yields `TradeIntent`; `_place_pick` drains it and **deletes `load_brief` (V1) + the brief-coupled parse (V2)**. Daemon never touches a brief. No back-compat for old `(ticker,date)` lines (solo-project doctrine — re-arm).
- **PR-8 (extraction) — `ManagerService` Protocol + tripwire gate.** Wrap the three faces as `submit_intent`/`query_state`/`stream_events`; re-point `world.run_tick` through the in-process transport adapter; all six guarantees green through the Protocol. Add the remaining dep rules, all green incl. function-scope. This is the pre-extraction proof that A/B/C is physically separable.

**Then (separate project — the extraction epic, ADR 0006 precedent):**
- Extract **A** first (`broker_contract`), publish, pin in both `pyproject.toml`s.
- Extract **B** (`brokers/` service repo consuming A).
- AlphaLens keeps a thin CLI shim building `TradeIntent`.
- **Add the tenant dimension here, not before:** `account_id` on every intent + journal path + safety cap; `MAX_OPEN` per-tenant. The diff is transport + tenancy, not logic.

---

## 7. Top 3 risks + mitigations

**Risk 1 — Null-`EarningsPort` silent rail-off (cost of choosing the port over a carried fact).** The null default keeps a feed-less client running but converts a missing safety input into a *silent* degradation. *Mitigation:* (a) require null wiring to be **explicit** (`NullEarningsPort()` passed, no implicit default); (b) the composition root emits a startup diagnostic through `NotificationPort` naming every null port ("earnings gate INERT"); (c) acceptance guarantee #5 asserts a null-port inert gate surfaces its inert state.

**Risk 2 — TWO distinct anchor risks; do not conflate them (zen review sharpened this).**
- **2a — planned-vs-realized BLEND anchor = P0 (see §4.3), NOT acceptable-as-is.** The bracket anchored to planned-blend while the stop price stays static under grow-amend places a wrong-distance stop. This is safety-adjacent and is FIXED by the mandatory `avg_price` re-anchor in PR-6 — not by the shadow, not by SIM-only. Do not ship the flip without it.
- **2b — carried anchor-FACT staleness (ATR, 52w-high frozen at arm) = bounded, acceptable.** These drift between arm and placement, but the 7-day ENTRY TTL bounds the window, re-arming refreshes them, and the recompute (2a) refreshes ATR at fill-completion anyway. Path-dependent policies recompute live and are immune. This is the residual that carrying-in-intent (vs a market-data port) legitimately accepts — because *this* part is not a rail; 2a is the part that is.

**Risk 3 — "any client" oversold at first extraction; multi-tenancy deferred.** The smallest cut keeps `compute_setup_plan`, `MAX_OPEN`, caps, `picks.jsonl`, and the journals as single-account singletons under one `~/.alphalens`. *Mitigation:* state the scope explicitly (first extraction = one tenant, transport-swappable; betlejem5-as-client is illustrative) and **reserve `account_id` in the schema now** so tenancy is additive, not a wire-format break. Port proliferation is mitigated by design — only two ports; `Clock`/`Calendar` left as pure shared functions.

---

## 8. Open questions for the operator

1. **Port/fact split rule.** Confirm: *port a fact iff it gates a safety rail AND can change arm→fill* → earnings = port, geometry anchors = carried fact, time/calendar = pure shared function.
2. **Dark-flag soak criteria.** What anchor-divergence threshold and how many SIM sessions gate the flip of `ALPHALENS_BROKER_EXIT_POLICY` from `setup_static` to `atr_bracket_1p5`?
3. **Shared-package naming + A2 placement.** Name the standalone leaf `broker_contract` (vs alternatives), and decide whether A2 infra ships *inside* the shared package or as a separate pinned infra module.
4. **Multi-tenancy timing.** Is `betlejem5`-as-a-real-client on the near roadmap? If yes, `account_id` scoping moves *into* the extraction epic; if illustrative, it stays a reserved field.
5. **Transport choice for extraction.** HTTP, queue, or local socket for the first non-in-process transport? Decides the `TradeIntent` serialization format and whether `stream_events` is long-poll, SSE, or a queue subscription.
6. **`kind="levels"` (bring-your-own) — genuinely deferred?** Confirm no first-extraction client needs explicit-levels geometry, so it stays a reserved discriminant.

---

**Related:** ADR 0011 (pipeline/research split), 0013 (trade-side layers + version keys), 0014 (broker-agnostic execution), 0006 (phase-robust-backtesting extraction precedent). Exit-geometry lens registry: `feedback/breakeven_lenses.py` + `feedback/ladder_replay.py`. Acceptance DSL: `apps/alphalens-research/tests/brokers/automanager/acceptance/`.
