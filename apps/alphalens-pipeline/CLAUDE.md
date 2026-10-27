# `alphalens-pipeline` — pipeline + CLI

Scope: `alphalens_pipeline/**` and `alphalens_cli/**`. This file loads only once Claude reads,
writes or edits a file under `apps/alphalens-pipeline/`, and it is NOT re-injected after
`/compact`. The workspace dependency direction, where new code goes, and the rule that every
vendor call goes through that vendor's canonical client stay in the root `CLAUDE.md`. The root no
longer lists WHICH client owns which vendor — #1782 cut that table — so read
`alphalens_pipeline/data/` for the list.

## Lazy CLI imports

- Keep every heavy import INSIDE the command body in `alphalens_cli/commands/*.py`. Never promote
  a cross-function duplicate to module level to satisfy a linter.
- Why: the Layer-1 `edgar-detect` timer fires every 15 min, so every invoke pays the import cost.
  A top-level research-tier import was measured at +913 ms per invoke. No test guards startup
  time — re-measure with `time .venv/bin/alphalens --help`.
- Live exemplars. Both carry the doctrine as a module-docstring note; `broker.py` holds 99
  indented imports, so find them rather than counting on a line number (an earlier version of
  this file listed four, and three of the four anchors had drifted within two weeks):
  `grep -nE '^[[:space:]]+(from|import) ' alphalens_cli/commands/broker.py`
  - Not every indented import is a lazy one: the three under `if TYPE_CHECKING:` never run at
    import time and are not an exemplar.
- One deliberate top-level exception, with its reasoning written beside it: the
  `broker_contract` imports in `broker.py` under the comment that opens `# Top-level rather than
  lazy` (~2.6 ms, and `_fail` needs the failure registry on every refusal path). Add an exception
  only with the same kind of measured note.
- `commands/{audit,doctrine,preaudit}.py` lazy-import `alphalens_research.*` because a top-level
  pipeline -> research import is forbidden. That rule's gate
  (`apps/alphalens-research/tests/test_module_dependencies.py`) walks the `alphalens_pipeline`
  package directory, and `alphalens_cli` is a SIBLING of it: a top-level lab import added to
  `commands/audit.py` turns nothing red, so the convention is all that holds here.
  `commands/preregister.py`
  lazy-imports the EXTERNAL `phase_robust_backtesting.ledger`, not the lab tier.

## Vendor quota SCOPE — per-IP vs per-key

| Vendor | Quota scope | Limit | Consequence |
|---|---|---|---|
| SEC EDGAR | per-IP (the User-Agent identifies the caller, it does not scope the quota) | 10 req/s | VPS and Mac hold independent buckets; every process on ONE host shares one bucket, however many keys it has. |
| Alpha Vantage | per API key | 25 req/day (free tier) | A second key is a second pool; one key called from many hosts is still one pool. |
| Polygon | per API key | 5 req/min on Stocks Basic, the free tier this project is on (Starter, $29/mo, is unlimited) | Quota follows the key, not the host. |
| OpenRouter | per API key | no free tier on v4-pro; prices in `data/alt_data/openrouter_client.py:52-57` | The pipeline uses its own key, so its cost stays separate from review tooling's. |

- The per-IP row is why `data/alt_data/sec_rate_coordinator.py` exists: on the VPS three processes
  (`edgar-detect`, thematic ingest, the companyfacts preload) share one residential IP, each with
  its own in-process throttle, and the aggregate drew a traffic-threshold 403 — epic #379. It
  serialises the next-allowed-call wall timestamp through one flock'd file under `~/.alphalens/`,
  holding the lock only for read-reserve-write and never across the sleep.
- Each client's module docstring states the limit AS LAST MEASURED, with the date it was taken
  (`sec_edgar_client.py`, `alphavantage_client.py`, `polygon_client.py`, `openrouter_client.py`).
  Read the date before trusting the figure: `openrouter_client.py` still carries a 2026-05-30
  snapshot of a promo that expired 2026-05-31, so its prices are stale by months. Correct the
  docstring and this table together when a plan changes.

## broker CLI surface

The `arm` command, the failure-code registry and the JSON envelope are all in
`alphalens_cli/commands/`, so they are edited HERE even though the contract they serve lives in
`apps/alphalens-broker-contract/`. Three different files: `commands/broker.py` holds the command
and the refusal-to-failure-code mapping, `commands/_cli_envelope.py` holds the envelope and the
registry (moved there by #1789), and the door's own gates, refusal classes and key check are
pipeline-side in `alphalens_pipeline/brokers/automanager/intent_door.py`. Read that package's
[`README.md`](../alphalens-broker-contract/README.md) `## The door` and `## The failure contract`
before changing any of them; it is authoritative and the gate order, the pick-key state table and
the TOCTOU residual are stated there, not here.

- **One JSON contract.** Every JSON-emitting command takes `--format human|json` (never a bare
  `--json`; the alias survives only on `reconcile` / `reconcile-fills`, which shipped before the
  option), builds its payload with `_envelope(schema, env, ...)` so `schema` and `env` lead, and
  prints it through `_emit_json`: ONE strict single-line JSON value on stdout, `allow_nan=False`
  (NaN/inf and circular data are refused, not emitted), `default=str` for other unknown types.
  Stdout is one value or empty. `arm` answers `alphalens.broker.arm/v2`.
  - Two commands render BEFORE they act instead: `arm` and `trades` call `_render_json` and echo
    the string. A payload that cannot be serialised must fail before the journal append, not
    after it.
- A new JSON-emitting command must be added to `_JSON_COMMANDS` in
  `apps/alphalens-research/tests/brokers/automanager/test_broker_json_contract_cli.py` with its
  required body keys — the table test reads the commands off the Typer app, so forgetting is a CI
  failure, not silent coverage loss. `auth`, `marketdata-auth`, `price-reader` and `manage` take
  no `--format`.
- `--help` renders without Rich frames whenever stdout is not a TTY: one `_help_markup_mode()`
  assignment on the ROOT app in `alphalens_cli/main.py` covers every group.
- **Branch on `code`, never on `retryable`.** `retryable` means "safe to re-run WITHOUT reconciling
  first", not "the cause was transient" — `write_outcome_unknown` has a transient cause and a
  forbidden retry, because the order may already rest at the broker.
- **The code registry is SPLIT and stays split:** `broker_contract/failure.py` owns the nine
  contract codes, `commands/_cli_envelope.py` (`_CLI_FAILURE_CODES`) owns the CLI concepts, and
  `_FAILURE_CODES` in the same module is the merged view. `broker.py` only re-exports the names.
  Putting a CLI concept in the shared package is the #1122 mistake again. A `_fail_with` call with an
  unregistered code is a CI failure (AST gate) and a logged warning at runtime.
- **Only `broker manage` places or amends orders.** An AST gate bars `place_*`/`amend_*` in every
  module of TWO roots — `alphalens_cli` and `alphalens_pipeline.account_cash`, the second added
  by #1689 because a pipeline-side helper that holds a broker is the shape the gate used to miss
  (#1466) — and allows only cancelling. There is no carve-out for `commands/broker.py`. The one
  placement path in neither root is `apps/alphalens-research/scripts/first_fill/`, which places
  SIM orders through the wrapper.

## Calendars

- `alphalens_pipeline/market/calendar.py` is exchange-parametrized: every helper takes
  `exchange: str = DEFAULT_EXCHANGE`, and `DEFAULT_EXCHANGE = "XNYS"` (line 80). Pass the
  ISO 10383 MIC; never hard-code venue hours or `open + 390 minutes`.
