# theme_mapper mechanical-rule head-to-head — design memo (D4)

**Status:** LOCKED (pre-compute) — adversarial review pending before the retro first-look compute.
**Date:** 2026-07-12
**Author:** Kamil Pająk
**Supersedes-as-powered-path:** the D3 forward grounding-stamp in `theme_mapper_grounding_leak_design_2026_07_12.md` §11.1 (D3 kept only as long-horizon insurance; instrument A ~1.5 yr to power).
**Related:** [[theme_mapper_grounding_leak_design_2026_07_12]] (D1 characterization + D2 confounded diagnostics); reading-vs-predicting thesis `reading_quality_eval_design_2026_07_11.md`.

---

## 1. Problem

`theme_mapper` proposes 5-15 tickers from **only the theme name** — pure PREDICTION from training knowledge (see the grounding-leak memo §1). D1 measured that **69-97% of its picks are free-association** (named in no ingested article). D2 showed the within-picks grounded/free split is uninterpretable (size/liquidity confound flips the sign under matching — a Simpson artifact). Neither answers the decision-relevant question:

> Would replacing the LLM's theme→ticker PREDICTION with a MECHANICAL rule that READS the theme's actual news do better?

This is the direct test of the project thesis ("let the LLM READ, let a mechanical rule PREDICT") applied to the one pipeline step that violates it.

## 2. Goal — H4

> **H4** — A mechanical **news-grounded** candidate generator (tickers ranked by how often they are named in the theme's own news over the production 30-day window) produces picks with **higher forward `market_excess_return`** (vs SPY, ~42-session window) than the LLM's picks, **size/liquidity-matched**.

Two-sided in principle, but the decision-relevant, robust direction is "mechanical ≥ LLM": if the mechanical rule wins **despite having no downstream gates** (see §4 asymmetry), the leak is confirmed and the fix is concrete (replace the proposal step). If the LLM wins or ties, news-grounding is not the lever.

**Scope guard:** measurement / first-look, not a production change. Any swap of the proposal step is a separate, later PR gated on a powered verdict.

## 3. Two variants (feasibility-driven)

| variant | what it compares | collider? | feasible | horizon |
|---|---|---|---|---|
| **V-retro** (this memo, primary now) | mechanical top-N picks **vs LLM gate-survivors**, forward `market_excess`, size-matched | asymmetric (LLM gated, mechanical ungated) — see §4 | **now** (~2 months matured) | first-look |
| **V-forward** (clean, pre-registered) | mechanical proposals **vs full LLM pre-gate proposals**, both **ungated**, forward `market_excess`, size-matched | none (both ungated, same universe) | needs a logging PR | ~months (mechanical + LLM proposal breadth ≫ 3% → N accrues fast) |

**Why V-retro can't be the clean proposal-stage test:** `~/.alphalens/thematic_candidates/` persists **only `verified=True` gate-survivors** (610/610 verified, 0 rejected; n_gates_passed ≥ 1 always). The LLM's rejected proposals are discarded, so the ungated LLM proposal set is unavailable retrospectively. V-forward requires a small PR to persist the full pre-gate proposal set (see §8).

## 4. The gating asymmetry (V-retro's one structural caveat)

V-retro compares LLM **survivors** (passed the mcap filter + ≥1 gate) against mechanical **proposals** (ungated). This is asymmetric:
- If **mechanical wins**, it wins *without* the gates that are supposed to help the LLM arm → the conclusion "reading beats predicting" is **robust** (the asymmetry works against the mechanical arm).
- If **LLM wins**, it could be the gates, not the proposal quality → **confounded**, not interpretable as "predicting beats reading".

So V-retro can **confirm** the leak (mechanical wins) but cannot **clear** the LLM (LLM wins is ambiguous). Stated up front; V-forward removes it.

## 5. Feasibility (measured 2026-07-12, VPS)

- **Mechanical pool is healthy:** over the 278 theme-date pairs the pipeline actually used, distinct grounded tickers per pair — mean 6.0, median 5, p25 3, p75 9, max 40; **90% ≥1, 76% ≥3, 57% ≥5**. The rule is not starved; it can match the LLM's ~2-15 breadth.
- **LLM arm outcomes:** `~/.alphalens/population_ladders/` — 610 survivor candidates (375 unique theme-ticker), **128 matured / 478 with `market_excess`**, brief_date 2026-05-19 → 2026-07-11.
- **Mechanical arm outcomes:** forward `market_excess` computed from the persistent `~/.alphalens/grouped_daily_history/` (split-adjusted daily closes, 2024-09 → present) — stock 42-session close-to-close minus SPY same-window. **No Polygon calls** (all local parquet). A minute-bar VWAP refinement (matching production `benchmark_excess.py`) is a later precision pass; close-to-close is adequate for a first-look.
- **Size/liquidity covariate:** dollar-volume from `grouped_daily_history` (close × volume), as in D2.

## 6. Mechanical rule (frozen definition) — SALIENCE membership, NOT frequency

**Revised per adversarial review (§12).** Ranking by article-frequency was rejected: news-frequency is itself an attention/coverage proxy whose residual return-predictive power *survives* size/liquidity matching (Menon 2017 information-intensity → lower future returns after characteristic controls; Barber-Odean 2008 attention; joint-coverage → contemporaneous inflation + reversal). A frequency-ranked rule would make the mechanical arm *another attention bet*, not a clean reading signal. The fix: use **membership / salience, not intensity**.

For theme Θ on brief_date D:
1. **Pool = primary-subject membership:** all tickers in `primary_entities` of `thematic_events` rows where Θ ∈ `themes`, over `[D − 30d, D]` (production `DEFAULT_LOOKBACK_DAYS = 30`). `primary_entities` is the extractor's **primary-subject** set (companies the article *is about*), so membership already encodes within-article salience — this is the "the article is about the ticker" signal, not "the ticker is often named".
2. **Weight: EQUAL** across all salient members. **No frequency ranking** (the review's core correction).
3. **Universe filter:** apply the **same mcap band** the LLM path applies (parity — deterministic universe constraint, not the leak).
4. **N:** take **all** salient members that pass the mcap band — do **NOT** tie N to the LLM survivor count (that couples the arms' attention profiles; §12 risk 3). Report performance **as a function of N** and of any salience cut so an attention-monotone pattern is visible if present.
5. **Config:** `MECH_RULE_VERSION = "mech-salience-equalweight-v1"` (poolability key for any forward stamp).

## 7. Method — V-retro first-look

- **Estimand:** difference in forward `market_excess` between the mechanical set and the LLM set, **paired by (Θ, D)** where both arms have matured members, and pooled across all matured members as the secondary view.
- **Metric:** 42-session close-to-close `market_excess` = stock return − SPY return over `[entry_session, entry_session + 42 sessions]`, `entry_session` = first session ≥ D (exchange calendar, XNYS).
- **Matching:** size/liquidity — match mechanical to LLM members on dollar-volume decile (+ sector where available); report both matched and unmatched (the D2 lesson: unmatched pooling invites a Simpson artifact).
- **Test:** permutation test (exchangeability of arm labels) on the mean/median difference — **not** Mann-Whitney (unequal variances likely); Wilson CI on each arm's sign-rate; bootstrap CI on the paired difference.
- **Multiplicity:** one new hypothesis on the EDGE program; report raw p + Bonferroni threshold; framed **first-look**.

### 7.1 Confounders (reported, not dropped)
0. **Residual attention survives matching (the interpretability ceiling).** Even with the salience-membership rule + dollar-volume matching, coverage/attention retains return-predictive power *within* size/liquidity strata (§12 risk 1). V-retro therefore has a hard ceiling: it cannot fully separate "grounding" from "residual attention". Only V-forward with the ungated salience rule + explicit attention controls approaches a clean read. Every V-retro result line carries this caveat.
1. **Gating asymmetry (bidirectional)** (§4) — the gates are part of the LLM *treatment* (they can genuinely improve the LLM arm by dropping stale/marginal names), and the ungated mechanical arm is biased toward the **attention/sentiment tails** a press gate would remove. So "mechanical wins → robust" holds only because the asymmetry pushes the mechanical arm *down*; "mechanical loses" is ambiguous (could be the ungated attention-tail exposure, not proposal quality). Stated in every result line.
2. **Size/liquidity** — matched (§7); the live confound per D2.
3. **Set overlap** — grounded LLM survivors may also be mechanical picks; report overlap and a disjoint-only sensitivity.
4. **Within-day look-ahead (mild)** — `thematic_events/D.parquet` = last run of day D; the mechanical rule sees the same end-of-day news the LLM's theme did. Symmetric-ish; noted.
5. **Matured-subset survival** — as in D2; report time-to-resolution per arm.
6. **Close-to-close vs production VWAP** — first-look uses close-to-close; production `market_excess` uses arrival-VWAP. The LLM arm's stored value is VWAP-based, the mechanical arm's is close-to-close → **recompute the LLM arm close-to-close too** for apples-to-apples (do not mix the two definitions).

## 8. Forward instrument (V-forward, the clean test) — pre-registered

Persist, at map-themes time, the **full pre-gate LLM proposal set** (currently discarded) and the **mechanical proposal set**, both ungated, on a shadow parquet with `(brief_date, theme, ticker, source ∈ {llm, mechanical}, llm_confidence?, mech_article_count?, mapper_config_version, mech_rule_version, proposal_shadow_version)`. Forward `market_excess` is then computed for both from price. This removes the gating asymmetry (both ungated) and the collider (same universe, same downstream treatment = none). N accrues fast (proposal breadth ≫ 3%). This is a small, parquet-only, forward-only PR (TDD + zen + VPS image deploy) — pre-registered here; built only if V-retro is directionally encouraging or the user wants the clean verdict regardless.

**IMPLEMENTED 2026-07-12** — `alphalens_pipeline/thematic/mapping/proposal_shadow.py` (+ `orchestrator._rows_for_theme`/`map_themes` capture the LLM pre-gate proposals). Written best-effort at map-themes time to **`<output_dir>/proposal_shadow/{date}.parquet`** (production `~/.alphalens/thematic_candidates/proposal_shadow/`; the shadow dir is derived from the candidates `output_dir` so tests stay hermetic). `mech_article_count` is a descriptor only — the rule is equal-weight membership (§6), never frequency-ranked. Deploy = VPS pipeline image rebuild (operator-owned); forward-only. Analysis (size/attention-matched permutation, §7) reads this + the persistent `grouped_daily_history` at verdict time (~2026-09+).

## 9. Success / kill (first-look) — framed for a net-negative universe

Prior work: this whole selection space is net-negative (picks fade), consistent with attention-driven overpricing + reversal. So both arms are expected negative; this is a **"which loses less"** comparison, **not** an alpha test. Frame any advantage as "better at avoiding the worst attention-driven reversals", never as a profitable signal.

- **Leak confirmed → build V-forward + prioritize the mechanical-rule swap design:** matched mean/median difference favors mechanical with a permutation p that is at least suggestive AND the sign survives dollar-volume matching AND disjoint-only AND is not monotone in salience-rank (a monotone-in-attention pattern would indicate the confound, not grounding).
- **No signal / LLM ties-or-wins:** ambiguous per §4/§7.1 — record as "salience-membership rule does not beat gated LLM selection retrospectively"; V-forward the only clean arbiter; deprioritize.
- **Short-inversion lens (report, do not act):** if both arms' excess is stably negative, note the magnitude — the reversal literature says the tradable signal here may be the *short*, not the *long*. Out of scope for a buy-side augmentation tool, but recorded for completeness.

## 10. Test plan

- [ ] Build the mechanical generator (frozen §6) + a small deterministic unit test on a seeded events fixture (TDD).
- [ ] Compute close-to-close `market_excess` for both arms from `grouped_daily_history` (recompute LLM arm close-to-close for parity, §7.1.6).
- [ ] Paired (Θ,D) + pooled comparison; dollar-volume-matched + disjoint-only sensitivities; permutation p; Wilson sign-rate; time-to-resolution per arm.
- [ ] Write results into §11; log verdict to the EDGE attribution ledger.
- [ ] If encouraged: V-forward shadow-logging PR (§8), separate, TDD + zen + deploy.

**Known gaps / not covered:** V-retro's gating asymmetry (§4) is not fixable retrospectively; close-to-close ≠ production VWAP (first-look only, both arms recomputed identically); matured-subset survival (~2 months of data); the mechanical rule is one frozen definition (frequency-rank) — richer rules (recency-weight, entity-confidence) are deferred to V-forward.

## 12. Adversarial review outcome (2026-07-12)

Perplexity (Sonar Reasoning Pro, literature-grounded); zen unavailable (tool cache bug embeds unrelated PEAD memos — same failure as the D-grounding memo). Findings applied above:

1. **(applied, §6) Frequency-rank was the worst possible baseline** — news-frequency is an attention proxy whose residual return-prediction survives size/liquidity matching (Menon 2017; Barber-Odean 2008; joint-coverage → inflation+reversal). Switched to **equal-weight salience membership** (primary-subject `primary_entities`), no ranking.
2. **(applied, §7.1) Gating asymmetry is bidirectional** — gates are part of the LLM treatment; ungated mechanical is biased to attention/sentiment tails. "Mechanical wins" stays robust; "mechanical loses" is ambiguous.
3. **(applied, §6.4) Decoupled N from the LLM survivor count** — take all mcap-passing salient members; report as a function of N/salience.
4. **(applied, §7.1.0) Residual-attention ceiling** — even the salience rule + matching cannot fully separate grounding from attention; V-retro is directional-only; V-forward is the clean arbiter.
5. **(applied, §9) Net-negative framing** — "which loses less", not alpha; short-inversion lens recorded.

**Net effect:** V-retro remains worth running as a **cheap, honestly-caveated directional first-look** (local data, no cost), but the review lowered its ceiling — the clean verdict is V-forward with the ungated salience rule + attention controls. Proceeding to the V-retro compute with the corrected rule.

## 11. Results — V-retro first-look (2026-07-12, VPS)

**Method as built:** fixed-horizon selection test (buy @ first session ≥ brief_date, hold H sessions, close-to-close return − SPY same window, from the local split-adjusted `grouped_daily_history`; both arms computed identically per §7.1.6). Horizons H ∈ {5, 10, 21} (42-session windows do not fit the data — grouped ends 2026-07-10). LLM arm = `thematic_candidates` survivors; mechanical arm = equal-weight salience membership (§6).

### 11.1 The arms live in different size universes (headline)
Unrestricted, the mechanical rule's picks are **mega-caps** — dollar-volume median **10^9.33 ≈ $2.1B/day** vs the LLM's mid-cap **10^7.90 ≈ $79M/day** (the LLM path's 500M-10B mcap gate). "Reading the theme's news" naively pulls the book to the biggest, most-covered names. The distributions are nearly disjoint, so the unrestricted pooled comparison is two different strategies (pooled diff ≈ 0, p = 0.69/0.89/0.12) — not informative.

### 11.2 Size-matched (restricted to the LLM's dollar-volume [p10,p90] band) — the apples-to-apples test

| H | LLM mean (n) | MECH mean (n) | diff (LLM−MECH) | perm p | MECH pos% vs LLM |
|---|---|---|---|---|---|
| 5 | +0.0016 (397) | +0.0065 (162) | −0.0049 | 0.51 | 50% / 51% |
| 10 | +0.0039 (362) | +0.0187 (148) | −0.0148 | 0.19 | 59% / 47% |
| 21 | **−0.0212 (225)** | **+0.0739 (114)** | **−0.0950** | **0.000** | **72% / 40%** |

Within a comparable size band the **mechanical salience-membership rule beats the LLM free-association**, the gap **grows with horizon**, and at H=21 the mechanical arm is **positive** (+7.4% excess vs SPY, 72% win-rate) while the LLM arm fades (−2.1%, 40%). **Disjoint-only** (drop tickers in both arms) holds: H=21 LLM −0.023 vs MECH +0.073. The direction is consistent across all three horizons. This is the first **positive, size-controlled** signal in this selection space and directly supports the thesis "reading beats predicting".

### 11.3 Skeptical caveats (this is a first-look, not a verdict)
- **H=21 is period-concentrated:** a full 21-session window fits only entries ≤ ~2026-06-10, so H=21 draws on ~3 weeks of brief_dates — the strong p=0.000 may partly reflect a late-May/early-June regime, not a stable edge. H=10 (wider window, entry ≤ ~2026-06-26) is directionally consistent (mechanical +0.019 vs +0.004) but only p=0.19.
- **Residual attention within band** (§7.1.0): the in-band mechanical dvol median (8.1-8.3) sits slightly above the LLM's (7.9), so a sliver of the size/attention channel survives even the band restriction.
- **Multiplicity:** three horizons tested; H=21 is the standout and the thinnest.
- **Gating asymmetry** (§4): the mechanical arm is ungated and still wins — the robust direction; consistent with "reading beats predicting".
- **Net-negative framing** (§9): at short H both arms hover near zero; the mechanical advantage is "loses less / fades less", except at H=21 where it turns genuinely positive.

### 11.4 Robustness sweep (2026-07-12, Workflow: 6 cuts + adversarial synthesis)

The +7.4% (H=21) draws on only **4 ISO weeks (21-24, late-May → mid-June)**. Six cuts on `/tmp/d4_base.py` decomposed it:

- **Week-by-week H=21:** diff is negative (mechanical wins) in **all 4 weeks** (−0.040, −0.057, −0.118, −0.135) — **sign robust**. But week 23 alone supplies MECH +0.134 on n=50 (~44% of the 114 pooled mechanical names) → **the +7.4% magnitude is a mid-June spike**; de-spiked (weeks 21-22) the edge is ~4-5%.
- **Leave-one-week-out H=21:** dropping any single week keeps diff −0.066 to −0.113, **p=0.000 in all four** — no single week is load-bearing. *But* with only 4 contiguous weeks, every drop keeps ~3 of the same regime → LOO rules out "one lucky week", **cannot** rule out "one lucky month".
- **Week-by-week H=10 (wider, 6 weeks):** mechanical wins in **only 2 of 6 weeks (23, 24)** — the same mid-June fortnight; weeks 21/22/25/26 **flip to LLM**. So H=10 is **weaker, not stronger** — it exposes the effect as a **single-fortnight phenomenon that persists to 21 sessions** (a legit slow-drift hypothesis, but about one fortnight).
- **Per-theme H=21:** **16/20 themes** mechanical>LLM across unrelated sectors (public_offering, consumer electronics, AI, biotech, defense) — **breadth real, not a 1-2 theme artifact** (cells tiny though).
- **Finer dvol quartiles H=21:** mechanical wins in **all 4** quartiles, **largest in the two LOWEST-dvol bins** (−0.141, −0.139), not monotone-increasing in dvol → **argues against a residual-attention/liquidity confound**.
- **Alternative rules:** membership (−0.095, p=0.000) and recency7d (−0.100, p=0.000) both replicate at H=21; min2_articles collapses to n=8-11 (a power failure, p=0.157, not a sign failure). Signal lives in broad single-article grounding.

### 11.5 Verdict — real-SIGN, artifact-MAGNITUDE

- **The sign is genuine** (4/4 weeks, LOO p=0.000, 16/20 themes, anti-confound dvol, 2 rules) — a mechanism plausibly exists.
- **The +7.4% is inflated by mid-June** (~44% of mass in week 23; evaporates at H=10 in 4/6 weeks). Carry forward a **~3-5% H=21 hypothesis, not 7.4%**, and expect regression toward ~2-3% or a wash.
- **All in-sample cuts are exhausted** — they reuse the same 4-week corpus, so none can answer the only question that matters: *does it survive outside late-May → mid-June?* Only fresh out-of-window data can.

**Recommendation (synthesis): BUILD V-forward, but expect small.** It is the only remaining instrument, it is cheap (log-only, no capital), and the sign-robustness + breadth + anti-confound evidence clears the "worth logging" bar. Pre-registration guardrails for §8:
- Primary endpoint **H=21, membership rule**; **recency7d** confirmatory; **drop min2_articles** (underpowered, no Bonferroni slot).
- **Log H=10 too and treat "H=10 keeps flipping positive" as a KILL signal** (would mean mid-June was regime, not mechanism).
- Need **≥8-10 fresh ISO weeks spanning a different regime** before any verdict (~2026-09+, the project's usual forward-first-look cadence) — 4 more contiguous weeks is not enough.
- Strictly display-only / in-sample-labelled / EDGE-telemetry lane; **not** a production `theme_mapper` change.

**Plain headline:** picking stocks by what's actually in the theme's news beat the LLM's gut picks over the tested month — but almost all the winning came from two hot weeks in mid-June, so V-forward watches it live to see if it holds outside that patch.
