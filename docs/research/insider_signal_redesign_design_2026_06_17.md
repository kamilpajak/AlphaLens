# Insider opportunistic-buying signal — redesign design memo

**Status:** PHASE 1 + 2 SHIPPED + DEPLOYED 2026-06-17 — Phase 1 (display honest-gate) PR #621 (`a06c3b93`); Phase 2 (buy-only / 180d / within-buyers + `insider_signal_version` poolability stamp; insider held out of `layer4`) PR #624 (`6f3c875`, migration 0013, verified on brief 2026-06-16: GME the sole buyer @ $114k pctile 100, 16 zeros now show no rank). Reviewed by zen deepseek-v4-pro + Perplexity + an empirical Phase-0 probe (signal real, not a coverage bug). **Phase 3** (ADV/holdings + count/cluster) and **Phase 4** (fold insider back into ordering after an offline lift test, ~2026-09+ at N≥30) DEFERRED; filing-date PIT + brief-prompt "(90d)→(180d)" relabel (cassette-pinned) DEFERRED. See memory `project_insider_signal_v2_2026_06_17`.
**Date:** 2026-06-17
**Author:** assistant (session-driven), for kamilpajak
**Scope:** `alphalens_pipeline/thematic/screening/insider_signal.py`, `_common.percentile_rank`, `thematic/screening/scorer.py` (`insider_is_positive`, `compose_weighted_score`, `_build_candidate_row`), brief prompt (`argumentation/prompts.py`), card display (`apps/web`). Django ingest mirrors columns only.

## 1. Problem statement

The thematic screener scores each candidate on opportunistic insider buying (Cohen–Malloy–Pomorski routine-vs-opportunistic classification of Form-4 P/S transactions). Two surfaced fields:

- `insider_score_usd` = signed sum of opportunistic P/S USD over a trailing **90d** window. `None` = no Form-4 history (renders `—`); `0.0` = history but no opportunistic trades in window; `<0` = net opportunistic selling.
- `insider_score_sector_percentile` = a **`≤`-percentile-rank** of `score_usd` within the industry peer cohort (ties counted, candidate included, empty peers → 50, thin cohort → null).

### 1.1 Empirical pathology (live data, 353 candidates / 29 days, 2026-05-19…06-16)

- `score_usd`: **250 == 0**, **19 < 0**, **0 > 0**, 84 `None`. min −26.7M (LUNR), median 0, **no candidate has any positive opportunistic-buy USD**.
- `sector_percentile`: median **98.6**, 124 candidates at exactly **100.0** with `score_usd == 0`.
- **228 / 269 (85%)** non-null candidates show percentile ≥ 90 while `score_usd ≤ 0`.
- **0 / 353** clear the `insider_is_positive` gate (`score_usd > $50k`).

### 1.2 Root causes (3 distinct)

1. **`≤`-percentile on a zero-inflated signal elevates zeros.** Opportunistic buying is rare → most peers are 0 or net-sellers; `0 ≤ 0` counts and 0 beats any negative, so a zero-buy candidate ranks ~100th. The percentile reads as "strong buying" but means "not selling, in a selling sector".
2. **Net (buy − sell) instead of buy-only.** `score_usd` nets opportunistic sells against buys. Sells are weakly/not informative; netting injects noise.
3. **Fixed absolute $50k gate + 90d window.** Non-standard normalization + a window shorter than the literature's effect horizon → the 2× insider weight in `layer4_weighted_score` never fires (dead weight).

### 1.3 Blast radius (bounded)

Per [[reference_candidate_lifecycle_selection_vs_ordering_2026_06_13]]: insider does **not** gate SELECTION (catalyst + map-themes gates + mcap own that). The percentile is **display-only** (card chip + brief LLM prompt line). `insider_is_positive` (absolute `score_usd > $50k`) feeds **ordering** (`layer4_weighted_score`, 2× weight) — currently always False → contributes nothing. So no capital is misallocated today, but: (a) the card/brief mislead the human on 85% of cards, and (b) the strongest-weighted ordering signal is dead.

## 2. Evidence base (Perplexity deep-research 2026-06-17, 18 sources)

Cohen-Malloy-Pomorski 2012 ("Decoding Inside Information"); Lakonishok-Lee 2001; Jeng-Metrick-Zeckhauser 2003 (+6%/yr on purchases, sells insignificant); FAJ "Some Insider Sales Are Positive Signals"; StarMine Insider Filings model; microcap GBM study.

- **Ranking:** zero-inflated signal must treat 0 as **neutral**, not top. Standard = **sign-aware two-stage rank** (rank only within positives and within negatives; zeros pinned to the midpoint). Mid-rank (average) ties within sign groups.
- **Normalization:** fixed $ is non-standard. Scale opportunistic-buy $ by **market cap** (`OP_fracMC`, analog of shares-bought/shares-outstanding), optionally by ADV (liquidity) and insider pre-holdings. Use **relative** (percentile-within-positives) gate, not absolute $.
- **Buys vs sells:** treat asymmetrically; **buy-only** primary. Crude netting "introduces noise, may invert the signal."
- **Horizon:** effect concentrated **6–12 months**, predictive 6–24 months; **90d look-back is short**. Suggest 180d+ with optional time-decay.
- **Integration:** replace dead absolute gate with **event-indicator + rank-within-positives + conviction weighting**; gate fires on normalized rank.
- **Data quality:** exclude option-exercises / 10b5-1 / gifts from "opportunistic buy"; verify CMP classification.

## 3. Proposed design

### 3.1 Raw signal (buy-only, normalized)
- `op_buy_usd` = Σ opportunistic **purchase**-only USD over window (drop sells from the magnitude; optionally retain a separate `op_sell_usd` for display, never netted into the score).
- `op_frac_mc` = `op_buy_usd / market_cap` (primary normalized magnitude). Market cap already fetched in the scorer. (ADV / insider-holdings normalization deferred — needs new data.)
- Window: **180d** (from 90d), optional linear time-decay (deferred to a tuning pass).

### 3.2 Sign-aware two-stage ranking (replaces `≤`-percentile)
```
state: x>0 positive | x==0 neutral | x<0 negative   (x = op_frac_mc, buy-only ⇒ x ≥ 0 in practice; keep negative branch for a future net variant)
score: x>0 → 0.5 + 0.5·rank⁺(x)    # mid-rank pct within positives
       x==0 → 0.5
       x<0 → 0.5·rank⁻(x)
```
Zeros neutral by construction; no zero ever tops the cohort. Thin-cohort → null (unchanged). No-data (`None`) → null (unchanged). Empty positive subset → all neutral (not 50-on-empty-peers artifact).

### 3.3 Integration (kill the dead gate)
- `Event_i = 1 if op_frac_mc > 0 else 0`.
- Insider exposure `E_i = sign-aware score` (∈ [0,1]); contributes `w·E_i`, optional 1.5–2× conviction multiplier for top-rank-within-events.
- Replaces the binary `insider_is_positive` boolean in `compose_weighted_score`. NOTE: `compose_weighted_score` returns 1–5 int; moving from boolean to continuous E changes the score distribution — needs a re-mapping that keeps the 1–5 contract (or widen it). Open question §6.

### 3.4 Display + prompt
- Card: show insider strength only when `op_buy_usd > 0`; when 0 → neutral/grey "$0 — no opportunistic buys (90d/180d)"; never a high-percentile bullish chip on a zero.
- Brief prompt line: keep raw `$Xk` + reframe percentile as "rank among net buyers in sector" (only when event present).

## 4. Phasing

- **Phase 1 (highest ROI, least data risk):** sign-aware two-stage ranking + buy-only magnitude (no mcap/window change). Fixes the display pathology + the netting noise. Card + prompt reframe. `layer4` still uses a boolean gate but now `op_buy_usd > 0` (relative, not $50k) so it can fire.
- **Phase 2:** `op_frac_mc` market-cap normalization + 180d window + event/conviction continuous integration into `layer4_weighted_score` (re-map to 1–5).
- **Phase 3 (optional/tuning):** time-decay, ADV / insider-holdings normalization, insider-count + role + persistently-profitable features.

## 5. Risks / implications

- **Forward-only.** Recompute changes historical percentiles; old briefs' parquets keep old values (panel/score forward-only precedent). EDGE/calibration for insider×outcome resets — acceptable (no insider×EDGE study live yet; N<30 everywhere).
- **`layer4_weighted_score` distribution shift** (Phase 2) — `confidence` chip + ordering move; this is ordering/display, not selection, but is user-visible. Needs a before/after diff on real dates.
- **Data-quality gate (mandatory before Phase 1 lock):** verify the CMP classifier excludes option-exercise / 10b5-1 / gift codes from "opportunistic buy"; check `compute_net_opportunistic_usd` already does (cohen_malloy_classifier). If it nets sells at the source, Phase 1 must split buy/sell there.
- **Solo-project doctrine:** no backward-compat; rename fields freely. But the Django mirror + the new `test_expert_column_parity`-style guards must move in lockstep.

## 6. Open questions (for adversarial review)

1. Is a market-cap-normalized magnitude defensible when most candidates are small/mid-cap thematic names with thin Form-4 coverage? Could `op_frac_mc` over-weight micro-caps (tiny denominator)? ADV-normalization vs mcap?
2. `compose_weighted_score` is a 1–5 int with insider 2×. How to fold a continuous E∈[0,1] without breaking the documented 1–5 contract or silently re-weighting the other factors?
3. Is 180d the right window for a *thematic event-driven* tool (vs the 6–12mo literature horizon for a standalone insider factor)? The catalyst is the driver here; insider is corroboration.
4. Should opportunistic **selling** be surfaced at all (display), given FAJ "some sales are positive"? Or dropped entirely to avoid the inversion noise?
5. Is the whole signal worth keeping if 0/353 candidates show any opportunistic buy — is this a coverage problem (form4 parquet freshness) or a genuine "thematic momentum names don't have insider buying" structural fact? Verify coverage before investing in the redesign.

## 7. Decision

Pending adversarial review (zen `deepseek/deepseek-v4-pro` + Perplexity pass on THIS memo) per the pre-compute review doctrine. Lock after findings folded in; then implement Phase 1.

## 8. Adversarial review outcome + Phase-0 results (2026-06-17)

Two independent reviewers (zen `deepseek/deepseek-v4-pro` thinkdeep; Perplexity Sonar-reasoning, 15 cites) plus an empirical Phase-0 probe. **Strong consensus**, and Phase-0 was executed.

### 8.1 Phase-0 (coverage/classifier diagnosis) — RAN, PASSED
- Form-4 store is healthy: **13,035 raw `P` purchases in 200d across 1,710 tickers** (99% priced). NOT a store-coverage bug.
- For the 128 brief tickers (125 in store), last 180d: **39 have ≥1 raw `P` purchase**; raw net P−S USD = 25 positive / 85 negative; raw buy-only USD>0 = 39 tickers.
- **Conclusion:** the live "0/353 positive" is NOT genuine absence — it is the compound effect of three transforms collapsing a real signal: (1) **netting** buy−sell (39 buy-havers → 25 net-positive), (2) **90d window** (vs 180d here — buys 90–180d ago are invisible), (3) **opportunistic-only** Cohen-Malloy filter (further drops routine-classified P). The signal is real and recoverable → the redesign is justified, and the highest-impact levers are **buy-only** and **window length**, exactly as proposed.

### 8.2 Correction to a reviewer assumption
Perplexity assumed the misleading percentile feeds ORDERING and urged "remove insider from ordering immediately." Verified false: the percentile is **display + LLM-prompt only**; ordering uses the boolean `insider_is_positive` (`score_usd>$50k`), which is always False → insider already contributes **zero** to `layer4_weighted_score`. So insider is NOT currently mis-ordering; the live harm is purely the misleading **display** (and a dead 2× weight). This makes the minimal display fix even safer (ordering untouched).

### 8.3 Consensus findings folded in
1. **Phase 0 is a gate, not a risk note** — DONE above (passed). [both reviewers, highest severity]
2. **Minimal option first** — fix the misleading display now (honest labels; no high-percentile chip on a zero/no-buy); this is independent of any signal redesign and ships immediately.
3. **Avoid market-cap normalization** on thin small/mid-cap cohorts — tiny denominators over-weight micro-caps; prefer **ADV-normalization**, market-cap **bucketing**, **winsorization**, or **count/cluster** features (number of distinct buyers, cluster ≥2 insiders). Defer `op_frac_mc`.
4. **Don't over-engineer ranking** — with buy-only the magnitude is ≥0, so the sign-aware two-stage scheme reduces to **`has_buy` indicator + within-buyers rank**. Use that; keep zeros neutral by construction.
5. **PIT / filing-lag (live tool!)** — anchor the signal timestamp to the **Form-4 FILING/availability date, not the trade date** (insiders file up to ~2 business days late); enforce a daily cutoff; any time-decay decays from filing date. The current `filter_records` uses `transaction_date` — must verify/fix to filing-date for a forward-live screen (look-ahead risk).
6. **No 2× weight / no 1–5 fold for an unvalidated signal** — keep insider as a separate display dimension; only fold into ordering AFTER an offline lift test (precision@K / does it improve the event-driven ranking) shows incremental value; start with a small weight, not 2×.
7. **Data-quality** — confirmed P/S-only with option/gift codes excluded (good); netting is the issue, not code contamination.

### 8.4 Revised plan (supersedes §4)
- **Phase 0 — DONE** (signal real, recoverable).
- **Phase 1 (ship now, display-only, no scoring change):** honest insider chip — show "recent insider buying" only when buy-only USD>0; for 0/negative/null show neutral "no opportunistic buys (Nd)" — never a high percentile on a zero. Reframe the brief prompt line likewise. Zero risk to selection/ordering (insider already inert there).
- **Phase 2 (signal recovery):** switch magnitude to **buy-only**, lengthen window **90→180d**, fix to **filing-date** PIT alignment, replace `≤`-percentile with **`has_buy` + within-buyers rank**. Keep it OUT of `layer4_weighted_score` (display/rank dimension only) until Phase 4.
- **Phase 3 (robust features):** count/cluster + ADV-normalization (NOT mcap) + winsorize/bucket; optional roles/persistently-profitable.
- **Phase 4 (integration, gated on evidence):** offline lift test; only then a *small* weighted contribution to ordering, never a 2× on an unvalidated factor.

**Lock:** Phase 1 is locked and ready (display-only, independent of the rest). Phases 2–4 stay DRAFT pending a go decision per phase.
