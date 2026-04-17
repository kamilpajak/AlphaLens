# AlphaLens

Stock analysis pipeline for active investing — combines real-time event detection, quantitative screening, and multi-agent LLM analysis to surface investment opportunities.

> **Status**: private, solo developer, macOS-only (runs under `launchd`). Built on top of a fork of [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents).

---

## Architecture

Four layers, each narrowing the candidate funnel before the expensive final stage.

```
Layer 1 — watchdog             → SEC EDGAR event detection           (real-time,  <1 min lag)
Layer 2a — prescreener         → S&P 500 fundamentals + technicals   (~30 min, ad-hoc)
Layer 2b — momentum screener   → curated YAML universe, theme-based  (daily 22:00)
Layer 3 — TradingAgents        → multi-agent LLM deep analysis       (~15 min / ticker)
          → final rating: BUY / OVERWEIGHT / HOLD / UNDERWEIGHT / SELL
```

**Design rationale**: Layer 3 is slow and expensive. Layers 1–2b filter the universe to high-conviction candidates so the LLM stage sees only what's worth analyzing. Layer 1 also auto-triggers Layer 3 when a material SEC filing lands on a held or watchlisted ticker.

---

## Quickstart

### Prerequisites

- macOS (launchd scheduling assumed; other platforms can still run the CLI manually)
- Python 3.13+ — managed via [`uv`](https://github.com/astral-sh/uv)
- API keys: Google AI (Gemini), Alpha Vantage, Telegram bot token + chat ID

### Setup

```bash
git clone git@github.com:kamilpajak/AlphaLens.git
cd AlphaLens

uv venv
uv pip install -e .
```

Create `.env` at repo root (see `.env.example`):

```
GOOGLE_API_KEY=...
ALPHA_VANTAGE_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

Populate portfolio at `~/.tradingagents/watchdog/portfolio.yaml`:

```yaml
held:
  - AAPL
  - MSFT
watchlist:
  - NVDA
  - GOOGL
```

### Running things

```bash
# Deep analysis of a single ticker (Gemini)
.venv/bin/python run_gemini.py

# Interactive CLI (upstream TradingAgents menu)
.venv/bin/tradingagents

# Layer 1 — EDGAR poll once, classify, dispatch
.venv/bin/tradingagents watchdog run-once

# Layer 1 — drain auto-trigger queue (feeds Layer 3)
.venv/bin/tradingagents watchdog process-queue

# Layer 2b — daily momentum scan, send Telegram report
.venv/bin/tradingagents watchdog momentum-screen --dry-run

# Status: queue, digest buffer, dedup
.venv/bin/tradingagents watchdog status

# Tests (unittest, not pytest)
.venv/bin/python -m unittest discover tests -v
```

### Scheduled jobs (launchd)

Three jobs in `launchd/`:

| Job | Interval | Purpose |
|---|---|---|
| `com.alphalens.watchdog.detect` | every 15 min | Layer 1 EDGAR poll |
| `com.alphalens.watchdog.worker` | every 5 min  | Drain auto-trigger queue |
| `com.alphalens.watchdog.momentum` | daily 22:00 | Layer 2b momentum scan |

Install:

```bash
cp launchd/com.alphalens.watchdog.*.plist ~/Library/LaunchAgents/
for job in detect worker momentum; do
  launchctl load ~/Library/LaunchAgents/com.alphalens.watchdog.${job}.plist
done
```

---

## Components

```
alphalens/                             ← my code
├── watchdog/                          Layer 1: EDGAR detection + classifier + dispatch + queue/worker
├── prescreener/                       Layer 2a: S&P 500 scoring (technical, fundamental, volume, composite ranker)
├── momentum_screener/                 Layer 2b: theme-based momentum on curated YAML universe
└── config_gemini.py                   shared Gemini TradingAgentsGraph config

tradingagents/                         ← upstream fork (TauricResearch/TradingAgents)
├── agents/                            analysts, researchers, traders, risk managers, portfolio manager
├── graph/                             LangGraph state graph + debate flow control
├── llm_clients/                       provider adapters (OpenAI-compat, Anthropic, Google + 429 retry patch)
└── dataflows/                         yfinance + Alpha Vantage adapters

cli/                                   Typer CLI — upstream menu + my watchdog subcommands
launchd/                               macOS scheduled jobs (plists + bash wrappers)
tests/                                 unittest suite (258 tests)
```

See `CLAUDE.md` for detailed agent flow and configuration reference.

---

## Configuration

**LLM config**: `alphalens/config_gemini.py` (single source of truth — both `run_gemini.py` and the watchdog worker route through `build_gemini_config()`).

**Data vendors**: `core_stock_apis` and `technical_indicators` use yfinance (free). `fundamental_data` and `news_data` use Alpha Vantage (requires key).

**Momentum universe**: `alphalens/momentum_screener/universe.yaml` — curated tickers grouped by theme (ai, quantum, etc.).

**Portfolio**: `~/.tradingagents/watchdog/portfolio.yaml` — your held + watchlist tickers for Layer 1 dispatch routing.

---

## Runtime data

Lives outside the repo in `~/.tradingagents/` — survives git operations:

```
~/.tradingagents/
├── watchdog/
│   ├── seen_events.db              SQLite — EDGAR event dedup
│   ├── auto_trigger_queue.db       SQLite — Layer 1 → Layer 3 queue
│   ├── digest.db                   SQLite — quiet-hour digest buffer
│   ├── portfolio.yaml              your held + watchlist
│   ├── company_tickers.json        SEC CIK mapping (cached)
│   └── {detect,worker,momentum}.{log,err}
├── cache/                          yfinance OHLCV cache
└── logs/                           TradingAgents full-state JSON logs
```

---

## Development

- **Package manager**: `uv` (not pip / poetry)
- **Testing**: unittest (not pytest) — `python -m unittest discover tests`
- **Commits**: Conventional Commits enforced (`feat(scope):`, `fix(scope):`, `refactor(scope):`, etc.)
- **New components always go in `alphalens/<name>/`** — never in `tradingagents/` (upstream territory) or top-level

See `docs/architecture.mmd.txt` for a mermaid diagram of the layer interactions.

---

## Upstream relationship

AlphaLens is built on a fork of [`TauricResearch/TradingAgents`](https://github.com/TauricResearch/TradingAgents) (v0.2.3). The upstream framework powers Layer 3 deep analysis.

- `origin` — `kamilpajak/AlphaLens` (this repo)
- `upstream` — `TauricResearch/TradingAgents`

To pull upstream updates: `git fetch upstream && git merge upstream/main` (expect conflicts in `cli/main.py`, `pyproject.toml`, `tradingagents/llm_clients/google_client.py`).

Upstream's original README is preserved at [`docs/UPSTREAM_README.md`](docs/UPSTREAM_README.md).

---

## License

Apache License 2.0 — inherited from upstream TradingAgents. AlphaLens additions are released under the same license.
