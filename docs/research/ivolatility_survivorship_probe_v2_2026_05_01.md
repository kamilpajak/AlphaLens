# iVolatility survivorship probe v2 verdict — PASS

**Date:** 2026-05-01
**Sample:** n=200
**Strict retention (T1+T2):** 99.5%  *production-ready or variant-resolvable*
**Reachable retention (T1+T2+T3):** 99.5%  *includes chain-only — not feature-extractable at scale*

## Tier hierarchy

- **T1** (equity-direct): 199  *original ticker → ivx hit, production-ready*
- **T2** (equity-via-variant): 0  *needs ETL ticker mapping, look-ahead bias risk*
- **T3** (chain-only): 0  *FAIL for v7 — 90M calls infeasible*
- **T4** (missing): 1  *FAIL*

## Gates (strict T1+T2 per zen CR)

| Gate | Observed | Threshold | Pass |
|------|----------|-----------|------|
| overall_strict_retention | 99.5% | ≥ 95.0% | ✅ |
| acquisition_strict_retention | 98.6% | ≥ 95.0% | ✅ |
| distress_strict_retention | 100.0% | ≥ 85.0% | ✅ |

## By delisting reason

| Reason | n | strict (T1+T2) | reachable (+T3) | T1 | T2 | T3 | T4 |
|--------|---|----------------|-----------------|----|----|----|----|
| acquisition | 70 | 98.6% | 98.6% | 69 | 0 | 0 | 1 |
| unknown | 130 | 100.0% | 100.0% | 130 | 0 | 0 | 0 |

## Ground-truth diagnostic

| Ticker | Expectation | Tier | Resolved as |
|--------|-------------|------|-------------|
| TWTR | Twitter taken private 2022-10-27 (v1 PASS — expected T1) | T1 | TWTR |
| SIVB | SVB Financial halted 2023-03-10 (v1 FAIL — expected T2 via SIVBQ) | T1 | SIVB |
| SBNY | Signature Bank halted 2023-03-12 (v1 PASS — expected T1) | T1 | SBNY |
| FRC | First Republic halted 2023-05-01 (v1 FAIL — expected T2 via FRCB) | T1 | FRC |
| ATVI | Activision acquired 2023-10-13 (v1 PASS — expected T1) | T1 | ATVI |
| VMW | VMware acquired 2023-11-22 (v1 PASS — expected T1) | T1 | VMW |
| SPLK | Splunk acquired 2024-03-18 (v1 PASS — expected T1) | T1 | SPLK |

Audit JSON: `docs/research/ivolatility_survivorship_probe_v2_2026_05_01.json`
