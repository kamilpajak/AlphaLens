# Regime gate — Phase 1 diagnostic (2026-04-29)

**Goal:** before sinking ~3-4h into Phase 2 (5 pre-registered backtests with Bonferroni gate), measure how much of the IS 2017-2022 failure window each classifier even *covers*. Coverage ≈ 0% means the gate is degenerate (identical to ungated scorer); coverage that is sparse and short-run captures noise events, not the underlying regime.

**Window:** 2017-01-03 → 2022-12-30 (1499 SPY trading days). This is the IS period over which mom+lowvol BASE failed phase-robustly (mean αt = +0.49, excess −5.7%/y, 44.5pp dispersion — see `project_mom_lowvol_combo_failed_2026_04_29.md`).

**Script:** `scripts/regime_gate_phase1_diagnostic.py`. Output dropped under `~/.alphalens/regime_gate/`.

## Headline numbers

| Classifier | OFF days | OFF % | Runs | Mean run | Max run |
|---|---|---|---|---|---|
| C1 — yield curve inverted (10Y−2Y < 0) | 130 | 8.7 | 3 | 43.3 | 125 |
| C2 — VIX > 20 | 611 | **40.8** | 45 | 13.6 | 246 |
| C3 — NFCI > +1 | 0 | **0.0** | 0 | 0.0 | 0 |
| C3a — NFCI > 0 (alt threshold) | 39 | 2.6 | 1 | 39.0 | 39 |
| C5 — BAA10Y > 2.5% (proxy for HY OAS > 400bp) | 173 | 11.5 | 1 | 173.0 | 173 |
| C6 — IWM 30d < 0 AND SPY 30d > rolling-252d-median | 67 | **4.5** | 27 | 2.5 | 11 |
| C6a — IWM 30d < 0 AND SPY 30d > 0 | 208 | 13.9 | 52 | 4.0 | 31 |
| C6b — (SPY − IWM) 30d spread > 5% | 140 | 9.3 | 21 | 6.7 | 32 |
| C6c — (SPY − IWM) 30d spread > 3% | 321 | **21.4** | 49 | 6.6 | 37 |

## Verdict against the Perplexity hypothesis

The plan specifically predicted **C6 > 40%** coverage as the primary candidate, with C1/C2/C3/C5 < 30%. The actual ranking is **inverted on C6**:

- **C6 (the target-aware cross-sectional gate) is only 4.5%**, max-run 11 days. This is below every alternative variant we tried.
- Even the loosest cross-sectional variant tested (C6c, 3% spread) tops out at **21.4%**, well below the predicted 40%+ for the canonical spec.
- The single classifier that lights up heavily is **C2 VIX>20 at 40.8%** — but VIX>20 captures stress-vol regimes (COVID, Q4 2018, all of 2022), not the steady mega-cap drift that actually killed mom+lowvol BASE.
- **C3 NFCI > +1 is degenerate (0%)** — threshold is GFC-level and 2017-2022 didn't reach it.
- **C5 BAA10Y > 2.5% is 11.5% in a single 173-day run** — captures March 2020 spread blowout only. As a binary gate it's effectively a window split, not a regime detector.

## What the data actually says about the failure mode

The mom+lowvol failure window 2017-2022 was **structurally normal on every macro stress dimension we have**: yield curve flat-to-positive most of the period, NFCI never hit +1, BAA stress only in COVID. The cross-sectional dispersion (mega-cap > small-cap performance) never resolved into a sharp daily-regime signal — it was a slow cumulative drift that compounded to enormous CAGR gaps without ever showing up as point-in-time dispersion strong enough to flip a sensible threshold.

This empirically backs the Perplexity warning at the bottom of the plan ("mega-cap concentration is structural, not cyclical"). A regime gate built on macro/cross-sectional-snapshot signals cannot detect this — there is no signal at the daily-classification grain.

## Phase 2 scope decision

Per the original plan, "if all 5 FAIL → accept structural close, pivot to mega-cap universe or Option C." Phase 1 already shows the gates can't even *cover* the failure. There is no production scenario in which gating on C1/C3/C5 changes outcomes meaningfully:

- **C3 (0%):** identical to ungated scorer. Degenerate. Skip.
- **C5 (one 173-day run):** degenerate split, not a regime detector. Skip.
- **C1 (8.7%, 3 runs, max 125 days):** captures late-2022 yield-curve inversion only. Tests "does mom+lowvol work better when not inverted." Not specific to mega-cap dominance.

That leaves classifiers that at least have non-trivial coverage and run structure:

- **C2 (VIX>20):** 40.8%, plausibly affects strategy returns.
- **C6c (3% dispersion spread):** 21.4%, the only cross-sectional spec with enough density to test.

**Recommendation:** reduce Phase 2 to **2 hypotheses (C2 + C6c)** with **Bonferroni n=2 → critical |t| ≈ 2.24**. Drop C1, C3, C5 with reasoning recorded in pre-registration ledger as "degenerate coverage in IS window — skipped at Phase 1 diagnostic."

This keeps the multiple-testing guardrail honest (we're not HARKing — coverage is observable ex-ante and skipping a 0%-coverage gate isn't outcome-driven).

If both C2 and C6c FAIL Phase 2, the structural-close conclusion lands with stronger evidence than running 5 hypotheses where 3 are degenerate would have provided.

## Perplexity Sonar Reasoning Pro 2026-04-29 PM — critique of the proposed Phase 2 redesign

The "drop C1/C3/C5, run only C2+C6c with n=2" recommendation above was overruled. Substantive points (verbatim sense):

1. **HARKing.** Pre-registration bound n=5; dropping based on Phase 1 coverage is post-hoc rationalization, not "degenerate coverage." Defensible only if an amendment clause was pre-registered (it wasn't). Honest path: report all 5 with Bonferroni |t|≈2.58 and accept lower power, OR amend the pre-registration explicitly and document the amendment.
2. **C6 was a bad operationalization.** Rolling-252d SPY median is regime-dependent, not condition-dependent — in 2017-2022 bull markets SPY > rolling median almost always, so the AND-gate with IWM<0 never lights up. C6c (3% spread, 21%) is not a regime either: 6.6-day mean run = microstructure noise clustering, not multi-week regime structure.
3. **C2 VIX gate is a decoy.** Mom+lowvol failed in BOTH high-VIX (2018, 2020, 2022) AND low-VIX (2017, 2019, 2021) — failure was structural drift, not vol-regime alpha decay. A VIX gate doesn't rescue structural drift, it just deletes sample.
4. **Phase 1 already falsifies the regime-gate hypothesis.** Failure window was unremarkable on every ex-ante stress measure. Running Phase 2 to confirm the null = theater + multiplicity p-hacking. Regime-gate rescue is empirically dead.
5. **Pivot:** (a) S&P 500 mega-cap V2 with **fresh pre-registration**, not "Phase 2 of same strategy" — calling it the same strategy is universe-shopping; calling it V2 is a separate research question. (b) Abandon the strategy — also honest, lands on falsification cleanly. Perplexity didn't both-sides: regime-gate dead, pick (a) or (b).

## Revised verdict (post-Perplexity)

**Stop Phase 2.** The diagnostic-coverage table is already an empirical falsification — every classifier with non-trivial coverage targets a stress dimension that wasn't operative during the failure window, and every classifier targeting the dispersion failure mode is too sparse to gate. No backtest of these gates will rescue the strategy because the underlying daily-classification grain doesn't capture the cumulative structural drift that killed it.

Decision tree forward:
- **Path A (mega-cap V2):** new pre-registration on S&P 500 universe (or top-100 by market cap), fresh signal class `mom_lowvol_megacap_2026_05`, Bonferroni stays at n=1 for the bare V2 hypothesis. Honest if framed as "different universe, different research question," dishonest if framed as rescue.
- **Path B (abandon factors, Option C):** reframe AlphaLens as decision-support / anti-pattern catalog per the 2026-04-25 strategic pivot. The 5 paradigm failures + this regime-gate falsification become the 6th data point.

## Caveats / data limitations

- **C5 used BAA10Y > 2.5% proxy** instead of literature-grounded BAMLH0A0HYM2 > 400bp. FRED truncated the HY OAS series to a rolling 3-year window in April 2026, so it can no longer cover 2017-2022. The substitution shifts the credit-stress threshold scale (Baa investment-grade vs HY); this is for diagnostic coverage only and would need re-anchoring against literature if C5 makes it into Phase 2 (which the recommendation above says it shouldn't).
- **C6 "rolling 252d median"** uses operational no-look-ahead median. An out-of-sample-median variant would be slightly different but the broader finding (cross-sectional dispersion is rare even when relaxed) holds across all four C6 specs tested.
