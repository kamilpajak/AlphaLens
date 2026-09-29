# intent-replay

A research backtester that takes a `TradeIntent` document (the same JSON that
`alphalens broker arm` accepts) plus price bars, and reports what the document
would have done.

It is a research tool. It stamps nothing, charges no multiplicity budget, and
carries no accrued history. Every value it needs is read from the document or
stated in the run configuration; nothing is inherited from a deployment, an
environment variable or a production constant. A missing value is a refusal,
never a default.

## Run configuration

Everything the replay needs beyond the document is stated in one JSON block
(spec section 5.2), parsed by `intent_replay.config.RunConfig.from_jsonable`
and rendered back into the result by `to_jsonable`:

| key | form | meaning |
|---|---|---|
| `entry_deadline` | `{kind, value, unit, source, formula}` | the entry-order cutoff, epoch ms UTC, with the rule that produced it; never null |
| `walk_start` | the same shape | the first timestamp of the walk, with the rule that produced it |
| `entry_trail_bps` | `null` only, in this version | the native entry-trail distance. A well-formed distance is REFUSED until entry trailing is modelled (`entry_trail_not_modelled`); the type and range checks still run first |
| `ceiling_price` | number > 0, or `null` | the take-profit cap |
| `time_stop_t` | epoch ms UTC, or `null` | the position time stop |
| `oco` | `false` | v1 models no OCO pair; `true` is refused |
| `costs` | five keys, see the spec | the threshold the take-profit cost gate compares against |

Inside `costs`, `fx_applies` must be `false`. A conversion costs 50 bps of the
notional round trip and no key in this block states that rate, so a run that
accepted `true` would price every take-profit tranche too cheap. Pricing it is
issue #1592.

A missing key is `config_incomplete` (`details.keys` names every missing key).
A stated value nothing can use — the wrong type, a non-finite number, a wrong
unit, an unknown key, `oco: true`, `costs.fx_applies: true`, or an
`entry_trail_bps` distance this version does not apply — is `config_invalid`.
The full reason table is with the refusal codes below. Nothing is defaulted.

## Command line

```
intent-replay run DOCUMENT --config PATH --bars PATH [--format json|ndjson]
intent-replay schema [COMMAND] [--format json]
```

`DOCUMENT` is a TradeIntent JSON file, the same bare document `alphalens broker
arm` takes, or `-` to read it from stdin. `--config` names the run configuration
block above. `--bars` names the price input: ONE JSON array of
`{t, open, high, low, close}` objects, `t` in epoch milliseconds UTC, strictly
increasing, with no key a bar does not model and no key missing. The four prices
must agree with each other: the high is not below the low, and the open and the
close are inside `[low, high]`. A bar is refused rather than repaired, because a
quadruple that contradicts itself would book trades at prices the bar never
carried — an open of 50.00 under a low of 67.00 fills a rung at 50.00. It is required,
because there is no bar series to invent and a run without one would answer
about nothing. `--format` takes `json` or `ndjson` (spec section 5.3: no human
renderer in v1). `intent-replay schema` prints a JSON description of the command
tree, its options, exit codes and failure codes, so a script or an agent need not
parse `--help`.

```json
[{"t": 1790170200000, "open": 68.0, "high": 68.2, "low": 67.9, "close": 68.0}]
```

The command runs the gates of the arming door in the door's order: derived
fields, the published input JSON Schema, completion, the codec, the fixed
point, `validate_intent`. Which document each gate judges is load-bearing and
is the same split the arming door makes. The first two and the fixed point
judge what the AUTHOR wrote, because the input shape does not describe the
fields a door derives and because the fixed point exists to catch what the
author sent and did not get back; the codec and `validate_intent` judge the
completed document.

The document is then INTERPRETED, which is where the fifth gate runs. The
interpreter reads the entry ladder into resting rungs (each with its share of
`spec.size.notional_acct` in the account currency — no share quantity, because
the FX rate and the venue's quantity lattice are not stated), the declared exit
levels, the take-profit ladder and the declared reaction, and it records every
path it read. `intent-replay` then refuses a path no class covers
(`path_unclassified`) and an entry tier whose mode it does not model
(`entry_mode_unsupported`). Nothing here says which declared level would REST
at a broker: the walk places `spec.disaster_stop`, because that is the level
that rests at the broker in both deployments, and carries
`exit.initial_levels.stop` without placing it.

Then the configuration block is parsed, and the bars last: each bar's shape, the
ordering of the sequence, and whether the window covers the stated `walk_start`.
Then the walk runs and the result is printed. `--format json` writes exactly one
JSON value on stdout; `--format ndjson` writes two transport lines. Both leave
stderr empty. The next section describes what is in the result. A refusal is
exactly one JSON object on stderr, the last line, with stdout empty; exit status
`0` accepted, `2` usage, `130` interrupted with nothing written, `1` everything
else.

Two choices inside the walk are worth stating here, because both are models and
not copies of a live system. The stop decision is taken at each bar's HIGH: the
minimum-distance clamp in `broker_contract.stop_decision` is anchored on the
price handed to it, the daemon polls many times inside one bar and its ratchet
keeps the best level any poll produced, so the high is the only choice that
reproduces the level the spec publishes for its own example. And a take-profit
does not rest at the broker in this model, so a bar that gaps above a tranche
fills AT the tranche's level, not at the open; the gap rule reaches only the
legs that really rest there, a rung and the disaster stop.

A third choice is forced on the walk, and since 2026-09-29 it IS published —
as a count, never as a claim. On one bar a rung and a take-profit can both be
touched: the low reaches the rung, the high reaches the tranche, and the bar
does not say which came first. The literature calls such a bar an SNU, a
"situation which is not unique" (arXiv:1412.5558). The walk fills entries
first, the same order it uses for every other pair, and `snu_bars` counts the
bar when that order decided money.

**No pessimism is claimed for this one, and that is a correction.** Measured on
the published template's ladder (rungs 68.00 and 66.50 carrying 60% and 40% of
1500, one tranche of 100% at 68.50, disaster stop 63.00) with a bar
`open 67.00 / high 68.60 / low 66.40` and then a bar falling to 62.00: filling
first nets **+37.898054**, taking profit first nets **+19.852941**. The gap,
18.045113, is exactly the deeper rung's profit.

An earlier version of this paragraph printed **-84.52** for the second reading.
That number came from running the tranche before ANY rung had filled, which is
not an order the bar permits, and it is retracted. It also said "no order of
steps can keep both rows and also put take-profits before rungs" — also false;
making the order conditional on whether the bar reaches the stop does exactly
that. What is true is narrower and is why the convention stops here: a
take-profit whose clamp sweeps the whole position ENDS the trade, so the two
readings can differ in whether the position survives, and then neither is a
bound. Section 4.4 has the three bands and the measurements.

`ceiling_price` is accepted in the configuration block and not read. A document
that carries a take-profit ceiling is refused by `validate_intent`
(`ceiling_price_unsupported`), and on the live side only the producer-side
bracket builder reads one, so a replay that capped tranches with it would apply
a rule no deployment applies.

The published templates in `apps/alphalens-broker-contract/examples/manual-pick/`
carry no `meta.trade_date`. The arming door fills it from its clock and the
venue's calendar; the replay has neither, so the date is stated in the document.
Add one key to a template before replaying it:

```json
"meta": {"source": "manual", "trade_date": "2026-09-23"}
```

**Replaying an armed pick.** A journal line already carries the date. Turn it
back into a document by removing only what the door derived (the recipe below
is run literally by the tests):

<!-- replay-copy-recipe -->
```bash
set -o pipefail
jq -cR 'fromjson? | select(.ticker == "KO" and .status == "armed") | .intent
        | del(.intent_id, .meta.armed_ts, .spec.tp_tranches[]?.r_multiple)' \
  ~/.alphalens/broker_orders/sim/picks.jsonl | tail -n 1 > ko.json
```

This differs from the copy recipe in the contract README, which also deletes
`meta.trade_date` and `meta.generation` because the door gives a NEW pick a new
date; the replay wants the date the pick was armed under.

In this version `run` can refuse with every code in the table below except
`path_unclassified`. The bar codes became reachable from the command when it
started taking `--bars`; the engine owns the SHAPE of a bar, so a file that
parses and is not a JSON array of bars is `bars_invalid`, while a file that is
not one JSON document at all is the CLI's own `bars_malformed`.
`path_unclassified` is wired in and no document can provoke it today: once
the interpreter has read the paths it reads, every path of the published input
schema is classified, the one path that is not
(`spec.tp_tranches[].r_multiple`) is refused as a derived field, and any other
key an author adds is refused by the fixed point. It is a tripwire for the day
the contract grows a field — which is exactly what it is for.

## Result

An accepted document prints the result envelope. With `--format json` that is
exactly one JSON value; with `--format ndjson` it is one `result` line and one
`summary` line, each carrying `"schema": "intent_replay.stream/v1"`. The
`result` line's `data` is byte-identical to what `--format json` prints, so a
caller can move between the two without a second parser.

The envelope has ten keys, in this order:

| key | what it holds |
|---|---|
| `schema` | `intent_replay.result/v1` |
| `intent_id` | the sentinel `REPLAY`; a replayed document has no arming identity |
| `instrument` | the document's own `ticker` and `mic` |
| `window` | the bar series that was handed in: `from_t`, `to_t`, `bars` |
| `config` | the configuration block, echoed key for key |
| `divergences` | the places this run is known to differ from the daemon |
| `intrabar_rule` | the name of the order the walk resolves a bar in |
| `outcome` | one of `closed_stop`, `closed_tp`, `closed_time_stop`, `open`, `no_fill` |
| `summary` | the nine measures below |
| `trace` | every event the walk emitted, in order |

**`intrabar_rule` names the rule; it does not bound the cash.** The value is the
constant `entries_then_stop_then_ladder`. On some bars the tape does not say
which of two things happened first, and the walk has to decide. `intrabar_rule`
says how it decided, and `snu_bars` says how often it had to. `pnl_cash` is one
outcome under one stated rule. It is not a conservative number, not a lower
bound, and not a worst case. Whether a best/worst envelope is even well defined
for a laddered document is an open question (issue #1616); this version promises
nothing about it.

**`snu_bars` counts bars, and the count only goes one way.** A positive count
means at least one bar's ordering changed the money. A count of zero means no
such bar was DETECTED, which is weaker than none having occurred, so it is not a
certificate. One shape is known to go uncounted and is measured: a bar on which
the position OPENS through a rung below the open, where nothing is held yet, so
the cost gate has no entry price to measure a tranche against.

That shape is worth **18.045113** — but read the construction before replaying
it. The figure comes from a SINGLE rung, the published template's deeper one
(600 at 66.50) with a tranche at 68.50, against a bar 67.00 / 68.60 / 66.40. It
is pinned by `test_a_bar_that_opens_the_position_is_a_known_blind_spot`.
Replaying the WHOLE two-rung template against that same bar gives a different
answer — 37.898054, with `snu_bars` reading 1, because the 68.00 rung fills
first and the bar is then detected. The blind spot is still reachable on the
full template, but only for a bar that opens ABOVE 68.00, where every rung it
can reach lies below the open. All four figures measured 2026-09-29.

The honest use of the count is deciding whether to trust the result at all,
never adjusting it.

The count is a frequency, never a size: two runs can both report 1 while the bar
decided 18.05 in one and 30.92 in the other. It also has no upper limit. A
tranche that sells only PART of the position leaves the run alive, so a later
bar can raise the count again.

**`pnl_cash` is gross.** The `costs` block decides which take-profit tranches
fire. It never reduces the cash.

**`notional_spent` is the budget the document declared, not what an order would
spend.** The live drain buys whole shares; the replay works in fractional units.
That is a deliberate scope cut rather than a fact the replay lacks, so it is not
a `divergences` entry. It is not small either. Section 5 of the design document
measures the gap at 17.50 on a 1500 budget, 130.00 on 8000 with rungs 120.00 and
115.00, and 60.81 on 1000 with rungs 196.13 and 175.40 — where the entry anchor
also moves by 0.56 in price, about 30 basis points, and the R denominator moves
with it.

**`window` describes the series you handed in, not the part the walk read.** It
can be wider on both sides. Bars before `walk_start` are skipped, and the walk
stops as soon as the position closes, so later bars are never looked at. Both
still count in `window`.

**`filled_fraction` can be slightly above 1.0, and one reason is not rounding.**
`validate_intent` accepts an entry ladder whose allocations sum to within 1e-6
of 100, so a document stating 50.0 and 50.000001 is a valid document and its run
reports `1.00000001`. Measured 2026-09-29. The replay does not clamp the value,
because clamping would hide that case; `/edge` clamps to `[0, 1]`, this does
not. Floating-point noise can also push it above 1.0, but only in the last few
digits, which is seven orders of magnitude smaller and is not the reason for the
decision.

### The summary

Nine keys. `filled_fraction` and `snu_bars` are bare numbers. Every other
measure carries its unit.

`notional_spent` and `pnl_cash` carry the document's own `spec.size.currency`.
`avg_entry_price` and the R denominator carry the symbolic unit
`instrument_currency`: they are prices in the INSTRUMENT's currency, which no
document path states, so the tool must not guess it. `pnl_pct_of_spent` carries
`percent`, so 4.58 means 4.58%. `r_multiple`, `mfe` and `mae` carry `R`.

`r_multiple` always carries its denominator as an object with five fields:
`kind` (`placed_stop`), `value`, `unit`, `source` (`spec.disaster_stop`) and
`formula` (`avg_entry_price - placed_stop`). The level that rests at the broker
is `spec.disaster_stop` on both deployments, in every document, including one
that supplies its own `exit.initial_levels`.

**When the denominator is not positive, three measures are null.** Not
"negative": exactly 0.0 is reachable, by a bar that opens at the disaster stop
and therefore fills there. `r_multiple.value`, `mfe` and `mae` all become null,
and the `denominator` object is still carried, so a reader sees why. This is not
a paper case — without the rule, a run that lost 109.375 reports +1.16 R.

Two null shapes, because the design document prints two. `r_multiple` keeps its
object, because the denominator inside it is the answer to "why is this null".
Every other measure — `mfe`, `mae`, `avg_entry_price`, `pnl_pct_of_spent` —
becomes a bare `null`, because a wrapper holding nothing but a unit answers
nothing.

**`mfe` is never negative and `mae` never positive, but only to within one unit
in the last place.** The structural argument is about fill prices: every fill
sits inside its own bar and the extremes are tracked from the first fill onward.
But `avg_entry_price` is `cash / units`, and that division can land one ulp below
a fill price the trough then equals. Measured 2026-09-29: one run in 200 000
produced `mae` at `+6.447756222868429e-16`. A check written as `mae <= 0` would
go red at that rate.

**The denominator is not held away from zero, and that is the document's
geometry rather than a defect.** A denominator of 1e-10 produces an R on the
order of 1e10. It cannot reach infinity: the denominator is a difference of two
prices of the same size, so its smallest non-zero value is one unit in the last
place, which is 7.105427357601002e-15 at a price near 63 (measured
2026-09-29). Section 5 of the design document works the resulting cap out.
`allow_nan=False` on the writer means an infinity or a NaN cannot reach stdout:
the writer REFUSES to emit it and raises instead. That is a statement about the
writer, not a proof that every number is finite.

### divergences

Each entry names a place where the replay is known to differ from the live
daemon for a reason no configuration value can close, because the replay lacks a
FACT rather than a setting. Five entries exist, and each is printed only on the
runs it applies to:

| entry | printed when |
|---|---|
| `daemon_trail_guards` | the declared policy moves the stop at all; it covers both stop arms |
| `daemon_reanchor_latch_is_journal_lifetime` | the reaction is `reanchor_on_fill` |
| `native_entry_trail_is_a_broker_model` | the run states an entry-trail distance |
| `take_profit_observation_time` | the resolved take-profit ladder is not empty |
| `cost_gate_prices_the_account_currency` | the ladder is not empty and `min_commission_applies` is true |

In THIS version `native_entry_trail_is_a_broker_model` cannot appear. The code
emits it, but a stated entry-trail distance is refused before the walk starts
(see `entry_trail_not_modelled` in the refusal table). The next change models
entry trailing, removes the refusal, and makes the entry reachable.

`cost_gate_prices_the_account_currency` is an admission the tool publishes about
itself: it prices the stated budget in the ACCOUNT currency while the daemon
prices the whole-share notional in the INSTRUMENT's. Above the fee card's knee
the two agree; below it the replay's threshold is too low, so it fires tranches
the daemon would decline.


## Refusal codes

One code per failure mode, with a closed `details.reason` vocabulary where a
mode has more than one rule inside it. The object is `broker_contract.failure`'s:
`code`, `message`, `retryable`, `details`, `suggestions`. Nothing this tool
refuses is retryable. The `owner` column names the half that defines the code:
`engine` (a module of this package that raises it as a typed exception), `contract`
(`broker_contract`), `CLI` (`intent_replay.cli`, which maps the engine's
reasons onto it).

| code | owner | retryable | meaning |
|---|---|---|---|
| `bars_empty` | engine | no | No bars were supplied. |
| `bars_unordered` | engine | no | The bars are not strictly increasing in time. |
| `bars_invalid` | engine | no | A bar that cannot be compared, or bar input that is not the published shape: a non-finite price, prices that contradict each other (a high below the low, or an open or close outside `[low, high]`), input that is not a JSON array, a bar that is not an object, a missing key, a key a bar does not model, or a field of the wrong type. `details.reason` names which, with `details.index` and `details.field` where they apply. |
| `window_too_short` | engine | no | The bars do not cover the stated `walk_start`; `details.reason` says which side. |
| `config_incomplete` | engine | no | A required configuration value was not stated; `details.keys` names every missing key. |
| `config_invalid` | engine | no | A stated configuration value nothing can use; `details.keys` and `details.reason` name it. A file that parses but is not an object is refused here too, with `<root>` standing for the whole block. |
| `path_unclassified` | engine | no | The document carries a path the replay neither interprets, translates nor lists as out of scope; `details.paths` names them. |
| `entry_mode_unsupported` | engine | no | An entry tier declares an `entry_mode` v1 does not model; `details.tiers` names them. Only `pullback` rests as a rung a bar walk can test, and the arming door ADMITS `immediate`, so this refusal is the interpreter's. |
| `intent_invalid` | contract | no | The document is internally inconsistent (`validate_intent`); `details.reason` names the rule, as at the arming door. |
| `intent_malformed` | CLI | no | The document is not the published input contract; `details.reason` names which rule, see below. |
| `config_malformed` | CLI | no | The configuration file could not be PARSED: not a UTF-8 JSON document, or an object in it repeats a key. `details.reason` names which, `details.path` the file. |
| `bars_malformed` | CLI | no | The bar file could not be PARSED: not a UTF-8 JSON document, or an object in it repeats a key. `details.reason` names which, `details.path` the file. A file that parses and is not the published bar shape is `bars_invalid`. |
| `usage` | CLI | no | The invocation is malformed (a bad option or value), or a file it names cannot be read (`details.path`). |

`intent_malformed` carries the reasons of the arming door that apply to a
replay, plus the parser's two. The door's venue, pick-key and generation
refusals do not apply (spec section 4.3), and `meta.schema_version` is not
read (section 4.3.1).

| code | `details.reason` | what the author did |
|---|---|---|
| `intent_malformed` | `not_json` | the bytes are not a UTF-8 JSON document |
| | `duplicate_key` | an object repeats a key; a JSON parser keeps the LAST silently, so the value sent first would vanish; `details.keys` lists them |
| | `derived_field_supplied` | the document carries `intent_id`, `meta.armed_ts` or a `r_multiple`, which a door computes; `details.paths` lists them |
| | `schema_violation` | the document fails the published input JSON Schema; `details.path` locates it |
| | `trade_date_required` | no `meta.trade_date`: the replay has no clock and no calendar to fill it from, so the author states it |
| | `trade_date_malformed` | `meta.trade_date` is not a date |
| | `undecodable` | the shape passes but the decoder refuses it (e.g. `generation: 1.0`) |
| | `key_discarded` | a key the decoder would DROP, so the replay would not carry what was sent; `details.paths` lists them. A `meta.trade_date` that parses but is not spelled `YYYY-MM-DD` lands here, as at the door |
| `config_malformed` | `not_json` | the configuration file is not a UTF-8 JSON document |
| | `duplicate_key` | an object in the configuration file repeats a key; `details.keys` lists them |
| `bars_malformed` | `not_json` | the bar file is not a UTF-8 JSON document |
| | `duplicate_key` | an object in the bar file repeats a key; `details.keys` lists them |

`config_invalid` has its own vocabulary. It answers a value the caller DID
state, so the key is present and nothing can use it. A key the caller did not
state is `config_incomplete` instead, and missing keys win: when keys are both
missing and unusable, only the missing ones are reported.

| code | `details.reason` | what the caller stated |
|---|---|---|
| `config_invalid` | `unknown_key` | a key this block does not model. The contract's decoder DROPS such a key with only a warning, so a misspelt `entry_trail_bp` would otherwise switch entry trailing off in a run whose author believes the distance was stated |
| | `wrong_type` | the value is not of the key's type; a stated `null` where none is allowed is included |
| | `numeric_not_finite` | a price or cost is NaN or infinite, so every comparison on it would silently pass |
| | `not_positive` | a distance or a price that must be above zero is not |
| | `negative` | a cost below zero |
| | `unit_mismatch` | the unit is not the one the walk compares against |
| | `empty_string` | a provenance field or a currency code with no text |
| | `oco_unsupported` | `oco` is stated `true`; v1 models no OCO pair, and the block travels in the result, so accepting it would describe a policy the run did not apply |
| | `fx_cost_not_stated` | `fx_applies` is `true` and no key states the rate. The omitted term is 50 basis points of the notional, so accepting it would price every round trip too cheap |
| | `entry_trail_not_modelled` | a well-formed entry-trail distance. TEMPORARY: this version parses the distance and does not apply it, so carrying it into the result would claim a policy the run never ran. State `null` until the next change models entry trailing. The type and range checks run first, so `true` is still `wrong_type` and `0` is still `not_positive`. One published consequence: the design document's own section 5.2 block states `entry_trail_bps: 50`, so this version refuses the very block it prints as canonical |

Design: `docs/superpowers/specs/2026-09-23-intent-replay-design.md`.
Implementation plan: `docs/superpowers/plans/2026-09-25-intent-replay-step1.md`.
