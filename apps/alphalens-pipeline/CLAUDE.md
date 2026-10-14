# `alphalens-pipeline` — pipeline + CLI

Scope: `alphalens_pipeline/**` and `alphalens_cli/**`. This file loads only once Claude reads,
writes or edits a file under `apps/alphalens-pipeline/`, and it is NOT re-injected after
`/compact`. The workspace dependency direction, where new code goes, and which client owns which
vendor stay in the root `CLAUDE.md`.

## Lazy CLI imports

- Keep every heavy import INSIDE the command body in `alphalens_cli/commands/*.py`. Never promote
  a cross-function duplicate to module level to satisfy a linter.
- Why: the Layer-1 `edgar-detect` timer fires every 15 min, so every invoke pays the import cost.
  A top-level research-tier import was measured at +913 ms per invoke. No test guards startup
  time — re-measure with `time .venv/bin/alphalens --help`.
- Live exemplars, line anchors verified 2026-10-14:
  - `alphalens_cli/commands/feedback.py` — the doctrine is the module-docstring note at lines
    9-11; the first real in-body import is at line 206.
  - `alphalens_cli/commands/broker.py` — the doctrine is the module-docstring note at lines
    77-79; in-body imports at lines 438, 610, 664 and 719. The block at 111-113 is
    `TYPE_CHECKING`-guarded, not a lazy-import site.
- One deliberate top-level exception, with its reasoning written beside it: `broker.py:98-105`
  imports `broker_contract` at module level (~2.6 ms, and `_fail` needs the failure registry on
  every refusal path). Add an exception only with the same kind of measured note.
- `commands/{audit,preaudit}.py` lazy-import `alphalens_research.*` because a top-level
  pipeline -> research import is forbidden (root rule; enforced by
  `apps/alphalens-research/tests/test_module_dependencies.py`). `commands/preregister.py`
  lazy-imports the EXTERNAL `phase_robust_backtesting.ledger`, not the lab tier.

## Vendor quota SCOPE — per-IP vs per-key

| Vendor | Quota scope | Limit | Consequence |
|---|---|---|---|
| SEC EDGAR | per-IP (the User-Agent identifies the caller, it does not scope the quota) | 10 req/s | VPS and Mac hold independent buckets; every process on ONE host shares one bucket, however many keys it has. |
| Alpha Vantage | per API key | 25 req/day (free tier) | A second key is a second pool; one key called from many hosts is still one pool. |
| Polygon | per API key | 5 req/min on the Starter plan ($29/mo) | Quota follows the key, not the host. |
| OpenRouter | per API key | no free tier on v4-pro; prices in `data/alt_data/openrouter_client.py:52-57` | The pipeline uses its own key, so its cost stays separate from review tooling's. |

- The per-IP row is why `data/alt_data/sec_rate_coordinator.py` exists: on the VPS three processes
  (`edgar-detect`, thematic ingest, the companyfacts preload) share one residential IP, each with
  its own in-process throttle, and the aggregate drew a traffic-threshold 403 — epic #379. It
  serialises the next-allowed-call wall timestamp through one flock'd file under `~/.alphalens/`,
  holding the lock only for read-reserve-write and never across the sleep.
- Each client's module docstring states its own current limit (`sec_edgar_client.py:1-6`,
  `alphavantage_client.py:7`, `polygon_client.py:3`, `openrouter_client.py:52-57`). Read those
  before trusting the table; correct both when a plan changes.

## Calendars

- `alphalens_pipeline/market/calendar.py` is exchange-parametrized: every helper takes
  `exchange: str = DEFAULT_EXCHANGE`, and `DEFAULT_EXCHANGE = "XNYS"` (line 80). Pass the
  ISO 10383 MIC; never hard-code venue hours or `open + 390 minutes`.
