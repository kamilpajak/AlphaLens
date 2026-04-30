# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repo.

## Project status (2026-04-25 →)

**AlphaLens** = research/learning infrastructure dla retail quant active alpha experimentation. Po **10/10 paradigm failures phase-robust** (Layer 2b/2c/2d/2e/2f/2g + tri-factor + mom+lowvol_combo + regime-gate rescue + quality+momentum + vol-target overlay) projekt repositioned: methodology bundle (pre-reg + multi-phase + Bonferroni) jest durable artifact, Layer 1 watchdog + literature review zostają live. **Search dla coraz lepszych screenerów pozostaje open-ended** — każdy nowy test podnosi Bonferroni bar dla następnego (ledger discipline), ale "no further prospecting" NIE jest pozycją projektu. Layer architecture w ADR 0007 (5 warstw: screener → selection-gate → engine → risk-overlay → attribution) — kolejne hipotezy mogą operować na nowej warstwie. Capital deployment based na current strategies jest off-table dopóki phase-robust PASS się nie pojawi.

**Live production:** Layer 1 SEC EDGAR watchdog (launchd `detect` only — `worker` archived per ADR 0008) + literature_review weekly+monthly Perplexity scan.
**Wszystko inne:** CLOSED, ARCHIVED lub RESEARCH_ONLY — kod zostaje jako reusable framework + anti-pattern catalog. Methodology bundle (preregistration ledger + multi_phase + audit driver) extracted do `kamilpajak/phase-robust-backtesting` (MIT). TradingAgents subtree usunięty 2026-04-30 (ADR 0008).

Pełny rozliczenie: `docs/research/paradigm_failures_postmortem.md` (10 paradigm failures across 3 architectural layers). Decyzje architektoniczne: `docs/adr/` (8 ADRs).

## Layer status

Lifecycle status każdej warstwy żyje w jej `__init__.py` jako `__status__` constant (enforced przez `tests/test_layer_status.py`):

Layout zorganizowany jako 11 top-level slotów (Phase 1-6 reorg 2026-04-30, ADR 0007):

| Path | Status | Notatka |
|------|--------|---------|
| `alphalens/core/` | ACTIVE (namespace) | plumbing — candidates, queue, registry, scorer_stats (Layer 3 runner/worker removed per ADR 0008) |
| `alphalens/watchdog/` | ACTIVE | Layer 1 — `detect` live w launchd, `worker` archived per ADR 0008 |
| `alphalens/literature_review/` | ACTIVE | Monthly + weekly Perplexity scan, live w launchd |
| `alphalens/screeners/prescreener/` | RESEARCH_ONLY | Layer 2a — unvalidated, manual ad-hoc |
| `alphalens/screeners/momentum_lowvol/` | RESEARCH_ONLY | Layer 2 mom + low-vol adapter — strategy FAIL'd as failure 7 but scorer reused as BASE for Layer 4 vol-target overlay test |
| `alphalens/gates/` | RESEARCH_ONLY | Layer 2 selection-gates (was `regime_gate/`); single occupant `wrapper.py` until concrete classifier added |
| `alphalens/backtest/` | ACTIVE | Layer 3 engine — engine, walk-forward removed (moved to attribution), multi_phase, multiple_testing, weighting, theme_analysis, llm_scorers, historical_validation, metrics (engine-side primitives + Sharpe consumed downstream) |
| `alphalens/overlays/` | RESEARCH_ONLY | Layer 4 risk-overlays (was `risk_overlay/`); single occupant `vol_target.py` |
| `alphalens/attribution/` | ACTIVE | Layer 5 — cost_model, factor_analysis, regime, decision_matrix, diagnostics, report, walk_forward |
| `alphalens/preregistration/` | ACTIVE | methodology bundle (mirror OSS `phase-robust-backtesting`) |
| `alphalens/data/` | ACTIVE (namespace) | data infrastructure — `data/store/` (PIT SoT for as-of-t reads), `data/{alt_data,fundamentals,macro}/` (RESEARCH_ONLY clients), `data/factors.py` (Fama-French CSV loader) |
| `alphalens/archive/` | namespace | ADR 0005 anti-pattern catalog: `rotation/, events/, guru/, quiver_screener/, screeners/{themed,lean,insider}/` |

## Layer architecture (active alpha experimentation)

Five-layer separation per **ADR 0007** (`docs/adr/0007-layer-architecture.md`). Each layer has a single responsibility; failures attribute to one layer:

1. **Screener** (Layer 2*: `alphalens/screeners/*`, archived ones in `alphalens/archive/`) — cross-sectional rank @ time t → top-N tickers
2. **Selection-gate** (`alphalens/gates/`) — binary/graded gate on the Scorer (modifies *which* tickers deploy)
3. **Backtest engine** (`alphalens/backtest/engine.py`) — runs scorer over strided rebalance calendar → `BacktestReport.portfolio_returns`
4. **Risk overlay** (`alphalens/overlays/`) — time-series sizing on portfolio realised vol (modifies *how much exposure*); first impl is vol-targeting per Moreira-Muir 2017
5. **Attribution** (`alphalens/attribution/{cost_model, factor_analysis, regime, ...}`) — cost-drag, Carhart-4F, Sharpe, Bonferroni → ledger verdict. Engine-side primitives (`rank_ic`, `turnover_pct`, `sharpe`) live in `alphalens/backtest/metrics.py` and are consumed downstream by attribution.

Compound hypotheses combine layers (e.g. mom+lowvol screener × VIX>20 selection-gate × vol-target overlay), each combination paying its own Bonferroni cost. Rule of thumb: layer 2 modifies *which*; layer 4 modifies *how much*. **Time-varying-beta hazard:** overlay-bearing strategies use Sharpe-improvement (not Carhart α t-stat) as primary success metric — see ADR 0007.

## Commands

```bash
# Setup (fresh clone) — requires Python 3.13
uv venv --python 3.13
uv sync

# Tests (unittest, NOT pytest)
.venv/bin/python -m unittest discover tests -v

# Live workflows
.venv/bin/alphalens watchdog run-once            # Layer 1: poll EDGAR, classify, dispatch
.venv/bin/alphalens queue scorer-stats --since-days 30   # historical viewer over candidates.db
.venv/bin/alphalens status                       # global queue + digest + dedup
.venv/bin/alphalens literature monthly           # ad-hoc deep literature scan (Perplexity high)
.venv/bin/alphalens literature weekly            # ad-hoc weekly RSS scan

# Backtest replay (closed scorers — research only, NOT for capital deploy)
.venv/bin/alphalens backtest --start 2021-04-19 --end 2026-04-17 --diagnose
.venv/bin/alphalens backtest --scorer lean
.venv/bin/alphalens themed status --days 90      # historical themed monitoring
.venv/bin/alphalens research validate-llm-filter --scorer rule
```

CLI komendy dla CLOSED layers istnieją jako research replay tooling — patrz `docs/adr/0005-closed-layers-as-anti-pattern-catalog.md`.

## Conventions

**Status markers** — każdy layer/screener `__init__.py` deklaruje `__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"]` plus `__closed_date__`, `__closed_reason__` i `__closed_evidence__: dict[str, str]` (mapping 7 gates → path / `"N/A: <reason>"` / `"UNTESTED: <reason>"`) jeśli `__status__ ∈ {CLOSED, ARCHIVED}`. Schema: `docs/research/kill_verdict_checklist.md`. Dodawanie nowej warstwy wymaga aktualizacji `LAYERS_WITH_STATUS` w `tests/test_layer_status.py`.

**English-only w kodzie** — komentarze, docstrings, identifiery po angielsku. Math notation (α, ρ, ×, −) zostaje. Polish prose żyje w CLAUDE.md, MEMORY, rozmowach, commit messages, postmortemach. Enforcement: `tests/test_no_polish_chars.py`.

**Dependency direction** — dwa enforcement rules w `tests/test_module_dependencies.py`:
- `alphalens.backtest.*` NIE importuje z `alphalens.screeners.*` (exemption: `historical_validation.py`).
- `alphalens.backtest.*` NIE importuje z `alphalens.attribution.*` (Layer 3 → Layer 5 dependency direction; engine produces `BacktestReport`, attribution consumes — reverse direction would be a cycle).

**Config parity** — `SCORER_CONFIG` w `lean_project/main.py` (Docker-inlined) musi matchować `LEAN_DEFAULTS` na shared keys. Enforcement: `tests/test_lean_config_parity.py`.

**Lazy CLI imports** — `alphalens_cli/commands/research.py` celowo NIE promote'uje cross-function duplikatów do top-level. Pomiar wykazał +913ms regresji startup time per `alphalens` invoke (Layer 1 watchdog cron odpala często, nie może płacić).

**No backward compatibility** — solo project, zero external users. Rename, refactor, drop old behavior w jednym commicie bez aliases.

**New components** — zawsze w `alphalens/<name>/` lub `alphalens_cli/`, nigdy w top-level.

## Where to find "why"

- **Architectural decisions:** `docs/adr/` (8 ADRs: pivot, queue contract, screener-agnostic backtest, ~~vendored upstream~~ *superseded*, closed-layer policy, OSS extraction, layer architecture, sunset TradingAgents)
- **Why each layer was closed:** `docs/research/paradigm_failures_postmortem.md` + per-layer `__closed_reason__` w `__init__.py`
- **Backtest reports archive:** `docs/backtest/`
- **Per-strategy design + audit docs:** `docs/research/`

## TradingAgents removal (2026-04-30)

Vendored TradingAgents subtree + Layer 3 LLM runner removed per [ADR 0008](docs/adr/0008-sunset-tradingagents-integration.md). Worker (`com.alphalens.watchdog.worker.plist`) archived. Layer 1 watchdog still detects EDGAR events and writes to `~/.alphalens/candidates.db`, but no consumer drains the queue today — `queue scorer-stats` remains as a historical viewer. If TA is needed in the future, clone it into a separate working directory and run it manually.

## Known issues (LIVE)

- **Prescreener (Layer 2a) unvalidated**: 45% fundamentals weight wymaga PIT historicals których Polygon Starter ($29/mo) nie dostarcza. Manual ad-hoc tylko, no performance guarantee.

Issues dotyczące CLOSED warstw (Lean Docker setup, Layer 2d backtest workflow, themed gate Phase 2) → patrz `launchd/archived/README.md` + `docs/research/paradigm_failures_postmortem.md`.

## Environment

- API keys w `.env` (GOOGLE_API_KEY, ALPHA_VANTAGE_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, POLYGON_API_KEY, PERPLEXITY_API_KEY)
- Google API key też w macOS Keychain pod `google-api-key`
- LLM config: Gemini 3 Pro (guru pilot, low thinking budget)
- Runtime data (poza repo, survives git ops):
  - `~/.alphalens/candidates.db` — Layer 1 candidate queue (historical log; no live drain)
  - `~/.alphalens/watchdog/` — portfolio.yaml, EDGAR dedup, digest, launchd logs
  - `~/.alphalens/lean/{data,results,logs}/` — Lean OHLCV cache (also used by backtest replay)
  - `~/.alphalens/guru_cache/` — guru pilot LLM response cache
