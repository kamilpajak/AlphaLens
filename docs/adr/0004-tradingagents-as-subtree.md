# ADR 0004 — TradingAgents as vendored git subtree

- **Status:** Superseded by [ADR 0008](0008-sunset-tradingagents-integration.md) (2026-04-30)
- **Date:** 2025-08-10
- **Supersedes:** —

## Context

AlphaLens depends heavily on TauricResearch's TradingAgents — a multi-agent
LLM trading framework that drives Layer 3 deep analysis. Three options were
considered for managing the dependency:

1. **PyPI install** — pin to a published version. Clean dependency graph, but
   TradingAgents is a young project with no stable release cadence and we
   needed local patches (Gemini 429 retry, config overrides).
2. **Git submodule** — preserves history, but submodules are awkward in
   monorepos and require contributors to learn submodule etiquette.
3. **Vendored git subtree** — squash upstream commits into a subdirectory,
   sync periodically with `git subtree pull`.

We needed: ability to apply local patches without forking publicly, low
contributor friction, and the option to send patches back upstream as PRs.

## Decision

TradingAgents is vendored at `TradingAgents/` via `git subtree --squash` from
`TauricResearch/TradingAgents`. The CLI package on our side is named
`alphalens_cli/` (not `cli/`) to avoid namespace collision with TradingAgents'
own `cli/` package.

Sync is manual on a roughly monthly cadence:

```bash
git subtree pull --prefix=TradingAgents \
  https://github.com/TauricResearch/TradingAgents.git main --squash
```

After each pull we reapply our patches. Currently this is just the Gemini 429
retry in `TradingAgents/tradingagents/llm_clients/google_client.py`. The
intent is to upstream them as PRs to keep the patch surface minimal (tracked
in memory: `project_pr_tradingagents_retry.md`,
`project_pr_signal_context_injection.md`).

## Consequences

- + No external dependency for a young, fast-moving project.
- + Local patches are safe; we are not blocked on upstream review cycles.
- + Single repo to clone, single virtualenv, no submodule choreography.
- − Sync conflicts at three known points (Gemini retry, config defaults,
  CLI naming). Manageable but real friction.
- − Disk + clone size are larger than a pip dep would be.
- ⚠ Edits to `TradingAgents/` must remain mergeable with upstream syncs.
  Anything that diverges meaningfully should be either a PR back upstream or
  a local wrapper in `alphalens/`. New code goes in `alphalens/`, **never**
  in `TradingAgents/`.

## References

- `CLAUDE.md` — "Upstream relationship" section
- Memory: `feedback_upstream_sync_workflow.md`,
  `project_pr_tradingagents_retry.md`, `project_pr_signal_context_injection.md`
