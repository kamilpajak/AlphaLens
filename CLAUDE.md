# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**AlphaLens** is a stock analysis pipeline. Own code lives in `alphalens/`. Upstream **TradingAgents** (multi-agent LLM trading framework, v0.2.3) is vendored at `TradingAgents/` as a `git subtree --squash` from `TauricResearch/TradingAgents` and powers Layer 3 deep analysis. AlphaLens additions: Layer 1 SEC EDGAR watchdog, Layer 2a S&P 500 prescreener, Layer 2b momentum screener.

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

# Watchdog CLI (Layer 1 + 2b)
.venv/bin/alphalens watchdog status
.venv/bin/alphalens watchdog run-once
.venv/bin/alphalens watchdog process-queue
.venv/bin/alphalens watchdog momentum-screen --dry-run

# Ad-hoc Gemini deep analysis on one ticker
.venv/bin/python run_gemini.py

# Upstream TradingAgents interactive menu (their console script)
.venv/bin/tradingagents
```

## Architecture

### Layered pipeline

```
Layer 1 — watchdog             → SEC EDGAR event detection          (real-time, <1 min)
Layer 2a — prescreener         → S&P 500 screen                     (~30 min, ad-hoc)
Layer 2b — momentum screener   → curated YAML universe, theme-based (daily 22:00)
Layer 3 — TradingAgents        → multi-agent LLM deep analysis      (~15 min/ticker)
          → BUY / OVERWEIGHT / HOLD / UNDERWEIGHT / SELL
```

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

### Key abstractions (paths under `TradingAgents/`)

- **LLM Factory** (`TradingAgents/tradingagents/llm_clients/factory.py`): provider-agnostic client creation
- **Vendor routing** (`TradingAgents/tradingagents/agents/utils/agent_utils.py`): `data_vendors` dict routes data by category
- **Memory** (`TradingAgents/tradingagents/agents/utils/memory.py`): BM25 similarity, no embeddings
- **State graph** (`TradingAgents/tradingagents/graph/setup.py`): LangGraph conditional edges
- **Gemini config** (`alphalens/config_gemini.py`): shared `build_gemini_config()` used by `run_gemini.py` and the watchdog worker

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

## Known issues

- **Gemini 429 RESOURCE_EXHAUSTED**: Google free tier has 1M input tokens/min on gemini-2.5-flash. Alpha Vantage fundamentals ~1.8MB trigger it. Custom retry in `TradingAgents/tradingagents/llm_clients/google_client.py` (10 retries, ~40s base delay). Planned upstream PR.
- **`backend_url` must be `None` for Google**: upstream DEFAULT_CONFIG has OpenAI URL → 404 for Google. `build_gemini_config()` handles this.
- **Prescreener value traps**: low P/E in cyclicals (semis, materials) can be peak-earnings trap. Layer 3 catches these — pipeline works as designed.

## Environment

- API keys in `.env` at repo root (GOOGLE_API_KEY, ALPHA_VANTAGE_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
- Google API key also in macOS Keychain under `google-api-key`
- Current LLM config: Gemini 3.1 Pro (deep) + Gemini 2.5 Flash (quick)
- Runtime data: `~/.alphalens/watchdog/` (my watchdog state — portfolio.yaml, SQLite dbs, launchd logs) and `~/.tradingagents/{cache,logs}/` (upstream — hardcoded in their code) — outside repo, survives git operations
