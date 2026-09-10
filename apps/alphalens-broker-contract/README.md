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
