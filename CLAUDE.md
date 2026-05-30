# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repo.

## Project status (snapshot)

**Two parallel research tracks active:**

1. **Factor-paradigm-search** — paradigm #14 PEAD v2 audit in-flight (gated on VPS av_cache backfill, ~21d AV free-tier quota). 14 prior paradigm-class failures + 2 inconclusive retrospectives + 1 slippage-fail catalogued in [`docs/research/paradigm_failures_postmortem.md`](docs/research/paradigm_failures_postmortem.md). No standing PASS; capital deployment off-table per pre-reg `capital_deploy_clause`.

2. **Thematic event-driven research assistant** — MVP Phase A-E shipped 2026-05-17 (PRs #128-#134). Buy-side decision-support tool augmenting WhatsApp investing group workflow. Tool is **augmentation, NOT replacement** — user cherry-picks → group discusses → each member decides. Design: [`docs/research/thematic_event_tool_v1_design_2026_05_15.md`](docs/research/thematic_event_tool_v1_design_2026_05_15.md). Remaining: Telegram bot, Form-4 independent path, feedback ledger sqlite.

**Live production:**
- Layer 1 SEC EDGAR detector (launchd `edgar-detect`-only; `worker` archived per ADR 0008)
- Literature review weekly + monthly Perplexity scan
- VPS daily thematic pipeline + API rebuild (Django pulled from GHCR per migration B); SvelteKit dashboard hosted on Cloudflare Pages, fronted by Access (Google SSO, Path A same-domain cookies). See `## VPS backfills` + `## Production topology (migration B)`.

**Everything else RESEARCH_ONLY** — code remains as reusable framework. Closed paradigms were extracted (reusable scorers promoted to live packages) and the rest removed per [ADR 0010](docs/adr/0010-archive-extracted-and-removed.md), superseding ADR 0005. Methodology bundle MIT-licensed as [`kamilpajak/phase-robust-backtesting`](https://github.com/kamilpajak/phase-robust-backtesting) per [ADR 0006](docs/adr/0006-phase-robust-backtesting-extraction.md). Search for better screeners stays open-ended — each new test raises the Bonferroni bar via ledger discipline, but "no further prospecting" is **not** a project position.

## Layer status

Lifecycle status of each layer lives in its `__init__.py` as the `__status__` constant (enforced by `apps/alphalens-research/tests/test_layer_status.py`). The workspace splits live infrastructure from the research lab per [ADR 0011](docs/adr/0011-split-pipeline-and-research.md):

### Live production (`apps/alphalens-pipeline/alphalens_pipeline/`)

| Path | Status | Notes |
|------|--------|-------|
| `alphalens_pipeline/core/` | ACTIVE (namespace) | plumbing — candidates, queue |
| `alphalens_pipeline/edgar_detector/` | ACTIVE | Layer 1 — `detect` live in launchd |
| `alphalens_pipeline/literature_scanner/` | ACTIVE | Monthly + weekly Perplexity scan, live in launchd |
| `alphalens_pipeline/thematic/` | ACTIVE | Daily thematic pipeline, live on VPS |
| `alphalens_pipeline/data/` | ACTIVE (namespace) | data infrastructure — `data/store/` PIT SoT, `data/{alt_data,fundamentals,macro}/` clients, `data/factors.py`, `data/universes/` |
| `alphalens_pipeline/scorers/` | ACTIVE | reusable validated-scorer library (fcff_yield, cohen_malloy_classifier, opportunistic_form4) |

The CLI binary `alphalens` is registered in `apps/alphalens-pipeline/pyproject.toml` and lives under `apps/alphalens-pipeline/alphalens_cli/`. Research-side commands (`audit`, `preaudit`, `preregister`) lazy-import the lab tier inside command bodies so the pipeline package has zero top-level imports from research.

### Research lab (`apps/alphalens-research/alphalens_research/`)

| Path | Status | Notes |
|------|--------|-------|
| `alphalens_research/screeners/prescreener/` | RESEARCH_ONLY | Layer 2a — unvalidated, manual ad-hoc |
| `alphalens_research/screeners/momentum_lowvol/` | RESEARCH_ONLY | Layer 2 mom + low-vol — strategy FAILed; scorer reused as base for Layer 4 vol-target overlay test |
| `alphalens_research/gates/` | RESEARCH_ONLY | Layer 2 selection-gates wrapper |
| `alphalens_research/backtest/` | ACTIVE | Layer 3 engine — engine, multi_phase, multiple_testing, weighting, theme_analysis, llm_scorers, historical_validation, metrics |
| `alphalens_research/overlays/` | RESEARCH_ONLY | Layer 4 risk-overlays; single occupant `vol_target.py` |
| `alphalens_research/attribution/` | ACTIVE | Layer 5 — cost_model, factor_analysis, regime, decision_matrix, diagnostics, report, walk_forward |
| `alphalens_research/preaudit/` | ACTIVE | per-strategy SmokeProfile + coverage gate before audit launch |
| `alphalens_research/diagnostics/` | ACTIVE | survivorship_pit, cyclicality screens |
| `alphalens_research/retrospective_audit/` | RESEARCH_ONLY | PIT universe loaders (U1/U2/U3) + SMD cache primitives for one-shot retrospectives |

**Methodology bundle** (preregistration ledger, multi_phase audit, multiple_testing thresholds, audit_multi_phase driver) consumed via external dep `phase-robust-backtesting>=0.2.0` — see [ADR 0006](docs/adr/0006-phase-robust-backtesting-extraction.md). `alphalens audit <strategy>` (`apps/alphalens-pipeline/alphalens_cli/commands/audit.py`) resolves a short strategy name to a file path and delegates in-process to `phase_robust_backtesting.audit_multi_phase.run_audit`.

## Layer architecture (active alpha experimentation)

Five-layer separation per **[ADR 0007](docs/adr/0007-layer-architecture.md)**. Each layer has a single responsibility; failures attribute to one layer:

1. **Screener** (`alphalens_research/screeners/*`) — cross-sectional rank @ time t → top-N tickers
2. **Selection-gate** (`alphalens_research/gates/`) — binary/graded gate on the Scorer (modifies *which* tickers deploy)
3. **Backtest engine** (`alphalens_research/backtest/engine.py`) — runs scorer over strided rebalance calendar → `BacktestReport.portfolio_returns`
4. **Risk overlay** (`alphalens_research/overlays/`) — time-series sizing on portfolio realised vol (modifies *how much exposure*); first impl is vol-targeting per Moreira-Muir 2017
5. **Attribution** (`alphalens_research/attribution/{cost_model, factor_analysis, regime, ...}`) — cost-drag, Carhart-4F, Sharpe, Bonferroni → ledger verdict. Engine-side primitives (`rank_ic`, `turnover_pct`, `sharpe`) live in `alphalens_research/backtest/metrics.py`.

Compound hypotheses combine layers (e.g. mom+lowvol × VIX>20 gate × vol-target overlay), each combination paying its own Bonferroni cost. **Time-varying-beta hazard:** overlay-bearing strategies use Sharpe-improvement (not Carhart α t-stat) as primary success metric — see ADR 0007.

**Dependency direction across the workspace split:** `alphalens_research.*` may import from `alphalens_pipeline.{data, core, scorers}` (lab consumes infra). `alphalens_pipeline.*` must not import from `alphalens_research.*` at top level — only lazy imports inside CLI command bodies are allowed. Enforced by `apps/alphalens-research/tests/test_module_dependencies.py` (ast.NodeVisitor walk that catches `import X`, `from X import Y`, and TYPE_CHECKING / try-except / with-nested forms).

## Commands

```bash
# Setup (fresh clone) — single workspace venv at ./.venv
uv sync                                          # both apps + dev tools

# Common orchestrator recipes (justfile)
just sync                                        # uv sync + pnpm install (web)
just test                                        # research + django + web in series
just lint                                        # ruff + svelte-check
just dev-django  / just dev-web                  # local dev servers
just up / just down                              # Django prod compose stack

# Direct invocations
uv run python -m unittest discover \
    -s apps/alphalens-research/tests \
    -t apps/alphalens-research -v
.venv/bin/alphalens edgar detect                   # Layer 1: poll EDGAR, classify, dispatch
.venv/bin/alphalens status                       # global queue + digest + dedup
.venv/bin/alphalens literature monthly           # ad-hoc deep literature scan (Perplexity high)
.venv/bin/alphalens literature weekly            # ad-hoc weekly RSS scan
```

Closed paradigms used to ship CLI replay tooling; that surface was removed per [ADR 0010](docs/adr/0010-archive-extracted-and-removed.md). Failure rationale lives in `docs/research/paradigm_failures_postmortem.md`.

## Conventions

**Status markers** — each layer/screener `__init__.py` declares `__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"]` plus `__closed_date__`, `__closed_reason__`, and `__closed_evidence__: dict[str, str]` (mapping 7 gates → evidence path) if `__status__ ∈ {CLOSED, ARCHIVED}`. Schema: `docs/research/kill_verdict_checklist.md`. New layer requires updating `LAYERS_WITH_STATUS` in `apps/alphalens-research/tests/test_layer_status.py`.

**English-only in code** — comments, docstrings, identifiers in English. Math notation (α, ρ, ×, −) stays. Polish prose lives in CLAUDE.md, MEMORY, conversations, commit messages, postmortems. Enforcement: `apps/alphalens-research/tests/test_no_polish_chars.py`.

**Dependency direction** — enforcement rules in `apps/alphalens-research/tests/test_module_dependencies.py`:
- `alphalens_research.backtest.*` does NOT import from `alphalens_research.screeners.*` (exemption: `historical_validation.py`)
- `alphalens_research.backtest.*` does NOT import from `alphalens_research.attribution.*` (Layer 3 → Layer 5 direction; engine produces `BacktestReport`, attribution consumes)
- `alphalens_pipeline.*` does NOT import from `alphalens_research.*` at top level (workspace DAG — lazy imports inside `alphalens_cli.commands.{audit,preaudit,preregister}` command bodies are the documented exception)

**Config parity** — `SCORER_CONFIG` in `lean_project/main.py` (Docker-inlined) must match `LEAN_DEFAULTS` on shared keys. Enforcement: `apps/alphalens-research/tests/test_lean_config_parity.py`.

**Lazy CLI imports** — `apps/alphalens-pipeline/alphalens_cli/commands/research.py` intentionally does NOT promote cross-function duplicates to top-level. Measured +913ms regression in `alphalens` startup time per invoke (Layer 1 edgar-detect cron fires often). The same pattern keeps `pipeline → research` from leaking into top-level imports across the workspace split.

**No backward compatibility** — solo project, zero external users. Rename, refactor, drop old behavior in one commit without aliases.

**New components** — pick the side per the [ADR 0011](docs/adr/0011-split-pipeline-and-research.md) DAG: infra / live services / data clients / scorer libraries → `apps/alphalens-pipeline/alphalens_pipeline/<name>/`; lab / backtest / attribution / overlays / preaudit / experiments → `apps/alphalens-research/alphalens_research/<name>/`; CLI commands → `apps/alphalens-pipeline/alphalens_cli/`. Never top-level.

**Web — atomic tokens never wrap mid-string** — in `apps/web`, any token that reads wrong when split across two lines (dates `YYYY-MM-DD`, math notation / formulas like `α=-2.01` or `L = λW`, numeric ranges `20-40 bps`, version strings, tickers) must carry Tailwind `whitespace-nowrap` on its wrapping element. Why: CSS treats `-` (hyphen-minus) and other separators as valid line-break opportunities, so `2026-05-25` can break to `2026-` / `05-25`. Apply rule: token inline with wrapping prose → wrap ONLY the token in its own `<span class="whitespace-nowrap">`; token sharing a flex row with an icon (e.g. chevron nav button) → put `whitespace-nowrap` on the flex anchor itself. Established PR #261 (all date sites). See [[feedback_web_nowrap_atomic_tokens_2026_05_27]].

**Paper-trade harness is exchange-agnostic** — every calendar / submitter / TTL / time-stop / sizing helper accepts an `exchange` parameter (ISO 10383 MIC) defaulting to `"XNYS"`. Adding XWAR (Polish), XTKS (Tokyo), XHKG, XSHG is a per-call argument change, not a refactor. Today's surfaces: `alphalens_pipeline/paper/calendar.py` (+ tests cover XNYS × XWAR side-by-side), `alphalens_pipeline/paper/{reconciler,exit_manager,planner}.py`, `alphalens_cli/commands/paper.py`, and Django-side `alphalens-django/market/calendar.py` + `/v1/market/status` endpoint. Plug-in points beyond just the calendar: per-ticker exchange routing, per-exchange broker client (Alpaca is US-only), FX leg in sizing, per-venue UI tz in `MarketStatusBanner.svelte`, per-venue weekend-session cutoff in `next_trading_open_utc` (current 23:59 UTC is XNYS-safe). Rule: if a new helper would need to be rewritten when the second exchange lands, refactor BEFORE merging. See [[project_exchange_agnostic_calendar_2026_05_30]] + `docs/research/paper_trading_non_trading_day_2026_05_29.md` §6.

## Workflow conventions

**TDD always** — production code is always red→green→refactor, even 2-line fixes. Write test first.

**Quality over speed** — never downgrade models / data sources to avoid rate limits. Wait, cache, throttle — don't lower precision.

**runpod = primary compute** — experiments (audits, holdouts, smoke N>50) run on runpod.io CPU pods. Local Mac is for code editing + tiny sanity checks. OOM-class issues default to "ship to runpod" rather than laptop-fit refactor.

**Proceed continuously between phases** — approved plan = green light for all phases; chain N→N+1 without per-phase confirmation. Stop only on blocker or destructive action.

**Cache iVolatility downloads** — persist raw API responses to `~/.alphalens/ivolatility_cache/` BEFORE processing; never re-fetch on retry/iteration ($399/mo metered).

**gh CLI repo scope** — always pass `--repo kamilpajak/AlphaLens` with `gh pr comment/view/create`. Why: ambiguous repo context (e.g. when checked into a sibling working tree) can silently target the wrong repo.

**No Keychain writes** — User control over macOS Keychain is sacred. Reading OK; never delete/add without explicit ask.

**Audit design memos post-session** — after multi-memo sessions: scan `docs/research/v*_design_*.md` and update **Status:** (DRAFT/LOCKED/REJECTED/SUPERSEDED) to match reality before closing.

**PR descriptions: surface known issues / limitations / deferrals** — noted limitations (silent-fail mode, edge cases, scope cuts, "worth fallback later" items) go in a dedicated `## Known issues` / `## Behaviour notes` PR-body section so future sessions can pick up follow-ups without re-discovering. Overrides global "keep PR bodies short" rule — known-issues section stays.

**One canonical HTTP client per external vendor** — every SEC EDGAR call goes through `alphalens_pipeline/data/alt_data/sec_edgar_client.py::SecEdgarClient`; every Alpha Vantage call through `alphavantage_client.py::AlphaVantageClient`; every Gemini call through `gemini_client.py::GeminiClient`; every Polygon call through `polygon_client.py::PolygonClient`. Why: shadow clients fragment the request stream and break quota tracking — and the quota scope differs per vendor:

| Vendor | Quota scope | Free-tier limit | Implication |
|---|---|---|---|
| SEC EDGAR | **per-IP** (User-Agent identifies but doesn't scope) | 10 req/s | VPS vs Mac get independent buckets; same machine gets one shared bucket regardless of how many keys. |
| Alpha Vantage | **per API key** | 25 req/day | Two keys = two pools; same key from many IPs still one pool. ToS discourages multi-account multiplication. |
| Polygon | **per API key** | 5 req/min on free, higher per plan | Same as AV; quota tied to the key, not the host. |
| Gemini (Google AI) | **per API key + per project** | Variable per model | Key-scoped + project-scoped quotas stack; rotating keys without rotating projects only multiplies the smaller bucket. |

Sites with an injected client (edgar_detector `SECEdgarSource`, `CIKLoader`; thematic mapper/extractor/generator; thematic press verification + news ingest; `PolygonShortInterestClient` domain wrapper) keep DI; module-level helpers call the respective `get_default_*_client()`. Env vars: `SEC_EDGAR_USER_AGENT`, `ALPHA_VANTAGE_API_KEY`, `GOOGLE_API_KEY`, `POLYGON_API_KEY`. Enforcement: `apps/alphalens-research/tests/test_no_raw_sec_http.py`, `apps/alphalens-research/tests/test_no_raw_av_http.py`, `apps/alphalens-research/tests/test_no_raw_gemini_sdk.py`, `apps/alphalens-research/tests/test_no_raw_polygon_http.py` — each with a positive-control case so the regex / URL-list cannot rot to empty silently. Design memos: `docs/research/{sec_edgar,alphavantage,gemini,polygon}_client_consolidation_2026_05_*.md`.

**Zen pre-MERGE codereview is mandatory** for any non-trivial PR (Python pipeline OR `web/` frontend). Workflow: push → open PR → `mcp__zen__codereview` with `gemini-3.1-pro-preview` + `thinking_mode="high"` → apply findings as additional commits on the open PR (preserve review trail) → wait CI green on latest commit → merge. Mixed-stack PRs need one combined zen pass, not two. Skippable only for doc-only / single-line typo / pure comment changes.

**Polish primary, English for tech terms** — Polish as primary in prose / conversations; English only for technical names without a Polish equivalent.

**`pnpm build` BEFORE `docker compose up` (local Docker stack only)** — applies to the LOCAL Docker stack at `deploy/docker/django-prod/docker-compose.yaml` (nginx bind-mounts `apps/web/build/` to `/usr/share/nginx/html`). On macOS Docker Desktop, if the source path is missing at container start, Docker creates an empty directory and the mount stays empty even after the build appears on host (no live re-bind). Symptom: nginx serves `403 Forbidden` on `/` plus `rewrite or internal redirection cycle while internally redirecting to "/index.html"` on every SPA route, while `/api/*` proxy works. Workflow: `pnpm --filter web build` first → then `just up` (or `docker compose -f deploy/docker/django-prod/docker-compose.yaml up -d`). If stack is already up with empty mount, fix with `docker compose -f deploy/docker/django-prod/docker-compose.yaml restart nginx` after building. Does NOT apply to production — SPA is hosted on Cloudflare Pages (per migration B); the Pages build pipeline runs `pnpm install --frozen-lockfile && pnpm build` on Cloudflare's side, the VPS does not serve the SPA.

**Pre-audit smoke before any audit > 1h compute** — `alphalens preaudit <strategy>` runs (1) per-DataDep coverage check against `~/.alphalens/` and (2) tiny end-to-end smoke subprocess (cap=300, 1-quarter window, ephemeral `--out`). Catches: missing data, coverage gap, hash drift, CLI passthrough breakage, end-to-end pipeline failure. Does NOT catch: OOM-at-scale, MooseFS I/O contention, time-varying signal corrosion. `apps/alphalens-research/scripts/launch_dual_audits.sh` prepends `alphalens preaudit <strategy> --skip-smoke` as fail-fast gate. New strategies require adding a `SmokeProfile` to `alphalens_research/preaudit/profiles.py::SMOKE_PROFILES` (enforced by `apps/alphalens-research/tests/test_preaudit_profiles.py`). Note: `_DEFAULT_SMOKE_TIMEOUT_S` is duplicated CLI-side at `apps/alphalens-pipeline/alphalens_cli/commands/preaudit.py` because typer.Option evaluates defaults at import time and the CLI lazy-imports research — parity pinned by `apps/alphalens-research/tests/test_preaudit_cli_default_in_sync.py`. Postmortem: `docs/research/insider_pc_compound_audit_launch_postmortem_2026_05_11.md`.

## Research methodology

**Adversarial review pre-compute** — before any run >1h compute: zen + Perplexity adversarial review of the locked design memo. Don't skip even on "obvious next" experiments — the pipeline has caught FATAL flaws in designs that looked sound.

**Layer 4 overlay design pre-screen (mandatory)** — before briefing reviewers on ANY Layer 4 overlay test (vol-target, drawdown-control, CPPI, time-stop, etc.):
1. **Pre-screen cyclicality EXCESS over benchmark baseline** — call `alphalens_research.attribution.signal_vol_regime.classify_cyclicality_excess(strategy_summary, benchmark_summary)` on base portfolio's daily returns vs IWM benchmark, both classified against the SAME IWM 60d realized vol regime. If verdict.proceed is False (excess R_mean ≤ -1.0), write REJECTED memo without registering. **Important:** raw `classify_cyclicality()` on R2000 long-only ALMOST ALWAYS returns EXTREME counter-cyclical because the IWM benchmark itself is EXTREME counter-cyclical (R≈-2.0). Use the excess-over-baseline variant to distinguish strategy-specific from universe-mechanical cyclicality. See [`feedback_universe_baseline_cyclicality_2026_05_10.md`](file://~/.claude/projects/-Users-jacoren-Developer-Personal-AlphaLens/memory/feedback_universe_baseline_cyclicality_2026_05_10.md).
2. **Cross-check factual base claims** — any MaxDD/Sharpe/return statistic in the brief MUST be verified against dumped artifacts (`~/.alphalens/audit/<strategy>/phase_*_returns.parquet`) via `alphalens_research.backtest.metrics.max_drawdown` + independent inline computation. Do NOT pass numbers from memory or postmortem prose — they may be hallucinations or stale.
3. **Quote excess-cyclicality verdict verbatim in memo §4 (Hypothesis)** — auditable artifact that the screen ran. Enforcement: `apps/alphalens-research/tests/test_overlay_design_compliance.py`.

**Burnt-holdout multiplicity compounds** — pure model-class swap on identical features+holdout+selection does NOT cleanse multiplicity. Use program-level Bonferroni count when data inputs unchanged. "Fresh class" Bonferroni counter intra-class only is statistical self-deception.

**Data-vendor PIT validation gate (mandatory for new sources)** — before using a new data provider (Alpha Vantage, SimFin, Polygon, iVolatility, etc.) in pre-registration: ≥5 sector-diverse anchor events × 2-source triangulation (Perplexity URL surfacing → Playwright/operator URL inspection) → ≤±2¢ OR ≤±1% delta vs source-quoted ground-truth. Result is a **HALT condition** — fail blocks audit launch, escalate to alternative vendor. Numeric extraction via Perplexity alone is insufficient — operator/Playwright must inspect ≥1 contemporaneous source URL per event.

**α2 sub-leveraged weighting + Little's-Law cost-model audit (event-driven strategies)** — before weighting decision for event-driven daily-rebalance: B0-style cost-model audit derives concurrent-position peak via Little's Law `L = λW`, sets `N_FIXED = peak_concurrent + 50% safety margin`. Weights = `1/N_FIXED` per active, gross ∈ [0,1], **no forced rebalancing** (peak overlap absorbed by pre-allocated capacity). Compares to legacy `gross=1 equal-weight` which forces deleveraging churn → ~16× cost amplification. Required artifact: `docs/research/<paradigm>_cost_model_audit_<DATE>.md`. Empirical p95 N_FIXED validation gate before final lock (see `paradigm14_pead_cost_model_audit_2026_05_14.md` §5.3).

**Literature ≠ oracle** — the project explores genuinely novel combinations (multi-source × PIT × interaction × live EDGAR @ retail scale); literature aggregate distributions are NOT informative priors. Methodology bundle (pre-reg + multi-phase + Bonferroni) = observation protocol, not gate.

**True PIT universe mandatory for paradigms >100 tickers** — every paradigm with universe >100 tickers MUST use true PIT panel from pre-reg day-one: intersected snapshot rosters from `apps/alphalens-pipeline/data/sp{500,400,600}_pit/` × delisted-ticker augmentation from `~/.alphalens/survivorship/{delisted_2007_2018,delisted_2021_2026}.parquet`. Implementation contract: `load_sp1500_pit_for_date_augmented(asof, include_delisted=True)` in `alphalens_pipeline/data/universes/sp1500_pit.py` (to implement alongside paradigm #16). Completed paradigms (1-15) are NOT re-run retrospectively — verdicts stand. Apply 20-40 bps/y snapshot-bias prior to literature numbers when universe is current-snapshot fallback (subtract ~0.3 t-stat from reported αt). Rationale: [`docs/research/plan_C_survivorship_retrospective_2026_05_14.md`](docs/research/plan_C_survivorship_retrospective_2026_05_14.md) rejection block.

**LLM training-cutoff blindness for numerical / real-time data** — never ask an LLM (Gemini Flash, Pro, or any) for a numerical or time-sensitive value (market cap, price, P/E, RSI, holdings %, news date, insider size, mcap bracket, volume threshold). The LLM filters via training-cutoff snapshot, not current state. Doctrine: **all numerical / quotable values come from authoritative sources** (yfinance / EDGAR / SEC / Polygon / Form-4 parquet) **and are pre-computed BEFORE the LLM call**. LLM does only reasoning + theme matching + text generation over injected facts. Also: **do not put bracket constraints (mcap range, P/E range, vol threshold) in LLM prompts** — filter post-hoc deterministically in Python. Enforcement: `apps/alphalens-research/tests/thematic/test_theme_mapping.py::TestGeminiMapperPromptBuilding::test_prompt_does_not_constrain_market_cap` pins that Pro prompt contains no `market cap`/`small-cap`/`mid-cap` tokens. Full empirical justification in [`feedback_llm_training_cutoff_numerical_data_2026_05_17.md`](file://~/.claude/projects/-Users-jacoren-Developer-Personal-AlphaLens/memory/feedback_llm_training_cutoff_numerical_data_2026_05_17.md).

## Project doctrine

**Keep searching screeners — never close the door** — discipline bounds the search, not closure. Don't write "no further prospecting" / "abandon factors". New hypotheses can operate on a new layer (ADR 0007); pre-reg ledger raises the Bonferroni bar for each new test.

**No passive pivot** — despite 14 paradigm failures, user has rejected the pivot to passive indexing. Active quant research continues.

## Where to find "why"

- **Architectural decisions:** `docs/adr/` (11 ADRs: pivot, queue contract, screener-agnostic backtest, ~~vendored upstream~~ *superseded*, ~~closed-layer policy~~ *superseded*, OSS extraction, layer architecture, sunset TradingAgents, Django replaces FastAPI, archive extracted and removed, split pipeline/research workspace)
- **Canonical closed-paradigms reference:** [`docs/research/paradigm_failures_postmortem.md`](docs/research/paradigm_failures_postmortem.md) — 14 failures + 2 inconclusive + 1 slippage-fail with αt values, dates, mechanisms, re-activation conditions
- **Per-layer kill reason:** `__closed_reason__` in each layer's `__init__.py`
- **Per-strategy design + audit docs:** `docs/research/`
- **Backtest reports archive:** `docs/backtest/`

## Known issues (LIVE)

- **Prescreener (Layer 2a) unvalidated** — 45% fundamentals weight requires PIT historicals that Polygon Starter ($29/mo) does not provide. Manual ad-hoc only, no performance guarantee.
- **GDELT theme YAML — multi-word quoted phrases only** — `alphalens_pipeline/thematic/config/gdelt_themes.yaml` queries must use multi-word phrases in quotes (e.g. `"CUDA toolkit"`, NOT `"CUDA"`). GDELT DOC API rejects single-word quoted tokens with `HTTP 200 + "The specified phrase is too short."` — `_http_get_json` raises this immediately as `GdeltQueryError` (no retry, no rate-limit burn). Static lint in `apps/alphalens-research/tests/thematic/test_gdelt.py::TestGdeltThemesYamlWellFormed` guards against regression. Live smoke per-bucket: `GDELT_LIVE_TEST=1 .venv/bin/python -m unittest tests.thematic.test_gdelt_live -v` (~90s wall, opt-in).

Issues regarding CLOSED layers (Lean Docker setup, Layer 2d backtest workflow, themed gate Phase 2) → see `docs/research/paradigm_failures_postmortem.md` and ADR 0010.

## VPS backfills (always-on, `jacoren@`)

Long-running data acquisition jobs that don't fit on the laptop run on the dedicated Linux VPS at `/home/jacoren/AlphaLens`. systemd-user units are versioned in `deploy/systemd/` and survive logout via `loginctl enable-linger jacoren`. Inspect via `journalctl --user -u <unit>` on the VPS.

| Unit | Pattern | Script | Output cache | Wall-time | Status |
|------|---------|--------|--------------|-----------|--------|
| `alphalens-form4-backfill.service` | long-running daemon (`Type=simple` + `Restart=on-failure`) | `apps/alphalens-research/scripts/run_form4_backfill.py` | `~/.alphalens/form4_parquet/` | ~5-10 days (SEC 10 req/s) | DONE 2026-05-08 (37MB final, 2.66M rows) |
| `alphalens-av-earnings-backfill.{service,timer}` | daily oneshot (`Type=oneshot` + `OnCalendar=*-*-* 00:05 UTC` + `Persistent=true`) | `apps/alphalens-research/scripts/av_earnings_daily_backfill.py` | `~/.alphalens/av_cache/earnings_<T>.json` | ~21 days (AV free-tier 25/day) | LIVE (paradigm-14 PEAD v2 backfill) |
| `alphalens-thematic-build.{service,timer}` | daily oneshot (`Type=oneshot` + `OnCalendar=*-*-* 06:30 UTC` + `Persistent=true`) wrapping `docker run --rm alphalens-pipeline` + `compose run --rm rebuild-cache` (Django stack) | `alphalens thematic {ingest,extract,map-themes,score,brief}` + `manage.py rebuild_briefs_cache` | `~/.alphalens/thematic_briefs/` + Postgres `briefs`/`days_meta` tables | ~5-15 min | LIVE — feeds CF Pages SPA via Django API (`apps/alphalens-django/`, image pulled from GHCR, see `## Production topology (migration B)`) |
| `alphalens-django` Docker stack | long-running (`docker compose up -d` per `deploy/docker/django-prod/`) | `ghcr.io/kamilpajak/alphalens-django:${ALPHALENS_DJANGO_TAG:-latest}` (pull-only on VPS); operator workflow: `docker compose pull && up -d` after CI publishes a new tag | Postgres `briefs`/`days_meta` (read by `rebuild-cache` from `~/.alphalens/thematic_briefs/`) | ~2-5 s downtime on `up -d` (Compose stops old container before starting new) | LIVE — origin behind cloudflared tunnel to `api.<domain>` |

**Why VPS, not Mac:**
- Mac sleeps / restarts → multi-day jobs lose state; VPS is always-on
- VPS is on residential ISP with different IP than Mac (SEC 10 req/s is per-IP)
- AV daily quota resets at 00:00 UTC; cron-trigger at 00:05 UTC catches the window cleanly

**Cache durability + sync:**
- All caches live under `~/.alphalens/<area>/` on VPS (general-purpose, not paradigm-specific)
- Nextcloud sync between VPS and Mac is opt-in per script (`--rclone-remote` arg). Currently OFF — VPS cache is source of truth for VPS-side consumers
- For Mac-side use: `rsync -av jacoren@vps:.alphalens/<area>/ ~/.alphalens/<area>/`

Operator recipes: `deploy/systemd/README.md` (systemd units), `deploy/docker/README.md` (Docker stack + Cloudflare wiring), `deploy/launchd/README.md` (macOS jobs), `deploy/runpod/README.md` (GPU/CPU pod bootstrap).

## Production topology (migration B)

- **SPA (`apps/web/`)** — built + served by **Cloudflare Pages** (GitHub integration, no CI workflow). Root `apps/web`, build cmd `corepack enable && pnpm install --frozen-lockfile && pnpm build`, output `build`. Production env var: `VITE_API_BASE=https://api.<domain>`. `static/_redirects` ships an SPA fallback (`/*  /index.html  200`) so client-side routes (`/brief/<date>`, `/experiments`) resolve after hard refresh.
- **API (`apps/alphalens-django/`)** — Docker image pushed to `ghcr.io/kamilpajak/alphalens-django` per CI workflow `.github/workflows/django-image.yml`. VPS pulls + runs the image via `deploy/docker/django-prod/docker-compose.yaml` (canonical, pull-only, no nginx, no SPA mount, `127.0.0.1:8000` for cloudflared); local dev adds `docker-compose.override.yaml` (auto-loaded by Compose when no `-f` is passed) which builds locally + brings up nginx with SPA bind-mount on `8080`. API reached cross-origin from CF Pages — `CORS_ALLOWED_ORIGINS` in Django prod env must list the Pages URL.
- **Origin** — Cloudflare Tunnel from VPS to `api.<domain>` mapped to `localhost:8000` (Django gunicorn behind Tunnel). `auth_cf` middleware validates `CF-Access-Jwt-Assertion` headers. **Auth path = same-domain cookies (Path A):** SPA at `app.<domain>`, API at `api.<domain>`, browser sends `CF_Authorization` cookie cross-origin. Required: `CORS_ALLOW_CREDENTIALS=True` + `CORS_ALLOWED_ORIGIN_REGEXES` for preview branches in Django prod env; CF Zero Trust → Access app for API origin must enable "Bypass Access for HTTP OPTIONS" (preflights don't carry the cookie). Service Tokens NOT used (Client Secret would be extractable from browser JS). See `apps/web/README.md` for the full path-A runbook.
- **Local Docker stack** — unchanged path for offline testing; nginx still bind-mounts `apps/web/build/` and serves both SPA + reverse-proxies `/api/*` to Django. Use `pnpm build` before `docker compose up` (see workflow conventions above).

## Environment

- API keys in `.env` (`GOOGLE_API_KEY`, `ALPHA_VANTAGE_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `POLYGON_API_KEY`, `PERPLEXITY_API_KEY`)
- Google API key also in macOS Keychain as `google-api-key`
- LLM config: Gemini 3.1 Pro (guru pilot, low thinking budget)
- Runtime data (outside repo, survives git ops):
  - `~/.alphalens/candidates.db` — Layer 1 candidate queue (historical log; no live drain)
  - `~/.alphalens/edgar-detect/` — portfolio.yaml, EDGAR dedup, digest, launchd logs
  - `~/.alphalens/form4_parquet/` — VPS Form-4 backfill output (hive-partitioned)
  - `~/.alphalens/av_cache/` — VPS AV EARNINGS daily backfill output (per-ticker JSON)
  - `~/.alphalens/thematic_briefs/` — daily thematic pipeline parquets (consumed by Django briefs ingest)
