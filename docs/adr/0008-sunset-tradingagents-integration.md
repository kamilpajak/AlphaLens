# ADR 0008 — Sunset TradingAgents integration

- **Status:** Accepted
- **Date:** 2026-04-30
- **Supersedes:** [ADR 0004](0004-tradingagents-as-subtree.md)

## Context

ADR 0004 (2025-08-10) vendored TauricResearch/TradingAgents as a `git subtree`
under `TradingAgents/` so that AlphaLens could call `TradingAgentsGraph.propagate()`
for per-stock multi-agent LLM analysis (Layer 3 in the layer architecture from
ADR 0007). At the time, every Layer 2 screener candidate was meant to feed into
that pipeline via the unified candidate queue (ADR 0002).

By 2026-04-30 the project has logged 10/10 paradigm failures across three
architectural layers (paradigm_failures_postmortem.md). Every screener built on
top of the TradingAgents runner has been killed under phase-robust testing —
themed, mom+lowvol combo, tri-factor, regime-gate, quality+momentum, and the
vol-target overlay. The Layer 3 worker (`com.alphalens.watchdog.worker.plist`,
5-minute interval) became dormant: the queue is no longer drained, no live
strategy needs the multi-agent verdict, and re-enabling the worker would only
accumulate Gemini quota burn against research that is already concluded.

The vendored subtree carries a maintenance tax that no longer pays for itself:

- A custom Gemini 429 retry patch in `google_client.py` that has to be
  re-applied after every `git subtree pull`.
- A planned-but-deferred upstream PR to inject `trigger_context` into
  `TradingAgentsGraph.propagate()`.
- Transitive dependencies (langchain-anthropic, langchain-openai, finnhub,
  langgraph, …) that AlphaLens itself does not use.
- ~30 production-code consumers spread across `alphalens/core/`,
  `alphalens_cli/commands/`, `alphalens/backtest/llm_scorers.py`, and
  `alphalens/data/fundamentals/fetcher.py`.

If TradingAgents is needed in a future research session, the user will clone
it into a separate working directory and run it manually. AlphaLens itself
should be cleanly decoupled.

## Decision

Remove the TradingAgents vendored subtree and every live consumer; reimplement
the two RESEARCH_ONLY paths that still need similar functionality standalone.

**Hard-delete:**

- `TradingAgents/` (entire subtree).
- `alphalens/core/runner.py` (`TradingAgentsRunner`).
- `alphalens/core/worker.py` (`AnalysisWorker` — depended on the runner).
- `alphalens/core/config_gemini.py` (`build_gemini_config()` — wrapper around
  upstream `DEFAULT_CONFIG`).
- `alphalens/watchdog/lock.py` (`worker_lock` — orphaned with the worker).
- `alphalens_cli/commands/analyze.py` (`alphalens analyze TICKER`).
- The `alphalens queue process` subcommand and `_build_worker` factory.
- The `alphalens research historical-acceptance` subcommand and its helper
  block (~640 lines of Layer-3-acceptance sampling machinery).
- `tradingagents_reduced_scorer` from `alphalens/backtest/llm_scorers.py`.
- Tests that exercise the deleted code (`test_runner.py`,
  `test_runner_context.py`, `test_worker.py`, `test_worker_lock.py`,
  `test_analyze_cli.py`, `test_config_gemini.py`,
  `test_research_historical_acceptance.py`, `test_google_api_key.py`,
  `test_model_validation.py`, `test_ticker_symbol_handling.py`).

**Reimplement standalone (preserve research surface):**

- `alphalens/data/fundamentals/fetcher.py` — replace the four lazy
  `from tradingagents.dataflows.alpha_vantage_fundamentals import …` imports
  with direct stdlib `urllib.request` calls to the Alpha Vantage REST API
  (`OVERVIEW / BALANCE_SHEET / CASH_FLOW / INCOME_STATEMENT`) plus a ported
  `_filter_reports_by_date` PIT helper. Public surface
  (`fetch_ticker_bundle`, `extract_features`) stays identical so tests and
  downstream PIT consumers (`data/store/fundamentals_pit.py`,
  `archive/screeners/themed/*`) continue to work.
- `alphalens_cli/commands/guru.py` (Layer 2f GuruAgent pilot) — replace
  `tradingagents.llm_clients.google_client.GoogleClient(...).get_llm()` with
  direct `langchain_google_genai.ChatGoogleGenerativeAI` construction plus a
  small inline subclass that normalizes Gemini-3 list-of-blocks `.content`
  back to a string (mirroring the upstream `normalize_content` helper).

**Dependencies:** drop `tradingagents` and the `[tool.uv.sources]` entry from
`pyproject.toml`; promote previously-transitive dependencies that AlphaLens
itself uses to direct deps (`langchain-google-genai>=4.2.2`, `typer>=0.21.1`,
`yfinance>=0.2.63`, `stockstats>=0.6.5`, `python-dotenv>=1.0`); drop ruff
exclude and coverage omit entries for `TradingAgents/`.

**Launchd:** archive `com.alphalens.watchdog.worker.plist` and the
`launchd/bin/alphalens-worker` shell wrapper; the user runs
`launchctl unload ~/Library/LaunchAgents/com.alphalens.watchdog.worker.plist`
manually after merge.

**Manual cleanup (out of repo):** `rm -rf ~/.tradingagents/` (cache + logs
created by the dormant worker; AlphaLens never read from this directory
itself).

## Consequences

**Positive:**

- ~10K LOC of vendored code removed; transitive deps shrink substantially.
- No more "remember to re-apply the Gemini 429 retry patch after subtree pull"
  ceremony.
- AlphaLens layer architecture (ADR 0007) is unambiguous — Layer 3 is now the
  vectorized backtest engine, full stop. The "TradingAgents-as-Layer-3" framing
  that lingered from ADR 0004 is gone.
- The candidate queue (`~/.alphalens/candidates.db`) remains on disk as a
  historical record of past Layer 1 detection events. No CLI viewer is
  shipped — query directly with sqlite if needed.

**Negative:**

- `alphalens analyze TICKER` (one-shot deep analysis) is gone. If the user
  wants this, they run TradingAgents from a separate clone.
- `alphalens research historical-acceptance` (Layer 3 acceptance sampler)
  is gone. The methodology lives on in
  `docs/research/strategy_validation_playbook.md`; rebuilding it later
  would require either a new LLM-scorer harness or wiring TradingAgents
  back in from outside the repo.
- The `tradingagents` choice in `alphalens research validate-llm-filter
  --scorer` is gone (only `rule`, `gemini`, `hybrid` remain).
- Layer 1 watchdog still detects EDGAR events and writes them to the queue,
  but no consumer drains them — they accumulate in `~/.alphalens/candidates.db`
  as a historical log only.

**Reversibility:** restoring TradingAgents would require re-running
`git subtree add --prefix=TradingAgents …`, re-introducing the
`[tool.uv.sources]` entry, re-applying the Gemini 429 retry patch, and
rewriting the runner/worker. The deletion is intentionally hard — partial
shims would just defer the cost.

## References

- Supersedes: [ADR 0004 — TradingAgents as vendored git subtree](0004-tradingagents-as-subtree.md)
- Layer architecture: [ADR 0007](0007-layer-architecture.md)
- Closed-layer policy: [ADR 0005](0005-closed-layers-as-anti-pattern-catalog.md)
- Per-layer kill rationale: [`docs/research/paradigm_failures_postmortem.md`](../research/paradigm_failures_postmortem.md)
- Worker plist + closure entry: [`launchd/archived/README.md`](../../launchd/archived/README.md)
