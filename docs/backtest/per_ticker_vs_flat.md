# Layer 2b: flat 100 bps vs. per-ticker cost model

- Window: 2021-04-19 → 2026-04-17
- Portfolio value: $100,000
- Top-N: 5, holding: 5, weighting: linear

## Side-by-side

| Metric | Gross | Flat 100 bps | Per-ticker |
| --- | ---: | ---: | ---: |
| Sharpe | +1.418 | +1.403 | -1.927 |
| Annual drag (bps) | 0 | 100 | 22571.9 |

## Decision gate

- Δ Sharpe (per-ticker vs. flat 100 bps): **-3.330**
- **INVESTIGATE** (|Δ| > 0.3): per-ticker model materially changes the decision. Inspect top-10 costliest tickers + per-theme breakdown.

## Top-10 kosztowne tickery (per-ticker model)

| Ticker | Enters | Exits | Total cost (USD) | Bps of NAV |
| --- | ---: | ---: | ---: | ---: |
| AXTI | 43 | 48 | $24,511.85 | 2451.18 |
| INOD | 47 | 47 | $21,843.41 | 2184.34 |
| GERN | 52 | 57 | $21,455.83 | 2145.58 |
| BELFB | 86 | 90 | $19,480.59 | 1948.06 |
| NVTS | 20 | 20 | $18,715.67 | 1871.57 |
| AEHR | 42 | 47 | $18,464.00 | 1846.40 |
| VKTX | 29 | 35 | $17,927.94 | 1792.79 |
| MDGL | 44 | 46 | $17,059.82 | 1705.98 |
| ACHR | 34 | 41 | $16,066.24 | 1606.62 |
| POET | 31 | 35 | $15,881.20 | 1588.12 |

**Koncentracja**: top-5 = 11.5% całego kosztu.

## Decompozycja per-theme

| Temat | Cost (USD) | % całości |
| --- | ---: | ---: |
| biotech | $287,284.45 | 31.1% |
| quantum | $270,684.52 | 29.3% |
| ai | $267,514.30 | 29.0% |
| semis | $97,994.83 | 10.6% |
