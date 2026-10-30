# Arming, failure codes and broker JSON

[`README.md`](README.md) in this package is the published contract. It loads on demand; this file
does too (only once Claude reads, writes or edits a file under
`apps/alphalens-broker-contract/`), and neither is re-injected after `/compact`.

Read the relevant README section BEFORE changing the arming or failure-code surface:

Cited by HEADING, never by line: a line number here rots within days, and five of the six that
used to sit in this section pointed at the wrong place — one at a table separator — while reading
as precise.

- **`## The failure contract (#1389)`** — the code table with `retryable` and `owner`, the closed
  `details.reason` vocabularies, and the stderr / exit-status rendering
  (`### How it reaches a caller`). Branch on `code`, never on `retryable`:
  `write_outcome_unknown` is transient in cause and forbidden to retry.
- **`## The door: submitting a document`**, its pick-key state table and
  `### The validity window (#1734)`. The known TOCTOU residual is stated in that window section —
  the key check reads the fold and then appends, so two concurrent submitters on one ticker can
  both pass it.

Rules:

- **The README is authoritative wherever the root `CLAUDE.md` disagrees.** Verified 2026-10-14:
  the root copy's queue-key rule carries no `window_ended` row, which the README table does
  (`grep -c window_ended` → root `CLAUDE.md` 0, README 3). Fix the root; never re-derive the rule
  from it.
- **The code registry is SPLIT and stays split.** `CONTRACT_FAILURE_CODES` in
  `broker_contract/failure.py` holds the broker taxonomy plus `intent_invalid`; everything only a
  command-line program can raise lives in `_CLI_FAILURE_CODES` in
  `apps/alphalens-pipeline/alphalens_cli/commands/_cli_envelope.py` (`commands/broker.py`
  re-exports it for gates that predate the move). Per #1122 the adapter reports
  and the contract never decides. Pinned by
  `apps/alphalens-research/tests/brokers/test_broker_contract_has_no_cli_codes.py`.
- **A new code lands in the README table in the SAME commit** —
  `apps/alphalens-research/tests/brokers/test_broker_failure_codes_published.py` checks both
  directions and the `owner` column.
- **A new JSON-emitting broker command must be added to `_JSON_COMMANDS`** in
  `apps/alphalens-research/tests/brokers/automanager/test_broker_json_contract_cli.py`, with
  the body keys its envelope must carry. `TableCoversEveryJsonCommand` in that file reads the
  commands off the Typer app, so omitting it is a CI failure, not a silent coverage gap. The
  README does not document the JSON envelope — that test is the gate.
