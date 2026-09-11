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
