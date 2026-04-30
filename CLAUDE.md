# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repo.

## Project status (2026-04-25 →)

**AlphaLens** = research/learning infrastructure dla retail quant active alpha experimentation. Po **10/10 paradigm failures phase-robust** (Layer 2b/2c/2d/2e/2f/2g + tri-factor + mom+lowvol_combo + regime-gate rescue + quality+momentum + vol-target overlay) projekt repositioned: methodology bundle (pre-reg + multi-phase + Bonferroni) jest durable artifact, Layer 1 watchdog + literature review zostają live. **Search dla coraz lepszych screenerów pozostaje open-ended** — każdy nowy test podnosi Bonferroni bar dla następnego (ledger discipline), ale "no further prospecting" NIE jest pozycją projektu. Layer architecture w ADR 0007 (5 warstw: screener → selection-gate → engine → risk-overlay → attribution) — kolejne hipotezy mogą operować na nowej warstwie. Capital deployment based na current strategies jest off-table dopóki phase-robust PASS się nie pojawi.

**Live production:** Layer 1 SEC EDGAR watchdog (launchd `detect` + `worker`) + literature_review weekly+monthly Perplexity scan.
**Wszystko inne:** CLOSED, ARCHIVED lub RESEARCH_ONLY — kod zostaje jako reusable framework + anti-pattern catalog. Methodology bundle (preregistration ledger + multi_phase + audit driver) extracted do `kamilpajak/phase-robust-backtesting` (MIT).

Pełny rozliczenie: `docs/research/paradigm_failures_postmortem.md` (10 paradigm failures across 3 architectural layers). Decyzje architektoniczne: `docs/adr/` (7 ADRs).

## Layer status

Lifecycle status każdej warstwy żyje w jej `__init__.py` jako `__status__` constant (enforced przez `tests/test_layer_status.py`):

| Path | Status | Notatka |
|------|--------|---------|
| `alphalens/watchdog/` | ACTIVE | Layer 1 — live w launchd |
| `alphalens/literature_review/` | ACTIVE | Monthly + weekly Perplexity scan, live w launchd |
| `alphalens/backtest/` | ACTIVE | screener-agnostic harness |
| `alphalens/screeners/themed/` | CLOSED 2026-04-22 | Layer 2b — momentum overfit + cost eats signal |
| `alphalens/screeners/lean/` | ARCHIVED 2026-04-19 | Layer 2c — Sharpe 0.25 net, FF3 α t=0.14 |
| `alphalens/screeners/insider/` | CLOSED 2026-04-24 | Layer 2d — Carhart t=2.14 IS → 0.68 OOS |
| `alphalens/screeners/momentum_lowvol/` | RESEARCH_ONLY | Layer 2 mom + low-vol adapter — strategy FAIL'd as failure 7 but scorer reused as BASE for Layer 4 vol-target overlay test |
| `alphalens/screeners/prescreener/` | RESEARCH_ONLY | Layer 2a — unvalidated, manual ad-hoc |
| `alphalens/rotation/` | CLOSED | Layer 2e — failed IS+OOS sanity |
| `alphalens/events/` | CLOSED | Layer 2f — 8-K event screen failed |
| `alphalens/guru/` | CLOSED | Layer 2g — LLM-researcher pilot failed |
| `alphalens/macro/` | RESEARCH_ONLY | reusable infra, no standalone strategy |
| `alphalens/regime_gate/` | RESEARCH_ONLY | Layer 2 selection-gate wrapper; rescue attempt failed Phase 1 diagnostic 2026-04-29 — wrapper retained dla future research |
| `alphalens/risk_overlay/` | RESEARCH_ONLY | Layer 4 time-series sizing overlay (vol-targeting per Moreira-Muir 2017); first hypothesis `vol_target_mom_lowvol_2026_04_30` pre-registered 2026-04-30 |

Core abstractions (zawsze ACTIVE, nie należą do żadnej layer): `candidates.py`, `queue.py`, `worker.py`, `runner.py`, `registry.py`, `config_gemini.py`.

## Layer architecture (active alpha experimentation)

Five-layer separation per **ADR 0007** (`docs/adr/0007-layer-architecture.md`). Each layer has a single responsibility; failures attribute to one layer:

1. **Screener** (Layer 2*: `alphalens/screeners/*`, `rotation/`, etc.) — cross-sectional rank @ time t → top-N tickers
2. **Selection-gate** (`alphalens/regime_gate/`) — binary/graded gate on the Scorer (modifies *which* tickers deploy)
3. **Backtest engine** (`alphalens/backtest/engine.py`) — runs scorer over strided rebalance calendar → `BacktestReport.portfolio_returns`
4. **Risk overlay** (`alphalens/risk_overlay/`) — time-series sizing on portfolio realised vol (modifies *how much exposure*); first impl is vol-targeting per Moreira-Muir 2017
5. **Attribution** (`alphalens/backtest/{cost_model, factor_analysis, metrics}`) — cost-drag, Carhart-4F, Sharpe, Bonferroni → ledger verdict

Compound hypotheses combine layers (e.g. mom+lowvol screener × VIX>20 selection-gate × vol-target overlay), each combination paying its own Bonferroni cost. Rule of thumb: layer 2 modifies *which*; layer 4 modifies *how much*. **Time-varying-beta hazard:** overlay-bearing strategies use Sharpe-improvement (not Carhart α t-stat) as primary success metric — see ADR 0007.

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
.venv/bin/alphalens literature monthly           # ad-hoc deep literature scan (Perplexity high)
.venv/bin/alphalens literature weekly            # ad-hoc weekly RSS scan

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

**Status markers** — każdy layer/screener `__init__.py` deklaruje `__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"]` plus `__closed_date__`, `__closed_reason__` i `__closed_evidence__: dict[str, str]` (mapping 7 gates → path / `"N/A: <reason>"` / `"UNTESTED: <reason>"`) jeśli `__status__ ∈ {CLOSED, ARCHIVED}`. Schema: `docs/research/kill_verdict_checklist.md`. Dodawanie nowej warstwy wymaga aktualizacji `LAYERS_WITH_STATUS` w `tests/test_layer_status.py`.

**English-only w kodzie** — komentarze, docstrings, identifiery po angielsku. Math notation (α, ρ, ×, −) zostaje. Polish prose żyje w CLAUDE.md, MEMORY, rozmowach, commit messages, postmortemach. Enforcement: `tests/test_no_polish_chars.py`.

**Dependency direction** — `alphalens.backtest.*` NIE importuje z `alphalens.screeners.*` (poza explicit exemption: `historical_validation.py`, RESEARCH-ONLY). Adaptery żyją przy screenerach. Enforcement: `tests/test_module_dependencies.py`.

**Config parity** — `SCORER_CONFIG` w `lean_project/main.py` (Docker-inlined) musi matchować `LEAN_DEFAULTS` na shared keys. Enforcement: `tests/test_lean_config_parity.py`.

**Lazy CLI imports** — `alphalens_cli/commands/research.py` celowo NIE promote'uje cross-function duplikatów do top-level. Pomiar wykazał +913ms regresji startup time per `alphalens` invoke (Layer 1 watchdog cron odpala często, nie może płacić).

**No backward compatibility** — solo project, zero external users. Rename, refactor, drop old behavior w jednym commicie bez aliases. Wyjątek: vendored patches w `TradingAgents/` muszą zostać mergeable z upstream sync.

**New components** — zawsze w `alphalens/<name>/` lub `alphalens_cli/`. Nigdy w `TradingAgents/` (upstream territory), nigdy w top-level.

## Where to find "why"

- **Architectural decisions:** `docs/adr/` (5 ADRs: pivot, queue contract, screener-agnostic backtest, vendored upstream, closed-layer policy)
- **Why each layer was closed:** `docs/research/paradigm_failures_postmortem.md` + per-layer `__closed_reason__` w `__init__.py`
- **Backtest reports archive:** `docs/backtest/`
- **Per-strategy design + audit docs:** `docs/research/`

## Configuration

Base config: `TradingAgents/tradingagents/default_config.py` (upstream, OpenAI-centric). **Always wrap with `alphalens.core.config_gemini.build_gemini_config()`** — deep-copies DEFAULT_CONFIG i override'uje dla Gemini.

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

Issues dotyczące CLOSED warstw (Lean Docker setup, Layer 2d backtest workflow, themed gate Phase 2) → patrz `launchd/archived/README.md` + `docs/research/paradigm_failures_postmortem.md`.

## Environment

- API keys w `.env` (GOOGLE_API_KEY, ALPHA_VANTAGE_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, POLYGON_API_KEY, PERPLEXITY_API_KEY)
- Google API key też w macOS Keychain pod `google-api-key`
- LLM config: Gemini 3.1 Pro (deep) + Gemini 2.5 Flash (quick)
- Runtime data (poza repo, survives git ops):
  - `~/.alphalens/candidates.db` — unified Layer 1 → Layer 3 queue
  - `~/.alphalens/watchdog/` — portfolio.yaml, EDGAR dedup, digest, launchd logs
  - `~/.alphalens/lean/{data,results,logs}/` — Lean OHLCV cache (also used by backtest replay)
  - `~/.tradingagents/{cache,logs}/` — upstream state (hardcoded w ich kodzie)
