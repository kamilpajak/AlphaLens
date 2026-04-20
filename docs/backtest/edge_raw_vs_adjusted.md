# EDGE: raw vs. adjusted prices

Theoretically EDGE is ratio-based and scale-invariant for multiplicative (split+dividend) adjustments. This report verifies empirically across known split events.

| Ticker | Split | n bars | Median raw | Median adj | Median Δ (adj − raw) | % diff |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| NVDA | 10-for-1 | 130 | 133.5 bps | 114.0 bps | +0.0 bps | -14.6% |
| TSLA | 3-for-1 | 130 | 135.0 bps | 106.9 bps | +0.0 bps | -20.8% |
| AMZN | 20-for-1 | 129 | 80.8 bps | 78.9 bps | +0.0 bps | -2.3% |
| GOOGL | 20-for-1 | 130 | 92.3 bps | 73.7 bps | +0.0 bps | -20.2% |
| SHOP | 10-for-1 | 130 | 156.4 bps | 131.6 bps | +0.0 bps | -15.8% |
| AAPL | 4-for-1 | — | — | — | — | fetch failed: 403 {"status":"NOT_AUTHORIZED","request_id":"e2fbf8d97c383b7c9b664f74e82d0cb1","message":"Your plan doesn't include this data timeframe. Please upgrade your plan at https://polygon.io/pricing"} |
| DXCM | 4-for-1 | 130 | 91.2 bps | 86.1 bps | -0.0 bps | -5.6% |
| PANW | 3-for-1 | 129 | 85.5 bps | 82.0 bps | -0.0 bps | -4.1% |
| CPRT | 2-for-1 | 130 | 69.8 bps | 67.6 bps | -0.0 bps | -3.3% |
| MNST | 2-for-1 | 127 | 38.3 bps | 35.3 bps | -0.0 bps | -7.8% |

## Decision gate

If all rows show |% diff| < 10% → adjusted prices are safe to use as production default (EDGE is effectively scale-invariant on this data). Larger deltas flag tickers where raw should be preferred or a ticker-specific calibration applied.