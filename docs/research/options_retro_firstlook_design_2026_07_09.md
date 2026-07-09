# Options retro EXPLORATORY PILOT — reconstruct options features for matured EDGE outcomes

**Status:** LOCKED (adversarial review complete 2026-07-09: Perplexity deep-research
+ zen deepseek-v4-pro, both rounds of findings applied; implementation pending
the trial-limits probe. Spend blocker removed — fresh iVolatility trial)
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
feature source. The question is explicitly CONDITIONAL: incremental
information within THIS pipeline's catalyst-selected, high-IV subpopulation
— not a claim about options factors in general (selection truncates the IV
distribution and creates collider structure between catalyst strength, IV,
ATR and outcomes). Registered as an exploratory look (see §7).

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
| VRP (decomposed) | vendor `ivx30` + `hv20` jointly | the ratio itself is NOT tested (near-degenerate vs the ATR partial); the VRP hypothesis = the IVX30 coefficient conditional on HV20 |
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

## 6. Analysis plan (amended per adversarial review)

- **Regression, not naive partial correlations:** ten-day market-excess on
  each feature + controls, clustered by brief day (51 day-clusters;
  overlapping k=10 windows make naive p-values invalid —
  Kolari-Pynnonen-class cross-correlation). **Primary p-values via wild
  cluster bootstrap** (51 clusters sits at the boundary where plain CR1
  cluster-robust SEs are downward-biased); CR2-corrected SEs reported
  alongside. Moving-block bootstrap only as a sensitivity diagnostic (≈5
  independent 10d blocks — too few for primary inference).
- **The family is EXACTLY these 4 tests** (pinned; changing any respec
  re-opens the memo):
  | # | Test | Specification |
  |---|---|---|
  | 1 | ivx30 level | excess ~ ivx30 + ATR + mcap + earnings30d |
  | 2 | term slope | excess ~ (ivx180−ivx30) + ATR + mcap + earnings30d |
  | 3 | VRP (decomposed) | excess ~ ivx30 + hv20 + mcap + earnings30d — read the ivx30 coefficient; NO ATR in this one (hv20 ≈ ATR would be near-collinear); the ratio ivx30/hv20 is never tested directly |
  | 4 | ivp30 | excess ~ ivp30 + ATR + mcap + earnings30d |
- **Collinearity diagnostics before inference:** VIF / condition numbers
  across {ivx30, ivp30, term slope, ATR, mcap}; VIF>10 → drop or
  orthogonalize (residualize features on ATR+mcap).
- **Power reality stated up front:** effective N after ticker-episode dedup
  and day-clustering is ~80-150 (design effect ~3-4 on ~350-400 pairs), so
  |rho|~0.1 is undetectable and ~0.2 marginal under the family correction.
  The pilot can SURFACE candidate effects; it cannot rule the class out.
- **Sub-window stability:** report effects split across halves of the
  7-week window (stability hint, not a test).
- ONE primary outcome (market-excess k=10). Terminal realized_r is
  descriptive only — reported, never tested (keeps the family at 4 tests).
- Deliverable: `options_retro_pilot_results_<date>.md` with per-feature
  coefficients, clustered CIs, VIFs, and the §8 decision mapping.

## 7. Multiplicity / look accounting (amended)

Explicit exploratory/confirmatory split (data-snooping doctrine, Harvey et
al. factor-zoo + White reality-check):

- **This pilot is EXPLORATORY** — hypothesis-generating, registered in the
  ledger as `options_retro_pilot_2026_07` with a 4-feature family × ONE
  primary outcome, Bonferroni within family AND the program-level counter
  (the July covariate re-runs on the same outcome panel are part of the
  broader family; this is why no discovery claim can come out of the
  pilot).
- **September (#774) is the CONFIRMATORY stage:** BEFORE it runs, the
  pilot's surviving candidates get pre-registered as specific directional
  hypotheses with an elevated hurdle (t>3 per Harvey's recommendation for
  new factors). Anti-contamination constraints on that pre-registration:
  (a) it reproduces the pilot's WINNING SPECIFICATION verbatim (controls,
  clustering, collinearity handling) — no respec based on pilot
  diagnostics; (b) NO thresholds derived from pilot data (the pilot
  delivers a directional yes/no, never a cutoff); (c) the outcome stays
  k=10 — any other horizon looked at in the pilot counts as an extra test
  now and cannot become September's primary.
- Anti-leakage rule: the pilot tests exactly the 4 pre-committed features;
  no adding features, horizons, or thresholds after seeing results.

## 8. Decision gates (amended — the pilot cannot close the class)

- **Pilot surfaces a candidate** (wild-cluster-bootstrap effect surviving
  Bonferroni over the pilot's OWN 4 tests — the program-level multiplicity
  counter applies to eventual DISCOVERY claims, never to this surfacing
  gate, else the gate would be dead by construction): it becomes a
  pre-registered directional hypothesis for the September confirmatory
  stage (#774); NO selection or exit change before confirmation.
- **Pilot null:** NOT class closure — with effective N~80-150 a null is
  expected even for economically meaningful effects (Type II). Consequence
  is only: no acceleration; #774 proceeds in September as the properly
  powered forward accumulation continues; no further retro spend.
- **Data-quality failure**: HALT — report coverage, no verdicts from a
  censored subsample. "Coverage" = the share of matured pairs for which
  ALL FOUR tests are computable, including ivp30's full 1-year lookback
  (a ticker present in smd but with history starting mid-window does NOT
  count as covered). Threshold: <70% → HALT (drop-ivp30-and-shrink-family
  is the one permitted fallback, re-opening §6's family table).

## 9. Cost / effort

- $0 — fresh trial account (user-provided). FIRST implementation step:
  probe the trial on 2-3 tickers BEFORE the full pull and verify
  explicitly: (a) smd endpoint access at all, (b) **>=14 months of daily
  history returned** (ivp30 needs a 1y lookback before the earliest brief
  date 2026-05-19), (c) daily AND total request quotas (if <180/day the
  pull becomes a multi-day resumable job), (d) whether failed calls count
  against quota (cache-first + resume verified on the probe tickers); if the trial caps below the
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

- **ivp30 lookback depth:** the trial may silently truncate daily history;
  a percentile over a shorter window is a DIFFERENT feature — the probe
  (§9) and the coverage rule (§8) both guard this.
- **Underpowered by construction:** effective N ~80-150; the pilot's only
  legitimate outputs are "candidate found" or "nothing detectable at this
  N" — never "class dead".
- **Horizon mismatch:** IVX30 is a 30d surface vs our k=10 outcome; noted,
  accepted (a 20d secondary outcome may be reported descriptively if the
  outcome panel supports it).
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
