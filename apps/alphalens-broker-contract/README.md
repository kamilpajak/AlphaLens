# alphalens-broker-contract

Shared, dependency-free broker-contract primitives consumed by BOTH
`alphalens-pipeline` and `alphalens-research`: the Boundary-1/2 wire types and
the pure ATR-bracket exit-geometry leaf (`broker_contract.exit_geometry`).
Stdlib-only by design so it can be extracted into a standalone published
package later (ADR 0006 `phase-robust-backtesting` extraction precedent)
without carrying any pipeline / research / broker-vendor dependency along
with it. See the broker-manager extraction design memo,
`docs/research/broker_manager_extraction_and_exit_geometry_2026_07_31.md`,
§2.1.

The package holds the broker contract (`contract.py`), the failure contract
(`failure.py`), sizing and quantity arithmetic (`sizing.py`, `quantity.py`,
`fx.py`, `constants.py`), the price-feed protocol (`price_feed.py`), the
`TradeIntent` document (`trade_intent/`: schema, codec, validation, JSON Schema
generator, legacy register) and the exit-geometry leaf (`exit_geometry/`).

## The failure contract (#1389)

Every refusal or breakage the `alphalens broker` command group reports is one
object, defined by `broker_contract.failure`:

```json
{
  "code": "write_outcome_unknown",
  "message": "Saxo 500 on POST /trade/v2/orders (x-request-id=…)",
  "retryable": false,
  "details": {"request_id": "…"},
  "suggestions": [{"argv": ["alphalens", "broker", "orders", "--format", "json"],
                   "why": "the write may already rest at the broker …"}]
}
```

**`retryable` means "safe to re-run WITHOUT reconciling first"** — not "the
cause was transient". The two come apart: a 5xx after a POST is transient in
cause and forbidden to retry, because the order may already rest at the broker.
That case is `write_outcome_unknown`, and it is why **`code` is the field to
branch on**; `retryable` is a coarse shortcut for a shell caller.

`message` is free to change. **`details` is diagnostic and UNSTABLE** — its keys
are not part of the contract unless the row below names them for that code.

Codes are only ever ADDED, never renamed and never repurposed — with one stated
exception: `unclassified` marks a refusal that has not been given a code yet.
A site graduating out of it into a specific code is EXPECTED and is not a
breaking change, so `unclassified` must never be a branch condition. Read it as
"a human must look".

| code | owner | retryable | meaning |
|---|---|---|---|
| `broker_transient` | contract | **yes** | The request provably never landed (network retries exhausted, or a 5xx on an idempotent verb). Re-run when the outage clears. |
| `broker_rate_limited` | contract | **yes** | The broker's throttle stayed exhausted after retries. Re-run later. |
| `write_outcome_unknown` | contract | no | A write failed AFTER it may have reached the broker. Reconcile against live order state before re-running; never blind-retry. Carries a `suggestions` argv. |
| `broker_auth` | contract | no | Credentials invalid, or the OAuth refresh chain is dead. Carries a `suggestions` argv. |
| `instrument_not_found` | contract | no | Instrument resolution failed for this (ticker, MIC). |
| `order_rejected` | contract | no | The broker rejected the order. `details.error_code` carries the vendor's structured code when the adapter could attach one. |
| `broker_unsupported` | contract | no | This broker does not offer the capability the operation needs. |
| `broker_failed` | contract | no | A broker failure that is neither transient, throttled, nor ambiguous. |
| `intent_invalid` | contract | no | The submitted `TradeIntent` is internally inconsistent. Nothing was queued. |
| `usage` | CLI | no | The invocation is malformed (unknown option value, bad date, bad bound, unknown instance name). |
| `env_ambiguous` | CLI | no | The shell names one broker instance and a queue-writing command defaults to another. Pass `--env` explicitly (#1377). |
| `live_refused` | CLI | no | A LIVE broker could not be built: the LIVE rails or auth surface are absent from the process (ADR 0017). |
| `state_layout` | CLI | no | Durable broker state is still in the pre-migration flat layout (ADR 0016 D4). |
| `pick_already_armed` | CLI | no | Another pick on this ticker is still armed: a live generation of the same (ticker, trade date), or an unplaced pick on the same venue under any date. `details` names its `armed_trade_date` and `armed_generation`. Carries a `suggestions` argv. |
| `pick_not_writable` | CLI | no | This pick key cannot take a write: the generation was disarmed or refused, or the daemon has already placed it. `details.reason` is `generation_spent` or `already_placed`. Carries a `suggestions` argv. |
| `intent_malformed` | CLI | no | The submitted document does not match the published wire contract. `details.reason` names which gate refused it (see below). Nothing was queued. |
| `venue_unsupported` | CLI | no | The document is well formed; this deployment does not trade that MIC. `details.mic` carries the venue. A venue list is deployment knowledge, never a document rule (#1122, #1404). |
| `queue_write_failed` | CLI | **yes** | Appending to a broker journal failed (disk full, permissions). Nothing was queued and no broker order can be in flight — the commands that report this write only to the queue — so the same command may be re-run once the cause clears. `details.journal` names the file. |
| `stream_metrics_missing` | CLI | no | The stream gauge textfile does not exist for this instance. Carries a `suggestions` argv. |
| `unclassified` | CLI | no | Not yet given a code. Never branch on it. |

#### `intent_invalid` publishes its `details` keys

`intent_invalid` covers every way a document can be internally inconsistent, so
the code alone does not say which rule broke. Rather than one top-level code per
rule, the discriminator is a
**published** `details` key — the escape the rule above names ("unless the row
below names them for that code"). For this code, and only this code, these keys
are part of the contract:

| key | meaning |
|---|---|
| `reason` | Closed vocabulary naming the broken rule, e.g. `entry_alloc_sum`, `stop_above_entry`, `tp_price_below_blend`. Defined in `broker_contract.trade_intent.validate.INTENT_INVALID_REASONS`. |
| `violations` | **Every** broken rule, not just the first: a list of `{reason, message}` objects, each carrying its index key when it is element-wise. A generated document can be fixed in one pass instead of a submit-fix-submit loop. |
| `tier_index` | 0-based index of the offending entry tier, for a rule about one rung. |
| `tranche_index` | 0-based index of the offending take-profit tranche. |
| `reaction_index` | 0-based index of the offending `exit.reaction_plan` entry. |

`message` carries the FIRST violation in the published order — identity, entry
ladder, stop, size, take-profits, exit declaration — so the operator's text is
stable while a machine reads the whole list.

#### `intent_malformed` and `pick_not_writable` publish a `reason` too

Same shape, same reason: one code per failure MODE, with a closed `details.reason`
vocabulary naming which rule inside it broke. Three codes cover the three
questions a submitted document has to answer — is it the right SHAPE
(`intent_malformed`), is it COHERENT (`intent_invalid`), and will this
deployment take it (`venue_unsupported`, `pick_not_writable`).

| code | `details.reason` | what the submitter did |
|---|---|---|
| `intent_malformed` | `not_json` | the bytes are not a JSON document |
| | `duplicate_key` | an object repeats a key; `json` parsers keep the LAST silently, so the value you sent first would vanish |
| | `derived_field_supplied` | the document carries `intent_id`, `meta.armed_ts` or a `r_multiple`, which the door computes; `details.paths` lists them |
| | `schema_version_unsupported` | `meta.schema_version` is not the version this door speaks |
| | `schema_violation` | the document fails the published input JSON Schema; `details.path` locates it |
| | `undecodable` | the shape passes but the decoder refuses it (e.g. `generation: 1.0` — JSON Schema calls that an integer, identity strings cannot) |
| | `trade_date_malformed` | `meta.trade_date` is not a `YYYY-MM-DD` date |
| | `trade_date_required` | a `"brief"` document with no `meta.trade_date`: day 1 of a brief pick is the session after its brief date, which the door cannot know |
| | `key_discarded` | a key the decoder would DROP, so the arm would not carry what you sent; `details.paths` lists them |
| `pick_not_writable` | `generation_spent` | that generation was disarmed or refused; a spent generation never comes back |
| | `already_placed` | the daemon has already placed this pick, so rewriting it would change the queue and not the market |

#### The exit declaration, and what it may say

`exit.reaction_plan` is how a document states **how its stop is managed** after
fill. It is a declaration the daemon honours, so the door refuses anything it
cannot honour rather than quietly doing something else:

| declaration | the daemon |
|---|---|
| absent, or an `exit` with an empty plan | never moves the stop |
| `TrailingStop(arm_trigger_r, trail_frac)` | arms a break-even trail at `arm_trigger_r` R of favourable excursion, then gives back `trail_frac` of it |
| `ReanchorOnFill(k_atr, atr)` | re-anchors the stop once, on fill-complete, to `avg_price - k_atr*atr` |

Absent means **the stop is never moved**, not "use this deployment's default".
That is deliberate: a document that says nothing about its exit has not asked for
one to be managed, and inheriting a server-side policy would move real stops that
nobody asked to move.

Parameters are yours to choose within the rules below; they are used as declared,
never replaced by this deployment's own numbers. `0.5R` is not a comparable
quantity across hand-set stops — 1R is 6.8% of entry on one instrument and 29% on
another — so the choice has to be the document's.

Refusals specific to the declaration: `reaction_plan_ambiguous` (more than one
stop-management primitive — the daemon manages one stop, and a precedence rule
invented server-side is one no client can read off the document),
`reaction_kind_unsupported`, `k_atr_non_positive`, `arm_trigger_r_non_positive`,
`trail_frac_out_of_range`, and `ceiling_price_unsupported`.

That last one is worth a sentence, because the field reads like a stop-side cap
and is not: `ceiling_price` applies as `tp = min(tp, ceiling_price)` and never
touches the stop. It is a take-profit — that is, a *placement* — instruction, and
nothing on the daemon side computes a take-profit: the re-anchor returns a stop,
and the only code that reads `ceiling_price` is the producer that builds the
bracket before sending it. So the refusal is permanent, not a "not yet".

**`initial_levels` is optional, and its presence is the placement instruction.**
Supply the levels and they are what gets placed; omit them and the ladder from
`spec` is. A document may therefore declare how its stop is MANAGED without
supplying a bracket to PLACE, and a re-anchor beside no levels is a coherent
document: it re-anchors the stop off the fill price while the `spec` ladder is
what rests at the broker.

Because those two numbers are placed, the door checks them: both finite, both
`> 0`, and `tp` strictly above `stop` (`take_profit_not_above_stop`). Nothing on
this side can veto levels a document supplies — there is no server-side switch
that ignores them — so an incoherent pair has to be refused here.

**What `validate_intent` does NOT check** is as much part of the contract as what
it does. Rules about the *invocation* rather than the document stay with the CLI:
`order_ttl_days == 0` is LEGAL (the planner resolves that sentinel to a default),
and the supported-venue list is broker-deployment knowledge. Single-field shape
and format — `meta.trade_date` parsing as a date, `schema_version` bounds — belong
to the JSON Schema layer. A door onto this contract is expected to apply both.

**Owner** says where the code is DEFINED, not who emits it: the contract package
holds the codes any consumer of the broker taxonomy needs, and the CLI holds the
ones only a command-line program can raise. The split follows the #1122
decision that moved the Saxo fee card out of this package — *"the ADAPTER
reports, never the contract decides"* — and is pinned by
`tests/brokers/test_broker_contract_has_no_cli_codes.py`.

### How it reaches a caller

`--format json` writes the object on **stderr** and leaves **stdout empty**;
`--format human` (the default) writes the operator's red prose instead. stderr
also carries operator chrome in JSON mode (the `env=… gateway=…` banner,
malformed-line warnings), so the machine rule is: **exactly one stderr line is a
JSON object, and it is the last line.**

Exit statuses stay coarse — the domain detail is in `code`:

| status | when |
|---|---|
| `0` | success |
| `1` | any other failure |
| `2` | `usage` |
| `4` | `stream_metrics_missing` |
| `7` | any `retryable` code |

## The document schema (#1405)

The wire shape of a `TradeIntent` is published as JSON Schema in two shapes,
both **generated** from `broker_contract/trade_intent/schema.py`, never
hand-edited; CI fails when a committed file and a fresh generation disagree:

| artefact | what it describes |
|---|---|
| [`docs/trade-intent-input-v3.schema.json`](docs/trade-intent-input-v3.schema.json) | what an AUTHOR sends to the door (#1468) |
| [`docs/trade-intent-v3.schema.json`](docs/trade-intent-v3.schema.json) | what the journal stores and the drain decodes |

The input shape is the stored one with each field's `door` role applied: a
**derived** field (`intent_id`, `meta.armed_ts`, `tp_tranches[].r_multiple`) is not
described at all and is refused if sent; a **filled** field (`meta.trade_date`,
`meta.generation`, the tier and tranche `tag`) is optional and its description says
what the door puts there; `meta.source` is **required**, because its stored
default `"brief"` exists for old journal lines and moves the day-1 gate by a
session.

```
python -m broker_contract.trade_intent.json_schema --write
```

Four things the file cannot say about itself.

**It pins shape, not acceptability.** Closed vocabularies (`side`, `entry_mode`,
each reaction `kind`), required fields, types and nullability live in the
schema; the rules that need arithmetic or cross-field reasoning — allocations
summing to 100, a stop below every entry rung, a take-profit above the blend,
NaN — live in `validate_intent` and report as `intent_invalid`. One rule has one
owner, so **schema-valid does not mean accepted**. The two halves are one
contract, read them together.

**It describes the door, checked on the wire, before decoding.** A document is
validated as it arrives, not after reconstruction — validating the decoded
object would mostly re-assert that the decoder built what its own types say.
The journal drain is a different entry point and is deliberately not
schema-gated, which is why the schema knows nothing about `meta.brief_date`: the
codec migrates that pre-#1252 key so old journal lines still replay, while a new
producer must send `trade_date`. That gap is a decision, pinned by a test.

**Identity, because a retry depends on it.** The queue folds picks on
`(ticker, trade_date, generation)` and keeps the LATEST, so re-submitting a pick
with its generation **replaces** it rather than adding a second one. A document
that omits `generation` asks for a NEW pick: the door takes `1 +` the highest
already recorded for that ticker and date (whatever became of it). The rules are
in the next section, and they are the difference between "your retry landed" and
"you replaced someone else's pick".

**Which `schema_version` to read, and what it does.** `meta.schema_version` is
the document's version; `spec.schema_version` is the same constant duplicated in
a second class, not an independent dial. **Exactly one gate reads it: the door**
(`broker arm`, #1406), which refuses any stated version other than its
own with `schema_version_unsupported`. An ABSENT key is the current version
rather than an unknown one — the field carries a default, so omitting it means
"whatever this contract is at".

Neither the codec nor `validate_intent` nor the JSON Schema reads it, and that
is deliberate: a future ADDITIVE version must decode in the door and in the
journal drain alike. It is not a promise that every old journal line decodes.
Every document written before #1467 sizes by percent, and the journal reader
skips those before decoding (see below).

Version 3 (#1467) broke that on purpose. It replaced
`spec.suggested_size_pct`, a percent of an equity frame the daemon read from its
own environment, with `spec.size`, an amount the document states:

```json
"size": {"notional_acct": 1500.0, "currency": "PLN"}
```

`notional_acct` is the budget for the whole entry ladder in the ACCOUNT currency;
each rung gets `notional_acct x alloc_pct / 100`. `validate_intent` refuses an
amount that is not positive (`size_notional_not_positive`) and a currency that is
not three uppercase letters (`size_currency_invalid`). Whether the currency is
this account's, and whether the amount stays under the deployment's per-pick
ceiling, are deployment facts: the daemon refuses those picks when it drains
them, before the day-1 gate, and never shrinks one to fit. A version-2 document
does not decode, because its percent cannot be turned into an amount without the
frame that is gone. The journal reader recognises such lines and skips them
(`legacy.py`, `size_pct_v2`). The drain refuses such a pick once, with an alert,
unless `submissions.jsonl` already holds a record for its pullback tiers. When
only its now tranche has a record, the refusal says to re-arm the pullback tiers
only.
The compatibility promise on top is a promise about what we EMIT — within a
major version, fields are only ADDED and only as optional — and the CI gate on
the generated artefact is what enforces it.


## The door: submitting a document (#1406, #1468, #1470)

```
alphalens broker arm <path|-> [--env sim|live] [--dry-run] [--format human|json]
```

A producer that can write JSON does not need a command of its own. The author
writes the TRADE and nothing the door can compute:

```json
{
  "instrument": {"ticker": "KO", "mic": "XNYS"},
  "spec": {
    "entry_tiers": [
      {"limit_price": 60.0, "alloc_pct": 75.0},
      {"limit_price": 58.0, "alloc_pct": 25.0}
    ],
    "disaster_stop": 55.0,
    "tp_tranches": [{"price": 66.0, "tranche_pct": 50.0}],
    "size": {"notional_acct": 1500.0, "currency": "EUR"}
  },
  "meta": {"source": "manual"}
}
```

The door then derives, and journals:

| field | value |
|---|---|
| `intent_id` | `TICKER:DATE` for a brief pick, `TICKER:DATE:manual` for a manual one, `-g<N>` after generation 1 |
| `meta.armed_ts` | the moment of arming; a replace KEEPS the `armed_ts` of the line it replaces |
| `meta.trade_date` | when absent: the next session of `instrument.mic` that has not closed (during a session, that session; after the close or on a holiday, the next one). Required on a `"brief"` document |
| `meta.generation` | when absent: the next free generation of (ticker, trade_date) |
| tags | when absent: `T1`, `T2`, … and `TP1`, `TP2`, … |
| `r_multiple` | `(price - blend) / (blend - stop)`, `blend` being the alloc-weighted planned entry |

`--dry-run` echoes all of it, with the account-currency amount of each tier, and
`--format json` answers with the full stored document under `intent`. The door
does not unwrap envelopes: a document is the bare input shape above.

Why the door computes these rather than trusting the author: `armed_ts` is the
idempotency key of an immediate tier, so a fresh value on a retry sends that tier
again; `trade_date` anchors the day-1 gap gate, and "today at the exchange" would
give a US pick armed after the New York close a session that has already closed;
and a non-finite `r_multiple` used to leave a take-profit ladder unmanaged.

**Gates on the document, in order.** Each answers a different question, and
none of them is implied by another:

| gate | question | refusal |
|---|---|---|
| derived fields | did the author send what the door computes? | `intent_malformed` / `derived_field_supplied` |
| JSON Schema | is it the published INPUT shape? | `intent_malformed` / `schema_violation` |
| version | does it state a version this door does not speak? | `intent_malformed` / `schema_version_unsupported` |
| venue | does this deployment trade the MIC? | `venue_unsupported` |
| identity | is `generation` a real integer, `trade_date` a date? | `intent_malformed` / `undecodable`, `trade_date_malformed` |
| the pick key | see the table below | `pick_already_armed`, `pick_not_writable` |
| the codec | can it be decoded? | `intent_malformed` / `undecodable` |
| the fixed point | does every key you sent survive decoding? | `intent_malformed` / `key_discarded` |
| `validate_intent` | is the document COHERENT? | `intent_invalid` |

The fixed point is the gate a reader is most likely to think redundant. It is not:
the decoder drops keys it does not model with only a log line, which is sensible
forward compatibility for a daemon reading its own journal and wrong for a door
that arms money. Without it a typo'd `limit_pirce` is discarded and the pick
arms at the price you did NOT send. The parser refuses a repeated JSON key for
the same reason — `json.loads` keeps the last silently.

The identity check is not redundant either, and the reason is worth stating
because it cannot be fixed: JSON Schema defines `integer` as any number with zero
fractional part, so `"generation": 1.0` passes the schema, while the identity
strings built from that field require a real integer (#1371). It runs before the
pick key is read, so the derivation never sees such a value.

**The pick key.** Venue is checked before it, because deriving a date reads the
MIC's calendar. Then:

| state | what happens |
|---|---|
| no `generation` sent, and nothing on that (ticker, trade_date) is armed | armed — a new pick, the next free generation |
| no `generation` sent, and a pick on that (ticker, trade_date) is armed, placed or not | refused, `pick_already_armed` |
| `generation` sent, the queue has never seen that key | armed — a new pick |
| `generation` sent, armed, and the daemon has not placed it | armed — **this is the idempotent replace**, the retry-after-timeout path; `armed_ts` is kept |
| `generation` sent, armed, but already placed | refused, `pick_not_writable` / `already_placed` |
| `generation` sent, disarmed or refused | refused, `pick_not_writable` / `generation_spent` |
| a DIFFERENT generation of that (ticker, trade_date) is still armed | refused, `pick_already_armed` |
| any other armed, UNPLACED pick on the same ticker and venue, under any date | refused, `pick_already_armed` |

The last row closes a retry across midnight: without it, a re-sent document
whose `trade_date` the door derives a day later would be a second live pick on
the same instrument. XNYS, XNAS and XASE count as one venue, because routing
probes them together. A pick that stays armed and unplaced (for example one whose
tiers size to zero shares) therefore blocks its ticker until it is disarmed; the
refusal names it and the `disarm` command.

Because a replace keeps `armed_ts`, it cannot re-open an immediate tier the daemon
already handled, including one it refused above its cap. A new cap is `disarm`
followed by a new document, which gets a new generation.

The last three are refusals for reasons that were measured rather than assumed.
Re-sending a document after `disarm` used to bring a cancelled pick back to
life. Re-sending it after placement used to rewrite a queue line the drain never
reads again, because it skips keys already joined to `submissions.jsonl` — so
the queue would say one thing and the market another.

Because the journal is append-only, a successful replace leaves **two lines and
one folded pick**. That is the shape to assert against, not the line count.

**Nothing is appended unless every gate passed**, and in `--format json` the
envelope is rendered before the append, so a payload that cannot be serialised
refuses without having armed anything. `--dry-run` runs every gate and appends
nothing.

**What the door does NOT do.** It applies no selection filter and never
normalises or rescales: it is a pure executor. It also
does not refuse a LIVE `--env` when the LIVE rails are absent, because arming is
not placing: the rails gate the daemon, and the guard that does exist here is
the refusal to take the instance off an ambient environment variable (#1377).
Concurrent submitters are not serialised; the key check reads the fold and then
appends, so two processes racing on one ticker can both pass it.

### Writing a manual pick (#1470)

`broker arm` is the only arming command. There is no flag form: a manual pick is a
document. Start from one of these templates, change the ticker, the levels and the
amount, and send it with `--dry-run` first:

| template | shape |
|---|---|
| [`examples/manual-pick/pullback-two-tiers.json`](examples/manual-pick/pullback-two-tiers.json) | two resting pullback tiers, one take-profit, `exit: null` (the stop is never moved) |
| [`examples/manual-pick/pullback-trailing-stop.json`](examples/manual-pick/pullback-trailing-stop.json) | the same ladder, and the stop trails once the position is 0.5R up |
| [`examples/manual-pick/immediate-plus-pullback.json`](examples/manual-pick/immediate-plus-pullback.json) | half bought at once with a price cap (`entry_mode: "immediate"`, listed first), half resting lower |

```bash
.venv/bin/alphalens broker arm my-pick.json --env sim --dry-run
.venv/bin/alphalens broker arm my-pick.json --env sim
```

Things to set every time:

- `size.currency` is the ACCOUNT currency of the instance you arm into. The daemon
  refuses a pick in any other currency when it drains it.
- `instrument.mic` is the venue. The door accepts XNYS, XNAS, XWAR, XETR and XPAR.
- A take-profit is a PRICE. The dry run prints the R of each one, so check it there.
- Keep `exit.initial_levels` null unless you mean it. A document with levels has the
  daemon place those two levels (one stop, one take-profit for the whole position)
  INSTEAD of `disaster_stop` and `tp_tranches` (#1414).

**Copying an armed pick.** To arm yesterday's pick again with corrected levels,
turn its journal line back into a document. The templates test runs this block
exactly as written:

<!-- manual-pick-copy-recipe -->
```bash
set -o pipefail
jq -cR 'fromjson? | select(.ticker == "KO" and .status == "armed") | .intent
        | del(.intent_id, .meta.armed_ts, .meta.generation, .meta.trade_date,
              .spec.tp_tranches[]?.r_multiple)' \
  ~/.alphalens/broker_orders/sim/picks.jsonl | tail -n 1 > ko.json
```

- It takes the LAST armed line for the ticker, which after a replace is the newest
  version of the pick. `fromjson?` skips a torn line instead of stopping at it.
- It removes what the door derives (`intent_id`, `armed_ts`, every `r_multiple`) and
  also `generation` and `trade_date`. The copy is therefore a NEW pick: the door gives
  it the next generation and the current session. Stating the old generation would
  mean "replace", and stating the old date would arm under that date.
- The door refuses the copy while the original is still armed. `disarm` it first.
- Manual picks only. A copied brief pick is refused (`trade_date_required`); produce
  it again with `alphalens thematic intent`.
- For LIVE, read `broker_orders/live/picks.jsonl` and arm with `--env live`.
- Lines armed before #1475 (2026-09-16) state a percent size or `meta.brief_date` and
  are refused by the door. Start from a template instead.
