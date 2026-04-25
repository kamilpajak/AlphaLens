# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repo.

## Project status (2026-04-25 →)

**AlphaLens** = research/learning infrastructure dla retail quant active alpha experimentation, **NIE** active alpha generation product. Po 5/5 paradigm failures (Layer 2b/2c/2d/2e/2f/2g) projekt został repositioned. Capital deployment based na current strategies jest off-table.

**Live production:** Layer 1 SEC EDGAR watchdog (launchd `detect` + `worker`).
**Wszystko inne:** CLOSED, ARCHIVED lub RESEARCH_ONLY — kod zostaje jako reusable framework + anti-pattern catalog.

Pełny rozliczenie: `docs/research/5_paradigm_failures_postmortem.md`. Decyzje architektoniczne: `docs/adr/` (5 ADRs).

## Layer status

Lifecycle status każdej warstwy żyje w jej `__init__.py` jako `__status__` constant (enforced przez `tests/test_layer_status.py`):

| Path | Status | Notatka |
|------|--------|---------|
| `alphalens/watchdog/` | ACTIVE | Layer 1 — live w launchd |
| `alphalens/backtest/` | ACTIVE | screener-agnostic harness |
| `alphalens/screeners/themed/` | CLOSED 2026-04-22 | Layer 2b — momentum overfit + cost eats signal |
| `alphalens/screeners/lean/` | ARCHIVED 2026-04-19 | Layer 2c — Sharpe 0.25 net, FF3 α t=0.14 |
| `alphalens/screeners/insider/` | CLOSED 2026-04-24 | Layer 2d — Carhart t=2.14 IS → 0.68 OOS |
| `alphalens/screeners/prescreener/` | RESEARCH_ONLY | Layer 2a — unvalidated, manual ad-hoc |
| `alphalens/rotation/` | CLOSED | Layer 2e — failed IS+OOS sanity |
| `alphalens/events/` | CLOSED | Layer 2f — 8-K event screen failed |
| `alphalens/guru/` | CLOSED | Layer 2g — LLM-researcher pilot failed |
| `alphalens/macro/` | RESEARCH_ONLY | reusable infra, no standalone strategy |

Core abstractions (zawsze ACTIVE, nie należą do żadnej layer): `candidates.py`, `queue.py`, `worker.py`, `runner.py`, `registry.py`, `config_gemini.py`.

## Commands

```bash
# Setup (fresh clone) — requires Python 3.13
uv venv --python 3.13
uv sync                                          # alphalens + tradingagents editable

# Tests (unittest, NOT pytest)
.venv/bin/python -m unittest discover tests -v

# Live workflows
.venv/bin/alphalens watchdog run-once            # Layer 1: poll EDGAR, classify, dispatch
.venv/bin/alphalens queue process                # drain unified queue → Layer 3
.venv/bin/alphalens queue scorer-stats --since-days 30
.venv/bin/alphalens analyze TICKER               # Layer 3 ad-hoc deep analysis
.venv/bin/alphalens status                       # global queue + digest + dedup

# Backtest replay (closed scorers — research only, NOT for capital deploy)
.venv/bin/alphalens backtest --start 2021-04-19 --end 2026-04-17 --diagnose
.venv/bin/alphalens backtest --scorer lean
.venv/bin/alphalens themed status --days 90      # historical themed monitoring
.venv/bin/alphalens research validate-llm-filter --scorer rule

# Upstream TradingAgents interactive menu
.venv/bin/tradingagents
```

CLI komendy dla CLOSED layers istnieją jako research replay tooling — patrz `docs/adr/0005-closed-layers-as-anti-pattern-catalog.md`.

## Conventions

**Status markers** — każdy layer/screener `__init__.py` deklaruje `__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"]` plus `__closed_date__` i `__closed_reason__` jeśli applicable. Dodawanie nowej warstwy wymaga aktualizacji `LAYERS_WITH_STATUS` w `tests/test_layer_status.py`.

**English-only w kodzie** — komentarze, docstrings, identifiery po angielsku. Math notation (α, ρ, ×, −) zostaje. Polish prose żyje w CLAUDE.md, MEMORY, rozmowach, commit messages, postmortemach. Enforcement: `tests/test_no_polish_chars.py`.

**Dependency direction** — `alphalens.backtest.*` NIE importuje z `alphalens.screeners.*` (poza explicit exemption: `historical_validation.py`, RESEARCH-ONLY). Adaptery żyją przy screenerach. Enforcement: `tests/test_module_dependencies.py`.

**Config parity** — `SCORER_CONFIG` w `lean_project/main.py` (Docker-inlined) musi matchować `LEAN_DEFAULTS` na shared keys. Enforcement: `tests/test_lean_config_parity.py`.

**Lazy CLI imports** — `alphalens_cli/commands/research.py` celowo NIE promote'uje cross-function duplikatów do top-level. Pomiar wykazał +913ms regresji startup time per `alphalens` invoke (Layer 1 watchdog cron odpala często, nie może płacić).

**No backward compatibility** — solo project, zero external users. Rename, refactor, drop old behavior w jednym commicie bez aliases. Wyjątek: vendored patches w `TradingAgents/` muszą zostać mergeable z upstream sync.

**New components** — zawsze w `alphalens/<name>/` lub `alphalens_cli/`. Nigdy w `TradingAgents/` (upstream territory), nigdy w top-level.

## Where to find "why"

- **Architectural decisions:** `docs/adr/` (5 ADRs: pivot, queue contract, screener-agnostic backtest, vendored upstream, closed-layer policy)
- **Why each layer was closed:** `docs/research/5_paradigm_failures_postmortem.md` + per-layer `__closed_reason__` w `__init__.py`
- **Backtest reports archive:** `docs/backtest/`
- **Per-strategy design + audit docs:** `docs/research/`

## Configuration

Base config: `TradingAgents/tradingagents/default_config.py` (upstream, OpenAI-centric). **Always wrap with `alphalens.config_gemini.build_gemini_config()`** — deep-copies DEFAULT_CONFIG i override'uje dla Gemini.

Key params: `llm_provider="google"`, `deep_think_llm` / `quick_think_llm`, `max_debate_rounds`, `backend_url=None` (must be None dla Google), `data_vendors`.

## Upstream relationship (TradingAgents)

`origin` → `kamilpajak/AlphaLens`. `upstream` → `TauricResearch/TradingAgents` via `git subtree --squash`. Pełen rationale: `docs/adr/0004-tradingagents-as-subtree.md`.

```bash
git subtree pull --prefix=TradingAgents \
  https://github.com/TauricResearch/TradingAgents.git main --squash
```

Po sync reapply local patches (currently: Gemini 429 retry w `TradingAgents/tradingagents/llm_clients/google_client.py`). Pending upstream PRs tracked w memory: `project_pr_tradingagents_retry.md`, `project_pr_signal_context_injection.md`.

## Known issues (LIVE)

- **Gemini 429 RESOURCE_EXHAUSTED**: Google free tier 1M input tokens/min na gemini-2.5-flash. Alpha Vantage fundamentals ~1.8MB triggerują. Custom retry w `TradingAgents/tradingagents/llm_clients/google_client.py` (10 retries, ~40s base delay).
- **`backend_url` must be `None` for Google**: upstream DEFAULT_CONFIG ma OpenAI URL → 404 dla Google. `build_gemini_config()` handles.
- **Signal-context injection deferred**: `runner.py::build_trigger_context(candidate)` formatuje per-source trigger string ale tylko loguje. Injection do TradingAgents initial state wymaga `trigger_context` kwarg na `propagate()` — planned upstream PR.
- **Cost tracking placeholder**: `candidates.cost_usd` jest NULL — TradingAgents nie eksponuje token accounting. Duration + `model_used` populated.
- **Prescreener (Layer 2a) unvalidated**: 45% fundamentals weight wymaga PIT historicals których Polygon Starter ($29/mo) nie dostarcza. Manual ad-hoc tylko, no performance guarantee.

Issues dotyczące CLOSED warstw (Lean Docker setup, Layer 2d backtest workflow, themed gate Phase 2) → patrz `launchd/archived/README.md` + `docs/research/5_paradigm_failures_postmortem.md`.

## Environment

- API keys w `.env` (GOOGLE_API_KEY, ALPHA_VANTAGE_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, POLYGON_API_KEY)
- Google API key też w macOS Keychain pod `google-api-key`
- LLM config: Gemini 3.1 Pro (deep) + Gemini 2.5 Flash (quick)
- Runtime data (poza repo, survives git ops):
  - `~/.alphalens/candidates.db` — unified Layer 1 → Layer 3 queue
  - `~/.alphalens/watchdog/` — portfolio.yaml, EDGAR dedup, digest, launchd logs
  - `~/.alphalens/lean/{data,results,logs}/` — Lean OHLCV cache (also used by backtest replay)
  - `~/.tradingagents/{cache,logs}/` — upstream state (hardcoded w ich kodzie)
