# AlphaLens

Stock analysis pipeline for active investing — combines real-time event detection, quantitative screening, and multi-agent LLM analysis to surface investment opportunities.

> **Status**: private, solo developer, macOS-only (runs under `launchd`). Built around [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents), vendored as a `git subtree --squash` at `TradingAgents/`.

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
```

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
- `alphalens` — my CLI (watchdog subcommands)
- `tradingagents` — upstream's interactive analysis menu

```bash
# Deep analysis of a single ticker (ad-hoc Gemini run)
.venv/bin/python run_gemini.py

# Upstream TradingAgents interactive menu (their flow for deep analysis)
.venv/bin/tradingagents

# Layer 1 — EDGAR poll once, classify, dispatch
.venv/bin/alphalens watchdog run-once

# Layer 1 — drain auto-trigger queue (feeds Layer 3)
.venv/bin/alphalens watchdog process-queue

# Layer 2b — daily momentum scan, send Telegram report
.venv/bin/alphalens watchdog momentum-screen --dry-run

# Status: queue, digest buffer, dedup
.venv/bin/alphalens watchdog status

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
alphalens/                             ← my code (Python package)
├── watchdog/                          Layer 1: EDGAR detection + classifier + dispatch + queue/worker
├── prescreener/                       Layer 2a: S&P 500 scoring (technical, fundamental, volume, composite ranker)
├── momentum_screener/                 Layer 2b: theme-based momentum on curated YAML universe
└── config_gemini.py                   shared Gemini TradingAgentsGraph config

alphalens_cli/                         ← my CLI (separate package to avoid collision with TradingAgents/cli/)
├── main.py                            entry point — `alphalens` console script
└── watchdog_main.py                   Typer sub-app for watchdog subcommands

TradingAgents/                         ← upstream vendored via git subtree --squash
├── tradingagents/                     their Python package (agents, graph, llm_clients, dataflows)
├── cli/                               their interactive menu (reachable via `tradingagents` console script)
├── main.py, Dockerfile, ...           their project-root files, isolated here
└── pyproject.toml                     their config (active via uv editable install)

launchd/                               macOS scheduled jobs (plists + bash wrappers)
tests/                                 unittest suite (258 tests)
```

See `CLAUDE.md` for detailed agent flow and configuration reference.

---

## Configuration

**LLM config**: `alphalens/config_gemini.py` (single source of truth — both `run_gemini.py` and the watchdog worker route through `build_gemini_config()`).

**Data vendors**: `core_stock_apis` and `technical_indicators` use yfinance (free). `fundamental_data` and `news_data` use Alpha Vantage (requires key).

**Momentum universe**: `alphalens/momentum_screener/universe.yaml` — curated tickers grouped by theme (ai, quantum, etc.).

**Portfolio**: `~/.alphalens/watchdog/portfolio.yaml` — your held + watchlist tickers for Layer 1 dispatch routing.

---

## Runtime data

Lives outside the repo, split between two directories that survive git operations:

```
~/.alphalens/                       ← AlphaLens runtime (mine)
└── watchdog/
    ├── seen_events.db              SQLite — EDGAR event dedup
    ├── auto_trigger_queue.db       SQLite — Layer 1 → Layer 3 queue
    ├── digest.db                   SQLite — quiet-hour digest buffer
    ├── portfolio.yaml              your held + watchlist
    ├── company_tickers.json        SEC CIK mapping (cached)
    └── {detect,worker,momentum}.{log,err}

~/.tradingagents/                   ← upstream TradingAgents runtime (hardcoded in their code)
├── cache/                          yfinance OHLCV cache
└── logs/                           TradingAgents full-state JSON logs
```

---

## Development

- **Package manager**: `uv` (not pip / poetry)
- **Testing**: unittest (not pytest) — `python -m unittest discover tests`
- **Commits**: Conventional Commits enforced (`feat(scope):`, `fix(scope):`, `refactor(scope):`, etc.)
- **New components always go in `alphalens/<name>/`** — never in `TradingAgents/` (upstream territory) or at top level

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
