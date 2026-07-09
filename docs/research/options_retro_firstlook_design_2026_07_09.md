# Options retro first-look — reconstruct options features for matured EDGE outcomes

**Status:** DRAFT (pending adversarial review; spend blocker REMOVED — user opened a fresh iVolatility trial account 2026-07-09)
**Date:** 2026-07-09
**Author:** research session
**Parent:** `docs/research/options_telemetry_design_2026_07_07.md` (forward telemetry, live since 2026-07-08)

## 1. Problem / motivation

The forward options telemetry (PR #772/#779) accumulates ~10-18 candidate
rows per day and its first-look vs EDGE (issue #774) needs N≥30 matured
outcomes with `chain_quality=OK` — realistically ~2026-09. Meanwhile the
EDGE population already holds **537 (brief_date, ticker) pairs across 51
brief days (2026-05-19 → 2026-07-08, 180 unique tickers)**, a large share
of which have matured market-excess outcomes today. Those candidates were
never stamped with options features (the yfinance snapshot is PIT by
construction and unreproducible), so the only way to use the banked
outcomes is to buy historical options-implied data and reconstruct the
features as-of each candidate's brief date.

This inverts the parent memo's sourcing ladder deliberately: the memo said
"iVolatility becomes relevant only if a forward signal appears"; here
~$399 (one metered month) buys ~2 months of calendar time on the SAME
question. If the retro shows a signal, the forward yfinance telemetry
becomes a clean out-of-sample confirmation set; if it shows nothing even
in-sample, we learn early and stop spending attention on the class.

## 2. Hypothesis under test

Options-implied features measured as-of the brief date separate matured
EDGE outcomes **after the ATR partial** (the ROIC/quality lesson: any
volatility proxy must show incremental value over realized-vol ATR, which
is already a confirmed Bonferroni-clear separator). This is the SAME
hypothesis as issue #774's first look, tested earlier on a different
feature source — it must be registered as a look (see §7).

## 3. Data source

**iVolatility `/equities/stock-market-data` (smd)** via the fresh TRIAL
account the user opened 2026-07-09 (no $399 spend; the metered-month
fallback stays available if the trial proves too limited).

- Existing infrastructure: `alphalens_pipeline/data/alt_data/
  ivolatility_smd_cache.py` (immutable per-ticker parquet cache, range-mode
  pulls, resumable) and the v7/v9D feature joiner
  (`alphalens_research/screeners/options_implied/features.py`) already
  compute exactly the validated stack.
- **PIT status: already validated.** The v7 pre-registration validated smd
  point-in-time correctness (ivp30 PIT validation 0.9990 per the feature
  joiner docstring); the data-vendor PIT gate for this source is satisfied
  and is NOT re-run here.
- Cache doctrine applies: raw responses persist to
  `~/.alphalens/ivolatility_cache/` BEFORE processing; never re-fetch on
  retry (metered).
- Request budget: range-mode = ONE call per ticker for the whole window →
  **~180 calls + retries margin** for the full universe. Trivially inside a
  metered month.

## 4. Features (4 columns, vendor-computed surfaces)

The v9D-validated stack, read verbatim from smd rows as-of each brief date:

| Feature | smd source | Note |
|---|---|---|
| `ivx30` | vendor 30d IV index | |
| `ivx180_minus_ivx30` | vendor term slope | |
| `ivx30_over_hv20` | vendor VRP ratio | contemporaneous HV — no store-lag issue retro |
| `ivp30` | 1y rolling percentile of ivx30 | computable retro (full daily history available) — the one feature the forward telemetry deliberately dropped |

**Not available: XZZ skew** — smd is a surface-index endpoint, not a
per-strike chain; the retro covers 4 of the 5 forward feature families.
The skew hypothesis stays forward-only (#774).

**Poolability: the retro features NEVER pool with the forward telemetry.**
Different vendor, different construction (vendor IVX30 vs our two-expiry
interpolation; vendor HV20 vs grouped-store RV20). The retro frame carries
its own label `options_retro_ivol_smd_v1` and lives in an analysis
artifact, not on the candidate parquets.

## 5. Outcomes and universe

- **Primary outcome: market-excess return at k=10 sessions** over all
  matured (brief_date, ticker) pairs — fill-INDEPENDENT, no NO_FILL
  haircut, the same outcome the selection-attribution runs use. Matured
  subset as of today: roughly brief dates ≤ 2026-06-24, i.e. **N≈350-400
  pairs** and growing daily.
- **Secondary axis: terminal `realized_r`** on the fill-dependent TERMINAL
  subset (~100+ rows) — reported, not verdict-bearing (selection into
  fills confounds).
- Ticker-episode dedup per the July attribution doctrine (persisting
  candidates are one episode, not N independent observations).
- Controls: ATR partial (mandatory), earnings-within-30d indicator (AV
  earnings cache), mcap, the July-rerun covariate set for comparability.

## 6. Analysis plan

Same apparatus as the July signal-attribution re-run (that code is the
spec): per-covariate association with market-excess, THEN partial
correlation given ATR; a feature "shows signal" only if the ATR-partial
survives the family's multiplicity correction. Weekend/duplicate handling
and regime clustering per the #774 checklist. Deliverable: one analysis
memo `options_retro_firstlook_results_<date>.md` with verdict per feature.

## 7. Multiplicity / look accounting

This is a TEST and burns a look: registered in the ledger as
`options_retro_firstlook_2026_07` with a 4-covariate family (ivx30, term
slope, vrp ratio, ivp30) × 1 primary outcome, Bonferroni within family
plus the program-level counter. Consequence accepted explicitly: the
September forward first-look (#774) is then a SECOND look at the class —
its role shifts from discovery to out-of-sample confirmation of whatever
the retro finds (or a re-test on a different feature construction if the
retro is null). No pretending otherwise later.

## 8. Decision gates

- **Retro signal (≥1 feature survives ATR partial + family correction):**
  forward telemetry (#774) becomes the OOS confirmation; no selection or
  exit change before that confirmation. iVolatility stays cancelled after
  the pull (cache is immutable).
- **Retro null:** the class stays telemetry-only; no further spend; #774
  proceeds as planned in September (different construction + skew, so a
  null retro does not pre-empt it, but the look accounting above applies).
- **Data-quality failure** (smd coverage <70% of matured pairs, e.g.
  delisted/small names missing): HALT per the vendor-gate spirit — report
  coverage, do not draw verdicts from a censored subsample.

## 9. Cost / effort

- $0 — fresh trial account (user-provided). FIRST implementation step:
  probe the trial's limits (request quota, history depth, smd access at
  all) on 2-3 tickers BEFORE the full pull; if the trial caps below the
  universe's needs, fall back to the metered month (~$399, separate user
  authorization) rather than drawing verdicts from a truncated pull —
  the §8 coverage HALT applies to trial-limit censoring exactly as to
  delisting censoring.
- ~180 range-mode requests; cache-first; resumable.
- Engineering: a fetch script reusing `ivolatility_smd_cache`, a join of
  smd rows to (brief_date, ticker) pairs, and a re-run of the existing
  attribution notebook/script — days, not weeks; laptop-scale compute (no
  runpod).

## 10. Risks / honest caveats

- **Vendor-feature mismatch risk:** a retro signal on vendor IVX30 may not
  transfer to our yfinance interpolation (and vice versa). This is why the
  two sets never pool and why September's confirmation matters.
- **Regime concentration:** all 51 brief days sit in one ~7-week window of
  2026 — any finding is regime-conditional by construction; the memo's
  measurement-first skepticism applies.
- **Survivorship in smd:** delisted/renamed tickers may be absent; §8's
  coverage HALT guards the verdict.
- **Trial-account limits:** quota/history caps could silently censor the
  pull — mitigated by the pre-pull probe and the §8 coverage HALT.
- **Speed-for-evidence precedent:** even at $0 this is a deliberate
  one-look exception, NOT a reopening of the "acquire data before
  evidence" door for selection work.

## 11. Out of scope

- Any selection, ordering, or exit change.
- Pooling retro features with the forward telemetry columns.
- XZZ skew retro (needs per-strike history — different product tier).
- Re-running or amending the closed v9D/pc_abnormal retrospectives.

## 12. Next steps (in order)

1. Adversarial review of this memo (zen + Perplexity) — pre-spend, per
   doctrine.
2. Put the trial account's key into `.env` as `IVOLATILITY_API_KEY`
   (old key expired); probe trial limits on 2-3 tickers before the full
   pull.
3. Register the look in the ledger; implement fetch+join (TDD); run
   analysis; write the results memo.
