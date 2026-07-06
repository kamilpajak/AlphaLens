# EDGE signal-attribution re-run (July): which signals separate winners from losers?

**Status:** DESCRIPTIVE / exploratory — forward-tracking candidates only, NO deploy-now gate
**Date:** 2026-07-06
**Prior:** [`edge_signal_attribution_2026_06_25.md`](edge_signal_attribution_2026_06_25.md) — this is the scheduled ~early-July re-run that memo's §9 called for (experts matured, ROIC-vs-ATR partial, layer4 forward-track).
**Scope:** The full brief-parquet signal set (news/catalyst, fundamentals/value, technicals, experts, gates/pipeline-meta, theme/sector/plan-geometry) vs fill-independent excess-return outcomes.
**Method:** Multi-agent Workflow — baseline scout → 6 signal-family finders → adversarial verification of every raw-p<0.05 candidate (reproduce, cross-horizon, leave-one-brief-date-out, leave-one-theme-out, partial vs `technical_atr_pct`, day-block bootstrap, ticker-collapse) → synthesis. 41 agents, 284 statistical tests.

---

## 1. Question & TL;DR

Same question as June, on ~2× deeper data: *within selection*, which recorded candidate signals predict which picks fade hardest?

- **June's single verdict-grade separator held and two joined it.** `technical_atr_pct` re-confirms at deeper N (rho −0.35 both horizons, now also car_20), and two NEW Bonferroni-clear, verification-robust separators emerged: **`technical_ma50_distance_pct` (+`technical_rsi` twin)** — an ATR-*independent* extension-fade at 10-20d — and **`n_gates_passed`/`pass_press`** — press-gate passers fade, now surviving the ATR partial that dissolved it in June.
- **ROIC died.** June open probe (a) is answered NO: the entire quality cluster (`roic_pct` ≡ `buffett_roic_latest` ≈ `roic_3y_avg` ≈ `quality_score` ≈ magic_formula) is a low-ATR proxy — the ATR partial annihilates it (car_5 partial r=−0.035 p=0.58; car_10 partial turns *negative*).
- **Experts (probe b): no signal — repackaging + pseudo-replication.** The headline `buffett_roic_3y_avg` rho +0.46 collapses to null at the honest unit (30 unique tickers repeated across days: ticker-collapsed p=0.18-0.87). `oneil_score` clean null; `expert_spread` and margin-of-safety are one-June-week cluster artifacts.
- **Value story revised:** cheapness multiples still don't separate (P/S spurious again), but a **FCF-margin axis** (`valuation_fcf_margin`, with `fcff_yield_sector_percentile`) is the cleanest ATR-orthogonal fundamental thread (robust, not yet Bonferroni-clear), and an **anti-value P/E tilt** at car_10 strengthens under the ATR partial (watch, don't act).
- **Baseline remains negative at every horizon and deepens with it.** No trustworthy absolutely-positive cohort exists.
- **New doctrine:** the unit of independence is the **ticker-episode**, not the row and not even the brief-day (§6).

---

## 2. Data & method

**Panel.** `~/.alphalens/population_ladders/*.parquet` (outcomes, 523 ladder rows → 415 plannable) ⋈ `~/.alphalens/thematic_briefs/*.parquet` (all signal columns) on `(brief_date, ticker)`, 50 brief-dates 2026-04-14..07-05. Outcome = fixed-horizon market-adjusted CAR (BHAR vs SPY, β=1), prior-close anchor, computed off `~/.alphalens/grouped_daily_history/` via `alphalens_research.diagnostics.{edge_stores, fixed_horizon, anchor}` (reused — no new substrate). Shared panel materialised at `~/.alphalens/diagnostics/signal_panel.parquet`; all finders/verifiers read that single artifact. The ladder-native `market_excess_return` (N=383, variable window) is reported as secondary context only.

**Horizons.** car_5 (N=345 plannable / 31 days, primary), car_10 (N=290 / 23 days, confirmation), car_20 (N=125 / **9 days only** — first time testable, context not confirmation).

**Multiplicity.** 284 tests → **Bonferroni α = 0.05/284 = 1.76e-4**. Collinear clusters pay one Bonferroni charge each, not per-member.

**Verification battery** (every raw-p<0.05 finding, adversarial default-spurious stance): reproduce → cross-horizon (car_5/10/20 + market_excess) → leave-one-brief-date-out worst-case p → leave-one-theme-out worst-case p → partial Spearman controlling `technical_atr_pct` (experts additionally vs `roic_pct`) → day-block structure + ticker-collapse where repetition is heavy.

---

## 3. Collinearity dedup — one Bonferroni charge per cluster

| # | Cluster (representative in bold) | Members | Cluster verdict |
|---|---|---|---|
| 1 | **ATR / choppiness** | **technical_atr_pct**; absorbed satellites: roic_pct-as-proxy, magic_formula_health_pass, peer_cohort_level, implied_risk_pct_full (=stop×weight geometry), valuation_ps/ev_rev partly, pct_off_52w_low partly | ROBUST, Bonferroni-clear both horizons |
| 2 | **MA50 extension / overbought** | **technical_ma50_distance_pct**, technical_rsi (rho 0.91 twin) | ROBUST, ATR-orthogonal (rho −0.07 vs ATR), Bonferroni-clear at car_10 |
| 3 | **52w-low extension** | **technical_pct_off_52w_low** (0.80-0.82 with ma200 block) | SUGGESTIVE — ATR-loaded (car_5 partial fails p=0.11) |
| 4 | **Quality (ROIC)** | **roic_pct** ≡ buffett_roic_latest, buffett_roic_3y_avg, buffett_quality_score (0.83), magic_formula_rank | SPURIOUS as independent signal — ATR-absorbed + ticker pseudo-replication |
| 5 | **FCF axis** | **valuation_fcf_margin**, fcff_yield_sector_percentile (0.44) | ROBUST (fcf_margin) / suggestive (sector pct); not Bonferroni-clear |
| 6 | **Anti-value / growth tilt** | **valuation_pe** (+0.50 ev_ebitda, −0.58 roe, −0.89 buffett_mos on n=32) | ROBUST-watch at car_10 only; not Bonferroni-clear |
| 7 | **Cheapness multiples** | **valuation_ps**, valuation_ev_rev (0.94 twin) | SPURIOUS (ATR/space proxy — June re-confirmed) |
| 8 | **Ordering** | **layer4_weighted_score**, rank_in_day (its within-day expression, partial p=0.92), selection_score (untestable, 26 rows/3 days) | SUGGESTIVE — positive lever held, ATR partial marginal at car_5 |
| 9 | **Press gate** | **n_gates_passed** ≡ pass_press (0.97), n_gates_failed mirror | ROBUST, Bonferroni-clear |
| 10 | **Gate-unknown / insider-coverage proxy** | **n_gates_unknown** | ROBUST in-sample but nearly extinct under insider-v2 (4/165 rows) — no forward utility |
| 11 | **Catalyst strength** | **catalyst_strength** (catalyst_confidence null despite 0.58 corr) | SUGGESTIVE, car_10-only slow-burn |
| 12 | **Event-type / theme / sector categoricals** | catalyst_event_type, theme omnibus, sector_name | SPURIOUS (cluster pseudo-replication) |
| 13 | **Industry** | **industry_name** | SUGGESTIVE — loser arm only (semis/comms fade); pharma winner arm = obesity-theme cluster |
| 14 | **Insider flow** | **insider_score_usd** | SUGGESTIVE — real pattern, but N≈5 unique events; ticker-dedup null |
| 15 | **Experts (panel)** | expert_spread, oneil_*, buffett qual pillars, margin_of_safety/owner_earnings | SPURIOUS or NULL across the board |
| 16 | **Joint loser flag** (June probe #1) | ATR-hi AND (52w_low-hi OR roic-lo) | SUGGESTIVE — separates within hi-ATR but ~80% is continuous-ATR curvature; **ROIC leg is dead weight** |

At α=1.76e-4, three clusters clear on verified evidence: **#1 ATR, #2 MA50-extension, #9 press-gate**.

## 4. Final verdict table

Effect = Spearman rho car_5 → car_10 → car_20 (plannable subset). B-clear = raw p < 1.76e-4 on car_5 or car_10 AND verification verdict robust.

| Signal | Family | Verdict | Effect (5→10→20) | Survives ATR partial | B-clear |
|---|---|---|---|---|---|
| technical_atr_pct | tech | **robust** | −0.35 → −0.35 → −0.28 | (is the confound; separable from 52w blocks) | **YES** (both) |
| technical_ma50_distance_pct (+rsi) | tech | **robust** | −0.07ns → −0.26 → −0.27 | **YES** (unchanged, −0.265 p=5e-6) | **YES** (car_10) |
| n_gates_passed / pass_press | gates | **robust** | −0.22 → −0.26 → −0.04ns | YES both horizons (halved at car_5, p=0.036) | **YES** (both) |
| n_gates_unknown | gates | robust (in-sample; extinct forward) | +0.16 → +0.14 → +0.24 | YES | no (p=0.004) |
| valuation_fcf_margin | value | **robust** | +0.12ns → +0.21 → +0.23 | YES (+0.206 p=0.0022) | no (p=0.0021) |
| valuation_pe | value | robust-watch | +0.02ns → +0.25 → +0.17ns | YES, strengthens (+0.337 p=3e-5) | no (p=0.0023) |
| layer4_weighted_score (+rank_in_day) | ordering | suggestive | +0.15 → +0.20 → +0.27 | car_10 yes (p=0.022); car_5 marginal (p=0.083) | no |
| catalyst_strength | news | suggestive | −0.01ns → +0.14 → +0.24 | YES (+0.158 p=0.007) | no |
| fcff_yield_sector_percentile | value | suggestive | +0.14 → +0.11ns → +0.17ns | YES (p=0.041) | no |
| technical_pct_off_52w_low | tech | suggestive | −0.20 → −0.23 → −0.18 | NO at car_5 (p=0.11); marginal car_10 | no (raw clears, partial fails) |
| joint_flag ATR×(52w_low∨ROIC) | composite | suggestive | MW p<1e-6 both | partially (ROIC leg adds nothing) | n/a (in-sample composite) |
| industry_name (loser arm: semis/comms) | sector | suggestive | KW 3.8e-4 → 1.7e-11 | YES | raw yes, verdict caps it |
| insider_score_usd (net-selling fades) | insider | suggestive | +0.11ns → +0.22 → +0.29 | car_10 yes; car_5 no | no (N≈5 events) |
| valuation_financials_age_days | value | suggestive (diagnostic) | +0.11 → +0.17 → −0.02flip | car_5 NO, car_10 marginal | no |
| buffett_quality_score | experts | suggestive (no edge beyond ROIC) | +0.38 → +0.20ns → n/a | marginal (p=0.072); collapses under roic partial | no |
| roic_pct (quality cluster) | value | **spurious** (ATR proxy) | +0.22 → +0.11ns → +0.29 | **NO** (r=−0.035 p=0.58; car_10 partial negative) | no |
| buffett_roic_3y_avg | experts | spurious (ticker pseudo-replication) | +0.46 → +0.36 → n/a | row-level yes / ticker-level NO (p=0.56) | no |
| buffett_margin_of_safety (+owner_earnings) | experts | spurious (2-3 sector events) | +0.03ns → −0.68 → n/a | yes but on invalid N | no |
| expert_spread | experts | spurious (one June week) | +0.06ns → +0.32 → n/a | yes but LODO/LOTO fail | no |
| oneil_earnings_growth_yoy | experts | spurious (6 days) | +0.09ns → −0.36 → n/a | yes but day-drop kills | no |
| catalyst_event_type | news | spurious (space episode) | KW 0.0014 / 0.0007 | level means ARE ATR-stratified | no |
| theme (omnibus) | theme | spurious content | KW 1.8e-11 both | yes mechanically; ticker-dedup kills | raw yes, content no |
| valuation_ps / ev_rev | value | spurious | −0.14 → −0.14 → −0.09ns | NO (collapses) | no |
| technical_ma200_distance_pct | tech | spurious (space cluster) | −0.12 → −0.19 → −0.15ns | yes (wrong confound) | no |
| magic_formula_health_pass | value | spurious (ATR proxy) | MW 0.014 → 0.98 | NO | no |
| peer_cohort_level | meta | spurious (ATR proxy) | KW 0.020 / 0.011 | NO | no |
| market_cap terciles | size | spurious (binning noise) | KW 0.16 / 0.027 | incoherent | no |
| sector_name | sector | spurious | KW 0.023 / 0.73 | sign flips per horizon | no |
| technical_volume_zscore | tech | spurious | −0.12 → −0.05ns → +0.02flip | NO | no |
| rank_in_day (standalone) | ordering | spurious (fold into layer4) | −0.06ns → −0.15 | NO pooled | no |
| buffett_management_candor | experts | spurious/null | KW 0.060 / 0.95 | degrades | no |

**Clean nulls (recorded, don't re-test):** catalyst_confidence, llm_confidence, staleness_days (3rd+4th confirmation), roe_pct, fcff_yield_pct raw, valuation_ev_ebitda, market_cap continuous, oneil_score (car_5/10), oneil_rs_approx, buffett moat_type/trend/data_coverage, insider_score_sector_percentile, cohort_size_in_day, stop_distance/gross_weight geometry, n_gates_failed, deep_drawdown_reversal, extra_themes_any. **Untestable this run:** novelty_rank/score, selection_score, atr_penalty (26 rows / 3 days), market_state_* (N=0 on matured rows), pass_tenk, brief plan constants (zero variance).

## 5. Versus the June memo

**HELD:** ATR as the anchor separator (deeper N, now car_20 too); catalyst_strength as the only clean slow-burn news signal (car_10 rho +0.14 vs June ~+0.18, still car_5-null); layer4 positive ordering direction; the space-cluster doctrine (event_type/P/S/news-kind re-confirmed spurious by the same mechanism); staleness null.

**STRENGTHENED / NEW:**
- `technical_ma50_distance_pct` (+rsi): June footnote → **robust, Bonferroni-clear, ATR-independent** — the best candidate for a genuine second axis (silent at 5d, −0.26/−0.27 at 10-20d, partial unchanged).
- `n_gates_passed`: June "dissolves into ATR under OLS" → now survives the ATR partial on both horizons at 2 more weeks of N. Upgraded to robust; still half-ATR at car_5, and it is really "press-gate passers fade".
- FCF axis (`valuation_fcf_margin` robust, ATR-orthogonal, confirmed on market_excess across 37 days) — revises June's "value does not separate" to "**multiples don't; FCF profitability does**".
- `valuation_pe` anti-value tilt at car_10 that *strengthens* under the ATR partial — new, watch-only.
- Industry loser arm (semis/comms fade, 12-17 days, ATR-orthogonal) — new suggestive.

**DIED:** `roic_pct` and the whole quality cluster (probe (a): **NO** — fully absorbed by ATR, residual sign flips negative at car_10); `technical_pct_off_52w_low` downgraded (ATR-loaded, car_5 partial fails); `expert_spread` (again); `valuation_composite_sector_percentile`; market-cap tercile blip.

**Open-probe answers:**
- **(a) ROIC vs ATR:** collapses — fold quality into the ATR/extension doctrine. Only admissible future form: pre-registered residual-ROIC×ATR joint test (residual currently points the wrong way at 10d).
- **(b) Experts first verdict: repackaging, not signal.** Buffett quant = ROIC/ATR repackaging + 30-ticker pseudo-replication (ticker-collapsed null); qual pillars null; oneil_score clean null (its market_excess −0.24 is the extension fade); expert_spread/mos = one-June-week artifacts. Nothing to act on; real test ~2026-09 at ticker-episode unit.
- **(c) layer4 forward-track: held.** Direction confirmed and strengthened raw (car_10 p=0.0006, car_20 +0.27), within-day robust, but ATR-attenuated (car_5 partial p=0.083) and non-monotone — it works as a *low-score avoid filter* (score 1-2 fade hard), not a top-pick ranker; not Bonferroni-clear. rank_in_day is entirely its expression; selection_score incremental test still pending (3 days of data).

## 6. New doctrine: the ticker-episode is the unit of independence

231/523 rows repeat a ticker within 3 days of its prior appearance (81 tickers; DFIN spans 18 brief-days, WK/BAH 15); adjacent-day rows for the same ticker often carry *literally identical* outcome values (same maturation window). Consequence: row-level significance, and even day-level LODO/leave-one-theme-out, overstate evidence — several findings this run were day-check survivors that died only when collapsed to one observation per ticker(-episode): theme omnibus content, `buffett_roic_3y_avg`, `insider_score_usd`, `buffett_margin_of_safety`.

**Doctrine (extends June's space-cluster rule):** before trusting any EDGE signal, dedupe same-ticker episodes (or cluster errors by ticker-episode); LODO/LOTO alone is insufficient. Effective N is ticker-episodes, below the brief-day count.

## 7. Baseline honesty

Plannable baselines: **car_5 −1.67%** (345/31d, 41% positive), **car_10 −2.30%** (290/23d), **car_20 −3.57%** (125/9d) — negative and deepening with horizon, unchanged from June. Nothing trustworthy is absolutely positive: the best cells are mid-ATR car_10 +2.7%, low-ATR +0.8%, high-FCF-margin +0.65%, high-PE +0.63% — all ≤3pp, in-sample, day-clustered. Apparently-positive cohorts are artifacts or clusters: NO_FILL +5.0/+8.7% (dip-buy selection-on-outcome), non-plannable +5.2% (no-trade-setup rows), pharma/bio +7.7/+21% (one 5-6-day obesity episode). **Everything remains "fades less", not "makes money"; the screener's edge problem is still selection-level and unsolved.**

## 8. Next probes (forward-track / telemetry only)

1. **Simplify the joint loser flag: forward-log `ATR-hi × pct_off_52w_low-hi` WITHOUT the ROIC leg** (verifier showed ROIC adds nothing beyond ATR and drags a coverage hole). Telemetry-only; pre-register the interaction before any ordering use.
2. **Promote the MA50-extension axis (ma50_distance/rsi) to the tracked-separator list next to ATR** — re-verify at ~40 car_10 days (~early-Aug) together with the press-gate residual (is `pass_press` still non-ATR then, and is the press gate mis-specified?).
3. **One pre-registered fundamentals hypothesis for the August re-run: FCF-margin** (representative of cluster #5), plus scheduled re-tests: catalyst_strength at ~40 car_10 days, selection_score incremental-vs-layer4 (~2 weeks), experts at **ticker-episode unit** ~2026-09. Adopt the §6 doctrine into the protocol now.

## 9. Caveats

- **Clustered effective N:** 31/23/9 distinct brief-days at car_5/10/20; worse, 179 unique tickers over 523 rows with heavy adjacent-day repetition — effective independent N is ticker-episodes, below day counts.
- **In-sample, short window:** most fundamentals populate only from 2026-05-27; experts from 06-11 (one June-July window, single config versions); car_20 (9 days) and market_excess (variable window) are context, not confirmation.
- **Negative baseline:** all findings are relative fade-avoidance tilts inside a book that loses to SPY at every horizon; none is evidence of positive expected return.
- **Multiplicity:** 284 tests, α=1.76e-4; only ATR, MA50-extension and press-gate clear it with robust verification. Everything else is exploratory and must re-clear on forward data.

## 10. Reproduce

```bash
for d in population_ladders thematic_briefs grouped_daily_history; do
  rsync -a "jacoren@vault.kamilpajak.pl:.alphalens/$d/" "$HOME/.alphalens/$d/"
done
# rebuild ~/.alphalens/diagnostics/signal_panel.parquet: join the two stores on
# (brief_date, ticker) and stamp car_5/10/20 via diagnostics.{edge_stores,
# fixed_horizon, anchor} (prior-close anchor, SPY leg from the grouped store),
# then re-run the Workflow against the fixed panel path.
```

All figures computed locally on the rsync'd VPS stores; no tracked code touched.
