# Thematic Outcome-Tracking & Signal-Calibration — Design Memo v1

**Date:** 2026-05-26
**Status:** **DRAFT** — pending adversarial review (zen + Perplexity) before implementation.
**Track:** Thematic event-driven decision-support tool (parallel to factor-paradigm-search). NOT a paradigm test under project doctrine 3.5 — this is signal-quality telemetry for an augmentation tool, not a strategy audit.
**Companion design:** [`thematic_event_tool_v1_design_2026_05_15.md`](thematic_event_tool_v1_design_2026_05_15.md)

---

## §0. Problem statement

The thematic tool surfaces daily brief candidates, each carrying decision-time (PIT) features persisted in the `briefs` Postgres table (`apps/alphalens-django/briefs/models.py`) and the upstream `~/.alphalens/thematic_briefs/` parquets. We want to measure **ex-post** whether those features and event-types were good predictors:

- **CONF** (`gemini_confidence`, 0–1) — LLM subjective theme-fit probability.
- **LAYER4** (`layer4_weighted_score`, ordinal 1–5) — quant composite (insider 2× + FCFF + value/reversal + technicals + catalyst floor).
- **CATALYST** (`catalyst_strength`, 0–1) + **type** (`catalyst_event_type`, 39-member enum).
- Plus the component fields already persisted (insider percentile, FCFF yield, RSI/MA distance, drawdown, etc.).

The **feature snapshot at decision time already exists for free** — the `Brief` row IS the PIT snapshot. The missing half is (1) the forward outcome and (2) the human decision (which candidate the group acted on).

## §1. Two distinct evaluation questions

| | (A) Signal calibration | (B) Decision quality |
|---|---|---|
| Question | Do the features rank/predict abnormal return? | Did the group's picks beat what they skipped + benchmark? |
| Input | `Brief` rows + forward prices | (A) + human decision capture |
| Automation | Fully automatic | Needs decision ledger (manual) |
| Maps to roadmap | `outcomes` table + return job | "feedback ledger sqlite" |

Build (A) first — it runs autonomously and accumulates from today. (B) is the manual layer, dorobiony równolegle.

## §2. Outcome variable — market-model CAR (NOT simple benchmark subtraction)

**Decision: market-model abnormal returns, accumulated as CAR.** Rationale from the event-study literature (Brown-Warner 1985; Kothari-Warner 2007; Lyon-Barber-Tsai 1999):

1. **Simple benchmark subtraction (`r − r_SPY`) is beta-biased.** A β=1.5 mid-cap shows fake "alpha" on every market up-move. Most candidates are $500M–$10B → beta dispersion is large. Reject simple subtraction as the primary measure (keep it as a sanity secondary only).
2. **CAR, not BHAR, for horizons < 60 trading days.** BHAR compounds a downward bias that grows with window length and volatility (LBT 1999: −1.5% to −8.5% over 12m) and is non-normal → weak tests. The tool's holding horizon is 4–8 weeks (≈20–40 sessions), squarely short-horizon. CAR = Σ daily AR is ~normal and composable.
3. **Estimation window:** `α, β` estimated over **[−250, −20] trading days** before event-time (≥120 obs required; skip ticker if fewer). The 20-day gap prevents the catalyst run-up from biasing β. Market factor = SPY excess return (upgrade path: Carhart-4F if factor data wired — deferred, see §8).
4. **Abnormal return:** `AR_t = r_t − (α̂ + β̂·r_mkt,t)`; `CAR(h) = Σ_{t=1..h} AR_t`.

### §2.1 Event-time & entry-timing leakage

- **Event-time = end of the screening run on brief `date` D.** Features are PIT as of D.
- **Window starts at the NEXT session's open** (you cannot trade D's close from a brief generated after close). Record the entry convention explicitly; never adjust post-hoc (p-hacking guard, Kothari-Warner: narrow/mis-set windows miss up to ~50% of the reaction).
- **Horizon set (trading days):** `h ∈ {1, 5, 20, 40, 60}`. 1/5 capture the catalyst pop; 20/40 map to the 4w/8w holding horizons; 60 is the decay tail. Adjacent horizons are highly correlated — treated as ~non-independent in §5.

### §2.2 State flags (bias controls, see §6)

- `delisted` — keep the row with realized (often negative) return; do NOT drop.
- `mna_completed` — for `catalyst_event_type == m_and_a`, freeze return at deal/last price; stop marking forward.
- `halted` / `insufficient_estimation_history` — flag and exclude from the affected horizon, log the exclusion count.

## §3. Schema additions

### §3.1 `outcomes` (new table / parquet, keyed `(date, ticker)`, FK to `Brief`)

```
date, ticker                      -- FK to Brief composite PK
entry_session_date                -- first session > D (entry @ open)
entry_open                        -- entry reference price
beta, alpha, est_window_n         -- market-model params from [-250,-20]
ar_1, ar_5, ar_20, ar_40, ar_60   -- CAR(h) market-model, per horizon
exc_spy_20, exc_spy_40            -- simple SPY-excess (secondary sanity)
mfe_20, mae_20                    -- max favorable / adverse excursion in 20d
positive_20, positive_40          -- binary: CAR(h) > 0 (calibration label)
delisted, mna_completed, halted   -- state flags
insufficient_history              -- excluded-from-horizon flag
computed_at                       -- recompute provenance
```

Populated by a periodic job (yfinance via the canonical client path) that, for any `Brief` row whose horizon has fully elapsed, fetches the estimation window + forward prices and writes the row. Idempotent; re-runs only fill rows whose `h` has matured.

### §3.2 `decisions` (decision ledger, sqlite — question B)

```
date, ticker, picked (bool), group_verdict (enum), note, recorded_at
```

Manual entry. Enables picked-vs-skipped-vs-benchmark analysis. Lives at `~/.alphalens/thematic/decisions.db`.

## §4. Calibration & discrimination of CONF (probability-like)

The "positive outcome" label = `CAR(h) > 0` (primary h=20). Sensitivity at h=40 reported.

- **Reliability diagram** — equal-frequency bins (not equal-width), binomial CIs on each bin's hit-rate. Don't over-read deviations inside the CI band at small n.
- **Brier score + decomposition** (uncertainty / resolution / reliability). Separates "easy problem" from "real skill" (resolution) from "stated p ≈ realized p" (reliability). Bootstrap CIs on each component.
- **AUC / ROC** — pure rank-ordering, insensitive to calibration. Analyst-recommendation benchmark AUC ≈ 0.55–0.65 (realistic ceiling).
- **Shrinkage prior:** analyst literature (Barber-Lehavy-McNichols-Trueman; Womack; Loh-Stulz) shows extreme confidence is systematically overstated ("strong buy" ≈ 50–55% hit). Expect to shrink CONF toward 0.5, especially 0.9–1.0, before trusting it. Check calibration **per `event_type`** (M&A worse, earnings better).

## §5. Ordinal LAYER4 & categorical event-type

- **Spearman rank IC** between score and CAR(h). Finance scale: 0.05–0.10 = meaningful; **>0.15 = suspect overfit/leak**. SE ≈ `1/√(N−1)`.
- **Quantile spread** Q5−Q1 with a **monotonicity check** (Q4 > Q5 ⇒ internally inconsistent composite). Equal-weight buckets. Target ≥50–100 bps/period with monotone improvement.
- **Event-type slice** — mean/median CAR + hit-rate per `catalyst_event_type`. This is the most directly actionable cut ("which event types produce winners") but also the worst-hit by multiplicity + thin per-bucket n.

### §5.1 Multiple testing — FDR, not Bonferroni

Metrics × horizons × event-types ⇒ 100+ nominal tests. Bonferroni at this n kills power.
- Use **Benjamini-Hochberg FDR** (we expect *some* signals real) as primary; Holm (FWER) as a stricter secondary.
- **Count effective independent tests, not raw.** Horizons 1/5/20/40/60 are highly correlated → ~1–2 independent, not 5. Estimate via eigenvalue decomposition of the test correlation matrix.
- **Cross-sectional return correlation** (many tickers per day/theme) inflates t-stats → effective N ≪ raw count. Use **block bootstrap** (block = trading day) for spread / IC significance; plain SE is wrong here.
- Pre-declare the **primary** test(s) that would drive any decision; everything else is labelled exploratory.

## §6. Known biases (must stay in the memo)

- **Conditional-on-surfaced** — we measure only what passed all filters. This validates "are surfaced picks good", NOT "is the signal good in the universe". Not a universe-level claim.
- **M&A truncation** — freeze at deal price (§2.2).
- **Delisting / survivorship** — keep delisted rows with realized return.
- **Look-ahead in benchmark/beta** — β from pre-event window only.
- **Cross-sectional correlation** — block bootstrap (§5.1).

## §7. What is NOT learnable at current n (power analysis)

This is the load-bearing caveat. Minimum detectable IC (80% power, α=0.05):

| N events | min detectable IC |
|---|---|
| 50 | ≈ 0.28 |
| 200 | ≈ 0.14 |

Realistic signal IC is 0.05–0.10. **With dozens of observations we cannot detect a real signal even if it exists**, and cross-sectional correlation lowers effective N further. Briefs began 2026-05-17 (~9 days as of this memo) → far below threshold.

**Implication:** build the ledger NOW; meaningful verdicts are a months-out, hundreds-of-events question. Early dashboards are descriptive only — no "CONF predicts returns" claim until pre-registered primary test clears FDR at adequate power.

## §8. Build order & deferrals

1. `outcomes` table + market-model CAR job (autonomous, question A). **Start here.**
2. Evaluation layer: reliability/Brier/AUC (CONF), rank IC + quantile + monotonicity (LAYER4), event-type slice — all FDR-corrected, block-bootstrap SE.
3. `decisions` ledger (question B) — parallel manual layer.

**Deferred:** Carhart-4F abnormal returns (market-model SPY suffices for v1; revisit if β-only residuals look factor-contaminated). Intraday entry timing (next-open convention is adequate for a 4–8 week horizon).

## §9. TDD contract (for implementation phase)

- CAR computation: red→green against a hand-checked fixture (known β, known returns → known CAR).
- Estimation-window guard: ticker with <120 obs → `insufficient_history`, excluded, counted.
- M&A truncation: synthetic deal-close row freezes return.
- yfinance access only via the canonical client path (per CLAUDE.md "one canonical HTTP client per vendor").

## §10. Open questions for review

1. **"Positive outcome" definition for CONF calibration** — `CAR(h) > 0` vs `CAR(h) > +X%` threshold (margin avoids counting +0.1% as a "win"). Leaning `> 0` primary, `> +2%` sensitivity.
2. **Market-model vs Carhart-4F for v1** — accept SPY-only β residuals, or wire factors now?
3. **Horizon weighting** — report all of {1,5,20,40,60}, or pre-declare h=20 as primary to shrink the multiplicity surface?
