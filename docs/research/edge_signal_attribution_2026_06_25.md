# EDGE signal-attribution: which signals separate winners from losers?

**Status:** DESCRIPTIVE / exploratory — forward-tracking candidates only, NO deploy-now gate (in-sample acting = the entry-grid overfit trap)
**Date:** 2026-06-25
**Scope:** Across the full 96-column brief-parquet signal set (news/catalyst, fundamentals/value, technicals/momentum, theme/sector, gates/pipeline-meta, experts), which signals — if any — separate EDGE winners from losers? Outcome label is **fill-independent** (not ladder `realized_r`): does the *pick* work, fill or no-fill?
**Method:** Multi-agent Workflow — 5 signal-family finders → adversarial verification of every raw-p<0.05 candidate (reproduce, car_10 confirmation, leave-one-brief_date-out, leave-one-theme-out, collinearity/partial-correlation, Bonferroni) → synthesis. Ran twice; this note reports the re-run on **fresh VPS data through 2026-06-24** (car_5 N=267, car_10 N=176 — ≈1.6×/2.3× the first pass).

---

## 1. Question & TL;DR

The thematic screener's picks are uniformly negative vs SPY and deepen with horizon (see [`project_selection_edge_uniformly_negative_2026_06_23`]). The lever is known to be **selection** (which names get picked), not entry (the entry-grid Faza-0 NO-GO). This sweep asks the next question: *within selection*, do any signal families predict which picks fade hardest?

- **Exactly one verdict-grade separator: `technical_atr_pct`** (entry-time volatility/choppiness). Only signal clearing Bonferroni on **both** horizons, strengthens with horizon, survives every robustness check. High-ATR / already-popped names fade hardest.
- **It does not turn the book positive.** Baseline car_5 ≈ −2.1%, car_10 ≈ −4.5%; every tercile/group is still negative. These signals identify which picks fade *less*, never which make money.
- **Quality (ROIC) separates; cheapness (P/E, P/S, EV/EBITDA, EV/Rev, FCFF-yield) does not.** ROIC-quality is the strongest non-ATR candidate but misses Bonferroni (~2.4× over the bar).
- **Several headline categorical "signals" are space-theme artifacts.** A 3-day space_infrastructure cluster manufactured fake `catalyst_event_type`, news-kind, and P/S effects. Any future categorical/valuation EDGE signal must control for theme.
- **Experts (buffett/oneil/expert_spread) remain untestable** — N=0 on matured car_10 (forward-only). Re-test ~early-July 2026.

---

## 2. Data & method

**Panel.** `~/.alphalens/population_ladders/*.parquet` (outcomes, 436 ladder rows → 328 plannable) ⋈ `~/.alphalens/thematic_briefs/*.parquet` (96 signal columns) on `(brief_date, TICKER)`. Outcome = fixed-horizon market-adjusted CAR (BHAR vs SPY, β=1), prior_close anchor, computed off `~/.alphalens/grouped_daily_history/` via `alphalens_research.diagnostics.{edge_stores, fixed_horizon, anchor}` (reused — no new substrate). Builder materialised one shared panel at `~/.alphalens/diagnostics/signal_panel.parquet`; all finders/verifiers read that single artifact.

**Why fill-independent CAR, not `realized_r`.** Ladder `realized_r` is NULL for NO_FILL and conditions on the entry mechanics. A fixed-horizon CAR from the event date scores the *selection* decision regardless of whether the dip-buy ladder filled — the correct unit for "was the pick good?".

**Horizons.** car_5 (N=267, primary), car_10 (N=176, confirmation), car_20 (N=0 — grouped store reaches 2026-06-23, too shallow; untestable).

**Multiplicity.** ~75 tests → Bonferroni α ≈ 0.0007. Collinear clusters pay one Bonferroni cost each, not per-member.

---

## 3. Verdict table

Effect = car_5 Spearman rho (continuous) or best-vs-worst level/tercile spread (categorical). Direction relative to the negative baseline ("better" = fades less).

| Signal | Family | Verdict | Effect | car_10 holds | day-robust | Bonferroni | Note |
|---|---|---|---|---|---|---|---|
| `technical_atr_pct` | tech | **robust** | rho −0.37→−0.49 | ✅ | ✅ | ✅ | Only verdict-grade separator. High vol/chop → fades hard. Own factor. |
| `technical_pct_off_52w_low` | tech | suggestive | rho −0.224 (both) | ✅ | ✅ | borderline | Cleanest static extension carrier (already run up off bottom → worse). |
| `roic_pct` | value | suggestive | rho +0.16→+0.27 | ✅ | ✅ | ✗ (p=0.0017) | Strongest quality signal; car_10 sig under every day-drop. ≡ buffett_roic_latest. |
| `layer4_weighted_score` | gates | suggestive | rho +0.16→+0.24 | ✅ | ✅ | ✗ | The one positive ORDERING lever; survives partialling catalyst+buffett. |
| `catalyst_strength` | news | suggestive | rho +0.18 (car_10) | car_10 only | ✅ | ✗ | Only non-contaminated suggestive news signal. |
| `n_gates_passed` / `pass_press` | gates | suggestive | rho −0.22→−0.27 | ✅ | ✅ | marginal | **Dissolves into ATR under OLS** — press-passed names are higher-ATR. Not independent. |
| `valuation_ev_rev` | value | suggestive | rho −0.147 car_5 | dies | ✅ | ✗ | Vanishes by car_10; P/S twin. |
| `buffett_quality_score` / `buffett_roic_3y_avg` | value | suggestive | rho +0.35 / +0.38 car_5 | N=0 | ✅ | ✗ | 6 days only, no car_10; ROIC re-packaging. |
| `theme` (omnibus) | theme | suggestive | Kruskal clears Bonf | — | ✅ | ✅ (omnibus) | "Themes differ" is real; named winner/loser levels are single-3-day clusters, not generalizable. |
| `catalyst_event_type` | news | **spurious** | p 0.001→0.084 after theme-drop | ✗ | ✗ | ✗ | "partnership worst" = 6 rows, 100% space_infrastructure, 3 days. Proxy for theme (Cramér's V 0.96). |
| `source_event_title` (news-kind) | news | **spurious** | p 0.016, m_and_a→null after space-drop | ✗ | ✗ | ✗ | Same space-cluster contamination. |
| `valuation_ps` | value | **spurious** | rho −0.14 → flips +0.06 after space-drop | ✗ | ✗ | ✗ | Entirely a space-theme proxy. |
| `expert_spread` | experts | **spurious** | rho +0.40→+0.10 after partialling | N=0 | ✗ | ✗ | 4 days, no car_10, absorbed by pct_off_52w_high. |
| `technical_ma200_distance_pct` | tech | **spurious** | rho −0.151 | ✅ | ✗ | ✗ | Day-fragile; cluster cousins don't carry it. |

---

## 4. The dominant loser pattern: extension / volatility at entry

The technicals family reproduces the run-up/extension → fade pattern as a coherent, mutually-reinforcing cluster: `technical_atr_pct` (robust, both horizons), `technical_pct_off_52w_low` (rho −0.22), `technical_ma50_distance_pct` / `technical_rsi` (negative, strengthening at car_10). **"Buying volatile, already-popped, extended names is the dominant loser pattern"** is the single most reliable conclusion.

Nuance: ATR is partially *separable* from pure extension — partial correlations survive both ways (max cross-rho ~0.37) — so read it as **two semi-independent loser-flags** (entry-time choppiness AND prior run-up), not one restated factor. This triangulates the dynamic pre-entry-runup probe in the sibling memo from the static-feature angle, and the entry-grid NO-GO: the lever is *which* names, and extended/volatile names are the worst of them.

**Changed since the first (thinner) run:** the press-gate inversion ("passing press → worse") looked like a separate mechanism surviving a drawdown control at N=169/78. On the deeper panel it **dissolves into ATR under OLS** — press-passed names are simply higher-ATR. Not a third mechanism.

---

## 5. Quality beats cheapness (ROIC, suggestive)

`roic_pct` is the strongest non-ATR candidate: car_10 (N=132) rho +0.27, p=0.0017, significant under every day-drop and theme-drop. It misses Bonferroni (~2.4× over). Pure-valuation cheapness signals (P/E, P/S, EV/EBITDA, EV/Rev, FCFF-yield) are all null — **quality separates, value does not**. Collinearity collapses the apparent breadth: `roic_pct ≡ buffett_roic_latest` (rho 1.0) ≈ fcff_margin ≈ magic_formula_rank ≈ buffett_quality_score ⇒ ONE quality hypothesis (count once for multiplicity).

Open test for the ~early-July re-run: **does ROIC survive partialling out `technical_atr_pct`?** (quality vs low-vol confound). If yes → a second independent lever; if it collapses into ATR → fold them together.

---

## 6. The space-cluster contamination trap

The space_infrastructure theme (RDW/LUNR/SPIR/IRDM/VSAT/BKSY) over 3 consecutive dates 2026-06-12..14 manufactured fake categorical/valuation signals: `catalyst_event_type` ("partnership worst"), `source_event_title` news-kind ("m_and_a worst"), `valuation_ps`, partly `valuation_ev_rev`. All collapse to null after dropping that one theme; several have rows sharing identical CAR values (duplicate/overlapping events) and span only 2-3 brief-dates.

**Doctrine:** any future categorical or valuation EDGE signal MUST control for theme before being trusted. Effective independent N is brief-DAYS, not rows; a single multi-ticker theme over a 3-day window is one observation, not 6-12.

---

## 7. Nulls worth recording (don't re-test)

market_cap / size buckets; `llm_confidence` (sign flips across horizons); `roe_pct`; `fcff_yield_pct`; `valuation_ev_ebitda`; `valuation_composite_sector_percentile`; `n_gates_failed`; **`source_event_published_at` recency/staleness** (third independent confirmation staleness is not a lever); `technical_ma200_slope_pct_per_day`; `technical_pct_off_52w_high`; `also_in_themes`; `valuation_financials_age_days`; `sector_name` (Kruskal NS, all 8 sectors net-negative). Degenerate / zero-variance: `verified`, `pass_tenk`, `pass_insider`, `buffett_understandable`, `oneil_new_high_split_suspected`, `deep_drawdown_reversal`.

---

## 8. Caveats

- **Negative baseline.** Every tercile/group is still negative — these are "fades less" tilts, not money-makers. No absolute-positive cohort exists.
- **Thin, clustered, lumpy N.** car_5 rests on ~21 distinct brief-dates, car_10 on ~14, all within 2026-05-27..06-24. car_20 = 100% NaN. Categorical levels routinely collapse onto 1-3 themes/days.
- **Only ATR clears multiplicity.** ROIC and the gate/theme "passes" are over the bar or single-theme-inflated. Everything except ATR is exploratory.
- **Forward-only experts.** All `buffett_*` populate from 2026-06-11 (≤6 days, zero car_10); all `oneil_*` from 4 days. Large small-window rho's are mostly ROIC/extension proxies — not independent evidence. Re-test ~early-July when matured (fill-independent selection-N crosses N≥30 then; see the timeline correction in the sibling memo).

---

## 9. Next probes (cheap, ≤3, no new data)

1. **Forward-log a joint "extended + low-quality" loser flag** = high `technical_atr_pct` tercile AND (high `pct_off_52w_low` OR low `roic_pct`). Both legs already on the candidate parquet → telemetry-only column. Track whether the joint flag separates harder than ATR alone (test ATR × quality interaction as N grows). Most actionable deprioritization rule. NOT a deploy-now gate.
2. **Forward-track `layer4_weighted_score` (+ `rank_in_day`)** as the one positive ordering lever; free internal outputs, both point the right way and strengthen at car_10.
3. **Re-run ROIC-quality + catalyst_strength on car_10 at ~early-July maturity**, controlling for theme and partialling out `technical_atr_pct` (does ROIC survive the low-vol confound?). No new data; just re-attribute when N≥30 clean car_10 outcomes exist.

---

## 10. Reproduce

Panel builder is a small read-only script (reuses `edge_stores` / `fixed_horizon` / `anchor`); it joins the two stores and writes `~/.alphalens/diagnostics/signal_panel.parquet`. Refresh inputs from VPS first:

```bash
for d in population_ladders thematic_briefs grouped_daily_history; do
  rsync -a "jacoren@vault.kamilpajak.pl:.alphalens/$d/" "$HOME/.alphalens/$d/"
done
# then rebuild the panel and re-run the Workflow against the fixed panel path
```

All figures computed locally on the rsync'd VPS stores; no tracked code touched.
