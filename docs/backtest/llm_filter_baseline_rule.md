# LLM Filter Validation — rule scorer

Window: 2023-07-01 → 2023-09-30, top-5, 315 picks

```
=== Historical Validation Results ===

Evaluated:        315 picks
  accepted:       312 (99.0%)
  rejected:       3
  uncertain:      0

Forward returns:
  accepted mean:  -0.456%
  rejected mean:  -4.597%
  **delta**:      +4.141% (accept - reject)

Hit rates (fwd return > 0):
  accepted:       43.6%
  rejected:       33.3%

Sharpe proxy (mean/std × √252):
  accepted:       -0.56
  rejected:       -5.84

LLM cost:         $0.00
LLM latency:      0.0 s (0.00 s/pick)

=== Decision ===
**DEPLOY** — LLM reject-rate correlates with underperformance
  (delta +4.14% AND hit-rate delta +10.3 p.p.)
  → Integracja 3-tier adaptive architecture warta kosztów.
```
