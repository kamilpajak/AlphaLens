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

## Concepts

### Basic terms

- **Ticker** — exchange symbol identifying a stock (e.g. `AAPL`, `NVDA`); the unit of selection in every screener.
- **Asof** — a point-in-time anchor (a date) at which features are computed using only data observable on that date; PIT-correctness means no later-revised data leaks back.
- **Rebalance** — the act of recomputing scores at an `asof` and updating portfolio holdings; in v3-v6 the rebalance stride is 5 trading days with a 20-day holding period, producing 75% overlap (4-tranche).
- **Holdout** — the date range withheld from model fitting and used only for verdict (e.g. 2024-04-30 → 2026-04-30); a strategy is judged by its performance on this unseen slice.

### Statistics & multiple-testing

- **αt (alpha t-stat)** — t-statistic of the Carhart-4F regression intercept; the primary success metric for screener strategies. A Bonferroni-adjusted threshold (typically `|αt| ≥ 2.86` at n=27 tests) is required for PASS.
- **Bonferroni correction** — multiple-testing adjustment that raises the critical t-statistic when N hypotheses share a data window. The project tracks a program-level Bonferroni budget across all experiments in `docs/research/preregistration/ledger.jsonl`; each new test raises the bar for the next.
- **Multi-phase audit** — running the same scorer at strided phase offsets (typically 5 phases, stride=21 days) on the same OOS window. PASS requires every phase to clear floor AND mean αt to clear the Bonferroni threshold. Catches strategies that depend on calendar luck.
- **Phase-robust** — verdict tier where every phase αt ≥ 1.5 AND mean αt ≥ critical AND dispersion ≤ gate (50pp standard, 70pp R2000). The rare positive outcome.
- **HAC / Newey-West** — heteroskedasticity-and-autocorrelation-consistent standard errors. `hac_maxlags` is locked to match the signal's serial-correlation horizon (e.g. 126 trading days for a 6-month signal).
- **Romano-Wolf bootstrap** — block bootstrap producing simultaneous confidence bounds across phases. Block size must encompass the signal window or it underestimates serial correlation.
- **Dispersion gate** — caps the allowed range of αt across phases. `mean ≥ 2.86` with `range > 50pp` flips PASS to INCONCLUSIVE — a "right average for the wrong reason" signature.
- **HARKing** — Hypothesizing After Results Known. Building a hypothesis from observed data then "validating" it on the same data inflates type-I error. Mitigated by pre-registration with frozen params and a SHA256 hash before any holdout look.
- **Burnt holdout** — same OOS window observed across multiple experiments. Each new fit on a burnt window adds to the program-level multiplicity count even if the model class differs (per `feedback_burnt_holdout_multiplicity.md`).
- **Lasso** — L1-regularized linear regression that automatically zeros uninformative feature coefficients; used in alt-data screeners to fit ranking models on the 10-feature whitelist. A CV-zeroed Lasso (every coef = 0) is a red flag — see `feedback_zero_coef_lasso_diagnostic.md`.

### Factor attribution

- **Carhart-4F** — 4-factor regression: market excess return (Mkt-RF), size (SMB), value (HML), momentum (UMD). The default attribution model in `alphalens/attribution/factor_analysis.py`.
- **Fama-French factors** — **Mkt-RF** (market minus risk-free), **SMB** (Small-Minus-Big size factor), **HML** (High-Minus-Low value factor); **UMD** (Up-Minus-Down momentum) is the Carhart extension.
- **Residualization** — projecting the raw signal on a panel of equity controls (typically `reversal_1m`, `momentum_6m`, `rv_30d`) and using the OLS residual as the score. Strips out known cross-sectional drivers so the strategy isn't just a hidden factor bet.
- **Sharpe-as-primary** — for overlay-bearing strategies (Layer 4) the primary success metric is Sharpe improvement, not αt — overlays mechanically modulate beta which biases αt downward (per ADR 0007 "time-varying-beta hazard").

### Data discipline

- **PIT (point-in-time)** — at every `asof`, features use only data that was observable on that date. Restated fundamentals, look-ahead universe membership, and forward-rolling indices all violate PIT. Enforced by `tests/test_pit_universe_loader.py` + `data/store/`.
- **Survivorship bias** — using today's universe to backtest historical asofs gives a free pass on dead companies. The project uses Russell PIT yamls (`data/universes/r{1000,2000,3000}_pit/YYYY-MM.yaml`) keyed to the membership-as-of date.
- **Fire-sale exclusion** — when a ticker is later delisted, drop returns in the 180 days before the delisting date (per `survivorship_pit.DelistingEvent`). Without this, distress signals get +100-300bps inflation from forced-liquidation moves they never could have ridden.
- **First-filed semantics** — for fundamentals (Foster SUE, Sloan accruals), use the value as it was first reported, not as later restated. Restatement-tracking lives in `data/fundamentals/companyfacts_parquet.py`.

### Architecture (5 layers per ADR 0007)

- **Layer 1 — watchdog** — SEC EDGAR event detection + classifier + dispatch (`alphalens/watchdog/`). Production, runs in launchd.
- **Layer 2 — screener** — cross-sectional rank @ asof → top-N tickers (`alphalens/screeners/*`). Selection-gates (`alphalens/gates/`) live here too as Layer 2b.
- **Layer 3 — backtest engine** — runs the scorer over a strided rebalance calendar, returns `BacktestReport` (`alphalens/backtest/engine.py`). Screener-agnostic.
- **Layer 4 — risk overlay** — time-series sizing on portfolio realised vol; modifies *how much exposure*, not *which tickers* (`alphalens/overlays/`). First impl: vol-targeting per Moreira-Muir 2017.
- **Layer 5 — attribution** — cost-drag + Carhart-4F + Sharpe + Bonferroni → ledger verdict (`alphalens/attribution/`).
- **Pre-registration ledger** — append-only `docs/research/preregistration/ledger.jsonl` recording every hypothesis with frozen params, SHA256 hash, hypothesis, gate definition, and final verdict. Forces honest accounting of the multiplicity budget.

### Domain — SEC filings

- **Form 4 / 4-A** — SEC filing reporting an insider's transaction in their company's stock; "/A" is an amendment of a prior filing. Filed by officers, directors, and 10% beneficial owners.
- **Accession number** — unique SEC identifier per filing (e.g. `0001209191-22-000001`); used as the primary key in the parquet store.
- **Cohen-Malloy classifier** — splits insider trades into **routine** (3 consecutive same-month years prior) vs **opportunistic** (everyone else with sufficient history) per JFE 2012 paper, p. 1786. Opportunistic-insider net buys generate +82bps/m abnormal returns in small/mid-caps.

### Verdicts & operational gates

- **Verdict tiers** — every screener experiment lands in one of: **PASS** (phase-robust, every phase clears floor + mean clears Bonferroni), **PASS_MARGINAL** (mean clears critical but dispersion or weakest phase below floor), **INCONCLUSIVE** (mean ∈ [floor, critical) — interesting but not significant), **FAIL** (mean < floor or every phase < 0).
- **Phase A auto-pivot** — pre-flight checks run on TRAIN before burning multi-phase OOS compute. Failing breadth (`BREADTH-FAIL`: <30% asof-quarters with ≥50 scored tickers), density (`DENSITY-FAIL`: <2 events per ticker/quarter), or direction (`DIRECTION-FAIL`: TRAIN ρ(score, fwd_excess) ≤ -0.05 — sign-flipped) abandons the experiment with a one-shot Bonferroni cost instead of a 5-phase one.
- **7-gate kill verdict** — every CLOSED layer ships a structured `__closed_evidence__` dict mapping 7 gates (Carhart-4F HAC, sanity_checks_4gate, walk_forward_oos, multiple_testing_correction, cost_drag, bootstrap_ci, survivorship_pit) to evidence paths. Schema in `docs/research/kill_verdict_checklist.md`, enforced by `tests/test_layer_status.py`.

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
