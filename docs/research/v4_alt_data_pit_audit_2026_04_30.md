# v4 alt-data PIT audit — feature shortlist (2026-04-30)

**Status:** Task #1 of `alt_data_screener_search_2026_05_XX` (fresh class, n=1 → |t|≥1.96).
Successor to the 3/3-FAIL `multi_source_two_stage_search_2026_04_30` class.

**Scope:** which alt-data features can be PIT-accurately sourced for **2014-2026** with the
current API budget (**$29/mo Polygon Starter** + free FRED + free SEC EDGAR + free Yahoo +
free FINRA)? Output: candidate-feature shortlist (8-15 features) with PIT availability
matrix → input to v4 pre-reg draft.

Brief for context: `~/.claude/.../memory/project_next_session_v4_fresh_class.md`.

## TL;DR

**Revised 2026-04-30 EVE after pre-Phase-A FINRA infrastructure investigation discovered
v1 design was infra-blocked.** Two prior revisions captured here:

- v0 → v1 (PM): post zen + perplexity adversarial review. 12 features → 10 features (NFCI/
  UMCSENT dropped for cross-sectional incoherence; raw features dropped for multicollinearity
  with their rank counterparts; PEAD truncation gate locked; SUE first-filed PIT requirement
  locked; time-decay multipliers added to event-driven features).
- v1 → v2 (EVE): pre-Phase-A infra investigation discovered FINRA Daily Short Sale Volume
  is not accessible at our budget (cdn.finra.org returns 403 to all programmatic access;
  FINRA Query API has only ~10mo trailing data with random gaps; consolidated archive starts
  2018-08-01 even if accessible). Group B reformulated using **Polygon /stocks/v1/short-
  interest endpoint** (verified working with our existing $29/mo Stocks Starter key — AAPL
  bi-monthly history 2017-12-29 → 2026-04-15 = 200 records). v1 marked `abandoned` in the
  ledger via `alt_data_screener_v1_finra_blocked_2026_04_30`; v2 registered as
  `alt_data_screener_v2_2026_04_30`.

**v2 net effect:** STRICT UPGRADE on three dimensions vs v1 — (a) literature pedigree
restored (Diether-Lee-Werner 2009 short-interest momentum is canonical, vs daily-flow
which had no published cross-sectional anomaly), (b) infra simpler (single REST endpoint
vs custom CDN scraper), (c) PIT cleaner (settlement_date + 8 BD dissemination lag is
documented in FINRA Rule 4560). Trade-off: training window 2018-01-01 → 2024-04-29
(6.3y, vs original 10y) because Polygon short-interest history begins 2017-12-29.

**Bonferroni effect of v1 abandonment:** in-class threshold is now n=2 → |t|≥2.24 (canonical
ledger computation counts both v1 abandoned and v2 active). v2 accepts the conservative
threshold per project framework.

Adversarial review summary in `### Adversarial review` section below.

10-feature shortlist locked. **Substantively fresh** vs v1-v3 (only 2 of 21 prior features
carry forward, by design as diagnostic anchors; 8 are new). All 10 features are PIT-
accessible 2014-2026 with the current budget — **zero paid upgrades required**.

Three v4-brief candidates were **rejected** during initial sourcing (analyst revisions,
analyst dispersion, options IV skew/PC ratio): no free PIT source, $29 Polygon Starter
does not include them. Two were **redefined** (earnings surprise, news sentiment) into
PIT-clean alternatives that do not require paid feeds.

## Per-candidate verdict table

Verdict legend: **PASS** = PIT-accessible and in budget; **REJECT** = no free PIT path
within $29/mo; **REDEFINE** = original framing infeasible, alternative locked.

| # | Original v4-brief candidate | Source intended | Verdict | PIT lag | Notes |
|---|---|---|---|---|---|
| 1 | analyst_revision_count_3m | Yahoo / Polygon | **REJECT** | — | yfinance gives current snapshot only (no PIT history); Polygon Starter $29/mo does NOT include analyst estimates / EPS revisions. I/B/E/S / FactSet / Refinitiv all paid (>$1k/mo). |
| 2 | analyst_dispersion_eps_fy1 | Polygon | **REJECT** | — | Same as #1. |
| 3 | short_interest_pct_float_chg_30d | FINRA bi-monthly | **PARTIAL** | 7 BD | FINRA Equity Short Interest archive: free, exchange-listed coverage starts **June 2021** only. Pre-2021 archive is OTC-only. Insufficient training history (only 3y train) → **redefined** to FINRA Daily Short Sale Volume (free, exchange-listed since 2009, daily granularity, T+1 PIT lag). See group **B** below. |
| 4 | options_put_call_volume_ratio | Polygon options | **REJECT** | — | Options not on Polygon Starter $29/mo. CBOE Datashop (>$1k/mo) is the standard paid path. SPX-level put/call from CBOE.com is free but ticker-level is not. |
| 5 | options_iv_skew_25d | Polygon options chain | **REJECT** | — | Same as #4 + IV requires intraday option-chain snapshots. **Workaround locked:** realized-vol downside-asymmetry (group F below) as a no-cost OHLCV-based proxy of the same construct. |
| 6 | news_sentiment_30d | Polygon news API | **REJECT** | — | Polygon news endpoint not confirmed on Starter $29/mo (massive.com pricing page does not list it; Perplexity third-party scrape suggests it's NOT in Starter). Building sentiment with rule-based scorer + Polygon news endpoint is contingent on tier upgrade — out of scope for v4. **Deferred to v5** if v4 produces signal. |
| 7 | earnings_surprise_pct_4q | EDGAR companyfacts | **REDEFINE** | 1 BD post-filing | EDGAR has actuals (already PIT-wired via `data/fundamentals/edgar_companyfacts.py`) but consensus EPS is paid. **Redefined** to two PIT-clean variants in group A: SUE-via-Foster-naive-forecast (no analyst data needed) + post-earnings drift on filing date. Different feature semantics from "consensus surprise" but addresses the same anomaly literature (Bernard-Thomas 1989 PEAD). |
| 8 | insider_form4_cluster_density | EDGAR Form 4 (already wired) | **PASS** | filing-date | One feature retained from old whitelist as **diagnostic anchor**. Lets us distinguish "v4 features fail entirely" from "v4 architecture broken" if Lasso zeros all v4-fresh coefs. |

## v4 feature shortlist — 10 features, PIT-clean, in budget

### Group A — Earnings dynamics (3 features, EDGAR-derived, FRESH framing)

Replaces v4-brief candidate #7 (earnings_surprise_pct_4q). Different framing, same
anomaly literature (post-earnings-announcement drift). All three features use a **time-
decay multiplier** `decay = exp(-recency_days / 30)` applied to the SUE and PEAD raw
values; rationale in adversarial-review section. `earnings_recency_days` itself is
fed unmodified as an event-time index.

| Feature | Source | PIT lag | Construction | Engineering effort |
|---|---|---|---|---|
| `earnings_sue_naive_4q_decayed` | EDGAR companyfacts | filing-date + 1 BD | Standardised Unexpected Earnings: ((current EPS − Foster-1977 naive forecast) / σ_residual) × exp(−recency_days/30). Naive forecast = lag-4 EPS + average drift over prior 8 quarters. **σ_residual MUST be computed using as-filed-at-each-historical-filing values, not latest-restated values** (Foster 1977 original construction; perplexity adversarial review). NO analyst consensus required. | high (new fcn in `data/fundamentals/sue.py` + first-filed PIT snapshot extraction from EDGAR companyfacts) |
| `earnings_pead_5d_post_decayed` | EDGAR + HistoryStore | filing-date + 5 BD | Cumulative ticker excess return over the 5 trading days **strictly after** the most recent 10-Q/10-K filing date `f`, where `f` satisfies `f + 5 BD ≤ asof` AND `f > asof - 90d` (look-ahead-free truncation). Multiplied by `exp(−recency_days/30)` to provide daily-updating gradient. | low-medium (HistoryStore lookup + truncation guard) |
| `earnings_recency_days` | EDGAR | filing-date | Trading days since most recent 10-Q/10-K filing where `filing_date + 5 BD ≤ asof`. Acts as event-time index. NOT decayed (it IS the event-time variable). | low |

**PIT contract:** filing-date (`filed`, not `period_end`) gates all three. The PEAD
truncation `f + 5 BD ≤ asof` eliminates the look-ahead leak that the initial audit draft
contained (zen adversarial review, Objection 2A). The SUE residual-std computation
requires building a 2D first-filed snapshot view of EDGAR companyfacts — extension to
existing PIT mechanism (perplexity adversarial review, Objection 3). Both fixes locked
into engineering scope.

### Group B — Short-interest dynamics (3 features, Polygon bi-monthly, LITERATURE-PEDIGREED) — v2

**v2 supersession (2026-04-30 EVE):** v1 used FINRA Daily Short Sale Volume (flow). v1
infra-blocked because cdn.finra.org returns 403 to all programmatic access + FINRA Query
API has only ~10mo trailing data + consolidated archive starts 2018-08-01 even if
accessible. v2 uses **Polygon /stocks/v1/short-interest endpoint** (bi-monthly open
interest), verified working with our existing $29/mo Stocks Starter key on AAPL (200
records spanning settlement_date 2017-12-29 → 2026-04-15).

**Literature pedigree (RESTORED in v2):** Diether-Lee-Werner (2009 RFS) document the
canonical short-interest cross-sectional anomaly using bi-monthly open interest
(% float short). Boehmer-Jones-Zhang (2008 JF) on informed shorting. Asquith-Pathak-
Ritter (2005 JFE) on short-interest predictive power. Engelberg-Reed-Ringgenberg (2018
RFS) on days-to-cover as squeeze-risk metric. v2 features map directly to these
constructs. Perplexity's blind-spot finding (i) — "FINRA daily flow has no published
cross-sectional anomaly literature" — is RESOLVED by switching to bi-monthly open
interest, not by retreating into a documented novelty bet.

| Feature | Source | PIT lag | Construction | Engineering effort |
|---|---|---|---|---|
| `short_interest_pct_float_change_60d` | Polygon /stocks/v1/short-interest + `alphalens/data/alt_data/shares_outstanding.py` | settlement_date + 8 BD | (short_interest / shares_outstanding at most-recent dissemination-eligible settlement) − (same 60 calendar days earlier). DLW-canonical short-interest momentum. | medium (new client `alphalens/data/alt_data/polygon_short_interest.py`; ~3-4h with tests) |
| `rank_short_interest_pct_float` | Polygon + cross-section | settlement_date + 8 BD | Cross-sectional percentile rank of (short_interest / shares_outstanding). Higher rank = more crowded short side. | low (derived) |
| `log1p_days_to_cover` | Polygon (`days_to_cover` field provided directly) | settlement_date + 8 BD | log1p(days_to_cover at most-recent dissemination-eligible settlement). Engelberg-Reed-Ringgenberg squeeze-risk metric; distinct economic content from positioning-level (% float). | low (field present in API response) |

**PIT contract:** Polygon's short-interest endpoint returns FINRA-sourced bi-monthly
data. FINRA Rule 4560: short-interest reported on the 15th + last business day of each
month (settlement dates), public dissemination 8 trading days later. v2 PIT lookup at
asof t uses most recent settlement_date with `(settlement_date + 8 BD) <= t`. Polygon's
`days_to_cover` field is computed by FINRA using a 20-day trailing avg_daily_volume
window through settlement, so it carries the same dissemination lag.

**Engineering note:** the v2 client is a thin REST wrapper (single endpoint, JSON,
paginated by ticker). Strictly simpler than the v1 FINRA-CDN scraper would have been.

**Cost of v1→v2 supersession:** Bonferroni in-class threshold tightens from |t|≥1.96
(n=1) to |t|≥2.24 (n=2) by canonical ledger computation. v2 accepts.

**Cost of training-window truncation:** Polygon short-interest history begins 2017-12-29,
so v2 training is 2018-01-01 → 2024-04-29 (6.3y) instead of v1's 10y. Lasso CV power
slightly reduced; holdout n=99 unchanged.

### Group C — DROPPED in adversarial review

Initial draft proposed `nfci_level` and `umich_consumer_sentiment_zscore` as market-mood
features. **Both dropped after perplexity adversarial review (blind-spot finding (ii)):
both are market-wide aggregates with zero cross-sectional dispersion at any given asof.
A cross-sectional Lasso treats them as near-zero-variance regressors that get zeroed
or absorbed into the intercept — they cannot contribute predictive variance to a per-
ticker score.** v1-v3's `vix_level` / `term_spread` were the same construct and were in
fact zeroed in v2-v3, supporting the empirical case for dropping them. ALFRED upgrade
to `fred_client.py` is therefore NOT required for v4. (M1+M2 PIT audit latent-FAIL
remains open as an infra issue but is not v4's blocker.)

### Group D — Insider carryover (2 features, already-wired diagnostic anchor)

Two features retained from v1-v3 as diagnostic anchors. Combined with the diagnostic-
anchor pattern from group A's Foster-SUE construction, this gives v4 a 4-feature
backbone of well-understood signals against which the 6 novelty bets can be evaluated.

| Feature | Source | PIT lag | Construction | Engineering effort |
|---|---|---|---|---|
| `insider_log_count` | EDGAR Form 4 parquet (already wired) | filing-date | log1p(insider buy cluster count over trailing 90 days). Verbatim from v1-v3. | none |
| `insider_log_dollar` | EDGAR Form 4 parquet (already wired) | filing-date | log1p(aggregate cluster dollar value over trailing 90 days). Verbatim from v1-v3. | none |

**Why retain two (not one):** insider_log_count and insider_log_dollar were both zeroed
in v2-v3 → including them as a 2-feature anchor lets us check if any architectural
upgrade in v4 (smaller feature count, time-decay event features, no macro broadcast
features) un-zeros them. Cheap diagnostic.

### Group E — Cross-sectional ranks (1 feature, derived)

`rank_short_volume_ratio` is part of group B. The remaining standalone rank feature:

| Feature | Source | Construction |
|---|---|---|
| `rank_realized_downside_skew_60d` | HistoryStore + cross-section | percentile rank of `std(negative daily returns over 60d) / std(positive daily returns over 60d)` per asof slice (descending — more downside-skew = higher rank). **Raw level dropped per multicollinearity adversarial finding** (zen Objection 4A). |

**Literature-pedigree gap (acknowledged):** realized ≠ implied skew. Bollerslev-Todorov
(2011) document partial empirical correlation but the cross-sectional anomaly literature
(Bali-Cakici-Whitelaw IVOL, Conrad-Dittmar-Ghysels skewness) is on **option-implied**
skew, not realized. Substituting realized loses the theoretical anchor (perplexity
adversarial review, blind-spot finding (iii)). Retained as a single rank feature
(reduced from 2 in initial draft) — same novelty-bet rationale as group B.

### Group F — Filing density (1 new feature, EDGAR-derived)

Replaces dropped raw `realized_downside_skew_60d`. New feature added to round to 10.

| Feature | Source | PIT lag | Construction | Engineering effort |
|---|---|---|---|---|
| `filing_density_4q` | EDGAR companyfacts | filing-date | Count of 10-Q + 10-K + 8-K filings within trailing 4 quarters (∼252 BD), capped at 30. Captures catalyst pressure / corporate-event density. Distinct from earnings_recency (which measures time-since-last-earnings only). | low (reuses EDGAR filing-date stream) |

**PIT contract:** `filed ≤ asof` gates the count. No restatement leakage because we are
counting events, not values.

## Feature count vs Harvey-Liu-Zhu rule of thumb

Harvey-Liu-Zhu (2016) suggest max ~√(T·SR_target) / 2 features given holdout sample T
and Sharpe target. With T = 99 holdout rebalances (from v3 pre-reg) and SR_target = 0.5,
max ≈ √(99·0.5)/2 ≈ 3.5. Our 12 features exceed this — but Lasso L1 enforces sparsity,
and **the same 21-feature whitelist passed this rule for v1-v3 with the same holdout
T=99.** Bonferroni n=1 (fresh class) means |t|≥1.96 is the only pass-rule, so the rule-
of-thumb concern is sub-leading.

## Required infrastructure deltas before pre-reg (v2)

Two infra builds before v4 Phase A can run:

1. **`alphalens/data/alt_data/polygon_short_interest.py`** (new — v2 replacement for
   FINRA client). Polygon REST client for `/stocks/v1/short-interest` endpoint. Per-
   ticker fetcher returning DataFrame indexed by `settlement_date` with columns
   `[short_interest, avg_daily_volume, days_to_cover]`. PIT contract: at asof t,
   available `settlement_date` values are those with `(settlement_date + 8 BD) <= t`.
   Cache to `~/.alphalens/polygon_short_interest/{ticker}.parquet`. ~3-4h with tests.
2. **`alphalens/data/fundamentals/sue.py`** (new). Foster-1977 naive forecast + SUE
   computation. **Includes 2D first-filed snapshot extraction from `edgar_companyfacts.py`
   for residual-std PIT correctness** (perplexity Objection 3). The first-filed snapshot
   logic is the harder half — for each historical (period_end, asof) pair, find the EPS
   value at the earliest `filed <= asof` for that period_end. ~6-8h with tests.

PEAD truncation logic + filing_density_4q + decay multipliers + Polygon-short-interest
joining are all small additions inside the new `alphalens/screeners/alt_data/features.py`
module. Estimated ~3-4h.

Total infra: ~12-16h. Inside v4 brief's 12-19h envelope.

## v4 design lock-in (post adversarial review)

Carrying forward UNCHANGED from v3 (proven correct):

- Architecture: global Lasso L1 + 3-fold expanding-window CV-MSE + 60d embargo + 25-pt λ grid
- Target: 20-day forward excess return
- Stride: 5d with overlapping 4-tranche
- HAC maxlags: 5
- Sharpe: Lo-2002 adjusted with max_lag=5
- Cost model: 10bps half-spread + 5bps adverse selection
- Universe: AlphaLens PIT + ADV ≥ $5M
- ≥1 nonzero coef pass-rule gate

Changing:
- Feature whitelist: 21 (v1-v3) → 10 (v4, this document, group A + B + D + E + F)
- Class label: `multi_source_two_stage_search_2026_04_30` → `alt_data_screener_search_2026_04_30`
- Bonferroni: n=4 in old class → n=1 in new class → |t| ≥ 1.96 pass-rule
- New: time-decay multiplier on event-driven SUE + PEAD features (`exp(-recency_days/30)`)

**Target horizon stays at 20d** despite zen Objection 1 (horizon mismatch) because:
1. PEAD literature documents 30-60d drift (Bernard-Thomas 1989 → Chordia-Goyal-Sadka),
   not 1-5d as Gemini's framing suggested. 20d sits inside the drift window.
2. Daily short flow is the only fast-decay feature in the set; the time-decay multiplier
   would be wrong for it (no event-time anchor). Group B is engineered as a level/change
   pair, NOT an event signal — change_60d already aggregates flow.
3. SUE level is static between events; the `exp(-recency_days/30)` multiplier converts it
   to a daily-updating gradient signal, which Lasso can use even at 20d horizon.

## Adversarial review

Two-stage review run before locking the v4 design:

**Stage 1 — `mcp__zen__chat` with gemini-3-pro-preview (thinking_mode: high):** raised
five objections — (1) target/feature horizon mismatch, (2) PEAD look-ahead bug, (3) SUE
first-filed PIT, (4) raw+rank multicollinearity, (5) Bonferroni gaming.

**Stage 2 — `mcp__perplexity__perplexity_reason` (sonar-reasoning-pro, search_context_size:
high):** confirmed zen objections (2), (3), (4) outright; partially confirmed (1) with the
nuance that PEAD literature decay is 30-60d not 1-5d (which keeps 20d target viable);
pushed harder on (5) (n=4 may itself be optimistic). Added three blind-spot findings:
(i) FINRA daily short volume has no published cross-sectional anomaly literature
(distinct from open-interest); (ii) NFCI has zero cross-sectional dispersion → Lasso
will zero it; (iii) realized downside skew ≠ implied skew empirically.

**Synthesis (locked):**
- Objection 2 (PEAD look-ahead) — accepted, fixed via `f + 5 BD ≤ asof` truncation.
- Objection 3 (SUE first-filed PIT) — accepted, fixed via 2D first-filed snapshot
  machinery in new `data/fundamentals/sue.py`.
- Objection 4 (raw+rank multicollinearity) — accepted, raw `short_volume_ratio_20d` and
  raw `realized_downside_skew_60d` dropped; only rank counterparts retained.
- Objection 1 (horizon mismatch) — partially accepted. Time-decay multiplier added per
  zen's secondary fix to convert event-static features into daily-updating gradients.
  Target stays at 20d because perplexity confirmed PEAD-drift literature window is
  compatible.
- Perplexity (ii) (NFCI cross-sectional incoherence) — accepted, NFCI + UMCSENT both
  dropped from the whitelist (correctness issue, not literature-pedigree issue).
- Perplexity (i) (FINRA flow vs open interest literature) and (iii) (realized vs implied
  skew) — surfaced explicitly in the per-group documentation as **deliberate novelty
  bets**. Retained because the project mission (per `feedback_literature_not_oracle.md`
  and `feedback_keep_searching_screeners.md`) is to test combinations not yet documented
  in the literature. Pre-reg ledger entry will document these gaps explicitly so the
  failure mode is clean if the bet loses.
- Objection 5 (Bonferroni) — partial pushback. Project framework is class-conditional
  Bonferroni per memory notes; v4 stays n=1 → |t|≥1.96 in-class. Pre-reg memo will
  surface the program-level FWER concern as a known methodological tension rather than
  silently override the project framework. Capital-deployment threshold (separate from
  in-class PASS) can be set higher (e.g., |t|≥2.50) if v4 PASSes.
- Perplexity's bottom-line ("drop everything without literature pedigree, run on 4-feature
  literature minimalist") — **rejected**. That recommendation is misaligned with the
  project mission: a minimalist literature-pedigreed v4 just retests Bernard-Thomas/
  Foster on a new sample without exploring novel feature space. The whole point of the
  search is to find combinations that aren't yet published.

## Final 10-feature whitelist

| # | Feature | Group | Decayed? |
|---|---|---|---|
| 1 | `earnings_sue_naive_4q_decayed` | A | yes (×exp(-recency/30)) |
| 2 | `earnings_pead_5d_post_decayed` | A | yes (×exp(-recency/30)) |
| 3 | `earnings_recency_days` | A | no (event-time index) |
| 4 | `short_interest_pct_float_change_60d` | B (v2) | no |
| 5 | `rank_short_interest_pct_float` | B (v2) | no |
| 6 | `log1p_days_to_cover` | B (v2) | no |
| 7 | `insider_log_count` | D | no (anchor) |
| 8 | `insider_log_dollar` | D | no (anchor) |
| 9 | `rank_realized_downside_skew_60d` | E | no |
| 10 | `filing_density_4q` | F | no |

10 features, all with non-zero per-asof cross-sectional dispersion, all PIT-accessible
2014-2026 with zero paid upgrades, two infra deltas (~10-13h), ~3-4h additional feature-
joiner work, in line with v4 brief's 12-19h total budget.

## Audit verdict

**PASS — proceed to pre-reg draft (Task #3).** The 10-feature whitelist is locked,
all PIT contracts are explicit, all adversarial objections (zen Objections 1-5 +
perplexity blind-spots i-iii) are either patched or surfaced as deliberate research
bets. Pre-reg JSON should mirror v3 template with the substitutions documented above.
