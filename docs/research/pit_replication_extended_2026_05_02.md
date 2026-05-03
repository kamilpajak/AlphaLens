# Extended PIT replication probe — PASS

**Date:** 2026-05-02
**Aggregate Pearson:** 0.9991 (threshold ≥ 0.95)
**Per-ticker threshold:** ≥ 0.85 (testable tickers only)
**Min testable tickers:** 3

## Per-ticker outcomes

| Ticker | Pearson | n pairs | Status | Note |
|---|---|---|---|---|
| AAPL | 0.9990 | 8 | ✅ PASS | mega-cap baseline (replicates v1 PIT probe Pearson 0.9990) |
| SPY | 0.9984 | 8 | ✅ PASS | index proxy — should be most stable PIT case |
| TSLA | 0.9998 | 8 | ✅ PASS | high-vol normal stock — stresses vendor IVP percentile under wide swings |
| SIVB | NaN | 0 | ⚠ UNTESTABLE | bank distress (pre-halt window 2022-04 to 2023-02) |
| FRC | NaN | 0 | ⚠ UNTESTABLE | bank distress (pre-halt window 2022-05 to 2023-04) |

**Verdict: PASS**

- aggregate gate: ✅ (0.9991 vs ≥ 0.95)
- per-ticker gate: ✅ (0 testable tickers below 0.85)
- diversity gate: ✅ (3/3 testable tickers)
- no errors: ✅

## UNTESTABLE caveat

2 ticker(s) marked UNTESTABLE: SIVB, FRC.

Cause: iVolatility's equity-keyed `/equities/eod/ivx` endpoint
drops historical IVX series for delisted tickers (vendor archive
limitation documented in probe v5 memory). For these tickers the
raw IVX backward window cannot be reconstructed empirically, so
vendor IVP cannot be cross-referenced against a locally-computed
value. Vendor smd snapshots themselves DO preserve historical
ivp30/ivx30 across delisting (probe v5 99.5% T1 retention), but
we cannot independently audit them. PIT correctness for these
tickers is INFERRED from the active-ticker fidelity (Pearson
0.999+), not directly tested.

Implication for v7: distress-event cross-section rows (e.g.
SIVB at 2023-Q1) use vendor smd values which we trust by
extension from active-ticker PIT verification.
