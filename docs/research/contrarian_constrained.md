# Constrained pure-contrarian — ADV liquidity floor + transaction cost stress

**RESEARCH ONLY.** Tests whether the small-cap contrarian premium documented
in `docs/research/layer2d_definitive_synthesis.md` survives realistic deployment
constraints. Score: −60d_return + 0.5 × 5d_return; portfolio: top-15 by score from
PIT universe filtered to 60d-median dollar ADV ≥ threshold. Each rebalance
(weekly, stride=5) computes ADV per ticker live; non-PIT names excluded.

- Top-N: 15, holding-signal: 60d, stride: 5
- Cost model: RealisticCostModel(adverse=5bps), drag = round_trip × turnover × stride/year
- ADV thresholds tested: ['$0M', '$1M', '$5M', '$20M', '$100M']

## Results — gross / net (cost-stressed)

| Period | ADV floor | cost (bps half-spread) | mean top-N | turnover/rebal | Sharpe gross | α 4F gross | t (4F) | drag/y | α 4F net | α 5F net | β_STR |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Full IS 2011-2022 | $0M | 5 | 15.0 | 29.4% | 0.68 | 101.4% | 2.42 | 2.97% | 98.4% | 93.3% | 0.09 |
| Full IS 2011-2022 | $0M | 15 | 15.0 | 29.4% | 0.68 | 101.4% | 2.42 | 5.93% | 95.5% | 90.4% | 0.09 |
| Full IS 2011-2022 | $1M | 5 | 15.0 | 29.8% | 0.24 | 23.5% | 0.77 | 3.01% | 20.5% | 26.4% | -0.10 |
| Full IS 2011-2022 | $1M | 15 | 15.0 | 29.8% | 0.24 | 23.5% | 0.77 | 6.01% | 17.5% | 23.4% | -0.10 |
| Full IS 2011-2022 | $5M | 5 | 15.0 | 28.6% | 0.34 | 31.8% | 1.10 | 2.88% | 28.9% | 33.8% | -0.08 |
| Full IS 2011-2022 | $5M | 15 | 15.0 | 28.6% | 0.34 | 31.8% | 1.10 | 5.76% | 26.0% | 30.9% | -0.08 |
| Full IS 2011-2022 | $20M | 5 | 15.0 | 25.3% | 0.20 | 16.1% | 0.60 | 2.55% | 13.5% | 13.7% | -0.00 |
| Full IS 2011-2022 | $20M | 15 | 15.0 | 25.3% | 0.20 | 16.1% | 0.60 | 5.10% | 11.0% | 11.1% | -0.00 |
| Full IS 2011-2022 | $100M | 5 | 8.5 | 4.2% | -0.28 | -24.7% | -1.13 | 0.43% | -25.1% | -24.1% | -0.02 |
| Full IS 2011-2022 | $100M | 15 | 8.5 | 4.2% | -0.28 | -24.7% | -1.13 | 0.86% | -25.5% | -24.5% | -0.02 |
| OOS 2023-2026 | $0M | 5 | 15.0 | 30.5% | 0.95 | 150.2% | 1.34 | 3.07% | 147.1% | 145.5% | -0.10 |
| OOS 2023-2026 | $0M | 15 | 15.0 | 30.5% | 0.95 | 150.2% | 1.34 | 6.14% | 144.0% | 142.4% | -0.10 |
| OOS 2023-2026 | $1M | 5 | 15.0 | 30.0% | 0.37 | 21.3% | 0.27 | 3.03% | 18.2% | 13.2% | -0.29 |
| OOS 2023-2026 | $1M | 15 | 15.0 | 30.0% | 0.37 | 21.3% | 0.27 | 6.05% | 15.2% | 10.1% | -0.29 |
| OOS 2023-2026 | $5M | 5 | 15.0 | 30.9% | -0.20 | -48.8% | -0.88 | 3.12% | -52.0% | -55.8% | -0.22 |
| OOS 2023-2026 | $5M | 15 | 15.0 | 30.9% | -0.20 | -48.8% | -0.88 | 6.24% | -55.1% | -58.9% | -0.22 |
| OOS 2023-2026 | $20M | 5 | 15.0 | 27.8% | -0.35 | -45.4% | -0.89 | 2.81% | -48.2% | -50.3% | -0.12 |
| OOS 2023-2026 | $20M | 15 | 15.0 | 27.8% | -0.35 | -45.4% | -0.89 | 5.61% | -51.0% | -53.1% | -0.12 |
| OOS 2023-2026 | $100M | 5 | 11.4 | 8.4% | -0.22 | -34.4% | -0.53 | 0.85% | -35.2% | -42.1% | -0.40 |
| OOS 2023-2026 | $100M | 15 | 11.4 | 8.4% | -0.22 | -34.4% | -0.53 | 1.70% | -36.1% | -42.9% | -0.40 |

## Decision criteria

- **CANDIDATE for Phase-3 validation**: OOS net 4F α t-stat ≥ 1.0 AND OOS Sharpe ≥ 0.5 AND α stable across ADV ≥ $5M, $20M (sign of robustness).
- **CONTRARIAN ANGLE CLOSED for retail capital**: OOS net α drops below 5%/y at any of ADV ≥ $5M with 15bps cost stress.
- **MID — needs more research**: Sharpe between 0.3-0.5; possibly investable with refined sizing/holding period (next steps: longer holding, weighting variants).

## Verdict — CONTRARIAN ANGLE CLOSED

**Decisive.** OOS net α at ADV ≥ $5M (lowest investable threshold for retail) is **−52%/y**
(t = −0.88, Sharpe = −0.20). At ADV ≥ $20M, OOS α = −51%/y. At ADV ≥ $100M (large-cap),
OOS α = −36%/y, with the candidate pool shrinking to mean top-N = 11.4 (some weeks have
fewer than 15 large-caps with sufficient drawdown to qualify).

**The 150%/y headline OOS α at zero-ADV is 100% tail-rebound artifact** of un-tradeable
names. Removing even the bottom 1% of the universe by liquidity (ADV < $1M) cuts gross α
from 150% to 21%. Removing names below $5M ADV flips α negative by 200pp. There is no
ADV / cost combination where the constrained version is investable.

In-sample (2011-2022) the picture was less catastrophic — the contrarian premium was a
real phenomenon then, with α=32% at ADV ≥ $5M (t = 1.10, marginal). But it failed to
persist OOS. Combined with Layer 2d's `definitive_synthesis.md` finding that the
behavioral pattern of insider clusters has been stable across periods (insiders keep
contrarian-buying), this paints a picture of:

- A real but **regime-dependent** small-cap reversal premium that paid out 2011-2022
- A 2023-2026 regime where the same selection produces NEGATIVE returns
- No insider-information edge on top of (or independent from) this premium

**Mechanistic explanation for OOS flip:** in mega-cap-concentration regimes (post-2022),
60d-drawdown stocks are predominantly genuinely-impaired (profit warnings, secular decline,
Fed-tightening losers), not mean-reversion candidates. Mean-reversion premium becomes
negative when "drawdown stocks" structurally include more permanent-loss names than
recovery names.

## What this rules out

- Pure-contrarian as a deployable retail strategy
- Insider-cluster-buying screener as a deployable retail strategy
- The combination (insider + contrarian) — both have been individually closed
- Any factor strategy that depends on small-cap reversal premium being persistent OOS

## What remains untested in this codebase

- **Pure 12-1 month momentum** (Jegadeesh-Titman 1993) on full PIT — Layer 2b was a
  themed-momentum variant that closed; the un-themed version on R2000-like universe
  has not been measured here
- **Quality / fundamental** strategies using SimFin cache (~/.alphalens/simfin_cache):
  ROE, debt, growth — Joel Greenblatt Magic Formula territory
- **Combo signals** (quality + momentum + value): even when individual factors are
  weak, AQR-style combinations may aggregate signal robustly
- **Different rebalance frequencies** — current weekly stride; monthly may give very
  different turnover/cost trade-off
- **Sector-neutral mean reversion** — within-sector dispersion may be more stable than
  cross-stock contrarian
- **Insider SELLS as short signal** — completely untested; would require re-parsing
  4.3GB raw Form 4 JSON cache

## Next experiment recommendation

Pure 12-1 month momentum on full PIT universe with the SAME ADV / cost framework. This is:
- Tractable with existing prices (no new data needed)
- A direct test of "does ANY simple factor strategy survive on this universe with realistic
  constraints" — orthogonal to the contrarian angle just closed
- Literature priors are well-established (FF Mom factor was Sharpe ~0.7 historically; has
  weakened post-2009)
- Will inform whether AlphaLens universe is just "too efficient for retail factor
  strategies" or whether contrarian was specifically the wrong angle
