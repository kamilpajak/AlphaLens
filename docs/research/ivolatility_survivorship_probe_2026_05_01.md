# iVolatility survivorship probe verdict — FAIL

**Date:** 2026-05-01
**Sample:** n=200 (decidable=200, indeterminate=0)
**Overall retention:** 51.0% (denominator excludes rate-limit-exhausted indeterminate)
**Symbol-mismatch in retained:** 0.0%

## Gates

| Gate | Observed | Threshold | Pass |
|------|----------|-----------|------|
| overall_retention | 51.0% | ≥ 95.0% | ❌ |
| acquisition_retention | 45.7% | ≥ 95.0% | ❌ |
| unknown_retention | 53.8% | ≥ 85.0% | ❌ |
| symbol_integrity | 0.0% | ≥ 0.0% | ✅ |

## By delisting reason

| Reason | n | retained | retention % |
|--------|---|----------|-------------|
| acquisition | 70 | 32 | 45.7% |
| unknown | 130 | 70 | 53.8% |

## By endpoint

| Endpoint | retained | retention % | tariff_denied |
|----------|----------|-------------|---------------|
| stock-prices | 102 | 51.0% | 0 |
| ivx | 45 | 22.5% | 0 |
| ivs | 49 | 24.5% | 0 |

## Ground-truth diagnostic (manual probe corroboration)

| Ticker | Expectation | Any data | stock-prices | ivx | ivs |
|--------|-------------|----------|--------------|-----|-----|
| TWTR | Twitter taken private 2022-10-27 (manual probe PASS) | ✅ | 19 | 19 | 338 |
| SIVB | SVB Financial halted 2023-03-10 (manual probe FAIL) | ❌ | 0 | 0 | 0 |
| SBNY | Signature Bank halted 2023-03-12 (manual probe PASS) | ✅ | 25 | 10 | 338 |
| FRC | First Republic halted 2023-05-01 (manual probe FAIL) | ❌ | 0 | 0 | 0 |
| ATVI | Activision acquired 2023-10-13 (manual probe PASS) | ✅ | 20 | 19 | 338 |
| VMW | VMware acquired 2023-11-22 (manual probe PASS) | ✅ | 20 | 20 | 338 |
| SPLK | Splunk acquired 2024-03-18 (manual probe PASS) | ✅ | 20 | 19 | 338 |

Audit JSON: `docs/research/ivolatility_survivorship_probe_2026_05_01.json`
