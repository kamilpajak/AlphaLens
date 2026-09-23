# bracket-replay — a document-driven backtester

**Status:** DRAFT (design agreed in brainstorming 2026-09-23; not yet implemented)
**Date:** 2026-09-23
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
| placement | a new dependency-free leaf package, `apps/bracket-replay/` |
| language | Python, sharing the contract's own arithmetic |
| `/edge` | shared envelope shape now, no `/edge` code now |
| stop management | one implementation, extracted into the shared contract leaf |

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
- Not a parameter optimiser. Measured throughput is 779 documents per second
  per core over a 42-session minute-bar window (16 380 bars); a sweep of a
  thousand documents takes 1.3 seconds. Nothing here is performance-shaped.

---

## 3. Architecture

```
apps/alphalens-broker-contract/          shared leaf, dependencies = []
  broker_contract/
    trade_intent/{schema, codec, validate}
    exit_geometry/{levels, policy, registry}
    sizing, fx, constants
    stop_decision.py                     NEW

apps/bracket-replay/                     NEW leaf, depends only on the contract
  bracket_replay/
    bars.py          price-input contract
    interpreter.py   document -> pending orders
    walk.py          bar walk
    trace.py         event trace
    measures.py      measures with units
    envelope.py      versioned result envelope
    cli.py           one-shot CLI

apps/alphalens-pipeline/                 one possible client
```

### 3.1 Dependency rules

| from | to | allowed |
|---|---|---|
| `broker_contract` | anything | no — stdlib only, `dependencies = []` |
| `bracket_replay` | `broker_contract` | yes, and only this |
| `bracket_replay` | `alphalens_pipeline`, pandas, anything else | no |
| `alphalens_pipeline` | `bracket_replay` | yes |
| `bracket_replay` | `alphalens_pipeline` | never |

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

### 3.3 What the document does not say

Four execution facts are not in the wire document today:

| fact | where it lives today |
|---|---|
| entry trailing | `ALPHALENS_BROKER_ENTRY_TRAIL_BPS`; `_entry_trail_eligible` reads no document field |
| 52-week ceiling | computed client-side; `ceiling_price` is refused at the door |
| position time stop | no wire form, by decision |
| take-profit resting at the broker as an OCO pair | `ALPHALENS_BROKER_OCO_ENABLED` |

The replay takes these as an explicit run configuration, separate from the
document, each defaulting to the inert value. A default run therefore replays
only what the document actually carries, and the trace records which of the
four were active.

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

### 4.2 Flow

```
JSON -> contract codec -> TradeIntent -> validate_intent
                                             |
bars + run config -> interpreter -> pending orders
                                             |
                                     bar walk (stop_decision per bar)
                                             |
                          trace -> measures -> envelope
```

Decoding uses the contract's own codec, not a second parser.

### 4.3 The replay refuses what the door refuses

A document passes the published schema, the codec and `validate_intent` before
anything is computed. An incoherent document gets the same refusal code it
would get at arming, not a number.

This is the point of the design, not caution: backtesting a document that could
not be armed is once again an instrument measuring a policy the system will not
execute.

The replay does **not** check venue, pick key or generation. Those are queue and
deployment concerns, not policy.

### 4.4 Two ambiguities made explicit

**A bar that touches both the stop and a take-profit.** The existing replay
carries this as a known quirk. Here it is a named configuration field —
`sl_first`, `tp_first` or `unresolved` — defaulting to `sl_first` as the
pessimistic reading, and always recorded in the trace. It can flip the sign of a
result, so it may not be an invisible default.

**Bars out of order.** The replay REFUSES input whose `t` is not strictly
increasing, and refuses duplicates. This deviates from the existing replay,
which sorts silently. The deviation is deliberate: for a research tool, quietly
reordering a caller's input produces a wrong answer that leaves no trace.

### 4.5 Trace

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
  "schema": "bracket_replay.result/v1",
  "intent_id": "KO:2026-09-23:manual",
  "instrument": {"ticker": "KO", "mic": "XNYS"},
  "window": {"from_t": 1758000000000, "to_t": 1761000000000, "bars": 16380},
  "config": {
    "intrabar": "sl_first",
    "entry_deadline_t": null,
    "entry_trail_bps": null,
    "ceiling_price": null,
    "time_stop_t": null,
    "oco": false,
    "costs": "none"
  },
  "outcome": "closed_tp",
  "summary": {
    "filled_fraction": 0.6,
    "notional_spent":   {"value": 900.0, "unit": "EUR"},
    "avg_entry_price":  {"value": 67.83, "unit": "USD"},
    "pnl_cash":         {"value": 41.20, "unit": "EUR"},
    "pnl_pct_of_spent": {"value": 4.58,  "unit": "percent"},
    "r_multiple": {
      "value": 0.72,
      "unit": "R",
      "denominator": {
        "kind": "document_disaster_stop",
        "value": 1.63,
        "unit": "USD",
        "formula": "avg_entry_price - spec.disaster_stop"
      }
    },
    "mfe": {"value": 1.10,  "unit": "R"},
    "mae": {"value": -0.34, "unit": "R"}
  },
  "trace": []
}
```

### 5.1 Why the denominator is an object

Two results may be compared exactly when their `denominator.kind` matches —
which is machine-checkable, so a consumer can refuse a comparison instead of
drawing it.

Only one kind exists today: `document_disaster_stop`. `spec.disaster_stop` is
mandatory in every document, so this measure always computes and every result
the tool produces is comparable by construction. The field still names itself,
so a second kind can be added later without renaming anything.

The current `/edge` lenses carry two different denominators under one name
precisely because neither was ever named.

### 5.2 The config block travels in the result

The four facts of §3.3 plus the intra-bar resolution come back in the output, so
a result cannot be read out of the world that produced it. The absence of
exactly this is why every `atr_bracket_1p5` value stamped before 2026-08-24
describes a different policy than a reader would assume, discoverable only from
a memo.

### 5.3 Formats

| format | when | stdout |
|---|---|---|
| `json` | one document | exactly one JSON value |
| `ndjson` | a stream | one object per line, ending in a `summary` event |
| `human` | reading | a table, only when stdout is a TTY |

The trace is in the envelope for a single document and omitted for a stream
unless requested.

### 5.4 Errors

A faulty document gets the same code it would get at arming —
`intent_invalid` or `intent_malformed` with `details.reason` — because the
replay refuses what the door refuses, so it should refuse the same way. New
codes only for its own failures: `bars_unordered`, `bars_empty`,
`window_too_short`.

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
- two runs over the same input produce byte-identical output.

The last one matters more than it looks: a research tool that returns a
different number on a repeat invalidates every conclusion drawn from it.

### 6.4 Door agreement

A test pushes every example in `examples/manual-pick/` through the replay. What
the door accepts, the replay accepts. This is §4.3 made executable.

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

**Step 1 — the replay.** Extract `stop_decision` into the contract, build
`bracket-replay`, wire the CLI. The daemon is **not touched**; the parity test
holds the two implementations together.

**Step 2 — the daemon, separately and later.** `position_manager` drops its own
copy and calls the leaf. Its own PR, its own review, with the step-1 parity test
as the net, then retired per §6.1.

The split exists because the blueprint says protection-critical code is not
touched in passing. Step 1 changes not one line the live daemon executes.

---

## 8. Risks and open questions

- **Entry trailing cannot be replayed** until it becomes a document fact. It is
  the last policy still selected by a deployment environment variable rather
  than by the document — the shape #1414 removed for exits. Until then a run
  with entry trailing configured is a model of a policy the document does not
  declare, and the trace must say so.
- **`stop_decision`'s view type** has to be declared in the contract without
  dragging `ProtectionView` from the daemon. If the minimal view turns out to
  need more than prices and a stop history, that is a signal the extraction
  boundary is wrong, and the design should stop rather than widen the contract.
- **Intra-bar resolution is a modelling choice with no right answer.** Minute
  bars cannot say which level was touched first. `unresolved` exists so a study
  can measure how often it matters instead of assuming.
- **This tool cannot validate a policy.** It replays documents over past bars
  and is in-sample by construction. Nothing it produces is evidence of an edge;
  it answers "what would this have done", never "does this work".

---

## 9. Relation to epic #1526

They are related and disjoint. #1526 is about the `/edge` lenses and is blocked
on an owner decision about the stamped series. This tool touches `/edge` not at
all.

If this is built first, sub-issue 1 of #1526 — "one document-driven replay" —
becomes wiring rather than construction: the engine already exists, and what
remains is the per-row generator and the series decision.
