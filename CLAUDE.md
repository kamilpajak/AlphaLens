# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AlphaLens is a stock analysis pipeline built around **TradingAgents** (v0.2.3), a multi-agent LLM framework that simulates a professional trading firm. Agents (analysts, researchers, traders, risk managers) collaborate through structured debates to produce buy/hold/sell decisions.

A **pre-screener** module (`tradingagents/prescreener/`) filters the S&P 500 down to top candidates before feeding them into TradingAgents for deep analysis. 66 unit tests.

## Commands

```bash
# Setup
source .venv/bin/activate        # Python 3.14 venv (created with uv)

# Run analysis (Gemini config)
.venv/bin/python run_gemini.py

# Run interactive CLI
.venv/bin/tradingagents

# Run all tests (unittest, not pytest)
.venv/bin/python -m unittest discover tests -v

# Run prescreener tests only
.venv/bin/python -m unittest discover tests -p "test_prescreener_*" -v

# Run single test
.venv/bin/python -m unittest tests/test_model_validation.py -v

# Quick indicator check
.venv/bin/python test.py
```

## Architecture

### Two-Stage Pipeline (Target)

```
Pre-screener (yfinance + stockstats, ~30min, $0)
  → Filter S&P 500 by technicals + fundamentals
  → Top 10-20 candidates
  → TradingAgents deep analysis (Gemini 3.1 Pro, ~15min/ticker)
  → Final BUY/OVERWEIGHT/HOLD/UNDERWEIGHT/SELL ratings
```

### TradingAgents Agent Flow

```
Analyst Team (parallel, quick_think_llm)
  ├── Market Analyst     → technical indicators report
  ├── Social Analyst     → sentiment report
  ├── News Analyst       → news impact report
  └── Fundamentals Analyst → financials report
         │
         ▼
Research Team (debate, deep_think_llm)
  Bull Researcher ↔ Bear Researcher → Research Manager synthesizes
         │
         ▼
Trader → investment plan with position sizing
         │
         ▼
Risk Management (3-way debate)
  Aggressive ↔ Neutral ↔ Conservative
         │
         ▼
Portfolio Manager → final 5-tier rating
```

### Key Abstractions

- **LLM Factory** (`tradingagents/llm_clients/factory.py`): Provider-agnostic client creation. OpenAI-compatible providers (xAI, DeepSeek, Ollama, etc.) share one client; Anthropic, Google, Azure have dedicated clients.
- **Vendor Routing** (`tradingagents/agents/utils/agent_utils.py`): `config["data_vendors"]` routes by category; `config["tool_vendors"]` overrides per-tool. Automatic fallback on rate-limit errors.
- **Memory** (`tradingagents/agents/utils/memory.py`): BM25 lexical similarity (no embeddings, no API calls). One instance per agent role. Updated via `reflect_and_remember()`.
- **State Graph** (`tradingagents/graph/setup.py`): LangGraph conditional edges control agent flow. `ConditionalLogic` enforces debate round limits.

### Configuration

All config lives in `tradingagents/default_config.py`. Key parameters:

- `llm_provider`: "openai" | "anthropic" | "google" | "xai" | "deepseek" | "openrouter" | "ollama"
- `deep_think_llm` / `quick_think_llm`: Model IDs per provider
- `max_debate_rounds` / `max_risk_discuss_rounds`: 1 (fast) to 5 (thorough)
- `data_vendors`: category → vendor mapping (yfinance, alpha_vantage)
- `backend_url`: Must be `None` for Google provider (defaults to OpenAI URL)
- `output_language`: Affects analyst reports only; internal debate stays English

### Data Vendors

| Vendor | What it provides | Config key |
|--------|-----------------|------------|
| yfinance | OHLCV, technicals (stockstats), fundamentals, news | Default for all |
| Alpha Vantage | Better fundamentals, news, insider transactions | Requires `ALPHA_VANTAGE_API_KEY` |

### Output

Results persist to `~/.tradingagents/logs/{TICKER}/TradingAgentsStrategy_logs/full_states_log_{DATE}.json`.

Generate human-readable Markdown reports from JSON logs:
```python
from cli.main import save_report_to_disk
save_report_to_disk(final_state, "TICKER", Path("reports/TICKER_DATE"))
# Creates: complete_report.md, 1_analysts/*.md, 2_research/*.md, etc.
```

## Known Issues

- **Gemini 429 RESOURCE_EXHAUSTED**: Google free tier has 1M input tokens/min limit on gemini-2.5-flash. Alpha Vantage fundamentals are large (~1.8MB). Custom retry logic added in `google_client.py` (waits ~40s per retry, up to 5 attempts). Upstream TradingAgents has no LLM-level retry for this.
- **`backend_url` must be `None` for Google**: Default config has OpenAI URL which causes 404 for Google provider.
- **Pre-screener may surface value traps**: Low P/E in cyclical stocks (semiconductors, materials) can be peak-earnings trap. TradingAgents Stage 2 catches these — the two-stage pipeline works as designed.

## Environment

- API keys in `.env` (GOOGLE_API_KEY, ALPHA_VANTAGE_API_KEY)
- Google API key also in macOS Keychain under `google-api-key`
- Current LLM config: Gemini 3.1 Pro (deep) + Gemini 2.5 Flash (quick)
