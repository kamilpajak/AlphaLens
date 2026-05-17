# Thematic Event-Driven Decision-Support Tool — Design Memo v1

**Date:** 2026-05-15 (locked 2026-05-16)
**Status:** **PHASE A-E SHIPPED** (2026-05-17). MVP pipeline live end-to-end:
- Phase A news ingestion (PR #128)
- Phase B Gemini Flash event extraction + theme rollup (PR #129)
- Phase C theme→beneficiary mapping + 4 verification gates (PR #130) — gate honesty + mcap post-filter (PR #131)
- Phase D Layer 4 4-signal screen (PR #132) — insider/FCFF/valuation/technicals stack
- Phase E Layer 5 brief generator with Pro/Flash routing (PR #133)
- Explainability gap closure: catalyst URL + 52w/MA200 + freshness + earnings (PR #134)

Original status: LOCKED — 7 user decisions resolved §14; Phase A engineering authorized (2026-05-16).
**Parallel track:** AlphaLens dodaje DRUGĄ równoległą ścieżkę obok kontynuowanej factor-paradigm-search. Paradigm #14 PEAD v2 audit nadal in-flight, paper-trade observations (v9D, pc_abnormal) continue, Layer 1 watchdog live, methodology bundle MIT-licensed durable. Ten tool jest **thematic event-driven discretionary decision-support** — research/operational tool augmentujący user's existing WhatsApp investing group workflow. NIE jest paradigm test pod project doctrine 3.5; nie zastępuje factor research. Both tracks operate independently.

---

## §0. Context — why this pivot

Six months of AlphaLens phase-robust paradigm-search produced:
- **14 paradigm failures** (Layer 2b/2c/2d/2e/2f/2g + tri-factor + mom×lowvol + regime-gate + quality×mom + vol-target overlay + insider_pc_compound + ev_fcff_yield + idiosyncratic_momentum)
- **2 INCONCLUSIVE retrospectives** (v9D αt 2.45, pc_abnormal αt 2.65) — INCONCLUSIVE per project doctrine 3.5 but real signals
- **1 SLIPPAGE-FAIL** (insider_form4_opportunistic, gross αt 2.71 OOS, net αt 1.27 @ H=50bps)
- **2 REJECTED-by-adversarial-review pre-engineering** in this session: HXZ profitability (2026-05-14), insider_form4_quality_gated (2026-05-15)

Perplexity research 2026-05-15 (sonar deep research) confirmed structural reality: **retail long-only single-factor αt ≥ 3.5 with Bonferroni-conservative protocol is essentially unachievable** in 2024-2026 market structure. Factor ensembles realistically deliver Sharpe 0.45-0.60 (≈ market-matching), value comes from tax efficiency + behavioral discipline rather than alpha.

**This memo describes a fundamentally different research program** — exploiting an edge that factor-quant doesn't address: **LLM-style cross-domain reasoning on news events** to identify second-order beneficiaries of mega-cap catalysts, where small/mid-cap downstream tickers haven't yet been priced. User-stated example: Nvidia CUDA-Q announcement → QUBT (quantum-computing micro-cap) ripples 1-7 days later, not minutes.

**Honest framing**: this is a **discretionary research assistant**, not a guaranteed-alpha generator. Tool's job is to (a) find candidate ideas, (b) argue WHY (with mandatory bear case), (c) let the user decide. The tool does NOT auto-execute. The user retains final discretion.

---

## §1. User-stated goals (locked from 2026-05-15 conversation)

| Decision | Lock |
|---|---|
| Theme domains | TBD configurable (start: AI/quantum/biotech/energy; expand based on user observation) |
| **Input universe (news source)** | **S&P 100 + sector leaders** (mega-cap "big players" whose news has wide ripple) |
| **Output universe (recommendations)** | **Small/mid-cap downstream beneficiaries** — $500M to $10B market cap sweet spot; liquidity floor: avg daily $ volume ≥ $5M (over 90d) |
| **Holding horizon** | **4-8 weeks** default (8w typical, 4w if catalyst-failure exit triggered) |
| **Recommendation cadence** | **3-5 candidates per week**, confidence-gated (skip week if no candidate ≥ confidence 3) |
| **Paper-trade tracker** | **Claude (operator) maintains** — sqlite ledger + monthly P&L report. Mandatory 6-month observation before any real capital decision. |
| **LLM stack** | **Gemini 2.5 Flash** for batch news extraction (cheap/fast); **Gemini 3 Pro** for theme-supply-chain reasoning + argumentation. Free-tier quota covers ~80% of operational volume. |

Items NOT yet locked (Phase A clarification gates):
- Theme domain whitelist
- Capital deployment trigger (Sharpe threshold from paper-trade observation)
- Notification channel (Telegram already configured per `.env`; email optional)

---

## §2. Architecture — 5 layers

```
┌─────────────────────────────────────────────────────────────────────┐
│ Layer 1: NEWS INGESTION (daily batch, ~100-300 items)               │
│ Sources (priority order):                                           │
│ ├─ SEC EDGAR 8-K filings (Layer 1 watchdog already live)            │
│ ├─ Polygon news API (already paid, focus on S&P 100 + sector leaders│
│ ├─ GDELT 2.0 (free, real-time, global news graph)                   │
│ ├─ RSS aggregator: Bloomberg headlines, FT, Reuters tech, TechCrunch│
│ └─ Optional Phase B: Marketaux $25/mo for entity-tagged news        │
│ Filtering: keep only items mentioning S&P 100 / sector-leader names │
│ Persistence: ~/.alphalens/thematic_news/{YYYY-MM-DD}.parquet        │
└─────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│ Layer 2: EVENT EXTRACTION (Gemini 2.5 Flash, batch nightly 02:00)   │
│ Input: ~100-300 news items                                          │
│ Output per item: structured JSON                                    │
│   {                                                                 │
│     event_type: product_launch|M&A|regulatory|partnership|...       │
│     primary_entities: [NVDA, ...],                                  │
│     themes: [quantum_computing, AI_quantum_hybrid, ...],            │
│     sentiment: positive|negative|neutral,                           │
│     second_order_implications: [string explanations],               │
│     theme_novelty_score: 0-1 (1=novel last 30d)                     │
│   }                                                                 │
│ Cluster aggregation: themes over 7-30d rolling window               │
│ Novel-theme flag: theme appearance velocity ≥3x 30d-baseline        │
│ Cost: ~$5-15/mo at 200 items/day × Flash tier                       │
└─────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│ Layer 3: THEME → BENEFICIARY MAPPING (Gemini 3 Pro reasoning)       │
│ Trigger: novel theme OR confidence-3+ event                         │
│ For each theme:                                                     │
│ ├─ LLM chain-of-thought: "Theme X → who downstream benefits?"       │
│ │   Constraints in prompt:                                          │
│ │   - Output 5-15 candidate tickers                                 │
│ │   - Each ticker must have: 10-K-grep keyword evidence OR          │
│ │     thematic-ETF holding membership OR explicit company press     │
│ │     release linking to theme                                      │
│ │   - Reject candidates that fail verification (anti-hallucination) │
│ ├─ Verification pass (mandatory):                                   │
│ │   ├─ ETF holdings reverse-lookup: download QTUM/ARKQ/BOTZ/CIBR/   │
│ │   │   ICLN/PNQI/SOXX/etc. holdings, check ticker membership       │
│ │   ├─ companyfacts_parquet 10-K business-description grep for      │
│ │   │   theme keywords                                              │
│ │   └─ Polygon news 30d retrospective: did this company make a      │
│ │       press release related to theme?                             │
│ │   Reject if ZERO of the 3 verification signals fire               │
│ └─ Output: ranked candidate list, ~5-10 verified tickers per theme  │
└─────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│ Layer 4: QUANTITATIVE SCREEN (Python, deterministic)                │
│ For each verified candidate:                                        │
│ ├─ Market cap filter: $500M ≤ cap ≤ $10B (liquidity-anchored)       │
│ ├─ Liquidity filter: avg daily $ volume 90d ≥ $5M                   │
│ ├─ Valuation metrics (from SimFin + yfinance):                      │
│ │   - Forward P/E, P/S, EV/Revenue, PEG                             │
│ │   - Revenue growth 3y CAGR, expected next-year                    │
│ ├─ Quality metrics:                                                 │
│ │   - Gross margin, operating margin, FCF positive                  │
│ │   - Debt/equity, cash runway                                      │
│ ├─ Technicals (from yfinance daily):                                │
│ │   - RSI(14), distance from 200d MA, distance from 52w high        │
│ │   - 14d ATR (volatility), 10d/90d volume ratio                    │
│ ├─ Theme-relative valuation: percentile rank within theme cohort    │
│ │   (cheapest quartile by P/S among quantum cohort, etc)            │
│ └─ Catalyst-freshness check: if stock +30%+ in last 30d → flag      │
│   "LATE ENTRY", downgrade confidence by 1                           │
└─────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│ Layer 5: ARGUMENTATION + REPORT (Gemini 3 Pro)                      │
│ For each top-5 candidate (sorted by confidence):                    │
│ ├─ TLDR: 1-sentence thesis                                          │
│ ├─ Event link: source URL + date + summary                          │
│ ├─ Supply chain reasoning: 2-3 paragraphs of why this benefits      │
│ ├─ Verification evidence: ETF holdings / 10-K keywords / press      │
│ ├─ Valuation case: cheap within theme cohort AND vs growth          │
│ ├─ Technical setup: support/resistance, RSI, accumulation/distrib   │
│ ├─ MANDATORY BEAR CASE: 3 reasons this could fail (anti-bias)       │
│ ├─ Suggested entry: limit price (5-10 bps below current)            │
│ ├─ Suggested position size: 1.5% (conf 3), 2.0% (conf 4), 2.5% (5)  │
│ ├─ Time exit: 8 weeks from entry (4 if catalyst-failure-triggered)  │
│ ├─ Catalyst-failure exit conditions: thesis-specific (e.g. NVDA     │
│ │   partners with competitor publicly, dilutive secondary offering) │
│ └─ Disaster stop: -25% from entry (DISASTER PREVENTION ONLY,        │
│     not active management — per Kaminski-Lo 2007 evidence)          │
│ Confidence score 1-5:                                               │
│   1 = weak (single signal, skip)                                    │
│   2 = below threshold (skip)                                        │
│   3 = baseline (eligible for digest)                                │
│   4 = strong (multiple signals align)                               │
│   5 = exceptional (rare, all signals align including timing)        │
└─────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│ Layer 6: PAPER-TRADE LEDGER (Claude operator, NOT real money)       │
│ Schema (sqlite at ~/.alphalens/thematic_paper_ledger.db):           │
│ ├─ recommendations(id, ticker, theme, entry_date, entry_price,      │
│ │   confidence, position_pct, thesis_md, bear_case_md, time_exit,   │
│ │   stop_price, source_event_url)                                   │
│ ├─ exits(rec_id, exit_date, exit_price, exit_reason, realized_pct)  │
│ ├─ themes(theme_name, first_seen_date, novelty_score, decay_status) │
│ └─ Monthly P&L report (markdown) summarizing:                       │
│     - Open positions (paper)                                        │
│     - Closed positions month + cumulative                           │
│     - Win rate, avg win, avg loss, Sharpe (12mo trailing)           │
│     - Theme performance (which themes worked, which decayed)        │
│     - Bear-case audit (did flagged risks materialize?)              │
└─────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
              Telegram digest (daily if novel theme;
              weekly Sunday otherwise)
```

---

## §3. Data sources + cost

| Source | Status | Monthly cost | Notes |
|---|---|---|---|
| SEC EDGAR 8-K filings | ✓ Layer 1 watchdog live | $0 | Mandatory data source already in production |
| Polygon news API | ✓ Starter $29/mo paid | $0 incremental | Filtered to S&P 100 + sector leaders |
| GDELT 2.0 | Free, integrate Phase A | $0 | Global news graph, real-time |
| RSS aggregator | Phase A | $0 | Python `feedparser`, ~10 sources |
| yfinance daily OHLCV | ✓ cached | $0 | Existing infrastructure |
| SimFin fundamentals | ✓ $25/mo paid | $0 incremental | Existing from paradigm #13 |
| companyfacts_parquet | ✓ cached | $0 | Existing, 2784 tickers, XBRL-derived |
| ETF holdings | Phase A | $0 | Free download from issuer websites; cache quarterly |
| Gemini 2.5 Flash API | Phase B | ~$5-15 | Batch extraction, ~200 items/day |
| Gemini 3 Pro API | Phase B | ~$15-50 | Theme reasoning + argumentation |
| Optional Marketaux | Phase C | $25 | Entity-tagged news premium, defer if Polygon adequate |
| **Total operational** | | **~$20-65/mo** | Free-tier Gemini quota may cover most |

---

## §4. LLM stack details

### Gemini 2.5 Flash (Layer 2 extraction)

- Per-item input: news headline + body (~1000 tokens)
- Per-item output: structured JSON (~200 tokens)
- Volume: ~100-300 items/day = ~250 items × 30 days = 7500/mo
- Cost (Flash pricing $0.075/1M input, $0.30/1M output 2026 estimate): ~$2-5/mo
- Free tier: 1500 requests/day available → likely fully free

### Gemini 3 Pro (Layer 3 reasoning, Layer 5 argumentation)

- Per theme: ~5000 tokens input (multi-step reasoning), ~2000 output
- Volume: ~10-20 themes/month × 5 candidates each = ~50-100 reasoning calls/month
- Per candidate argumentation: ~3000 in, ~1500 out
- Cost (3 Pro $1.25/1M in, $5/1M out): ~$15-50/mo
- Free tier: limited; will exceed free tier in active weeks

### Anti-hallucination contract

All LLM outputs MUST be verifiable. Specifically Layer 3 candidate proposals:
- Each ticker must pass at least ONE of: ETF holding membership, 10-K keyword grep, recent press release linking to theme
- Rejected candidates that fail verification are logged for accuracy auditing
- Monthly accuracy report: % of LLM-proposed candidates that pass verification (target ≥70%)

---

## §5. Paper-trade ledger contract

**6-month mandatory observation tier** before any real-capital decision.

Observation acceptance criteria (after 6 months):
- **Sharpe ≥ 0.5** on paper P&L (annualized from monthly observations)
- **Win rate ≥ 45%** (reasonable for thematic discretionary; less than this = signal too noisy)
- **Max drawdown ≤ -20%** (paper portfolio, marked-to-market daily)
- **Bear-case audit accuracy**: at least 60% of triggered "catalyst-failure" exits should be CORRECT calls (i.e. the bear case actually materialized; not just normal volatility)
- **Theme decay tracking**: at least 50% of themes should follow predicted decay timeline (~10-12 weeks)

If criteria met after 6 months → user decides on real-capital allocation (start small, ~10% of intended).
If criteria NOT met → tool is rejected as decision-support; methodology archived as research artifact.

**Honest disclosure**: this is a STRICTER acceptance criterion than vendor tools (Wealthfront/Betterment don't disclose live performance). The 6-month observation is the user's protection against the same survivorship-bias concern we have about the friend's track record.

---

## §6. Position sizing + exit rules

### Position sizing (per Perplexity-validated retail-realistic guidance)

- Confidence 3: **1.5%** of capital
- Confidence 4: **2.0%** of capital
- Confidence 5: **2.5%** of capital (rare)
- Max single position: **3.0%** (hard cap, never override)
- Max concurrent positions: **8-12** (function of theme diversity, not target count)
- Max single-theme exposure: **20%** of capital (concentration limit)
- Gross portfolio long exposure: **50-75%** (cash buffer for opportunities + drawdown survival)

### Exit rules (per Perplexity Kaminski-Lo-validated evidence)

1. **Time exit**: 8 weeks from entry (4 weeks if catalyst-failure exit triggered earlier)
2. **Catalyst-failure exit**: thesis-specific conditions defined per recommendation (e.g. "NVDA partners with competitor publicly")
3. **Theme decay exit**: if theme drops below novelty threshold for 4 consecutive weeks AND position not in profit, exit
4. **Disaster stop**: **-25% from entry** (DISASTER PREVENTION ONLY, not active management). Per Kaminski-Lo 2007 + Lund 2025: tighter stops degrade Sharpe for mean-reverting strategies; even -25% is mostly neutral but acts as circuit breaker against catastrophic single-position loss
5. **Profit-taking**: NO mechanical take-profit. Per Perplexity: trailing stops cut value/quality winners early; fixed targets cap upside. Time-exit at 8w handles realization naturally.

### Tax-loss harvesting (DEFERRED to Phase D — operational addition after paper-trade validation)

Per Perplexity research: TLH is highest-Sharpe-impact intervention (~0.05-0.10 Sharpe pts, very high reliability). NOT implemented in v1 because:
- Paper-trade ledger has no tax implications
- TLH needs broker-specific integration for live use
- Add after Phase F (real-capital decision)

---

## §7. Honest expectations + risks

### What this tool IS

- A research assistant that processes ~200 news items/day with LLM-style reasoning to surface 3-5 thematic candidate ideas/week
- A paper-trade tracker with disciplined entry/exit rules
- A bear-case-forcing mechanism (every recommendation comes with mandatory disconfirmation reasoning)
- A 6-month observation gate before real capital

### What this tool is NOT

- An automated trading system (user retains all execution discretion)
- A guaranteed-alpha generator (the friend's track record may be lucky; that's what observation gate is for)
- A factor-quant successor (operates on a different alpha source: cross-domain LLM reasoning + second-order beneficiaries)
- A short-term trading tool (4-8w hold; not intraday)

### Realistic outcome distribution (5-year forward, $25-50k capital)

Honest priors, anchored to Perplexity 2026-05-15 research:

| Outcome | Probability | Description |
|---|---|---|
| Tool produces useful research aid; net outperforms S&P 500 by 0-3% annually | **40-50%** | Most likely outcome — LLM reasoning + discipline beats passive marginally |
| Tool finds 2-3 great trades/year (+30-50% returns), balanced by losers | **25-30%** | Catalyst-driven hits + average misses |
| Tool consistently beats S&P 500 by 5-10% annually | **10-15%** | Optimistic case |
| Tool degenerates into confirmation-bias machine | **10-20%** | If bear-case discipline fails OR user overrides recommendations |
| Tool fails 6-month observation, kill paper-trade | **15-25%** | Most likely "rejection" outcome |

**Honest framing**: variance HIGHER than factor-quant (which is market-matching ± 2%). Thematic event-driven can be market±15% in a year. Higher variance = both more potential AND more disaster risk.

### Key risks

| Risk | Severity | Mitigation |
|---|---|---|
| LLM hallucinates supply chains | HIGH | Layer 3 mandatory verification gate (ETF / 10-K / press release) |
| Confirmation bias in argumentation | HIGH | Mandatory bear case + confidence score; Phase D Phase D: bear-case-accuracy audit |
| Friend's track record is survivorship | HIGH | 6-month paper-trade observation gate; Sharpe ≥ 0.5 acceptance criterion |
| News already priced (primary) | HIGH for mega-cap, LOW for second-order | Tool DESIGN focuses on second-order beneficiaries (small/mid-cap downstream) |
| Theme half-life faster than expected | MEDIUM | Time-exit baked at 8w; theme decay monitor |
| Small-cap liquidity trap | MEDIUM | Hard floors: $500M market cap + $5M daily $ volume |
| Tax inefficiency (8w hold = short-term gains) | MEDIUM | Acknowledged tradeoff; tax drag ~50-100bps/y |
| User behavioral abandonment | MEDIUM | Paper-trade-first removes early real-money pressure |
| LLM cost overrun | LOW | Free tier covers most; Gemini Flash for batch is cheap |
| News API outage / data gaps | LOW | Multi-source (Polygon + GDELT + RSS) provides redundancy |

---

## §8. MVP scope + 3-week timeline

| Phase | Wall | Deliverable |
|---|---|---|
| **A — News ingestion pipeline** | 3-4 days | Polygon news + GDELT + SEC 8-K + RSS aggregator → parquet daily |
| **B — LLM event extraction (Gemini Flash)** | 2-3 days | Layer 2 with structured prompt + JSON validation; cost benchmark |
| **C — Theme aggregator + ETF reverse-lookup** | 3-4 days | Layer 3 with verification gates; bootstrap 8-12 thematic ETF holdings |
| **D — Quantitative screen** | 2 days | Layer 4 reusing paradigm #13 SimFin + paradigm #15 valuation/technicals primitives |
| **E — Argumentation + report generation (Gemini 3 Pro)** | 3-4 days | Layer 5 with mandatory bear case, confidence scoring, markdown template |
| **F — Paper-trade ledger + monthly P&L report** | 2-3 days | sqlite schema, daily mark-to-market, monthly markdown report |
| **G — Telegram notification** | 1 day | Reuse existing TG bot config, daily/weekly digest |
| **MVP delivery** | **~3 weeks active engineering** | + ongoing operation |

After MVP: **6-month paper-trade observation tier** runs autonomously with monthly P&L reports surfacing for user review.

---

## §9. Pre-engineering gates

Before Phase A starts, per CLAUDE.md mandatory adversarial review:

1. **Zen adversarial review** (gemini-3-pro-preview, high thinking) on this memo §1-7
2. **Perplexity adversarial review** (sonar deep research) on architecture viability + LLM-supply-chain-reasoning prior art
3. Both reviewers' verdicts incorporated as §10 A1/A2 amendments before status → LOCKED
4. User explicit approval of MVP scope + 3-week timeline + paper-trade observation tier acceptance criteria
5. **NO ledger registration** (this is not a paradigm test; the ledger is for hypothesis-driven Bonferroni-corrected paradigms; this is a tool with paper-trade-then-decision lifecycle)

---

## §10. Post-review amendments (audit trail)

*To be populated after adversarial review.*

### A0. Pre-lock checklist

- [ ] Zen adversarial review on §1-7 architecture + honest priors
- [ ] Perplexity adversarial review on LLM-supply-chain-reasoning prior art + retail thematic momentum evidence
- [ ] Both reviewers' verdicts incorporated as §10 A1/A2 amendments
- [ ] User explicit approval of MVP scope + observation tier criteria
- [ ] Status DRAFT → LOCKED
- [ ] Phase A engineering can begin AFTER lock

---

## §11. Honest pre-review flags from author (Claude, 2026-05-15)

Pre-emptive self-critique to invite specific challenge:

1. **LLM supply-chain reasoning has not been validated against published benchmarks.** I'm claiming Gemini 3 Pro can identify second-order beneficiaries reliably; this is unproven for financial-domain reasoning. The Layer 3 verification gate is the defense, but verification depends on data quality (ETF holdings can be stale; 10-K keyword grep is shallow).
2. **Sharpe ≥ 0.5 acceptance criterion may be too lenient.** S&P 500 historical Sharpe is ~0.55. If the tool produces 0.5 paper Sharpe and S&P matches that, tool produces zero alpha. Acceptance criterion should probably include "Sharpe > S&P 500 Sharpe over same period + 0.1" or similar.
3. **6-month observation may be too short** to distinguish luck from skill. Friend's "years of success" might require 24+ months to validate.
4. **Position-sizing 1.5-2.5% may be too conservative** for high-conviction thematic plays. Or too aggressive for noise-dominated outcomes.
5. **Mandatory bear case is necessary but not sufficient** against confirmation bias — Gemini 3 Pro will still generate bull-skewed arguments because most input news is fundamentally bullish (companies don't publish "we suck" press releases).
6. **Catalyst-failure exit conditions are LLM-generated and unverifiable in advance.** "Exit if NVDA partners with competitor" — how does the tool detect this happened? Either we build a news monitor for it (Phase B+) or we trust the user to notice. Both are weak.
7. **No backtesting tier** — unlike factor paradigms, this tool has NO historical backtest. Paper-trade observation is the only validation. This means we can't know expected outcome until 6 months in. Real risk.

These are honest concerns the adversarial reviewers should challenge.

---

## §12. Updates 2026-05-16 — Layer 4 multi-signal stack finalized

After user's 2026-05-16 questions about which paradigm scorers reuse, Layer 4 LOCKED z czterech validated/standard signals (NIE pięciu — paradigm #15 IM dropped):

### Layer 4 final composition

For each Layer 3-verified candidate:

| Sygnał | Source | Status | Use w briefie |
|---|---|---|---|
| **Cohen-Malloy insider score** | `alphalens/screeners/insider_activity/cohen_malloy_classifier.py` (paradigm #11 reuse) | VALIDATED gross αt **+2.71** OOS | "Insider buy $X.XM last 30d (Cohen-Malloy classified, percentile XX)" |
| **FCFF yield + cohort percentile** | `alphalens/screeners/ev_fcff_yield/scorer.py` (paradigm #13 reuse, pure functions) | VALIDATED αt mean 1.18, every-phase positive 15/15 | "FCFF yield 7.8% vs cohort median 2.1%" |
| **SimFin valuation panel** | `alphalens/data/store/simfin.py::SimFinFundamentalsStore` (paradigm #13 infrastructure) | Standard valuation toolkit | "P/S 18x vs cohort 32x, PEG 1.2 vs 2.8" |
| **Technicals** | yfinance daily OHLCV cache | Standard practice | "RSI 51, sits at 50d MA, ATR 4.2%" |

**Dropped from MVP (per honest sanity check 2026-05-16):**
- ❌ Paradigm #15 idiosyncratic momentum — IS αt 0.02 is noise, whole `price_factor_search_2026_04_29` class (n=5: pure_mom, contrarian, mom×lowvol, qual×mom, idio_mom) ALL FAILED. No basis for "rising trend" reuse.

**Provisional (NOT in MVP, may add later):**
- v9D options-implied (αt 2.45 pre-2018 retrospective) + pc_abnormal_volume (αt 2.65) — defer until 12-month paper-trade observation completes (~2027-05). Need iVolatility SMD lookup for each candidate (expensive).
- AV EARNINGS surprise data — could be added as bonus signal if paradigm #14 PEAD v2 audit passes.

### Form-4 dual use in MVP

Form-4 data (form4_parquet, 37MB, 2.66M rows from paradigm #11 VPS backfill) used in DWA miejsca:

1. **Layer 3 verification gate (signal #4)** — checking if candidate has recent opportunistic-insider net buying (Cohen-Malloy classified, last 30-90d). Adds to confidence score if positive.

2. **Layer 1b independent candidate source** — daily scan for unusual insider activity spikes, cross-referenced with active themes from Layer 2. Provides candidates NOT discovered through news path.

### SimFin role consolidated

SimFin Start tier ($25/mo already paid) NIE jest news source. Backbone dla Layer 4:
- FCFF computation (OCF + Interest×(1-τ) - Capex)
- EV computation (Price×Shares + LTD + STD - Cash)
- Theme-relative valuation percentiles (P/E, P/S, EV/Rev, PEG)
- Quality flags (gross margin, FCF positive, debt/equity)
- All via existing `SimFinFundamentalsStore` class (paradigm #13 infrastructure, zero new engineering)

### Updated cost summary

| Source | Status | Monthly cost |
|---|---|---|
| Polygon news + prices | $29/mo paid ✓ | 0 incremental |
| SimFin fundamentals | $25/mo paid ✓ | 0 incremental |
| form4_parquet (Cohen-Malloy) | Free local ✓ | 0 |
| companyfacts_parquet (XBRL) | Free local ✓ | 0 |
| SEC EDGAR 8-K | Free, Layer 1 watchdog live ✓ | 0 |
| GDELT 2.0 | Free | 0 |
| RSS aggregator | Free | 0 |
| ETF holdings (QTUM/BOTZ/etc) | Free issuer download | 0 |
| Gemini API (Flash + 3 Pro) | NEW | $30-60 |
| **Total nowy operational** | | **~$30-60/mo** |

### Engineering scope refinement (form-4 inclusion)

| Faza | Wall | Co |
|---|---|---|
| A — News ingest + LLM extraction | 3 dni | Polygon + GDELT + 8-K + RSS → Gemini Flash batch |
| B — Theme mapping + 4 verification gates (incl. form-4 wrapper) | 3-4 dni | +0.5d za form-4 verification signal |
| C — Quant screen with multi-signal reuse | 2-3 dni | form-4 score + FCFF yield + SimFin valuation + technicals |
| D — Short-format WhatsApp brief generator | 2 dni | Gemini 3 Pro prompt template |
| E — Feedback ledger sqlite | 1 dzień | recommendations + shares + outcomes |
| F — Telegram bot integration | 1 dzień | Daily/weekly digest |
| G — Form-4 independent candidate source path | 1 dzień | Layer 1b spike detection |
| **MVP total** | **~2 tygodnie aktywnego eng** | |

---

## §13. Session handoff (2026-05-16) — next session start here

**Status:** DRAFT memo pre-engineering. Architecture LOCKED on Layer 4 multi-signal stack (paradigm #11 form-4 + paradigm #13 FCFF yield + SimFin valuation + technicals; NO IM). Form-4 dual use confirmed. SimFin role consolidated. 8 layers, ~2 tygodnie engineering, $30-60/mo Gemini operational.

**7 nierozstrzygniętych decyzji wymagających user input przed Phase A:**

1. **Theme domain whitelist** — start scope: AI/tech only, lub szerzej (energy, biotech, materials, healthcare, geopolitics)?
2. **Friend's recent picks history** — czy możesz udostępnić listę 10-20 ostatnich kandydatów friend zaproponował w WhatsApp grupie (ostatnie 6-12 miesięcy) z thesis + outcome? Po MVP look-back test: czy tool by znalazł te same nazwy w odpowiednich windowach?
3. **Group size + composition** — ile osób w WhatsApp grupie? Każdy decyduje independently czy jest koordynacja?
4. **Cadence** — daily digest (5-10 candidates) lub weekly batch (15-25 candidates)?
5. **Ship-to-group automation** — czy chcesz że tool automatically szeruje high-confidence (4-5) candidates do grupy, czy zawsze przez Ciebie manualnie?
6. **Friend's awareness/role** — czy friend wie o narzędziu? Jeśli tak, czy chce tool's output być adversarial check dla jego picks (= sanity check) czy collaboration?
7. **LLM budget confirmation** — OK z $30-60/mo Gemini? Free tier prawdopodobnie pokryje ~80% wolumenu, ale rezerwa.

**First action w nowej sesji:** użyj prompt poniżej do startu:

> Kontynuujemy budowę thematic event-driven tool (parallel track do factor research). Memo at `docs/research/thematic_event_tool_v1_design_2026_05_15.md`, project memory `project_thematic_tool_pivot_2026_05_16.md`. Status: DRAFT, Layer 4 multi-signal stack LOCKED (form-4 #11 + FCFF #13 + SimFin valuation + technicals). 7 unresolved decisions w §13 memo. Moje odpowiedzi: [user answers 1-7].

**Po user-answers:**
- Memo update do v2 z lockiem decyzji
- Status: DRAFT → LOCKED
- (Optional) **JEDEN zen review** z balanced prompting (NOT "be brutal" — symmetric "challenges + supports" frame) per feedback `adversarial_reviewer_bias_2026_05_16.md`
- Phase A engineering kicks off

**Files to read in new session for full context:**

| Plik | Use |
|---|---|
| `docs/research/thematic_event_tool_v1_design_2026_05_15.md` (TEN memo) | Pełna architektura + 7 decyzji + handoff |
| `~/.claude/projects/.../memory/project_thematic_tool_pivot_2026_05_16.md` | Project state, validated paradigm scorer inventory |
| `~/.claude/projects/.../memory/feedback_adversarial_reviewer_bias_2026_05_16.md` | Reviewer bias context (don't deferr to "be brutal" verdicts) |
| `~/.claude/projects/.../memory/feedback_validated_paradigm_scorer_reuse_2026_05_16.md` | Reuse taxonomy: form-4 + FCFF YES, IM NO |
| `alphalens/screeners/insider_activity/` | Cohen-Malloy reuse infrastructure (paradigm #11) |
| `alphalens/screeners/ev_fcff_yield/scorer.py` | FCFF pure functions (paradigm #13) |
| `alphalens/data/store/simfin.py` | SimFin store infrastructure |
| `CLAUDE.md` § Project status | Parallel-track framing (NOT pivot) |

**Reusable paradigm modules inventory (validated, ready to import w MVP):**

```python
# Paradigm #11 Cohen-Malloy form-4
from alphalens.screeners.insider_activity.cohen_malloy_classifier import (
    classify_opportunistic_insiders,
)
from alphalens.screeners.insider_activity.opportunistic_form4 import (
    aggregate_net_buys,
)

# Paradigm #13 EV/FCFF yield (pure functions)
from alphalens.screeners.ev_fcff_yield.scorer import (
    compute_fcff,         # OCF + Interest*(1-tax) - Capex
    compute_ev,           # Price*Shares + LongTermDebt + ShortTermDebt - Cash
    compute_fcff_yield,   # FCFF / EV
    winsorize,            # generic
    rank_zscore,          # generic
)

# SimFin infrastructure
from alphalens.data.store.simfin import SimFinFundamentalsStore

# Common (paradigm #15 refactor)
from alphalens.screeners._common import winsorize, rank_zscore
```

**Paradigm reuse policy** (per `feedback_validated_paradigm_scorer_reuse_2026_05_16.md`):
- ✅ Reuse paradigm #11 (Cohen-Malloy) — gross αt 2.71 validated
- ✅ Reuse paradigm #13 (FCFF yield) — αt 1.18 every-phase positive validated
- ❌ DO NOT reuse paradigm #15 IM — IS αt 0.02 noise; whole price_factor_search class dead
- ⏸ Provisional v9D/pc_abnormal (paper-trade observation) — defer until ~2027-05

**No outstanding engineering tasks before user answers 7 decyzji.** Wait for user input then proceed.

---

## §14. User decisions LOCKED (2026-05-16)

| # | Decision | Lock |
|---|---|---|
| 1 | Theme domain whitelist | **BROAD**: AI/tech + energy + biotech + materials + healthcare + geopolitics. Sub-themes auto-discovered by Gemini Flash; no hard whitelist enforced. |
| 2 | Look-back validation anchor | **Single case post-MVP**: NVIDIA CUDA-Q announcement (April 2025) → QUBT ripple 1-7d later. Tool must surface QUBT in correct window for validation pass. Expand to multi-case later. |
| 3 | WhatsApp group size + dynamics | **9 members**. Brief format = **thesis only**. No "open questions for group" section. No coordinated voting tracked by tool. |
| 4 | Cadence | **Daily short list** of 5-10 candidates surfaced to user. User cherry-picks subset to share with group. |
| 5 | Ship-to-group | **Manual only** — tool never auto-shares. User reviews every output before forwarding. |
| 6 | Friend's role | **Adversarial check** (na razie). Tool output benchmarked against friend's picks; tool's value = catches friend missed OR rejects friend's bad picks. No collaboration mode yet. |
| 7 | LLM budget | **$30-40/mo Gemini ceiling**. Free tier expected to cover ~90%+; paid = burst capacity reserve. Telegram already configured per `.env`. |

### Derived implications

- **Q1 broad scope** → ETF universe for Layer 3 verification expands: QTUM/ARKQ/BOTZ/CIBR (AI/quantum) + ICLN/PBW/QCLN (clean energy) + IBB/XBI (biotech) + LIT/REMX (materials) + IHI/IHF (healthcare devices/services) + ITA/PPA (defense/geopolitics). Bootstrap ~15 thematic ETFs in Phase C.
- **Q2 single anchor** → look-back validation is lightweight post-MVP gate, not blocker. Run NVDA→QUBT replay AFTER Phase G complete; if tool would have surfaced QUBT in 1-7d window after CUDA-Q press release → Phase H paper-trade begins. If miss → diagnose Layer 2/3 prompts before paper-trade.
- **Q3 9 members + thesis-only** → §5 Layer-5 report template drops "Bear case" 3-reason section to single-paragraph bear summary. Confidence score retained. Bear case still mandatory but compressed. Saves Gemini 3 Pro tokens ~30%.
- **Q4 daily short** → cadence shifts §5 brief schema: 5-10 candidates/day, weekly batch derived as roll-up. Daily Telegram digest at 07:00 local (post-overnight Gemini batch).
- **Q5 manual** → no auto-share code path in Phase G. Telegram digest goes ONLY to user's personal chat (`TELEGRAM_CHAT_ID` from `.env`), never to group chat ID.
- **Q6 adversarial check** → feedback ledger §6 schema adds `friend_concurrent_pick` boolean column tracking whether friend independently surfaced same ticker in same window. Comparison metrics: precision/recall/disagreement-direction.
- **Q7 budget** → Layer 2 Gemini Flash batch capped at 200 items/day (was 300) to fit free-tier safely; spillover queued to next day. Layer 3/5 Gemini 3 Pro capped at $30/mo hard ceiling; if approaching, downgrade marginal-confidence (conf=3) candidates to Flash argumentation.

### Status after lock

- Phase A engineering authorized.
- No mandatory zen+Perplexity "be brutal" review (per `feedback_adversarial_reviewer_bias_2026_05_16.md`; reviewer good for technical critique post-engineering, BAD for go/no-go on uncertain projects).
- Optional balanced zen review on §1-7 architecture available but not blocking.
- Phase A first action: inventory existing Polygon news / SEC EDGAR / RSS infrastructure → identify reuse vs net-new before writing code.
