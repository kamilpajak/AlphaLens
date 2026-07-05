# Design Memo: Equity-Calibrated Market-State Context Signal (`market_state`)

**Status:** DRAFT — 2026-07-05 (reworked after adversarial review; see §7)
**Author:** quant research (solo)
**Type:** display-only context signal + forward-validation plan
**Origin:** a crypto trading bot derives a discrete "H4 Market State" from H4 candle structure (price vs SMA50/SMA200, MA slope, ATR% context, distance-to-SMA200, range compression). This memo ports that idea to equities.
**Related:** expert-panel epic #541 (buffett/oneil pattern), insider signal v2, EDGE selection-attribution.
**Doctrine anchors:** forward-log-then-validate, pre-registration + Bonferroni, N≥30 maturation gate, LLM-training-cutoff blindness (all numerics from authoritative sources, pre-computed).

---

## 0. TL;DR and framing

Port the crypto "market state" heuristic to equities as an **index-level (SPY), daily-bar, discrete regime label**. Re-calibrate the crypto hard thresholds so the vol axis is a **quantile of the index's own trailing realized vol** (self-normalizing across regimes), add a **VIX regime** leg, and ship it **display-only** — a context banner on the daily brief, held out of the brief sort exactly like `buffett_quality_score`/`oneil_score` (PR-6 sort allowlist). It carries a `market_state_config_version` poolability key so any deferred EDGE study can partition rows.

**This is a heuristic trend/volatility label, not an estimated regime (HMM).** The prior research pass (Perplexity, 2026-07-05) confirmed: the *features* are literature-aligned, the *methodology* (hard thresholds) is a heuristic that needs equity-specific calibration and forward validation. We ship the heuristic (interpretable, zero training risk); the HMM stays an explicitly-registered future alternative, not v1.

**Three claims, kept strictly separate:**
- **H-context (ships now, no Bonferroni cost):** the label is descriptive metadata on the brief. Rendering it makes *no statistical claim*.
- **H-A (regime predicts forward INDEX behavior):** a pre-registered hypothesis, testable purely on index data. Its main value is a diagnostic **screen on H-B** + dropping the display `unvalidated` tag — **not** a behavior change (see §4.6).
- **H-B (regime conditions the tool's own selection edge):** a *separate* pre-registered hypothesis that could eventually change behavior. **Decoupled (D4 resolved 2026-07-05):** tested on a **sector-relative** outcome (`sector_excess_return` = candidate minus its sector ETF), not the SPY-subtracted metric — see §4.2.

Nothing feeds selection until H-B is both decoupled and passes forward-out-of-sample AND pays its Bonferroni cost.

> **Adversarial-review outcome (2026-07-05, zen-style skeptic in-workflow): NEEDS_REWORK on the science, plumbing sound.** This memo is the reworked version. §7 records every finding and how it was resolved. **FATAL-1 resolved 2026-07-05 (D4, §4.2): H-B decoupled via a sector-relative outcome.** PR-0..PR-3 (display-only) may proceed; PR-4 may register H-A + H-B but locks only after the sector-excess enrichment (PR-2b) is logging forward + the pre-compute adversarial review.

---

## 1. State taxonomy (reworked to 4 states + `unknown`)

### 1.1 The grid: trend × volatility

A **2-axis** classifier — **trend** {up, down, neutral} × **volatility** {low, high} — collapsing to **4 named states**, plus a first-class `unknown`.

| State | Trend | Vol | Meaning |
|---|---|---|---|
| `bull_quiet` | up | low | uptrend, calm — "risk-on grind" |
| `bull_volatile` | up | high | uptrend but choppy — late-cycle / news-driven |
| `bear_volatile` | down | high | downtrend + stress — the drawdown / VIX-spike regime |
| `bear_quiet` | down | low | slow orderly bleed (rare) |
| `neutral × *` | neutral | — | folds to the nearest of the four by `dist200` sign (see §1.3) |
| `unknown` | — | — | inputs missing (stale VIX cache / insufficient bars). First-class token, never silently mapped. |

**Change from the first draft (per review, transfer-error + overfitting):** `range_compression` is **dropped as a top-level state**. The squeeze (BB-in-KC) is a crypto-origin construct; "compression precedes a big move" is an *untested carryover hypothesis at the index level*. Keeping it as a state would bake an unvalidated crypto prior into the taxonomy with <1y of data to check it. Instead the squeeze is a **raw boolean telemetry flag** (`market_state_squeeze_on`) stamped alongside — testable later, never a state.

### 1.2 Why 2-axis (not the crypto flat 4-label ladder)
- **Orthogonality = testability.** Each axis is independently calibrated and independently falsifiable forward (can test "does the vol axis alone separate forward drawdown?" without trend contamination).
- **Equity vol asymmetry** — *asserted as a prior, not validated*: down-moves cluster with high vol, up-moves with low vol. The grid isolates `bear_volatile` (the drawdown state) from `bull_quiet`. If <1y forward data shows two cells are statistically indistinguishable, collapse toward 3 states.
- **Reuses existing regime vocabulary** — `alphalens_research/attribution/regime.py::classify_regime` emits {bull, flat, bear}; the trend axis shares it so regime-breakdown tooling composes.

### 1.3 Precise decision rule (all params frozen a-priori — see §2)

`asof` = brief date; index = **SPY** (single pre-committed index — see §6 D1). Bars are daily; all windows in trading days.

**Trend axis** — `trend ∈ {up, down, neutral}`:
```
c        = SPY close at asof
sma50    = SMA(close, 50)
sma200   = SMA(close, 200)
slope50  = (sma50 − sma50 SLOPE_WIN days ago) / sma50
dist200  = (c − sma200) / sma200

up      ⟺ c > sma200 AND sma50 > sma200 AND slope50 > +SLOPE_EPS
down    ⟺ c < sma200 AND sma50 < sma200 AND slope50 < −SLOPE_EPS
neutral ⟺ otherwise (cross disagreement, |slope50| ≤ SLOPE_EPS, or |dist200| ≤ DIST_FLAT_BAND)
```

**Volatility axis** — `vol ∈ {low, high}`:
```
atr_pct    = ATR(14) / close
atr_pct_q  = rolling_quantile_rank(atr_pct, ATR_QUANTILE_LOOKBACK)   # in [0,1]
vix_regime = classify_vix(vix_asof)                                  # low/mid/high (reuse feedback/regime.py)

high ⟺ atr_pct_q ≥ ATR_HIGH_Q  OR  vix_regime == "high"
low  ⟺ otherwise
```
The **OR combiner is a single, pre-committed a-priori choice** (not "let forward data pick" — see §7 HIGH-4). It is labelled a crypto-origin hypothesis: either realized (ATR%) or implied (VIX) elevation flips to volatile. To change it to AND / 2-of-3 is a *new* `config_version` and a *new* pre-registered test, counted in Bonferroni.

**Neutral fold + state map:**
```
neutral → nearest of {bull_*, bear_*} by sign(dist200), keeping the vol axis
(up,   low)  → bull_quiet
(up,   high) → bull_volatile
(down, high) → bear_volatile
(down, low)  → bear_quiet
missing input → unknown
```

Store the label as a **string** (`market_state`) plus the raw continuous drivers (`market_state_atr_pct`, `market_state_atr_pct_q`, `market_state_dist200`, `market_state_vix`, `market_state_vix_decile`, `market_state_squeeze_on`). The forward study correlates the **continuous** drivers, never only the bucket — same discipline as `disagreement.py`.

---

## 2. Equity calibration — all thresholds FROZEN a-priori, UNVALIDATED

**Critical rework (§7 FATAL-2 + MEDIUM-overfitting):** the usable equity panel is **<1 year** (grouped store starts 2024-09-11 ≈ 446 sessions; SMA200 + 252d ATR quantile burn ~1y of warmup). There is **no in-sample window to fit or firebreak against.** Therefore **every threshold below is frozen at a literature/crypto prior with NO fitting.** They are honest priors carrying **zero validation**. The `-UNVALIDATED` suffix is mandatory in the config_version. The firebreak in §4.3 is consequently a **no-op** (nothing is fit, so nothing can leak) — this is the honest posture given the data.

### 2.1 Index and bars
- **Index: SPY only** (single pre-committed choice, §6 D1). QQQ is **not** computed as a second tested series in v1 (would double the Bonferroni family). If QQQ is wanted later it is a new pre-registered class entry.
- Source: `~/.alphalens/grouped_daily_history/<date>.parquet` (split-adjusted, `adjusted=true`) via `rs_history.read_grouped_day` — the same disk-only store the O'Neil R term reads at score stage. **No new network.**

### 2.2 Frozen hyperparameter manifest (literature priors)
| Param | Frozen value | Origin / note |
|---|---|---|
| `SMA_FAST` / `SMA_SLOW` | 50 / 200 | standard equity trend filter (golden/death cross) |
| `SLOPE_WIN` | 20 | slope measured over ~1 month |
| `SLOPE_EPS` | 0 (sign-only) | deadband; sign-only default |
| `DIST_FLAT_BAND` | ±0.02 | ±2% of price → lean neutral |
| `ATR_WIN` | 14 | Wilder ATR |
| `ATR_QUANTILE_LOOKBACK` | 252 | ~1y window the current ATR% is ranked against |
| `ATR_HIGH_Q` | 0.70 | 70th pct → high-vol (prior, not fit) |
| `VIX_LOW` / `VIX_HIGH` | 15 / 25 | reuse `feedback/regime.py::classify_vix` buckets |
| `BB_WIN`/`BB_K`, `KC_WIN`/`KC_MULT` | 20/2.0, 20/1.5 | TTM squeeze — **telemetry flag only**, not a state |

Proposed token: `MARKET_STATE_CONFIG_VERSION = "mstate-v1-spy-sma50x200-atrq70-vix15_25-UNVALIDATED"`. Bump on ANY parameter change (mirrors `disagreement.PANEL_CONFIG_VERSION`, `selection_score.SCORER_CONFIG_VERSION`).

### 2.3 ATR% as a realized-vol quantile (the key equity re-calibration)
The crypto bot used fixed ATR% cutoffs; equity ATR% has a different scale and drifts across decades, so the threshold is a **quantile of the index's own trailing realized vol** (`rolling_quantile_rank`), reusing the exact `data/macro/signals.py::vix_decile` idiom (rolling rank / length) on the ATR% series. This self-normalizes across regimes.

### 2.4 VIX leg
VIX from **FRED `VIXCLS`** via `FREDClient.fetch_series('VIXCLS')` (cached `~/.alphalens/macro/FRED_VIXCLS.parquet`) — **never** yfinance `^VIX` (would trip `test_no_raw_yfinance_http`; FRED is the canonical macro client). `classify_vix` for the bucket, `vix_decile` stamped raw.

### 2.5 Breadth — DROPPED from v1 as a signal input (display annotation only, excluded from tests)
**Rework (§7 HIGH-5):** the augmented PIT loader cited in the first draft (`load_sp1500_pit_for_date_augmented(..., include_delisted=True)`) **does not exist** — only `load_sp1500_pit_for_date` / `load_sp1500_pit_union` exist; the augmented loader is "to implement alongside paradigm #16" (CLAUDE.md). So v1 breadth would use the **survivorship-biased current-roster snapshot** (roster rolls to survivors → breadth reads structurally high on older dates → spurious correlation with survivor-inflated forward index moves).

Decision: **breadth is NOT an axis input and NOT a tested covariate in v1.** If shown at all it is a display-only annotation carrying an explicit `breadth_survivorship_biased=True` flag, and it is **excluded from every H-A / H-B correlation**. Breadth-as-signal is blocked until the delisted-augmented loader lands.

---

## 3. Integration plan (minimal touch-points — buffett/oneil pattern, verified sound)

The signal is computed **once per asof** (index-level) and **broadcast** to every candidate row (exploration-confirmed Option A). Zero Django migration beyond adding fields; mirrors how `disagreement.enrich` stamps `panel_config_version` on every row.

### 3.1 Pipeline: new module + one call site
**New file** `apps/alphalens-pipeline/alphalens_pipeline/market/market_state.py` (new `market/` package, infra side per ADR 0011):
- `MARKET_STATE_CONFIG_VERSION` constant + `MARKET_STATE_COLUMNS: tuple[str,...]`.
- `classify(asof, *, grouped_store, fred_client) -> dict` — pure classification; unit-tested at every axis boundary.
- `enrich(frame, *, asof, ...) -> pd.DataFrame` — copies the empty-frame + broadcast idiom from `disagreement.enrich` (lines 71–101). **Explicit dtypes for all ~9 columns** on the zero-row branch (`object` for the 2 string cols `market_state`/`market_state_config_version`, `float64` for the rest, `boolean` for `squeeze_on`) — see §7 LOW-integration. Two missing-value conventions documented: `'unknown'` string for the label, `NaN` for floats. Stamp `config_version` unconditionally.

**Call site:** `alphalens_cli/commands/thematic.py` (~line 500), right after `enriched = disagreement.enrich(enriched)`:
```python
from alphalens_pipeline.market import market_state
enriched = market_state.enrich(enriched, asof=target, grouped_store=..., fred_client=FREDClient.from_env())
```
Inject store + FRED client via DI so tests pass fakes (canonical-client doctrine; enforced by `test_no_raw_{polygon,fred,yfinance}_http`).

### 3.2 No-selection-leak — architectural guard, not just a convention
`market_state` must NOT feed `layer4_weighted_score`, `selection_score`, or any gate. Two guards (§7 MEDIUM-scope):
1. Sort-allowlist test (`_NON_EXPERT_SORT_ALLOWLIST` in `test_sort_and_dedup.py`) pins it out of the sort chain — the PR-6 pattern.
2. **NEW positive-control test** that FAILS if any `market_state*` column appears as an *input* to `layer4_weighted_score` / `selection_score` (not just the final sort key), mirroring the `test_no_raw_*_http` anti-rot tests. This makes "display-only" an enforced boundary, not a convention that a future `scorer_config` bump can silently cross.

### 3.3 Django: flat fields + contract registration
`Brief` model (near `scorer_config_version`): `market_state` (CharField), `market_state_config_version` (CharField), + one nullable FloatField per telemetry column, + `market_state_squeeze_on` (nullable BooleanField). Register **every** column in `LEGACY_CONTRACT_COLUMNS` (`test_schema_parity.py`) — `test_no_orphan_brief_fields` is the guard. `makemigrations briefs` → nullable-on-populated-table (safe). **No coerce change** (`parquet.py::_coerce_for_field` already dispatches Float/Char/Bool). `market_state` is a **flat field, NOT inside `expert_assessments`** — index-level context, not a per-ticker lens; keeps it out of the blob-corruption trap in `_row_to_brief`.

### 3.4 SPA: display-only context banner
Serializer auto-exposes (`exclude=("pk",)`). `types.ts` `Candidate`: add `market_state?: string` + telemetry. Render a **brief-level banner ONCE** at the top of the day view (not per-card — it's index-level), e.g. tone-neutral chip `market · bull-quiet` with a `JargonTip` glossary entry stating **"context, not a signal · unvalidated"**. Optional drawer "Market Context" section with the axis breakdown (trend / vol / VIX) + `unvalidated · context-only` label. `Number.isFinite` / optional-chaining shims on all reads (PR-8b pattern). Tone map display-only: `bull_quiet`→green, `bull_volatile`→amber, `bear_volatile`→red, `bear_quiet`→muted-red, `unknown`→muted.

---

## 4. Forward-validation plan (reworked to be executable & honest)

### 4.1 Log now (Phase 1, ships with PR-1)
From the first nightly `alphalens-thematic-build` after deploy, every brief parquet carries `market_state` + telemetry + `config_version`. The population ladder monitor already logs, per (brief_date, ticker), `forward_return` and `market_excess_return` over ~42 sessions (`feedback/population_ladder_monitor.py` → `edge/models.py::LadderOutcome`). `market_state` is a **date-level covariate** joined to those outcomes. Forward-only population (older parquets predate the new image), same as expert-panel. The clock starts today — that is the entire value of log-now.

### 4.2 Two pre-registered hypotheses — reworked

Registered in `docs/research/preregistration/ledger.json` under a new class `market_regime_signals_2026_07` **before any test**; `alphalens preregister threshold` prints the corrected critical value.

**Bonferroni family is pre-committed and fully enumerated (§7 HIGH-4).** v1 commits a-priori to **ONE index (SPY), ONE trend definition (SMA-cross), ONE vol combiner (OR)**. No "log both and let forward data pick." H-A's family is **3 metrics × 3 horizons = 9** tests; the HMM alternative (§6 D2), QQQ, alternate trend defs, and alternate combiners are **NOT in v1's family** — each is a future class entry that pays its own cost when registered. The class denominator is stated in the ledger before compute.

**H-A — does the label mean anything for the INDEX?**
- **H0:** the forward k-day distribution of SPY {return, max-drawdown, realized-vol} is identical across the 4 states.
- **Metric:** per state, forward k-day (k ∈ {5, 10, 21}) index return, realized drawdown, realized vol.
- **Test:** cluster-robust across states (see effective-N below), effect = median spread best-vs-worst.
- **Executable purely on index data** — no ladder needed; can run the moment episode-N per state is sufficient.

**H-B — does regime condition the tool's own selection edge? — DECOUPLED (D4 resolved 2026-07-05: peer/sector benchmark)**
- **The confound:** `market_excess_return = forward_return − SPY_window_return` (per `feedback/benchmark_excess.py`) is **already SPY-subtracted**, and `market_state` is computed **FROM SPY**. So "does SPY-relative edge differ across states defined by where SPY is" is entangled with the mechanical SPY-path↔MA/ATR-bucket relationship. A Kruskal result here would be partly a SPY-autocorrelation artifact, and passing it could wrongly green-light selection use.
- **Resolution — sector-relative outcome (option a, chosen):** H-B is tested on a NEW metric `sector_excess_return = candidate_forward_return − sector_etf_window_return` over the **same** ~42-session window and the same VWAP-arrival/last-bar-exit convention as `benchmark_excess`, where the sector ETF is picked by the candidate's own SIC/FF48 classification (**already stamped on the Brief**: `industry_id` SIC4 / `sector_name` / `peer_cohort_level`, from `data/fundamentals/sic_index.parquet`) via a static SIC→SPDR-select-sector map (XLK/XLE/XLF/XLV/XLY/XLP/XLI/XLB/XLU/XLRE/XLC, + SMH for semis). The outcome benchmark is now the candidate's **sector — a different series** from the SPY-derived label, so a positive H-B can no longer be a pure SPY-autocorrelation artifact. `market_excess_return` (SPY) is KEPT for other uses; sector-excess is the H-B estimand, carrying an `outcome_benchmark_version = "sector-etf-v1-sic4"` poolability key.
- **Feasibility (verified in-repo):** zero new data/vendor — sector classification already on the Brief; sector-ETF daily+minute bars already in the Polygon grouped store (same client as SPY). Only gap: the static SIC→ETF map (small dict/parquet). Implementation mirrors `benchmark_excess.enrich_store_with_benchmark_excess()`.
- **Honest residual (reduction, not elimination):** sector ETFs still load on the market factor (β≈1 to SPY) and `market_state` is a market-level label, so a second-order residual correlation remains. This breaks the *mechanical same-series* entanglement, not all correlation. A fully **β-neutral** outcome (regress sector-excess on SPY forward, test the residual) or an **equal-weight peer-cohort** benchmark (reuse `thematic/screening/sector_peers.py`) is a **registered future refinement**, run only if H-B shows signal on the sector-excess estimand.
- **Unresolvable sector** (`peer_cohort_level == "thin"` / unmapped SIC) → `sector_excess_return = None`, **excluded** from H-B; never fall back to SPY (that would reintroduce the confound).
- **Estimand + test:** cohort mean `sector_excess_return` grouped by `market_state`, restricted to the LLM-proposal conditional support (conditional-support OPE per `feedback_ledger_counterfactual_design_2026_06_02.md` — never raw importance weighting), episode-clustered inference + episode-N gate (§4.3). H-B is a **separate Bonferroni class entry**. Regime-as-filter (feeding selection) still gated behind H-B forward-OOS pass + Bonferroni; passing H-A does **not** license selection. **Log forward NOW** — the sector-excess enrichment ships EARLY (PR-2b) so H-B forward data uses the clean estimand from day one.

### 4.3 Effective-N = independent regime EPISODES, not calendar days (§7 HIGH-3)
Daily labels are heavily autocorrelated: a 200d-MA trend + 252d-ATR-quantile vol state persists for weeks-to-months, so 30 consecutive `bull_quiet` days ≈ 1–2 independent episodes, not 30 draws. Kruskal-Wallis assumes independent observations; feeding it autocorrelated daily labels inflates the effective sample and the rejection rate.

- **N is redefined as the count of independent regime episodes** (contiguous same-state runs). Gate: **≥30 episodes per state** for a per-state number (likely **years** away, stated honestly), ≥50 for a cross-state claim.
- **Inference is cluster-robust**: cluster by regime episode (block bootstrap or episode-level test), never row-level Kruskal on daily labels.
- Given <1y usable history and states that flip a handful of times, most states will have **single-digit** episodes for a long time. `bear_quiet` may never reach the gate — that is an honest "insufficient data," never forced. **First look ≠ verdict.**

### 4.4 Firebreak + freeze
Because §2 freezes all thresholds a-priori with **no fitting**, there is no in-sample fit to firebreak against — the firebreak is a no-op by construction (the honest consequence of <1y data). Any parameter change ⇒ new `config_version` ⇒ the analyst partitions old vs new rows (poolability key), never pools across versions.

### 4.5 Harness
`apps/alphalens-research/scripts/analyze_market_state_edge.py` (research side, Mac/runpod, not hot path): joins `LadderOutcome` / population-ladder parquets to per-date `market_state`, runs the **episode-clustered** tests for H-A (and H-B once decoupled), enforces the episode-N gate in output (no numbers below 30 episodes; "early/high-variance" 30–100; full inference ≥100 — the `feedback_edge_dashboard` §3.2 rule applied at episode level), prints Bonferroni-adjusted p-values vs the ledger threshold. An EDGE-dashboard `?group_by=market_state` slice is deferred (needs Brief⋈LadderOutcome join, same deferral as the `/edge` scorer-version chip).

### 4.6 What a validated H-A licenses (and does not)
A passing H-A means only that the regime label has real predictive content **for the INDEX** (SPY forward return / drawdown / vol separate across the 4 states). It is a statement about the market, **not** about the tool's picks. Be explicit about what it does and does not earn:

**H-A licenses:**
- **Dropping the `unvalidated` qualifier on the context banner** — the display label graduates from "descriptive, unproven" to "validated market context." Still display-only; no sort, no gate.
- **A diagnostic screen on H-B — this is H-A's main value.** If H-A **fails** (the label does not even separate *index* outcomes), the taxonomy is noise: a red flag to collapse states (D3) or drop the signal, and evidence that maturing H-B is probably not worth the wait. If H-A **passes**, the label is real and H-B is worth waiting for. H-A is thus a cheap *pre-screen on H-B*, more than a feature in itself.

**H-A does NOT license (each needs its own pre-registered hypothesis):**
- **Any selection / ordering change** — that is H-B (regime conditions the tool's OWN edge). A real *index*-regime signal says nothing about whether the tool's picks do better or worse in that regime.
- **Market-timing prescriptions on the card** ("risk-off — reduce exposure", "wait for `bull_quiet`"). The tool is augmentation, not a market-timing engine (project doctrine: regime timing as alpha is a *separate* hypothesis paying its own Bonferroni; the group decides, the tool informs). At most the banner states the regime factually and non-prescriptively.
- **Confidence from a long-history OOS pass** — per §8.3, non-stationarity makes a 20–30y H-A firebreak weakly interpretable; a pass is suggestive, not a license to act.

**Consequence for sequencing (ties to §8.4):** because H-A is **near-inert for v1 behavior** — its payoff is a display caveat + a diagnostic screen — validating it is **not urgent**. The backfill to test H-A can wait until H-B forward data is accruing anyway (~2026-09+). Ship the free FATAL-1 work (done, D4) and the display-only PRs (PR-0..3) now; spend backfill effort only when the H-B clock makes H-A worth cashing in as its screen.

---

## 5. Scope / phasing (PR breakdown)

Each PR: small, TDD (red→green), no selection use, follows config_version + poolability conventions. Zen pre-merge review (deepseek-v4-pro, thinking=high) on non-trivial ones.

- **PR-0 — pure primitives.** Stateless pure functions: `atr_pct`, `rolling_quantile_rank` (reuse `vix_decile` idiom), SMA/EMA slope, BB-in-KC squeeze boolean — each with inline-fixture unit tests. No store, no network, no stamping.
- **PR-1 — signal + stamp.** `market/market_state.py`: `classify` + `enrich` (broadcast idiom from `disagreement.enrich`, explicit dtypes for all columns), `MARKET_STATE_CONFIG_VERSION`, `MARKET_STATE_COLUMNS`. Wire the one call site. Boundary unit tests at every threshold (vix=14.99 vs 15.01; atr_pct_q at ATR_HIGH_Q±ε; cross disagreement→neutral; missing VIX→unknown). **Sort-allowlist test + the new no-selection-input positive-control test** (§3.2). DI clients (no raw HTTP).
- **PR-2 — Django fields.** Add `Brief` fields, register in `LEGACY_CONTRACT_COLUMNS`, `makemigrations`, parquet round-trip ingest test (incl. zero-row + all-`unknown` schema cases). No serializer / coerce change.
- **PR-2b — sector-excess outcome enrichment (EDGE ledger, ships EARLY).** New enrichment pass mirroring `benchmark_excess.enrich_store_with_benchmark_excess()`: static SIC→SPDR-sector map + `sector_excess_return` / `sector_etf_ticker` / `sector_etf_window_return` columns + `outcome_benchmark_version` poolability key + Django `LadderOutcome` fields. **Independent of the market_state signal**; ships early so H-B forward data uses the decoupled estimand from day one (D4). TDD; excludes unresolvable-sector rows (no SPY fallback). Zero new vendor (sector on Brief, ETF bars in grouped store).
- **PR-3 — SPA display.** `types.ts`; brief-level context banner + `JargonTip` ("context, not a signal · unvalidated"); optional drawer section; tone map. Held out of every sort. Storybook story (dev-only). CF Pages auto-deploy. **PR-0..3 ship H-context and may proceed now.**
- **PR-4 — pre-registration + harness. GATED.** Ledger entry for `market_regime_signals_2026_07` (H-A with the fully-enumerated 9-test family; **H-B registrable now — D4 decoupled via `sector_excess_return` (§4.2); PR-2b must be logging it forward first**); `alphalens preregister threshold` recorded; `scripts/analyze_market_state_edge.py` with the episode-N gate + cluster-robust test + Bonferroni. **Adversarial review (zen + Perplexity) of the locked memo before any test compute.** Memo status → LOCKED only when §4 is executable.

Deploy operator-owned (VPS pipeline image rebuild + Django `compose pull && up -d` auto-migrate + `rebuild_briefs_cache --force` + CF Pages). Forward-only, same as expert-panel.

---

## 6. Open questions / decision points

- **D1 — index.** **Resolved for v1: SPY only** (to keep the Bonferroni family honest). QQQ as a second tested series is a future pre-registered entry, not v1. (The tool's candidates are theme/tech-tilted so QQQ *may* be more relevant — a testable future claim, not a v1 assumption.)
- **D2 — heuristic vs HMM.** **Heuristic first** (interpretable, zero overfit risk, ships now, per-axis testable). Gaussian-HMM-on-features is an explicit future alternative in the same class — pays its own Bonferroni cost and must beat the heuristic on the SAME forward window before replacing it.
- **D3 — state count.** **4** (trend×vol grid) + `unknown`. `range_compression` dropped to a telemetry flag (§1.1). Collapse toward 3 only if forward data shows two cells indistinguishable.
- **D4 — H-B decoupling method. RESOLVED 2026-07-05: (a) peer/sector.** H-B outcome = `sector_excess_return` (candidate − its SPDR sector ETF), not the SPY-subtracted metric. Chosen over residualization (b) because it needs no fitted model on <1y data and is cleaner to explain. Feasible from existing data (sector already on the Brief via SIC/FF48 `sic_index.parquet`; sector-ETF bars already in the grouped store; only a static SIC→ETF map is new). Residual market-β leakage acknowledged (§4.2); a β-neutral variant and an equal-weight peer-cohort variant (reuse `sector_peers.py`) are registered future refinements. Unblocks H-B: PR-2b logs it forward, PR-4 registers it.
- **D5 — vol combiner.** **Resolved for v1: OR**, pre-committed a-priori and labelled crypto-origin. AND / 2-of-3 are future config_versions, each a new test.
- **D6 — ship display-only now?** **Yes.** Rendering a descriptive, `unvalidated`-labelled context banner makes no statistical claim, costs no Bonferroni, and starts the forward log today. Exact buffett/oneil/insider posture.

---

## 7. Adversarial-review resolution log (2026-07-05)

In-workflow skeptic verdict: **NEEDS_REWORK** — "engineering-integration half is sound; the problem is the science." Resolutions:

| # | Sev | Finding | Resolution in this memo |
|---|---|---|---|
| 1 | FATAL | H-B confounded: `market_excess_return` is SPY-subtracted while `market_state` is computed from SPY (shared driver). | **RESOLVED 2026-07-05 (D4 / §4.2):** H-B tested on `sector_excess_return` (candidate − its SPDR sector ETF) — a *different series* from the SPY-derived label. Feasible from existing data (sector on Brief, ETFs in grouped store); β-residual acknowledged, β-neutral variant is a registered refinement. PR-2b logs it forward. |
| 2 | FATAL | Usable equity panel <1y (grouped store from 2024-09-11; SMA200+252d-ATR burn ~1y). No in-sample fit / firebreak possible. | §2: **all thresholds frozen a-priori, no fitting, `-UNVALIDATED` suffix.** §4.4: firebreak is a no-op by construction. §4.3: honest "years away" for episode-N. |
| 3 | HIGH | Effective-N overstated — autocorrelated daily labels ≈ single-digit independent episodes. | §4.3: **N = independent regime episodes**, cluster-robust inference (block bootstrap), not row-level Kruskal on daily labels. |
| 4 | HIGH | Bonferroni family undercounts ("pick best index/trend/combiner from forward data" = hidden multiple testing). | §4.2 + §2.1/§2.2 + D1/D5: **pre-commit ONE index, ONE trend def, ONE combiner a-priori.** v1 family = 9 tests, enumerated in the ledger. QQQ/HMM/alt-combiners are future class entries. |
| 5 | HIGH | Breadth loader `load_sp1500_pit_for_date_augmented` does not exist; snapshot is survivorship-biased. | §2.5: **breadth dropped from v1 as signal input**; display-only annotation excluded from all H-A/H-B correlation; real loader named; bias stated in body. |
| 6 | MED | Thresholds are unvalidated magic numbers; taxonomy could be curve-fit to remembered episodes. | §2: frozen, `-UNVALIDATED`, first forward result is descriptive only; no tuning claim. |
| 7 | MED | Residual crypto carryover (squeeze-precedes-move, OR-combiner, vol asymmetry). | §1.1: `range_compression` dropped to telemetry flag. §1.3/D5: OR labelled crypto-origin hypothesis. §1.2: vol asymmetry marked an unvalidated prior. |
| 8 | MED | Display-vs-selection separation is a test convention, not an architectural boundary. | §3.2: **new positive-control test** fails if any `market_state*` column feeds `layer4_weighted_score`/`selection_score`. |
| 9 | LOW | Broadcast idiom copies `disagreement` imperfectly (10 mixed-dtype cols, two missing conventions). | §3.1: explicit empty-frame dtypes per column; two missing conventions (`'unknown'` / `NaN`) documented; round-trip schema test on zero-row + all-`unknown`. |

---

## 8. Data-provider evaluation — verdict: NO PURCHASE (2026-07-05)

**Question:** the usable equity panel is <1y (FATAL-2). Could buying a historical-data provider resolve the memo's validation doubts, so the taxonomy can be fit + firebreak-tested on real history?

**Method:** Perplexity deep-research on the provider landscape + an in-repo grounding workflow (data-window verification, doubt→dataset mapping, platform/canonical-client cost) + an adversarial skeptic pass. Skeptic verdict: **SOUND_WITH_FIXES** — "buy nothing expensive" is correct; "do the free backfill now" is over-sold.

### 8.1 Which doubts are even resolvable by buying data?
| Doubt | Data-buyable? | Why |
|---|---|---|
| FATAL-1 (H-B SPY-on-SPY confound) | **NO** | design confound. No dataset breaks the mechanical entanglement — only the §4.2 decoupling (non-SPY benchmark / residualization) does. |
| H-B (regime conditions the tool's OWN selection edge) | **NO** | needs the tool's own **forward** ladder outcomes. No vendor backfills the future; N≥30 is ~2026-09+ at the earliest. |
| FATAL-2 / MEDIUM-overfit (no in-sample window) | **only for H-A** | long index history gives an in-sample fit + OOS window **for H-A** (index-regime hypothesis) only. |
| HIGH-3 (effective-N = episodes) | **partially, H-A only** | decades of history = dozens of past regime episodes for H-A; H-B episode-N is still forward-bound. |
| HIGH-5 (breadth survivorship) | **yes, but v2** | needs PIT + delisted rosters — but **breadth was dropped from v1** (§2.5), so this buys nothing v1 uses. |

**So data helps only H-A. It does NOT rescue H-B or fix the confound.**

### 8.2 Why NO expensive provider (verified in-repo)
- **Norgate** is the *only* retail sub with multi-decade PIT S&P constituents + delisted bars (<$100/mo), but its Python API needs a **Windows desktop Updater running in the background** → dead-on-arrival on the AlphaLens macOS + Linux VPS + Docker + runpod stack. Not an actionable option (kept as a rejected note, §8.5).
- The expensive datasets (PIT constituents, delisted bars) only buy back **breadth**, which v1 dropped. The augmented loader `load_sp1500_pit_for_date_augmented` is **confirmed unbuilt anywhere in the repo** — no half-wired breadth path exists to tempt an early purchase.
- Institutional gold-standard (CRSP/WRDS, Bloomberg, FactSet, direct SPDJI) is out on cost/access for a solo quant.

### 8.3 Why NOT even the "free" backfill now
The cheap path (backfill 20–30y SPY/QQQ daily bars from Polygon/yfinance; VIX already on FRED since 1990) is tempting but **not yet, and not truly free**:
1. **Not $0.** yfinance long-history split/dividend series have gaps + back-adjustment revisions → real reconciliation work vs Polygon. A 25h free-tier Polygon backfill **contends with live Polygon quota** (6×/day thematic build + nightly feedback/grouped jobs); the project already hit a Polygon-429 hung-run (PR #747). Honest floor ≈ **$29 (one Polygon Starter month) + a few hours**, not $0.
2. **Non-stationarity undercuts the long-history fit.** Fitting *fixed* thresholds over 20–30y that the memo itself calls non-stationary (2001 vs 2026 microstructure) is incoherent: a threshold fit on 2001–2015 has no reason to transfer to 2016–2026, so a "passing OOS firebreak" is nearly uninterpretable. Either use rolling/expanding-window **self-referential** percentiles (which ATR% already does, §2.3) — in which case there is no cross-era threshold to fit — or restrict to post-2015, which re-hits FATAL-2's too-little-data wall. **Long history does not deliver the clean OOS the fit-then-freeze plan assumed.**
3. **H-A may be inert.** The backfill validates only H-A, which the memo never shows is useful on its own. If a passing H-A changes no v1 behavior until H-B matures (~2026-09+), validating it now is activity, not progress.

### 8.4 Decision — spend $0, sequence behind free design work
1. **First (free): make the FATAL-1 decision (D4)** — pick a non-SPY outcome benchmark (peer/sector) or a pre-registered residualization. This gates whether H-A thresholds even aim at the right target; fitting before it risks locking choices that must be re-fit once the benchmark changes.
2. **"What does a validated H-A license?" — DONE, see §4.6.** Answer: near-inert — H-A earns only a display caveat drop + a diagnostic screen on H-B, no v1 behavior change. Because it is inert without H-B, **the backfill can wait too.**
3. **Backfill only if** (1) and (2) settle and a validated H-A licenses a shippable v1 behavior — then a **Polygon Starter month (~$29)** run scheduled off all live-job windows is cleaner than the free path (avoids quota starvation). Use **self-referential rolling percentiles**, not fixed cross-era thresholds.
4. **Breadth re-entry rule:** reconsider breadth-as-signal (and therefore PIT/delisted data) **only after** H-A is validated AND H-B shows a regime-conditioned edge (~2026-09+). Until then, "v1 dropped breadth" is a time-boxed deferral, not a permanent close (project doctrine: never close the door).

### 8.5 Provider reference (for a future breadth-as-signal v2 only — not v1)
| Provider | Covers | Cost | Individual-usable? |
|---|---|---|---|
| Polygon (**already integrated**) | long SPY/QQQ OHLC | $0 free / $29 Starter | yes — **the path if any** |
| yfinance (**already integrated**) | long SPY/QQQ OHLC + `^VIX` | free | yes (quality caveats) |
| FRED (**already integrated**) | VIX since 1990 | free | yes — VIX solved |
| EODHD | PIT S&P constituents (2–12y), delisted, index OHLC | ~$25–80/mo | yes, cross-platform — **best if breadth returns but only ~1 decade deep** |
| Norgate | multi-decade PIT + delisted + index OHLC | <$100/mo | **NO — Windows-Updater-locked, dead on this stack** |
| CRSP/WRDS, Bloomberg, FactSet, SPDJI | gold-standard multi-decade PIT | institutional | **NO — out on cost/access** |

**Bottom line:** don't buy anything. The one provider that could give multi-decade survivorship-free breadth (Norgate) doesn't run on this stack; everything else v1 needs (long index OHLC + VIX) is already available for ~$0. The real blockers — the FATAL-1 confound and the tool's own selection edge (H-B) — are unbuyable at any price. Settle the free design decision first; spend engineering time (and maybe $29) only if a validated H-A is shown to license a shippable v1 behavior.

---

## Verified file citations (confirmed present on `main`)
- Enrich/config-version/empty-frame idiom: `apps/alphalens-pipeline/alphalens_pipeline/experts/disagreement.py` (L71–101, `PANEL_CONFIG_VERSION` L47).
- Poolability key: `apps/alphalens-pipeline/alphalens_pipeline/thematic/screening/selection_score.py`.
- ATR%-quantile primitive to reuse: `apps/alphalens-pipeline/alphalens_pipeline/data/macro/signals.py::vix_decile` (L22–37); FRED client `data/macro/fred_client.py`.
- VIX buckets: `apps/alphalens-feedback/alphalens_feedback/regime.py::classify_vix`.
- Trend cross-check vocabulary: `apps/alphalens-research/alphalens_research/attribution/regime.py::classify_regime`.
- Grouped-daily store: `apps/alphalens-pipeline/alphalens_pipeline/data/rs_history.py` (starts 2024-09-11, ~446 sessions).
- PIT loader (breadth, DROPPED v1): `apps/alphalens-pipeline/alphalens_pipeline/data/universes/sp1500_pit.py` — `load_sp1500_pit_for_date` / `load_sp1500_pit_union` exist; augmented-delisted loader does NOT.
- Score-stage call site: `apps/alphalens-pipeline/alphalens_cli/commands/thematic.py` (~L500, after `disagreement.enrich`).
- Django model + guard: `apps/alphalens-django/briefs/models.py`, `briefs/tests/test_schema_parity.py`; ingest `briefs/ingest/parquet.py`.
- SPA: `apps/web/src/lib/types.ts`, `CandidateCard.svelte`, `ExpertPanel.svelte`; serializer `apps/alphalens-django/briefs/api/serializers.py` (`exclude=("pk",)`).
- Forward-validation infra + confound source: `feedback/population_ladder_monitor.py`, `feedback/benchmark_excess.py` (SPY-subtracted outcome, `DEFAULT_BENCHMARK_TICKER="SPY"`, ~42-session window), `edge/models.py::LadderOutcome`, `docs/research/feedback_edge_dashboard_2026_06_04.md` §3.2, `feedback_ledger_counterfactual_design_2026_06_02.md`, `docs/research/preregistration/ledger.json`.
- Sector-relative outcome (D4 resolution): mirror `feedback/benchmark_excess.py::enrich_store_with_benchmark_excess`; sector classification already on the Brief — `apps/alphalens-django/briefs/models.py` (`industry_id` SIC4 / `industry_name` / `sector_name` / `peer_cohort_level`) from `apps/alphalens-pipeline/alphalens_pipeline/data/fundamentals/sic_index.parquet`; peer-cohort resolution to reuse: `apps/alphalens-pipeline/alphalens_pipeline/thematic/screening/sector_peers.py`; sector-ETF bars in the whole-market `grouped_daily_history` store (same Polygon client as SPY).
