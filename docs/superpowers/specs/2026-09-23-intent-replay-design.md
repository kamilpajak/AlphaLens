# intent-replay — a document-driven backtester

**Status:** LOCKED 2026-09-25 (design agreed in brainstorming 2026-09-23; revised after adversarial
review the same day; revised again 2026-09-24 after an arming-surface survey and a second
adversarial review that refuted several statements of the first revision — see §3.3, §4.3.1, §8;
closed 2026-09-25 by deciding the five items §8.1 left open and the entry-trailing question of §8,
under the rule now stated as §2.1; §3.1 and §5.4 corrected the same day after an
adversarial review of the implementation plan found the JSON Schema gate
unimplementable under the old per-package dependency rule; §5.1 corrected
2026-09-26 after PR 5 ran both placement paths and refuted its claim about which
stop rests, #1597; implementation under way, epic #1571, PR 1-5 merged;
sections 4.4, 5.4, 6.3 and 8.1 revised 2026-09-26 before PR 6, after running the stop arms and the
cost gate: the re-anchor arm can LOWER the resting stop, a rung below a stop
that has moved is unreachable, and the cost gate's FX leg has no stated rate;
sections 4.3.1, 5, 5.2 and 8.1 revised 2026-10-01 by deciding #1592 under its
option 1 — the FX leg and the per-fill minimum are priced from STATED facts, and
§5.2.1 is the key set; sections 4.3.1, 5, 5.2, 5.2.1, 5.4 and 8.1 revised again
2026-10-02 when that decision was IMPLEMENTED — the three surfaces §5.2.1 held
back all moved, `spec.size.currency` became *interpreted*, the
`cost_gate_prices_the_account_currency` divergence retired, the symbolic unit
`instrument_currency` retired, the `USD/PLN` token became `USD_per_PLN`, and the
whole-share paragraph of §5 was re-measured under the buffer; §5 revised
2026-10-08 to publish the walked sub-window beside the input series)
**Date:** 2026-09-23, last revised 2026-10-08
(2026-09-28, PR 7's plan: sections 4.4, 4.6, 5, 5.1, 5.2, 5.3 and 5.4 revised — among them a
FOURTH tie row, which fixed the rung/take-profit order as "the take-profit first".
2026-09-29: that row's RESOLUTION is WITHDRAWN. Running it showed it is not a bound in
either direction, and the literature this section was re-deriving says why: for a document
with more than one entry or exit a unique worst case need not exist at all. The row's
DETECTION survives, the section stops claiming pessimism it cannot deliver, and the
vocabulary becomes the published one — see the revision note in 4.4.
2026-09-29, a second correction, this time of this document against the code that shipped the
same day: §6.3 stated two properties the engine does not have. Its `snu_bars` bullet named TWO
uncounted shapes where the detector leaves ONE uncounted — the cost-gate verdict flip is
counted, and a test pins it — and its `mfe`/`mae` bullet claimed a strict sign that
`avg = cash / units` breaks by one ulp. Both were found by running the tests and the walk while
planning PR 7. The lesson is in the shape of the mistake: #1614 revised this section and #1613
repaired the code it describes, on the same day, in that order, and neither step re-read the
other)
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
| intra-bar ties | ONE declared decision rule, fixed, never a configuration field. Pessimistic where a worse resolution is well defined per period; NOT a bound on the run, and for a laddered document a unique worst case need not exist (§4.4) |
| a rung below a stop that has MOVED | skipped on every bar the stop sits above it, never cancelled. Filling it books a purchase at a price the same bar had already sold at, and a fill after the position closes is a re-entry this tool does not model. Under a trail the level tested is the TRIGGER and the boundary is the bar's OPEN, not its low: a bar opening above the stop reached the trigger first, so the fill stands and the stop takes it out after (§4.4) |
| which bars count as SNUs | only the bars whose ordering the tape cannot settle AND where it changes the money. A same-bar LIMIT-rung fill and stop-out is forced by the levels and is NOT counted, against the existing `/edge` replay; under a trail that argument does not carry, and §4.4 says what replaces it. The count detects; it does not certify, and it is a frequency rather than a magnitude (§4.4, §6.3) |
| the gap-open fill price | it follows the tape wherever the order RESTS at the broker — a rung, the disaster stop, and a trailing entry order once placed, all fill at the bar's open when the open is already through them. The bar that ARMS a trail is the exception: its order is placed at the touch, which cannot precede the open, so it takes its trigger however high the bar opened. A take-profit rests nowhere in v1, so it fills at its level (§4.4) |
| the cost gate's FX leg | priced from stated facts, decided 2026-10-01 under #1592: the configuration states the instrument's currency, a mid rate, the round-trip FX cost rate and the sizing buffer, and `fx_applies` becomes DERIVED rather than declared (§5.2.1, §8.1). IMPLEMENTED 2026-10-02: the keys exist, the refusal `fx_cost_not_stated` is gone, and the `cost_gate_prices_the_account_currency` divergence retired with it |
| every input path | classified here as interpreted, translated or out of scope; an unclassified path is refused, never approximated (§4.3.1) |
| where a run value comes from | the document, or STATED in the run configuration — never inherited from a deployment, an environment variable or a production constant (§2.1) |
| day-1 anchor | translated: the client resolves the first session and states it as `walk_start`; `meta.source` is not read here (§4.1, §4.3.1) |
| the trail's two order-state guards | not a configuration field. A replay has no order legs and no amend history, so both are inapplicable; the resulting divergence is REPORTED (§3.2) |
| the take-profit cost gate | its threshold is a required, stated configuration value; absent is a refusal, not a costless run (§2.1, §5.2) |
| `entry_mode: "immediate"` | refused in v1 with its own code; modelling it is a later version (§4.3.1, §5.4) |
| entry trailing | MODELLED, with the trail distance stated in the configuration. The alternative was a tool describing an entry ladder production does not use (§3.3, §8). A rung stops resting: the first bar to reach it ARMS a trailing order referenced on `min(open, limit)`, whose trigger is `trough + distance` with the distance absolute and frozen at the arm (§4.4) |
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
consume after the split. It returns PRICES — the level to place, and, through
`decide_trail_detail` / `decide_reanchor_detail`, the policy's raw proposal and
the envelope's output, which the daemon needs for its own log lines and for the
`envelope_clamped` journal record (#1015). Never a broker action and never a
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
| `ratchet_floor` | float \| None | the level a new proposal must clear, COMPOSED by the caller as the higher of the last trailed level and the level the stop is resting at. Named `last_trailed_level` until 2026-10-02, when #1581 found the leaf ratcheting against one floor where the daemon ratchets against two (#1514): the journaled level can lag the resting one, and a proposal between them would patch the resting stop DOWN. Both inputs are the caller's own — a journal fold and an order leg's price — so the composition stays caller-side and nothing order-shaped crosses |
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

**Both guards sit on the RE-ANCHOR arm as well, and this section missed that
until 2026-09-28.** `stop_decision._reanchor` checks `has_sole_standalone_stop`
and `amend_in_backoff` in the same order `_trail` does, and the walk supplies
both unconditionally for whichever arm the declared policy selects. So a
document declaring `reanchor_on_fill` is exactly as free of broker obstacles as
one declaring `trailing_stop`, and the divergence §5.2 names covers it. The
reading above stays correct for the trailing arm; it was simply not the whole
surface. A static run is the one place the guards are unreachable rather than
assumed: `_reanchor` returns at `if not policy.requires_amend_stop` before either
is read.

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
`snu_bars` reports. Entry trailing is the hardest: its trigger is a
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
the split blueprint lists `market.calendar` as edge **E1** — one of the three
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
  `spec.size.currency` left this class again when #1592 landed, and the way it
  left is the clause's own limit: a label the result echoes can also be a value
  something reads, and then the label stops deciding the class.

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
| `instrument.mic` | translated (calendar, settlement currency) + out of scope (fee card) | the calendar reaches the replay through `entry_deadline` (§4.1), and the settlement currency through `fx.instrument_currency` (§5.2.1) — no table in this repo maps a MIC to a currency, so the client resolves it and states it with its own `formula`. The settlement half was *out of scope* until #1592 landed, on a reason that did not hold: that `spec.size` is already in the account currency is why a conversion is NEEDED, not why it can be ignored. The fee card is out of scope for the §2 reason alone: the threshold arrives stated |
| `spec.size.currency` | interpreted | it is the account code the run configuration is parsed against, so its value decides the fx key set of §5.2.1 — a stated magnitude on a same-currency run is refused and an absent one on a cross-currency run is refused. A path whose value can raise a refusal is interpreted by the definition above. It was *out of scope* until #1592 landed, as a label on the unit of the cash answer; the label is still true and is no longer the whole story |
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
| `spec.size.currency` | interpreted | above |
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
`instrument.mic` is *translated* twice over — for its calendar, through the
deadline of §4.1, and for its settlement currency, through
`fx.instrument_currency` (§5.2.1) — while its fee card is *out of scope*,
because §2 makes cost a non-goal and the threshold arrives stated. All three
halves had to be written down to get there, which is exactly the work the gate
exists to force: the settlement currency sat in the wrong class for three days
on a reason that read as an argument and was not one.

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

### 4.4 Intra-bar ties: what the tape cannot settle, and what the replay does

A minute bar carries an open, a high, a low and a close. It does NOT carry the
ORDER in which the high and the low occurred. When one bar touches two levels
whose outcomes differ, the data cannot say which happened first, and the replay
must assume.

**The field has a name for this and a published taxonomy; this section used
neither until 2026-09-29.** Maier-Paape and Platen call such a bar an **SNU** —
their abbreviation for "situation which is not unique" — in *Backtest of Trading
Systems on Candle Charts* (arXiv:1412.5558, Institut für Mathematik, RWTH
Aachen). Their words: "there are always situations, which can not or not
uniquely (SNU: situation which is not unique) be determined". They propose four
modes a backtester may offer the user — worst case (`wc`), best case (`bc`),
ignore the trade (`ig`) and load finer data (`ex`) — and AgenaTrader ships them
as a "Decision Mode" setting. This replay offers none of the four as a switch.
It applies ONE stated decision rule and reports how often that rule decided
money. That is a fifth thing, and naming it is the point of this revision.

**The INTENT is still pessimistic: where a worse resolution is well defined, it
wins.** Three facts fix how far that reaches, and the section claimed more than
all three allow until 2026-09-29.

**It is per PERIOD, not per run.** The same authors need the same restriction:
their best and worst cases are taken "on premise that it is best/worst for the
current period only", because exiting at a target now may be worse than holding
for a better exit later. A rule that is worse on this bar is not thereby a lower
bound on the run.

**For a document with more than one entry or exit, a unique worst case need not
EXIST.** Löw, Maier-Paape and Platen restrict their whole theory to "setups
which allow for at most one entry execution and ... at most one entry and one
exit", and close with: "Many of those concepts can be generalized to more
complex situations with more than one entry or exit. It remains to be shown how
our results can be transferred to these setups. Furthermore, in that case there
would no longer necessarily be unique worst cases and best cases."
(*Correctness of Backtest Engines*, arXiv:1509.08248.) Every document this tool
replays is that shape: a laddered entry, a laddered exit and a disaster stop. So
the convention below is a DECLARED DECISION RULE, not an approximation of a
worst case that may not exist.

**Measured, which is what forced this revision.** On 2026-09-28 a fourth row was
added here fixing the rung/take-profit order as "the take-profit first", on the
argument that it sells fewer units and is therefore worse. Running it showed the
rule is not a bound in either direction: it ended 39.83 units BETTER than a
tape-consistent alternative on one input and 144.68 units WORSE on another,
because a take-profit whose clamp sweeps the whole position TERMINATES the trade
and so escapes every later loss. The resolution is withdrawn; its DETECTION is
kept below.

Three situations have a resolution that is worse in the per-period sense, and
they are the ones the existing replay already resolves this way:

| one bar touches | resolution | why this is the worse one |
|---|---|---|
| the stop and a take-profit | the stop | +1R becomes −1R |
| a lower entry rung and the stop | fill first, then stop out | without the fill there would be no loss |
| a new low and the trailing entry trigger | the trigger fires on the PRE-BAR trough | the trough had not ratcheted down yet, so the buy pays the higher trigger |

Row 1 is not this tool's invention: QuantConnect's Lean says the same thing in
prose where it resolves a contingent pair — "we can't know which one would of
happen first, so we make the pessimistic assumption: stop orders, like the stop
loss, go first". Not every engine agrees. NautilusTrader walks a bar
open→high→low→close by default, which on a long resolves the same pair the other
way, and offers the open-proximity heuristic only as an opt-in. TradingView and
NinjaTrader both pick the order from whether the open sits nearer the high or the
low, so neither declares a fixed path at all. This replay's order is FIXED
because section 6.3 requires two runs over one input to be byte-identical.

The third row arrived on 2026-09-25 with the decision to model entry trailing
(§3.3) — which is what the earlier text anticipated in saying the convention
covers the class rather than the cases. The trigger is a running low plus a
distance, so a document entering on a trail rather than at fixed rungs can meet
the convention on any bar that makes a new low and retraces.

**The rule that decides which of those bars COUNT is the one at the end of this
section, not "makes a new low and retraces".** That phrase is looser than the
criterion and an earlier revision left it standing alone. A bar counts when both
readings are consistent with it AND lead to different money, which for a trail
means all three of: the open did not gap through the trigger of an order ALREADY
RESTING (that gap is the first print and forced, and both readings fill there),
the low could have fallen below the trough the bar inherited, and the retrace
reached `low + distance` so that the low-first reading would have fired. A bar
making a new low and retracing five basis points at a fifty-basis-point distance
satisfies the phrase and changes nothing.

The first of those three says RESTING for a reason that is not a detail. On the
ARMING bar no order exists at the open — it is placed when the price reaches the
rung — so an open above the trigger has settled nothing and the bar is counted.
That is the common case rather than an edge one: for a rung placed as a pullback
trap of depth `p`, the open lies above the trigger whenever `p > d / (1 + d)`,
which is under half a percent at 50 basis points.

**And the model, published here because §4.4 is where a reader checks a bar
against it.** With `entry_trail_bps` stated, nothing rests at a rung:

| | |
|---|---|
| arming | the first bar whose low reaches the rung, referenced on `min(open, limit)` — the touch is the first print when the bar gapped through the level, and the level otherwise |
| the trigger | `trough + distance`, the distance ABSOLUTE and frozen at the arm. The wire field is a price distance computed once, so the trigger is not `trough x (1 + d)`; the two agree at the touch and part as the trough falls, by 21 bps at a 30% drawdown on a 50 bps distance |
| the order inside a bar | the trigger is tested against the trough the bar INHERITED, and only then does the bar's low ratchet it down — row 3 above |
| the arming bar | can fire, at `reference + distance`, and takes that level however high it opened: its order is placed at the touch, which cannot precede the open. Seeding the trough at the reference makes this fall out of the same rule as every later bar rather than needing a case of its own |
| the resting stop | a bar opening ABOVE it fires and is then stopped out; a bar opening BELOW it closed the position at the first print, and buying after that is the re-entry this document declines to model |
| depth | a bar whose FIRST price is already below the NEXT-LISTED rung hands the move on, and the shallower rung never arms. LISTED, not cheapest: `validate_intent` does not order the ladder. Measured against the live engine 2026-09-29 — the wire arms on the touch tick and an armed tier is terminal for its watcher, so a depth reached later suspends nothing |
| the deadline | an armed or a handed-on rung expires with the others, under the one cause §4.6 publishes |
| quantity | from the rung's LIMIT, as in the drain. A trailing fire changes when and at what price a rung executes, never how much it buys |

**How often the convention decides, for a trail, measured by RUNNING it.** The
construction decides the number, so it is published with it: the last 20 sessions
of the split-adjusted daily store, every one of the 11 654 tickers with a
complete history, an eight-bar watch, one rung as a pullback trap at the first
bar's open less `p`, 50 bps, and no take-profit ladder so the count carries this
source alone.

| trap `p` | mean `snu_bars` | watches reporting 0 | watches that fired |
|---|---|---|---|
| 0.5% | 0.67 | 34.2% | 69.8% |
| 1.0% | 0.57 | 44.1% | 57.0% |
| 2.0% | 0.44 | 57.1% | 43.8% |

The trap depth is in that table because it decides whether the ARMING bar can be
counted, and for realistic depths it always can: a rung at `open × (1 − p)` has
its trigger at `rung × (1 + d)`, and the open lies above that trigger exactly
when `p > d / (1 + d)`, which is 0.4975% at 50 bps. The arming bar is therefore
the common case rather than an edge one, and it is counted because the order is
not resting at the open. Zero from this source is common without being the rule,
and the frequency follows the bar's granularity and the trap depth rather than
any property of the model. Two earlier figures do not survive: a claim that the
row "fires most often" was the frequency of one CONJUNCT, and a mean of 0.23 came
from a construction that was never published with it and cannot be reproduced
from the text.

**A FOURTH situation is an SNU that this section does NOT resolve, and says so.**
A rung whose limit is at or above the bar's open is through at the first print,
so its fill is not in question. A rung BELOW the open fills somewhere inside the
bar, and if that same bar also reaches an unfired take-profit, the tape does not
say which came first. It is deliberately not a row of the table above, because
the table's third column is a claim this case cannot support.

The reason is the clamp. A tranche sells `min(fraction × intended, held)`, so
whether it sweeps the WHOLE position depends on how much is held when it fires —
which is exactly what the unknown order decides. Three bands, and the boundary
between them is computable from the bar without looking ahead:

| band | what the two readings do | can a worse one be named? |
|---|---|---|
| the touched ladder wants FEWER units than are already held | both sell the same units at the same price, and the rung fills in both | no SNU at all |
| the ladder's appetite reaches what is held even AFTER the deep rung fills | both readings sweep the position on this bar, so there is no later tape to escape | YES, per period: tranche-first sells fewer units and is worse |
| in between — the ladder sweeps only the smaller position | one reading ends the trade here, the other carries a residue into later bars | NO. This is the case the paragraph above measured at 39.83 one way and 144.68 the other |

The third band is why no resolution is published for this case. A rule that is
worse per period is better or worse per RUN depending on bars the replay has not
read, and by Löw et al. a unique worst case for a laddered document need not
exist to be found.

**And the second band is not a place to apply the convention selectively, for a
reason that has nothing to do with convenience.** The boundary between bands 2
and 3 is drawn by the take-profit COST GATE: which tranches count toward the
ladder's appetite depends on what the gate affords, and the gate's verdict
itself moves with the order, because a deep fill lowers the average entry and
lowers the threshold with it. Making the FILL ORDER depend on that boundary
would make it depend on the stated commission — a document would fill different
rungs because its `costs` block changed. Execution order must not be a function
of the cost model.

So the walk keeps ONE order for every bar: each touched rung fills, then the
ladder is reviewed. That order is a DECLARED RULE with no pessimism claimed for
it, and in the second band it knowingly leaves a nameable worse reading untaken.
What the replay owes the reader here is the COUNT, which is what says so.

Measured 2026-09-28 on the published template (rungs 68.00 and 66.50 carrying
60% and 40% of 1500, one tranche of 100% at 68.50, disaster stop 63.00) with a
bar `open 67.00 / high 68.60 / low 66.40` and then a bar falling to 62.00:

| reading, both consistent with the bar | units sold | net cash |
|---|---|---|
| the low reaches rung 2, then the high reaches the tranche (the declared rule) | 22.2579 | +37.898054 |
| the high reaches the tranche, the sweep completes, rung 2 never fills | 13.2353 | +19.852941 |

That bar sits in the SECOND band — both readings sweep — so there the worse
reading is nameable and it is the second one. The rule does not take it, and
the count is what says so. An earlier revision of this section published the
opposite and called it pessimism; a third input, where only one reading sweeps,
is what refuted that.

**Which bars are COUNTED, and why it is not every bar the table covers.** Row
2 is resolved by the convention and NOT counted, because its order is forced
by the levels rather than assumed: `validate_intent` requires
`spec.disaster_stop` below every rung, so a long has to cross the rung to
reach the stop — and the same holds when the bar opens below the stop, because
there is no position to protect until the rung has filled. **That reason is about
the LIMIT and does not carry to a trail:** nothing constrains a TRIGGER against
the stop, since the trigger sits above the trough, the trough can be above the
stop, and a price walking down from the open need never pass the trigger on its
way there. Under a trail the two orderings are different money and the bar is
counted, by row 3's own predicate. Counting it would
put bars where the rule changed nothing into a number whose published meaning
is how much of the answer came from the rule. So `snu_bars` counts the bars of
rows 1 and 3 and the fourth situation's second and third bands — the ones where
both orderings are consistent with the bar and lead to different money. Decided
2026-09-26 with PR 6, against the existing `/edge` replay, which counts a
same-bar entry fill as ambiguous.

**The field is named `snu_bars`, not `ambiguous_bars`, from 2026-09-29.**
`ambiguous_bars` is already a published name on the `/edge` side — an integer on
the ladder-outcome model, in its serializer and in the parity OpenAPI — carrying
the NARROWER meaning of one SL-first flag. Epic #1526 exists to express `/edge`
lenses as documents this tool replays, so two different integers were on course
to meet under one name. `snu_bars` cannot collide, and a reader who looks the
term up lands on arXiv:1412.5558 rather than on a word this project invented.
The rename is free now and expensive after a consumer exists.

**Row 2's guarantee covers the stop the document DECLARED, not one that has
moved.** Nothing constrains a trailed or re-anchored stop against a rung still
resting below it, and the published templates reach that state: on
`pullback-trailing-stop.json` (rungs 68.00 and 66.50, `trailing_stop` 0.5 /
0.6) a fill at 68.00 and a peak of 70.60 put the stop at 69.56 with the 66.50
rung still live. **The walk SKIPS such a rung on every bar the stop sits above
it**, the gap-open rule below included, and does not fill it. Under a trail the
level to test is the TRIGGER rather than the limit, and one boundary the limit
case does not need appears: the bar's OPEN against the stop. A bar opening ABOVE
the stop reached the trigger first, so the fill stands and the stop takes it out
afterwards — a loss, and the worse resolution this section keeps. A bar opening at
or BELOW the stop closed the position at its first print, and a purchase after
that is the re-entry this section declines two paragraphs down. It is skipped and
not cancelled: the re-anchor arm can put the stop back below the rung (§6.3),
and the rung is live again on the first bar where it is.

Two reasons, separated because only the first is about the tape. For a bar whose
low reaches the rung, continuity settles it: the price must cross 69.56 first,
and the stop closes the position there. For a bar that OPENS below both, both
resting orders execute in the same auction and the tape settles nothing — the
walk still does not fill, because a fill at or after the position closes is a
RE-ENTRY, a second position this tool does not model. That half is a scope
boundary, not a claim about the broker: on both deployed paths the entry order is
still working after the stop fills, so live it would fill, and nothing cancels
it. It is deliberately not a `divergences` entry either, for the reason §6.4
gives about an out-of-scope field: §5.2 reserves that list for facts the replay
LACKS, and a re-entry v1 declines to model is the opposite case.

Filling such a rung is what a first implementation did, and it booked a purchase
at a price the same bar had already sold at — 57% of the reported result on the
template above, in the flattering direction. Decided 2026-09-26 with PR 6
(#1577).

**The gap rule is separate from the tie convention, and it does not reach
every leg.** A bar that opens already through a level is not ambiguous — the
open is the first trade — so an order that RESTS at the broker fills there: a
rung at `min(open, limit)`, the disaster stop at `min(open, stop)`, and a
trailing entry order already placed at `max(open, trigger)`. The bar that ARMS a
trail is outside the rule: its order goes on at the touch, which cannot precede
the open, so it takes its trigger however high the bar opened. A
take-profit is the exception, for §3.3's reason: the OCO pair is retired on
SIM and unset on LIVE, a run must state `oco: false`, so no take-profit rests
anywhere. An engine realises it at its next observation and market-sells at
whatever the bid is by then, which the bar's open bounds in neither direction.
What the replay lacks is the engine's observation TIME, not a price, so it
cannot say which print the engine saw; a take-profit therefore fills AT the
level the document names. The residual is a fact the replay lacks and is
reported as the `take_profit_observation_time` divergence (§5.2), because what
it leaves open is WHETHER a tranche fired at all and no price convention closes
that. Decided 2026-09-26 with PR 6.

**No optimistic mode is offered.** A `tp_first` switch would be a knob whose
only use is making a result look better, and in a research tool such a knob is
eventually turned and then forgotten.

The cost of the convention is not uniform across policies: a tight stop meets
ambiguous bars far more often than a 1.5 x ATR bracket, so the assumption
enters any comparison BETWEEN policies, not just the level of one. That is why
the summary carries `snu_bars` — the count of bars where the rule actually had to
decide something. A large count means much of the result comes from the rule
rather than from the tape, and the reader is entitled to know which.

**What zero does NOT mean.** An earlier revision said zero means "the assumption
carried nothing and the result is hard data". That is false and was false when
it was written. The counter is produced by the same single pass that produces
the cash, so it can only detect what that pass can see; section 6.3 lists the
shapes it is known to miss. Zero means no SNU was DETECTED, which is weaker than
no SNU occurring, and a reader must not read it as a certificate.

**And the count is a frequency, never a magnitude.** Two runs can both report 1
while the bar decided 18.05 units in one and 30.92 in the other. Pricing an SNU
needs both readings carried forward to the end of the tape, which is a different
engine and an open question rather than a scheduled feature (§8), so until then
the honest use of `snu_bars` is to decide whether to trust a result at all, not
to correct it.

One scoping note. The fourth situation lifts the ceiling the count used to have:
a take-profit that sells only part of the position leaves the walk running, so a
laddered exit can meet it on several bars. Before that, only row 1 could be
counted outside a run stating an entry-trail distance, and row 1 ends the walk,
so the count was 0 or 1 and read as a flag.

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

`horizon_open` carries the units still held AND the `price` they are valued at:
the CLOSE of the last bar the walk saw. Added 2026-09-28 by PR 7's plan, for one
reason — without it the envelope's cash would depend on a number that is in
neither the trace nor the summary, and §5 promises a summary its own trace can
reproduce. Both replay engines in this repo already mark an open remainder to the
last close, and the research one says why in a comment: a mark is a valuation,
not a fill, so it pays no fee and takes no slippage. The cost gate is not
consulted for it either.

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
  "walked": {"from_t": 1790170200000, "to_t": 1790262000000, "bars": 481},
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
    "fx": {
      "instrument_currency": {
        "kind": "venue_settlement_currency",
        "value": "USD",
        "unit": "iso_4217",
        "source": "instrument.mic",
        "formula": "XNYS settles in USD"
      },
      "mid_rate": {
        "kind": "fx_mid",
        "value": 1.08,
        "unit": "USD_per_EUR",
        "source": "ecb_reference_rate",
        "formula": "ECB euro foreign exchange reference rate, 2026-09-23 14:15 UTC"
      },
      "round_trip_cost_rate":  {"value": 0.005, "unit": "fraction"},
      "sizing_buffer_pct":     {"value": 1.0,   "unit": "percent"}
    },
    "costs": {
      "commission_rate":       {"value": 0.0008, "unit": "fraction"},
      "min_commission":        {"value": 1.0,    "unit": "USD"},
      "min_commission_applies": true,
      "exit_edge_min_bps":     {"value": 5.0,    "unit": "bps"}
    }
  },
  "fx": {
    "applies": true,
    "notional_spent": {"value": 962.28, "unit": "USD"}
  },
  "divergences": [
    "daemon_trail_guards",
    "native_entry_trail_is_a_broker_model",
    "take_profit_observation_time"
  ],
  "intrabar_rule": "entries_then_stop_then_ladder",
  "outcome": "closed_tp",
  "summary": {
    "filled_fraction": 0.6,
    "snu_bars": 3,
    "notional_spent":   {"value": 891.0,  "unit": "EUR"},
    "avg_entry_price":  {"value": 67.83,  "unit": "USD"},
    "pnl_cash":         {"value": 40.788, "unit": "EUR"},
    "pnl_pct_of_spent": {"value": 4.58,   "unit": "percent"},
    "r_multiple": {
      "value": 1.90,
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

**Eight things about that block, added 2026-09-28 and 2026-09-29 while planning
PR 7 — the first version that EMITS it — and amended 2026-10-02 when #1592
landed and 2026-10-08 when the walked window was published.**

**`window` is the series handed in and `walked` is the part the loop read, and
the second exists because the first was being read as the holding horizon.** A
position whose `outcome` is `open` is valued at the CLOSE of the last bar the
walk saw (§4.6), so its `r_multiple` depends entirely on where the walk ended —
and `window` cannot say where that was. Bars before `walk_start` are skipped and
the loop breaks on the bar that closes the position, so `walked` can be narrower
at both ends at once. A real measurement took `window.bars` for the horizon on a
121-bar series of which 90 were context before `walk_start` and 31 were walked. The two
blocks carry the SAME three key names on purpose: put side by side, the only
thing left to explain is the skip and the break. `bars` is a count and not
`(to_t − from_t)` divided by a bar width — the tape has session gaps, and in the
block above 481 bars span 25.5 hours. Both timestamps are `null` when no bar was
read, which the CLI cannot print, because `window_too_short` refuses a series
that does not straddle `walk_start` (§5.4). The `walked` figures in the block
above are chosen for a fictional run whose `trace` is already `[]`; they are
internally consistent with its `closed_tp` outcome, not a measurement.

**The top-level `fx` block is what the conversion DID**, and it is separate from
the `fx` inside `config` because that one has to round-trip: the configuration
parser refuses a derived key on input, exactly as the arming door does, so
`applies` and the derived notional cannot ride inside the echo. `applies` is the
comparison of `spec.size.currency` against `fx.instrument_currency` and nothing
else; `notional_spent` is the same spend in the instrument's currency, printed
because the rate's magnitude is otherwise almost unobservable (§8.1). On a
same-currency run `applies` is `false` and `notional_spent` is `null` — the
conversion is the identity there, so the account figure already IS the
instrument figure and a copy would read as a second measurement.

**`intrabar_rule` names the decision rule, and the cash is NOT a bound.** The
key was added 2026-09-29, after §4.4 had claimed pessimism for five days that it
cannot deliver for a laddered document. A reader who saw that claim would take
`pnl_cash` for a conservative number; it is one outcome under one stated rule,
and `snu_bars` says how often the rule had to decide. The precedent is in this
repo on the other side: `/edge`'s chart payload already publishes
`intrabar_rule: "sl_first"` beside its own count, for the same reason — a result
cannot be read out of the world that produced it (§5.2).

**The R it printed was impossible, and the identity that catches it is one
line.** For any definition where `pnl = proceeds − spend`, `spend = units × avg`
and the denominator is per share, `r = (pnl_pct / 100) × avg / den` holds without
knowing how R is computed. Run against this block's own numbers, that reads
**1.9049734**, and the block printed 0.72 — out by a factor of 2.65. Holding
spend, avg, pnl and den, R is 1.90 and the block now says so. Had 0.72 been the
intended figure, `pnl_cash` would read 15.4162 and `pnl_pct_of_spent` 1.7302.
The identity is a §6.3 property, so this class of error cannot come back.

**Two fields carry the INSTRUMENT's currency, and until 2026-10-02 they could
not name it.** `avg_entry_price` and `denominator` are prices in that currency.
No DOCUMENT path states it, so both carried the symbolic unit
`instrument_currency` — a unit the tool could not spell. #1592 made the currency
a STATED fact (`fx.instrument_currency`, §5.2.1), so both fields now carry a
real code and the symbolic token is retired. The cash fields were always
different: `spec.size.currency` IS stated, so `notional_spent` and `pnl_cash`
carry the ACCOUNT's code — and this block prints both, EUR for the cash and USD
for the prices. It is the cross-currency document §8.1 describes, a 1500 EUR
budget on a KO/XNYS instrument, and it no longer declares its own conversion:
the two codes derive it.

**`pnl_cash` is GROSS.** The `costs` block decides WHICH take-profit tranches
fire and never reduces the cash. §2 makes cost a non-goal and §8.1 says the
replay is handed a threshold rather than modelling execution economics; this is
the first place a reader could take the number for a net one.

**`notional_spent` is what the walk BOOKED, and with a trail it can land on
either side of the declared budget.** The quantity is fixed from the rung's limit
when the order is composed, so a trailing fire above the limit spends more than
the rung's share and one below it spends less. Without a trail the fill price is
`min(open, limit)` and the spend can only come in at or under the budget, which
is the case the rest of this paragraph is about.

The whole-share gap is a separate matter and points one way. The
drain buys whole shares (`floor(tier_notional / limit)`); the replay works in
fractional units, which is the step-1 plan's recorded scope cut and not a fact
the replay lacks, so it is not a `divergences` entry. It is not small. Measured
2026-09-28: on this ladder at a 1500 budget the drain spends 1482.50 against the
replay's 1500.00, a gap of 17.50; at 8000 on rungs 120.00 and 115.00 the gap is
130.00; at 1000 on rungs 196.13 and 175.40 it is 60.81, and there the entry anchor
moves too — 187.83800 against 187.27654, which is 0.56 in price or about 30 bps,
and the denominator moves with it. Those figures are the PRE-buffer gap, measured at a rate
of 1.0 so that the two sides are in one currency.

Re-measured 2026-10-02, now that the walk applies the stated sizing buffer
(§5.2.1). The buffer is NOT a divergence — the drain withholds it too — so the
residual has two named components and only the second is a gap. Each row below
is this ladder (68.00 at 60%, 66.50 at 40%) on a 1500 budget, with both sides
in the INSTRUMENT's currency, which is what the rate is for:

| rate | buffer | the ladder splits | the drain spends | lattice gap |
|---|---|---|---|---|
| 1.0 | 0% | 1500.00 | 1482.50 | 17.50 |
| 1.0 | 1% | 1485.00 | 1416.00 | 69.00 |
| 1.08 | 0% | 1620.00 | 1550.50 | 69.50 |
| 1.08 | 1% | 1603.80 | 1550.50 | 53.30 |

The gap is not monotone in the buffer, and that is the row to read twice: at
1.08 adding the buffer SHRINKS it from 69.50 to 53.30, because the floor lands
differently on a smaller tier. So "the buffer makes the lattice worse" is false
as a general claim, and the construction has to travel with the number —
one rate and one buffer give one figure, not a direction.

Every figure in this paragraph and in the example's summary above is recomputed
by `apps/alphalens-research/scripts/record_intent_replay_fx_numbers.py`, which
no test pins because it IS the pin: the figures are prose, and prose goes stale
silently. Re-run it when a figure here moves.

**The denominator is not held away from zero.** On this block's own cash a
denominator of 1e-10 publishes an R near 2.9e10, and that is the document's
geometry rather than a defect. It cannot reach infinity: the denominator is a
difference of two prices of the same size, so its smallest non-zero value is one
unit in the last place — 7.1e-15 at a price near 63, which caps R around 4.0e14,
a large finite number. That is why `allow_nan=False` is enough to keep stdout
strict. Not positive is a different case and §5.1 handles it.

### 5.1 The denominator is the stop that was actually placed

`denominator.kind` is always `placed_stop`, and on both deployments the level
actually placed is `spec.disaster_stop` — in every document, including one that
supplies `exit.initial_levels`. `denominator.source` names it.

**When the denominator is NOT POSITIVE, `r_multiple.value`, `mfe` and `mae` are
`null` and the `denominator` object is still carried**, so a reader sees why
rather than a missing key. `allow_nan=False` forbids a NaN on the wire, so null
is the only form available; `/edge` answers the same way, returning no realized R
when its own risk is not positive.

This is not a paper case. The walk books a limit fill at `min(bar.open, limit)`
— a trailing fire prices elsewhere (§4.4) — and
places the stop only AFTER the first fill, so a bar that opens below
`spec.disaster_stop` fills beneath the floor. `validate_intent` constrains the
stop only against the rung LIMITS, never against an opening price. Measured
2026-09-28 over two bars: a rung at 64.00 filling at 64.00, then a bar
`50.00 / 51.00 / 49.00 / 50.00` that fills a second rung at 50.00 and stops out
at 50.00, gives `notional_spent` 893.7008, `avg_entry_price` 56.9725, a
denominator of −6.0275 and `pnl_cash` of −109.375. Without the rule that reports
**+1.1568 R** for a loss of 109 units.

"Not positive" rather than "negative", because exactly `0.0` is reachable too: a
bar opening exactly AT the disaster stop fills there, so the average entry is the
floor and the difference is zero. Dividing by it raises, rather than returning a
number anyone could argue about.

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
> `kind`/`value`/`unit`/`source`/`formula` object of §5.1. A config value that
> carries a MAGNITUDE carries the `value`/`unit` pair. A config value that is a
> plain run switch stays a bare scalar.

The middle shape was missing from this rule until 2026-10-01, although the
`costs` block has used it since PR 3: `commission_rate`, `min_commission` and
`exit_edge_min_bps` are each a `value`/`unit` pair, so a rule admitting only the
other two shapes described none of them. The omission only started to matter
with #1592, whose new keys are magnitudes where the unit IS the question — a
round-trip cost given as `0.005` and as `0.5` differ by a hundredfold and both
read as plausible.

The anchor and the boundary rule live inside `formula` as resolved values rather
than as free-text sibling keys, because a reader can check a formula against the
numbers beside it and cannot check a label. There is no null form for "no
deadline" (§4.1): an earlier revision kept one from the days the block had inert
defaults, and PR 3 removed it. The rule exists because the alternative — a caller asserting "I
translated this path" with nothing able to check the assertion — is an echo with
a story attached, and §4.3.1 rules out echoes.

`snu_bars` serves the same purpose for the one assumption that is NOT
configurable (§4.4).

**`divergences` is the list a knob would have hidden.** It names, per run, each
place where the replay is known to differ from the daemon for a reason no
configuration value can close, because the replay LACKS a fact rather than a
setting. Four entries exist at v1.

| entry | what the replay lacks | emitted when |
|---|---|---|
| `daemon_trail_guards` | orders, so neither order-state guard can be evaluated (§3.2) | the declared policy moves the stop at all: `policy.trails or policy.requires_amend_stop` |
| `daemon_reanchor_latch_is_journal_lifetime` | the journal, so the daemon's idempotence latch cannot be reproduced | the reaction is `reanchor_on_fill` |
| `native_entry_trail_is_a_broker_model` | any local implementation of the ratchet and the fire to compare against (§8) — and, for the watch that PLACES the order, the quotes, the session boundaries and the account state its other gates read | the run states an entry-trail distance |
| `take_profit_observation_time` | the poll time and the quote (§4.4) | the resolved take-profit ladder is not empty |

`daemon_trail_guards` reads "trails" for a historical reason and covers BOTH
stop arms. `stop_decision._reanchor` checks `has_sole_standalone_stop` and
`amend_in_backoff` exactly as `_trail` does, and the walk supplies both
unconditionally, so a re-anchoring document is as optimistic as a trailing one.
§3.2 reads only `_maybe_trail` guard by guard, and that is a gap in this
document rather than in the code. A static run (`exit: null`) is the one case
where silence is honest: `_reanchor` returns at `if not policy.requires_amend_stop`
before either guard is reached.

`daemon_reanchor_latch_is_journal_lifetime` was added 2026-09-28. The daemon
holds `reanchored_by_uic`, a map from instrument to the average price a confirmed
re-anchor fired at, folded from JOURNAL lines, and suppresses a re-anchor when
the new average is within 1e-6 of the stored one. The replay computes the same
predicate from its own trace, with EXACT equality and only within one run, so it
differs twice over: in lifetime and in tolerance. The direction is not uniform —
a re-anchor can move the stop DOWN after a lower fill (§6.3) — so this entry
makes no claim about flattering.

`cost_gate_prices_the_account_currency` was added 2026-09-28 and **RETIRED
2026-10-02 with #1592**, in the commit that priced the leg. It said the replay
priced the stated budget in the ACCOUNT currency while the daemon priced the
whole-share notional in the INSTRUMENT's — two differences under one name, and
only the first was a missing fact. The currency is now stated
(`fx.instrument_currency`, §5.2.1) and the gate prices it, so the replay no
longer LACKS anything there, which is what an entry here has to assert. The
whole-share half is a recorded scope cut, and §5 says in terms that a scope cut
is not a `divergences` entry, so the row retires rather than being renamed.

What retiring it COSTS, stated because nothing in the result says it any more:
the 1.05, 15.24 and 38.10 bps this section attributed to the row belong to the
whole-share lattice, they survive this change unchanged, and after the
retirement no run reports them. That is correct under the membership rule above
and it is still a loss of reporting.

Two of the four flatter the result, which is why they are printed rather than
footnoted; `take_profit_observation_time` can run either way and the re-anchor
latch depends on the path. An entry is added by editing this section, on the same
reasoning as §4.3.1: a divergence that can appear without anyone writing it down
is a divergence nobody will find. The per-run column is part of that: an entry
printed on every run regardless of the document would carry no information, and
one printed on no run would be a promise rather than a report.

### 5.2.1 The FX facts the gate needs, and the one it cannot check

Decided 2026-10-01 under #1592, which listed two options: this block gains
stated FX facts rather than v1 being restricted to same-currency documents. The
other option would have refused every pick this account actually holds, since
the budget is in PLN and the instruments settle in USD — it removes the tool's
purpose rather than its gap. §8.1 carries the measurements; this section is the
key set.

| key | shape | unit | why it cannot be omitted | required |
|---|---|---|---|---|
| `fx.instrument_currency` | §5.1 translated value | ISO 4217 code | it is the settlement currency of `instrument.mic`, which no document path states (§4.3.1) and which no table in this repo maps from a MIC. The gate compares `min_commission` against a notional, and the two are in different currencies until this is stated | always |
| `fx.mid_rate` | §5.1 translated value, whose `unit` SPELLS the direction as `<instrument>_per_<account>` — for a PLN budget on a USD instrument, `USD_per_PLN` | that token, so the direction is part of the value | it makes `max(min_commission, commission_rate x notional)` commensurable, and the walk sizes with it | when the two currencies differ |
| `fx.round_trip_cost_rate` | `value`/`unit` pair, as `commission_rate` is | `fraction` | the daemon reads `FX_ROUND_TRIP_RATE` and §2.1 forbids inheriting it. In bps the term IS the rate, flat at every notional, so one stated fraction prices the leg | when the two currencies differ |
| `fx.sizing_buffer_pct` | `value`/`unit` pair | `percent` | the drain shrinks the budget by it before dividing, and the walk now applies it too (§8.1) | when the two currencies differ |

**Why the rate carries provenance when it translates no document path.** The
§5.1 shape is tied to translation, and a mid rate is not a document path
resolved — it is a market fact the caller supplies. It gets the shape anyway, for
a different reason, stated here so the rule is not quietly stretched: it is the
one value in the block that NOTHING in the run can check, so its `source` and
the as-of time inside `formula` are the only audit a later reader has. The
direction, by contrast, IS checkable, which is why it lives in the unit rather
than in prose: the token must read
`<fx.instrument_currency>_per_<spec.size.currency>` exactly, and anything else is
refused by string equality. It says the direction in words for a reason found
the hard way — this section first wrote it as the pair `USD/PLN`, which under
market convention reads as PLN per USD, the INVERSE of what it meant, and
`FxRateQuote.base_currency` names the account side and reinforces that reading.
A token meant to guard against an inversion read as the inversion. That check is what makes an inverted rate a refusal instead of a
silently flattering run (§8.1).

Five consequences, each one something the old block could get wrong in silence:

- **`fx_applies` stops being stated and becomes DERIVED** from
  `spec.size.currency` against `fx.instrument_currency`. A caller could
  previously declare `false` on a cross-currency document; §8.1 measures what
  that costs. A derived value cannot contradict the two codes it comes from.
- **`min_commission.unit` is CHECKED against `fx.instrument_currency`** instead
  of being a label nothing compares.
- **The gate's notional is the tranche's**, not the entry budget's (§8.1).
- **The symbolic unit `instrument_currency` is retired**, and `units.ISO_4217`
  replaces it as the unit of a stated CODE. §5 gives it to
  `avg_entry_price` and the R denominator precisely because no path states the
  currency; once `fx.instrument_currency` does, those fields carry a real code.
- **The result publishes the derived instrument-currency notional** beside the
  stated rate. The rate's magnitude is otherwise almost unobservable, and a tool
  that pays for provenance on a value it cannot check should at least print what
  the value produced.

**Three surfaces move with the implementation rather than here, and in each case
the reason is a test.** The §5 example's `config` block is read key by key by
`test_envelope` and compared against the built envelope, so a key written here
ahead of the code turns it red. The `divergences` table is pinned at five
entries, and `cost_gate_prices_the_account_currency` is retired by this decision
rather than added to. And `spec.size.currency` becomes *interpreted*, because
deriving the conversion is a read whose value can raise a refusal — but the class
cannot move before the read exists: patching the §4.3.1 column alone makes the
gate refuse the very document it describes. The classification gate also runs
inside the interpreter, which today receives no configuration at all, so the
implementation settles where the comparison lives before that row can change.

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

**The stream line has its own schema, published here 2026-09-28.** A line is a
transport envelope; the §5 result rides inside it as `data`:

```
{"schema":"intent_replay.stream/v1","type":"result","sequence":1,"data":{ …the §5 envelope… }}
{"schema":"intent_replay.stream/v1","type":"summary","sequence":2,"documents":1}
```

The alternative — putting `type` and `sequence` into the result envelope itself —
was rejected because it would leave one identifier, `intent_replay.result/v1`,
describing two different objects depending on a `--format` value the consumer
cannot see in `schema`. With the wrapper there is one identifier per shape,
`data` is byte-identical to what `--format json` prints, `type` is reserved for
transport so it can never collide with a result field, and a future `error` event
has somewhere to go without changing the line. The cost is real and small: a `jq`
filter over a stream needs a `.data` prefix that the single-value form does not.
Every line carries `schema`, the `summary` line included, so the summary's own
shape can be versioned when a stream longer than one document arrives.

`sequence` is the line's own 1-based index. Per-line identity is never
`intent_id`: two variants of one pick collide on the arming door's
`TICKER:DATE:manual`.

A single document is a stream of length one (§0), so this shape is what v1
prints. The INPUT shape for more than one document is not decided here.

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
parse at all (below, added 2026-09-25 by PR 4, carrying `details.path`),
`bars_malformed` for a BAR file the CLI cannot parse at all (below, added
2026-09-26 by PR 6, carrying `details.path`), and `entry_mode_unsupported`
for the `immediate` tranche v1 does not model
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
`not_positive`, `negative`, `unit_mismatch`, `empty_string`, `oco_unsupported`
(§8.1), `not_a_currency_code` (a stated currency that is not a three-letter
uppercase ISO 4217 code — the door holds the document's own code to that shape,
and the two are compared) and `buffer_out_of_range` (a sizing buffer at or above
100 per cent: at 100 the budget is zero and the walk divides by it, above 100 it
is negative and the run books a profit on a short the document never declared).
Both of the last two arrived 2026-10-02 with #1592, which also REMOVED
`fx_cost_not_stated` — a stated `fx_applies: true` whose round-trip rate the
block did not carry, added 2026-09-26 by PR 6 and retired once the rate became a
stated key. Missing keys win: when
keys are both missing and unusable, the run is
refused `config_incomplete` naming only the missing ones, and hears about values
on the next pass.

**A stated distance is a POLICY the walk applies, and there is no reason for
refusing one.** There was: `entry_trail_not_modelled` stood here while the walk
parsed the distance and ignored it, because echoing a stated value into the
result would have claimed a policy the run did not apply — the shape §4.3.1
refuses for `ceiling_price` and §8.1 for `oco`. The walk models the trail now
(§4.4), so the reason is gone with the refusal. The type and range checks are
unchanged and still run first, which is what keeps `true` a `wrong_type` and
`0` a `not_positive` rather than letting either reach the walk as a distance.

There is no UPPER bound either, and that is a decision rather than an omission.
The live rail caps the flag at 150 and its reader treats anything outside
`[0, 150]` as 0 — the three-limit ladder, which is the opposite policy — but a
deployment rail is not a fact of the document, and §5.2 publishes the key as an
integer `>= 1`. A larger value describes a run no deployment will make, and the
package README says so rather than the block refusing it.

One consequence for anyone reproducing a number from this document: the §5.2
example states `entry_trail_bps: 50`, so it is a TRAILING run. Its `snu_bars: 3`
and its cash are that run's, and the figures §4.4 and §6.3 quote from the
published template are the LIMIT ladder's — they were measured with the distance
null and do not reproduce from the canonical block.

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
perfectly ordered and still carry a value nothing can compare. PR 6 widened
its reason vocabulary rather than adding codes, on the same reading:
`not_a_list`, `wrong_type`, `missing_key` and `unknown_key` all say "this is
not a usable bar", which is the mode the code already names. `unknown_key` is
there for the reason `config_invalid` has it — a misspelt `hgih` dropped
silently would be a bar the author did not send.

**`bars_malformed`** is the shape `config_malformed` has, again, for the BAR
file (`not_json`, `duplicate_key`, with `details.path`); added 2026-09-26 by
PR 6. The four bar codes are the ENGINE's and describe the SEQUENCE and its
values, while a file that is not one JSON document is a CLI concept, and
putting a CLI concept in a leaf is the #1122 mistake. The published bar shape
is one JSON array of `{t, open, high, low, close}` objects.

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
| the native entry trail | the ratchet and the fire belong to the broker (§8), so there is no local implementation of THOSE to disagree with. The watch that places the order is ours, and one member of it — the depth rule that hands a move to the next-listed rung — is modelled, so parity could in principle reach that one member; the rest of the watch reads quotes, sessions and account state a replay lacks |
| the two order-state guards | they are inapplicable rather than implemented differently (§3.2); a parity run would have to invent the very facts a replay lacks |
| the intra-bar tie convention | it resolves what the data cannot say (§4.4). Both answers are consistent with the bar, so no test distinguishes a right one |

Each is covered instead by a named divergence in the result (§5.2) or by
`snu_bars`. That is weaker than a test and is the honest strength of the
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
- the placed stop is monotone non-decreasing WITHIN one average fill. Across
  fills it is not, and this section said otherwise until 2026-09-26: the
  re-anchor arm clamps against `plan_stop` rather than against the level
  standing, so a later and lower rung fill re-anchors LOWER — run during PR 6,
  66.20 then 65.79643916913948 on rungs 68.00 and 67.00 with `k_atr` 1.5,
  `atr` 1.20 and `spec.disaster_stop` 63.00. This bullet said 65.70 until
  2026-10-01; that is the EQUAL-UNITS level, and the walk weights the average by
  budget over limit, which `test_a_later_rung_fill_reanchors_LOWER` has recorded
  since PR 6. The floor is part of the measurement, because the clamp refuses
  any target below it: with a floor in (65.79643916913948, 66.20) only the FIRST
  re-anchor fires, and at or above 66.20 neither does. Both endpoints are OPEN
  and this bullet had both wrong until 2026-10-01 — swept that day and pinned by
  `TheFloorDecidesHowManyReanchorsFireTest`: a floor EQUAL to the second level
  still fires both, and a floor equal to 66.20 fires neither, because
  `_decide_stop` returns early when the target equals the stop standing.
  The epoch-scoped form is not what the properties assert, and 2026-10-01 is
  why. Scoping to an average fill is VACUOUS on the re-anchor arm — the latch
  allows one move per average, so 0 of 14 913 generated runs ever put two in
  one epoch and a one-element sequence is monotone by construction — and WEAKER
  THAN TRUE on the trail arm, whose ratchet persists across fills. It would
  also force a test to rebuild the epoch from `cash / units` plus the latch
  predicate, which is the production arithmetic it is checking. So each arm
  carries its own property: the trail is monotone across the whole trace, and
  the re-anchor arm emits no more moves than there are fills.
  The trail arm IS monotone across the whole trace, because its ratchet
  compares the CLAMPED level against the last trailed one. The replay
  reproduces both arms rather than adding a never-down rule of its own, which
  would be the idealised-policy defect §3.2 exists to remove;
- the sign of `pnl_cash` agrees with the sign of `r_multiple`;
- `r_multiple` satisfies `r == (pnl_pct_of_spent / 100) × avg_entry_price /
  denominator` whenever the denominator is positive. The identity follows from
  `pnl = proceeds − spend` and `spend = units × avg` alone, so it holds for any
  definition of R and needs no knowledge of how the summary computed it. Added
  2026-09-28, after this document's own §5 example printed an R that the identity
  refutes;
- `mfe` is never negative and `mae` never positive BEYOND ONE ULP of the average
  entry, whenever the denominator is positive. Every fill lies inside its own
  bar, and that is what the argument needs rather than any one formula: a limit
  fill is `min(open, limit)`, which is at most the open; a trailing fire is
  `max(open, trigger)` where the fire condition already gives `trigger <= high`,
  and when the trigger is above the open it is above the low as well. So the
  price is never outside `[low, high]` either way. The extremes are
  tracked from the first fill onward, so the trough never exceeds the average
  entry and the peak never falls below it. That argument is about the FILL
  PRICES. The average is `cash / units`, and the division can land one ulp below
  a fill price the trough then equals, which is why the bound is structural and
  not strict. This bullet claimed the strict form until 2026-09-29. Measured that
  day: one run in 200 000 produced `mae` at `+6.447756222868429e-16`, and it
  reproduces from a single rung at `limit_price` 52.37 for a `notional` of
  2587.43, a floor of 41.35, and one bar 52.60/52.63/52.37/52.49. A strict
  `mae <= 0` would go red at that rate. Rounding the notional to 1270.85 makes
  the effect vanish, so an implementation quoting a shortened figure would look
  refuted.
  A tolerance of "one ulp of `avg_entry_price`" is NOT the bound, and this
  bullet said it was until 2026-10-01. It is wrong twice. The UNIT: `mfe` and
  `mae` are `(extreme - average) / denominator`, so they are in R, while a ULP
  of a price is in the instrument's currency — the R-space form is wrong by a
  factor of the denominator, and two rungs on one bar 61.05/61.05/60.87/61.05
  with a floor of 60.88 refute it directly (`mfe` at -4.179663151529959e-14,
  5.9x one ULP of the average). The MAGNITUDE: one ULP is not a bound in price
  space either, because the average sums cash and units over the fills, so the
  rounding grows with their number — three rungs filling at one price put the
  trough exactly TWO ULPs above the average. So the property is asserted on the
  FILL PRICES, where the argument above already lives and no tolerance is
  needed: the trough is at or below every fill price and the peak at or above
  every one. Zero violations in 8798 filled runs, including the case that
  refutes the average form. Pinned by
  `ExcursionsNeverCrossTheFillPricesTest` and its three witnesses.
  `/edge` can produce the opposite signs by a mechanism this engine does not have
  — it books a fill AT the limit even on a bar that traded entirely below it — so
  a test asserting THAT would still assert nothing;
- a document with `exit: null` produces zero `stop_moved` events;
- bars that touch no entry produce `outcome: "no_fill"` and zero cash. Under a
  trail "touch" splits in two and the property is about the second: reaching a
  rung ARMS an order, and only reaching its trigger fills one, so a run whose
  bars touch every rung and never retrace far enough is `no_fill` too;
- `snu_bars` is positive ONLY when at least one bar's ordering the tape cannot
  settle changed the money. The converse does NOT hold, and this bullet claimed
  it until 2026-09-29: a biconditional cannot be implemented by the single pass
  that also produces the cash, because detecting every SNU means evaluating the
  readings it did not take. ONE shape is known to go uncounted, and it is
  measured: a bar on which the position OPENS through a rung below the open,
  where nothing is held so the cost gate has no entry price to measure a tranche
  against (worth 18.045113 on the published template). It is pinned by
  `test_a_bar_that_opens_the_position_is_a_known_blind_spot`.
  Until 2026-09-29 this bullet named a SECOND uncounted shape: a bar where the
  cost gate's own verdict flips with the order, because a deep fill lowers the
  average entry and the threshold with it. That shape is COUNTED. The detector
  asks the gate on both sides of the deep fill and reports the bar when the two
  verdicts differ, which `test_the_bar_counts_when_the_cost_gate_verdict_turns_on_the_order`
  pins at `snu_bars == 1`. The bullet described the detector as it stood before
  #1613 repaired it: #1614 wrote this section earlier the same day, #1613 changed
  the code and not the document, and nothing brought the two back together.
  A same-bar LIMIT fill and stop-out is not an SNU at all: the rung sits above
  the stop the document declared, so the order is forced rather than assumed.
  That guarantee is the limit's, not entry's in general — nothing constrains a
  trailing TRIGGER against the stop, so such a bar IS counted, by row 3's own
  predicate (§4.4);
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

- **Pricing an SNU instead of counting it is an OPEN QUESTION, not a roadmap
  item, and the literature is why.** The obvious next step is to carry both
  readings of an SNU forward and report the spread — what Maier-Paape and Platen
  call running `wc` and `bc`, and what econometrics would call reporting an
  identified set rather than a point. Two things have to be settled before it can
  be promised. First, whether a worst case is even WELL DEFINED here: Löw et al.
  restrict their results to at most one entry and one exit and say that beyond
  that "there would no longer necessarily be unique worst cases and best cases",
  and every document this tool takes is laddered on both sides. Second, whether
  the spread computed from the readings this engine can see would be a SHARP
  identified set or merely an outer one — the local-versus-global gap §4.4
  describes is exactly the reason to expect the latter. A spread that is neither
  well defined nor sharp, published beside a point estimate, would read as a
  bound and be none. Its own issue, and the issue's first job is that question,
  not the implementation.
- **Entry trailing is modelled as of 2026-09-25, and the model is of a BROKER
  rather than of our code.** An earlier revision called it the last policy still
  selected by a deployment environment variable. That was wrong twice: the OCO
  pair is selected the same way, and entry trailing is not selected by the flag
  alone — `entry_mode` decides which tiers reach the machine and the flag also
  decides which arm gates run. The serious half was simpler. On both deployments
  the flag is set, so an eligible pick rests a native trailing order PER TIER
  instead of the three limit entries, and the trail distance is nowhere in the
  document. A
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
  there is no local implementation of the ratchet or the fire to compare against.
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

  **Two of those five are switches, and only one of the two gates a magnitude
  the block carries; running the gate during PR 6 showed the difference
  matters.** `min_commission_applies` gates `min_commission`, which is stated.
  `fx_applies` gates a round-trip rate that is not, and the daemon reads a
  constant (`FX_ROUND_TRIP_RATE`, 0.0050) that §2.1 forbids inheriting. The
  omitted term is exactly 50 bps at every notional — the whole
  `exit_edge_min_bps` buffer of the §5 example, ten times over on a small
  tranche. So `fx_applies: true` was refused from PR 6 until #1592 landed
  (`fx_cost_not_stated`, since removed from §5.4). That was #1592's own standing
  rule rather than a new decision: "no later PR prices a cross-currency run with
  a constant or an implicit 1:1 rate; such a run is refused". The rule held —
  the run is now priced from a STATED rate, and no constant was inherited.

  **The refusal is not about the difficulty of stating a rate.** The FX term is
  a rate times a notional over that same notional, so in bps it IS the rate EM
  measured 50.0000 bps at notionals from 100 to 1 000 000, and the same in any
  currency. One stated fraction would price it. What `fx_applies: true` DECLARES
  is the problem: no document path states the instrument's currency (§4.3.1
  puts the settlement currency of `instrument.mic` out of scope), so that key is
  the only place a caller says the run is cross-currency — and on such a run
  the per-fill minimum is still priced on the ACCOUNT-currency budget, an
  implicit 1:1 of exactly the kind the rule above forbids. Pricing the
  conversion leg alone would satisfy the arithmetic and keep the forbidden rate.
  That is a third option #1592 does not list, and it belongs on that issue
  rather than in an implementation PR.

  **Decided 2026-10-01: option 1, with three measurements that shaped it
  (#1592).** The configuration states the instrument's currency, a mid rate, the
  round-trip FX cost rate and the sizing buffer (§5.2.1). `fx_applies` stops
  being declared and is DERIVED from the two currency codes, which removes the
  one thing the block could previously get wrong in silence — a caller stating
  `false` on a cross-currency document. `min_commission_applies` keeps its
  stated form for the reason given above: it gates a magnitude the block
  carries, and it is a fee-card fact rather than a currency fact.

  **One: the gate prices a TRANCHE, not the entry budget.** The walk passes
  `min(tranche.fraction * intended, held)`. On a 1020 position of 15 shares at
  68.00, under the 8 bps card with a 1.00 per-fill minimum and a 50 bps edge: one
  tranche prices 19.6078 bps on either reading — the only case where the two
  agree, and the shape of the §5 example, which therefore cannot discriminate
  them — while two tranches price 39.2157 against the budget reading's 19.6078,
  and five price 98.0392 against that same 19.6078. The budget reading always
  UNDER-prices, so it fires tranches the daemon declines.

  **Two: the stated rate's magnitude is nearly unobservable, and an inversion
  flatters this account.** Above the knee the fee is `2 x commission_rate` with
  the notional cancelling, so a rate stated upside down — 3.70 where 0.27027 was
  meant, a factor of 13.7 — leaves the threshold BIT-IDENTICAL at an 8000 budget:
  66.93889999999999 both ways. One threshold covered 200 000 notionals spanning a
  factor of 1000, with a range of exactly 0.0. The fee itself is pure ad valorem
  only to within one ulp — 15.02 per cent of those notionals differ from
  `2 x commission_rate`, by at most 1.0 ulp — which is why this claim is written
  about the THRESHOLD, the quantity the gate compares, rather than about the fee.
  Below the knee the rate does bite, and there an inversion restores the too-low
  threshold this change exists to remove. So the result publishes the DERIVED
  instrument-currency notional, in a top-level `fx` block beside the echoed
  configuration (§5): a reader who sees 29 600 against an 8000 budget catches
  the inversion by inspection, where 16.0 bps cannot. It is a top-level block
  and not a key inside the echoed `fx` because that echo has to round-trip
  through the parser, which refuses a derived key on input.

  **Three: the buffer cannot be applied in one place only.** The drain shrinks
  the budget before dividing. A buffer used by the gate and not by the walk would
  leave one run holding two position sizes, which is a result claiming a policy
  the run did not apply — the `oco` shape decided above. So the walk sizes on the
  stated rate and buffer too. The whole-share lattice is NOT folded in: §5 calls
  it a recorded scope cut and it stays one.

  **The gate is not exact even when the currencies match, and the boundary is
  the fee card's knee.** The replay prices the stated budget; the daemon prices
  the WHOLE-SHARE notional. Above `min_commission / commission_rate` — 1 250 on
  the US card — the fee is pure ad valorem and therefore scale-invariant, so
  the two thresholds are identical to full precision. Below that knee the
  per-fill minimum binds unequally on the two amounts: measured 2026-09-26, a
  budget of 1 000 against a whole-share notional of 950 is 1.05 bps apart, 250
  against 210 is 15.24 bps, and 100 against 84 is 38.10 bps, in every case with
  the replay's threshold too LOW, so it fires tranches the daemon declines. The
  whole-share floor is the step-1 plan's deferral and is recorded there.

  The cross-currency case USED to add its own error on top, and the block could
  not detect it: a caller stated `fx_applies: false` on a cross-currency
  document and nothing compared that claim against anything. Measured 2026-09-26
  on a PLN account at USDPLN 3.70: 0 bps on a full 8 000 PLN tranche, -11.75 bps
  at 2 667, -21.00 at 2 000 and -54.00 at 1 000, the same direction as the knee
  gap. That error is CLOSED as of 2026-10-02: the flag is derived from the two
  codes, so the declaration no longer exists to be wrong. The §5 worked example
  was such a document and now states its conversion. The knee gap above is NOT
  closed — it has no currency in it — and after the divergence retired (§5.2)
  nothing in the result reports it.

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
