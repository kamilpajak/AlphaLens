# alphalens/backtest — backtest harness

**The heart of AlphaLens.** Every Layer 2 deploy/archive decision passes through this infrastructure before any launchd job is touched. It said "yes" to Layer 2b (themed: Sharpe 1.53 net, FF3 α_t 2.60 HAC) and "no" to Layer 2c (lean: Sharpe 0.25, α_t 0.14). The harness is where an idea becomes an empirically validated strategy — or gets shelved.

The harness is **screener-agnostic**: any strategy that implements the `Scorer` protocol plugs into the same engine, reports, factor attribution, and cost model. The same infrastructure has validated the momentum scorer, the early-stage scorer, the Lean ranker, the Carhart revalidation, several survivorship probes, and the LLM-filter evaluation runs.

## Flow

```
OHLCV (Lean CSV zip)
       │
       ▼
HistoryStore  ← entry point, point-in-time truncation
       │
       │  + scorer: (histories, config) -> DataFrame[ticker, score]
       │  + scorer_config, top_n, holding_period, weighting, benchmark
       ▼
BacktestEngine.run(start, end)
       │
       │  rebalance loop (every `rebalance_stride` trading days):
       │    truncate_to(t) → scorer(histories) → top-N → forward_return(1d, Nd)
       │
       ▼
BacktestReport       ── portfolio_returns (Sharpe-ready, non-overlapping)
       │             ├─ portfolio_returns_holding (signal diagnostic, overlaps)
       │             ├─ ic_series (Rank IC per rebalance)
       │             ├─ universe_median_returns
       │             └─ scored_frames (optional, for diagnostics)
       │
       ├── metrics.py            → Sharpe, IC + t-stat, Calmar, max DD
       ├── cost_model.py         → flat bps drag (75 / 100 / 150)
       ├── factor_analysis.py    → CAPM → FF3 → Carhart-4F + HAC + industry controls
       ├── regime.py             → bull / bear / flat breakdown
       ├── diagnostics.py        → IC by decile, bear-regime vol decomposition
       ├── theme_analysis.py     → HHI + dominant-theme concentration alerts
       ├── historical_validation → LLM filter evaluation harness
       └── report.py             → markdown + CSV + decision matrix
```

## Contracts

### `Scorer` (input)

```python
Scorer = Callable[[Mapping[str, pd.DataFrame], Mapping], pd.DataFrame]
```

- **Takes**: `histories` (each DataFrame truncated point-in-time to `t`) + a `config` dict.
- **Returns**: a DataFrame with at minimum `ticker` and `score` columns. The engine does `sort_values("score", ascending=False).head(top_n)` internally.
- **Invariant**: the engine hands the scorer truncated histories — the scorer must not look into the future. Every rolling window (SMA, ROC, RSI) must be computed on `histories[ticker]` without extending it.

Example adapters:
- `alphalens.archive.screeners.themed.backtest_adapter.momentum_scorer_adapter` — Layer 2b `MomentumScorer` (7-metric classic momentum)
- `alphalens.archive.screeners.themed.backtest_adapter.early_stage_scorer_adapter` — Layer 2b `EarlyStageScorer` (base-breakout / VCP)
- `alphalens.archive.screeners.lean.lean_project.scorer.rank_universe` — archived Layer 2c (already matches `Scorer` signature, no adapter needed)

### `HistoryStore` (data)

In-memory, pure pandas, zero I/O in the hot loop. Loading is the caller's responsibility:

```python
from alphalens.archive.screeners.lean.lean_csv_loader import load_lean_histories
from alphalens.data.store.history import HistoryStore

histories = load_lean_histories(DATA_DIR, tickers)  # zip-CSV → dict[ticker, DataFrame]
store = HistoryStore(histories)
```

**DataFrame conventions**: each value must be a `DatetimeIndex`-indexed DataFrame sorted ascending, with lowercase columns `["open", "high", "low", "close", "volume"]`. The Lean CSV loader produces this shape out of the box. If you write a custom loader, match it.

**Why Lean zip-CSV and not yfinance?** Lean data has a stable format, uses adjusted prices via factor files, ships a deterministic trading calendar, and works offline. yfinance loses names mid-backtest (delistings, rate limits) which breaks reproducibility over 5-year windows.

### `BacktestReport` (output)

- `portfolio_returns` — 1-day forward return of top-N **(Sharpe-ready, non-overlapping)**
- `portfolio_returns_holding` — holding-period forward return of top-N (overlapping, for signal diagnostics, **not** Sharpe)
- `ic_series` — cross-sectional Rank IC on `holding_period`-day forward returns
- `universe_median_returns` — 1-day median across the whole scored universe
- `rebalance_results` — per-day snapshots (`RebalanceSnapshot`: tickers, scores, forward returns, IC)
- `scored_frames` — optional (pass `retain_scored_frames=True`) — full scored DataFrame per day, needed for IC-by-decile and tail concentration diagnostics

## Data prerequisites

The harness itself is pure pandas, but running a backtest end-to-end needs two external data caches to be populated first.

### OHLCV via Lean CSV

The default scorers read OHLCV from `~/.alphalens/lean/data/equity/usa/daily/`. The top-level `CLAUDE.md` has the full setup: install Docker Desktop, set `POLYGON_API_KEY`, then run the Polygon → Lean CSV sync (a one-time ~100-minute bootstrap at 5 req/min on Polygon's free Basic tier, then daily incremental updates of a single grouped-daily call).

If the cache is empty, `load_lean_histories()` returns empty DataFrames and `BacktestEngine` raises `"No trading days found for benchmark ..."`. Fix: populate the cache first.

### Fama-French factor files

`factor_analysis.run_carhart_attribution` calls `factors.load_carhart_daily`, which reads Ken French's CSV files from `~/.alphalens/factors/`. These are **not** auto-downloaded. Grab them manually (a quarterly refresh is enough):

- `F-F_Research_Data_5_Factors_2x3_daily_CSV.zip` — Mkt-RF, SMB, HML, RMW, CMA, RF
- `F-F_Momentum_Factor_daily_CSV.zip` — UMD / Mom (the momentum factor)
- `12_Industry_Portfolios_daily_CSV.zip` — optional, for industry-adjusted Carhart

All three are free at [Ken French's data library](https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html). Unzip into `~/.alphalens/factors/` and you are done. If the files are missing, `factors.py` raises `FileNotFoundError` with the exact path it was looking for.

## Usage

### CLI (production)

```bash
# Layer 2b themed + momentum scorer (the default wiring)
.venv/bin/alphalens backtest \
  --start 2021-04-19 --end 2026-04-17 \
  --top-n 5 --holding 5 --weighting linear \
  --diagnose \
  --report docs/backtest/my_run.md

# Layer 2c lean (archived — diagnostic only)
.venv/bin/alphalens backtest --scorer lean ...
```

Default outputs:
- `docs/backtest/mvp1_report.md` — markdown with summary, regime breakdown, factor attribution, cost table, diagnostics
- `--csv path.csv` — optional per-rebalance CSV with portfolio returns, IC, top-N

### Programmatic

```python
from datetime import date
from alphalens.backtest.engine import BacktestEngine
from alphalens.data.store.history import HistoryStore
from alphalens.archive.screeners.themed.backtest_adapter import momentum_scorer_adapter
from alphalens.archive.screeners.themed.config import THEMED_DEFAULTS
from alphalens.archive.screeners.lean.lean_csv_loader import load_lean_histories
from alphalens.archive.screeners.lean.config import DATA_DIR

universe = ["NVDA", "AMD", "AVGO", ...]  # add the benchmark separately
store = HistoryStore(load_lean_histories(DATA_DIR, universe + ["SPY"]))

engine = BacktestEngine(
    store,
    scorer=momentum_scorer_adapter,
    scorer_config=dict(THEMED_DEFAULTS, benchmark="SPY"),
    top_n=5, holding_period=5, benchmark="SPY",
    screener_tickers=universe, weighting="linear",
    retain_scored_frames=True,  # required for IC-by-decile
)
report = engine.run(start=date(2021, 4, 19), end=date(2026, 4, 17))
```

From there:
- `alphalens.backtest.report.build_summary(report)` → `BacktestSummary` (Sharpe gross/moderate, IC, IC t-stat, turnover)
- `alphalens.backtest.factor_analysis.run_carhart_attribution(report.portfolio_returns, factors)` → `[CAPM, FF3, Carhart-4F]` with HAC t-stats
- `alphalens.backtest.regime.regime_breakdown(returns, ic, median, labels)` → per-regime Sharpe / IC

## Adding a new scorer

1. Write `scorer(histories, config) -> DataFrame[ticker, score]`. Pure pandas, no I/O.
2. Optionally write an adapter if you need column renaming or benchmark wiring — see `screeners/themed/backtest_adapter.py` for a reference.
3. Register the pipeline in `alphalens/registry.py` if the scorer has its own pipeline and CLI.
4. Run it through the CLI (`--scorer <name>`) or programmatically.

### Deploy gates (heuristics, not enforced code)

> These are the thresholds I apply manually when deciding whether a validated strategy is ready for launchd. **Nothing in the codebase enforces them at runtime.** The decision matrix in `report.py` uses a permissive `sharpe_moderate > 0.3` as the MVP1 "paper-trade gate" — a much weaker bar than the production-deploy target below.

Thresholds I use for production deploy (stricter than the MVP1 paper-trade gate):

- Net Sharpe > 1.0 (moderate cost profile)
- FF3 α t-stat > 2.0 HAC
- Rank IC t-stat > 1.5
- Carhart-4F α t-stat > 1.5 HAC (alpha survives control for the UMD momentum factor)
- Industry-adjusted Carhart α t-stat > 1.5 (the edge is stock selection, not sector timing)

Layer 2c failed all of them. Layer 2b passed all of them, and an augmented survivorship test strengthened rather than weakened the result.

## Factor attribution methodology

Three specifications built incrementally (CAPM → FF3 → Carhart-4F):

- **CAPM**: `y = α + β_Mkt · (Mkt - RF)`
- **FF3**: `+ β_SMB · SMB + β_HML · HML`
- **Carhart-4F**: `+ β_UMD · UMD` (the momentum factor — if α collapses here, the strategy was repackaged momentum-factor beta, not independent edge)

All three use **Newey-West HAC** covariance (`maxlag = int(4·(n/100)^(2/9))`, about 10 for 1260 trading days). Skipping HAC inflates significance because daily OLS residuals are autocorrelated.

**Industry controls** (`factors.load_industry12_daily`): the 12 FF industry portfolios as additional regressors. If Carhart α disappears once sector controls are in, the edge is sector timing, not stock selection. See `scripts/revalidate_carhart.py` for the full sequence on Layer 2b.

## Cost model

**Flat** (default, production):
- 75 / 100 / 150 bps annualised drag (aggressive / moderate / conservative)
- Scaled by turnover: stable top-N portfolios see less drag than high-churn ones
- `cost_sensitivity_table()` returns a 4-row frame (gross / aggressive / moderate / conservative) with Sharpe and annualised return per profile

**Per-ticker** (on the `feature/per-ticker-cost-model` branch, **not on main**):
- EDGE spread estimator (Ardia-Guidotti-Kroencke 2024) + Almgren-Chriss market impact
- Empirically **does not work** on our universe: a 68× overestimate on AAPL. See `project_per_ticker_cost_findings.md` in memory — the per-ticker drag is ~95% artifact without real tick data from Polygon Advanced ($199/mo).

## Module guide

| File | Purpose |
|---|---|
| `engine.py` | `BacktestEngine`, `RebalanceSnapshot`, `BacktestReport` — the core replay loop |
| `history_store.py` | `HistoryStore` — point-in-time OHLCV cache + forward-return lookup |
| `weighting.py` | `compute_position_weights(n, scheme)` — `equal` / `linear` / `conviction`. Linear is the best performer (+7% Sharpe, +27% Calmar vs equal per the 2026-04-19 sweep) |
| `metrics.py` | Sharpe, IC + t-stat + rolling, decile spread, max DD, Calmar, Herfindahl concentration |
| `cost_model.py` | Flat-bps drag + sensitivity table across gross / aggressive / moderate / conservative |
| `factor_analysis.py` | OLS wrappers + `run_carhart_attribution` + rolling regression + HAC lag rule |
| `factors.py` | Loaders for Fama-French factors (`FF3`, `Carhart-4F`, `12_Industries`) from local Ken French CSVs |
| `regime.py` | Bull / bear / flat classifier on trailing benchmark return + per-regime metric breakdown |
| `diagnostics.py` | IC-by-decile, tail-concentration score, vol decomposition by regime |
| `theme_analysis.py` | Theme HHI + dominant theme per day + concentration alerts (Layer 2b specific) |
| `historical_validation.py` | `evaluate_historical_picks(picks, scorer_fn)` — pluggable LLM / rule scorer evaluation + decision matrix |
| `llm_scorers.py` | Reference LLM scorers: Gemini Flash tractability, hybrid rule → Gemini, TradingAgents reduced |
| `report.py` | `build_summary`, `write_markdown_report`, `rebalance_results_to_dataframe` |

## Known limitations

- **Universe survivorship**: the production universe is pre-selected after the fact. Survivorship probe "Test B" (augmented backtest, 113 → 163 names including delisted) showed the bias is *opposite* — α strengthened (Carhart t 2.62 → 2.99 HAC). A point-in-time reconstruction ("Test A") is still pending.
- **Adjusted prices**: Lean CSV uses adjusted closes (factor files). Per-ticker cost validation showed adjusted prices underestimate EDGE spread by 10–20% vs raw. Irrelevant for the flat cost model; a blocker for per-ticker.
- **Turnover approximation**: `cost_model.apply()` falls back to 100% turnover when no per-day turnover series is passed. That overestimates drag. `cost_sensitivity_table` uses this fallback; real deploy runs pass `report.turnover`.
- **1-day-Sharpe vs N-day-IC horizons**: `portfolio_returns` is 1-day forward (for Sharpe); `ic_series` is N-day forward (for signal quality). Don't mix them — using N-day overlapping returns in Sharpe understates σ and inflates Sharpe.

## Key validations (archive)

Reports in `docs/backtest/`:
- `mvp1_5year.md` — full Layer 2c validation (failed, archived)
- `mvp1_5year_daily.csv` — daily portfolio returns for Layer 2c
- `mvp1_baseline.md`, `mvp1_5year_iwm.md` — benchmark variants
- `early_stage_comparison.md` — early-stage scorer vs momentum scorer
- `layer2b_survivorship.md` — augmented backtest with delisted names
- `llm_filter_{baseline_rule,gemini}.{md,csv}` — Phase 0 LLM filter validation

Memory notes with the decisions behind those numbers:
- `project_mvp1_backtest_findings.md`
- `project_weighting_sweep_findings.md`
- `project_carhart_revalidation.md`
- `project_survivorship_probe.md`
- `project_theme_expansion_v1.md`
- `project_llm_prescreen_validation.md`
