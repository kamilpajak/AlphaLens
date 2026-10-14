# Mandatory research gates

Procedures, not style rules. Each one is a HALT: a failed gate blocks the launch instead of
becoming a caveat in the memo. Re-verified against the code on 2026-10-14; every number below was
re-measured rather than carried.

## 1. Pre-audit smoke — before any audit over 1 h compute

```bash
.venv/bin/alphalens preaudit <strategy>
```

Two stages, in order:

1. **Coverage** — checks every declared `DataDep` under `~/.alphalens/` for existence and
   date-range coverage. It fails before any subprocess spawns.
2. **Smoke** — runs the experiment script end-to-end on a tiny universe over a short window, into
   an ephemeral `--out`.

- Exit codes: `0` both stages pass, `1` coverage or smoke failed, `2` unknown strategy / no
  `SmokeProfile` registered.
- Default smoke timeout is **600 s**, from `_DEFAULT_SMOKE_TIMEOUT_S` in
  `apps/alphalens-pipeline/alphalens_cli/commands/preaudit.py:37`. **Known disagreement:** the
  research-side docstring at `alphalens_research/preaudit/__init__.py:17` still says
  "≤5 min timeout". That line is stale; 600 s is what the CLI passes, and
  `apps/alphalens-research/tests/test_preaudit_cli_default_in_sync.py` pins the CLI constant to the
  research-side value (the constant is duplicated because `typer.Option` evaluates its default at
  import time and the CLI lazy-imports the research tier).
- Catches: missing data, a coverage gap, hash drift, CLI passthrough breakage, any end-to-end
  pipeline failure. Does **not** catch: OOM at scale, MooseFS I/O contention, time-varying signal
  corrosion.
- A new strategy needs a `SmokeProfile` in
  `alphalens_research/preaudit/profiles.py::SMOKE_PROFILES`, or an entry in
  `SMOKE_PROFILE_EXEMPT`. `apps/alphalens-research/tests/test_preaudit_profiles.py` enforces that
  and also catches a ghost profile (keyed by a strategy no longer in `AUDIT_SCRIPTS`), silently
  dropped smoke args, and a `has_component_hash_guard=True` claim the script does not back.
- Keep `smoke_window` to 1-3 months. The registered profiles use one- or two-quarter windows and
  `--universe-size-cap` of 300 once and 200 three times (measured 2026-10-14) — read `profiles.py`
  for the one you need rather than assuming a single repo-wide cap.
- `apps/alphalens-research/scripts/launch_dual_audits.sh:52` prepends
  `alphalens preaudit <strategy> --skip-smoke` as a coverage-only fail-fast gate.
- Postmortem that created this gate:
  [`insider_pc_compound_audit_launch_postmortem_2026_05_11.md`](insider_pc_compound_audit_launch_postmortem_2026_05_11.md).

## 2. Layer 4 overlay design pre-screen — before briefing any reviewer on an overlay test

Applies to every Layer 4 overlay test: vol-target, drawdown-control, CPPI, time-stop — anything
that sizes on realised portfolio vol.

1. **Pre-screen cyclicality EXCESS over the benchmark baseline.** Call
   `alphalens_research.attribution.signal_vol_regime.classify_cyclicality_excess(strategy_summary, benchmark_summary)`
   on the base portfolio's daily returns against the IWM benchmark, with both summaries classified
   against the SAME IWM 60-day realised-vol regime. If `verdict.proceed is False`
   (excess R mean ≤ -1.0, the module's `_DEFAULT_EXCESS_STRONG_THRESHOLD` at
   `signal_vol_regime.py:326`), write a REJECTED memo and do not register the hypothesis.

   Never decide this on raw `classify_cyclicality()` (`signal_vol_regime.py:207`). On an R2000
   long-only book it almost always returns EXTREME counter-cyclical, because the IWM benchmark is
   itself EXTREME counter-cyclical (R≈-2.0). Only the excess-over-baseline variant separates
   strategy-specific cyclicality from the universe-mechanical kind. Background: the machine-local
   auto-memory note `feedback_universe_baseline_cyclicality_2026_05_10`, which is not in this repo.

   **Known disagreement.** The gate's own docstring, in
   `apps/alphalens-research/tests/test_overlay_design_compliance.py:5`, tells you to call
   `classify_cyclicality()`. That is the looser of the two instructions — follow
   `classify_cyclicality_excess` here. The test cannot tell them apart either: its evidence marker
   (line 43) is the substring `classify_cyclicality`, which matches both names, so a memo quoting
   the wrong function still passes CI.
2. **Cross-check every factual base claim.** Any MaxDD / Sharpe / return figure in the brief must
   be verified against the dumped artifacts at `~/.alphalens/audit/<strategy>/phase_*_returns.parquet`,
   using `alphalens_research.backtest.metrics.max_drawdown` plus an independent inline computation.
   Never carry a number from memory or from postmortem prose.
3. **Quote the excess-cyclicality verdict verbatim in memo §4 (Hypothesis)** — that quote is the
   auditable artifact proving the screen ran.

Enforcement: `apps/alphalens-research/tests/test_overlay_design_compliance.py` scans
`docs/research/*overlay*.md` for any one of its evidence markers; pre-2026-05-10 memos are
grandfathered by an explicit allowlist, and adding to that allowlist needs an explicit instruction.

## 3. Data-vendor PIT validation gate — before a new vendor enters a pre-registration

Before using any new data provider (Alpha Vantage, SimFin, Polygon, iVolatility, …) in a
pre-registration:

- **At least 5 sector-diverse anchor events**, each triangulated across **2 sources**: Perplexity
  surfaces candidate URLs, then Playwright or the operator INSPECTS at least one contemporaneous
  source URL per event. Numeric extraction by Perplexity alone is insufficient.
- Tolerance: **≤±2¢ or ≤±1%** delta against the source-quoted ground truth.
- The result is a **HALT condition**. A fail blocks the audit launch and escalates to an
  alternative vendor.
- Precedents to copy the shape from — tolerance is per field TYPE, so state the bands in the memo
  rather than inheriting these:
  - [`paradigm14_pead_v2_design_2026_05_13.md`](paradigm14_pead_v2_design_2026_05_13.md) §3.1 —
    5 named anchor events from 2018-Q1, acceptance "≥4 of 5 agree within ±2¢ or ±1% of consensus",
    escalation path named on fail.
  - [`edgar_fundamentals_validation_2026_05_19.md`](edgar_fundamentals_validation_2026_05_19.md) —
    5 anchors × 2 sources with per-field bands: ≤±1% on instant balance-sheet fields, ≤±5% on TTM
    fields (fiscal-calendar drift), a ±$0.02 dollar floor on price, and named exempt fields.

## 4. α2 sub-leveraged weighting + Little's-Law cost-model audit — event-driven, daily rebalance

Before locking the weighting decision for an event-driven daily-rebalance strategy, run a
cost-model audit that derives the concurrent-position peak via Little's Law, `L = λW`:

- Set `N_FIXED = peak_concurrent + 50% safety margin`. Weight is `1/N_FIXED` per active position,
  gross ∈ [0, 1], and there is **no forced rebalancing** — peak overlap is absorbed by
  pre-allocated capacity.
- Worked derivation for PEAD v2 (S&P 500, 20-day hold, top-quintile filter), from that memo §5.1:
  λ_avg ≈ 7.94 events/day → L_avg ≈ 159 concurrent → top-quintile average ≈ 32. Peak earnings
  weeks give λ_peak ≈ 25/day → L_peak = 500 → top-quintile peak ≈ 100. So
  `N_FIXED = 100 × 1.5 = 150`, off-peak gross ≈ 0.21, peak ≈ 0.67, capped at 1.0.
- Required artifact: `docs/research/<paradigm>_cost_model_audit_<DATE>.md`.
- Empirical validation gate before the final lock: compute the 95th percentile of
  `concurrent_top_quintile_count` over a sample year using the real cohort-rank logic.
  p95 ≤ 100 → `N_FIXED = 150` is conservative and acceptable. p95 > 100 → bump to
  `1.5 × empirical_p95` and re-log the calibration
  ([`paradigm14_pead_cost_model_audit_2026_05_14.md`](paradigm14_pead_cost_model_audit_2026_05_14.md)
  §5.3).

**Corrected number.** The comparison against legacy `gross=1` equal-weight was long quoted as
"~16× cost amplification". The cited memo measures **2.0×**, and it measures something narrower:
the repo's set-based turnover (`len(curr − prev) / N`) undercounts a weight-based one-way turnover
by a factor of 2.0 on the memo's worked 4→5 position transition — 0.20 against 0.40, labelled
"Undercount factor: 2.0×" in §3. No 16× figure appears anywhere in that memo. Use 2.0×, and say
what it is a factor *of*.

Two further findings from the same memo, still open as repo-wide debt:

- `compare_cost_scenarios` passes `daily_turnover=None`, so `CostModel.apply` falls back to
  assuming 100% turnover every bar. Scenario comparisons are therefore conservative, but cannot
  distinguish 30% from 60% churn.
- A weight-based turnover function was deferred, not written.

That memo cites pre-split module paths (`alphalens/attribution/cost_model.py`,
`alphalens/backtest/metrics.py`); read them as `alphalens_research/...`. It also carries an
internal inconsistency, so quote §5: §5 and the §9 changelog lock `N_FIXED=150`, while §6
(line 121) and §7 (line 129) still carry the superseded `N_FIXED=30` from the first publication.

## 5. True PIT universe — mandatory for any paradigm over 100 tickers

- Every paradigm whose universe exceeds 100 tickers must use a true point-in-time panel from
  pre-registration day one: intersected snapshot rosters from
  `apps/alphalens-pipeline/data/sp{500,400,600}_pit/` — the `DEFAULT_DATA_ROOT` of
  `alphalens_pipeline/data/universes/sp1500_pit.py:35`, overridable with `$ALPHALENS_DATA_ROOT` —
  augmented with delisted tickers from
  `~/.alphalens/survivorship/{delisted_2007_2018,delisted_2021_2026}.parquet`.
- Implementation contract: `load_sp1500_pit_for_date_augmented(asof, include_delisted=True)` in
  `alphalens_pipeline/data/universes/sp1500_pit.py`. **It does not exist yet** — checked
  2026-10-14, the module defines only `load_sp1500_pit_for_date` (line 87) and
  `load_sp1500_pit_union` (line 128). Write it alongside the first paradigm that needs it.
- The two delisted stores are Mac-only and unequal: `delisted_2021_2026.parquet` is the one code
  reads today (`screeners/options_implied/target.py` plus ten scripts under
  `apps/alphalens-research/scripts/`, counted 2026-10-14),
  `delisted_2007_2018.parquet` has no code consumer yet. Their row counts are quoted in
  `event_sourced_lane_design_2026_09_03.md` (6 237 names) and
  `plan_C_survivorship_retrospective_2026_05_14.md` (2 051 names); neither was re-read here,
  because the stores are not in a worktree.
- Completed paradigms are **not** re-run retrospectively; their verdicts stand.
- When a universe falls back to the current snapshot, apply a **20-40 bps/y snapshot-bias prior**
  to literature numbers — subtract roughly 0.3 from the reported α t-stat.
- Rationale, including why the retrospective autopsy was dropped in favour of simply adopting the
  prior: [`plan_C_survivorship_retrospective_2026_05_14.md`](plan_C_survivorship_retrospective_2026_05_14.md)
  rejection block.
