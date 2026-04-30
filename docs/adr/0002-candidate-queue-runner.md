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
choke point that turned that shape into whatever downstream consumer was
attached at the time.

## Decision

Two abstractions, both in `alphalens/`:

1. **`Candidate`** (`candidates.py`) — frozen dataclass: `(ticker, source,
   priority, payload, dedup_key)`. The only legal currency between screeners
   and the rest of the system. Sources implementing the `CandidateSink`
   Protocol are the only legal producers.
2. **`CandidateQueue`** (`queue.py`) — SQLite-backed implementation of
   `CandidateSink` at `~/.alphalens/candidates.db`. `UNIQUE(dedup_key)` plus
   priority + retry-window scheduling enforce idempotency and fairness.

Originally a third abstraction (`AnalysisWorker` + a screener-agnostic runner)
drained the queue and forwarded each candidate to a per-stock LLM analysis
pipeline. That consumer was removed by [ADR 0008](0008-sunset-tradingagents-integration.md);
the queue still records candidates as a historical log, but no live process
drains them today.

Per-screener identity (which pipeline produced what) is decoupled from
per-source priority via `registry.py::SCREENERS` and `SOURCE_PRIORITY`. This
matters because the themed pipeline emits candidates tagged either `momentum`
or `early-stage` depending on the injected scorer — the pipeline is one entry,
the priority is per-source.

## Consequences

- + Adding a screener is one entry in `registry.SCREENERS` plus a class
  emitting `Candidate` objects. Nothing else.
- + Dedup, retry, DLQ, budget, and notifier plumbing are written once.
- + Decoupling the queue from any specific consumer made it cheap to remove
  the original Layer 3 runner (ADR 0008) without touching producers.
- − Schema changes in `Candidate` or the SQLite store ripple through every
  producer. Mitigated by the early-stage-project posture (ADR-style: break
  freely until users depend on stability).

## References

- `CLAUDE.md` — "Key abstractions" + "Layered pipeline" sections
- `alphalens/core/candidates.py`, `alphalens/core/queue.py`,
  `alphalens/core/registry.py`
