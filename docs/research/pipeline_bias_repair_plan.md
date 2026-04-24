# Pipeline bias repair plan (issue #18 roadmap)

**Date:** 2026-04-22
**Context:** Post 2 perplexity reviews wykryliśmy ≥6 metodologicznych flag w Layer 2b pipeline. Gate family już closed; baseline "validated alpha" status pod znakiem zapytania po Bonferroni argumentacji. Live themed screener deployment (bez gate) kontynuuje paper/live, ale capital deploy zablokowany do ukończenia planu.

## Executive summary

4 fazy, zorganizowane według cost-value i decision gates. Każda faza może zakończyć plan wcześnie jeśli znajdzie fatal flaw. Wstępnie ~2-3 dni pracy rozsmarowane na kilka sesji.

| Phase | Cel | Effort | Decision gate |
|---|---|---|---|
| **1. Classify + Bonferroni** | Określić statystyczną istotność baseline'a | 3-4h | Jeśli Bonferroni kills baseline α → close Layer 2b |
| **2. Walk-forward OOS** | OOS validation baseline + gate | 1 dzień | Jeśli OOS α ≈ 0 → close; jeśli OOS α > 1.0 → proceed to 3 |
| **3. PIT universe reconstruct** | Eliminate survivorship | 1 dzień | Jeśli PIT backtest α ≈ 0 → close |
| **4. Execution realism** | Microcap liquidity + real cost | 4-6h | Calibrate cost model; if real cost > 150bps → strategy revision |

**Non-goals:**
- Nie re-deploy Layer 2b live screener (nadal paper/live, bez capital) — niezależne od tego planu
- Nie naprawiamy universe.yaml po jednym "survive → fail" binary, potrzeba full re-reconstruction PIT jeśli Phase 3 fires
- Gate family (#14, #15, #17) — remains CLOSED; fix nie resurrects family

---

## Phase 1 — Classification + multiple-testing correction (3-4h)

### 1A. Classify delisted tickers (30-60 min)

**Why:** "Augmented backtest 113→163 → Sharpe 1.49→1.75" z memory może być M&A effect (selection), nie survivorship correction. M&A acquisitions często mają pre-announcement pops → adding ich to universe to selection bias, nie bias correction.

**Steps:**
1. Zbierz listę delisted tickers które były considered dla universe (AKRO, VERV, LAZR z comment w universe.yaml + Test B augmented list z memory)
2. Dla każdego: query finnhub `/stock/delisted` lub Polygon `/v3/reference/tickers?active=false` — pobrać reason (M&A / bankruptcy / voluntary / regulatory / other)
3. Ew. manual lookup dla edge cases (acquirer name, deal closing date, price)

**Output:** `docs/research/delisted_classification.md` — tabela z ~30-50 tickers + reason + status

**Acceptance criteria:**
- Każdy delisted ticker zaklasyfikowany
- Per-category Sharpe effect quantified (z istniejącego augmented backtest danych)

**Decision:**
- Jeśli ≥70% delisted to **M&A** → augmented backtest effect IS selection, nie survivorship correction. Memory `project_survivorship_probe` Test B finding uniewazniony. Phase 3 PIT reconstruction musi distinguish M&A przed inclusion.
- Jeśli ≥70% to **bankruptcy / voluntary** → augmented finding valid survivorship correction. Bias direction negative (zgodne z memory). Phase 3 priority wyższy (dodać delisted bankrupts).

### 1B. Bonferroni / FDR correction całego 2026-04 research program (2-3h)

**Why:** ~16 hipotez przeprowadzonych w #14, #15, #17, #18 bez multiple-testing correction. Perplexity r2 calculated 56% false positive rate pod H0 true. Bonferroni α_adj = 0.05/16 = 0.003 (t > 2.97 dwustronnie).

**Steps:**
1. Zbierz listę wszystkich primary hypothesis tests przeprowadzonych 2026-04:
   - Per research doc: wyciągnij α t-stats, IC t-stats, Sharpe t-stats (jeśli raportowane)
   - Rozróżnij "primary hypothesis" vs "diagnostic subplot" (diagnostics nie liczą do correction)
2. Apply 3 correction schemas:
   - Bonferroni (conservative, family-wise error rate)
   - Benjamini-Hochberg FDR (less conservative, false discovery rate)
   - Hommel (tighter than Bonferroni)
3. Dla każdego primary test:
   - Check czy survives (p_adj < 0.05)
   - Report original p → adjusted p → decision change
4. Flag strategies których "validated alpha" nie przeżywa correction

**Output:** `docs/research/multiple_testing_audit_2026-04.md` — full table + decision.

**Acceptance criteria:**
- Każdy primary hypothesis test from 2026-04 listed
- Bonferroni + BH + Hommel values computed
- Per-strategy verdict: survives / fails correction

**Decision:**
- **Baseline momentum α t=2.62** (p≈0.009) vs Bonferroni threshold 0.003 → prawdopodobnie **FAIL**. Layer 2b "validated alpha" claim musi być wycofany do dalszej validation.
- **Baseline early-stage α t=1.86** (p≈0.06) — fails even uncorrected. Już nie claim'owany jako validated.
- **Gate α t values** — wszystko below 1.6, fails everything. Close-family verdict ultra-robust.

**Jeśli baseline momentum fails Bonferroni:**
- Paper/live deployment themed screener bez gate kontynuuje (nie capital deploy)
- Walk-forward OOS (Phase 2) staje się CRITICAL — tylko OOS pass może rescue baseline claim
- Memory `project_themed_screener_design` wymaga update: "validated alpha" → "pending OOS validation"

---

## Phase 2 — Walk-forward out-of-sample validation (1 dzień)

### 2A. Chronological train/test split

**Why:** Wszystkie 2026-04 wyniki są in-sample. Perplexity r2: "Solo dev + unlimited backtesting = implicit p-hacking." OOS validation jest jedyny sposób na disprove data-mining.

**Setup:**
- Train window: 2021-04-19 → 2024-12-31 (~3.7 lat)
- Test window: 2025-01-01 → 2026-04-17 (~1.3 lat, ~325 dni handlowych)
- No re-tuning parametrów — użyj EXACT params z current deployment (top-5, linear, 5d-hold)

**Backtests:**
1. Baseline momentum on test window
2. Baseline early-stage on test window
3. Gate momentum on test window (sanity — close family verdict)
4. Gate early-stage on test window

**Output:** `docs/research/walk_forward_oos_validation.md`

### 2B. Acceptance criteria

**Baseline momentum OOS:**
- α t-stat > 1.5 HAC → signal survives OOS
- α t-stat ∈ [0, 1.5] → weak, retain strategy w paper/live, no capital yet
- α t-stat ≤ 0 → dead, close themed screener entirely

**Baseline early-stage OOS:**
- α t-stat > 1.5 HAC → unexpected, validate further
- Else → consistent z in-sample weak signal; remove from production pipeline

**Gate OOS:** confirmatory only; expect close-family verdict holds.

**Walk-forward complications:**
- Test window 325 dni = Sharpe variance wide (CI ±0.4-0.5 na Sharpe 1.0)
- Może być underpowered jak Phase 3B bear subset był
- Mitigation: report full 95% CI na Sharpe i α, nie tylko point estimate

**Decision:**
- Baseline momentum OOS survives → proceed to Phase 3 (universe PIT)
- Baseline momentum OOS fails → close themed screener, end plan here, pivot kierunki

---

## Phase 3 — PIT universe reconstruction (1 dzień)

### 3A. Universe membership PIT log

**Why:** Moja current `universe.yaml` jest fixed view z 2026-04-17. Backtest od 2021 "widzi" IONQ (added 2026-04), CIFR (crypto miner, added 2024), etc. Nawet jeśli fundamental-gate używa PIT data, universe membership jest look-ahead (dodałem ticker po tym jak zobaczyłem że "temat rośnie").

**Steps:**
1. Git log per-ticker: extract date każdego ticker został added do universe.yaml (from `git blame`)
2. Dla pre-2021 tickers (added in initial commit): check czy ticker IPO'd przed 2021 (SimFin SimFinId ma IPO date? Albo Polygon `/v3/reference/tickers/{ticker}?date=2021-04-19` aby zobaczyć czy existed)
3. Build point-in-time universe: for each backtest date, use only tickers known/available na tę datę

**Output:** `alphalens/screeners/themed/universe_pit.yaml` (with per-ticker `earliest_date` field)

### 3B. Add delisted tickers per Phase 1A classification

**Steps:**
1. Z Phase 1A list classified delisted, add te które były w thematic universe but dropped przed 2026-04
2. Dla każdego: mark `latest_date` kiedy wypadł
3. Set distinction M&A ticker (treat jako terminal return z acquisition price) vs bankruptcy (zero terminal)

### 3C. Re-run baseline 5y z PIT universe

**Backtests:**
1. Baseline momentum 5y + FF/Carhart — post-fix SimFin + PIT universe
2. Baseline early-stage 5y — samo

**Acceptance:**
- Compare α t-stat PIT vs original: jeśli drop > 30%, universe effect jest dominant
- Compare Sharpe: memory Test B wspomina "Sharpe 1.49→1.75" dla augmented — ale to było M&A-contaminated? Jeśli Phase 1A pokaże >70% delisted to M&A, Test B finding reinterpreted jako selection effect

**Decision:**
- PIT universe baseline α t > 2.97 (Bonferroni-corrected) → real validated alpha
- PIT universe baseline α t ∈ [1.5, 2.97] → marginal, depends on Phase 2 OOS
- PIT universe baseline α t < 1.5 → close themed screener

---

## Phase 4 — Execution realism audit (4-6h)

### 4A. Microcap liquidity audit

**Why:** Perplexity r2: daily rebalance na 113 tickers z micro-cap niszczy Sharpe through spread + market impact nieodmodelowany.

**Steps:**
1. Pobierz median daily volume (ADV) × close price = $ volume dla każdego ticker na start date (2021-04-19) i end date (2026-04-17)
2. Compute per-ticker 30-day average bid-ask spread (Polygon `/v2/aggs/grouped/locale/us/market/stocks/{date}` lub yfinance proxy)
3. Flag tickers z $ ADV < $5M albo spread > 30 bps
4. Estimate realistic transaction cost: `0.5 × (bid-ask + market_impact_est)` gdzie market_impact_est = 0.1 * sqrt(position_size / ADV)

**Output:** `docs/research/liquidity_audit.md` — per-ticker table + category breakdown.

**Acceptance:**
- % universe < $5M ADV (flag if > 20%)
- Median realistic cost per rebalance (flag if > 50 bps)

### 4B. Refined cost model

**Steps:**
1. Per-ticker cost estimate (from 4A)
2. Include daily rebalance commissions (1-3 bps typical retail)
3. Re-run baseline backtest z realistic cost (not 75/100/150 bps flat)

**Decision:**
- Realistic cost < 100 bps → current 100bps "moderate" scenario is fair
- Realistic cost > 150 bps → strategy needs LOWER turnover design (rebalance weekly? monthly? reduce top-N)
- Realistic cost > 200 bps → Close economic viability

### 4C. Factor data vintage check (bonus, 30 min)

**Why:** Perplexity r2: "Ken French current vintage = subtle look-ahead bias."

**Steps:**
1. Check download date of FF5 + UMD CSVs w cache
2. Cross-validate α estimates z shifted-vintage data (if Dartmouth provides historical vintages)
3. If difference significant → point-in-time FF loading

**Acceptance:** Mild — doc current vintage, flag future work if significant drift found.

---

## Decision tree (visual)

```
Phase 1A (classify delisted) + 1B (Bonferroni)
    │
    ├─ Baseline α survives Bonferroni?
    │   ├─ NO → plan continues (Phase 2 critical)
    │   └─ YES → plan continues (Phase 2 confirmatory)
    │
    ▼
Phase 2 (walk-forward OOS)
    │
    ├─ OOS α t > 1.5?
    │   ├─ NO (≤ 1.5) → CLOSE themed screener; pivot to other strategies
    │   └─ YES → proceed to Phase 3
    │
    ▼
Phase 3 (PIT universe reconstruct)
    │
    ├─ PIT backtest α t > 1.5?
    │   ├─ NO → CLOSE themed screener
    │   └─ YES → proceed to Phase 4
    │
    ▼
Phase 4 (execution realism)
    │
    ├─ Realistic cost < 150 bps?
    │   ├─ NO → strategy needs design revision (turnover reduction)
    │   └─ YES → VALIDATED. Document "Layer 2b post-audit validated alpha"
    │
    ▼
Layer 2b capital deployment approved
```

**Worst case early exit:** Phase 2 OOS fails → close themed screener entirely. ~1.5 dni total effort (Phase 1 + Phase 2).

**Best case completion:** All 4 phases pass → real validated Layer 2b. ~2.5-3 dni effort.

---

## Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Phase 1A API rate limits (finnhub free tier) | Medium | Low | Fall back to yfinance delisted lookup + manual check |
| Phase 2 OOS underpowered (325 dni) | High | Medium | Report full CI, acknowledge limitation, triangulate z Phase 3 |
| Phase 3 SimFin nie ma historical tickers w 2021 universe | Medium | High | Fallback: use Polygon ticker reference `as_of` date |
| Phase 4 Polygon Starter nie ma per-second bid-ask | High | Low | Proxy z daily high-low spread lub yfinance 1-min data |
| Multiple-testing correction też invalidates baseline | Medium | High | Plan continues; Phase 2 OOS is the proper rescue test |
| Walk-forward shows regime change 2025-26 | Medium | Low | Interesting finding; not a plan blocker |

---

## Non-goals (explicit)

- Nie będziemy zmieniać gate design (binary vs multiplicative, additive vs mult). Gate family closed.
- Nie będziemy re-walidować #14, #15, #17 osobno — subsumowane przez Phase 2 (OOS z fixed SimFin).
- Nie ML/alt-data kierunki teraz (memory `fundamental_gate_family_closed` wspomina jako follow-up) — dopiero po Phase 4.
- Nie naprawiamy Layer 2a prescreener — memory wspomina że nie deployed.
- Nie re-rebuild MVP1 memory claim "Sharpe 1.71 net, FF3 α 34.6%" — to jest consumed by Phase 2 OOS.

---

## Timeline

Rozsmarowane na sesje (solo dev, bez SLA):

- **Sesja 1 (3-4h):** Phase 1A + 1B. Decision: baseline survives Bonferroni?
- **Sesja 2 (1 dzień):** Phase 2. Decision: OOS survives?
- **Sesja 3 (1 dzień):** Phase 3. Decision: PIT universe survives?
- **Sesja 4 (4-6h):** Phase 4. Final verdict + memory/issue/docs cleanup.

**Checkpoint after każdą sesją:** commit findings, update issue #18 + memory. Jeśli decision = CLOSE, stop plan, pivot.

---

## Artifacts to produce

- `docs/research/delisted_classification.md` (Phase 1A)
- `docs/research/multiple_testing_audit_2026-04.md` (Phase 1B)
- `docs/research/walk_forward_oos_validation.md` (Phase 2)
- `alphalens/screeners/themed/universe_pit.yaml` (Phase 3)
- `docs/research/pit_universe_backtest.md` (Phase 3)
- `docs/research/liquidity_audit.md` (Phase 4A)
- `docs/research/realistic_cost_backtest.md` (Phase 4B)
- `docs/research/pipeline_bias_repair_final.md` (summary post-complete)

## Issue tracking

Phases zostaną raportowane jako comments na issue #18. Jeśli plan kończy się decision to CLOSE themed screener, otwieram osobne issue na "Layer 2b retirement + pivot kierunki".
