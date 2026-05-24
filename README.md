# AlphaLens

[![CI](https://github.com/kamilpajak/AlphaLens/actions/workflows/ci.yml/badge.svg)](https://github.com/kamilpajak/AlphaLens/actions/workflows/ci.yml)
[![Quality Gate](https://sonarcloud.io/api/project_badges/measure?project=kamilpajak_AlphaLens&metric=alert_status)](https://sonarcloud.io/summary/new_code?id=kamilpajak_AlphaLens)
[![Coverage](https://sonarcloud.io/api/project_badges/measure?project=kamilpajak_AlphaLens&metric=coverage)](https://sonarcloud.io/summary/new_code?id=kamilpajak_AlphaLens)
[![Python 3.13](https://img.shields.io/badge/python-3.13-blue.svg)](https://www.python.org/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Research lab infrastructure for retail active alpha experimentation — real-time SEC EDGAR event detection, quantitative screening, a vectorised backtest engine for paradigm validation, and a daily-brief web app served from Django + Postgres.

## What's here

The repo is a small monorepo with three Python workspace members + a frontend + shared infra:

- **`apps/alphalens-pipeline/`** — live production tier: `alphalens_pipeline` (edgar_detector, thematic build, literature_scanner, data clients, scorer library) + the `alphalens` Typer CLI binary. Split rationale: [ADR 0011](docs/adr/0011-split-pipeline-and-research.md).
- **`apps/alphalens-research/`** — research lab: `alphalens_research` (screeners, backtest engine, attribution, overlays, gates, preaudit, diagnostics, paper-trade). Lab imports from pipeline (`data`, `core`, `scorers`); the reverse is forbidden at top level (enforced by `tests/test_module_dependencies.py`).
- **`apps/alphalens-django/`** — read/write briefs API (Django 6 + DRF + Postgres + Cloudflare Access). Migration history in [ADR 0009](docs/adr/0009-django-replaces-fastapi.md).
- **`apps/web/`** — SvelteKit + Tailwind dashboard that consumes the Django API.
- **`deploy/`** — all deploy targets: `docker/` (pipeline + django-prod), `systemd/` (Linux VPS units), `launchd/` (5 live macOS jobs), `runpod/` (GPU/CPU pod bootstrap).
- **`docs/adr/`** — 11 ADRs covering the load-bearing decisions.

Architectural detail and quick contributor guide: [`CLAUDE.md`](CLAUDE.md). Per-layer postmortems: [`docs/research/paradigm_failures_postmortem.md`](docs/research/paradigm_failures_postmortem.md).

## Status (2026-05-22)

Two parallel research tracks active:

1. **Factor paradigm search** — paradigm #14 PEAD v2 audit running on VPS (gated on AV free-tier 21-day backfill). 14 paradigm-class failures + 2 inconclusive retrospectives in the postmortem ledger. Capital deployment off the table per pre-reg `capital_deploy_clause`.
2. **Thematic event-driven assistant** — MVP Phase A-E shipped 2026-05-17. Buy-side decision support for a private WhatsApp investing group; tool is augmentation, not replacement.

The first phase-robust positive observation landed 2026-05-09 — PASS_MARGINAL on Cohen-Malloy opportunistic Form-4 — replicated across two independent OOS windows. PASS_MARGINAL is not a full PASS; it unlocks eligibility for advanced overlay testing, not capital deployment.

Live in production:
- Layer 1 SEC EDGAR detector (launchd `edgar-detect` every 15 min)
- Literature review (Perplexity, monthly + weekly)
- Paper-trade refresh + scorer (Sun 17:00, Mon 06:00)
- VPS daily thematic pipeline → Django brief API → SvelteKit dashboard

Everything else is RESEARCH_ONLY (active research scorers) or live infrastructure (data clients, backtest engine, attribution).

## Layer status

Each layer / screener package declares `__status__ ∈ {ACTIVE, CLOSED, RESEARCH_ONLY, ARCHIVED}` in its `__init__.py`; enforced by `apps/alphalens-research/tests/test_layer_status.py`.

### Live production (`apps/alphalens-pipeline/alphalens_pipeline/`)

| Path | Status | Notes |
|------|--------|-------|
| `alphalens_pipeline/core/` | ACTIVE (namespace) | Plumbing — candidates, queue |
| `alphalens_pipeline/edgar_detector/` | ACTIVE | Layer 1 — `detect` live in launchd |
| `alphalens_pipeline/literature_scanner/` | ACTIVE | Monthly + weekly Perplexity scan, live in launchd |
| `alphalens_pipeline/thematic/` | ACTIVE | Daily thematic pipeline (news → brief), live on VPS |
| `alphalens_pipeline/data/` | ACTIVE (namespace) | PIT SoT + vendor clients + S&P 400/500/600 PIT yamls |
| `alphalens_pipeline/scorers/` | ACTIVE | Reusable validated-scorer library |

### Research lab (`apps/alphalens-research/alphalens_research/`)

| Path | Status | Notes |
|------|--------|-------|
| `alphalens_research/backtest/` | ACTIVE | Layer 3 engine — screener-agnostic |
| `alphalens_research/attribution/` | ACTIVE | Layer 5 — cost / factor / regime / verdict |
| `alphalens_research/preaudit/` | ACTIVE | Per-strategy SmokeProfile + coverage gate |
| `alphalens_research/diagnostics/` | ACTIVE | Survivorship + cyclicality screens |
| `alphalens_research/paper_trade/` | ACTIVE | Forward-observation refresh + scorer, live in launchd |
| `alphalens_research/gates/` | RESEARCH_ONLY | Layer 2 selection-gate wrapper |
| `alphalens_research/overlays/` | RESEARCH_ONLY | Layer 4 sizing overlays |
| `alphalens_research/screeners/*` | RESEARCH_ONLY | Active research scorers; per-strategy memos in `docs/research/` |

Closed paradigms used to live under `alphalens_research/archive/`; reusable scorers were promoted to `alphalens_pipeline/scorers/` and the rest was removed per [ADR 0010](docs/adr/0010-archive-extracted-and-removed.md).

External methodology dep: [`kamilpajak/phase-robust-backtesting`](https://github.com/kamilpajak/phase-robust-backtesting) (MIT) — see [ADR 0006](docs/adr/0006-phase-robust-backtesting-extraction.md).

## Concepts

### Basic terms

- **Ticker** — exchange symbol identifying a stock; the unit of selection in every screener.
- **Asof** — a point-in-time anchor at which features are computed using only data observable on that date.
- **Rebalance** — recomputing scores at an asof and updating holdings. Default stride 5 trading days, 20-day holding (75% overlap, 4-tranche).
- **Holdout** — the date range withheld from model fitting and used only for verdict.

### Statistics & multiple-testing

- **αt** — t-stat of the Carhart-4F regression intercept; primary success metric. Bonferroni-adjusted threshold typically `|αt| ≥ 2.86` at n=27 tests.
- **Bonferroni correction** — multiple-testing adjustment that raises critical t when N hypotheses share a data window. Program-level Bonferroni budget tracked in `docs/research/preregistration/ledger.json`.
- **Multi-phase audit** — same scorer at strided phase offsets (typically 5 phases, stride=21 days) on the same OOS window. PASS requires every phase to clear floor AND mean αt to clear Bonferroni.
- **Phase-robust** — every phase αt ≥ 1.5 AND mean αt ≥ critical AND dispersion ≤ gate.
- **HAC / Newey-West** — heteroskedasticity-and-autocorrelation-consistent SE. `hac_maxlags` is locked to match signal serial-correlation horizon.
- **Romano-Wolf bootstrap** — block bootstrap producing simultaneous confidence bounds across phases.
- **Dispersion gate** — caps allowed range of αt across phases. `mean ≥ 2.86` with `range > 50pp` flips PASS → INCONCLUSIVE.
- **HARKing** — Hypothesising After Results Known. Mitigated by pre-registration with frozen params and SHA256 hash before any holdout look.
- **Burnt holdout** — same OOS window observed across multiple experiments. Each new fit adds to program-level multiplicity even if model class differs.

### Factor attribution

- **Carhart-4F** — 4-factor regression: Mkt-RF, SMB, HML, UMD. Default in `apps/alphalens-research/alphalens_research/attribution/factor_analysis.py`.
- **Fama-French factors** — **Mkt-RF**, **SMB** (size), **HML** (value); **UMD** (momentum) is the Carhart extension.
- **Residualisation** — projecting the raw signal on a panel of equity controls (`reversal_1m`, `momentum_6m`, `rv_30d`) and using the OLS residual as the score.
- **Sharpe-as-primary** — for overlay-bearing strategies (Layer 4) primary metric is Sharpe improvement, not αt — overlays modulate beta which biases αt downward.

### Data discipline

- **PIT (point-in-time)** — at every asof, features use only data observable on that date. Enforced by `apps/alphalens-research/tests/test_pit_universe_loader.py` + `alphalens_pipeline/data/store/`.
- **Survivorship bias** — Russell PIT yamls (`alphalens_pipeline/data/universes/r{1000,2000,3000}_pit/YYYY-MM.yaml`) keyed to membership-as-of date.
- **Fire-sale exclusion** — drop returns 180 days before delisting date (per `survivorship_pit.DelistingEvent`). Without this, distress signals get +100-300bps inflation.
- **First-filed semantics** — for fundamentals, use the value as first reported, not later restated.

### Architecture (5 layers per ADR 0007)

1. **Watchdog** (`alphalens_pipeline/edgar_detector/`) — SEC EDGAR event detection + classifier + dispatch.
2. **Screener** (`alphalens_research/screeners/*`) — cross-sectional rank @ asof → top-N tickers.
3. **Backtest engine** (`alphalens_research/backtest/engine.py`) — strided rebalance → `BacktestReport`.
4. **Risk overlay** (`alphalens_research/overlays/`) — time-series sizing on portfolio realised vol.
5. **Attribution** (`alphalens_research/attribution/`) — cost-drag + Carhart-4F + Sharpe + Bonferroni → ledger verdict.

Pre-registration ledger (`docs/research/preregistration/ledger.json`) records every hypothesis with frozen params and SHA256 hash before holdout look.

### Domain — SEC filings

- **Form 4 / 4-A** — SEC filing reporting an insider's transaction; "/A" is an amendment.
- **Accession number** — unique SEC identifier per filing; primary key in the parquet store.
- **Cohen-Malloy classifier** — splits insider trades into **routine** vs **opportunistic** per JFE 2012. Opportunistic-insider net buys generate +82bps/m abnormal returns in small/mid-caps.

### Verdicts & operational gates

- **Verdict tiers** — **PASS** (phase-robust, every phase clears floor + mean clears Bonferroni), **PASS_MARGINAL** (mean clears critical but dispersion or weakest phase below floor), **INCONCLUSIVE** (mean ∈ [floor, critical)), **FAIL** (mean < floor or every phase < 0).
- **Phase A auto-pivot** — pre-flight checks on TRAIN before burning OOS compute. Failing breadth / density / direction abandons the experiment with a one-shot Bonferroni cost.
- **7-gate kill verdict** — every CLOSED layer ships `__closed_evidence__` mapping 7 gates → evidence paths. Schema: `docs/research/kill_verdict_checklist.md`; enforced by `tests/test_layer_status.py`.

## Quickstart

### Prerequisites

- macOS for launchd scheduling (CLI itself runs anywhere)
- Python 3.13 via [`uv`](https://github.com/astral-sh/uv)
- API keys: Google AI (Gemini), Alpha Vantage, Telegram bot; optional Polygon

### Setup

```bash
git clone git@github.com:kamilpajak/AlphaLens.git
cd AlphaLens
uv venv --python 3.13
uv sync
```

`.env` at repo root:

```
GOOGLE_API_KEY=...
ALPHA_VANTAGE_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
POLYGON_API_KEY=...
```

Layer 1 routing config at `~/.alphalens/edgar-detect/portfolio.yaml`:

```yaml
held:    [AAPL, MSFT]
watchlist: [NVDA, GOOGL]
```

### Running things

```bash
# Live workflows
.venv/bin/alphalens edgar detect                 # Layer 1: poll EDGAR, classify, dispatch
.venv/bin/alphalens status                     # global queue + digest + dedup
.venv/bin/alphalens literature monthly         # ad-hoc Perplexity deep scan (~1h)
.venv/bin/alphalens literature weekly          # ad-hoc weekly RSS scan (~15min)

# Paper-trade prospective replication
.venv/bin/alphalens paper-trade refresh-data --strategy v9d
.venv/bin/alphalens paper-trade score --strategy v9d
.venv/bin/alphalens paper-trade verdict --strategy v9d

# Pre-registration ledger
.venv/bin/alphalens preregister add ...
.venv/bin/alphalens preregister threshold
.venv/bin/alphalens preregister complete <id> --verdict ...

# Multi-phase audit (canonical research gate)
.venv/bin/alphalens audit insider_form4_opportunistic \
    --is-start 2018-01-01 --is-end 2023-12-31 --rebalance-stride 21

# Tests (unittest, not pytest)
.venv/bin/python -m unittest discover apps/alphalens-research/tests -t apps/alphalens-research -v

# Inspect the queue directly
sqlite3 ~/.alphalens/candidates.db \
  "SELECT id, ticker, source, priority, status, decision FROM candidates ORDER BY id DESC LIMIT 20;"
```

### Scheduled jobs (launchd)

| Job | When | Purpose |
|---|---|---|
| `com.alphalens.edgar-detect` | every 15 min | Layer 1 EDGAR poll → submit Candidates |
| `com.alphalens.literature-scan-monthly` | 1st of month, 09:00 | Perplexity deep literature scan |
| `com.alphalens.literature-scan-weekly` | Sunday, 18:00 | Perplexity RSS scan |
| `com.alphalens.paper-trade-track` | Sunday, 17:00 | Paper-trade portfolio refresh |

Install:

```bash
cp deploy/launchd/com.alphalens.*.plist ~/Library/LaunchAgents/
for plist in com.alphalens.edgar-detect \
             com.alphalens.literature-scan-monthly \
             com.alphalens.literature-scan-weekly \
             com.alphalens.paper-trade-track \
  launchctl load ~/Library/LaunchAgents/${plist}.plist
done
```

## Layout

```
apps/
├── alphalens-pipeline/          ← live production tier (Python)
│   ├── alphalens_pipeline/      ← edgar_detector, thematic, literature_scanner, data, core, scorers
│   ├── alphalens_cli/           ← Typer CLI entry points (alphalens binary)
│   └── data/                    ← S&P 400/500/600 PIT yamls
├── alphalens-research/          ← research lab (Python)
│   ├── alphalens_research/      ← screeners, backtest, attribution, overlays, gates, preaudit, diagnostics, paper_trade
│   ├── tests/                   ← unittest suite (~2000+ tests; architectural enforcers — pipeline + research together)
│   └── scripts/                 ← experiment runners + backfill orchestrators
├── alphalens-django/            ← briefs API (Django 6 + DRF + Postgres)
│   ├── briefs/                  ← models + ingest + /v1/* viewsets
│   ├── auth_cf/                 ← Cloudflare Access JWT
│   └── config/                  ← settings split (base/dev/prod)
└── web/                         ← SvelteKit + Tailwind dashboard

deploy/
├── docker/
│   ├── Dockerfile.pipeline      ← pipeline image (thematic daily)
│   └── django-prod/             ← Django + nginx + Postgres compose
├── systemd/                     ← VPS user units (form4-backfill, av-earnings, thematic build)
├── launchd/                     ← macOS scheduled jobs (5 live)
└── runpod/                      ← GPU/CPU pod bootstrap + experiment runner

docs/
├── adr/                         ← 11 Architecture Decision Records
├── research/                    ← paradigm postmortems, design memos, ledger
└── backtest/                    ← historical run outputs
```

## Development

- **Package manager**: `uv` (not pip / poetry)
- **Testing**: unittest (not pytest) — `python -m unittest discover apps/alphalens-research/tests -t apps/alphalens-research`
- **Commits**: Conventional Commits (`feat(scope):`, `fix(scope):`, `refactor(scope):`, …)
- **Code language**: English in source; enforced by `tests/test_no_polish_chars.py`
- **New components** — pick the side per the [ADR 0011](docs/adr/0011-split-pipeline-and-research.md) DAG: infra / live services / data clients / scorer libraries → `apps/alphalens-pipeline/alphalens_pipeline/<name>/`; lab / backtest / attribution / overlays / preaudit / experiments → `apps/alphalens-research/alphalens_research/<name>/`; CLI commands → `apps/alphalens-pipeline/alphalens_cli/`; Django app → `apps/alphalens-django/`.

Five enforcement tests guard architectural invariants (all in `apps/alphalens-research/tests/`):

- `test_layer_status.py` — every layer declares `__status__` + 7-gate `__closed_evidence__` for CLOSED/ARCHIVED.
- `test_module_dependencies.py` — intra-research: `alphalens_research.backtest.*` ⇏ `alphalens_research.screeners.*` and `alphalens_research.backtest.*` ⇏ `alphalens_research.attribution.*`. Workspace DAG: `alphalens_pipeline.*` ⇏ `alphalens_research.*` at top level (lazy CLI imports are the documented exception).
- `test_lean_config_parity.py` — kept for the legacy data layer; will retire when its last consumer is gone.
- `test_no_polish_chars.py` — English-only in source.
- `test_preaudit_cli_default_in_sync.py` — pins the duplicated `DEFAULT_SMOKE_TIMEOUT_S` constant between the CLI-side typer.Option default and the research-side runner.

## License

MIT.
