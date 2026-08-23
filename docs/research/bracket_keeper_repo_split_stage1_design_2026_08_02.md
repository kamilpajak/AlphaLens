# bracket-keeper — repo split Stage 1 design

**Status:** PARKED (blueprint ready; do NOT execute now). Unpark trigger: a real 2nd consumer ~4 weeks out. When unparked, execution still waits until after a clean go-live soak.
**Date:** 2026-08-02
**Owner memo:** `docs/research/broker_manager_extraction_and_exit_geometry_2026_07_31.md` (LOCKED). This doc details the "2B" physical-split step left open there ("New repo: CI, packaging, version-pin, deploy mechanics").
**Baseline:** origin/main `7d6783f9` (post in-tree 2A arc; `broker_contract` shared leaf complete).

---

## 0. Decision — PARKED (2026-08-02, post-adversarial-review)

Adversarial review (own critique + zen `deepseek-v4-pro` high + Perplexity uv
fact-check) plus operator input concluded: **park the physical repo-move.**

- **No 2nd consumer for ≥2 months** (operator). The split's benefit only
  materializes with a real 2nd consumer; the in-tree 2A arc already banked the
  dependency-decoupling value. Doing 2B now = pure cost (2 repos, SHA-pin dance,
  CI + calendar duplication) for no near-term benefit → **YAGNI**.
- **Do NOT touch the live daemon's composition root before go-live.** The earlier
  "2B-pre is safe weekend work" framing is **withdrawn**: P2 changes
  `build_default_deps` (the wiring the live daemon runs); changing
  protection-critical code the weekend before a money-adjacent go-live is an
  unforced risk that also widens the gap between the pinned go-live commit and
  main. Defer ALL of 2B (pre + move).
- **Honest Stage-1 scope:** when unparked, Stage-1 delivers CODE SEPARATION +
  dependency hygiene, NOT a turnkey reusable service. A pluggable composition
  root (a default runner + injectable adapters `bracket-keeper` ships) is
  Stage-2; its acceptance gate is a trivial 2nd consumer wired WITHOUT touching
  the original daemon.

Everything below (§1-§9) is the ready-to-execute blueprint for when the unpark
trigger fires. R2 (git-dep pin — uv-confirmed: `git+#subdirectory=`+SHA, downstream
does not inherit upstream workspace refs), R3 (own calendar copy for a stable
helper), R4 (daemon stays in monorepo for Stage-1) stand, with the triggers noted
in §4/§7. `broker_contract` pyproject verified standalone (`dependencies=[]`,
setuptools, no workspace refs) → git-subdirectory consumption is buildable.

---

## 1. Problem / how it works today

The broker-manager (SIM Saxo auto-manager) is a pure executor living in-tree at
`apps/alphalens-pipeline/alphalens_pipeline/brokers/`. The in-tree 2A arc
(PR-1..8 + earnings-deletion + 2A-1..2A-4a) already:

- extracted the shared A-tier leaf `broker_contract` (stdlib-only: `contract` Protocol + types, `trade_intent/{schema,codec}`, `exit_geometry/{levels,registry}`, `sizing` money-math, `fx`, `constants`);
- cut the load-bearing couplings client-side (earnings gate deleted, `NotificationPort` injected, daemon is brief-free, `ManagerService` Protocol proven separable).

What is left for a **physical** split into a standalone, client-agnostic repo
(`bracket-keeper`) is: the residual upward edges from `brokers/` into the
AlphaLens monorepo, plus the packaging / CI / deploy mechanics of a second repo.

### Verified residual edges (the only blockers)

`brokers/** → alphalens_pipeline.*` (excluding `broker_contract`), the COMPLETE
post-arc set — three targets:

| # | edge | scope | sites | drags |
|---|------|-------|-------|-------|
| E1 | `paper.calendar` (`trading_days_elapsed`, `advance_trading_sessions`) | TOP-LEVEL | `reconcile.py:58`, `saxo/broker.py:59` | **pandas + exchange_calendars** |
| E2 | `observability.textfile` (`emit_domain_metrics`) | lazy (fn body) | `control_loop.py:247`, `:597` | prometheus textfile write |
| E3 | `paper.sizing.planned_blended_entry_from_spec` | lazy (fn body) | `control_loop.py:1649` | none (pure over `TradeSpec`) |

Third-party deps already inside `brokers/**`: **`requests`**, **`websockets`**
(Saxo adapter only). No pandas/numpy/httpx directly in `brokers/`.

Broker **tests** live at `apps/alphalens-research/tests/brokers/` (reconcile,
saxo_broker, broker_contract, control_loop, acceptance `world.py` + 6 guarantee
files) — these move with the code.

The CLI composition root `alphalens_cli/commands/broker.py` imports every
`brokers.*` symbol **lazily** (inside command bodies / `TYPE_CHECKING`). So the
daemon entrypoint does NOT structurally have to move (see §5).

## 2. Goal

Make `brokers/` a standalone private repo **`bracket-keeper`** that depends only
on `broker_contract` + third-party, consumable by AlphaLens (and future clients:
betlejem5, others) as a pinned dependency — **behavior-preserving**, file-journal
persistence untouched (Stage 1; the daemon stays a single in-process consumer, so
no network boundary yet — that is Stage 2 / Q5 transport, deferred).

Client-agnostic name chosen: **`bracket-keeper`** (repo) / **`bracket_keeper`**
(import package). Private repo.

## 3. Two sub-stages

The residual edges force a natural split. **2B-pre is safe weekend work**
(in-tree, behavior-preserving, does not touch the pinned go-live). **2B-move is
the outward-facing, irreversible step — deferred until after the Monday soak.**

### 2B-pre — sever the 3 residual edges (in-tree, Workflow cadence, this weekend)

Each is a behavior-preserving refactor landing on AlphaLens main. After all
three, `brokers/` imports ONLY `broker_contract` + third-party, locked by a new
tripwire.

- **P1 (E3) — move `planned_blended_entry_from_spec` (+ helper `_blend_priced_tiers`) into `broker_contract.sizing`.**
  It is a pure computation over a `TradeSpec` (client-half kept in `paper/` by
  2A-4a, but `control_loop` is its only broker consumer). Verify leaf-safe (only
  stdlib + `trade_intent.schema` — expected yes), then move; `paper/sizing.py`
  re-imports one-way from the leaf (no alias) for its own client uses, as 2A-4a
  already does for `TradeSetupNotPlannableError`. Smallest edge, do first.

- **P2 (E2) — inject the metrics sink as a callback** (mirror PR-4's
  `NotificationPort`). `control_loop` takes a `metrics_sink: Callable[..., None]`
  in `build_default_deps`; the CLI composition root wires the real
  `observability.textfile.emit_domain_metrics`. Removes the `observability` edge
  without dragging AlphaLens infra into the leaf-only service. Behavior-preserving
  (the real emitter is still called in prod; a no-op default in tests).

- **P3 (E1) — calendar.** `paper.calendar` drags pandas + exchange_calendars, so
  it cannot enter the stdlib-only `broker_contract` leaf. The calendar helper is
  already exchange-parametrized (MIC) and **already duplicated Django-side** —
  duplication is the established pattern. **Recommendation: `bracket-keeper`
  carries its own `calendar.py`** (moved/vendored, declaring its own
  `pandas` + `exchange-calendars` deps). AlphaLens keeps `paper.calendar` for its
  other consumers (feedback replay, `/v1/market/status`). At 2B-pre time this is
  just: confirm the plan; the physical duplication happens in 2B-move. (This is
  the deferred 2A-4b decision — see §7 alternatives.)

  After P1+P2, the ONLY residual edge is `paper.calendar` (E1), which 2B-move
  resolves by giving `bracket-keeper` its own copy. So the 2B-pre tripwire is:
  `brokers ↛ alphalens_pipeline` **except** `broker_contract` **and**
  `paper.calendar` (the one sanctioned edge until the repo move duplicates it).

### 2B-move — create the repo + physically relocate (AFTER go-live soak)

Irreversible / outward-facing. Ordered steps:

1. **Create private repo `kamilpajak/bracket-keeper`** (operator action / explicit confirm).
2. **Move `brokers/**` → `bracket_keeper/`** in the new repo (git history preserved via `git filter-repo` or subtree). Add `calendar.py` (P3 copy).
3. **Wire `broker_contract` as a pinned dependency** of `bracket-keeper` — see §4.
4. **Move broker tests** (`apps/alphalens-research/tests/brokers/`) → `bracket-keeper/tests/`; stand up its CI (uv sync + unittest discover + ruff, mirroring AlphaLens research CI = unittest discover, NOT pytest).
5. **Rewire AlphaLens** to depend on the `bracket-keeper` dist (pin), delete the in-tree `brokers/`, repoint `alphalens_cli/commands/broker.py` lazy imports `brokers.*` → `bracket_keeper.*`. The `alphalens broker manage` / `auth` CLI stays put (composition root, §5).
6. **Update VPS deploy** — `uv sync` pulls the pinned `bracket-keeper`; daemon still runs `alphalens broker manage` host-venv (unchanged operator workflow). Bump = bump the pin + `uv sync` + `systemctl --user restart alphalens-broker-manager`.
7. **Merge + extraction-deploy in a cycle AFTER go-live soak** (memo: avoid hotfix-fork).

## 4. Cross-repo `broker_contract` sharing (decision #2)

`broker_contract` must be consumed by BOTH repos. Options:

- **(a) git-dep pin to the AlphaLens subdirectory** — both repos declare
  `broker_contract @ git+https://github.com/kamilpajak/AlphaLens.git@<sha>#subdirectory=apps/alphalens-broker-contract`.
  AlphaLens stays the SoT; `bracket-keeper` pins a commit. Zero publish infra,
  private-repo friendly, matches the ADR 0006 precedent (`phase-robust-backtesting`
  consumed as a pinned dep). **RECOMMENDED for Stage 1.** Caveat to verify: uv
  `#subdirectory=` git-source support (fallback: promote `broker_contract` to its
  own repo, option c).
- **(b) publish `broker_contract` to a package index + pin** — cleanest for a
  truly standalone service, but adds publish steps and a private index (it is a
  personal/private project). Defer to Stage 2 if/when `bracket-keeper` goes
  multi-consumer in earnest.
- **(c) move `broker_contract` to its own repo first** — architecturally the
  "right" home for a contract shared by two service repos, but a second repo to
  version now; premature for Stage 1.
- **(d) git submodule** — fragile, rejected.

## 5. CLI / daemon placement (decision #3 — resolved by the dependency map)

The daemon entrypoint **stays in AlphaLens**. `alphalens broker manage` /
`auth --refresh` live in `alphalens_cli/commands/broker.py` (composition root,
sanctioned to import infra: telegram, calendar, observability), and every
`brokers.*` import there is lazy. So AlphaLens remains the place that wires infra
into the pure executor; `bracket-keeper` is a **library/service dependency**, not
its own binary, in Stage 1. Big win: the operator workflow and the systemd unit
(`alphalens-broker-manager` running `alphalens broker manage` host-venv) are
**unchanged** by the split. A dedicated `bracket-keeper` daemon binary is a
Stage-2 concern (transport reimpl, Q5).

## 6. Risks / rollback

- **Circular-dep hazard** — `bracket-keeper` must NEVER import back into
  `alphalens_pipeline`. That is the entire point of P1-P3. Enforced by
  `bracket-keeper` CI (its own dep-direction test) + the AlphaLens 2B-pre tripwire.
- **Pin drift** — a `broker_contract` change in AlphaLens needs a deliberate pin
  bump in `bracket-keeper`. Explicit pin, deliberate bump; not `@main`.
- **Go-live isolation** — 2B-move lands AFTER soak by design; the Monday go-live
  deploys the in-tree pin `2a75993b`, untouched. Until 2B-move merges, everything
  is in-tree and reversible.
- **Rollback of the move** — keep a rollback pin; if the rewire PR misbehaves,
  revert it and the daemon runs from in-tree `brokers/` again (the delete is the
  last step).

## 7. Alternatives considered (calendar)

- Relax the `broker_contract` leaf invariant to allow pandas/exchange_calendars —
  rejected: kills the "publishable stdlib leaf" property that the whole 2A arc
  protected.
- A separate shared `market-calendar` package both repos consume — viable, but a
  second shared package to version now; the Django-side duplication precedent says
  copy-into-consumer is acceptable, so defer the shared-package option.

## 8. Open decisions for the operator

1. **Repo host/visibility** — `kamilpajak/bracket-keeper`, private? (assumed yes)
2. **`broker_contract` sharing** — §4 option (a) git-dep pin [recommended], (b) publish, or (c) own-repo-first?
3. **Calendar** — §3 P3: `bracket-keeper` carries its own copy [recommended], or shared `market-calendar` pkg?
4. **Timing** — confirm 2B-pre this weekend (in-tree, safe) + 2B-move after Monday soak. Repo CREATION itself waits for explicit go-ahead.

## 9. Verification / test plan

- **2B-pre:** each P-step keeps the broker + sizing suites at unchanged expected
  values (behavior-preserving); new tripwire `brokers ↛ alphalens_pipeline`
  (except `broker_contract` + sanctioned `paper.calendar`) with a non-vacuous
  positive control; full research suite green; zen deepseek-v4-pro high per PR.
- **2B-move:** `bracket-keeper` CI green (its moved tests + its own dep-direction
  test); AlphaLens green after the rewire (the acceptance guarantee suite must
  stay byte-identical through the import repoint); `uv sync` resolves the pin on a
  clean clone; VPS dry-run `uv sync` + daemon restart on SIM before flipping the
  live unit.
