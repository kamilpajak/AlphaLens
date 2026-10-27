# Arming, failure codes and broker JSON

[`README.md`](README.md) in this package is the published contract. It loads on demand; this file
does too (only once Claude reads, writes or edits a file under
`apps/alphalens-broker-contract/`), and neither is re-injected after `/compact`.

Read the relevant README section BEFORE changing the arming or failure-code surface. Sections are
named by HEADING, not by line number: this file carried line anchors until 2026-10-27 and the
cashbook work (#1789, #1806) inserted 43 lines above them, so every one of them pointed at the
wrong row.

- **`## The failure contract`** — the code table with `retryable` and `owner`, the closed
  `details.reason` vocabularies, and the stderr / exit-status rendering
  (`### How it reaches a caller`). Branch on `code`, never on `retryable`:
  `write_outcome_unknown` is transient in cause and forbidden to retry.
- **`## The door: submitting a document`**, its pick-key state table (the prose anchor is
  `**The pick key.**`) and `### The validity window`. The known TOCTOU residual is stated under
  `**What the door does NOT do.**` — the key check reads the fold and then appends, so two
  concurrent submitters on one ticker can both pass it.

Rules:

- **The README is authoritative wherever any other file disagrees.** It used to disagree with a
  queue-key rule in the root `CLAUDE.md`; #1782 deleted that copy, so there is nothing left to
  reconcile. Do not re-introduce one. The root keeps only the arming-door sentence and a pointer
  here.
- **The code registry is SPLIT and stays split.** `CONTRACT_FAILURE_CODES` in
  `broker_contract/failure.py` holds the eight broker-taxonomy codes plus `intent_invalid`, nine
  in all; everything only a command-line program can raise lives in `_CLI_FAILURE_CODES` in
  `apps/alphalens-pipeline/alphalens_cli/commands/_cli_envelope.py`. `commands/broker.py` only
  re-exports the name, and a function's globals are the module it was DEFINED in, so patching it
  on `broker.py` reaches nothing. `_FAILURE_CODES` in the same module is the merged view. Per
  #1122 the adapter reports and the contract never decides. Pinned by
  `apps/alphalens-research/tests/brokers/test_broker_contract_has_no_cli_codes.py`.
- **A new code lands in the README table in the SAME commit** —
  `apps/alphalens-research/tests/brokers/test_broker_failure_codes_published.py` checks both
  directions and the `owner` column.
- **A retired field stays in the legacy register.** `broker_contract/trade_intent/legacy.py` names
  each one, and every code site that still reads it carries a `# LEGACY(<key>)` marker. Dropping
  the field without clearing the register, or the marker without the entry, turns
  `apps/alphalens-research/tests/trade_intent/test_legacy_register.py` red in both directions.
- **A new JSON-emitting broker command must be added to `_JSON_COMMANDS`** in
  `apps/alphalens-research/tests/brokers/automanager/test_broker_json_contract_cli.py`, with the
  body keys its envelope must carry. `TableCoversEveryJsonCommand` in that file reads the commands
  off the Typer app, so omitting it is a CI failure, not a silent coverage gap. The README
  publishes only the `trades` envelope (`## alphalens broker trades`); for every other command
  that test is the gate.
