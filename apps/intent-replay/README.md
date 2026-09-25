# intent-replay

A research backtester that takes a `TradeIntent` document (the same JSON that
`alphalens broker arm` accepts) plus price bars, and reports what the document
would have done.

It is a research tool. It stamps nothing, charges no multiplicity budget, and
carries no accrued history. Every value it needs is read from the document or
stated in the run configuration; nothing is inherited from a deployment, an
environment variable or a production constant. A missing value is a refusal,
never a default.

Design: `docs/superpowers/specs/2026-09-23-intent-replay-design.md`.
Implementation plan: `docs/superpowers/plans/2026-09-25-intent-replay-step1.md`.
