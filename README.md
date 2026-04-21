# AlphaLens

Stock analysis pipeline for active investing — combines real-time event detection, quantitative screening, and multi-agent LLM analysis to surface investment opportunities.

> **Status**: private, solo developer, macOS-only (runs under `launchd`). Built around [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents), vendored as a `git subtree --squash` at `TradingAgents/`.

---

## Architecture

The pipeline filters a broad universe of stocks down to a handful of candidates for expensive final LLM analysis. **One deployed screener (Layer 2b themed) + Layer 1 event-driven** after rigorous validation; Layer 2a kept as manual ad-hoc, Layer 2c archived:

```
Layer 1 — watchdog          → SEC EDGAR event detection            (real-time, <1 min)  ─┐
Layer 2a — prescreener      → S&P 500 fundamentals (unvalidated)   (~30 min, ad-hoc)    ─┤
Layer 2b — themed screener  → curated YAML universe (validated)    (daily 22:00 CET)    ─┼─► CandidateQueue ─► AnalysisWorker ─► Layer 3
Layer 2c — Lean screener    → ARCHIVED (failed 5-year backtest)                         ─┘     (SQLite)         (budget + retry)    TradingAgents
                                                                                                                                   → BUY / OVERWEIGHT /
                                                                                                                                     HOLD / UNDERWEIGHT / SELL
```

**Validation status** (5-year backtest 2021-04 to 2026-04):
- **Layer 2b** ✅ validated: Sharpe 1.53 net, FF3 alpha +96% annualized t-stat +2.60, IC t-stat +1.83 (all above deploy gates)
- **Layer 2c** ❌ archived: Sharpe 0.25 net, FF3 alpha t-stat 0.14 — no statistical alpha, plist in `launchd/archived/`
- **Layer 2a** ⚠️ unvalidated: fundamentals-heavy scorer requires point-in-time Compustat / Polygon Advanced data; use as ad-hoc manual tool

**Post-hoc audit** (Perplexity's 3 flagged gaps, reports in `docs/backtest/`):
- **PIT survivorship** ✅ PASS (`survivorship_pit_a.md`): 0 / 5155 picks delisted within 30/90/180d vs 0.88% universe base rate — the scorer actively avoids names about to die.
- **Walk-forward OOS** ✅ PASS (`walk_forward.md`): 38 rolling 252-day windows, 86.84% with Sharpe > 0.5, 63.16% with Carhart α_t > 1.5 HAC, zero consecutive-negative-Sharpe stretches.
- **Cost validation / scale-path** ❌ FAIL at $10M AUM (`cost_validation.md`): 8.87% of pick-days would require > 15% of daily volume. Strategy has a hard AUM ceiling < $10M at the top-5 daily-rebalance configuration. Flat 100 bps remains the production cost model.

**Unified handoff**: every screener emits `Candidate(ticker, source, priority, payload, dedup_key)` into `~/.alphalens/candidates.db`. The worker drains FIFO within priority (watchdog_sec=0 > momentum=10 > lean=15 > prescreener=20), applies a daily budget cap, retries with exponential backoff, and moves persistent failures to DLQ (`status='dead'` after 5 attempts). The Layer 2c `backtest/` submodule is actively reused for Layer 2b validation.

---

## Quickstart

### Prerequisites

- macOS (launchd scheduling assumed; other platforms can still run the CLI manually)
- Python 3.13 (not 3.14 yet — transitive dep `tiktoken 0.9.0` has no 3.14 prebuilt wheel, and we don't want a Rust toolchain on the critical path) — managed via [`uv`](https://github.com/astral-sh/uv)
- API keys: Google AI (Gemini), Alpha Vantage, Telegram bot token + chat ID

### Setup

```bash
git clone git@github.com:kamilpajak/AlphaLens.git
cd AlphaLens

uv venv --python 3.13       # tiktoken has no 3.14 prebuilt wheel
uv sync                      # installs both alphalens and tradingagents editable via tool.uv.sources
```

Create `.env` at repo root (see `.env.example`):

```
GOOGLE_API_KEY=...
ALPHA_VANTAGE_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
POLYGON_API_KEY=...            # free "Stocks Basic" tier — https://polygon.io
```

Layer 2c (Lean screener — archived) requires **Docker Desktop** only if the strategy is ever revived. The plist currently lives in `launchd/archived/`, and `POLYGON_API_KEY` is already wired up for the backtest harness.

Populate portfolio at `~/.alphalens/watchdog/portfolio.yaml`:

```yaml
held:
  - AAPL
  - MSFT
watchlist:
  - NVDA
  - GOOGL
```

### Running things

Two console scripts are installed:
- `alphalens` — my CLI (groups: `watchdog`, `queue`, `themed`, `research`; top-level: `analyze`, `status`, `backtest`)
- `tradingagents` — upstream's interactive analysis menu

```bash
# Deep analysis of a single ticker (Layer 3 ad-hoc, Gemini config)
.venv/bin/alphalens analyze TSHA
.venv/bin/python run_gemini.py                 # alternative no-CLI entry point

# Upstream TradingAgents interactive menu (their flow for deep analysis)
.venv/bin/tradingagents

# Layer 1 — EDGAR poll once, classify, dispatch (SEC events → queue)
.venv/bin/alphalens watchdog run-once

# Drain the candidate queue — runs Layer 3 on whichever candidates are waiting
.venv/bin/alphalens queue process

# Layer 2b — themed universe scan; --analyze also submits top-N to queue
.venv/bin/alphalens themed screen --dry-run
.venv/bin/alphalens themed screen --analyze --dry-run
.venv/bin/alphalens themed screen --scorer early-stage       # base-breakout scorer

# Monitoring Layer 2b (last 90 days of runs — theme HHI, staleness, turnover)
.venv/bin/alphalens themed status --days 90

# Backtest (5-year, with diagnostics + factor decomposition)
.venv/bin/alphalens backtest --start 2021-04-19 --end 2026-04-17 --diagnose
.venv/bin/alphalens backtest --scorer lean                   # re-examine archived Layer 2c

# Research / audit (diagnostic tools, write reports to docs/backtest/)
.venv/bin/alphalens research survivorship-pit                # PIT universe reconstruction (Test A-lite)
.venv/bin/alphalens research walk-forward                    # rolling OOS stability test
.venv/bin/alphalens research cost-validation                 # tiered flat-bps + scale-path gate
.venv/bin/alphalens research validate-llm-filter --scorer rule   # LLM filter validation

# Status: queue breakdown (pending/in_progress/done/dead), digest buffer, dedup
.venv/bin/alphalens status

# Inspect the queue directly
sqlite3 ~/.alphalens/candidates.db \
  "SELECT id, ticker, source, priority, status, decision FROM candidates ORDER BY id DESC LIMIT 20;"

# Tests (unittest, not pytest)
.venv/bin/python -m unittest discover tests -v
```

### Scheduled jobs (launchd)

Four jobs in `launchd/`:

| Job | Interval | Purpose |
|---|---|---|
| `com.alphalens.watchdog.detect` | every 15 min  | Layer 1 EDGAR poll → submit Candidates |
| `com.alphalens.watchdog.worker` | every 5 min   | Drain `candidates.db` → Layer 3 |
| `com.alphalens.watchdog.themed` | daily 22:00 CET | Layer 2b themed scan (Telegram + optional `--analyze`) |
| ~~`com.alphalens.watchdog.lean`~~ | ~~daily 23:30 CET~~ | **Archived** — failed 5-year validation (plist w `launchd/archived/`) |

Install:

```bash
cp launchd/com.alphalens.watchdog.*.plist ~/Library/LaunchAgents/
for job in detect worker themed; do
  launchctl load ~/Library/LaunchAgents/com.alphalens.watchdog.${job}.plist
done
```

---

## Components

```
alphalens/                             ← my code (Python package)
├── candidates.py                      shared domain model — Candidate, AnalysisResult, CandidateSink Protocol
├── queue.py                           SQLite priority queue (CandidateQueue) with dedup + retry + DLQ
├── worker.py                          AnalysisWorker — drains queue, budget guard, notifier
├── runner.py                          TradingAgentsRunner — only place constructing TradingAgentsGraph
├── registry.py                        SCREENERS dict — register a new screener with one line
├── config_gemini.py                   shared Gemini TradingAgentsGraph config
├── watchdog/                          Layer 1: EDGAR detection + classifier + dispatch
├── backtest/                          screener-agnostic backtest harness
├── tick_data/                         tick-level trade data loader (Polygon)
└── screeners/                         pipeline strategies
    ├── themed/                        Layer 2b: curated YAML universe + pluggable scorer (momentum|early-stage)
    ├── prescreener/                   Layer 2a: S&P 500 scoring (technical, fundamental, volume, composite)
    └── lean/                          Layer 2c — ARCHIVED: Polygon sync + QuantConnect Lean in Docker

alphalens_cli/                         ← my CLI (separate package to avoid collision with TradingAgents/cli/)
├── main.py                            entry point — `alphalens` console script (root Typer)
└── commands/                          one file per group/command
    ├── analyze.py, status.py, backtest.py   top-level commands
    ├── watchdog.py                          Layer 1 group (run-once)
    ├── queue.py                             Layer 3 ops group (process, scorer-stats)
    ├── themed.py                            Layer 2b group (screen, status)
    └── research.py                          research group (validate-llm-filter)

TradingAgents/                         ← upstream vendored via git subtree --squash
├── tradingagents/                     their Python package (agents, graph, llm_clients, dataflows)
├── cli/                               their interactive menu (reachable via `tradingagents` console script)
├── main.py, Dockerfile, ...           their project-root files, isolated here
└── pyproject.toml                     their config (active via uv editable install)

launchd/                               macOS scheduled jobs (plists + bash wrappers)
tests/                                 unittest suite (758 tests)
```

See `CLAUDE.md` for detailed agent flow and configuration reference.

---

## Configuration

**LLM config**: `alphalens/config_gemini.py` (single source of truth — both `run_gemini.py` and the watchdog worker route through `build_gemini_config()`).

**Data vendors**: `core_stock_apis` and `technical_indicators` use yfinance (free). `fundamental_data` and `news_data` use Alpha Vantage (requires key).

**Themed universe**: `alphalens/screeners/themed/universe.yaml` — curated tickers grouped by theme (quantum, AI, semis, nuclear, crypto, …).

**Lean universe** (archived): `alphalens/screeners/lean/lean_project/universe.yaml` — ~780 Russell-like US small/mid caps grouped by GICS sector (technology, healthcare, financials, …). Refresh quarterly.

**Lean scoring config** (archived): `alphalens/screeners/lean/config.py::LEAN_DEFAULTS` (host) + `lean_project/main.py::SCORER_CONFIG` (algo). Weights cover ROC(20/60), volume surprise, trend stack, breakout flag, near-high. Keep the two dicts in sync.

**Portfolio**: `~/.alphalens/watchdog/portfolio.yaml` — your held + watchlist tickers for Layer 1 dispatch routing.

---

## Runtime data

Lives outside the repo, split between directories that survive git operations:

```
~/.alphalens/                       ← AlphaLens runtime (mine)
├── candidates.db                   SQLite — unified screener→Layer 3 queue (all screeners)
├── watchdog/
│   ├── seen_events.db              SQLite — EDGAR event dedup
│   ├── digest.db                   SQLite — quiet-hour digest buffer
│   ├── portfolio.yaml              your held + watchlist
│   ├── company_tickers.json        SEC CIK mapping (cached)
│   └── {detect,worker,themed}.{log,err}
└── lean/
    ├── data/equity/usa/daily/      Lean-format OHLCV zips (populated by Polygon sync)
    ├── results/candidates.json     last Lean algo output (ranked tickers + metrics)
    ├── logs/                       Lean stdout/stderr per run
    ├── sync_state.json             last-synced trading date
    └── launchd.{log,err}

~/.tradingagents/                   ← upstream TradingAgents runtime (hardcoded in their code)
├── cache/                          yfinance OHLCV cache
└── logs/                           TradingAgents full-state JSON logs
```

---

## Development

- **Package manager**: `uv` (not pip / poetry)
- **Testing**: unittest (not pytest) — `python -m unittest discover tests`
- **Commits**: Conventional Commits enforced (`feat(scope):`, `fix(scope):`, `refactor(scope):`, etc.)
- **New screener pipelines go in `alphalens/screeners/<name>/`**, other components in `alphalens/<name>/` — never in `TradingAgents/` (upstream territory) or at top level

See `docs/architecture.mmd.txt` for a mermaid diagram of the layer interactions.

---

## Upstream relationship

AlphaLens vendors [`TauricResearch/TradingAgents`](https://github.com/TauricResearch/TradingAgents) (v0.2.3) at `TradingAgents/` via `git subtree --squash`. The upstream framework powers Layer 3 deep analysis and is editable-installed so `import tradingagents.*` works transparently.

To pull upstream updates:
```
git subtree pull --prefix=TradingAgents https://github.com/TauricResearch/TradingAgents.git main --squash
```
After each sync, reapply the single vendored patch: Gemini 429 retry logic in `TradingAgents/tradingagents/llm_clients/google_client.py` (~33 lines, stable region). Goal is to upstream this as a PR so future syncs replay cleanly.

Upstream's own README lives at [`TradingAgents/README.md`](TradingAgents/README.md).

---

## License

Apache License 2.0 — inherited from upstream TradingAgents. AlphaLens additions are released under the same license.
