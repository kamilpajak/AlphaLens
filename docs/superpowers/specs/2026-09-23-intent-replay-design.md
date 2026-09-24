# intent-replay — a document-driven backtester

**Status:** DRAFT (design agreed in brainstorming 2026-09-23; revised after adversarial review the
same day; revised again 2026-09-24 after an arming-surface survey and a second adversarial review
that refuted several statements of the first revision — see §3.3, §4.3.1, §8; not yet implemented)
**Date:** 2026-09-23, last revised 2026-09-24
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
  the client, per the rule that broker economics live behind the adapter.
- Not a parameter optimiser. A PROXY measurement — a hand-written loop calling
  the real level functions, without order matching or trace allocation — ran a
  42-session minute-bar window (16 380 bars) at 1.3 ms per document. That is an
  UPPER BOUND on throughput, not a measurement of this design; the real
  interpreter will be slower, plausibly by an order of magnitude. The
  conclusion survives that penalty: nothing here is performance-shaped. Do not
  quote the figure as measured.

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
    bars.py          price-input contract
    interpreter.py   document -> pending orders
    walk.py          bar walk
    trace.py         event trace
    measures.py      measures with units
    envelope.py      versioned result envelope
    cli.py           one-shot CLI

apps/alphalens-pipeline/                 one possible client
```

The package is named for its INPUT, not for one of the policies it can replay.
An earlier draft called it `bracket-replay`, which would have stretched
"bracket" over documents that declare a trailing stop and no bracket at all —
the same mislabelling as the closed #1160.

### 3.1 Dependency rules

| from | to | allowed |
|---|---|---|
| `broker_contract` | anything | no — stdlib only, `dependencies = []` |
| `intent_replay` | `broker_contract` | yes, and only this |
| `intent_replay` | `alphalens_pipeline`, pandas, anything else | no |
| `alphalens_pipeline` | `intent_replay` | yes |
| `intent_replay` | `alphalens_pipeline` | never |

Two independent barriers enforce this. `dependencies = []` in `pyproject` fails
the build. An AST gate modelled on the existing
`tests/test_module_dependencies.py` catches the lazy in-function import that the
first barrier does not see.

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

Nothing here is broker-order-shaped: the two order-state questions cross as
booleans. If the extraction turns out to need more than this, that is a signal
the boundary is wrong and the design stops rather than widening the contract.

### 3.3 What the document does not say

Four execution facts are not fully stated by the wire document today. An earlier
revision of this section listed them as one class. They are four different
things, and the differences decide how the replay must treat each.

| fact | wire form | what it does today | consequence for the replay |
|---|---|---|---|
| entry trailing | **partial**: `EntryTierSpec.entry_mode` decides which tiers reach the machine and `spec.disaster_stop` is required to arm it, but the trail distance is not in the document | **places a native trailing order at the broker**, and on the deployed configuration it is the path every armed pick takes | the largest gap in this design: the replay has no model of the path positions actually enter through |
| 52-week ceiling on a take-profit | **has one**: `ReanchorOnFill.ceiling_price`, modelled by the codec and refused at the door | no consumer on the live broker path; the only code that computes it reads a brief column | the one fact of the four that becomes a document fact by LIFTING a refusal rather than by designing a field (§4.3.1) |
| position time stop | none, by decision | no live consumer since ADR 0012 removed the paper-trade harness; `TIME_STOP_DAYS = 42` is read only by the `/edge` replay | a measurement convention of another instrument, not an execution rule — a run that applies it is not replaying the document |
| take-profit resting at the broker as an OCO pair | none | gated by `ALPHALENS_BROKER_OCO_ENABLED`, default off; **retired on SIM by decision** (position-attached exits do not work on a netting account) and unset on LIVE | changes WHO resolves the exit, which this design cannot yet express (below) |

Three things the table cannot carry:

**"Defaults to the inert value" means something different in every row.** For
the OCO pair it means a working capability that a deployment decision retired.
For the time stop it means a number belonging to a different tool. For entry
trailing it means switching off the path the money currently takes. A run
configuration that presents all four as equivalent switches invites a reader to
treat a default run as neutral, and for entry trailing it is not.

**All four feed §4.4, not only one.** Each adds or moves a level, so each
changes how often the tie convention has to decide and therefore what
`ambiguous_bars` reports. Entry trailing is the hardest: its trigger is a
running low that ratchets down, plus a distance, so whether it fires inside a
bar depends on whether the low came before the retrace — which OHLC cannot say.
That is a third row of §4.4's table, not an exception to it, and §4.4 already
anticipates it.

**The OCO row is a modelling gap, not a switch.** A pair resting at the broker
resolves on a touch; an engine resolves at its next observation. This document
does not state what that observation is relative to a bar, so there is nothing
here to configure yet. §8.1 is where that belongs.

The replay takes these as an explicit run configuration, separate from the
document, each defaulting to the inert value, and the trace records which were
active. That is correct for three of the rows and insufficient for entry
trailing, which §8 states as a risk rather than hiding in a default.

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
them does not read the field at all. §8.1 states that as an open decision. So
the run configuration carries the deadline with its provenance, in the shape
§5.1 already uses for the R denominator, and `spec.order_ttl_days` is a
TRANSLATED path in the sense of §4.3.1 rather than an unread one.

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
arming.

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
- **out of scope** — a queue or deployment concern the replay deliberately
  ignores. Today: `meta.generation`, and the venue and pick-key checks §4.3
  already declines.

*Interpreted* is not listed, because the interpreter proves that class at
runtime by actually reading the path. The other two ARE listed, here, and this
table is the whole of them:

| path | class | why |
|---|---|---|
| `instrument.ticker` | out of scope | identity. The walk is over the bars it is handed; nothing resolves a symbol |
| `instrument.mic` | translated (calendar) + out of scope (fee card, settlement currency) | the calendar reaches the replay through `entry_deadline` (§4.1); costs are a non-goal per §2 and `spec.size` is already in the account currency |
| `spec.size.currency` | out of scope | it labels the unit of the cash answer; it changes nothing the replay does, and §4.3.1 does not count a label as interpretation |
| `spec.order_ttl_days` | translated | sessions the leaf cannot count (§4.1) |
| `meta.generation`, `meta.armed_ts`, `intent_id` | out of scope | queue and identity concerns, the same ones §4.3 already declines |
| `meta.schema_version`, `spec.schema_version` | out of scope | the door is the only gate that reads a version; the codec and `validate_intent` stay version-blind on purpose, and so does this |

One path is interpreted in a way a reader can miss: `spec.side` is pinned to
`long`, and gate 4 of §4.3 refuses anything else with `side_not_long`. Raising a
refusal is interpretation by the definition above, so `side` needs no row — but
only because the gate runs. A replay that skipped `validate_intent` would walk a
short document as a long one.

A path that is neither read by the interpreter nor in this table is refused, and
the refusal names it (`path_unclassified`, §5.4). Two consequences are intended.
`meta.source` is absent from the table AND unread, so every document refuses
until §8.1's day-1 anchor is decided — the gate is doing its job, not
malfunctioning. `meta.trade_date` refuses with it and for the same reason: the
two together are what the anchor is computed from, so neither gets a class until
that decision lands. And adding a row is an edit to this section, so the set of
things this tool quietly does not honour cannot grow without someone writing it
down.

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
a named convention, not a configuration field. Two situations fall under it
today, and both are already resolved this way by the existing replay:

| one bar touches | resolution | why this is the worse one |
|---|---|---|
| the stop and a take-profit | the stop | +1R becomes −1R |
| a lower entry rung and the stop | fill first, then stop out | without the fill there would be no loss |

A third situation added later is resolved the same way; the convention covers
the class, not the two cases.

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
  "intent_id": "KO:2026-09-23:manual",
  "instrument": {"ticker": "KO", "mic": "XNYS"},
  "window": {"from_t": 1758000000000, "to_t": 1761000000000, "bars": 16380},
  "config": {
    "entry_deadline": {
      "kind": "order_ttl_sessions",
      "value": 1760976000000,
      "unit": "epoch_ms_utc",
      "source": "spec.order_ttl_days",
      "formula": "session_close_utc(advance_trading_sessions(2026-09-24, 7, XNYS))"
    },
    "entry_trail_bps": null,
    "ceiling_price": null,
    "time_stop_t": null,
    "oco": false,
    "costs": "none"
  },
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
        "source": "exit.initial_levels.stop",
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

`denominator.kind` is always `placed_stop`, and the level it measures to is
`exit.initial_levels.stop` when the document supplies one, `spec.disaster_stop`
otherwise. `denominator.source` names which.

An earlier draft made the denominator `spec.disaster_stop` unconditionally, on
the argument that it is mandatory in every document and therefore always
computable. That was wrong, and wrong precisely for the documents this tool
exists to study. `_geometry_tranche_ladder` journals `initial_levels.stop` as
the plan stop, and the never-naked cover places THAT. For any document
supplying its own levels, `spec.disaster_stop` is a number nothing protects the
position with, so an R measured against it names a risk nobody took.

Comparability survives because the KIND is one concept — the distance to the
stop actually resting — even though its derivation differs per document. The
current `/edge` lenses carry two different denominators under one name; this
carries one denominator that says where it came from.

### 5.2 The config block travels in the result

The four facts of §3.3 come back in the output, so a result cannot be read out
of the world that produced it. The absence of exactly this is why every
`atr_bracket_1p5` value stamped before 2026-08-24 describes a different policy
than a reader would assume, discoverable only from a memo.

The block carries two more keys, and neither is decoration. `costs` belongs with
the four, not with the measurement settings: §8.1 shows the cost gate decides
WHICH take-profit tranches fire, so a costless run diverges in events and not
only in cash. `entry_deadline` is the one TRANSLATED path of §4.3.1, and it
follows a rule the rest of the block does not need:

> A config value that TRANSLATES a document path carries the
> `kind`/`value`/`unit`/`source`/`formula` object of §5.1. A config value that is
> a plain run switch stays a bare scalar.

The anchor and the boundary rule live inside `formula` as resolved values rather
than as free-text sibling keys, because a reader can check a formula against the
numbers beside it and cannot check a label. `null` remains the form for "no
deadline". The rule exists because the alternative — a caller asserting "I
translated this path" with nothing able to check the assertion — is an echo with
a story attached, and §4.3.1 rules out echoes.

`ambiguous_bars` serves the same purpose for the one assumption that is NOT
configurable (§4.4).

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
`window_too_short`, and `path_unclassified` for the gate of §4.3.1, carrying
`details.paths`. That refusal needs its own code rather than borrowing
`key_discarded`: at the door `key_discarded` means the codec lost a key, which
is a different fact and would send a reader to the wrong place.

Errors go to stderr, stdout stays empty, the exit status is non-zero: `0` ok,
`2` usage, `1` everything else. Suggestions are an `argv` array.

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
  refuses and both tests go red at once;
- two runs over the same input produce byte-identical output.

The last one matters more than it looks: a research tool that returns a
different number on a repeat invalidates every conclusion drawn from it.

### 6.4 Door agreement

A test pushes every example in `examples/manual-pick/` through the replay. What
the door accepts, the replay accepts, including the round-trip gate of §4.3.

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

- **Entry trailing cannot be replayed, and it is the path production uses.**
  An earlier revision called it the last policy still selected by a deployment
  environment variable. That was wrong twice: the OCO pair is selected the same
  way, and entry trailing is not selected by the flag alone — `entry_mode`
  decides which tiers reach the machine and the flag also decides which arm
  gates run. The serious half is simpler. On both deployments the flag is set,
  so an eligible pick rests a native trailing order instead of the three limit
  entries this design models, and the trail distance is nowhere in the document.
  A replay of such a document therefore describes an entry ladder that did not
  happen. This is the one item on this list that could be a reason to change the
  architecture rather than a gap to note, and deciding that is out of scope
  here.
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

### 8.1 Capabilities this design does not yet handle

Found by a multi-agent survey of the arming surface on 2026-09-24 and each
confirmed against the source before being written here. The heading carried a
count until the list grew, which is the usual fate of a count in a heading.

Some of these move the answer in the FLATTERING direction — a replay that
silently ignores them reports a better number than the daemon would have
produced, which is the failure mode hardest to notice. The rest are not
directional at all; they simply have no defensible default, which is why each
needs deciding here rather than at the keyboard.

- **`entry_mode: "immediate"` is a second entry shape, and the design knows only
  the first.** `EntryTierSpec.entry_mode` (#1247) takes `"pullback"` — a resting
  rung below the market — or `"immediate"`, a tranche the daemon buys AT DRAIN,
  for which `limit_price` is the operator's CAP rather than a pullback level. A
  bar walk asking "did the low touch the limit" replays an immediate tranche as
  a resting order that waits, which is not what happens and not what it costs.
  `entry_mode_unknown` is a live refusal code, so the vocabulary is enforced —
  the replay simply has no model of its second member.

- **The reaction primitives need order-state inputs a replay does not have.**
  §3.2 lists `has_sole_standalone_stop` and `amend_in_backoff` as required
  fields of the minimal view, and insists they cross as the PREDICATE's result
  rather than as order legs. A replay has no broker orders, so it must supply
  both — and this document never says what. The choice is not a detail: `False`
  means the trail never fires, `True` means it always does. That is the whole
  result. Neither is obviously right, which is why it needs deciding here rather
  than at the keyboard.

- **A take-profit tranche is gated on clearing its own cost, and the replay has
  no cost model.** `live_exit_engine._exit_clears_cost` refuses to fire a target
  that does not cover the round trip. §2 makes cost a non-goal and the envelope
  says `"costs": "none"`, which is coherent for MEASURING cash — but the gate is
  not accounting, it is a REFUSAL that changes which exits happen. A costless
  replay fires tranches the daemon would decline, so the trace diverges in
  events, not only in cash.

- **The day-1 anchor depends on `meta.source`, which the design never reads.**
  `control_loop` passes `day1_includes_trade_date=source == "manual"` at two
  sites: a manual pick counts `meta.trade_date` itself as day 1, a brief pick
  starts the session after. That is a full session of difference in where the
  walk begins, on every hand-authored pick — and hand-authored picks are exactly
  the documents with `initial_levels`, the shape this tool exists to study.
  `meta.source` is therefore a required path that §4.3.1 gives NO class, so the
  gate refuses every document until this is decided. That is the gate working:
  the alternative is an anchor chosen at the keyboard and never written down.

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

None of these is a reason to change the architecture, with the entry-trailing
risk of §8 as the possible exception. Each is a decision the implementation
would otherwise make silently — some in the direction that looks better, the
rest with no defensible default at all.

---

## 9. Relation to epic #1526

They are related and disjoint. #1526 is about the `/edge` lenses and is blocked
on an owner decision about the stamped series. This tool touches `/edge` not at
all.

If this is built first, sub-issue 1 of #1526 — "one document-driven replay" —
becomes wiring rather than construction: the engine already exists, and what
remains is the per-row generator and the series decision.
