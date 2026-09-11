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

This is the first cut (step 2A-1): only the `exit_geometry` leaf has moved
here so far. `contract.py`, `intent.py`, `sizing.py`, `fx.py`, `constants.py`,
`calendar.py` follow in later 2A sub-PRs.

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
| `live_refused` | CLI | no | A LIVE operation was refused: ad-hoc placement on LIVE is forbidden (ADR 0017), or the LIVE rails / auth surface are absent. |
| `state_layout` | CLI | no | Durable broker state is still in the pre-migration flat layout (ADR 0016 D4). |
| `pick_already_armed` | CLI | no | A live earlier generation of this (ticker, trade date) is still armed. Carries a `suggestions` argv. |
| `pick_not_writable` | CLI | no | This pick key cannot take a write: the generation was disarmed or refused, or the daemon has already placed it. `details.reason` is `generation_spent` or `already_placed`. Carries a `suggestions` argv. |
| `intent_malformed` | CLI | no | The submitted document does not match the published wire contract. `details.reason` names which gate refused it (see below). Nothing was queued. |
| `venue_unsupported` | CLI | no | The document is well formed; this deployment does not trade that MIC. `details.mic` carries the venue. A venue list is deployment knowledge, never a document rule (#1122, #1404). |
| `policy_refused` | CLI | no | A safety policy refused the operation (gross guard, FX divergence, unverifiable instrument currency, a resting order in the way). |
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
| | `envelope_unknown` | a top-level `schema` that this door does not publish, or one with no `intent` inside |
| | `schema_version_unsupported` | `meta.schema_version` is not the version this door speaks |
| | `schema_violation` | the document fails the published JSON Schema; `details.path` locates it |
| | `undecodable` | the shape passes but the decoder refuses it (e.g. `generation: 1.0` — JSON Schema calls that an integer, identity strings cannot) |
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
`reaction_kind_unsupported`, `reanchor_without_levels`, `k_atr_non_positive`,
`arm_trigger_r_non_positive`, `trail_frac_out_of_range`, and
`ceiling_price_unsupported`.

That last one is worth a sentence, because the field reads like a stop-side cap
and is not: `ceiling_price` applies as `tp = min(tp, ceiling_price)` and never
touches the stop. It is a take-profit — that is, a *placement* — instruction, and
placement is not yet something this contract carries. Refusing it is better than
accepting a field that would be silently discarded.

**`initial_levels` is optional**, and its presence is not a formality: a document
may declare how its stop is MANAGED without supplying a bracket to PLACE. The two
halves are independent.

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

The wire shape of a `TradeIntent` is published as JSON Schema at
[`docs/trade-intent-v2.schema.json`](docs/trade-intent-v2.schema.json). It is
**generated** from `broker_contract/trade_intent/schema.py`, never hand-edited,
and CI fails when the committed file and a fresh generation disagree:

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
codec migrates that pre-#1290 key so old journal lines still replay, while a new
producer must send `trade_date`. That gap is a decision, pinned by a test.

**Identity, because a retry depends on it.** The queue folds picks on
`(ticker, trade_date, generation)` and keeps the LATEST, so re-submitting a pick
**replaces** it rather than adding a second one. A document that omits
`generation` is generation 1. Who assigns it depends on the producer:
`arm-manual` takes `1 +` the highest already recorded for that ticker and date
(whatever became of it), while a document submitted to the door carries its own
— and the door then checks that the key it names still takes a write. The rules
are in the next section, and they are the difference between "your retry landed"
and "you replaced someone else's pick".

**Which `schema_version` to read, and what it does.** `meta.schema_version` is
the document's version; `spec.schema_version` is the same constant duplicated in
a second class, not an independent dial. **Exactly one gate reads it: the door**
(`broker arm-intent`, #1406), which refuses any stated version other than its
own with `schema_version_unsupported`. An ABSENT key is the current version
rather than an unknown one — the field carries a default, so omitting it means
"whatever this contract is at".

Neither the codec nor `validate_intent` nor the JSON Schema reads it, and that
is deliberate: the journal DRAIN is a different entry point, it reads history,
and v1 documents must keep decoding there. So the door refusing `"1"` is not a
claim that v1 is unreadable; it is a claim about what a NEW producer may send.
The compatibility promise on top is a promise about what we EMIT — within a
major version, fields are only ADDED and only as optional — and the CI gate on
the generated artefact is what enforces it.


## The door: submitting a ready document (#1406)

```
alphalens broker arm-intent <path|-> [--env sim|live] [--dry-run] [--format human|json]
```

A producer that can write JSON does not need a command of its own. `arm` parses
a brief, `arm-manual` compiles operator levels, and both then build the same
artefact; this takes that artefact directly. Either a bare `TradeIntent` or one
of this group's own envelopes is accepted — `arm-manual --format json` pipes
straight in — and both roads queue the same bytes. An envelope this door does
not publish is refused rather than peeled hopefully.

**Four gates on the document, in order.** Each answers a different question, and
none of them is implied by another:

| gate | question | refusal |
|---|---|---|
| JSON Schema | is it the published SHAPE? | `intent_malformed` / `schema_violation` |
| the codec | can it be decoded? | `intent_malformed` / `undecodable` |
| the fixed point | does every key you sent survive decoding? | `intent_malformed` / `key_discarded` |
| `validate_intent` | is the document COHERENT? | `intent_invalid` |

The third gate is the one a reader is most likely to think redundant. It is not:
the decoder drops keys it does not model with only a log line, which is sensible
forward compatibility for a daemon reading its own journal and wrong for a door
that arms money. Without it a typo'd `limit_pirce` is discarded and the pick
arms at the price you did NOT send. The parser refuses a repeated JSON key for
the same reason — `json.loads` keeps the last silently.

The second gate is not redundant either, and the reason is worth stating because
it cannot be fixed: JSON Schema defines `integer` as any number with zero
fractional part, so `"generation": 1.0` passes the schema, while the identity
strings built from that field require a real integer (#1371).

**Then two questions about the deployment, not the document.** Is the venue one
this deployment trades (`venue_unsupported`), and does the pick key still take a
write:

| state of `(ticker, trade_date, generation)` | what happens |
|---|---|
| the queue has never seen it | armed — a new pick |
| armed, and the daemon has not placed it | armed — **this is the idempotent replace**, the retry-after-timeout path |
| armed, but already placed | refused, `pick_not_writable` / `already_placed` |
| disarmed or refused | refused, `pick_not_writable` / `generation_spent` |
| a DIFFERENT generation of that key is still armed | refused, `pick_already_armed` |

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
normalises or rescales — same pure-executor doctrine as its siblings. It also
does not refuse a LIVE `--env` when the LIVE rails are absent, because arming is
not placing: the rails gate the daemon, and the guard that does exist here is
the refusal to take the instance off an ambient environment variable (#1377).
Concurrent submitters are not serialised; the key check reads the fold and then
appends, so two processes racing on one key can both pass it — the same shape
`arm-manual` has.
