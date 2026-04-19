# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**AlphaLens** is a stock analysis pipeline. Own code lives in `alphalens/`. Upstream **TradingAgents** (multi-agent LLM trading framework, v0.2.3) is vendored at `TradingAgents/` as a `git subtree --squash` from `TauricResearch/TradingAgents` and powers Layer 3 deep analysis. AlphaLens additions: Layer 1 SEC EDGAR watchdog (live), Layer 2b themed momentum screener (**validated alpha, live**), Layer 2a S&P 500 prescreener (unvalidated, manual ad-hoc use), Layer 2c Lean batch screener (**archived after failed 5-year validation** — infrastructure reusable, not deployed).

Project root belongs to AlphaLens. Upstream sits in its own subfolder — edit patches there, sync with `git subtree pull`. The top-level CLI package is named `alphalens_cli/` (not `cli/`) to avoid namespace collision with TradingAgents' own `cli/` package.

## Commands

```bash
# Setup (fresh clone) — requires Python 3.13 (tiktoken has no 3.14 prebuilt wheel)
uv venv --python 3.13
uv sync                               # installs both alphalens and tradingagents editable (via tool.uv.sources)

# Run tests (unittest, not pytest)
.venv/bin/python -m unittest discover tests -v

# Filter tests
.venv/bin/python -m unittest discover tests -p "test_prescreener_*" -v

# Watchdog CLI (Layer 1 + 2b + 2c)
.venv/bin/alphalens watchdog status                              # queue, digest, dedup stats
.venv/bin/alphalens watchdog run-once                            # Layer 1: poll EDGAR, classify, dispatch
.venv/bin/alphalens watchdog process-queue                       # drain unified candidate queue → Layer 3
.venv/bin/alphalens watchdog momentum-screen --dry-run           # Layer 2b report only, Telegram
.venv/bin/alphalens watchdog momentum-screen --analyze --dry-run # Layer 2b + submit top-N to queue
.venv/bin/alphalens watchdog momentum-status --days 90           # Layer 2b monitoring dashboard (theme HHI, staleness, turnover)
.venv/bin/alphalens watchdog backtest --start 2021-04-19 --end 2026-04-17 --diagnose   # Pełny 5-letni backtest + diagnostyki

# Layer 2c (archived — failed 5-year validation; plist moved to launchd/archived/):
# .venv/bin/alphalens watchdog lean-screen --sync-only             # Polygon → Lean CSV sync
# .venv/bin/alphalens watchdog lean-screen --dry-run               # full run, print top-N
# .venv/bin/alphalens watchdog lean-screen --analyze               # full run + submit to queue

# Ad-hoc Gemini deep analysis on one ticker
.venv/bin/python run_gemini.py

# Upstream TradingAgents interactive menu (their console script)
.venv/bin/tradingagents
```

## Architecture

### Layered pipeline

```
Layer 1 — watchdog           → SEC EDGAR event detection            (real-time,  <1 min) ─┐
Layer 2a — prescreener       → S&P 500 screen (unvalidated)         (~30 min, ad-hoc)    ─┤
Layer 2b — momentum screener → curated YAML universe, 4 themes      (daily 22:00 CET)    ─┼─► CandidateQueue ─► AnalysisWorker ─► Layer 3
Layer 2c — Lean screener     → ARCHIVED (failed 5-year validation)                       ─┘                        (TradingAgents)
                                                                                                                   BUY / OVERWEIGHT /
                                                                                                                   HOLD / UNDERWEIGHT / SELL
```

Every screener emits a `Candidate` (in `alphalens/candidates.py`) into the shared priority queue at `~/.alphalens/candidates.db`. The worker drains FIFO within priority (watchdog_sec=0 > momentum=10 > lean=15 > prescreener=20), hands each candidate to `TradingAgentsRunner`, and records outcomes inline. Retry with exponential backoff + DLQ (`status='dead'` after 5 attempts).

### TradingAgents agent flow (Layer 3)

```
Analyst Team (parallel, quick_think_llm)
  ├── Market Analyst     → technical indicators
  ├── Social Analyst     → sentiment
  ├── News Analyst       → news impact
  └── Fundamentals Analyst → financials
         │
         ▼
Research Team (debate, deep_think_llm): Bull ↔ Bear → Research Manager
         │
         ▼
Trader → investment plan
         │
         ▼
Risk Mgmt (3-way): Aggressive ↔ Neutral ↔ Conservative
         │
         ▼
Portfolio Manager → final 5-tier rating
```

### Key abstractions

**AlphaLens core** (`alphalens/`):
- **`candidates.py`** — `Candidate` (frozen dataclass: ticker, source, priority, payload, dedup_key), `AnalysisResult`, `CandidateSink` Protocol. Every screener produces Candidates; nothing else is allowed through to Layer 3.
- **`queue.py`** — `CandidateQueue` implementing `CandidateSink`. SQLite table `candidates` with `UNIQUE(dedup_key)`, priority + retry window scheduling, inline cost/duration columns. `default_queue_path()` → `~/.alphalens/candidates.db`.
- **`worker.py`** — `AnalysisWorker` drains the queue, respects daily budget, handles failure/retry/DLQ, notifies via injected sender.
- **`runner.py`** — `TradingAgentsRunner` is the only place that constructs `TradingAgentsGraph`. Builds per-source trigger context via `build_trigger_context()` (logged only; upstream PR tracks injection into graph state).
- **`registry.py`** — `SCREENERS` dict (name → pipeline class) + `SOURCE_PRIORITY` mapping. Add a new screener = one entry here.
- **`config_gemini.py`** — `build_gemini_config()` deep-copies upstream `DEFAULT_CONFIG` and overrides for Gemini. Used by `run_gemini.py` and `TradingAgentsRunner`.

**Lean screener** (`alphalens/lean_screener/`, Layer 2c — **ARCHIVED**):
- Strategia failed 5-year rigorous validation (Sharpe 0.25 net, FF3 α t-stat 0.14 = zero alpha).
- Infrastruktura pozostaje i jest aktywnie reużywana przez backtest harness (`backtest/engine.py`, `backtest/diagnostics.py`, `backtest/weighting.py`, `backtest/theme_analysis.py`), FF3 factors, Polygon sync.
- launchd plist przeniesiony do `launchd/archived/com.alphalens.watchdog.lean.plist` — nie ładuje się w produkcji.
- `registry.SCREENERS["lean"]` zostaje zarejestrowane (harm neutralny, można re-enable jednym ruchem gdy strategia zostałaby przeprojektowana).

**Backtest harness** (`alphalens/lean_screener/backtest/`) — reusable niezależnie od Layer 2c:
- **`history_store.py`** — in-memory OHLCV cache z point-in-time truncation, forward returns computation.
- **`engine.py`** — `BacktestEngine` replay loop z `DailyResult` + `BacktestReport`, weighting schemes (equal/linear/conviction).
- **`weighting.py`** — `compute_position_weights(n, scheme)` dla position-sizing (linear najlepiej performs per 2026-04-19 sweep).
- **`metrics.py`** — Sharpe, IC + t-stat + rolling, decile spread, max DD, Calmar, concentration.
- **`cost_model.py`** — 75/100/150 bps annual drag scenarios.
- **`regime.py`** — bull/bear/flat classifier na trailing benchmark return.
- **`factor_analysis.py`** — Fama-French 3-factor regression przez `statsmodels` OLS.
- **`theme_analysis.py`** — HHI + dominant theme per day + concentration alerts.
- **`diagnostics.py`** — IC decomposition by decile, bear-regime vol decomposition.
- **`report.py`** — markdown + CSV + decision matrix generation.

**Upstream** (`TradingAgents/`):
- **LLM Factory** (`TradingAgents/tradingagents/llm_clients/factory.py`): provider-agnostic client creation
- **Vendor routing** (`TradingAgents/tradingagents/agents/utils/agent_utils.py`): `data_vendors` dict routes data by category
- **Memory** (`TradingAgents/tradingagents/agents/utils/memory.py`): BM25 similarity, no embeddings
- **State graph** (`TradingAgents/tradingagents/graph/setup.py`): LangGraph conditional edges

### Configuration

Base config: `TradingAgents/tradingagents/default_config.py` (upstream defaults, OpenAI-centric). **Always wrap with `alphalens.config_gemini.build_gemini_config()`** — it deep-copies DEFAULT_CONFIG and overrides for Gemini.

Key parameters (override as needed on returned dict):

- `llm_provider`: "google" (default from build_gemini_config), or "openai", "anthropic", etc.
- `deep_think_llm` / `quick_think_llm`: model IDs
- `max_debate_rounds` / `max_risk_discuss_rounds`: 1 (fast) – 5 (thorough)
- `backend_url`: **must be `None` for Google**
- `data_vendors`: yfinance (default) or alpha_vantage (fundamentals/news better)

## Upstream relationship

- `origin` → `kamilpajak/AlphaLens` (private, my repo)
- `upstream` → `TauricResearch/TradingAgents` (pulled via subtree)

**Pull upstream updates:**
```bash
git subtree pull --prefix=TradingAgents https://github.com/TauricResearch/TradingAgents.git main --squash
```
Reapply `fix(vendored)` patches afterward (currently: Gemini 429 retry in `TradingAgents/tradingagents/llm_clients/google_client.py`). Submit these as PRs upstream to shrink the patch surface.

## Output

TradingAgents state logs: `~/.tradingagents/logs/{TICKER}/TradingAgentsStrategy_logs/full_states_log_{DATE}.json`.

Generate human-readable reports:
```python
from cli.main import save_report_to_disk   # upstream util, resolves to TradingAgents/cli/main.py via editable install
```
(Path changed — this helper lives in the subtree's CLI, not mine.)

## Project stage conventions

- **Solo, early-stage, zero external users.** No backward-compatibility shims, no dual-path logic, no deprecated-but-still-works branches. Rename, refactor, drop old behavior in a single commit without aliases or fallbacks. If a runtime schema (SQLite, yaml) changes, drop/migrate the data — don't keep version markers.
- **Exception:** vendored patches in `TradingAgents/` must stay mergeable with upstream sync (that interface has an external stakeholder).
- **New components always go in `alphalens/<name>/`** — never into `TradingAgents/` (upstream territory), never at top level.

## Known issues

- **Gemini 429 RESOURCE_EXHAUSTED**: Google free tier has 1M input tokens/min on gemini-2.5-flash. Alpha Vantage fundamentals ~1.8MB trigger it. Custom retry in `TradingAgents/tradingagents/llm_clients/google_client.py` (10 retries, ~40s base delay). Planned upstream PR.
- **`backend_url` must be `None` for Google**: upstream DEFAULT_CONFIG has OpenAI URL → 404 for Google. `build_gemini_config()` handles this.
- **Prescreener (Layer 2a) unvalidated**: 45% fundamentals weight wymaga point-in-time historical data którego Polygon Starter ($29/mo) nie dostarcza. Backtest na samych technicals+volume byłby biased. Status: manual ad-hoc use only, **no performance guarantee**. Pełna walidacja wymaga upgrade do Polygon Advanced $199/mo albo alternatywa (Sharadar, FactSet).
- **Prescreener value traps**: low P/E in cyclicals (semis, materials) can be peak-earnings trap. Layer 3 catches these — pipeline works as designed.
- **Signal-context injection deferred**: `alphalens/runner.py::build_trigger_context(candidate)` formats a per-source trigger string (e.g. "Triggered by momentum screener: score=0.85, themes=AI") but only logs it. Injecting into the TradingAgents initial state requires a `trigger_context` kwarg on `propagate()` — tracked as a planned upstream PR to stay subtree-mergeable.
- **Cost tracking columns are placeholders**: `candidates.cost_usd` is written as `NULL` for now — TradingAgents doesn't expose token accounting. Duration + `model_used` populated on every successful run.
- **Layer 2c (Lean screener) archived**: failed 5-year rigorous validation (Sharpe 0.25 net, FF3 alpha t-stat 0.14). Kod w `alphalens/lean_screener/` pozostaje bo `backtest/` submodule jest aktywnie używany. Plist przeniesiony do `launchd/archived/`. Wszystkie notatki operacyjne poniżej (Docker, Polygon, Lean aux data) dotyczą **gdyby** strategia została wskrzeszona w przyszłości z lepszym designem — **nie używać obecnie w produkcji**.
- **Lean screener requires Docker + Polygon Basic key** (gdy wskrzesić): `lean-screen` shells out to `quantconnect/lean:latest` via `docker run`. Install/start Docker Desktop and set `POLYGON_API_KEY` (free Stocks Basic tier is enough — the grouped-daily endpoint is one call per trading day). First run bootstraps ~2 years of history at 5 req/min, ~100 min of wall time (one-time). `SCORER_CONFIG` in `lean_project/main.py` duplicates `LEAN_DEFAULTS` values because the algorithm runs inside Lean without the host package on its path — **keep the two in sync manually**.
- **Lean aux data must be extracted from the image once**: Lean expects `symbol-properties/`, `market-hours/`, `map_files/`, `factor_files/` alongside `equity/usa/daily/`. The Docker image ships them at `/Lean/Data/`, but our `-v /Data` bind mount shadows that path. One-time extraction (run once after `docker pull`, again after fresh image upgrades):
  ```bash
  docker run --rm --entrypoint sh -v ~/.alphalens/lean/data:/host quantconnect/lean:latest \
    -c 'for d in /Lean/Data/*/; do cp -rn "$d" /host/ 2>/dev/null || true; done'
  ```
  Symptom if missing: `FileNotFoundException: /Data/symbol-properties/symbol-properties-database.csv`.

## Environment

- API keys in `.env` at repo root (GOOGLE_API_KEY, ALPHA_VANTAGE_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, POLYGON_API_KEY)
- Google API key also in macOS Keychain under `google-api-key`
- Current LLM config: Gemini 3.1 Pro (deep) + Gemini 2.5 Flash (quick)
- Runtime data:
  - `~/.alphalens/candidates.db` — unified Layer 1+2a+2b+2c → Layer 3 queue (all screeners write here)
  - `~/.alphalens/watchdog/` — portfolio.yaml, EDGAR dedup, digest buffer, launchd logs
  - `~/.alphalens/lean/{data,results,logs}/` + `sync_state.json` — Lean CSV inputs, Docker outputs, launchd/Docker logs, last-synced date
  - `~/.tradingagents/{cache,logs}/` — upstream state (hardcoded in their code)
  - All survive git operations (outside repo)
