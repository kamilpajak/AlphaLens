# Layer 3 Rejection Analysis — what patterns would a scorer filter catch?

Analyzed 52 rejected picks across both scorers (26 momentum, 26 early-stage).

## Primary rejection reasons

| Reason | Momentum | Early-stage | Total |
| --- | ---: | ---: | ---: |
| valuation_extreme | 11 | 6 | 17 |
| dilution_cash_burn | 4 | 7 | 11 |
| macro_headwind | 3 | 7 | 10 |
| fundamentals_weak | 2 | 5 | 7 |
| momentum_exhausted | 2 | 0 | 2 |
| regulatory_legal | 2 | 0 | 2 |
| technical_broken | 1 | 0 | 1 |
| execution_risk | 1 | 0 | 1 |
| management_governance | 0 | 1 | 1 |

## Most common red flags (across all rejections)

- (3×) negative operating cash flow
- (1×) p/s ratio >30 for capital-intensive hardware
- (1×) $2.56b debt, nearly 5x cash reserve
- (1×) trading 65% above 200-day moving average
- (1×) institutional ownership at 113% of float
- (1×) recent profits driven by tax benefits, not operations
- (1×) p/s ratio > 100 for pre-profit company
- (1×) cash runway insufficient for $1b capex
- (1×) 35% above 50-dma, severely overextended
- (1×) revenue growth from near-zero base (statistical illusion)
- (1×) high probability of equity dilution
- (1×) daily volatility (atr) nearly 10%
- (1×) 25x p/b for shrinking revenues & losses
- (1×) aggressive $160-180m quarterly cash burn
- (1×) clinical catalyst fully priced into valuation
- (1×) second-to-market vs entrenched leader
- (1×) technical rejection at upper bollinger band
- (1×) 100%+ institutional ownership (crowded trade)
- (1×) rsi > 81 & vertical rally without support
- (1×) 24% 'air pocket' to 50-day moving average

## Suggested scorer filters (deduped)

- (1×) reject capital-intensive hardware firms if p/s > 30 and price is >60% above their 200-day moving average.
- (1×) reject pre-profit companies if p/s > 100 or if stock trades >30% above its 50-dma.
- (1×) reject p/b > 10 for companies with negative revenue growth and accelerating net losses.
- (1×) reject if current price is >20% above 50dma and forward p/e > 40x for companies reporting negative annual net income.
- (1×) reject us-domiciled companies with inventory > 3x last quarter revenue, if majority revenue from geopolitically sensitive regions.
- (1×) reject hardware-intensive firms if p/s > 20 or rsi > 70 due to overextension and crowded trade.
- (1×) reject stocks with a trailing p/e > 40 if their operating margin is below 10%.
- (1×) reject if price drops >15% on >5x average volume within 24h of significant positive corporate news.
- (1×) reject stock if 10-day price change > 20% and sequential revenue growth is negative.
- (1×) reject if stock is in a death cross and trades more than 5% below its 50-day sma, indicating a falling knife.
- (1×) reject picks with trailing p/e ratio > 200 and a 50-day moving average below its 200-day moving average.
- (1×) reject pre-profit companies with cash runway less than 12 months and negative operating margins.
- (1×) reject if p/b ratio > 20x for companies with negative quarterly free cash flow.
- (1×) reject picks with trailing p/e > 150x if sequential profitability declines more than 50%.
- (1×) reject any stock with a forward p/e ratio greater than 75x or a trailing p/e greater than 200x.

## False negative focus (rejected picks that rallied ≥10% in 20d)

These are where Layer 3 rejected a winner — scorer improvement is less valuable here (we want LESS filtering, not more), but the categorization shows if Layer 3 has systemic blind spots.

| Date | Ticker | Scorer | Regime | fwd20d | Primary reason | Filter suggested |
| --- | --- | --- | --- | ---: | --- | --- |
| 2024-11-25 | INVZ | early-stage | bull | +164.4% | dilution_cash_burn | Reject any pre-profit company if liquid assets cover less than 18 months of TTM  |
| 2022-07-18 | BELFB | early-stage | bear | +51.6% | macro_headwind | Reject if institutional ownership exceeds 95% and stock is trading >15% above it |
| 2023-06-01 | IONQ | momentum | bull | +46.4% | valuation_extreme | Reject pre-profit companies if P/S > 100 or if stock trades >30% above its 50-DM |
| 2025-05-27 | BBAI | early-stage | flat | +40.1% | management_governance | Reject any company with a public securities fraud investigation or a material ac |
| 2025-04-10 | QUBT | early-stage | bear | +33.0% | valuation_extreme | Reject picks if P/S ratio exceeds 100x for companies with trailing revenue below |
| 2026-03-06 | RLAY | momentum | flat | +32.9% | dilution_cash_burn | Reject clinical-stage biotech if cash runway is less than 3 years considering pr |
| 2022-07-18 | MARA | momentum | bear | +30.9% | dilution_cash_burn | Reject any non-cashflow positive company with <2 quarters of cash runway or LT d |
| 2025-04-10 | PATH | momentum | bear | +20.4% | valuation_extreme | Reject if stock is in a death cross and trades more than 5% below its 50-day SMA |
| 2023-06-01 | LSCC | early-stage | bull | +19.5% | valuation_extreme | Reject if forward P/E is more than 3x the current annual revenue growth percenta |
| 2023-04-21 | VKTX | momentum | flat | +14.2% | dilution_cash_burn | Reject biotech picks if cash runway is <12 months or stock trades >150% above it |
| 2023-10-20 | KTOS | early-stage | bear | +11.7% | dilution_cash_burn | Reject if forward P/E > 100 combined with net profit margin below 2% and negativ |
| 2025-05-27 | AMBA | momentum | flat | +11.1% | dilution_cash_burn | Reject if company's reported operating cash flow is less than its stock-based co |
| 2023-09-11 | ACMR | momentum | flat | +10.7% | valuation_extreme | Reject if stock trades >60% above its 200-day moving average or has negative ope |
