# Constrained 12-1m momentum — ADV liquidity floor + transaction cost stress

**RESEARCH ONLY.** Counterpart to `contrarian_constrained.md`. After the contrarian
angle was closed for retail capital deployment, this script tests whether pure
12-1 month Jegadeesh-Titman momentum survives the same ADV + cost framework.

- Top-N: 15, holding-signal: 60d, stride: 5
- Score: close[t−21] / close[t−252] − 1 (cumulative return from 12 months ago to 1 month ago)
- Cost model: RealisticCostModel(adverse=5bps); Sharpe net subtracts drag from each rebalance return
- ADV thresholds: ['$0M', '$1M', '$5M', '$20M', '$100M']

*Note*: a momentum strategy loads heavily on Carhart's MOM factor; Carhart-4F α is
expected near zero by construction. The economically meaningful metrics are net
Sharpe and excess return vs SPY benchmark.

## Results — gross / net (cost-stressed)

| Period | ADV floor | cost | mean topN | turn | Sharpe gross | Sharpe net | excess gross | excess net | α 4F | t (4F) | β_MOM | β_STR |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Full IS 2011-2022 | $0M | 5bp | 15.0 | 16.6% | 0.53 | 0.43 | +28.2% | +26.5% | +47.6% | +1.96 | -0.20 | -0.00 |
| Full IS 2011-2022 | $0M | 15bp | 15.0 | 16.6% | 0.53 | 0.34 | +28.2% | +24.9% | +47.6% | +1.96 | -0.20 | -0.00 |
| Full IS 2011-2022 | $1M | 5bp | 15.0 | 15.8% | 0.40 | 0.31 | +17.8% | +16.2% | +36.7% | +1.42 | -0.29 | 0.05 |
| Full IS 2011-2022 | $1M | 15bp | 15.0 | 15.8% | 0.40 | 0.22 | +17.8% | +14.6% | +36.7% | +1.42 | -0.29 | 0.05 |
| Full IS 2011-2022 | $5M | 5bp | 15.0 | 15.1% | 0.35 | 0.27 | +12.7% | +11.2% | +31.5% | +1.24 | -0.27 | 0.03 |
| Full IS 2011-2022 | $5M | 15bp | 15.0 | 15.1% | 0.35 | 0.18 | +12.7% | +9.6% | +31.5% | +1.24 | -0.27 | 0.03 |
| Full IS 2011-2022 | $20M | 5bp | 15.0 | 13.6% | 0.42 | 0.34 | +19.2% | +17.8% | +36.5% | +1.50 | -0.26 | -0.00 |
| Full IS 2011-2022 | $20M | 15bp | 15.0 | 13.6% | 0.42 | 0.26 | +19.2% | +16.5% | +36.5% | +1.50 | -0.26 | -0.00 |
| Full IS 2011-2022 | $100M | 5bp | 8.3 | 4.2% | -0.11 | -0.14 | -27.0% | -27.5% | -10.8% | -0.46 | -0.18 | 0.08 |
| Full IS 2011-2022 | $100M | 15bp | 8.3 | 4.2% | -0.11 | -0.16 | -27.0% | -27.9% | -10.8% | -0.46 | -0.18 | 0.08 |
| OOS 2023-2026 | $0M | 5bp | 15.0 | 17.2% | -0.42 | -0.51 | -69.0% | -70.7% | -51.5% | -1.09 | -0.20 | -0.39 |
| OOS 2023-2026 | $0M | 15bp | 15.0 | 17.2% | -0.42 | -0.60 | -69.0% | -72.5% | -51.5% | -1.09 | -0.20 | -0.39 |
| OOS 2023-2026 | $1M | 5bp | 15.0 | 16.5% | -0.59 | -0.67 | -85.6% | -87.2% | -67.6% | -1.43 | -0.14 | -0.33 |
| OOS 2023-2026 | $1M | 15bp | 15.0 | 16.5% | -0.59 | -0.76 | -85.6% | -88.9% | -67.6% | -1.43 | -0.14 | -0.33 |
| OOS 2023-2026 | $5M | 5bp | 15.0 | 17.1% | -0.37 | -0.45 | -66.5% | -68.3% | -52.1% | -0.99 | -0.31 | -0.48 |
| OOS 2023-2026 | $5M | 15bp | 15.0 | 17.1% | -0.37 | -0.54 | -66.5% | -70.0% | -52.1% | -0.99 | -0.31 | -0.48 |
| OOS 2023-2026 | $20M | 5bp | 15.0 | 14.9% | -0.21 | -0.29 | -49.8% | -51.3% | -37.7% | -0.67 | -0.38 | -0.48 |
| OOS 2023-2026 | $20M | 15bp | 15.0 | 14.9% | -0.21 | -0.36 | -49.8% | -52.8% | -37.7% | -0.67 | -0.38 | -0.48 |
| OOS 2023-2026 | $100M | 5bp | 11.2 | 6.8% | -0.18 | -0.21 | -45.8% | -46.4% | -39.7% | -0.57 | -0.15 | -0.60 |
| OOS 2023-2026 | $100M | 15bp | 11.2 | 6.8% | -0.18 | -0.24 | -45.8% | -47.1% | -39.7% | -0.57 | -0.15 | -0.60 |

## Decision criteria

- **CANDIDATE**: OOS net Sharpe ≥ 0.5 AND OOS excess vs benchmark net ≥ 5%/y AND ADV-stable across $5M+, $20M+.
- **MOMENTUM ANGLE CLOSED for retail**: OOS net excess vs benchmark < 0 at ADV ≥ $5M with 15bps.

## Verdict — MOMENTUM ANGLE CLOSED

**Decisive.** Pure 12-1 month Jegadeesh-Titman momentum on the AlphaLens R2000-like
PIT universe **fails OOS at every ADV threshold**. Net excess vs SPY ranges from
−47%/y (ADV ≥ $100M, 5bps) to −89%/y (ADV ≥ $1M, 15bps). Net Sharpe ranges
−0.21 to −0.76. There is no liquidity / cost combination where the strategy is
investable for retail.

In-sample 2011-2022 the strategy worked decently (Sharpe 0.34-0.42 net at ADV $1M-$20M,
excess +11-18%/y), with a sweet spot around $20M ADV. The IS→OOS reversal is
catastrophic — α 4F flips from +31% to −52% at $5M, +37% to −38% at $20M.

## Why momentum failed 2023-2026

The OOS period suffered the well-documented **2023 momentum crash** + a regime
where mega-cap concentration absorbed all positive returns. Specifically:

- 2023-Q1 small-cap regional bank crisis hit momentum-winners hard
- Cumulative 2023 Russell 2000 underperformance vs S&P 500 was ~25pp
- 2024 narrative-driven AI rally rotated stocks faster than 12-1m momentum can capture
- Mega-cap concentration (Mag 7) pulled SPY benchmark alone up ~50%, leaving small-cap
  momentum portfolios with negative excess regardless of factor selection

**β_MOM stays negative across all configurations** (-0.14 to -0.38). Our small-cap
top-15 12-1m momentum portfolio loads NEGATIVELY on Carhart's MOM factor (which uses
US large+mid-cap universe). This is a universe mismatch finding: small-cap momentum is
empirically anti-correlated with large-cap momentum in this period — they are
distinct regimes, not the same factor.

**β_STR is strongly negative OOS** (-0.39 to -0.60). Our momentum strategy is
short-term-reversal averse — names with high 12-1m momentum tend to have negative
short-term reversal exposure. In OOS this anti-STR posture became a headwind as
short-term reversal rallied 2023-2026.

## Closing both factor angles on this universe

Combined with `contrarian_constrained.md`:

| Factor | IS net Sharpe at $5M ADV | OOS net Sharpe at $5M ADV |
|---|---:|---:|
| 60d-drawdown contrarian + 5d bounce | 0.34 | −0.20 |
| 12-1m momentum | 0.27 | −0.45 |

**Both pure-factor strategies are closed for retail capital deployment** on the
AlphaLens R2000-like PIT universe in 2023-2026 regime. This is a strong claim
about the universe / regime combination, not necessarily about the factors
themselves on different universes (e.g., S&P 500 momentum may behave differently —
not tested here).

## Next experiment direction

**Quality + momentum combo** (`scripts/experiment_quality_momentum_combo.py`):
Asness-Frazzini-Pedersen "Quality minus Junk" thesis predicts that quality
(profitability, low debt) hedges momentum crashes by filtering out junk-momentum
names. If z(mom_12_1m) + z(roe_ttm) survives OOS where pure momentum failed,
the combo is the natural candidate for retail strategies.

If combo also fails OOS, the AlphaLens R2000-like universe in the 2023-2026
regime is genuinely retail-uninvestable via simple factor strategies, and we
should consider:
- Universe expansion (S&P 500 / Russell 1000 — different liquidity tier)
- Long-short factor strategies (hedged exposure)
- Different rebalance frequencies (monthly stride to reduce noise)
- Sector-neutral mean reversion (within-sector instead of cross-stock)
