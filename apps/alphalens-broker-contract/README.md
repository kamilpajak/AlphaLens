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
generator, legacy register), the exit-geometry leaf (`exit_geometry/`) and the
post-fill stop decision (`stop_decision.py`: the daemon's trail and re-anchor
arms over a nine-field view. It was a copy held equal by a parity test until
the daemon called it; since #1581 the daemon calls it, and a golden corpus of
the daemon's own answers is what keeps the behaviour pinned).

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
| | `trade_date_required` | a legacy `"brief"` document with no `meta.trade_date`: day 1 of a brief pick is the session after its brief date, which the door cannot know. Write `"manual"`: the brief producer was removed in #1552 |
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
| `TrailingStop(arm_trigger_r, trail_frac)` | arms a break-even trail at `arm_trigger_r` R of favourable excursion, then trails at `entry + trail_frac * (peak - entry)` — it KEEPS `trail_frac` of the excursion and gives back the rest, so `1.0` parks the stop at the peak |
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
session. Write `"manual"`: `"brief"` marked picks from the `thematic intent`
producer, which #1552 removed, and is kept only as a LEGACY allowance
(`source_brief`).

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
| `intent_id` | `TICKER:DATE:manual` for a manual pick (`TICKER:DATE` for a legacy brief one), `-g<N>` after generation 1 |
| `meta.armed_ts` | the moment of arming; a replace KEEPS the `armed_ts` of the line it replaces |
| `meta.trade_date` | when absent: the next session of `instrument.mic` that has not closed (during a session, that session; after the close or on a holiday, the next one). Required on a legacy `"brief"` document |
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

`broker arm` is the only arming command, and since #1552 every pick is written by
hand: the automatic producer from a brief row (`thematic intent`) was removed so
that every decision in a pick (entries, stop, take-profits, size, and how the stop
is managed) is one the author stated. To trade a brief idea, copy the levels you
want from the brief card into a template. There is no flag form: a pick is a
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
- Manual picks only. A copied legacy brief pick is refused (`trade_date_required`);
  write it again from a template with `"source": "manual"`.
- For LIVE, read `broker_orders/live/picks.jsonl` and arm with `--env live`.
- Lines armed before #1475 (2026-09-16) state a percent size or `meta.brief_date` and
  are refused by the door. Start from a template instead.

## `alphalens broker trades`: one read-only record per pick (#1701)

`alphalens broker trades` answers "what did each pick actually do?". It returns
one record per pick, built from the keeper's journals and, unless `--offline`,
from the broker's own record. A record holds the plan (the last armed
`TradeIntent`, verbatim), the pick's lifecycle, every entry tier, every exit with
its reason, and a gross outcome. The command places, amends and cancels nothing
and writes no journal. The design is
`docs/research/broker_trades_command_design_2026_10_03.md`.

```
alphalens broker trades [--env sim|live] [--format human|json]
                        [--state closed|open|never_filled|unresolved|all]   (default: all)
                        [--since YYYY-MM-DD] [--ticker TICKER] [--pick PICK_KEY]
                        [--limit N | --all]   (default 200, newest trade_date first)
                        [--offline]           (journals only)
```

Examples for the replay consumer:

```
alphalens broker trades --env live --state all --all --format json
alphalens broker trades --env live --pick VST:2026-09-21 --format json
alphalens broker trades --env sim --offline --format json
```

The envelope is `alphalens.broker.trades/v1`. Its JSON Schema is
[`docs/broker-trades-v1.schema.json`](docs/broker-trades-v1.schema.json). It is
generated from the builder's vocabularies
(`python -m alphalens_pipeline.brokers.automanager.trades_schema --write`, which
writes only that file), and a test
fails when the file and a fresh generation differ.

**Versioning.** Within v1, new optional fields may be added, and new values may
be added to every vocabulary below. A consumer must treat an unknown value as
unknown. The published schema lists the values of its release; each addition is
a change to this README in the same PR. Renaming or removing a field or a value,
or changing a type or a unit, needs v2.

**What the schema checks.** Its `$id` is `urn:alphalens:broker:trades:1`, the
schema file for the envelope `schema` value `alphalens.broker.trades/v1`. Every
Measured field has a fixed unit and value type (`price:<ccy>`, `shares`, a
currency code, `%`, `R`, `s`, or no unit), so a string where a number belongs or
a quantity in a price unit fails validation. Objects stay open to unknown keys,
because v1 may add optional fields. Enums are closed and list the values of the
release that generated the file, so validate a body against the schema file of
the release that produced it; an older file refuses a newer value that this
README allows.

**Refusals.** The command adds no failure code. A bad option is `usage`; a broker
without the fill-history capability is `broker_unsupported` (run `--offline` for
the journal half); a broker read error is classified by its exception; a legacy
state layout is `state_layout`. Everything uncertain about a trade is content
and exits 0.

**Every value is a `Measured`:**

```json
{"value": -1.0, "unit": "USD", "source": "venue.bookings", "ref": "trade:6885451891", "null_reason": null}
```

- `unit` is a currency code for money, `shares`, `s`, `%`, `R`, or
  `price:<ccy>` for a price. It is `null` for a time, a rate, an identifier
  (the `uic`) and a code (a MIC, a currency name).
- Every time is RFC 3339 UTC with milliseconds and `Z`.
- `ref` names the row: `order:<id>`, `trade:<id>`, `line:<journal>:<kind>`, or a
  plan path.
- `null_reason` is set exactly when `value` is null.

**Fee and amount signs.** Fees and booking amounts are signed as the venue sends
them: negative is a cost or money out. `commission` and `exchange_fee` are in the
booking currency (USD on LIVE). `fx_conversion` is the conversion charge in the
account currency: the `ConversionRateAccountCurrency` field of the trade's
`Share Amount` booking, about 0.25 % of the traded value. That reading of the
field comes from arithmetic on 25 LIVE rows, not from Saxo's documentation. The
charge is already inside the realized rate, so `pnl_cash_acct` includes it;
`fees.fx_conversion` states it so a consumer can add it back. When one fill is
shared between owners, the pick's share of every booking amount is
`amount × attributed_qty / FilledAmount`, with source `derived` and the warning
`booking_prorated`. Financing, dividends and withholding tax are never included
(`outcome.fees_not_included`): they are not tied to a fill.

**Gross and net.** `outcome.pnl_cash` and `outcome.pnl_cash_acct` are GROSS of
commission and exchange fee. `outcome.net_cash_acct` is the venue's own net of
those two, in the account currency, summed over the record's legs from the
trades report's `BookedAmountAccountCurrency`; its `source` is
`venue.trades_report` when every leg is taken whole and `derived` once a
partial close prorates one. `fees.commission_acct` and `fees.exchange_fee_acct`
are those two fees in the ACCOUNT currency, so a consumer can rebuild the net
and check it; the record carries `net_disagrees_with_fees` when the two differ,
and keeps the venue's figure. Do NOT add `fees.fx_conversion` to either figure:
the charge is already inside both, because the venue books each cash leg at the
rate it converted at.

#### Outcome fields and the replay

Several outcome names are also the replay's, but a shared name is NOT the same
quantity. The mapping, field by field:

| `broker trades` field | unit | replay counterpart |
|---|---|---|
| `outcome.notional_spent` | instrument currency (USD on LIVE) | `fx.notional_spent` (instrument currency). The replay's `summary.notional_spent` is in the account currency, at one stated rate. |
| `outcome.pnl_cash` | instrument currency | `summary.pnl_cash` only after conversion at a stated rate; the replay's is in the account currency |
| `outcome.net_cash_acct` | account currency | none: the replay derives no net. It is the venue's figure, net of commission and exchange fee only |
| `outcome.pnl_cash_acct`, `notional_spent_acct` | account currency | none: they use the realized booking rates at entry and at exit, so they include the FX move between the two dates (VST: 92.04 PLN, against 82.02 PLN at the entry rate) |
| `outcome.denominator_stop` | `price:<ccy>` | `r_multiple.denominator.source` (the stop level, `spec.disaster_stop`) |
| `outcome.risk_per_share` | `price:<ccy>` | `r_multiple.denominator.value` (average entry minus that stop) |
| `outcome.r_multiple` | `R` | `summary.r_multiple`: both gross, both from `spec.disaster_stop` |
| `outcome.mfe_lower_bound` | `price:<ccy>` | the replay's `mfe` is in `R` from bar highs; `mfe_lower_bound / risk_per_share` is at most it. There is no MAE here. |
| `exits[].tp_label` `TPn` | | `tp_fired` tranche index n - 1 |

Units are spelled differently: `price:<ccy>` here is the bare `<ccy>` in the
replay, and `%` here is the replay's `percent`. Times are RFC 3339 here and epoch
milliseconds in the replay (`t`); compare replay bar times only with times whose
`source` is `venue.audit`, because offline times are keeper detection times
(`keeper.*`).

#### Sources

| source | read from today |
|---|---|
| `plan` | the intent document on the armed line |
| `keeper.pick_queue` | `picks.jsonl` |
| `keeper.entry_watch` | `entry_trails.jsonl` and its compaction snapshots |
| `keeper.stop_journal` | `standalone_stops.jsonl` and its compaction snapshots |
| `keeper.submissions` | `submissions.jsonl` |
| `venue.audit` | `/cs/v1/audit/orderactivities` |
| `venue.trades_report` | `/cs/v1/reports/trades` (skipped on SIM) |
| `venue.bookings` | `/cs/v1/reports/bookings` (skipped on SIM) |
| `venue.instrument` | `/ref/v1/instruments/details` (tick size) |
| `derived` | computed by the builder |

A source is a ROLE, not a file, so the keeper repo split does not force a v2.
`sources[role]` in the envelope says whether each was `read`, `skipped` or
`offline`, why not (`offline`, `sim_reports_unusable`, `no_picks`), its read
window and its row count.

#### Null reasons

| null_reason | meaning |
|---|---|
| `offline` | the broker was not read |
| `not_journaled` | the keeper never writes this fact |
| `compacted_before_snapshots` | the fact sits behind `snapshot_horizon` and is in no snapshot |
| `no_audit_row` | the order id is known, but the audit window has no row for it |
| `sim_reports_unusable` | the SIM report endpoints return canned data from other accounts |
| `not_closed` | the position is still open |
| `never_filled` | no entry tier filled |
| `non_positive_risk` | the entry is at or beyond the denominator stop, so R is undefined |
| `ambiguous_attribution` | the fill cannot be given to one pick |
| `legacy_plan_shape` | the value needs a size, and the plan is percent-sized |
| `fx_rate_not_realized` | no `Share Amount` booking with a `ConversionRate` was read for this trade |
| `report_row_missing` | the audit has the fill, but the trades or bookings report has no row for it yet |
| `non_finite` | a computed value was NaN or infinite |
| `stop_amend_history_unavailable` | offline, and the stop's moves cannot be known |
| `account_amount_not_reported` | the report row is there, but the account-currency amount on it is not: no `BookedAmountAccountCurrency` on a trades row, or no `AmountAccountCurrency` on a fee booking row. Distinct from `report_row_missing`, where there is no row at all. One such row nulls the whole member rather than leaving a sum over the others |

#### Exit reasons

Checked in this order in broker mode. Offline, a reason comes from journal
markers only; a stop fill with no marker has `reason` null and
`reason_null_reason` `stop_amend_history_unavailable`. The `trade_alerts`
column maps the daemon's `ExitReason`; a test checks that every member has a row.

| reason | rule | replay event | `trade_alerts.ExitReason` |
|---|---|---|---|
| `take_profit` | owned through a TP or OCO-tp reference | `tp_fired` with tranche index n - 1 for the label TPn (the label is 1-based, the replay's index 0-based); on the last tranche also the zero-unit marker `position_closed(tp_complete)` | `TAKE_PROFIT` |
| `disaster_stop` | a stop order with no price-changing audit row; the fill price is not compared | `position_closed(stop)` | `PLAN_STOP` |
| `trailed_stop` | price-changing rows, and a `trailed` line on the uic between the order's placement and fill within one tick of the last changed price | `stop_moved(trail)`, then `position_closed(stop)` | `TRAILED_STOP` |
| `reanchored_stop` | the same, matched against a `reanchored` line | `stop_moved(reanchor-on-fill)`, then `position_closed(stop)` | `REANCHORED_STOP` |
| `stop_moved_kind_unknown` | price-changing rows, and no marker in the window (lost before the snapshot horizon) | `stop_moved` with a reason that cannot be known, then `position_closed(stop)` | `STOP` |
| `manual_close` | a closing fill with no `ExternalReference` (a heuristic: a machine order with no reference would also match) | none; exclude from replay comparison | - |
| `manual_open` | in `unattributed_fills` only: an opening fill with no reference that no pick owns | none | - |
| `unknown` | anything else, with its evidence in `reason_evidence` | none | - |

The replay event column uses the event kinds and reasons of
`intent_replay.trace` (`KINDS`, `CLOSE_REASONS`, `STOP_MOVE_REASONS`); a test
checks every one. The replay's `time_stop` has no row: LIVE has no time stop by
design. A take-profit fills at its limit or better at the venue, while the
replay's `tp_fired` fills at the plan level; `reason_evidence` names that level,
and `exit_price_off_plan_level` flags a fill worse than it.

#### Attribution

How a fill was given to a pick, in rule order, when picks share a `uic`.

| attribution | rule |
|---|---|
| `journal_tie` | a journal line ties the order to the pick |
| `external_reference` | the order's `ExternalReference` names the pick |
| `position_link` | the closing fill's `RelatedPositionId` equals the `PositionId` of exactly one pick's opening fill (only manual closes carry it; `PositionId`s are never compared with each other) |
| `fifo_fallback` | the remainder went to the oldest open lot, with the warning `attribution_fifo_assumed` |

Quantity left after every lot is closed is listed in `unattributed_fills`, with
`reason` and, when that is null (offline), `reason_null_reason`.

`external_reference` also covers the uic rule of a take-profit reference
`u<uic>-tp<n>-sell` that no `tranche_fired` line ties to a pick: the reference
qualifies the order as a take-profit, and the single pick holding an open lot on
that uic receives it. `reason_evidence` then holds `uic_rule` (AMBA:2026-09-04 on
LIVE). With open lots of two picks the fill is `ambiguous_attribution` instead.

#### States

| state | rule |
|---|---|
| `closed` | the attributed exit quantity equals the entry quantity |
| `open` | entry quantity exceeds exit quantity, and the record is complete enough to say so |
| `never_filled` | no entry tier filled |
| `unresolved` | offline only: lines may be lost, so the builder does not guess |

`counts.all` is taken before `--state`, `counts.selected` after it; both before
`--limit`. `truncated` says that `--limit` cut the list.

#### State reasons

| state_reason | state | meaning |
|---|---|---|
| `refused` | `never_filled` | the pick was refused |
| `disarmed` | `never_filled` | the pick was disarmed before a fill |
| `expired` | `never_filled` | the pick is armed, its tiers ended without a fill, and the last one expired |
| `cancelled` | `never_filled` | the pick is armed, its tiers ended without a fill, and the last one was cancelled |
| `pending` | `never_filled` | the pick is armed, and it has no tier yet, a tier is still open, or its last tier is suspended |
| `compacted_before_snapshots` | `unresolved` | the pick is older than `snapshot_horizon`, or no snapshot exists |
| `not_journaled` | `unresolved` | a now-bracket entry; the journals hold no fill for it |
| `ambiguous_attribution` | `unresolved` | a fill on its uic could not be given to one pick |

#### Warnings

| code | meaning |
|---|---|
| `fold_order_uncertain` | offline: a re-armed crid's history holds a collapsed duplicate line, so its fold order may be wrong |
| `entry_not_in_journal` | an audit fill is an entry of this pick but no journal line names it |
| `audit_report_disagree` | the trades report's executions disagree with the audit's quantity or average price by more than one tick |
| `partial_fill_shape_unverified` | only partial `Fill` rows exist; the last cumulative row was used |
| `journal_audit_disagree` | the journal's fill price or quantity differs from the audit |
| `attribution_fifo_assumed` | a fill was attributed by FIFO (`fifo_fallback`) |
| `exit_qty_exceeds_pick` | the pick's exit order sold more than the pick held; the rest is in `unattributed_fills` |
| `exit_not_in_journal` | offline: an open pick whose stop has no fill line (often closed by the keeper; not a hint of a manual close) |
| `report_lags_audit` | an audit fill has no report row yet; its fees and realized FX are null |
| `booking_type_unmapped` | a trade-tied booking has a `BkAmountType` the builder does not map; its raw value is in `detail` |
| `booking_prorated` | a booking amount was shared between owners by quantity |
| `ambiguous_attribution` | a fill on the pick's uic could not be given to one pick |
| `side_unresolved` | the plan does not resolve a side; outcome math was refused |
| `exit_price_off_plan_level` | a take-profit filled worse than its plan level (`spec.tp_tranches[n-1].price`, also in `reason_evidence`) by more than one tick: the order was not at the plan's level. Broker mode only; a better fill is price improvement and is not flagged |
| `net_disagrees_with_fees` | `outcome.net_cash_acct` differs from `pnl_cash_acct` plus the account-currency fees by more than half a cent. Both sides are vendor amounts over the same rows, so a difference means the fee attribution is wrong somewhere. The record keeps the VENUE's net, which is what the account was charged |

#### Replay exclusions

`replay_exclusions` on each record lists why it cannot be compared with an
`intent-replay` run of its plan. An empty list means it can. A consumer that
compares realized trades with the replay filters on `replay_exclusions == []`,
then strips what the door derives from `plan` (`intent_id`, `meta.armed_ts`,
`spec.tp_tranches[].r_multiple`; the jq recipe under "Writing a manual pick")
and keeps `meta.trade_date`. On the LIVE journals of 2026-10-03 that leaves
EWTX, ASTS, SMMT and VST.

| exclusion | meaning |
|---|---|
| `offline` | the record was built `--offline`: prices and exit reasons are journal values, not the venue's |
| `not_final` | the state is `open` or `unresolved`, so there is no final outcome |
| `manual_close` | an exit is a `manual_close`; the replay has no such event |
| `unknown_exit` | an exit's reason is `unknown` |
| `exit_reason_null` | an exit's reason is null (offline, the stop's moves cannot be known) |
| `ambiguous_attribution` | a fill on the uic could not be given to one pick |
| `entry_mode_unsupported` | a now-bracket tier (`entry_mode: immediate`), which the replay refuses |
| `legacy_plan_shape` | the plan is percent-sized (schema 1 or 2) or absent, which the replay refuses |

No value of a `reason`, `null_reason` or warning code starts with `place_` or
`amend_`: the CLI's no-orders gate flags such strings.
