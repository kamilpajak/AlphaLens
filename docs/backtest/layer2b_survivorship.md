# Layer 2b survivorship-bias probe — Test B

Test B z bateri falsyfikacyjnej zaproponowanej przez Perplexity. Augmentuje
curated 113-name universe o **delisted thematic small/mid caps** (biotechs
acquired lub liquidated, semi photonics, consumer robotics) zidentyfikowane
przez Polygon `active=false` sweep 2021-06-01 → 2026-04-17.

- **Okno**: 2021-06-01 → 2026-04-17 (Polygon plan boundary; oryginalny backtest 2021-04-19)
- **Discovered**: 4265 tickerów zniknęło między 2021 a 2026
- **Po liquidity filter** ($3M+ ADV, $2+ price): 969
- **Thematic matches** (strict SIC + name filter): 72
- **Fetchable z ≥60 bars**: 50

## TL;DR — wynik nieoczekiwany

**Dodanie delisted thematics POPRAWIA wszystkie kluczowe metryki:**
- Sharpe: 1.49 → **1.75** (+0.26)
- FF3 alpha ann: 94.7% → **116.6%** (+21.8pp)
- FF3 alpha t-stat: 2.73 → **3.15** (+0.42)
- IC t-stat: 3.97 → **5.16** (+1.19)

**Interpretacja**: curated YAML z 2026-04-19 ma **odwrotną niż intuicyjnie survivorship bias** —
wykluczyłem biotech M&A **zwycięzców** (Karuna, Horizon, Mirati, Turning Point, Global Blood,
ImmunoGen, Prometheus, CymaBay, Chinook, Reata, SpringWorks, Akero), którzy by wygenerowali
dodatkowy alpha. Pre-acquisition rallies to jeden z najczystszych momentum signals.

Wykluczone failures (CARA, SEEL, ONTX, EVFM) pojawiają się w top-5 rzadko (3-23 dni) i ich
negative contribution jest **mniejsza** niż positive contribution z acquired winners.

**Net**: survivorship bias NIE inflate'uje Sharpe jak oryginalnie zakładano. Jeśli cokolwiek,
**deflate'uje** o ~0.26 Sharpe. Rzeczywisty unbiased Sharpe prawdopodobnie ~1.7-1.8 a nie ~0.6-0.9.

**Co to NIE falsyfikuje**: główne zmartwienie per Perplexity to **selection bias na survivorach
których wybrałem w 2026** (np. wolałem VKTX niż NTLA bo wiem o GLP-1 rally). Test B tego nie
testuje — to wymaga Testu A (point-in-time universe reconstruction with April-2021 info only).

## Metrics comparison

| Metric | Baseline (curated 113) | Augmented (+delisted thematic) | Delta |
| --- | ---: | ---: | ---: |
| universe_size | 113.0000 | 163.0000 | +50.0000 |
| daily_snapshots | 999.0000 | 999.0000 | +0.0000 |
| sharpe_gross | 1.4877 | 1.7487 | +0.2610 |
| ic_mean | 0.0248 | 0.0304 | +0.0056 |
| ic_tstat | 3.9667 | 5.1614 | +1.1948 |
| annual_return_gross_pct | 101.7174 | 128.7328 | +27.0154 |
| max_drawdown_pct | -50.1043 | -48.8428 | +1.2615 |
| ff3_alpha_ann_pct | 94.7101 | 116.5578 | +21.8477 |
| ff3_alpha_tstat | 2.7316 | 3.1476 | +0.4160 |
| ff3_r2 | 0.0060 | 0.0045 | -0.0014 |

## Delisted names that entered top-5 in augmented run

- 34 of 50 delisted names ever scored into top-5

| Ticker | Days in top-5 |
| --- | ---: |
| KRTX | 353 |
| NPTN | 343 |
| IMGN | 91 |
| CBAY | 66 |
| SURF | 66 |
| AKRO | 55 |
| KDNY | 43 |
| RVNC | 34 |
| SWTX | 34 |
| RXDX | 28 |
| RETA | 24 |
| ONTX | 23 |
| GBT | 21 |
| TPTX | 18 |
| HZNP | 17 |
| SEEL | 16 |
| YMAB | 16 |
| GTHX | 14 |
| CTIC | 13 |
| SAGE | 11 |
| ITOS | 7 |
| ITRM | 7 |
| MRSN | 6 |
| OMIC | 6 |
| IGMS | 4 |
| ADAP | 3 |
| CARA | 3 |
| EVFM | 3 |
| IRBT | 2 |
| MRTX | 2 |
| ONCT | 2 |
| SELB | 2 |
| KMPH | 1 |
| VIRX | 1 |