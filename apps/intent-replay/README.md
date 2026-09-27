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
| `entry_trail_bps` | integer >= 1, or `null` | the native entry-trail distance; `null` is trailing OFF |
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
unit, an unknown key, `oco: true`, `costs.fx_applies: true` — is
`config_invalid`. Nothing is defaulted.

## Command line

```
intent-replay run DOCUMENT --config PATH --bars PATH [--format json|ndjson]
intent-replay schema [COMMAND] [--format json]
```

`DOCUMENT` is a TradeIntent JSON file, the same bare document `alphalens broker
arm` takes, or `-` to read it from stdin. `--config` names the run configuration
block above. `--bars` names the price input: ONE JSON array of
`{t, open, high, low, close}` objects, `t` in epoch milliseconds UTC, strictly
increasing, with no key a bar does not model and no key missing. It is required,
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
Then the walk runs. **In this version an accepted document still has nothing to
print:** the command exits 0 with empty stdout and empty stderr. The walk's
trace, its `ambiguous_bars` counter and its fill figures are computed and the
envelope that renders them arrives in the next change. A refusal is exactly one
JSON object on stderr, the last line, with stdout empty; exit status `0`
accepted, `2` usage, `130` interrupted with nothing written, `1` everything
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
| `bars_invalid` | engine | no | A bar that cannot be compared, or bar input that is not the published shape: a non-finite price, input that is not a JSON array, a bar that is not an object, a missing key, a key a bar does not model, or a field of the wrong type. `details.reason` names which, with `details.index` and `details.field` where they apply. |
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

Design: `docs/superpowers/specs/2026-09-23-intent-replay-design.md`.
Implementation plan: `docs/superpowers/plans/2026-09-25-intent-replay-step1.md`.
