# intent-replay — a document-driven backtester

**Status:** LOCKED 2026-09-25 (design agreed in brainstorming 2026-09-23; revised after adversarial
review the same day; revised again 2026-09-24 after an arming-surface survey and a second
adversarial review that refuted several statements of the first revision — see §3.3, §4.3.1, §8;
closed 2026-09-25 by deciding the five items §8.1 left open and the entry-trailing question of §8,
under the rule now stated as §2.1; §3.1 and §5.4 corrected the same day after an
adversarial review of the implementation plan found the JSON Schema gate
unimplementable under the old per-package dependency rule; §5.1 corrected
2026-09-26 after PR 5 ran both placement paths and refuted its claim about which
stop rests, #1597; implementation under way, epic #1571, PR 1-5 merged)
**Date:** 2026-09-23, last revised 2026-09-26
**Baseline:** `origin/main` `a444f6b2`
**Related:** epic #1526 (express every `/edge` what-if lens as a TradeIntent document),
`docs/research/bracket_keeper_repo_split_stage1_design_2026_08_02.md` (PARKED blueprint),
`docs/research/bezpazery_lens_design_2026_07_16.md` §7,
`docs/research/exit_policy_comparison_prereg_2026_08_24.md` (VOID) §2.2, §3.2.

---

## 0. What this is

A research tool. It takes one or more `TradeIntent` documents — the same JSON
`alphalens broker arm` accepts — plus price bars, and reports what each document
would have done: an ordered trace of what filled, where the stop stood, which
take-profit tranches fired, and a summary in stated units.

It is **not** the `/edge` lens engine. It stamps nothing, charges no
multiplicity budget, and carries no accrued history.

### Decisions taken

| question | decision |
|---|---|
| purpose | research tool, one-shot, CLI — not the `/edge` engine |
| input scope | one or more documents; a single document is a stream of length one |
| output | execution trace plus summary measures that carry their units |
| placement | a new dependency-free leaf package, `apps/intent-replay/` |
| language | Python, sharing the contract's own arithmetic |
| `/edge` | shared envelope shape now, no `/edge` code now |
| stop management | one implementation, extracted into the shared contract leaf |
| intra-bar ties | always pessimistic — a fixed convention, never a configuration field (§4.4) |
| every input path | classified here as interpreted, translated or out of scope; an unclassified path is refused, never approximated (§4.3.1) |
| where a run value comes from | the document, or STATED in the run configuration — never inherited from a deployment, an environment variable or a production constant (§2.1) |
| day-1 anchor | translated: the client resolves the first session and states it as `walk_start`; `meta.source` is not read here (§4.1, §4.3.1) |
| the trail's two order-state guards | not a configuration field. A replay has no order legs and no amend history, so both are inapplicable; the resulting divergence is REPORTED (§3.2) |
| the take-profit cost gate | its threshold is a required, stated configuration value; absent is a refusal, not a costless run (§2.1, §5.2) |
| `entry_mode: "immediate"` | refused in v1 with its own code; modelling it is a later version (§4.3.1, §5.4) |
| entry trailing | MODELLED, with the trail distance stated in the configuration. The alternative was a tool describing an entry ladder production does not use (§3.3, §8) |
| the R denominator | `spec.disaster_stop`, in every document — the level both deployments actually place. The earlier choice of `exit.initial_levels.stop` rested on a claim about the daemon that running it refuted (§5.1) |
| which take-profit ladder fires | the one the daemon places: a document supplying `exit.initial_levels` fires ONE tranche of 100% at its `tp`, and its own `spec.tp_tranches` never fires (§5.1) |
| the dependency rule | per MODULE, not per distribution: engine modules stay stdlib plus contract, the door and CLI may use `jsonschema` for gate 1. One barrier — the AST gate — not two (§3.1) |

---

## 1. How it works today, and the problem

A `/edge` what-if lens is a `BreakevenLens` dataclass in
`feedback/breakeven_lenses.py` carrying a `kind` plus loose parameters,
dispatched to one of three replay functions in `feedback/ladder_replay.py`.
Its input is a `trade_setup` mapping — a brief artifact.

The policy a lens replays is therefore written twice: once as lens parameters,
once as the live implementation in `broker_contract/exit_geometry/` and the
broker daemon. Nothing keeps the two copies in step. Three closed issues have
the same shape:

- **#1114** — the ATR-bracket lens anchored on the realised fill while
  production anchored on the planned blend. The two differed by 4.19 on SMG,
  2026-08-24, and the same gap landed on the derived stop.
- **#1232** — the breakeven lens replays ignored the 7-session entry-order TTL.
- **#1160** — the panel labelled every lens "exit-stop only", which is false for
  three of the six.

A fourth case never became an issue. The exit-policy comparison
pre-registration had to state in §3.2 that "Neither lens is arm B", because the
lens stop is static where the live stop re-anchors on fill.

### The measured fact that shapes this design

The existing replay is already almost dependency-free. At module scope it
imports only `math`, `collections.abc`, `dataclasses`, `typing` and one leaf
from `broker_contract`. Every AlphaLens coupling is a lazy import inside a
function, and every one of them exists to parse a `trade_setup`:
`parse_ladder`, `planned_blended_entry`, `arm_disaster_stop`, the `thematic`
ladder primitives.

Changing the input from `trade_setup` to `TradeIntent` is therefore the same
change as making the replay client-agnostic. "JSON as the argument" and the
bracket-keeper split are one piece of work, not two.

---

## 2. Goal and non-goals

**Goal.** Answer "what would this document have done" against real bars, with a
result a reader can check rather than trust.

**Non-goals.**

- Not a document generator. Where documents come from is a separate question.
- Not a `/edge` column. See §9.
- Not a cost model. Execution economics enter through an adapter supplied by
  the client, per the rule that broker economics live behind the adapter. That
  bounds what the replay COMPUTES, not what it needs: `_exit_clears_cost` refuses
  a take-profit tranche that does not clear its own round trip, so the threshold
  that gate compares against is a required configuration value (§2.1, §5.2). The
  replay neither derives it nor defaults it.
- Not a parameter optimiser. A PROXY measurement — a hand-written loop calling
  the real level functions, without order matching or trace allocation — ran a
  42-session minute-bar window (16 380 bars) at 1.3 ms per document. That is an
  UPPER BOUND on throughput, not a measurement of this design; the real
  interpreter will be slower, plausibly by an order of magnitude. The
  conclusion survives that penalty: nothing here is performance-shaped. Do not
  quote the figure as measured.

### 2.1 The run configuration is STATED, never inherited

Everything the replay needs is either read from the document or stated in the run
configuration. Nothing reaches it from a deployment drop-in, an environment
variable, a production constant or another module's default.

This rule decides five of the six items §8.1 used to leave open, which is why it
is stated once here rather than re-derived per field. A research tool whose
numbers depend on a value it inherited silently cannot answer the question it
exists for: the reader cannot tell which run produced which number, and a
deployment change months later moves a result nobody re-ran.

Three consequences, all deliberate:

- **A missing required value is a REFUSAL, never a default.** `config_incomplete`
  (§5.4) names the missing key. A default is a value the caller did not state,
  which is the thing this rule forbids — and a default that happens to match
  today's deployment is the worst case of all, because it is correct right up to
  the moment it quietly is not.
- **The configuration travels in the result** (§5.2), so a run is
  reconstructible from its own output.
- **Ergonomics are solved by a config FILE, not by defaults.** The rule makes a
  full invocation verbose; the answer is a small run-configuration file kept
  beside the document, not a set of values the tool supplies on the caller's
  behalf.

The rule does NOT make the replay agree with the daemon. Where the daemon
consults something a replay cannot have — resting order legs, an amend failure
history, a broker's own order type — the difference is REPORTED as a named
divergence (§5.2) rather than closed by a configuration field. A knob cannot
supply a missing fact; it can only hide that the fact is missing.

---

## 3. Architecture

```
apps/alphalens-broker-contract/          shared leaf, dependencies = []
  broker_contract/
    trade_intent/{schema, codec, validate}
    exit_geometry/{levels, policy, registry}
    sizing, fx, constants
    stop_decision.py                     NEW

apps/intent-replay/                      NEW leaf, depends only on the contract
  intent_replay/
    bars.py          price-input contract          ENGINE
    config.py        the stated run configuration   ENGINE
    classification.py the path classes of 4.3.1     ENGINE
    refusal.py       the one refusal builder         ENGINE
    interpreter.py   document -> pending orders     ENGINE
    walk.py          bar walk                       ENGINE
    trace.py         event trace                    ENGINE
    measures.py      measures with units            ENGINE
    envelope.py      versioned result envelope      ENGINE
    door.py          the five gates of 4.3 / 4.3.1  ADAPTER
    cli.py           one-shot CLI                   ADAPTER

apps/alphalens-pipeline/                 one possible client
```

The package is named for its INPUT, not for one of the policies it can replay.
An earlier draft called it `bracket-replay`, which would have stretched
"bracket" over documents that declare a trailing stop and no bracket at all —
the same mislabelling as the closed #1160.

### 3.1 Dependency rules

**The rule is per MODULE, not per distribution.** A Python distribution declares
its dependencies as a whole — there is no per-file declaration — so "this package
is dependency-free" cannot be the rule here. Gate 1 of §4.3 validates the
published JSON Schema, and the validator is third-party. What CAN be a rule is
which modules may import it.

| from | to | allowed |
|---|---|---|
| `broker_contract`, any module | anything third-party | no — stdlib only, `dependencies = []` |
| `intent_replay`, any module | `broker_contract` | yes |
| `intent_replay` ENGINE modules | anything third-party | **no** — stdlib and `broker_contract` only |
| `intent_replay` ADAPTER modules (`door`, `cli`) | `jsonschema` | yes, and nothing else third-party |
| `intent_replay`, any module | `alphalens_pipeline`, `alphalens_research`, pandas | never |
| `alphalens_pipeline` | `intent_replay` | yes |

`jsonschema` is the distribution's ONE third-party dependency and it exists for
one purpose: gate 1. Note what does NOT cross the boundary — the schema TEXT is
not third-party. `generate_schema` lives in
`broker_contract/trade_intent/json_schema.py`, inside the dependency-free leaf. So
the contract still produces the schema and only the validation of it needs a
library, which is exactly the split the arming door already has: the schema is
generated in the contract and validated in `alphalens_cli`.

**The engine is where the purity claim lives, and it is the half that matters.**
The measurement modules can be lifted into a standalone package carrying no
dependency but the contract, which is the property §3's placement argument rests
on. `door` and `cli` are the adapter edge, and an adapter is allowed a library —
the same doctrine the broker failure codes follow, where the adapter reports and
the contract does not decide.

**One barrier, not two.** An earlier revision of this section claimed two
independent barriers and named `dependencies = []` as the first. That was wrong
for this package. `dependencies = []` is `broker_contract`'s barrier;
`intent_replay` necessarily declares at least the contract, and now `jsonschema`
as well. So the AST gate is the ONLY barrier here, modelled on the existing
`tests/test_module_dependencies.py`, and it carries the whole rule including the
per-module split — which the existing gate's rule schema does not express today,
so it grows a rule KIND rather than a row.

A gate that is the only barrier needs a positive control: a fixture that MUST be
flagged, asserted to be flagged, in the same test file. Every
`test_no_raw_<vendor>_http.py` in this repo carries one for this reason. Without
it the gate silently rots to empty the first time the package is reorganised, and
the one barrier becomes none.

### 3.2 Why `stop_decision` belongs to the contract

The policy lives in the contract, but the guards that decide whether a policy
applies live in the daemon. `BreakevenTrailPolicy.decide_reanchor` is in
`broker_contract`; "a single resting standalone stop", the ratchet against the
trail history, and anchoring the clamp on the live price rather than on the
average fill are all in `position_manager`.

A replay that calls the policy alone does not reproduce the daemon. It
reproduces an idealised version without the guards — which is the defect class
this design exists to remove.

So the decision — position state plus policy plus market view, in; a new stop
level or `None`, out — is extracted into the contract, the leaf both sides
consume after the split. It returns a price, never a broker action and never a
journal write; those stay in the daemon.

The minimal view it needs, read off `_maybe_trail` and `_maybe_reanchor` field
by field:

| field | type | why |
|---|---|---|
| `avg_price` | float | the anchor, and the 1R numerator |
| `peak` | float \| None | high-water mark; a trail without one is a feed veto |
| `last_price` | float \| None | the trail clamp anchors here, not on `avg_price` |
| `plan_stop` | float | the never-below floor and the 1R denominator |
| `reaction` | primitive \| None | what the document declared |
| `has_sole_standalone_stop` | bool | the PREDICATE's result, never the order legs |
| `amend_in_backoff` | bool | same — a result, not the failure history |
| `last_trailed_level` | float \| None | the ratchet floor |
| `already_reanchored` | bool | the re-anchor arm's idempotence latch, as the PREDICATE's result: a confirmed re-anchor already fired for this average fill (`reanchored_by_uic`, compared by the daemon with its own tolerance); a replay knows this from its own trace |

Nothing here is broker-order-shaped: the three state questions cross as
booleans. If the extraction turns out to need more than this, that is a signal
the boundary is wrong and the design stops rather than widening the contract.
The ninth row was not in the first revision of this table, although the table
claimed to have been read off both arms: the review of the PR 2 plan
(2026-09-25) found `_maybe_reanchor` reading the latch, and the owner decided
it crosses as a boolean like the two guards above it, with the daemon's
`1e-6` blend tolerance staying in the daemon the way the backoff TTL does.
The daemon's latch is journal-lifetime (a marker from an earlier position on
the same instrument suppresses a later re-anchor); a replay does not model
that, and reports it as a divergence (section 5.2).

**What the replay passes for those two booleans, and why it is not a choice.** An
earlier revision listed this as an open decision, on the grounds that `False`
means the trail never fires and `True` means it always does. That framing was
wrong. `_maybe_trail` (`position_manager.py:782`) gates the trailing arm on nine
conditions, and seven are facts of the document or of the tape: the declared
policy (`resolve_declared_policy(plan.reaction).trails`), a finite positive
average fill, the policy's own activation threshold, the never-below-floor clamp
against the declared stop, a usable peak, a usable last price, and the ratchet
against the trail history. A replay has all seven. The remaining two are neither
policy nor price:

| daemon guard | what it asks | in a replay |
|---|---|---|
| `_sole_standalone_stop(legs)` | is exactly one clean standalone stop resting at the broker | there are no order legs at all |
| `uic in view.amend_recently_failed` | did a previous amend get refused | there is no broker to refuse one |

The replay passes the values that mean "no broker obstacle", and that is the only
coherent reading: a guard about resting orders cannot be evaluated where there
are none. The document decides whether the stop trails, which is what #1236
intended.

This is NOT the same as reproducing the daemon. The daemon does decline to trail
for reasons no document mentions — an OCO shape, an amend inside the backoff
window. So the replay trails at least as often as the daemon would, which
flatters the result. That belongs in the output as a named divergence
(`daemon_trail_guards`, §5.2), not in the configuration: it is a fact the replay
lacks, and a knob would let a caller pretend to supply it.

### 3.3 What the document does not say

Four execution facts are not fully stated by the wire document today. An earlier
revision of this section listed them as one class. They are four different
things, and the differences decide how the replay must treat each.

| fact | wire form | what it does today | consequence for the replay |
|---|---|---|---|
| entry trailing | **partial**: `EntryTierSpec.entry_mode` decides which tiers reach the machine and `spec.disaster_stop` is required to arm it, but the trail distance is not in the document | **places a native trailing order at the broker**, and on the deployed configuration it is the path every armed pick takes | MODELLED as of 2026-09-25, with the distance stated in the configuration (§2.1). The model is of the BROKER's order type, not of our code — a new risk, §8 |
| 52-week ceiling on a take-profit | **has one**: `ReanchorOnFill.ceiling_price`, modelled by the codec and refused at the door | no consumer on the live broker path; the only code that computes it reads a brief column | the one fact of the four that becomes a document fact by LIFTING a refusal rather than by designing a field (§4.3.1) |
| position time stop | none, by decision | no live consumer since ADR 0012 removed the paper-trade harness; `TIME_STOP_DAYS = 42` is read only by the `/edge` replay | a measurement convention of another instrument, not an execution rule — a run that applies it is not replaying the document |
| take-profit resting at the broker as an OCO pair | none | gated by `ALPHALENS_BROKER_OCO_ENABLED`, default off; **retired on SIM by decision** (position-attached exits do not work on a netting account) and unset on LIVE | changes WHO resolves the exit, which this design cannot yet express (below) |

Three things the table cannot carry:

**"The inert value" means something different in every row, which is why none of
them is a default.** For the OCO pair inert means a working capability that a
deployment decision retired. For the time stop it means a number belonging to a
different tool. For entry trailing it means switching off the path the money
currently takes. A configuration presenting all four as equivalent switches
invites a reader to treat a default run as neutral, and for entry trailing it is
not — which is the concrete case behind the rule in §2.1 that a run STATES each of
these or is refused. Stating `oco: false` costs a line and says who decided it;
inheriting it says nothing.

**All four feed §4.4, not only one.** Each adds or moves a level, so each
changes how often the tie convention has to decide and therefore what
`ambiguous_bars` reports. Entry trailing is the hardest: its trigger is a
running low that ratchets down, plus a distance, so whether it fires inside a
bar depends on whether the low came before the retrace — which OHLC cannot say.
That is a third row of §4.4's table, not an exception to it, and §4.4 already
anticipates it.

**The OCO row is a modelling gap, not a switch.** A pair resting at the broker
resolves on a touch; an engine resolves at its next observation. This document
does not state what that observation is relative to a bar, so at the time this
row was written there was nothing here to configure. §8.1 is where that
belongs, and since 2026-09-25 it carries the decision: `oco` is stated, and only
`false` is accepted in v1 — the switch exists so that the run says who decided
it, and the model does not.

The replay takes these as an explicit run configuration, separate from the
document, and the trace records which were active. Per §2.1 none of them carries
an inherited default: a run states each, or it is refused. The earlier revision
of this paragraph had them "each defaulting to the inert value", which for entry
trailing meant switching off the path the money takes and presenting that as the
neutral run — the exact reading the first bullet above warns against.

---

## 4. Data flow and the price contract

```python
@dataclass(frozen=True)
class Bar:
    t: int      # epoch milliseconds, UTC
    open: float
    high: float
    low: float
    close: float
```

No volume: queue position is not modelled, so it would be a field nobody reads.

### 4.1 The leaf knows no calendar

The replay knows nothing about sessions, exchanges or trading hours. It walks
exactly the bars it is given.

This is required, not tidy. Calendars mean `exchange_calendars` plus pandas, and
the split blueprint lists `paper.calendar` as edge **E1** — one of the three
residual couplings blocking the split — precisely because it drags those two.
`dependencies = []` rules it out.

One consequence: `spec.order_ttl_days` counts TRADING sessions, which the leaf
cannot resolve. The client converts it to an absolute cutoff timestamp and
passes it in the run configuration. The existing replay already does exactly
this with its `entry_expiry_ms`.

Passing the timestamp alone is not enough, for two reasons. The first is that
the result then records a number without the rule that produced it, which §5.2
forbids for the facts of §3.3 and should forbid here for the same reason. The
second is worse: the running deployments do not agree on the rule, and one of
them does not read the field at all. §8.1 recorded that as an open decision until
2026-09-25. So the run configuration carries the deadline with its provenance, in
the shape §5.1 already uses for the R denominator, and `spec.order_ttl_days` is a
TRANSLATED path in the sense of §4.3.1 rather than an unread one.

**The replay does not choose among the four live rules.** Decided 2026-09-25: the
deadline is a REQUIRED configuration value, and the caller states which rule
produced it inside `formula`. Picking one here — even the deployed one — would be
the replay inheriting a deployment fact, which §2.1 forbids, and it would make
every result depend on a choice its reader cannot see. A run that omits the
deadline is refused with `config_incomplete`, not walked to the last bar. A run
that states `null` for it is refused too (`config_invalid`, decided 2026-09-25
with PR 3): every decoded document carries `spec.order_ttl_days` — the codec
defaults it, and `0` is a legal sentinel — so a null would translate a present
path into nothing, with no `formula` a reader could check. A run that wants no
deadline states one past its last bar and says so in `formula`.

The same reasoning fixes the day-1 anchor. `meta.source` decides whether
`meta.trade_date` is itself day 1 or the session before day 1, and resolving "the
next session" needs the calendar this leaf does not have. So the client resolves
it and states `walk_start` in the same provenance shape, and both paths are
TRANSLATED (§4.3.1).

### 4.2 Flow

```
JSON -> contract codec -> TradeIntent -> door gates
                                             |
bars + run config -> interpreter -> pending orders
                                             |
                                     bar walk (stop_decision per bar)
                                             |
                          trace -> measures -> envelope
```

Decoding uses the contract's own codec, not a second parser.

### 4.3 The replay refuses what the door refuses

Four gates run before anything is computed, in this order:

1. the published input JSON Schema, on the wire;
2. the codec;
3. **decode and re-render, refusing any key that does not survive the round
   trip**;
4. `validate_intent`.

Step 3 is not optional and an earlier draft of this document omitted it. The
codec DROPS keys it does not model with only a warning, so a document carrying
`limit_pirce` passes every other gate and replays at a price its author never
wrote. It is the same gate, for the same reason, that stops such a document
arming. (Read `limit_pirce` as an ADDED key beside `limit_price`: that is what
reaches gate 3. A REPLACEMENT fails gate 1, because `limit_price` is required.)

**Two wire checks copied from the door, and one deliberately not.** Added
2026-09-25 by PR 4, after running the input schema over the three templates:
the published input shape has no `additionalProperties: false`, so a supplied
`intent_id`, `meta.armed_ts` or `r_multiple`, a `meta.trade_date` that is not a
date, and a stated `meta.schema_version` of any value all pass the four gates
above. The replay therefore runs the door's derived-field refusal
(`derived_field_supplied`; it refuses the PRESENCE of a field the adapter fills
and reads no value, so it interprets nothing §4.3.1 classes out of scope) and
the door's date check (`trade_date_malformed`, with the stated date normalised
to `YYYY-MM-DD` as the door normalises it, so a spelling that parses but is not
canonical is `key_discarded` at gate 3, the door's own reason). It does NOT
read `meta.schema_version`: §4.3.1 classes both version paths out of scope
because the codec and `validate_intent` are version-blind on purpose, and a
later-version document carrying something this contract cannot model is
refused by gate 3 instead; a test pins that a stated `"4"` is admitted.

An incoherent document gets the same refusal code it would get at arming, not a
number. This is the point of the design, not caution: backtesting a document
that could not be armed is once again an instrument measuring a policy the
system will not execute.

The replay does **not** check venue, pick key or generation. Those are queue and
deployment concerns, not policy — which is why §4.3.1 has to classify them as
out of scope rather than leave them unread.

### 4.3.1 A fifth gate the door does not need

The four gates above are the door's. They are not enough here, because the door
and the replay finish in different places. The door's job ends at "a daemon that
understands this document will execute it"; the replay must understand it
ITSELF. So it needs one gate the door has no use for.

**The replay refuses a document it did not CLASSIFY.** An earlier revision of
this section asked a different question — "did the interpreter read this path?"
— and that version does not work. It is recorded here rather than deleted,
because the way it fails is the reason for the shape that replaces it.

Every path of the published input schema belongs to exactly one of three
classes, and the classification is part of THIS document, not of a caller's
input:

- **interpreted** — the interpreter reads it and its value changes what the
  replay does: an order it places, a level it moves, a bar at which something
  fires, or a refusal it raises. Copying a value into the result envelope is not
  interpretation; an echo changes nothing.
- **translated** — the replay cannot resolve it, because resolving it needs a
  calendar, a fee card or a fill. The client resolves it and passes the result
  in the run configuration, which carries the rule as well as the value (§4.1,
  §5.2). The set is closed and listed here.
- **out of scope** — a queue or deployment concern, an identity, or a label
  the envelope may echo but nothing reads; the replay deliberately ignores it.
  Today: `meta.generation`, the venue and pick-key checks §4.3 already declines,
  and the rung and tranche labels. The label clause was added on 2026-09-25 with
  PR 3: the table already carried `instrument.ticker`, `spec.size.currency` and
  the two `schema_version` rows, none of which the first sentence admitted.

*Interpreted* is not listed, because the interpreter proves that class at
runtime by actually reading the path — with one exception, two paths that gate
4 proves by REFUSING rather than by a read (`spec.side`, `ceiling_price`; the
optional-path table below), which `validate_intent` cannot report and which
are therefore written down in `intent_replay.classification.REFUSED_BY_DOOR`,
each pinned by a test that runs the refusal. The other two classes ARE listed,
here, and this table is the whole of them:

| path | class | why |
|---|---|---|
| `instrument.ticker` | out of scope | identity. The walk is over the bars it is handed; nothing resolves a symbol |
| `instrument.mic` | translated (calendar) + out of scope (fee card, settlement currency) | the calendar reaches the replay through `entry_deadline` (§4.1); costs are a non-goal per §2 and `spec.size` is already in the account currency |
| `spec.size.currency` | out of scope | it labels the unit of the cash answer; it changes nothing the replay does, and §4.3.1 does not count a label as interpretation |
| `spec.order_ttl_days` | translated | sessions the leaf cannot count (§4.1) |
| `meta.generation`, `meta.armed_ts`, `intent_id` | out of scope | queue and identity concerns, the same ones §4.3 already declines |
| `meta.schema_version`, `spec.schema_version` | out of scope | the door is the only gate that reads a version; the codec and `validate_intent` stay version-blind on purpose, and so does this |
| `meta.source`, `meta.trade_date` | translated | together they fix the first session of the walk, and "the next session" needs a calendar (§4.1). The client resolves it and states `walk_start` |
| `spec.entry_tiers[].entry_mode` | interpreted | `pullback` is a resting rung the walk can test; `immediate` is refused with `entry_mode_unsupported` (§5.4), and a refusal is interpretation by the definition above |
| `spec.entry_tiers[].tag`, `spec.tp_tranches[].tag` | out of scope | labels with no sizing semantics (the schema's own words); an echo in the trace is not a read |
| `account_id` | out of scope | a reserved tenant dimension with one value; a deployment concern like `meta.generation` |

The last two rows were added on 2026-09-25 by PR 3 (#1574), the third time the
coverage check below fired: enumerating EVERY path of the input schema, not only
the required ones, left exactly those three without a class, and a pick copied
with the README's `jq` recipe carries all of them.

**Completeness: the sixteen REQUIRED paths.** The table above lists only the two
classes that need listing, which leaves a reader unable to check that every
required path is accounted for — and before the interpreter exists there is no
runtime to prove `interpreted` with. So the design also records where each
required path lands. This is a coverage record for review, NOT the gate's source
of truth: the gate still proves `interpreted` by actually reading, exactly as
argued above, and this table going stale cannot make the gate wrong.

| required path | class | note |
|---|---|---|
| `instrument` | container | |
| `instrument.ticker` | out of scope | above |
| `instrument.mic` | translated + out of scope | above |
| `meta` | container | |
| `meta.source` | translated | above, with `meta.trade_date` (itself required only on a legacy `"brief"` document) |
| `spec` | container | |
| `spec.size` | container | |
| `spec.size.currency` | out of scope | above |
| `spec.size.notional_acct` | interpreted | the budget the entry ladder spends |
| `spec.disaster_stop` | interpreted | the level that rests, and therefore the R denominator (§5.1), and the condition under which an entry trail arms (§3.3) |
| `spec.entry_tiers` | container | |
| `spec.entry_tiers[].limit_price` | interpreted | the rung the walk tests, or the operator's cap on an `immediate` tranche — which v1 refuses (§5.4) |
| `spec.entry_tiers[].alloc_pct` | interpreted | the share of the budget at that rung |
| `spec.tp_tranches` | container | |
| `spec.tp_tranches[].price` | interpreted | the target level |
| `spec.tp_tranches[].tranche_pct` | interpreted | the share of the position exited there |

The count and the list come from the schema, not from reading this document:
enumerate `required` recursively through `$defs` — descending only through
properties a `required` list names, arrays as `[]` — and compare against the
tables. Doing that on 2026-09-25 is what produced the six `interpreted` rows
above — the first pass of this section had none of them, and a check that cannot
produce a missing row has tested nothing. PR 3 (#1574) promoted the check to a
test that also enumerates EVERY path of the schema and holds the three tables of
this section equal to the module's listed classes in both directions; the
optional paths land here:

| optional path | class | note |
|---|---|---|
| `exit` | container | `null` means the stop is never moved (§6.3) |
| `exit.initial_levels` | container | |
| `exit.initial_levels.stop` | interpreted | the stop the document declares; the live-exit ladder's stop reference, not the level that rests (§5.1) |
| `exit.initial_levels.tp` | interpreted | the level placed, and the whole take-profit ladder for a document that supplies it: one tranche of 100% here (§5.1) |
| `exit.reaction_plan` | container | |
| `exit.reaction_plan[].kind` | interpreted | routes the stop decision (§3.2) |
| `exit.reaction_plan[].k_atr`, `exit.reaction_plan[].atr` | interpreted | the re-anchor arm's inputs (§3.2) |
| `exit.reaction_plan[].arm_trigger_r`, `exit.reaction_plan[].trail_frac` | interpreted | the trail arm's inputs (§3.2) |
| `exit.reaction_plan[].ceiling_price` | interpreted (gate 4 refusal: `ceiling_price_unsupported`) | the instance already queued, below; a lifted refusal makes this row false and the reachability test red |
| `spec.side` | interpreted (gate 4 refusal: `side_not_long`) | pinned to `long`; a replay that skipped `validate_intent` would walk a short document as a long one |
| `spec.entry_tiers[].entry_mode` | interpreted | above |
| `spec.order_ttl_days`, `meta.trade_date` | translated | above |
| `spec.schema_version`, `meta.schema_version`, `meta.generation` | out of scope | above |
| `spec.entry_tiers[].tag`, `spec.tp_tranches[].tag`, `account_id` | out of scope | above |

The two gate-4 rows are interpreted paths a reader can miss, because the proof
is a refusal rather than a read: raising a refusal is interpretation by the
definition above, but `validate_intent` reports nothing about the paths it
examined, so the interpreter's read set cannot carry them. They are listed in
`REFUSED_BY_DOOR` for that reason only, and the list is pinned by a test that
builds each offending document and asserts the named reason is RAISED — a test on
membership of the reason in the vocabulary would stay green after the refusal
was lifted.

A path that is neither read by the interpreter nor in this table is refused, and
the refusal names it (`path_unclassified`, §5.4). Adding a row is an edit to this
section, so the set of things this tool quietly does not honour cannot grow
without someone writing it down.

Until 2026-09-25 `meta.source` and `meta.trade_date` deliberately carried NO
class, so the gate refused every document that could exist. That was the gate
working rather than failing: the day-1 anchor is a full session of difference on
every hand-authored pick, and leaving it unstated would have meant choosing it at
the keyboard. It is now decided, and the two rows above are the decision.

**Why not "did the interpreter read it".** Two required paths settle it.
`instrument.mic` is read by nothing: the envelope of §5 echoes it, while the
MIC's actual semantics — which calendar the sessions come from, which fee card
applies, which currency the position settles in — are honoured nowhere in this
design. `meta.source` is read by nothing either, and §8.1 shows it decides where
the walk begins. Under the read question both are unread, so the gate refuses
every document that can exist, and §6.3's positive control — a document whose
every path is read — cannot be constructed at all. Under the classify question
`instrument.mic` is *translated* for its calendar, through the deadline of §4.1,
and its fee card and settlement currency are *out of scope* because §2 makes
cost a non-goal and `spec.size` is already in the account currency. Both halves
had to be written down to get there, which is exactly the work the gate exists
to force.

This is the door's own round-trip gate applied one level deeper. Step 3 above
asks "did the CODEC keep every key?". It cannot ask "did anything READ it",
because at the door nothing has yet. Once the contract grows a field, the codec
models it, the round trip passes, `validate_intent` says nothing — and an
interpreter written before that field existed silently ignores it and still
reports a number.

Two rules follow, and without them the gate is decoration:

- **The replay never resolves a policy through the degrading path.**
  `resolve_declared_policy` returns the INERT policy for a primitive it cannot
  honour rather than raising, because in the daemon it is reached from a journal
  stamp inside the protection pass where a raise would starve the never-naked
  backstop. That is correct in the daemon and poison here: it turns "I do not implement
  this" into "the stop never moved", which is a plausible number. The replay
  either uses its own resolver or a strict mode of that one.
- **Adding a reaction primitive is a decision, not an edit.** `validate_intent`
  refuses an unhonourable primitive today, but the check is an allowlist on
  CLASS IDENTITY (`isinstance(p, ReanchorOnFill | TrailingStop)`), not on whether
  the primitive carries what its own execution needs. The two coincide today by
  luck: both honoured primitives are self-sufficient. `ReanchorOnFill` carries an
  absolute `atr` snapshot precisely so no second fetch is needed, and
  `TrailingStop` needs only `arm_trigger_r`, `trail_frac` and prices the walk
  already has. Whoever adds a third class to that line must answer the question
  the line does not ask.

**The invariant this protects, stated once:** a document, its bars AND the
declared run configuration are together sufficient to replay every reaction the
document declares.

The document alone is not, and an earlier revision claimed it was. §8.1 names
two reactions whose inputs come from neither the document nor the tape: the
order-state predicates a trail consults, and the cost gate a take-profit tranche
must clear. Until those are decided, sufficiency is a goal of this design rather
than a property of it, and the two rules above are what keep the gap visible.

#### The instance already queued

This is not hypothetical. `ReanchorOnFill.ceiling_price` is a field in the
published wire shape today, modelled by the codec, refused by the door
(`ceiling_price_unsupported`) and marked `LEGACY(reanchor_ceiling_price)`.
Meanwhile §3.3 treats the 52-week ceiling as run configuration defaulting to
inert.

The day that refusal is lifted, a document carrying a ceiling passes every gate
while the replay reads its own `config.ceiling_price` — `null` — and computes a
take-profit without the cap the document asked for. The number comes out, looks
right, and describes a different policy. The classification gate is what turns
that into a refusal: a lifted refusal makes `ceiling_price` a path no class
covers, and no class covers it until someone edits §4.3.1 to say which.

### 4.4 Intra-bar ties: pessimistic, fixed, and counted

A minute bar carries an open, a high, a low and a close. It does NOT carry the
ORDER in which the high and the low occurred. When one bar touches two levels
whose outcomes differ, the data cannot say which happened first, and the replay
must assume.

**The convention: whichever resolution is worse for the position wins.** It is
a named convention, not a configuration field. Three situations fall under it,
and the first two are already resolved this way by the existing replay:

| one bar touches | resolution | why this is the worse one |
|---|---|---|
| the stop and a take-profit | the stop | +1R becomes −1R |
| a lower entry rung and the stop | fill first, then stop out | without the fill there would be no loss |
| a new low and the trailing entry trigger | the trigger fires on the PRE-BAR trough | the trough had not ratcheted down yet, so the buy pays the higher trigger |

The third row arrived on 2026-09-25 with the decision to model entry trailing
(§3.3) — which is what the earlier text anticipated in saying the convention
covers the class rather than the cases. It is also the row that fires most often.
The trigger is a running low plus a distance, so a document entering on a trail
rather than at fixed rungs meets the convention on every bar that makes a new low
and then retraces. For such a document `ambiguous_bars` is not a footnote; it is
the number that says how much of the answer came from the rule.

**No optimistic mode is offered.** A `tp_first` switch would be a knob whose
only use is making a result look better, and in a research tool such a knob is
eventually turned and then forgotten.

The cost of the convention is not uniform across policies: a tight stop meets
ambiguous bars far more often than a 1.5 x ATR bracket, so the assumption
enters any comparison BETWEEN policies, not just the level of one. That is why
the summary carries `ambiguous_bars` — the count of bars where the convention
actually had to decide something. Zero means the assumption carried nothing and
the result is hard data. A large count means much of the result comes from the
rule rather than from the tape, and the reader is entitled to know which.

### 4.5 Bars out of order

The replay REFUSES input whose `t` is not strictly increasing, and refuses
duplicates. This deviates from the existing replay, which sorts silently. The
deviation is deliberate: for a research tool, quietly reordering a caller's
input produces a wrong answer that leaves no trace.

### 4.6 Trace

An ordered list of events, each carrying `t`, a kind, and kind-specific fields:

`entry_filled` · `entry_expired` · `stop_placed` · `stop_moved` · `tp_fired` ·
`position_closed` · `horizon_open`

`stop_moved` carries the reason (`trail`, `reanchor-on-fill`,
`tp-tranche-resize`) and the level before and after.

---

## 5. Result envelope

Cash is the primary measure and R is secondary, always carrying its
denominator. The VOID pre-registration reached the same conclusion while
studying this exact problem: "The fix is not a better summary statistic on the R
scale. R itself is the problem" — and replaced R with net cash.

```json
{
  "schema": "intent_replay.result/v1",
  "intent_id": "REPLAY",
  "instrument": {"ticker": "KO", "mic": "XNYS"},
  "window": {"from_t": 1790170200000, "to_t": 1791230400000, "bars": 3510},
  "config": {
    "entry_deadline": {
      "kind": "order_ttl_sessions",
      "value": 1791230400000,
      "unit": "epoch_ms_utc",
      "source": "spec.order_ttl_days",
      "formula": "session_close_utc(advance_trading_sessions(2026-09-24, 7, XNYS))"
    },
    "walk_start": {
      "kind": "day1_session_open",
      "value": 1790170200000,
      "unit": "epoch_ms_utc",
      "source": "meta.source + meta.trade_date",
      "formula": "session_open_utc(2026-09-23); source=manual counts trade_date itself as day 1"
    },
    "entry_trail_bps": 50,
    "ceiling_price": null,
    "time_stop_t": null,
    "oco": false,
    "costs": {
      "commission_rate":       {"value": 0.0008, "unit": "fraction"},
      "min_commission":        {"value": 1.0,    "unit": "USD"},
      "min_commission_applies": true,
      "fx_applies":             false,
      "exit_edge_min_bps":     {"value": 5.0,    "unit": "bps"}
    }
  },
  "divergences": ["daemon_trail_guards", "native_entry_trail_is_a_broker_model"],
  "outcome": "closed_tp",
  "summary": {
    "filled_fraction": 0.6,
    "ambiguous_bars": 3,
    "notional_spent":   {"value": 900.0, "unit": "EUR"},
    "avg_entry_price":  {"value": 67.83, "unit": "USD"},
    "pnl_cash":         {"value": 41.20, "unit": "EUR"},
    "pnl_pct_of_spent": {"value": 4.58,  "unit": "percent"},
    "r_multiple": {
      "value": 0.72,
      "unit": "R",
      "denominator": {
        "kind": "placed_stop",
        "value": 1.63,
        "unit": "USD",
        "source": "spec.disaster_stop",
        "formula": "avg_entry_price - placed_stop"
      }
    },
    "mfe": {"value": 1.10,  "unit": "R"},
    "mae": {"value": -0.34, "unit": "R"}
  },
  "trace": []
}
```

### 5.1 The denominator is the stop that was actually placed

`denominator.kind` is always `placed_stop`, and on both deployments the level
actually placed is `spec.disaster_stop` — in every document, including one that
supplies `exit.initial_levels`. `denominator.source` names it.

An earlier revision of this section made the denominator
`exit.initial_levels.stop` when the document supplied one. Running both
placement paths on 2026-09-26, during PR 5, refuted the sentence that choice
rested on (#1597).

What rests on the book comes from the `planned` journal line:
`_fold_planned_exits` turns it into `PlannedExit.stop_price`, `PlaceStop` places
that, and `position_manager._maybe_trail` and `_maybe_reanchor` both read it as
the never-below floor and as the 1R denominator (§3.2, `plan_stop`). On the
entry-trail path — the deployed one — that line is written by
`control_loop._journal_entry_planned_disaster`, which reads
`record["disaster_stop"]` (`control_loop.py:3012,3026`), off a `watch_open` line
carrying `float(plan.disaster_stop)` (`control_loop.py:2148`). The
geometry-aware writer `_planned_exit_levels` exists only on the classic bracket
path, and a document supplying its own levels reaches it on neither deployment:
with `ALPHALENS_BROKER_ENTRY_TRAIL_BPS` set, which is the versioned drop-in on
SIM and on LIVE, the pick is intercepted into the entry-trail watch; with the
trail off, `_refuse_geometry_without_trail` (`control_loop.py:9740`) refuses the
pick rather than placing it. What `exit.initial_levels.stop` does reach is the
`tranche_plan` line, as the live-exit ladder's stop reference, so the replay
reads it — it simply never rests.

The earlier revision argued the other way: for a document supplying its own
levels, `spec.disaster_stop` is a number nothing protects the position with, so
an R measured against it names a risk nobody took. The rule is right and its
premise was backwards. Applied to what runs, the same rule selects the disaster
stop, because `exit.initial_levels.stop` is the level nothing places today.
Following this section as it was written would have measured every hand-authored
pick against a level resting on neither deployment, which is what the rule
exists to prevent.

**Which take-profit ladder fires.** One function decides both halves, and it
decides this half the other way. When a document supplies `initial_levels`,
`_geometry_tranche_ladder` returns ONE tranche of 100% at `initial_levels.tp`,
and `_journal_tranche_plan_core` journals that instead of `spec.tp_tranches`, so
the author's declared ladder never fires. The replay fires what the daemon
fires: one tranche of 100% at `exit.initial_levels.tp` for such a document, and
`spec.tp_tranches` for a document that supplies no levels. Both paths are read
(§4.3.1), and the trace names the ladder the run used.

Comparability is stronger than the earlier shape promised: the KIND is one
concept — the distance to the stop actually resting — and now one path supplies
it in every document. `denominator.source` stays in the envelope anyway. The
current `/edge` lenses carry two different denominators under one name, and a
reader of a v1 result should not have to find this section to learn where the
number came from.

### 5.2 The config block travels in the result

The four facts of §3.3 come back in the output, so a result cannot be read out
of the world that produced it. The absence of exactly this is why every
`atr_bracket_1p5` value stamped before 2026-08-24 describes a different policy
than a reader would assume, discoverable only from a memo.

The block carries three more keys, and none is decoration. `costs` belongs with
the four rather than with the measurement settings: the cost gate decides WHICH
take-profit tranches fire, so a costless run diverges in events and not only in
cash — which is why §2.1 makes it stated and required instead of letting it
default to none. `entry_deadline` and `walk_start` are the TRANSLATED paths of
§4.3.1, and they follow a rule the rest of the block does not need:

> A config value that TRANSLATES a document path carries the
> `kind`/`value`/`unit`/`source`/`formula` object of §5.1. A config value that is
> a plain run switch stays a bare scalar.

The anchor and the boundary rule live inside `formula` as resolved values rather
than as free-text sibling keys, because a reader can check a formula against the
numbers beside it and cannot check a label. There is no null form for "no
deadline" (§4.1): an earlier revision kept one from the days the block had inert
defaults, and PR 3 removed it. The rule exists because the alternative — a caller asserting "I
translated this path" with nothing able to check the assertion — is an echo with
a story attached, and §4.3.1 rules out echoes.

`ambiguous_bars` serves the same purpose for the one assumption that is NOT
configurable (§4.4).

**`divergences` is the list a knob would have hidden.** It names, per run, each
place where the replay is known to differ from the daemon for a reason no
configuration value can close, because the replay LACKS a fact rather than a
setting. Two entries exist at v1: `daemon_trail_guards` (§3.2 — the replay trails
at least as often as the daemon, because the two order-state guards cannot be
evaluated without orders) and `native_entry_trail_is_a_broker_model` (§8 — the
entry trail is a model of Saxo's server-side order type, which no local
implementation can be compared against). Both flatter the result, which is why
they are printed rather than footnoted. An entry is added by editing this section,
on the same reasoning as §4.3.1: a divergence that can appear without anyone
writing it down is a divergence nobody will find.

### 5.3 Formats

| format | when | stdout |
|---|---|---|
| `json` | one document | exactly one JSON value |
| `ndjson` | a stream | one object per line, ending in a `summary` event |

No human-rendered table in v1: it is a convenience, and `jq` exists. The
`--format` option still takes the name, so adding `human` later changes no
call site.

The trace is in the envelope for a single document and omitted for a stream
unless requested.

### 5.4 Errors

A faulty document gets the same code it would get at arming —
`intent_invalid` or `intent_malformed` with `details.reason` — because the
replay refuses what the door refuses, so it should refuse the same way. New
codes only for its own failures: `bars_unordered`, `bars_empty`,
`bars_invalid`, `window_too_short`, `path_unclassified` for the gate of §4.3.1 (carrying
`details.paths`), `config_incomplete` for a required configuration value the
caller did not state (§2.1, carrying `details.keys`), `config_invalid` for a
configuration value the caller stated but nothing can use (below, carrying
`details.keys`), `config_malformed` for a configuration FILE the CLI cannot
parse at all (below, added 2026-09-25 by PR 4, carrying `details.path`), and
`entry_mode_unsupported` for the `immediate` tranche v1 does not model
(§4.3.1, carrying `details.tiers`). `usage` is not in that list: it is the
invocation's own code, inherited from the arming door, and it never describes
a document or a block.

**`config_invalid` is a stated value nothing can use; `config_incomplete` stays
"not stated".** Added 2026-09-25 by PR 3, on the argument that gave `bars_invalid`
its own code: a complete configuration can still carry a value nothing can use,
and answering that caller with "you did not state this" — the published meaning
of `config_incomplete` and of its `details.keys` — sends them to the wrong place.
One code, a closed `details.reason` vocabulary: `unknown_key` (a key the block
does not model — the codec drops such a key with a warning, which is why the
door needs its fixed-point gate, and a misspelt `entry_trail_bp` must not switch
entry trailing off in a run whose author believes the distance was stated),
`wrong_type` (a stated `null` where none is allowed included), `numeric_not_finite`,
`not_positive`, `negative`, `unit_mismatch`, `empty_string` and `oco_unsupported`
(§8.1). Missing keys win: when keys are both missing and unusable, the run is
refused `config_incomplete` naming only the missing ones, and hears about values
on the next pass.

**`intent_malformed` is a CLI-owned code, and this tool's CLI owns its own copy.**
The broker's code registry is deliberately SPLIT: `broker_contract/failure.py`
holds the nine codes the contract owns, while `intent_malformed` is defined in
`alphalens_cli/commands/broker.py` because it names a CLI concept, and putting a
CLI concept in the shared package is the #1122 mistake. That constraint applies
here unchanged, so the sentence above must not be read as "the leaf returns
`intent_malformed`". The engine modules raise typed exceptions; `intent_replay.cli`
maps them to codes, and that is where `intent_malformed` is defined for this tool.
The leaf never names it. The two CLIs therefore agree on the STRING without
sharing a definition, which is what the split already accepts for the broker's own
commands.

This CLI's `intent_malformed` reason vocabulary is the door's minus the venue,
pick-key and generation reasons (§4.3) and the version reason (§4.3.1), plus the
parser's `not_json` and `duplicate_key`; the package README publishes the table.
**`config_malformed`**, added 2026-09-25 by PR 4, is the same shape for the
configuration FILE (`not_json`, `duplicate_key`, with `details.path`): a file that
is not one JSON object is a content failure and not `usage`, on the argument that
split `config_invalid` from `config_incomplete` — the wrong code sends the caller
to the wrong place, and `usage` says "fix the invocation". `usage` stays what it
is at the arming door: a bad option, or a file the invocation names that cannot
be read.

**`window_too_short` fires when the supplied bars do not COVER `walk_start`.**
Decided 2026-09-25, after the implementation plan found the code published here
with no trigger defined anywhere. One code, two reasons in `details.reason`, on
the project's rule of one code per failure mode with a closed reason vocabulary:

| `details.reason` | when | why a truncated walk would be wrong |
|---|---|---|
| `ends_before_walk_start` | the last bar precedes the stated `walk_start` | there is nothing to walk; a result would describe no session at all |
| `begins_after_walk_start` | the first bar follows it | the entry ladder was live over an interval the tape does not cover, and a walk starting at the first available bar would report those rungs as unfilled over a stretch it never saw |

The second reason is the one that matters. A replay that quietly started at the
first bar it had would produce a number, and the number would be about a
narrower window than the caller asked for — the same failure §4.5 refuses for
unordered input, for the same reason: in a research tool, adjusting the
caller's input silently produces a wrong answer that leaves no trace.

The check is a pure function of the bar sequence and ONE timestamp. It lives in
`bars`, takes the stated `walk_start` as an argument, and does not read the
configuration itself — so it can exist before the configuration model does.

**`bars_invalid` is a bar that cannot be compared.** Added 2026-09-25 when the
first implementation found a refusal the list above did not name: a bar whose
price is NaN or infinite. `json.loads` accepts a bare `NaN`, and a NaN answers
False to every ordering comparison, so a bar carrying one would pass every
check downstream and describe a fill that never happened. The value type
refuses it at construction, with `details.reason = numeric_not_finite` — the
word `validate_intent` already publishes for the same fact in a document — and
`details.field` naming the price. It is its own code rather than a reason under
`bars_unordered` because it is a different failure mode: the sequence may be
perfectly ordered and still carry a value nothing can compare.

`entry_mode_unsupported` is a refusal and not a warning on purpose. The daemon
buys an `immediate` tranche AT DRAIN, where `limit_price` is the operator's cap
rather than a pullback level, so a bar walk asking "did the low touch the limit"
replays it as a resting order that waits. That produces a number, and the number
is about a different order. That refusal needs its own code rather than borrowing
`key_discarded`: at the door `key_discarded` means the codec lost a key, which
is a different fact and would send a reader to the wrong place.

Errors go to stderr, stdout stays empty, the exit status is non-zero: `0` ok,
`2` usage, `130` interrupted with nothing written (added 2026-09-25 by PR 4: the
interpreter's own status for an interrupt, caught so that the last line of
stderr is never a traceback), `1` everything else. Suggestions are an `argv`
array.

---

## 6. Testing

### 6.1 The parity test

Step 1 leaves two implementations of the stop decision: the new one in the leaf
and the existing one in the daemon. Their agreement is a claim, so it is tested.

A Hypothesis property test draws a position state, a declared policy and a
market view, and requires `stop_decision` and the daemon's decision to produce
the same level or both `None`. The property suite already exists and loads its
profile at import (PR #1523), so this adds a module, not infrastructure.

Two conditions without which the test is decoration:

- **It must import the live daemon composition, not a copy.** A tripwire that
  points at a dead copy after a move is green and guards a corpse.
- **It must be retired in step 2.** Once the daemon calls the leaf, the test
  compares the leaf to itself. A test that can no longer fail has stopped
  testing; the PR that switches the daemon deletes it and puts
  behaviour-pinning golden cases in its place.

**What this test does NOT cover, stated so nobody infers otherwise.** It compares
the STOP decision, and only that. Three parts of the replay have no counterpart to
compare against:

| part | why parity cannot reach it |
|---|---|
| the native entry trail | our code stops at arming; the ratchet and the fire belong to the broker (§8). There is no local implementation, so there is nothing to disagree with |
| the two order-state guards | they are inapplicable rather than implemented differently (§3.2); a parity run would have to invent the very facts a replay lacks |
| the intra-bar tie convention | it resolves what the data cannot say (§4.4). Both answers are consistent with the bar, so no test distinguishes a right one |

Each is covered instead by a named divergence in the result (§5.2) or by
`ambiguous_bars`. That is weaker than a test and is the honest strength of the
claim.

### 6.2 Golden cases

Computed with the real functions during design, 2026-09-23:

| case | expectation |
|---|---|
| fill BETTER than the anchor, `reanchor_on_fill` declared | zero `stop_moved` — the clamp refuses |
| anchor 68.00, ATR 1.20, average fill 68.50 | one `stop_moved`, level 66.70 |
| trail, peak exactly `+0.5R` | one `stop_moved`, level `+0.300R` |
| trail, peak `+0.49R` | zero events — dark before activation |

### 6.3 Walk properties

- never sells more than was filled;
- the placed stop is monotone non-decreasing across the whole trace;
- the sign of `pnl_cash` agrees with the sign of `r_multiple`;
- a document with `exit: null` produces zero `stop_moved` events;
- bars that touch no entry produce `outcome: "no_fill"` and zero cash;
- `ambiguous_bars` is zero whenever no bar touches two levels of opposite
  outcome, and positive whenever one does;
- a document carrying a path §4.3.1 classifies in none of its three classes is
  REFUSED, and the refusal names that path — with a positive control, a document
  whose every path IS classified, so the gate cannot rot to "accepts
  everything". The published examples in `examples/manual-pick/` are that
  control, which is also what ties this property to §6.4: if the classes of
  §4.3.1 do not cover every required path of the input schema, every example
  refuses and both tests go red at once. Note what the control is run on: a
  COMPLETED example, per §6.4 — the published template plus a stated
  `meta.trade_date`. An uncompleted template refuses in the codec, which
  is a different failure that would make this property look satisfied for the
  wrong reason;
- two runs over the same input produce byte-identical output.

The last one matters more than it looks: a research tool that returns a
different number on a repeat invalidates every conclusion drawn from it.

### 6.4 Door agreement

A test pushes every example in `examples/manual-pick/` through the replay. What
the door accepts, the replay accepts, including the round-trip gate of §4.3 — with
two published exceptions, and one step that has to happen first. The first
exception and the step were found by running the codec over the three files on
2026-09-25, and are recorded here because the sentence above was written without
doing that; the second exception was decided on 2026-09-25 with PR 4 and is the
`meta.trade_date` row of the table below.

**The examples are TEMPLATES, not complete documents.** Their `meta` carries only
`source`, so all three fail the CODEC before any gate runs:
`IntentMeta.__init__() missing 2 required positional arguments: 'armed_ts' and
'trade_date'`. Completing a template is the door's job and the fill lives
pipeline-side (`intent_door.complete()`), which §3.1 forbids this package from
importing. So the adapter module completes them itself, and the classification of
§4.3.1 says how:

| field | class | how the adapter supplies it |
|---|---|---|
| `intent_id` | out of scope | a fixed sentinel. Out of scope means it changes nothing the replay does, so any value is as good as any other |
| `meta.armed_ts` | out of scope | the same |
| `meta.trade_date` | translated | NOT a sentinel, and not derived: STATED in the document, for every `source`, and normalised to `YYYY-MM-DD` as the door normalises it (decided 2026-09-25 with PR 4). The arming door fills a missing date from its clock and the venue's calendar (`session_not_closed`), and this leaf has neither (§4.1); deriving it from `walk_start` would be a calendar claim, and the UTC date of a session open is not the session's date for a venue that opens before 00:00 UTC. Absent is `intent_malformed` / `trade_date_required` — the door's reason for a legacy brief document, here for every document. A spelling that parses but is not canonical is `key_discarded`, as at the door |

This is deliberately not a `divergences` entry. §5.2 reserves that list for facts
the replay LACKS; an out-of-scope field is one the replay does not need, which is
the opposite case, and conflating them would make the divergence list mean two
things.

**The second exception is that date.** The arming door ACCEPTS a `manual`
template without `meta.trade_date` and fills it; the replay REFUSES it. The
door's fill is "the session that has not closed at the moment of arming", a
fact about the clock, and a replay has no moment of arming. So the test pushes
the templates through with the date stated, and asserts that a template as
published is refused with `trade_date_required`.

**The other exception.** `immediate-plus-pullback.json` declares `entry_mode:
"immediate"` on tier 0, and the door ACCEPTS it — `_ALLOWED_ENTRY_MODES`
(`validate.py:229`) admits the value, and only an unknown value raises
`entry_mode_unknown`. §5.4 requires the replay to REFUSE it with
`entry_mode_unsupported`. So this test asserts two different things: the two
pullback examples are accepted, and that one file is refused with exactly that
code, naming tier 0 in `details.tiers`.

Whoever meets the red here should not drop the file from the test or soften the
code to a warning. Either would quietly undo the §8.1 decision, and the decision
is the point.

### 6.5 CI

Tests live under the existing discovery root, as `broker_contract`'s tests
already do — one `unittest discover`, no second workflow run. They move with the
package at split time.

Two gates from the first PR: coverage at 80% and cognitive complexity S3776 at
most 15. The bar-walk loop is the natural candidate to exceed the second, so
splitting it into "match orders in this bar" and "advance state" is a design
decision, not a rescue after a red Sonar run.

TDD throughout: red before green, including for two-line fixes.

---

## 7. Staging

**Step 1 — the replay.** COPY `stop_decision` into the contract, build
`intent-replay`, wire the CLI. The daemon is **not touched**; the parity test
holds the two implementations together. Copy, not extract: step 1 deliberately
leaves two implementations standing (§6.1), and calling it an extraction is what
would make the safety argument below sound stronger than it is.

The path bookkeeping of §4.3.1 belongs to this step, not a later one. It is a
record the interpreter keeps while it reads, checked against the classification
this document publishes; retrofitting it into a finished interpreter means
revisiting every read site and trusting that none was missed, which is the same
check with none of the guarantee.

**Step 2 — the daemon, separately and later.** `position_manager` drops its own
copy and calls the leaf. Its own PR, its own review, with the step-1 parity test
as the net, then retired per §6.1.

The split exists because the blueprint says protection-critical code is not
touched in passing. Step 1 changes not one line the live daemon executes:
adding an uncalled module to a package the daemon imports executes nothing.

---

## 8. Risks and open questions

- **Entry trailing is modelled as of 2026-09-25, and the model is of a BROKER
  rather than of our code.** An earlier revision called it the last policy still
  selected by a deployment environment variable. That was wrong twice: the OCO
  pair is selected the same way, and entry trailing is not selected by the flag
  alone — `entry_mode` decides which tiers reach the machine and the flag also
  decides which arm gates run. The serious half was simpler. On both deployments
  the flag is set, so an eligible pick rests a native trailing order instead of
  the three limit entries, and the trail distance is nowhere in the document. A
  replay that ignored this would describe an entry ladder that did not happen.

  The decision was to model it, with the distance stated in the configuration
  (§2.1). Refusing such documents was the alternative, and it would have refused
  nearly every realistic hand-authored pick — the exact population this tool
  exists to study.

  **What the decision buys is a risk the rest of this list does not carry, which
  is why the bullet stays.** Once the trailing order rests, `TRAIL_ARMED` records
  that "the native Saxo trailing order is RESTING at the broker — the server owns
  the ratchet + fire", and the state is terminal for our engine
  (`entry_trail_watcher.py:146-148`). So what is being modelled is the VENDOR's
  order type: the trigger ratchets down with the running low, and the fire is a
  MARKET order rather than a limit — the `StopLimitPrice` ceiling does not bind,
  which a live probe established and a reading of our code would not have. Two
  consequences. The parity test of §6.1 cannot cover this path at all, because
  there is no local implementation to compare against: our code stops at arming.
  And the model rests on a vendor's documented behaviour plus one probe, which is
  the weakest evidence anywhere in this design. The run therefore reports it as
  the `native_entry_trail_is_a_broker_model` divergence (§5.2) instead of letting
  a reader assume a test covers it.
- **This design stated something about the live path that running it refuted,
  and the same run left a live-side question open.** §5.1 chose its R
  denominator on the sentence "`_geometry_tranche_ladder` journals
  `initial_levels.stop` as the plan stop, and the never-naked cover places
  THAT". Driving both placement paths during PR 5 showed the opposite: the level
  that rests is `spec.disaster_stop`, and a document's own stop reaches only the
  ladder reference. §5.1 now follows what runs, which carries two consequences.
  The section is pinned to the deployed behaviour of one function, so a keeper
  change moves it — that is the risk, and it is why §5.1 cites the two writers by
  line. And whether the keeper SHOULD place a document's declared stop, and
  refuse or flag the declared take-profit ladder it supersedes, is an open
  question on the live side (#1598). The replay does not wait for that answer: a
  tool reporting what a document would have done reports the deployment that
  runs, and the day the keeper changes, this section changes with it.
- **The extraction boundary is a claim, not yet a fact.** §3.2 lists the
  minimal view field by field, and the two order-state questions cross as
  booleans. If implementation finds it needs an order leg, a journal handle or a
  calendar, the boundary is wrong and the work stops there rather than widening
  the contract.
- **Step 2 is the risky half and nothing here de-risks it.** It edits
  `position_manager`, which is money-adjacent. The argument for doing it
  separately rests on a blueprint rule, not on a measurement.
- **Effort is unestimated.** This document describes a shape, not a cost. No
  line of the interpreter has been written.
- **This tool cannot validate a policy.** It replays documents over past bars
  and is in-sample by construction. Nothing it produces is evidence of an edge;
  it answers "what would this have done", never "does this work".

### 8.1 Capabilities this design did not handle, and how each was decided

Found by a multi-agent survey of the arming surface on 2026-09-24 and each
confirmed against the source before being written here. The heading carried a
count until the list grew, which is the usual fate of a count in a heading.

Some of these move the answer in the FLATTERING direction — a replay that
silently ignores them reports a better number than the daemon would have
produced, which is the failure mode hardest to notice. The rest are not
directional at all; they simply have no defensible default, which is why each
needed deciding here rather than at the keyboard.

**All five were decided on 2026-09-25.** §2.1 is the rule that settled four of
them: a value the replay cannot derive is stated by the caller, and a missing one
is a refusal rather than a default. The fifth — the trail's order-state guards —
turned out not to be a decision at all (§3.2). Each item keeps its original
wording with the resolution beneath it, because the reasoning is what a later
reader needs and a rewritten bullet loses it.

- **`entry_mode: "immediate"` is a second entry shape, and the design knows only
  the first.** `EntryTierSpec.entry_mode` (#1247) takes `"pullback"` — a resting
  rung below the market — or `"immediate"`, a tranche the daemon buys AT DRAIN,
  for which `limit_price` is the operator's CAP rather than a pullback level. A
  bar walk asking "did the low touch the limit" replays an immediate tranche as
  a resting order that waits, which is not what happens and not what it costs.
  `entry_mode_unknown` is a live refusal code, so the vocabulary is enforced —
  the replay simply has no model of its second member.

  **Decided: refuse it in v1**, with `entry_mode_unsupported` (§5.4). Modelling
  "buy at drain" needs a drain-time price the bars may not carry, and a wrong
  model of an entry costs more than a named refusal.

- **The reaction primitives need order-state inputs a replay does not have.**
  §3.2 lists `has_sole_standalone_stop` and `amend_in_backoff` as required
  fields of the minimal view, and insists they cross as the PREDICATE's result
  rather than as order legs. A replay has no broker orders, so it must supply
  both — and this document never says what. The choice is not a detail: `False`
  means the trail never fires, `True` means it always does. That is the whole
  result. Neither is obviously right, which is why it needs deciding here rather
  than at the keyboard.

  **Decided: not a configuration field, and on inspection not a decision.**
  `_maybe_trail` gates on nine conditions and seven come from the document or the
  tape; the two named here ask whether one clean stop rests at the broker and
  whether a previous amend was refused, and a replay has neither orders nor a
  broker. §3.2 records the reading guard by guard. The replay passes "no broker
  obstacle" and REPORTS the resulting optimism as `daemon_trail_guards`.

- **A take-profit tranche is gated on clearing its own cost, and the replay has
  no cost model (resolved below).** `live_exit_engine._exit_clears_cost` refuses to fire a target
  that does not cover the round trip. §2 makes cost a non-goal and the envelope
  said `"costs": "none"` when this was written, which is coherent for MEASURING
  cash — but the gate is not accounting, it is a REFUSAL that changes which exits
  happen. A costless
  replay fires tranches the daemon would decline, so the trace diverges in
  events, not only in cash.

  **Decided: the threshold is a stated, required configuration value** (§2.1,
  §5.2) — `commission_rate`, `min_commission`, whether the per-fill minimum and
  the FX leg apply, and `exit_edge_min_bps`. The replay still does not MODEL
  execution economics; it is handed the number the gate compares against. Omitting
  it is `config_incomplete`, not a costless run. This narrows §2's cost non-goal
  rather than reversing it: the non-goal bounds what the tool computes, and the
  gate needs a threshold, not a model.

- **The day-1 anchor depends on `meta.source`, which the design never reads.**
  `control_loop` passes `day1_includes_trade_date=source == "manual"` at two
  sites: a manual pick counts `meta.trade_date` itself as day 1, a brief pick
  starts the session after. That is a full session of difference in where the
  walk begins, on every hand-authored pick — and hand-authored picks are exactly
  the documents with `initial_levels`, the shape this tool exists to study.
  `meta.source` is therefore a required path that §4.3.1 gives NO class, so the
  gate refuses every document until this is decided. That is the gate working:
  the alternative is an anchor chosen at the keyboard and never written down.

  **Decided: translated.** The client resolves the first session of the walk and
  states it as `walk_start` in the provenance shape of §5.1; `meta.source` and
  `meta.trade_date` both take the `translated` class (§4.1, §4.3.1). The gate
  accepts documents again.

- **The entry deadline is a fact of PLACEMENT, not of the document.** Four live
  implementations resolve `spec.order_ttl_days` four ways, and they disagree on
  the anchor, the calendar and whether the cutoff session is tradeable. The
  entry-trail watch — the deployed path — anchors on `meta.trade_date`, uses the
  instrument's MIC, cuts at the session close, and reads a CONSTANT rather than
  the document's own field. The Saxo bracket anchors on the day the daemon
  actually placed, so queue latency alone moves the deadline. `/edge` anchors on
  the first session after the brief and cuts at the session open, one session
  earlier in effect. The reconciler anchors on the submission record. The
  published field description says the sessions are XNYS whatever the instrument
  is, which the code does not do. The replay cannot derive the deadline from the
  document and must be given it (§4.1); this design does not say which of the
  four it is given, and that choice moves the entry window by whole sessions.

  **Decided: required and stated, with the rule inside `formula`.** The replay
  picks none of the four, the deployed one included. Choosing here would be the
  tool inheriting a deployment fact (§2.1) and would hide the choice from whoever
  reads the result. §4.1 carries the decision; omitting the deadline is
  `config_incomplete`.

- **The OCO pair has a stated switch and no model (added 2026-09-25 by PR 3).**
  §3.3 lists `oco` among the four facts a run states and calls the pair "a
  modelling gap, not a switch", pointing here — and until PR 3 nothing here
  decided what a stated `true` means. The config block travels in the result
  (§5.2), so a run accepting `true` over a walk that models no pair would be a
  result claiming a policy the run did not apply: the `ceiling_price` shape of
  §4.3.1 on the configuration side.

  **Decided: `oco` is required and only `false` is accepted in v1.** A stated
  `true` is refused with `config_invalid` (`oco_unsupported`, §5.4), never
  echoed. Modelling the pair means stating what "the engine's next observation"
  is relative to a bar (§3.3), which is a design edit here, not a knob.

None of these changed the architecture. The entry-trailing risk of §8 was the
possible exception and in the end it did not: modelling the native order adds a
module and a stated value, not a different shape. What it did add is the one
divergence in this design that no test can close (§6.1, §8).

Each of the five would otherwise have been decided silently by whoever wrote the
first version of the interpreter — four of them in the direction that looks
better. That is the whole reason they were written down as open rather than
resolved at the keyboard.

---

## 9. Relation to epic #1526

They are related and disjoint. #1526 is about the `/edge` lenses and is blocked
on an owner decision about the stamped series. This tool touches `/edge` not at
all.

If this is built first, sub-issue 1 of #1526 — "one document-driven replay" —
becomes wiring rather than construction: the engine already exists, and what
remains is the per-row generator and the series decision.
