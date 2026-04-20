# alphalens/backtest — backtest harness

**Serce AlphaLens.** Każda decyzja deploy/reject dla Layer 2 strategii przechodzi przez tę infrastrukturę przed dotknięciem launchd'a. Odpowiedziała "tak" dla Layer 2b (themed, Sharpe 1.53 net / FF3 α_t 2.60 HAC) i "nie" dla Layer 2c (lean, Sharpe 0.25 / α_t 0.14) — tu się materializuje różnica między pomysłem a empiryczną walidacją.

Harness jest **screener-agnostic**: dowolna strategia implementująca `Scorer` protocol podpina się do tego samego silnika, raportów, factor attribution i cost model. Ta sama infrastruktura walidowała momentum, early-stage, Lean, Carhart revalidation, survivorship probes oraz evaluation LLM filtrów.

## Przepływ

```
OHLCV (Lean CSV zip)
       │
       ▼
HistoryStore  ← punkt wejścia, point-in-time truncation
       │
       │  + scorer: (histories, config) -> DataFrame[ticker, score]
       │  + scorer_config, top_n, holding_period, weighting, benchmark
       ▼
BacktestEngine.run(start, end)
       │
       │  daily loop:
       │    truncate_to(t) → scorer(histories) → top-N → forward_return(1d, Nd)
       │
       ▼
BacktestReport       ── portfolio_returns (Sharpe-ready, non-overlapping)
       │             ├─ portfolio_returns_holding (signal diagnostic, overlaps)
       │             ├─ ic_series (Rank IC per day)
       │             ├─ universe_median_returns
       │             └─ scored_frames (optional, for diagnostics)
       │
       ├── metrics.py            → Sharpe, IC+t-stat, Calmar, max DD
       ├── cost_model.py         → flat bps drag (75/100/150)
       ├── factor_analysis.py    → CAPM → FF3 → Carhart-4F + HAC + industry controls
       ├── regime.py             → bull/bear/flat breakdown
       ├── diagnostics.py        → IC by decile, bear-regime vol decomposition
       ├── theme_analysis.py     → HHI + dominant-theme concentration alerts
       ├── historical_validation → LLM filter evaluation harness
       └── report.py             → markdown + CSV + decision matrix
```

## Kontrakty (co musisz dać, co dostajesz)

### `Scorer` (wejście)

```python
Scorer = Callable[[Mapping[str, pd.DataFrame], Mapping], pd.DataFrame]
```

- **Dostaje**: `histories` (truncated point-in-time do `t`, column `close/open/high/low/volume`) + `config` dict.
- **Zwraca**: DataFrame z co najmniej `ticker` i `score`. Engine sam robi `sort_values("score", ascending=False).head(top_n)`.
- **Invariant**: engine ufnie przekazuje truncated histories — scorer nie może zaglądać w przyszłość. Wszystkie window'y (rolling SMA, ROC) muszą być obliczone na `histories[ticker]` bez rozszerzania.

Przykładowe adaptery:
- `alphalens.screeners.themed.backtest_adapter.momentum_scorer_adapter` — Layer 2b `MomentumScorer` (7-metric)
- `alphalens.screeners.themed.backtest_adapter.early_stage_scorer_adapter` — Layer 2b `EarlyStageScorer` (base-breakout)
- `alphalens.screeners.lean.lean_project.scorer.rank_universe` — archived Layer 2c (już zgodny z `Scorer`, bez adaptera)

### `HistoryStore` (dane)

In-memory, pure-pandas, zero I/O w hot loopie. Loading jest odpowiedzialnością callera:

```python
from alphalens.screeners.lean.lean_csv_loader import load_lean_histories
from alphalens.backtest.history_store import HistoryStore

histories = load_lean_histories(DATA_DIR, tickers)  # zip-CSV → dict[ticker, DataFrame]
store = HistoryStore(histories)
```

Dlaczego Lean zip-CSV a nie yfinance? Lean data ma stały format, adjusted prices z factor files, trading calendar, i jest offline-stable. yfinance podczas 5-letniego backtestu gubi nazwy (delisting, rate limits) co łamie reproducibility.

### `BacktestReport` (wyjście)

- `portfolio_returns` — 1-day forward return top-N **(Sharpe-ready, nie nakłada się)**
- `portfolio_returns_holding` — holding-period return top-N (overlapping, dla signal diagnostics, nie Sharpe)
- `ic_series` — Rank IC liczony po `holding_period`-day fwd return cross-sectionally
- `universe_median_returns` — 1-day median całego scorowanego uniwersum
- `daily_results` — lista per-day snapshot'ów (`DailyResult`: tickery, scores, fwd returns, IC)
- `scored_frames` — opcjonalnie (`retain_scored_frames=True`) — full DataFrame scored per day, dla IC-by-decile + tail concentration

## Uruchomienie

### CLI (produkcja)

```bash
# Layer 2b themed + momentum scorer (domyślne wiring)
.venv/bin/alphalens backtest \
  --start 2021-04-19 --end 2026-04-17 \
  --top-n 5 --holding 5 --weighting linear \
  --diagnose \
  --report docs/backtest/my_run.md

# Layer 2c lean (archived — diagnostic only)
.venv/bin/alphalens backtest --scorer lean ...
```

Wyjście domyślne:
- `docs/backtest/mvp1_report.md` — markdown z summary, regime breakdown, factor attribution, cost table, diagnostics
- `--csv path.csv` — opcjonalny daily CSV z portfolio returns, IC, top-N

### Programmatic

```python
from datetime import date
from alphalens.backtest.engine import BacktestEngine
from alphalens.backtest.history_store import HistoryStore
from alphalens.screeners.themed.backtest_adapter import momentum_scorer_adapter
from alphalens.screeners.themed.config import THEMED_DEFAULTS
from alphalens.screeners.lean.lean_csv_loader import load_lean_histories
from alphalens.screeners.lean.config import DATA_DIR

universe = ["NVDA", "AMD", "AVGO", ...]  # plus benchmark
store = HistoryStore(load_lean_histories(DATA_DIR, universe + ["SPY"]))

engine = BacktestEngine(
    store,
    scorer=momentum_scorer_adapter,
    scorer_config=dict(THEMED_DEFAULTS, benchmark="SPY"),
    top_n=5, holding_period=5, benchmark="SPY",
    screener_tickers=universe, weighting="linear",
    retain_scored_frames=True,  # potrzebne dla IC-by-decile
)
report = engine.run(start=date(2021, 4, 19), end=date(2026, 4, 17))
```

Dalej można:
- `alphalens.backtest.report.build_summary(report)` → `Summary` (sharpe, IC t-stat, turnover)
- `alphalens.backtest.factor_analysis.run_carhart_attribution(report.portfolio_returns, factors)` → `[CAPM, FF3, Carhart-4F]` × HAC t-stats
- `alphalens.backtest.regime.regime_breakdown(returns, ic, median, labels)` → per-regime Sharpe/IC

## Dodawanie nowego scorera

1. Napisz `scorer(histories, config) -> DataFrame[ticker, score]`. Pure pandas, no I/O.
2. Opcjonalnie napisz adapter jeśli potrzebujesz column renaming lub benchmark wiring — patrz `screeners/themed/backtest_adapter.py` jako referencja.
3. Zarejestruj pipeline w `alphalens/registry.py` (jeśli scorer ma własny pipeline + CLI).
4. Odpal backtest przez CLI (`--scorer <nazwa>`) albo programmatic.

**Gate do deploy** (z mojego validation workflow, nie formal reguła):
- Net Sharpe > 1.0 (moderate cost profile)
- FF3 α t-stat > 2.0 HAC
- Rank IC t-stat > 1.5
- Carhart-4F α t-stat > 1.5 HAC (alpha przeżywa kontrolę na UMD factor)
- Industry-adjusted Carhart α t-stat > 1.5 (edge to stock selection, nie sector tilt)

Layer 2c oblało wszystkie. Layer 2b przeszło wszystkie (plus augmented test z delisted names wzmocnił wyniki).

## Factor attribution methodology

Trzy specyfikacje budowane incrementally (CAPM → FF3 → Carhart-4F):
- **CAPM**: `y = α + β_Mkt·(Mkt-RF)`
- **FF3**: `+ β_SMB·SMB + β_HML·HML`
- **Carhart-4F**: `+ β_UMD·UMD` (momentum factor — jeśli α collapsuje tu, strategia to repackaged momentum beta, nie niezależny edge)

Wszystkie z **Newey-West HAC** (maxlag = `int(4·(n/100)^(2/9))` ≈ 10 dla 1260 dni). Brak HAC → fałszywa istotność bo daily errors są autocorrelated.

**Industry controls** (`factors.load_industry12_daily`): 12 FF industry portfolios jako dodatkowe regresory. Jeśli Carhart α zniknie po dodaniu sector controls → edge to timing, nie selection. Patrz `scripts/revalidate_carhart.py`.

## Cost model

**Flat** (default, production-ready):
- 75 / 100 / 150 bps annualized drag (aggressive / moderate / conservative)
- Skalowany turnover'em: drogie strategie high-churn widzą więcej drag niż stabilne top-N
- `cost_sensitivity_table()` zwraca frame 4×(sharpe, ann_return) dla wszystkich profili

**Per-ticker** (na branchu `feature/per-ticker-cost-model`, nie w production):
- EDGE spread estimator (Ardia-Guidotti-Kroencke 2024) + Almgren-Chriss market impact
- Empirycznie **nie działa** na naszym universum: 68× overestimate na AAPL. Per memory `project_per_ticker_cost_findings.md`: ~95% artifact bez ticks z Polygon Advanced ($199/mo).

## Moduły (crisp)

| Plik | Odpowiedzialność |
|---|---|
| `engine.py` | `BacktestEngine`, `DailyResult`, `BacktestReport` — core replay loop |
| `history_store.py` | `HistoryStore` — point-in-time OHLCV cache, forward-return lookup |
| `weighting.py` | `compute_position_weights(n, scheme)` — equal / linear / conviction. Linear best-performing (+7% Sharpe, +27% Calmar vs equal per 2026-04-19 sweep) |
| `metrics.py` | Sharpe, IC + t-stat + rolling, decile spread, max DD, Calmar, Herfindahl concentration |
| `cost_model.py` | Flat-bps drag + sensitivity table across gross/aggressive/moderate/conservative |
| `factor_analysis.py` | OLS wrappers + `run_carhart_attribution` + rolling regression + HAC maxlag rule |
| `factors.py` | Loader dla Fama-French factors (`FF3`, `Carhart-4F`, `12_Industries`) z Ken French website, cached locally |
| `regime.py` | Bull/bear/flat classifier oparty na trailing benchmark return + breakdown metryk |
| `diagnostics.py` | IC-by-decile, tail-concentration score, vol decomposition by regime |
| `theme_analysis.py` | Theme HHI + dominant theme per day + concentration alerts (Layer 2b specific) |
| `historical_validation.py` | `evaluate_historical_picks(picks, scorer_fn)` — pluggable LLM/rule scorer eval, decision matrix |
| `llm_scorers.py` | Reference LLM scorers: Gemini Flash tractability, hybrid rule→Gemini, TradingAgents reduced |
| `report.py` | `build_summary`, `write_markdown_report`, `daily_results_to_dataframe` |

## Znane ograniczenia

- **Universe survivorship**: production-universe pre-selected po fakcie. Survivorship probe Test B (augmented backtest 113→163 delisted+active) pokazał bias odwrotny — α okrzepła (t=2.62 → 2.99 HAC). Test A (point-in-time reconstruction) wciąż todo.
- **Adjusted prices**: Lean CSV używa adjusted closes (factor files). Per-ticker cost validation pokazała że adjusted underestimuje EDGE spread o 10–20% vs raw. Dla flat cost model nieistotne; dla per-ticker blokujące.
- **Turnover approximation**: fixed 100% assumption w `cost_model.apply()` gdy brak per-day turnover series. Daje overestimate drag. `cost_sensitivity_table` używa tego; realny deploy policzył z `report.turnover`.
- **One-day hold Sharpe vs N-day hold IC**: `portfolio_returns` to 1-day fwd (dla Sharpe), `ic_series` to N-day fwd (dla signal quality). Nie mieszać — overlapping daily Sharpe z N-day returns zaniża σ i inflate'uje Sharpe.

## Kluczowe walidacje (archive)

Reports w `docs/backtest/`:
- `mvp1_5year.md` — pełna Layer 2c walidacja (oblała, archived)
- `mvp1_5year_daily.csv` — daily portfolio returns dla Layer 2c
- `mvp1_baseline.md`, `mvp1_5year_iwm.md` — benchmark variants
- `early_stage_comparison.md` — early-stage vs momentum scorer
- `layer2b_survivorship.md` — augmented backtest z delisted names
- `llm_filter_{baseline_rule,gemini}.{md,csv}` — Phase 0 LLM filter validation

Memory notes z wnioskami:
- `project_mvp1_backtest_findings.md`
- `project_weighting_sweep_findings.md`
- `project_carhart_revalidation.md`
- `project_survivorship_probe.md`
- `project_theme_expansion_v1.md`
- `project_llm_prescreen_validation.md`
