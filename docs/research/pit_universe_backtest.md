# Phase 3: PIT universe reconstruction — early-stage scorer (issue #18)

**Date:** 2026-04-22
**Goal:** Test czy Phase 2 OOS α t = 1.70 (early-stage 60d) jest artefaktem survivorship + retrospective-additions bias, czy real signal.

## Setup

Trzy warianty universum:

| Universe | N tickers | Zawiera |
|---|---:|---|
| PIT-113 (Phase 2, baseline) | 113 | Wszystkie tickery z 2026-04-20 universe.yaml |
| PIT-95 | 95 | Tylko initial commit 2026-04-17, bez 18 retrospective semis z 2026-04-19 |
| PIT-95-BK | 120 | PIT-95 + 25 bankruptcy/liquidation delisted z Phase 1A (NIE M&A) |

Exclusions rationale:
- Usunięto 18 retrospective semis z 2026-04-19 → removes "I added what I knew worked"
- Dodano 25 bankruptcy delisted → true survivorship correction bez M&A selection boost
- NIE dodano 25 M&A delisted → M&A = selection effect (Phase 1A finding), not survivorship

## Wyniki — early-stage scorer Carhart-4F HAC α t-stat

### 5d hold

| Universe | TRAIN (2021-04 → 2024-12) | **OOS (2025-01 → 2026-04)** |
|---|---:|---:|
| PIT-113 (Phase 2) | +1.21 | **+1.53** |
| PIT-95 (no retrospective) | +0.80 | **+1.21** |
| PIT-95-BK (+25 bankruptcy) | **−0.86** | +1.20 |

### 60d hold

| Universe | TRAIN | **OOS** |
|---|---:|---:|
| PIT-113 (Phase 2) | +1.21 | **+1.70** |
| PIT-95 | +0.80 | **+1.36** |
| PIT-95-BK | **−0.86** | +1.35 |

### Sharpe (gross) OOS

| Universe | 5d OOS Sharpe | 60d OOS Sharpe |
|---|---:|---:|
| PIT-113 | +1.65 | +1.96 |
| PIT-95 | +1.18 | +1.52 |
| PIT-95-BK | +1.26 | +1.61 |

## Kluczowe findings

### 1. 18 retrospective semis contributed ~0.3 α t do OOS

Phase 2 wynik α t=1.53-1.70 drops to 1.21-1.36 po removal 18 semis added 2026-04-19 (theme expansion). To retrospective-addition selection bias — **realnie ~20% claimed OOS alpha pochodziło z tickerów dodanych post-hoc**.

### 2. Bankruptcy augment reveals TRUE survivorship bias

Dodanie 25 bankruptcy delisted do train window:
- **Train Sharpe: +0.60 → −0.41** (flipped sign)
- **Train Carhart α t: +0.80 → −0.86** (flipped sign)
- **Train ann return: +30% → −29%**

Early-stage scorer **PICKED bankruptcy names in train** — i te names crashed. Curated 2026-view universe masked this bo wykluczała failed tickers. **Pierwotny memory probe "Test B survivorship" interpretation była poprawna w kierunku bias'u (positive, not negative jak zakładano po Test B), ale z innych powodów** (scorer pickuje risk, not just winners).

### 3. OOS stability — bankruptcy augment nie zmienia OOS wyniku

5d OOS: 1.21 → 1.20 (PIT-95 vs PIT-95-BK). Stable.
60d OOS: 1.36 → 1.35. Stable.

Bankruptcy tickers delisted przed OOS window (2025+), więc nie wpływają na OOS results. 5y delisted cluster: większość bankruptcies 2022-2024.

### 4. True PIT-corrected signal — α t 1.20-1.35, BELOW 1.5 threshold

Repair plan decision gate: "PIT backtest α t > 1.5 → proceed to Phase 4; else close."

- **5d OOS α t = 1.20**: FAILS threshold
- **60d OOS α t = 1.35**: FAILS threshold (close but below)

### 5. IC still strong OOS 60d

60d OOS IC t = +2.84 (mean IC +0.026) — ranking quality survives PIT correction. Portfolio-level α nie, ale scorer's signal is real.

## Interpretacja

Phase 2 OOS claim (α t=1.70) **half-inflated**:
- ~0.3 t-stat z retrospective semis (selection)
- Reszta: true signal (α t=1.35 po PIT correction)

60d OOS Carhart α = +100.79% annualized, t=1.35. Very high alpha, moderate t-stat → concentration risk. Early-stage top-5 portfolio makes large bets on small number of names; handful of 2025 winners drive 100% of signal.

**Nie zwalidowane, ale nie dead.** Porównanie:
- Full Carhart α (100%) >> z most equity factors (value/momentum premium ~5-10%/year)
- t=1.35 na 262 dni → p_raw ≈ 0.18 (nie istotne 5%)
- Signal present but underpowered at this sample size + post-correction

## Decision per repair plan

**Strict reading:** Phase 3 FAILS. α t < 1.5 → close themed screener.

**Nuanced reading:**
- Signal is marginal not zero; IC t=2.84 OOS 60d says ranking works
- Bankruptcy augment shows scorer DOES pick some failures (not pure survivor bias)
- Sample n=262 OOS is limited (perplexity previously warned about this)
- α t=1.35 < 1.5 but p=0.18 is not "clearly no signal" — it's "underpowered test"

**Moja rekomendacja:**
- Treat Phase 3 as **"signal weakened but not disproven"**
- Capital deploy pozostaje ZABLOKOWANY (nie validated)
- Paper/live deployment kontynuuje — daily plist runs early-stage, gather forward OOS data
- Skip Phase 4 na razie — jeśli Phase 3 wyniki są tak marginal, transaction cost audit tylko je pogorszy
- **Pivot decision:** rozważyć czy warto kontynuować themed screener strategia w obecnej formie, czy pivot na inne approaches (alt data, ML ranker, completely different scorer)

## Co Phase 3 NIE rozwiązało

- Initial 95 tickers z 2026-04-17 też są curated z hindsight (IONQ, RGTI, QBTS — SPAC IPO 2020-2021, "thematic" definition post-hoc)
- M&A delisted NIE włączone — memoria Test B (Sharpe 1.75 z M&A) może być realnie achievable jeśli 2021 investor miał te tickery w universe
- Prawdziwy PIT universe reconstruction wymaga historical analyst lists, Wayback Machine — pracą 3-5 dni (memory: "Test A pending")

## Actions

- [x] Phase 3A: git-blame 95 vs 113 — 18 retrospective semis identified
- [x] Phase 3B: PIT-95 baseline backtest — signal dropped 0.3 t-stat
- [x] Phase 3C: PIT-95 + bankruptcy augment — signal stable OOS, train reveals bias
- [ ] Update memory — Phase 2 claim revised down
- [ ] Issue #18 comment with Phase 3 findings

## Artifacts

- `alphalens/archive/screeners/themed/universe_pit_95.yaml` — 95-ticker PIT universe
- `docs/backtest/pit95_early_stage_{5d,60d}.csv` — PIT-95 daily
- `docs/backtest/pit95_bk_aug_early_stage_{5d,60d}.csv` — PIT-95 + bankruptcy daily
- `/tmp/alphalens_issue18/phase3_pit_backtest.py` — PIT-95 compute
- `/tmp/alphalens_issue18/phase3b_bankruptcy_aug.py` — augmented compute
- This file: `docs/research/pit_universe_backtest.md`
