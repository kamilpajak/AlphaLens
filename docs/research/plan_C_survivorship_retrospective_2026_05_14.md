# Plan C — Survivorship-Bias Retrospective Audit (paradigm research diagnostic)

**Date:** 2026-05-14
**Status:** **REJECTED 2026-05-14** post zen adversarial review (gemini-3-pro-preview, continuation_id `7e79f785-c2b6-40c1-945c-4c7f03aa79f1`). Not executed.

## Rejection rationale (2026-05-14 post-review)

Zen identified the plan as procrastination-disguised-as-rigor with multiple methodological holes:

1. **Apples-to-oranges universe mismatch** — existing `pit_universe/` 2015-06 sample has ~500 tickers (likely small-mid-cap subset); comparing against S&P 1500 PIT-union snapshot (~1500 tickers) confounds universe composition with survivorship bias. Δαt would be driven by factor exposure differences (small-cap factor loadings, dispersion), not the actual bias we want to measure.
2. **Decision rules unprincipled** — averaging Δαt across n=4 heterogeneous paradigms (insider event-driven vs options-implied vs residual-momentum) conflates signal-specific idiosyncrasies with systematic bias. Each signal type has different survivorship mechanics (e.g. mean-reversion strategies *destroyed* by including bankruptcy losers; momentum *inflated* by excluding them — sign-flips per signal).
3. **Confounders missed** — yfinance reuses delisted tickers / drops them after ~3-5y → backfill source itself is survivorship-biased. Plus micro-cap slippage in dying companies (illiquidity spikes before delisting) would inflate true-PIT αt artificially if cost model isn't regime-adjusted.
4. **H2 motivated reasoning** — hypothesis "some FAIL verdicts may have been false-FAILs" was wishful thinking. If adding bankrupt losers to a momentum strategy pushes αt from 1.58 to >3.5, signal is a bankruptcy-rebound anomaly, not idiosyncratic momentum.
5. **Action invariance** — even if Plan C produces clean Δαt evidence, the resulting action (require true-PIT for paradigm #16) is the SAME action we'd take by simply adopting Perplexity's snapshot-bias prior as the default. The retrospective autopsy doesn't change anything actionable about future paradigms.
6. **Budget unrealistic** — Phase B delisted-OHLCV alignment (ticker reuse, splits-pre-bankruptcy, CIK mapping) is 1-2 weeks of grungy data engineering, not 1-2 days. Memo underestimated by 5-10x.

## Decision

Drop Plan C. Adopt **Perplexity-cited 20-40 bps/y snapshot-bias prior as the default**. Update CLAUDE.md `## Research methodology` with new mandatory pre-audit gate: any paradigm with universe size > 100 tickers MUST use true PIT panel from day one (intersected snapshot rosters + delisted-ticker augmentation from `~/.alphalens/survivorship/`). Skip retrospective autopsy of completed paradigms.

Memo retained as research-record (not deleted) per project documentation policy.

---

**Original draft below:**

---
**Motivation:** Quantify how much of reported αt across past paradigms is inflated by survivorship bias from snapshot-fallback S&P 1500 PIT universe vs true PIT membership augmented with delisted-ticker histories. After 14 paradigm failures, this is a one-time protocol audit — NOT a new paradigm.

---

## §0. Why now

Perplexity research 2026-05-14 (continuation N/A — sonar deep research) raised survivorship bias as a leading-candidate explanation for AlphaLens's 14-failure track record:

> "When backtests use current-period index constituents or accounting databases mapped backward (rather than true point-in-time constituents), reported alphas overstate by approximately 15-50 bps annually, depending on the anomaly. For quality factors, the inflation is typically 20-40 bps; for accruals, 25-50 bps."

But **AlphaLens has prior empirical evidence that the bias direction is AMBIGUOUS** on our existing universe — `survivorship/baseline_vs_augmented.csv` shows Carhart-4F αt **rising** from 2.62 → 2.99 (+0.37) when delisted tickers are added to a sample backtest, and AP-8 anti-pattern doc mentions PIT-95 vs current dropping αt from 1.70 → 1.36 (-0.34) on a different sample. **Direction depends on the universe construction method**: retrospectively-curated tickers tend to be ex-post winners (inflates αt); raw inclusion of delisted bankruptcies adds losers (deflates αt); the net effect varies by sub-universe.

Plan C resolves this empirically for the paradigms that came closest to PASS — so we know whether INCONCLUSIVE / SLIPPAGE-FAIL verdicts were real-signal-below-bar or survivorship-inflated false-positives.

## §1. Scope: which paradigms to audit

Audit the **4 highest-αt paradigms** in the project, ranked by max reported αt across IS/OOS/FL or retrospective windows:

| # | Paradigm | Reported αt | Original verdict | Reason on shortlist |
|---|---|---|---|---|
| 1 | insider_form4_opportunistic | +2.71 gross OOS | SLIPPAGE-FAIL 2026-05-12 | Highest reported αt; if survivorship inflated → original PASS_MARGINAL questionable |
| 2 | pc_abnormal_volume retrospective | +2.65 pre-2018 | INCONCLUSIVE 2026-05-05 | Triggered paper-trade; if inflated → paper-trade signal weaker than thought |
| 3 | v9D options-implied retrospective | +2.45 pre-2018 | INCONCLUSIVE 2026-05-05 | Same — paper-trade in observation |
| 4 | idiosyncratic_momentum FL window | +1.58 | FAIL 2026-05-14 | Smallest of the four; useful as control — if Δαt is consistent, builds confidence |

Excluded from Plan C: ev_fcff_yield #13 (uses R2000 ex-financials via SimFin universe, different construction), PEAD-v2 #14 (in-flight, audit not yet done), Layer-2 closed paradigms (FAIL outright, marginal even with PIT correction).

## §2. Hypothesis + decision rules

**H0 (null):** Δαt = αt_snapshot − αt_true_pit ≈ 0 across the 4 paradigms. Protocol is unbiased; original verdicts stand.

**H1 (Perplexity-direction):** Δαt > +0.3 t-stat consistently. Snapshot fallback overstates αt. INCONCLUSIVE paradigms (v9D, pc_abnormal) should be downgraded; protocol needs adjustment for future paradigms.

**H2 (AlphaLens prior-direction):** Δαt < -0.2 t-stat consistently. Snapshot under-states αt (survivorship excludes winners that came back, or includes lookback-curated winners). Original verdicts are conservative; some FAILs might have been PASS_MARGINAL with true PIT.

**H3 (heterogeneous):** Sign varies per paradigm. Bias direction depends on signal type — e.g. options-implied (v9D, pc_abnormal) bias one direction, fundamental (IM) another. Most likely outcome given prior evidence.

**Decision rule:**
- If |Δαt| < 0.15 mean across 4 paradigms → protocol healthy, no action needed.
- If +0.3 ≤ Δαt mean ≤ +0.5 → systematic +20-30 bps/y inflation; update paradigm-failure postmortem and adjust future Bonferroni doctrine by adding a true-PIT requirement.
- If Δαt mean > +0.5 → strong inflation; retroactively downgrade INCONCLUSIVE verdicts; require true PIT for all future paradigms.
- If Δαt mean < -0.2 → systematic *deflation*; some FAIL verdicts may have been false-FAILs; consider rerunning shortlisted FAILs (IM FL window αt 1.58 + delta could cross threshold) on true PIT.

**Pre-commitment:** decision rule is locked before any compute. No moving the goalpost after seeing results.

## §3. Data inventory (what we already have)

**PIT universe (already built):**
- `~/.alphalens/pit_universe/{YYYY-MM}.yaml` — 207 monthly snapshots 2009-01 through 2026-03
- Each yaml: `asof: YYYY-MM-DD` + `tickers: [...]` — ~500-2000 tickers per snapshot
- Built by `scripts/build_pit_universe.py` (XBRL shares outstanding + yfinance, one-shot)
- **Caveat to verify:** what universe is this? Per the 2015-06 sample (~500 tickers) it looks like a small-mid-cap subset, not full R2000. **Phase A action item:** characterize PIT universe vs S&P 1500 PIT union we used for IM/PEAD.

**Delisted ticker registry:**
- `~/.alphalens/survivorship/delisted_2007_2018.parquet` — 2051 tickers (cols: ticker, delisted_date, reason, source, cik)
- `~/.alphalens/survivorship/delisted_2021_2026.parquet` — 6237 tickers (cols: ticker, delisted_date, name, reason)
- **Gap:** 2018-2020 delisted not in either file. **Phase A action item:** confirm continuity or backfill the gap.
- Reason codes: mostly "unknown" (~7300/8300) + 909 explicit "acquisition". For our purpose (we want to include the price history up to delisting date), reason categorization is informative but not required.

**Delisted OHLCV:**
- `~/.alphalens/survivorship/lean_data/equity/usa/daily/<ticker>.zip` — Lean-format daily bars
- Built by `scripts/fetch_survivorship_ohlcv.py` — currently biotech-focused (per script's name-based filter `therapeutics/biopharm/biosciences`); extend filter to FULL UNIVERSE for plan C
- **Gap:** non-biotech delisted tickers may not have OHLCV. **Phase A action item:** measure coverage on sample.
- **Backup source:** yfinance keeps delisted ticker histories for ~5 years post-delisting on most names; can refetch for missing names.

**Prior comparison artifact:**
- `~/.alphalens/survivorship/baseline_vs_augmented.csv` — 2 rows showing baseline (n=113) vs augmented (n=163) on some prior experiment. Carhart-4F αt 2.62 → 2.99. Useful empirical anchor.

## §4. Methodology

### §4.1 Phase A — Data inventory + gap analysis (1 day)

1. Materialize the **true PIT universe panel** as a single parquet:
   - For each month-end m in 2010-01 through 2024-12, list of tickers active as of m (from `pit_universe/{YYYY-MM}.yaml`)
   - Augmented with delisted tickers whose `delisted_date > m` (still active at m)
   - Output: `~/.alphalens/survivorship/pit_panel_true.parquet` (~300k rows, schema: `asof_month, ticker, status` where status ∈ {active, delisted_active})
2. Cross-check PIT universe size per month vs the S&P 1500 PIT union we used as snapshot fallback. Compute mean monthly tickers + dispersion.
3. Catalog delisted-OHLCV coverage:
   - For every ticker in the panel that delisted during 2010-2024, check whether `~/.alphalens/prices/` or `~/.alphalens/survivorship/lean_data/` has its history
   - Output: `~/.alphalens/survivorship/coverage_2010_2024.csv` listing missing tickers
4. **Gate:** if >20% of delisted tickers have no OHLCV coverage, Phase B starts with backfill. If <20%, proceed to re-run.

### §4.2 Phase B — Delisted OHLCV backfill (1-2 days, conditional)

Run only if Phase A coverage check fails:

1. Extend `fetch_survivorship_ohlcv.py` to drop the biotech-name filter (generalize to all delisted tickers in panel).
2. For each missing ticker, try yfinance first (free, often works for tickers delisted in past 3-5 years), fall back to Polygon historical (paid, we have key in `.env`).
3. Persist to `~/.alphalens/prices/<ticker>.parquet` (same format as live tickers) so existing experiment scripts can consume.
4. **Cost ceiling:** $0 (yfinance) preferred; only escalate to Polygon if yfinance gap is >30%.

### §4.3 Phase C — Add `--universe-mode=PIT_TRUE` to experiment scripts (1 day)

Modify the 4 paradigm experiment scripts to accept a new universe-mode flag:
- `scripts/experiment_insider_form4_opportunistic.py` — currently universe-mode logic exists
- `scripts/experiment_pc_retrospective.py` — for pc_abnormal retrospective
- `scripts/experiment_v9d_retrospective_pre_2018.py` — for v9D retrospective
- `scripts/experiment_idiosyncratic_momentum.py` — already has `--universe-mode` arg (currently locks to `SP1500`); add `PIT_TRUE`

Implementation contract:
- `--universe-mode=PIT_TRUE` loads `pit_panel_true.parquet`
- At each rebalance date t, universe = tickers with status ∈ {active, delisted_active} where `asof_month ≤ t and (status='active' OR delisted_date > t)`
- Experiment downstream consumes the universe identically — only the list of eligible tickers per rebalance changes
- Pre-existing histories cache transparently picks up delisted-ticker OHLCV from the same directory (no separate loader)

### §4.4 Phase D — Re-run 4 paradigm audits on true PIT (1 day, ~$1 spend on runpod)

Re-launch each paradigm with `--universe-mode=PIT_TRUE` using the existing audit orchestrators where they exist (insider_form4, idiosyncratic_momentum) or single-pass retrospectives (v9D, pc_abnormal). Same windows + cost grid + Carhart-4F regression as the original audit. Wall budget: ~30 min each on a CPU pod, total ~2 hours.

### §4.5 Phase E — Verdict memo + ledger amendment (0.5 day)

1. Build comparison table: paradigm | window | αt_snapshot | αt_true_pit | Δαt | Δexcess_ann_pct
2. Apply decision rule from §2 to mean Δαt across 4 paradigms
3. Write `docs/research/survivorship_audit_verdict_2026_MM_DD.md`
4. If protocol bias detected: add amendment block to each affected ledger entry (do NOT mutate original verdict; add `outcome_corrected` block) + update CLAUDE.md project status
5. Update memory: add finding to `feedback_universe_baseline_cyclicality_2026_05_10.md` peer or new `feedback_survivorship_bias_*.md` entry

## §5. Risks + mitigations

| Risk | Probability | Mitigation |
|---|---|---|
| PIT universe is too small subset of S&P 1500 (per 2015-06 sample ~500 tickers) — comparison not apples-to-apples | HIGH | Phase A characterization step; may require parallel run with snapshot universe restricted to PIT-panel-overlap subset for fair comparison |
| Delisted OHLCV coverage worse than 50% | MEDIUM | Phase B backfill; if still <70%, document gap and report Δαt with caveat |
| Re-run produces different verdict for IM/insider_form4 (which we already audited & closed) | MEDIUM | Acceptable outcome; verdict-corrected entries are normal in research record |
| `--universe-mode=PIT_TRUE` engine integration breaks something | LOW | TDD: smoke test on 6-month window before full audit |
| Cost overrun on runpod (~$1 budget) | LOW | Use community CPU EU-RO-1 (same as paradigm #15, $0.07/hr) |
| Survivorship comparison itself is multiple-test (each paradigm gets compared to its own baseline) — Bonferroni? | LOW | Plan C is a diagnostic AUDIT not a new paradigm hypothesis test. Decision rule is descriptive (Δαt magnitude + direction), not significance-thresholded. No Bonferroni adjustment needed. |
| Existing infrastructure scripts have bit-rotted since their last use | MEDIUM | Phase A includes a smoke run of `build_pit_universe.py --limit 50` and `fetch_survivorship_ohlcv.py --dry-run` to verify. |

## §6. Total budget

- Wall: 4-6 days of part-time engineering across 5 phases
- Compute: $1-2 on runpod (4 paradigm re-runs × ~30 min × $0.07/hr)
- New code: ~200 LOC across 4 experiment scripts + 1 panel materializer
- New tests: ~30-40 (universe loader contract + smoke for `--universe-mode=PIT_TRUE`)

## §7. Sequencing + dependencies

**Independent of:** paradigm #14 PEAD-v2 audit (waiting on AV cache backfill), paradigm #16 selection (we will pick AFTER plan C verdict — its outcome shapes whether QMJ / earnings-rev / HXZ priors need adjustment).

**Blocking on:** nothing. Can start immediately.

**Recommended sequencing:** Phase A first (cheap, 1 day). Phase A may surface that the prior PIT universe is too small/different/incomplete to support the comparison — in which case Plan C is downgraded to a smaller-scope audit (e.g. only IM since IM was most recently audited with the current orchestrator infrastructure). Make Phase A a decision gate.

## §8. Post-Plan-C decision tree

After Plan C completes, the project's posture on paradigm #16 selection is:

- **If protocol healthy (|Δαt|<0.15):** proceed with normal paradigm-#16 selection (HXZ profitability or earnings-rev as top picks per Perplexity research)
- **If protocol systematically inflates (Δαt > 0.3):** require true PIT for paradigm #16. Re-prior all candidates: subtract 0.3 t-stat from their reported priors. QMJ retail prior drops from 8-15% → 3-7% for 2.5 marginal bar.
- **If protocol systematically deflates (Δαt < -0.2):** some past FAILs may have been close. Re-prior all candidates upward by 0.2 t-stat. Earnings revision momentum prior goes from 10-18% → 14-22% for 2.5 marginal bar. Mildly improved odds for paradigm #16.
- **If heterogeneous:** apply paradigm-specific corrections at the audit stage. Most realistic outcome.

## §9. Sources / prior art

- `docs/research/active_alpha_anti_patterns.md` §AP-8 (Universe survivorship bias from retrospectively-curated tickers)
- `docs/research/pit_universe_backtest.md` (Layer 2b 1.70→1.36 αt drop with PIT-95)
- `~/.alphalens/survivorship/baseline_vs_augmented.csv` (Carhart-4F αt 2.62→2.99 augmented)
- Perplexity research 2026-05-14: estimated 20-40 bps annual quality-factor inflation under snapshot universes
- `scripts/build_pit_universe.py`, `scripts/fetch_survivorship_ohlcv.py`, `scripts/probe_pit_replication.py` — existing infrastructure to reuse
