# Layer 2b survivorship-bias probe

Test B from the Perplexity-recommended falsification battery. Augments the
curated 113-name universe with **delisted thematic small/mid caps** (biotechs
acquired or liquidated, semi photonics, consumer robotics) identified via
Polygon `active=false` sweep over 2021-06-01 → 2026-04-17.

- **Window**: 2021-06-01 → 2026-04-17 (Polygon plan boundary; original backtest started 2021-04-19)
- **Delisted candidates fetched**: 50 (from 4265 disappeared tickers, 969 liquidity-filtered, 72 thematic, 50 fetchable with ≥60 bars)

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
| ff3_alpha_ann_pct | 94.5047 | 116.4339 | +21.9293 |
| ff3_alpha_tstat | 2.6168 | 2.9861 | +0.3693 |
| ff3_r2 | 0.0069 | 0.0051 | -0.0018 |
| carhart_4f_alpha_ann_pct | 94.4442 | 115.9248 | +21.4806 |
| carhart_4f_alpha_tstat | 2.6179 | 2.9892 | +0.3714 |
| carhart_4f_r2 | 0.0069 | 0.0055 | -0.0015 |

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