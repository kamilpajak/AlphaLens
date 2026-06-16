# AlphaLens

[![CI](https://github.com/kamilpajak/AlphaLens/actions/workflows/ci.yml/badge.svg)](https://github.com/kamilpajak/AlphaLens/actions/workflows/ci.yml)
[![Quality Gate](https://sonarcloud.io/api/project_badges/measure?project=kamilpajak_AlphaLens&metric=alert_status)](https://sonarcloud.io/summary/new_code?id=kamilpajak_AlphaLens)
[![Coverage](https://sonarcloud.io/api/project_badges/measure?project=kamilpajak_AlphaLens&metric=coverage)](https://sonarcloud.io/summary/new_code?id=kamilpajak_AlphaLens)
[![Python 3.13](https://img.shields.io/badge/python-3.13-blue.svg)](https://www.python.org/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![License: PolyForm Noncommercial 1.0.0](https://img.shields.io/badge/License-PolyForm_Noncommercial_1.0.0-blue.svg)](https://polyformproject.org/licenses/noncommercial/1.0.0/)

AlphaLens is a personal research-lab and decision-support monorepo for retail active-equity research. It combines real-time SEC EDGAR event detection, a daily thematic event-driven brief (news → ranked candidate cards surfaced in a web dashboard), and a rigorous quant backtest/attribution engine for factor-paradigm validation. It is decision **support**, not automated trading — capital deployment is off-table and there is no standing strategy PASS.

## What's inside

- **Live EDGAR detector** — Layer 1 SEC EDGAR poller that classifies new filings and queues candidates, firing every 15 minutes in production.
- **Daily thematic brief pipeline + web dashboard** — ingest news → LLM theme extraction → beneficiary mapping → scorer screen → daily brief of ranked candidate cards. Each card carries an **expert panel** (Buffett value/quality + O'Neil momentum lenses, with a disagreement spread; display-only).
- **EDGE market-behavior feedback** — the `/edge` dashboard plus a broker-free **ladder monitor** that replays trade-setup ladders over price paths to measure market behavior (the sole go-forward feedback metric; N≥30 gated).
- **Backtest / attribution research lab** — a screener-agnostic replay engine and a 5-layer attribution stack producing risk-adjusted metrics and a GO/KILL verdict for factor-paradigm audits.
- **Literature scanner** — periodic Perplexity literature review (monthly deep scan + weekly RSS), auto-committed to `main`.

## Architecture

A uv workspace split into live infrastructure and a research lab:

```
apps/
  alphalens-pipeline/   # live services + data clients + validated scorers; the `alphalens` CLI
  alphalens-research/   # backtest engine, attribution, screeners, overlays (research lab)
  alphalens-django/     # Django 6 + DRF API serving briefs, edge, market-status (/v1/*)
  web/                  # SvelteKit SPA dashboard (briefs, /edge, /experiments)
deploy/                 # Dockerfiles, compose stacks, systemd-user units, monitoring
docs/                   # ADRs + research memos + backtest archive
```

The pipeline/research split is one-way: `alphalens_research.*` may import live infrastructure (`data`, `core`, `scorers`), but `alphalens_pipeline.*` must not import from the research lab at top level. This is machine-enforced — see [ADR 0011](docs/adr/0011-split-pipeline-and-research.md) for the workspace split and [ADR 0007](docs/adr/0007-layer-architecture.md) for the layer architecture.

## Tech stack

Python 3.13 + [uv](https://github.com/astral-sh/uv) · Typer CLI · Django 6 + DRF + Postgres · SvelteKit + Tailwind CSS · Cloudflare Pages + Access · Docker + systemd on a Linux VPS.

## Quickstart

```bash
# Clone and install the workspace (single venv at ./.venv)
git clone https://github.com/kamilpajak/AlphaLens.git
cd AlphaLens
uv sync                       # both Python apps + dev tools
pnpm --filter web install     # web dependencies
```

Set API keys in `.env` at the repo root (see [`.env.example`](.env.example) for the full catalogue):

```
OPENROUTER_API_KEY=...        # DeepSeek v4 Pro/Flash — all LLM calls
ALPHA_VANTAGE_API_KEY=...
POLYGON_API_KEY=...
PERPLEXITY_API_KEY=...
FRED_API_KEY=...
TELEGRAM_BOT_TOKEN=...        # + TELEGRAM_CHAT_ID for alerts
SEC_EDGAR_USER_AGENT=...      # SEC contact string (built-in default locally)
```

Example commands (the CLI binary is `alphalens`, via `.venv/bin/alphalens` or `uv run alphalens`):

```bash
alphalens status                              # global queue + digest + dedup
alphalens edgar detect                        # Layer 1: poll EDGAR, classify, queue
alphalens thematic score                      # run a thematic pipeline stage
alphalens literature scan --window weekly     # ad-hoc literature scan
```

Run the research-lab test suite (unittest, not pytest):

```bash
uv run python -m unittest discover \
    -s apps/alphalens-research/tests \
    -t apps/alphalens-research -v
```

Or use the orchestrator: `just test` (Python + Django + web), `just lint`, `just dev-django`, `just dev-web`.

## Production

The SvelteKit SPA is hosted on **Cloudflare Pages**; the Django API runs on a Linux VPS as a Docker Compose stack (image published to GHCR), reached through a **Cloudflare Tunnel** and gated by **Cloudflare Access** (Zero Trust, Google SSO). Scheduled jobs — EDGAR detection, the thematic build, feedback backfills, Form-4 ingest, literature scans — run as **systemd-user units** on the VPS. See [`deploy/systemd/README.md`](deploy/systemd/README.md) and [`deploy/docker/README.md`](deploy/docker/README.md) for the operator runbooks.

## Docs

- [`CONTRIBUTING.md`](CONTRIBUTING.md) — how to contribute, DCO sign-off, contribution licensing
- [`CLAUDE.md`](CLAUDE.md) — architecture and contributor guide (layer statuses, conventions, doctrine)
- [`docs/adr/`](docs/adr/) — architectural decision records
- [`docs/research/paradigm_failures_postmortem.md`](docs/research/paradigm_failures_postmortem.md) — catalogue of closed factor paradigms with kill rationale

## License

[PolyForm Noncommercial License 1.0.0](LICENSE) — the source is open to read, study, fork, and use for any **noncommercial** purpose. Commercial use (using the software for commercial advantage or monetary compensation) is not permitted. This is a source-available license, not an OSI "open source" license.
