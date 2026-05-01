# Options data provider evaluation — 2026-05-01

**Status:** IN_PROGRESS — iVolatility trial active until 2026-05-08, support email sent 2026-05-01, awaiting response. ThetaData/Polygon Options/alternatives under research.

**Decision context:** v7 design (options-implied features) blocked pending vendor selection. Class `alt_data_screener_search_2026_04_30` CLOSED 6/6 FAIL; class `nonlinear_alt_data_search_2026_05_01` CLOSED 1/1 FAIL; class `analyst_alt_data_search_2026_05_01` ABORTED yfinance survivorship. Program-level Bonferroni n=12 burnt holdout, next test naive |t|≥2.85 / Romano-Wolf |t|≥3.5.

## iVolatility — current state

### Subscription

- **Trial active:** "Lab and IV Data Cloud Individual" subscriptions, 7-day free, then **$399/mo** if continued.
- **API key validated** via `?apiKey=...` query param. NEVER persist to keychain (per `feedback_no_keychain_writes.md`).

### Endpoint access matrix on current tier

| Endpoint | HTTP | Records | Notes |
|---|---|---|---|
| `/equities/eod/stock-prices` | 200 | 21 (AAPL Apr 2026) | Bulk OK |
| `/equities/eod/ivx` | 200 | 21 | IV term structure 7d/14d/21d/30d/60d/90d Call/Put/Mean |
| `/equities/eod/ivs` | 200 | 338 (AAPL/day) | Per-strike IV+delta surface, ~13 periods × ~26 strikes |
| `/equities/eod/hv` | 200 | 21 | HV term structure 10d-180d |
| `/equities/eod/single-stock-option-raw-iv` | 200 | per-contract w/ Greeks | Per-contract time series |
| `/equities/eod/single-stock-option` | 200 | per-contract EOD |  |
| `/equities/eod/option-series-on-date` | 200 | 2308 (AAPL/day) | Full chain w/ optionId |
| `/equities/option-series` | 200 | varies | Chain query |
| `/equities/eod/options-rawiv` | **403** | tariff_denied | Bulk per-asof IV chain BLOCKED |
| `/equities/eod/options-rawiv_1545` | **403** | tariff_denied | 15:45 snapshot BLOCKED |
| `/equities/eod/options-nbbo` | **403** | tariff_denied | NBBO bulk BLOCKED |
| `/quotes/options` (real-time) | **403** | tariff_denied | n/a (not needed) |

### Probe v2 (DEFINITIVE — uses ivolatility wrapper + ticker variant cascading + 4-tier hierarchy per zen CR)

Driver: `scripts/probe_ivolatility_options_survivorship_v2.py`. Audit: `docs/research/ivolatility_survivorship_probe_v2_2026_05_01.json`. n=200 stratified, runtime 2734.5s (~45.5 min).

**Tier hierarchy per zen CR** (T1+T2 strict gate; T3 chain-only is FAIL because ~90M calls infeasible at scale):

| Tier | n | % | Status |
|------|---|---|--------|
| T1 (equity-direct) | 45 | 22.5% | production-ready |
| T2 (variant Q/B/N/V) | 3 | 1.5% | needs ETL mapping |
| T3 (chain-only refs) | 30 | 15.0% | FAIL — 90M calls infeasible |
| T4 (missing) | 122 | 61.0% | FAIL |

**By reason**:
- **Acquisition (n=70)**: strict 1.4% (1 T1, 0 T2, 9 T3, 60 T4). DECIMATED — M&A targets have almost NO IV data in iVolatility despite stock prices in v1.
- **Unknown (n=130, mixed Ch11/standard)**: strict 36.2% (44 T1, 3 T2, 21 T3, 62 T4)

**Variant cascading effectiveness**: 3/200 hits — SIVB→SIVBQ (Q), FRC→FRCB (B), FSNN→FSNNQ (Q). zen's "fragile hack" critique confirmed empirically.

**Ground-truth predictions 7/7 correct**: SIVB T2 via SIVBQ, FRC T2 via FRCB, all M&A T1, SBNY T1.

| Gate | Observed | Threshold | Pass |
|------|----------|-----------|------|
| Overall strict (T1+T2) | 24.0% | ≥95% | ❌ gap 71pp |
| Acquisition strict | 1.4% | ≥95% | ❌ gap 94pp |
| Distress strict | 36.2% | ≥85% | ❌ gap 49pp |

**Verdict progression (REVERSED again 2026-05-01 PM late)**:
- v1 (any-endpoint): 51% retention "FAIL" — misleading (counted equity-side, not IV-side)
- v2 (raw HTTP, strict T1+T2): 24% "FAIL" — 3 probe bugs
- v2 fixed: 51% "FAIL" — sample contained SPACs
- v3 (optionable-filtered): 54.5% strict / 99.5% reachable — T3 marked "FAIL in disguise"
- **v3 + stock-market-data exploration: 99.5% feature-extractable retention via /equities/stock-market-data endpoint** ✓

**T3 cases ARE recoverable** via `/equities/stock-market-data` endpoint (verified 8/8 sample: ONP, HGT, TRNC, WGP, OPHT, GTXI, FTD, CHKE all return populated IVX30, HV30, openInterest call/put at appropriate historical dates). This endpoint provides 100+ pre-computed features per ticker per date including IVX/IVR/IVP/HV/HVP/IVX-HV ratio/Beta/Correlation — superseding ivx+ivs+hv+stock-opts-by-param composite approach used in probe v3.

**Final verdict (2026-05-01 PM revision #5)**: iVolatility $399 retail tier IS viable for v7 if architected around `/equities/stock-market-data` as primary feature source. Q-suffix look-ahead bias remains structural concern (mitigated partially via underlying-info Master Symbology lookup at retail).

**Probe v5 (FINAL, n=200, 2026-05-01 22:47): PASS 99.5% T1**:
- Overall strict T1+T2: **99.5%** (gate ≥95%) ✓
- Acquisition strict: 98.6% (gate ≥95%) ✓
- Distress strict: **100.0%** (gate ≥85%) ✓
- Tier counts: T1=199, T2=0, T3=0, T4=1 (FOUN SPAC), T5=0

**Two fixes between v4 and v5**:
1. smd multi-row US-exchange filter (TSX/L row had NaN ivx30) — recovered CTT, FRX
2. smd date cascading [30d, 180d, 365d, 730d] (smd coverage drops pre-delisting for low-liquidity tickers) — recovered HGT (365d), CHKE (365d), OHRP (365d)

Sole non-T1 case is FOUN (Founder SPAC) — genuinely no IV data ever; pre-merger blank-check vehicle without options market. Excluded from v7 universe by ADV filter regardless.

**Probe v4 (superseded, n=200, 2026-05-01 21:57)**:
- Overall strict T1+T2: 97.0%
- Tier counts: T1=194, T2=0, T3=1, T4=4, T5=1

**Critical**: T2=0 means stock-market-data preserves ORIGINAL ticker indexing for ALL optionable delistings (including Ch11 cases like SIVB queried directly without Q-suffix). zen's prior "look-ahead via Q-suffix" concern is **MOOT for the smd path** — clean PIT-correct query semantics with original tickers.

**Final verdict**: **iVolatility $399 retail tier VIABLE for v7** when architected around `/equities/stock-market-data` as primary feature extraction endpoint. PROCEED with iVolatility subscription (rather than cancelling) and design v7 around smd.

**Outstanding items before pre-reg lock**:
- (1) iVolatility support response on Master Symbology — now lower priority since smd works with original ticker
- (2) zen+perplexity adversarial review of v7 redesign with smd-primary architecture
- (3) Empirical verification that smd preserves PIT semantics on a known regime event (e.g. compare SIVB ivx30 from smd at 2023-03-08 to historical IV value reported elsewhere)

### Probe v1 (HISTORICAL — superseded by v2)

Driver: `scripts/probe_ivolatility_options_survivorship.py`. Audit: `docs/research/ivolatility_survivorship_probe_2026_05_01.json`. v1 used "any endpoint records>0" as retention criterion → counted stock-prices availability, masked IV-side sparsity. Bug fixes (rate-limit + ivs cascading) brought retention 48% → 51%, but criterion itself was wrong per v7 needs.

Driver: `scripts/probe_ivolatility_options_survivorship.py`. Audit: `docs/research/ivolatility_survivorship_probe_2026_05_01.json`. Bug fixes applied 2026-05-01 PM per zen CR (HTTP_MAX_RETRIES=7, BACKOFF=2.0+jitter, concurrency=2, ivs cascading offsets [7,14,21,30]d).

| Metric | Pre-fix | **Post-fix** | Gate | Pass |
|---|---|---|---|---|
| Overall retention | 48.0% | **51.0%** | ≥95% | ❌ gap 44pp |
| Acquisition retention | 40.0% | **45.7%** | ≥95% | ❌ gap 49pp |
| Unknown (Ch11/standard) | 52.3% | **53.8%** | ≥85% | ❌ gap 31pp |
| Symbol mismatch in retained | 0.0% | **0.0%** | ≤0% | ✅ |
| Rate-limit-exhausted (indeterminate) | n/a | **0** | — | rate-limit fix succeeded |

**Per-endpoint retention** (post-fix):
- `stock-prices`: 51.0% (equity-side retention)
- `ivx`: 22.5% (IV term structure available for half of tickers that have prices)
- `ivs`: 24.5% (surface similarly sparse)

**Insight**: Even tickers iVolatility retains often have stock prices but NO IV-side data. The IV-side is structurally sparser than the equity-side at the same tier.

**Ground-truth diagnostics (post-fix)**:
- TWTR ✅ all 3 endpoints (sp=19, ivx=19, ivs=338)
- VMW ✅ all 3 (sp=20, ivx=20, ivs=338)
- SPLK ✅ all 3 (sp=20, ivx=19, ivs=338)
- SBNY ✅ all 3 — ivs=338 after cascading offsets fix (was 0 pre-fix)
- ATVI ✅ all 3 — ivs=338 after cascading offsets fix (was 0 pre-fix)
- **SIVB ❌** all empty across all offsets — confirmed structural vendor data gap, NOT probe bug
- **FRC ❌** all empty across all offsets — confirmed structural vendor data gap, NOT probe bug
- BBBY anomaly: stock-prices returns symbol=OSTK (Nov-2023 ticker reuse), ivx/ivs return symbol=BBBY with appropriate distressed IV (manual probe finding; BBBY itself not in n=200 random sample)

### Code review of probe — zen gemini-3-pro-preview

Two real bugs identified, both bias retention DOWNWARD:
1. **CRITICAL**: rate-limit retries with insufficient backoff cause `records_found=0` false-negatives. 21/200 tickers (~10%) potentially affected. Fix: MAX_RETRIES=7, BACKOFF=2.0, jitter, raise on exhaustion.
2. **HIGH**: `ivs` query date `delisted_date - 7d` lands on weekends/holidays/post-halt for some tickers. Fix: cascade through 7/14/21/30d offsets.

**Both fixes applied 2026-05-01 PM. Definitive post-fix retention: 51.0%** (vs 48.0% pre-fix; vs zen's projected 58.5% upper bound — actual gain smaller because ivs cascading recovered a few endpoint hits but not whole-ticker retention). Rate-limit fix succeeded (0 indeterminate at concurrency=2 + 7 retries). **Verdict structurally unchanged: 51.0% << 95% gate, all retention gates FAIL by 31-49pp margin.**

### Adversarial review summary — zen + perplexity converge

Both reject "PASS with caveats" framing:
- n=7 manual probe was statistically meaningless (famous events vendor-patched)
- Excluding bank-failure cluster as "robustness check" = data manipulation
- Same v7 hypothesis under reduced feature stack must still satisfy program-level Bonferroni |t|≥2.85 (NO reset to in-class |t|≥1.96)
- BBBY/OSTK endpoint inconsistency = hard PIT integrity violation (vendor lacks unified master symbology)
- Bar = "dataset integrity sufficient for hypothesis," not "less bad than yfinance"

### Architectural blocker independent of retention

Even at $399/mo, bulk `options-rawiv`/`options-nbbo` remain 403'd. For 1500 tickers × 2000 asof × ~30 strikes/expirations ≈ 90M calls via per-contract `single-stock-option-raw-iv`, infeasible at any rate limit.

**Workable feature set on current tier**: ATM IV, 25Δ skew (interpolated from /ivs), IV-vs-HV ratio, term structure slope. P/C ratios and GEX require BLOCKED bulk endpoints.

### Support email sent 2026-05-01

Three questions:
1. Why SIVB/FRC return 0 records while SBNY (same week) returns clean halted rows?
2. BBBY/OSTK endpoint-inconsistent ticker resolution — design choice, bug, or PIT defect?
3. Delisted-chain retention policy at top tier; upgrade path for raw chains?

Awaiting response (typical turnaround unknown). Trial expires 2026-05-08.

## Polygon.io Options — investigation in progress

### Current state on Polygon Starter ($29)

Tested 2026-05-01:
- `/v3/reference/options/contracts?underlying_ticker=AAPL` → 200 OK, returns chain reference data
- `/v3/reference/options/contracts?underlying_ticker=BBBY&as_of=2023-04-15` → **200 OK with delisted BBBY chain** (contracts expiring 2023-04-21 with strikes from $0.50)
- `/v2/aggs/ticker/O:AAPL.../range/1/day/...` → **401 NOT_AUTHORIZED** "Plan doesn't include this data timeframe"
- `/v3/snapshot/options/AAPL` → 401

**Polygon retains delisted chain reference data** (which contracts existed). Historical price/IV per contract requires upgraded plan.

### Open questions (perplexity research pending)

- Polygon Options Developer ($79?) — does it unlock historical OHLC per contract? Has IV/Greeks?
- Delisted underlying retention at paid tier?
- BBBY/SIVB/FRC historical chains accessible?

## ThetaData — research deferred

Per user 2026-05-01: **NO free trial available**. Blind purchase risk for $80/mo Standard tier.

Open questions:
- Explicit delisted ticker chain retention policy
- BBBY/SIVB/FRC test cases retained or missing?
- Bulk Python SDK ergonomics

## Decision matrix (current state)

| Path | Cost | Status | Verdict |
|---|---|---|---|
| Continue iVolatility past trial | $399/mo | retention 48%, architectural blocker | **CANCEL trial** unless support response shifts evidence |
| ThetaData blind | $80/mo | unknown retention | Research-pending; blind buy risk |
| Polygon Options upgrade | $79/mo (?) | retains delisted reference, historical TBD | **Investigate** — promising leading indicator |
| Status quo (Polygon $29 only) | $29/mo | no historical options data | Not viable for v7 |

## Next steps

1. ~~Email iVolatility support~~ ✅ sent 2026-05-01
2. Perplexity research Polygon Options + ThetaData paid tiers (in progress)
3. Re-run probe with rate-limit + ivs date fixes for definitive retention number
4. Await iVolatility support response (decision deadline 2026-05-08)
5. If support response insufficient: cancel iVolatility trial, evaluate Polygon Options Developer based on perplexity findings

## Files

- `scripts/probe_ivolatility_options_survivorship.py` — n=200 stratified probe driver
- `docs/research/ivolatility_survivorship_probe_2026_05_01.json` — audit trail
- `docs/research/ivolatility_survivorship_probe_2026_05_01.md` — verdict markdown
- `docs/research/options_provider_evaluation_2026_05_01.md` — this memo
