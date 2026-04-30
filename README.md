# AlphaLens

[![CI](https://github.com/kamilpajak/AlphaLens/actions/workflows/ci.yml/badge.svg)](https://github.com/kamilpajak/AlphaLens/actions/workflows/ci.yml)
[![Quality Gate](https://sonarcloud.io/api/project_badges/measure?project=kamilpajak_AlphaLens&metric=alert_status)](https://sonarcloud.io/summary/new_code?id=kamilpajak_AlphaLens)
[![Coverage](https://sonarcloud.io/api/project_badges/measure?project=kamilpajak_AlphaLens&metric=coverage)](https://sonarcloud.io/summary/new_code?id=kamilpajak_AlphaLens)
[![Python 3.13](https://img.shields.io/badge/python-3.13-blue.svg)](https://www.python.org/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Research lab infrastructure for retail active alpha experimentation — real-time SEC EDGAR event detection, quantitative screening, and a vectorized backtest engine for paradigm validation.

## Status (2026-04-25 →)

The project pivoted from "active alpha generation" to **research / learning infrastructure** after [9 paradigm failures](docs/research/paradigm_failures_postmortem.md) (Layer 2b/2c/2d/2e/2f/2g + tri-factor + mom+lowvol_combo + regime-gate rescue + quality+momentum, all phase-robust FAIL). Capital deployment based on the current strategies is **off the table** until a phase-robust PASS appears; the screener search itself stays open-ended under pre-registration discipline. The codebase remains as:

- **Reusable research framework** — backtest engine, factor attribution, sanity checks, multiple-testing corrections, regime classifier, **time-series sizing overlays (`alphalens/overlays/`, vol-targeting per Moreira-Muir 2017)**, cost models, weighting schemes; layer architecture in [ADR 0007](docs/adr/0007-layer-architecture.md)
- **Production-grade data clients** — Polygon, FRED, SEC EDGAR, Alpha Vantage fundamentals
- **LLM scoring infrastructure** — GuruScorer single-prompt pilot (Layer 2f, RESEARCH_ONLY) using LangChain + Gemini directly
- **Anti-pattern catalog** — every closed strategy ships a `__closed_reason__` marker plus a postmortem entry
- **Spin-off OSS toolkit** — pre-registration ledger + multi-phase audit + Bonferroni helpers extracted as a standalone library: [`kamilpajak/phase-robust-backtesting`](https://github.com/kamilpajak/phase-robust-backtesting) (see [ADR 0006](docs/adr/0006-phase-robust-backtesting-extraction.md))

**Live in launchd**: only Layer 1 SEC EDGAR watchdog (read-only event detection, daily Telegram digest, near-zero maintenance). The Layer 3 LLM runner that previously consumed the candidate queue was removed by [ADR 0008](docs/adr/0008-sunset-tradingagents-integration.md).

> Architectural rationale: see [`docs/adr/`](docs/adr/) (8 ADRs).
> Per-layer postmortem: [`docs/research/paradigm_failures_postmortem.md`](docs/research/paradigm_failures_postmortem.md).
> Quick contributor guide: [`CLAUDE.md`](CLAUDE.md).

---

## Layer status

Each layer/screener package declares its lifecycle in `__init__.py` as `__status__ ∈ {ACTIVE, CLOSED, RESEARCH_ONLY, ARCHIVED}`, enforced by `tests/test_layer_status.py`.

| Path | Status | Notes |
|------|--------|-------|
| `alphalens/watchdog/` | ACTIVE | Layer 1 — `detect` live in launchd; `worker` archived per [ADR 0008](docs/adr/0008-sunset-tradingagents-integration.md) |
| `alphalens/backtest/` | ACTIVE | Screener-agnostic harness, reused across all replay |
| `alphalens/archive/screeners/themed/` | CLOSED 2026-04-22 | Layer 2b — momentum overfit OOS, realistic execution cost ~100% ann eats signal |
| `alphalens/archive/screeners/lean/` | ARCHIVED 2026-04-19 | Layer 2c — Sharpe 0.25 net, FF3 α t-stat 0.14 |
| `alphalens/archive/screeners/insider/` | CLOSED 2026-04-24 | Layer 2d — Carhart t=2.14 IS → 0.68 OOS, classic overfit |
| `alphalens/screeners/prescreener/` | RESEARCH_ONLY | Layer 2a — unvalidated, manual ad-hoc only |
| `alphalens/archive/rotation/` | CLOSED | Layer 2e — failed IS+OOS sanity (R12 macro overlay) |
| `alphalens/archive/events/` | CLOSED | Layer 2f — 8-K event-driven screen failed |
| `alphalens/archive/guru/` | CLOSED | Layer 2g — LLM-researcher pilot failed |
| `alphalens/data/macro/` | RESEARCH_ONLY | Reusable infra (FRED client, regime scorer) |
| `alphalens/gates/` | RESEARCH_ONLY | Layer 2 selection-gate wrapper (rescue attempt failed Phase 1 2026-04-29) |
| `alphalens/overlays/` | RESEARCH_ONLY | Layer 4 time-series sizing overlay (vol-targeting, Moreira-Muir 2017) |

CLOSED-layer code is retained as a research framework + anti-pattern record (see [ADR 0005](docs/adr/0005-closed-layers-as-anti-pattern-catalog.md)).

---

## Quickstart

### Prerequisites

- macOS (launchd scheduling assumed; CLI runs anywhere)
- Python 3.13 via [`uv`](https://github.com/astral-sh/uv) (3.14 not yet — `tiktoken 0.9.0` lacks a 3.14 prebuilt wheel)
- API keys: Google AI (Gemini), Alpha Vantage, Telegram bot, optional Polygon Stocks Basic

### Setup

```bash
git clone git@github.com:kamilpajak/AlphaLens.git
cd AlphaLens
uv venv --python 3.13
uv sync
```

Create `.env` at repo root:

```
GOOGLE_API_KEY=...
ALPHA_VANTAGE_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
POLYGON_API_KEY=...           # only for backtest replay against Lean OHLCV
```

Populate `~/.alphalens/watchdog/portfolio.yaml` (held + watchlist for Layer 1 routing):

```yaml
held:    [AAPL, MSFT]
watchlist: [NVDA, GOOGL]
```

### Running things

```bash
# Live workflows
.venv/bin/alphalens watchdog run-once          # Layer 1: poll EDGAR, classify, dispatch
.venv/bin/alphalens queue scorer-stats         # historical viewer over candidates.db
.venv/bin/alphalens status                     # queue + digest + dedup

# Backtest replay (closed scorers — research only, NOT for capital deploy)
.venv/bin/alphalens backtest --start 2021-04-19 --end 2026-04-17 --diagnose
.venv/bin/alphalens backtest --scorer lean
.venv/bin/alphalens themed status --days 90    # historical themed monitoring
.venv/bin/alphalens research validate-llm-filter --scorer rule

# Tests (unittest, not pytest) — 1278 tests
.venv/bin/python -m unittest discover tests -v

# Inspect the queue directly
sqlite3 ~/.alphalens/candidates.db \
  "SELECT id, ticker, source, priority, status, decision FROM candidates ORDER BY id DESC LIMIT 20;"
```

### Scheduled jobs (launchd)

Two live jobs in `launchd/`:

| Job | Interval | Purpose |
|---|---|---|
| `com.alphalens.watchdog.detect` | every 15 min | Layer 1 EDGAR poll → submit Candidates |
| `com.alphalens.watchdog.worker` | every 5 min  | Drain `candidates.db` → Layer 3 |

Closed-strategy plists live in `launchd/archived/` with reactivation notes (themed, lean, insider). See [`launchd/README.md`](launchd/README.md).

```bash
cp launchd/com.alphalens.watchdog.*.plist ~/Library/LaunchAgents/
for job in detect worker; do
  launchctl load ~/Library/LaunchAgents/com.alphalens.watchdog.${job}.plist
done
```

---

## Components

```
alphalens/                     ← my code (Python package)
├── core/
│   ├── candidates.py          shared domain — Candidate, AnalysisResult, CandidateSink Protocol
│   ├── queue.py               SQLite priority queue with dedup + retry + DLQ
│   ├── registry.py            SCREENERS dict — register a new screener in one line
│   └── scorer_stats.py        historical Layer 3 acceptance-rate viewer
├── watchdog/                  Layer 1: EDGAR detection + classifier + dispatch (ACTIVE)
├── backtest/                  Screener-agnostic backtest harness (ACTIVE)
├── alt_data/                  SEC EDGAR + Form 4 + Russell universe builder (RESEARCH_ONLY)
├── fundamentals/              Polygon-backed fundamentals + soft-gate logic (RESEARCH_ONLY)
├── macro/                     FRED client + regime signals (RESEARCH_ONLY)
├── quiver_screener/           Quiver Quantitative congressional-trades client (CLOSED)
├── rotation/                  Layer 2e tactical sector rotation (CLOSED)
├── events/                    Layer 2f 8-K event screener (CLOSED)
├── guru/                      Layer 2g LLM-researcher (CLOSED)
└── screeners/                 pipeline strategies
    ├── themed/                Layer 2b themed momentum (CLOSED)
    ├── prescreener/           Layer 2a S&P 500 composite (RESEARCH_ONLY)
    ├── insider/               Layer 2d Form 4 cluster-buy (CLOSED)
    └── lean/                  Layer 2c Russell rule-based (ARCHIVED)

alphalens_cli/                 ← my CLI (separate package)

docs/
├── adr/                       Architecture Decision Records (8 ADRs)
├── research/                  paradigm postmortem + per-strategy design + audit reports
└── backtest/                  historical backtest run outputs

launchd/                       macOS scheduled jobs (live: detect, literature; archived: worker, themed, lean, insider)
tests/                         unittest suite (~1365 tests; several architectural enforcers)
```

Full architecture detail and key abstractions: [`CLAUDE.md`](CLAUDE.md). Layer separation rationale: [`ADR 0007`](docs/adr/0007-layer-architecture.md).

---

## Configuration

**LLM**: `alphalens_cli/commands/guru.py` constructs `langchain_google_genai.ChatGoogleGenerativeAI` directly for the Layer 2f GuruAgent pilot. There is no shared LLM config wrapper today.

**Data vendors**: `yfinance` for prices and technical indicators; `alphalens/data/fundamentals/fetcher.py` calls Alpha Vantage REST directly (no third-party client).

**Themed universe** (Layer 2b — closed): `alphalens/archive/screeners/themed/universe.yaml`. Quarterly refresh runbook: [`docs/runbook_layer2b_refresh.md`](docs/runbook_layer2b_refresh.md).

**Portfolio**: `~/.alphalens/watchdog/portfolio.yaml`.

---

## Runtime data

Lives outside the repo, survives git operations:

- `~/.alphalens/candidates.db` — Layer 1 candidate queue (historical log; no live drain)
- `~/.alphalens/watchdog/` — portfolio.yaml, EDGAR dedup, digest buffer, launchd logs
- `~/.alphalens/lean/{data,results,logs}/` — Lean OHLCV cache (used by backtest replay)
- `~/.alphalens/guru_cache/` — guru pilot LLM response cache

---

## Development

- **Package manager**: `uv` (not pip / poetry)
- **Testing**: unittest (not pytest) — `python -m unittest discover tests`
- **Commits**: Conventional Commits (`feat(scope):`, `fix(scope):`, `refactor(scope):`, …)
- **Code language**: English in source code (`alphalens/`, `alphalens_cli/`, `tests/`); enforced by `tests/test_no_polish_chars.py`
- **New components** go in `alphalens/<name>/` or `alphalens_cli/`, never at top level

Four enforcement tests guard architectural invariants — they are not regular unit tests:

- `tests/test_layer_status.py` — every layer `__init__.py` declares `__status__`
- `tests/test_module_dependencies.py` — `alphalens.backtest.*` ⇏ `alphalens.screeners.*`
- `tests/test_lean_config_parity.py` — Docker `SCORER_CONFIG` ↔ host `LEAN_DEFAULTS`
- `tests/test_no_polish_chars.py` — English-only in source

---

## TradingAgents removal (2026-04-30)

AlphaLens previously vendored [`TauricResearch/TradingAgents`](https://github.com/TauricResearch/TradingAgents) as a `git subtree` at `TradingAgents/`. The integration was removed by [ADR 0008](docs/adr/0008-sunset-tradingagents-integration.md) — the worker that drained the candidate queue was dormant after 10/10 paradigm failures, the maintenance tax (custom Gemini 429 retry, deferred upstream PRs, transitive deps) no longer paid for itself, and any future use will happen from a separate clone. The original subtree decision lives on for history at [ADR 0004](docs/adr/0004-tradingagents-as-subtree.md) (status: Superseded).

---

## License

MIT.
