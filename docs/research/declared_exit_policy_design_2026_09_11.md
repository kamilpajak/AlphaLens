# Declared exit policy — the permission to move a stop becomes a document fact

**Status: LOCKED** (2026-09-11)
**Issue:** [#1236](https://github.com/kamilpajak/AlphaLens/issues/1236), step **B** of the
[#1403](https://github.com/kamilpajak/AlphaLens/issues/1403) arc (after #1389 and #1404).

## 1. The problem

Whether the daemon may move a position's stop is decided today by a **side effect**:
`PlannedExit.reanchor is not None`, which is true only when the `planned` journal line
carries a `geometry` blob with a finite ATR. *Which* move then happens is chosen by one
process-wide environment variable, `ALPHALENS_BROKER_EXIT_POLICY`.

The intent document says nothing about either. That blocks
[#1406](https://github.com/kamilpajak/AlphaLens/issues/1406) (`arm --from-intent`): a door
that accepts a `TradeIntent` from an external producer has nothing to accept, and nothing
to refuse, on the subject of exit management.

The goal of this work is therefore narrow and stated up front: **make the permission a
declaration in the document.** It is a precondition for #1406, not a feature anyone is
waiting to use. `arm-manual` gets no new flag; the only future producer of a declaration
is the door itself.

## 2. What was measured

Every number below was produced by running code against real data on 2026-09-11, against
`origin/main` @ `068e7e82`. Nothing here is read off a docstring.

### 2.1 A premise in the issue thread is false

The issue comment of 2026-09-10 states that across the full history of both instances,
"26 LIVE intents and 37 SIM — exactly zero carried a geometry". Counted against today's
journals:

| source | carries geometry | does not |
|---|---|---|
| `picks.jsonl` LIVE (`intent.exit`) | **15** | 11 explicit nulls + 16 lines predating the field |
| `picks.jsonl` SIM | **30** | 7 + 47 |
| `planned` lines, LIVE | **3** | 9 |
| `planned` lines, SIM | **21** | 23 |

What *does* hold is the **conclusion**: there is not a single `trailed` marker in either
instance. Nothing has ever trailed — but not for the stated reason. Positions were
eligible and the arm simply never fired.

This matters because the two readings imply opposite designs. "Nothing ever carried a
geometry" invites a free hand. "Geometry is everywhere and eligible" means any change to
what the presence of geometry implies is a live-money change.

### 2.2 The brief path always produces geometry, and such a pick trails today

112 brief parquets → 940 rows with a `trade_setup`, 936 plannable. **936 of 936** build an
`ExitGeometrySpec` carrying a `ReanchorOnFill` with a finite ATR. No row lacks the `atr`
key; no bracket is unbuildable.

Pushing one real brief row through the whole chain — `build_exit_geometry_spec` →
`_geometry_shadow_stamp` → `_build_planned_line` → `_fold_planned_exits` →
`_reconcile_long` — under the deployed `breakeven_trail` policy yields an `AmendStop`.
**A brief-armed pick trails today**, and its declaration is `ReanchorOnFill`.

### 2.3 The migration is a measured non-event on LIVE

A read-only `broker status --env live` at 08:19 UTC (orders disabled) reports five open
positions: uic 29957 LULU, 23402 ALB, 641 RHI, 13697176 UBER, 19756014 QUBT. The uics
still carrying a `geometry` stamp are 6734 GME, 23474 OLN and 20237 SMG — **none of them
holds an open position.** The SIM journal's 21 geometry-bearing lines are harmless but not
empty, so SIM is not a non-event, merely a sandbox.

State can change before deploy, so this read is a **pre-deploy step**, never a guarantee.

### 2.4 The inherited trailed level is real and reaches the `--no-tp` shape

Running `_fold_trailed_since_latest_plan`: a pick armed `--no-tp` on a uic that an earlier
pick trailed **inherits** that earlier level; a pick carrying TP tranches resets the fold
to empty. `--no-tp` is, in the CLI help, literally "trail-only pick" — the very shape a
trailing declaration would use.

The consequence is not a bad stop: `_build_managed_exits` skips a uic with no
`tranche_plan`, so nothing wrong gets placed. The consequence is that **the opt-in is
silently dark** until the high-water mark clears the previous position's level.

### 2.5 Compaction would disarm a reset keyed on `planned`

`_compact_standalone_stop_journal_lines` returns the order
`planned(A), planned(B), tranche_plan(A), trailed` — the marker lands *after* both
`planned` lines, and the stale level survives compaction unchanged. A reset driven by
`planned` lines would therefore fire before the marker is read and achieve nothing. The
identity has to live **on the marker**, not in a reset.

### 2.6 The ATR capability

`BreakevenTrailPolicy.decide_reanchor` already returns an identical target with
`atr=None`. `AtrBracketPolicy.decide_reanchor` **raises `TypeError`** on `atr=None`.
Removing the caller-side ATR veto before teaching the ATR family to refuse would convert a
veto into a crash inside the protection pass. This fixes the order of the work.

### 2.7 The env var controls two things, not one

`ALPHALENS_BROKER_EXIT_POLICY` decides **(1)** how a stop is managed after fill and
**(2)** whether the client's levels are *placed at all*
(`use_geometry = policy.applies_geometry and exit_spec is not None`). Under the deployed
`breakeven_trail` (`applies_geometry=False`) the client's `initial_levels` are **not**
placed: `_journal_tranche_plan_core` sources the ladder from `plan.tp_tranches` and
journals the stop verbatim. This work takes over (1) only.

## 3. Decisions

1. **Purpose is the precondition for #1406.** No `arm-manual` flag. The acceptance suite
   is the only executable proof, because `world.arm()` hands a `TradeIntent` straight to
   the service without touching the CLI.

2. **Every producer declares what it wants.** `ReanchorOnFill` means literally "re-anchor
   on fill"; `TrailingStop(arm_trigger_r, trail_frac)` means "trail"; no declaration means
   the stop is never moved. The brief path starts declaring `TrailingStop(0.5, 0.6)` —
   which is what actually runs in production — so its behaviour is unchanged.

   The alternative readings were rejected for concrete reasons. Letting `ReanchorOnFill`
   mean "defer to the daemon's env var" puts a primitive in the contract whose meaning
   lives in our deployment, which is the coupling the declaration exists to remove.
   Letting it mean ATR re-anchor *without* changing the brief path would silently move
   every brief pick off `breakeven_trail`, reversing the decision recorded in
   `breakeven_trail_live_policy_design_2026_08_27.md` with nobody deciding it.

3. **Placement becomes a declaration too — in a separate issue.** Presence of
   `initial_levels` will mean "place these"; absence will mean "place the ladder from
   `spec`". That kills `ALPHALENS_BROKER_EXIT_POLICY` entirely, along with the LIVE boot
   rail `live_rails._check_exit_policy` and its pins in three unit files, and it forces a
   decision on the #1114 anchor-divergence stamp. The order is forced, not a preference:
   that issue removes `initial_levels` from the brief path, and this one must first give
   that path something to declare.

## 4. Design

### 4.1 Contract

`ExitGeometrySpec.initial_levels` becomes `InitialLevels | None`. Without that, the
sentence "declares `TrailingStop` with no geometry supplied" cannot be written down at
all. `codec._decode_exit` stops requiring the key.

This invalidates a property recorded on 2026-09-05: that `exit_spec is None` already
forces `use_geometry=False` for a manual pick. After this change a manual pick **may**
carry a non-null `exit`.

Six sites in `control_loop.py` decide whether client geometry is placed, and five more
dereference `exit_spec.initial_levels.*` — including the #1112 cost gate at drain. After
the type change each one is an `AttributeError` on the money path, and two of them pass
`applies_geometry` onward without the `exit_spec` clause. They therefore all route through
a single predicate:

```python
def _places_client_geometry(policy, exit_spec) -> bool:
    return (
        policy.applies_geometry
        and exit_spec is not None
        and exit_spec.initial_levels is not None
    )
```

One fact instead of eleven, only one of which has to be forgotten.

### 4.2 Refusals at the door

`validate_intent` (the #1404 module; published in the broker-contract README beside the
`intent_invalid` row) gains: at most **one** stop-management primitive in `reaction_plan`;
`ReanchorOnFill` requires `initial_levels`; `arm_trigger_r > 0`, `trail_frac` in `(0, 1]`,
`k_atr > 0`, every number finite — **finiteness checked before any comparison**, because a
NaN answers `False` to all of them; `ModelPush` refused, since its levels arrive through an
`amend_exit` call that does not exist.

`ReanchorOnFill.ceiling_price` is also refused, for a reason worth recording. It reads
like a stop-side cap, and it is not: in `atr_bracket_levels` it applies as
`tp = min(tp, ceiling_price)` and never touches the stop, which is
`blended - stop_atr_mult * atr`. It is a take-profit — that is, a *placement* — parameter,
so this work has nothing to honour it with. Today it is silently dropped (`ReanchorFacts`
keeps only `k_atr` and `atr`), and a door must not accept a field it discards. The
placement issue lifts the refusal.

### 4.3 Resolution

A pure `resolve_declared_policy(reaction) -> ExitPolicy` in `broker_contract.exit_geometry`:
`None` → `SetupStaticPolicy()`; `TrailingStop(r, f)` → `BreakevenTrailPolicy(r, f)`;
`ReanchorOnFill(k, atr)` → a re-anchor policy parameterised by the **declared** `k`, not by
the registry's `geom.stop_atr_mult` — otherwise a declared `k_atr=2.0` would be silently
ignored, which is the defect class this work exists to remove. The shared arithmetic goes
in `exit_geometry/levels.py` next to `chandelier_target` and `fractional_giveback_target`.

Parameters are free within the rules above. That is the point: 0.5R is not a comparable
quantity across hand-set stops (1R is 6.8% of entry on AMBA and 29% on RHI), so an
operator or a client must be able to choose. The policy `name` stays the registry family
name and the parameters travel in the journal stamp, so an audit can still say what ran.

### 4.4 Carrying the declaration to the daemon

The protection pass never sees the intent; it reads the journal. The `planned` line gains
a `"reaction"` key holding the jsonable primitive — encoder `dataclasses.asdict`, decoder
`codec._decode_reaction_primitive`, both already in the tree. The entry-trail path passes
it through `watch_open` exactly as it passes `geometry_stamp`. `_fold_planned_exits` folds
it into `PlannedExit.reaction`, and `PlannedExit.reanchor` / `ReanchorFacts` are removed:
the declaration is now the single source of both the permission and the ATR, and keeping
both would preserve the overload this work removes.

A malformed `"reaction"` key degrades to `reaction=None` and **the line survives**. This is
not defensive habit: `_latest_planned_by_crid` filters only on `kind`/`crid`/`gen`, so an
unguarded decoder raises `TradeIntentDecodeError` (a `ValueError`), and
`_run_protection_pass` catches only `BrokerError` — the exception would escape the entire
protection pass and leave every position unmanaged for that tick.
`_reanchor_facts_from_governing` already does exactly the right thing for a malformed
`geometry` blob; this mirrors it.

### 4.5 Per-position policy

`_reconcile_long` selects `resolve_declared_policy(plan.reaction)` instead of
`view.exit_policy`. Both arms read it and stop vetoing on a missing stamp — the veto is now
a missing declaration. `view.exit_policy` remains only for the placement path.

Two consequences follow that the issue thread did not name.

**The boot capability gate stops covering declared policies.** `build_default_deps`
fail-fasts when `exit_policy.requires_amend_stop` and the broker lacks `SupportsAmendStop`
— but that is the *daemon's* policy. A declared `TrailingStop` (`requires_amend_stop=True`)
walks past that gate whenever the daemon sits on, say, `setup_static`. And the executor
holds `if amend_stop is None: return` — a silent exit whose comment ("the pure arm never
emits this") stops being true. The declaration would then be ignored with no alert, no
marker and no trace, which is precisely the failure a door cannot have. Two separate fixes,
because they fail at different moments: the boot gate requires `SupportsAmendStop`
unconditionally once the declarative path is live, and the executor's silent return becomes
a throttled alert.

**The re-anchor idempotence latch needs the same identity.** `reanchored_by_uic` is keyed
by uic and `avg_price` over the journal's lifetime. That was documented and accepted while
every pick shared one `k` from the registry, so a suppressed re-anchor would have targeted
the same level anyway. Once `k` is declared per pick, a new position can be suppressed at
another position's blend and target something different. Since the `planned` line now
carries `pick_key`, resetting this latch on a new key is free, and leaving it unreset would
be an unexplained asymmetry with the `trailed` marker.

### 4.6 Peaks

`_run_protection_pass` can no longer ask `deps.exit_policy.trails`, because trailing is now
a property of one position. The peak fetch stays **outside** `build_protection_view`: that
function is a pure assembler, and putting a network call inside it would move I/O under a
boundary that catches only `BrokerError`. Instead the pass runs a cheap pre-fold of the
journal for uics that declare trailing and hands that set to `_fetch_protection_peaks`,
which keeps its own boundary. Peaks are then fetched only for those uics, where today they
are fetched for every long. The cost is a second journal read on the trailing path, which
on a never-naked path is worth the boundary.

### 4.7 Trailed-level identity

The `trailed` marker carries the governing `pick_key`: `planned` lines gain the field,
`PlannedExit` carries it, `_maybe_trail` puts it on the `AmendStop`, and the executor
journals it. `_fold_trailed_since_latest_plan` becomes two-pass and **order-insensitive** —
it first establishes the governing key per uic from the newest `planned` / `tranche_plan`
line anywhere in the file, then keeps a marker only when the keys agree.
`_build_managed_exits` compares the same way. A missing key on both sides counts as a
match, so lines written before this change keep their behaviour instead of being dropped.

Order-insensitivity is not elegance. A reset keyed on `planned` lines would be reordered
into uselessness by the compactor (§2.5); an identity carried on the marker cannot be. The
compaction election `_elect_trailed_lines` must mirror the new fold — the trap recorded in
`reference_journal_compaction_evicts_sticky_fields`.

## 5. Scope

Two pull requests on this issue.

**PR-1 — identity and capability.** The `decide_reanchor(atr: float | None)` move, with the
ATR family refusing cleanly; the `trailed` marker's pick identity with its two-pass fold and
mirrored compaction election.

PR-1 is often described as behaviour-neutral. Half of it is: the capability move is neutral
by construction, because the callers still veto on a missing stamp. The identity fix is
**not** neutral in principle — changing which markers survive the fold *is* the repair. It
is neutral only on the measured population, which contains zero `trailed` markers. The
distinction belongs in the PR description, because "neutral because the population is empty
and I checked" is a claim someone can re-check, and "neutral by definition" is not.

**PR-2 — the declaration.** Everything in §4.

## 6. Acceptance

Four sentences that must hold whatever the broker is. They are the design step, written
before the wiring, and they live in
`tests/brokers/automanager/acceptance/` as the suite's seventh promise:

1. A pick that declares nothing does not move its stop, whatever `EXIT_POLICY` says.
2. A pick that declares `TrailingStop` trails — with no geometry supplied. This sentence
   cannot be written today; it is the change.
3. A pick declaring a policy that needs geometry, without geometry, is refused at arm time,
   loudly.
4. The same intent behaves identically with `meta.source` `"brief"` and `"manual"` —
   provenance is not policy. No form of this sentence exists today.

`world.py` cannot express these yet; it has coverage verbs only. It gains
`arm(..., exit_policy=...)`, `assert_stop_at` and `assert_stop_did_not_move`.

Alongside them, the checks without which the four could go green vacuously: a positive
control that the same shape *with* a declaration does move the stop; a brief-path
equivalence check pinning the exact `AmendStop.stop_price` measured today; the inherited
level not binding a later pick **including after compaction**; the ATR family refusing
rather than raising on `atr=None`; a malformed `reaction` still yielding a protected
position; and `test_manual_pick_no_stop_move.py` staying green **without edits**, since a
manual pick declares nothing.

## 7. Risks

- **The only producer of a declaration does not exist yet** — it is #1406. The whole
  declarative path is proven by the acceptance suite alone. This is the chosen scope, not
  an oversight.
- **A manual pick may now carry a non-null `exit`**, which the 2026-09-05 analysis ruled
  out. The `_places_client_geometry` predicate is what keeps that from reaching a geometry
  ladder under an `applies_geometry` policy.
- **Compaction must mirror the new fold**, or the identity fix is undone at the next boot.
- **SIM carries 21 geometry-bearing `planned` lines.** Harmless, but not empty — SIM is not
  the non-event LIVE is.
- **Removing `PlannedExit.reanchor` touches many trailing-arm tests.** That is the cost of
  deleting an overload, not a symptom of a bad split.
- **Re-check §2.3 before deploy.** If an open position does sit on a geometry-bearing
  `planned` line by then, its stop stops being managed — fail-safe, since the stop stays
  put and is never loosened, but it must be accepted deliberately rather than discovered.

## 8. See also

- [`breakeven_trail_live_policy_design_2026_08_27.md`](breakeven_trail_live_policy_design_2026_08_27.md)
  — the deployed policy decision this work preserves.
- [`broker_manager_extraction_and_exit_geometry_2026_07_31.md`](broker_manager_extraction_and_exit_geometry_2026_07_31.md)
  — the Boundary-2 contract and the deferred client-side validate step, delivered by #1404.
- [ADR 0014](../adr/0014-broker-agnostic-execution-layer.md) — the broker-agnostic
  execution layer this contract belongs to.
