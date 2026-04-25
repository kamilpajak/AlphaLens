# ADR 0002 — Candidate / Queue / Runner core abstraction

- **Status:** Accepted
- **Date:** 2025-09-01
- **Supersedes:** —

## Context

Before this contract, each screener invented its own way to deliver tickers
to deep analysis: Layer 1 wrote dispatch payloads, the original prescreener
shelled out, and ad-hoc scripts mutated state directly. Adding a new screener
meant re-implementing dedup, priority, retry, daily budget, and notifier
plumbing each time.

We needed a single shape for "this is a name worth analysing" and a single
choke point that turned that shape into a TradingAgents run.

## Decision

Three abstractions, all in `alphalens/`:

1. **`Candidate`** (`candidates.py`) — frozen dataclass: `(ticker, source,
   priority, payload, dedup_key)`. The only legal currency between screeners
   and the rest of the system. Sources implementing the `CandidateSink`
   Protocol are the only legal producers.
2. **`CandidateQueue`** (`queue.py`) — SQLite-backed implementation of
   `CandidateSink` at `~/.alphalens/candidates.db`. `UNIQUE(dedup_key)` plus
   priority + retry-window scheduling enforce idempotency and fairness.
3. **`AnalysisWorker` + `TradingAgentsRunner`** (`worker.py`, `runner.py`) —
   `AnalysisWorker` drains the queue, respects daily budget, retries with
   exponential backoff, and dead-letters after 5 failures. `TradingAgentsRunner`
   is the **only** site in the codebase allowed to construct a
   `TradingAgentsGraph`.

Per-screener identity (which pipeline produced what) is decoupled from
per-source priority via `registry.py::SCREENERS` and `SOURCE_PRIORITY`. This
matters because the themed pipeline emits candidates tagged either `momentum`
or `early-stage` depending on the injected scorer — the pipeline is one entry,
the priority is per-source.

## Consequences

- + Adding a screener is one entry in `registry.SCREENERS` plus a class
  emitting `Candidate` objects. Nothing else.
- + Dedup, retry, DLQ, budget, and notifier plumbing are written once.
- + The "only Runner constructs TradingAgentsGraph" rule means LLM cost
  accounting and Gemini config (see `config_gemini.py`) live in one place.
- − Schema changes in `Candidate` or the SQLite store ripple through every
  producer. Mitigated by the early-stage-project posture (ADR-style: break
  freely until users depend on stability).
- ⚠ The `trigger_context` upstream-PR is deferred — runner currently logs the
  per-source trigger string but does not inject it into the TradingAgents
  initial state (see memory `project_pr_signal_context_injection.md`).

## References

- `CLAUDE.md` — "Key abstractions" + "Layered pipeline" sections
- `alphalens/candidates.py`, `alphalens/queue.py`, `alphalens/worker.py`,
  `alphalens/runner.py`, `alphalens/registry.py`
