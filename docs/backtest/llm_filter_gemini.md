# LLM Filter Validation — gemini scorer

Window: 2023-07-01 → 2023-09-30, top-5, 315 picks

```
=== Historical Validation Results ===

Evaluated:        315 picks
  accepted:       298 (94.6%)
  rejected:       4
  uncertain:      13

Forward returns:
  accepted mean:  -0.430%
  rejected mean:  -2.095%
  **delta**:      +1.665% (accept - reject)

Hit rates (fwd return > 0):
  accepted:       43.0%
  rejected:       75.0%

Sharpe proxy (mean/std × √252):
  accepted:       -0.54
  rejected:       -0.97

LLM cost:         $0.01
LLM latency:      1467.4 s (4.66 s/pick)

=== Decision ===
**ITERATE** — marginal signal, wymaga większej sample lub lepszego promptu
  (delta +1.66%, hit-rate delta -32.0 p.p.)
  → Rozszerzyć sample do 90+ dni, przetestować różne scorer prompts.
```
