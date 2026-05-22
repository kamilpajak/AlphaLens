# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repo.

## Project status (snapshot)

**Two parallel research tracks active:**

1. **Factor-paradigm-search** — paradigm #14 PEAD v2 audit in-flight (gated on VPS av_cache backfill, ~21d AV free-tier quota). 14 prior paradigm-class failures + 2 inconclusive retrospectives + 1 slippage-fail catalogued in [`docs/research/paradigm_failures_postmortem.md`](docs/research/paradigm_failures_postmortem.md). No standing PASS; capital deployment off-table per pre-reg `capital_deploy_clause`.

2. **Thematic event-driven research assistant** — MVP Phase A-E shipped 2026-05-17 (PRs #128-#134). Buy-side decision-support tool augmenting WhatsApp investing group workflow. Tool is **augmentation, NOT replacement** — user cherry-picks → group discusses → each member decides. Design: [`docs/research/thematic_event_tool_v1_design_2026_05_15.md`](docs/research/thematic_event_tool_v1_design_2026_05_15.md). Remaining: Telegram bot, Form-4 independent path, feedback ledger sqlite.

**Live production:**
- Layer 1 SEC EDGAR watchdog (launchd `detect`-only; `worker` archived per ADR 0008)
- Literature review weekly + monthly Perplexity scan
- VPS daily thematic pipeline + API rebuild + Cloudflare-fronted SvelteKit dashboard (see `## VPS backfills`)

**Everything else CLOSED / ARCHIVED / RESEARCH_ONLY** — code remains as reusable framework + anti-pattern catalog per [ADR 0005](docs/adr/0005-closed-layers-as-anti-pattern-catalog.md). Methodology bundle MIT-licensed as [`kamilpajak/phase-robust-backtesting`](https://github.com/kamilpajak/phase-robust-backtesting) per [ADR 0006](docs/adr/0006-phase-robust-backtesting-extraction.md). Search for better screeners stays open-ended — each new test raises the Bonferroni bar via ledger discipline, but "no further prospecting" is **not** a project position.

## Layer status

Lifecycle status of each layer lives in its `__init__.py` as the `__status__` constant (enforced by `tests/test_layer_status.py`). Layout is 11 top-level slots (Phase 1-6 reorg 2026-04-30, ADR 0007):

| Path | Status | Notes |
|------|--------|-------|
| `alphalens/core/` | ACTIVE (namespace) | plumbing — candidates, queue, registry |
| `alphalens/watchdog/` | ACTIVE | Layer 1 — `detect` live in launchd, `worker` archived per ADR 0008 |
| `alphalens/literature_review/` | ACTIVE | Monthly + weekly Perplexity scan, live in launchd |
| `alphalens/screeners/prescreener/` | RESEARCH_ONLY | Layer 2a — unvalidated, manual ad-hoc |
| `alphalens/screeners/momentum_lowvol/` | RESEARCH_ONLY | Layer 2 mom + low-vol adapter — strategy FAIL'd; scorer reused as BASE for Layer 4 vol-target overlay test |
| `alphalens/gates/` | RESEARCH_ONLY | Layer 2 selection-gates; single occupant `wrapper.py` |
| `alphalens/backtest/` | ACTIVE | Layer 3 engine — engine, multi_phase, multiple_testing, weighting, theme_analysis, llm_scorers, historical_validation, metrics |
| `alphalens/overlays/` | RESEARCH_ONLY | Layer 4 risk-overlays; single occupant `vol_target.py` |
| `alphalens/attribution/` | ACTIVE | Layer 5 — cost_model, factor_analysis, regime, decision_matrix, diagnostics, report, walk_forward |
| `alphalens/data/` | ACTIVE (namespace) | data infrastructure — `data/store/` PIT SoT, `data/{alt_data,fundamentals,macro}/` clients, `data/factors.py` |
| `alphalens/archive/` | namespace | ADR 0005 anti-pattern catalog |

**Methodology bundle** (preregistration ledger, multi_phase audit, multiple_testing thresholds, audit_multi_phase driver) consumed via external dep `phase-robust-backtesting>=0.2.0` — see [ADR 0006](docs/adr/0006-phase-robust-backtesting-extraction.md). `alphalens audit <strategy>` (`alphalens_cli/commands/audit.py`) resolves a short strategy name to a file path and delegates in-process to `phase_robust_backtesting.audit_multi_phase.run_audit`.

## Layer architecture (active alpha experimentation)

Five-layer separation per **[ADR 0007](docs/adr/0007-layer-architecture.md)**. Each layer has a single responsibility; failures attribute to one layer:

1. **Screener** (`alphalens/screeners/*`, archived ones in `alphalens/archive/`) — cross-sectional rank @ time t → top-N tickers
2. **Selection-gate** (`alphalens/gates/`) — binary/graded gate on the Scorer (modifies *which* tickers deploy)
3. **Backtest engine** (`alphalens/backtest/engine.py`) — runs scorer over strided rebalance calendar → `BacktestReport.portfolio_returns`
4. **Risk overlay** (`alphalens/overlays/`) — time-series sizing on portfolio realised vol (modifies *how much exposure*); first impl is vol-targeting per Moreira-Muir 2017
5. **Attribution** (`alphalens/attribution/{cost_model, factor_analysis, regime, ...}`) — cost-drag, Carhart-4F, Sharpe, Bonferroni → ledger verdict. Engine-side primitives (`rank_ic`, `turnover_pct`, `sharpe`) live in `alphalens/backtest/metrics.py`.

Compound hypotheses combine layers (e.g. mom+lowvol × VIX>20 gate × vol-target overlay), each combination paying its own Bonferroni cost. **Time-varying-beta hazard:** overlay-bearing strategies use Sharpe-improvement (not Carhart α t-stat) as primary success metric — see ADR 0007.

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
```

Closed paradigms used to ship CLI replay tooling; that surface was removed per [ADR 0010](docs/adr/0010-archive-extracted-and-removed.md). Failure rationale lives in `docs/research/paradigm_failures_postmortem.md`.

## Conventions

**Status markers** — each layer/screener `__init__.py` declares `__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"]` plus `__closed_date__`, `__closed_reason__`, and `__closed_evidence__: dict[str, str]` (mapping 7 gates → evidence path) if `__status__ ∈ {CLOSED, ARCHIVED}`. Schema: `docs/research/kill_verdict_checklist.md`. New layer requires updating `LAYERS_WITH_STATUS` in `tests/test_layer_status.py`.

**English-only in code** — comments, docstrings, identifiers in English. Math notation (α, ρ, ×, −) stays. Polish prose lives in CLAUDE.md, MEMORY, conversations, commit messages, postmortems. Enforcement: `tests/test_no_polish_chars.py`.

**Dependency direction** — two enforcement rules in `tests/test_module_dependencies.py`:
- `alphalens.backtest.*` does NOT import from `alphalens.screeners.*` (exemption: `historical_validation.py`)
- `alphalens.backtest.*` does NOT import from `alphalens.attribution.*` (Layer 3 → Layer 5 direction; engine produces `BacktestReport`, attribution consumes)

**Config parity** — `SCORER_CONFIG` in `lean_project/main.py` (Docker-inlined) must match `LEAN_DEFAULTS` on shared keys. Enforcement: `tests/test_lean_config_parity.py`.

**Lazy CLI imports** — `alphalens_cli/commands/research.py` intentionally does NOT promote cross-function duplicates to top-level. Measured +913ms regression in `alphalens` startup time per invoke (Layer 1 watchdog cron fires often).

**No backward compatibility** — solo project, zero external users. Rename, refactor, drop old behavior in one commit without aliases.

**New components** — always in `alphalens/<name>/` or `alphalens_cli/`, never top-level.

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

**One canonical HTTP client per external vendor** — every SEC EDGAR call goes through `alphalens/data/alt_data/sec_edgar_client.py::SecEdgarClient`; every Alpha Vantage call through `alphavantage_client.py::AlphaVantageClient`; every Gemini call through `gemini_client.py::GeminiClient`; every Polygon call through `polygon_client.py::PolygonClient`. Why: rate caps are per-IP / per-key — shadow clients fragment the request stream and break quota tracking. Sites with an injected client (watchdog `SECEdgarSource`, `CIKLoader`; thematic mapper/extractor/generator; thematic press verification + news ingest; `PolygonShortInterestClient` domain wrapper) keep DI; module-level helpers call the respective `get_default_*_client()`. Env vars: `SEC_EDGAR_USER_AGENT`, `ALPHA_VANTAGE_API_KEY`, `GOOGLE_API_KEY`, `POLYGON_API_KEY`. Enforcement: `tests/test_no_raw_sec_http.py`, `tests/test_no_raw_av_http.py`, `tests/test_no_raw_gemini_sdk.py`, `tests/test_no_raw_polygon_http.py` — each with a positive-control case so the regex / URL-list cannot rot to empty silently. `alphalens/archive/` is exempt from the Polygon + Gemini enforcement tests per ADR 0005 (closed-layer anti-pattern catalog). Design memos: `docs/research/{sec_edgar,alphavantage,gemini,polygon}_client_consolidation_2026_05_*.md`.

**Zen pre-MERGE codereview is mandatory** for any non-trivial PR (Python pipeline OR `web/` frontend). Workflow: push → open PR → `mcp__zen__codereview` with `gemini-3-pro-preview` + `thinking_mode="high"` → apply findings as additional commits on the open PR (preserve review trail) → wait CI green on latest commit → merge. Mixed-stack PRs need one combined zen pass, not two. Skippable only for doc-only / single-line typo / pure comment changes.

**Polish primary, English for tech terms** — Polish as primary in prose / conversations; English only for technical names without a Polish equivalent.

**Pre-audit smoke before any audit > 1h compute** — `alphalens preaudit <strategy>` runs (1) per-DataDep coverage check against `~/.alphalens/` and (2) tiny end-to-end smoke subprocess (cap=300, 1-quarter window, ephemeral `--out`). Catches: missing data, coverage gap, hash drift, CLI passthrough breakage, end-to-end pipeline failure. Does NOT catch: OOM-at-scale, MooseFS I/O contention, time-varying signal corrosion. `scripts/launch_dual_audits.sh` prepends `alphalens preaudit <strategy> --skip-smoke` as fail-fast gate. New strategies require adding a `SmokeProfile` to `alphalens/preaudit/profiles.py::SMOKE_PROFILES` (enforced by `tests/test_preaudit_profiles.py`). Postmortem: `docs/research/insider_pc_compound_audit_launch_postmortem_2026_05_11.md`.

## Research methodology

**Adversarial review pre-compute** — before any run >1h compute: zen + Perplexity adversarial review of the locked design memo. Don't skip even on "obvious next" experiments — the pipeline has caught FATAL flaws in designs that looked sound.

**Layer 4 overlay design pre-screen (mandatory)** — before briefing reviewers on ANY Layer 4 overlay test (vol-target, drawdown-control, CPPI, time-stop, etc.):
1. **Pre-screen cyclicality EXCESS over benchmark baseline** — call `alphalens.attribution.signal_vol_regime.classify_cyclicality_excess(strategy_summary, benchmark_summary)` on base portfolio's daily returns vs IWM benchmark, both classified against the SAME IWM 60d realized vol regime. If verdict.proceed is False (excess R_mean ≤ -1.0), write REJECTED memo without registering. **Important:** raw `classify_cyclicality()` on R2000 long-only ALMOST ALWAYS returns EXTREME counter-cyclical because the IWM benchmark itself is EXTREME counter-cyclical (R≈-2.0). Use the excess-over-baseline variant to distinguish strategy-specific from universe-mechanical cyclicality. See [`feedback_universe_baseline_cyclicality_2026_05_10.md`](file://~/.claude/projects/-Users-jacoren-Developer-Personal-AlphaLens/memory/feedback_universe_baseline_cyclicality_2026_05_10.md).
2. **Cross-check factual base claims** — any MaxDD/Sharpe/return statistic in the brief MUST be verified against dumped artifacts (`~/.alphalens/audit/<strategy>/phase_*_returns.parquet`) via `alphalens.backtest.metrics.max_drawdown` + independent inline computation. Do NOT pass numbers from memory or postmortem prose — they may be hallucinations or stale.
3. **Quote excess-cyclicality verdict verbatim in memo §4 (Hypothesis)** — auditable artifact that the screen ran. Enforcement: `tests/test_overlay_design_compliance.py`.

**Burnt-holdout multiplicity compounds** — pure model-class swap on identical features+holdout+selection does NOT cleanse multiplicity. Use program-level Bonferroni count when data inputs unchanged. "Fresh class" Bonferroni counter intra-class only is statistical self-deception.

**Data-vendor PIT validation gate (mandatory for new sources)** — before using a new data provider (Alpha Vantage, SimFin, Polygon, iVolatility, etc.) in pre-registration: ≥5 sector-diverse anchor events × 2-source triangulation (Perplexity URL surfacing → Playwright/operator URL inspection) → ≤±2¢ OR ≤±1% delta vs source-quoted ground-truth. Result is a **HALT condition** — fail blocks audit launch, escalate to alternative vendor. Numeric extraction via Perplexity alone is insufficient — operator/Playwright must inspect ≥1 contemporaneous source URL per event.

**α2 sub-leveraged weighting + Little's-Law cost-model audit (event-driven strategies)** — before weighting decision for event-driven daily-rebalance: B0-style cost-model audit derives concurrent-position peak via Little's Law `L = λW`, sets `N_FIXED = peak_concurrent + 50% safety margin`. Weights = `1/N_FIXED` per active, gross ∈ [0,1], **no forced rebalancing** (peak overlap absorbed by pre-allocated capacity). Compares to legacy `gross=1 equal-weight` which forces deleveraging churn → ~16× cost amplification. Required artifact: `docs/research/<paradigm>_cost_model_audit_<DATE>.md`. Empirical p95 N_FIXED validation gate before final lock (see `paradigm14_pead_cost_model_audit_2026_05_14.md` §5.3).

**Literature ≠ oracle** — the project explores genuinely novel combinations (multi-source × PIT × interaction × live EDGAR @ retail scale); literature aggregate distributions are NOT informative priors. Methodology bundle (pre-reg + multi-phase + Bonferroni) = observation protocol, not gate.

**True PIT universe mandatory for paradigms >100 tickers** — every paradigm with universe >100 tickers MUST use true PIT panel from pre-reg day-one: intersected snapshot rosters from `data/universes/sp{500,400,600}_pit/` × delisted-ticker augmentation from `~/.alphalens/survivorship/{delisted_2007_2018,delisted_2021_2026}.parquet`. Implementation contract: `load_sp1500_pit_for_date_augmented(asof, include_delisted=True)` in `alphalens/data/universes/sp1500_pit.py` (to implement alongside paradigm #16). Completed paradigms (1-15) are NOT re-run retrospectively — verdicts stand. Apply 20-40 bps/y snapshot-bias prior to literature numbers when universe is current-snapshot fallback (subtract ~0.3 t-stat from reported αt). Rationale: [`docs/research/plan_C_survivorship_retrospective_2026_05_14.md`](docs/research/plan_C_survivorship_retrospective_2026_05_14.md) rejection block.

**LLM training-cutoff blindness for numerical / real-time data** — never ask an LLM (Gemini Flash, Pro, or any) for a numerical or time-sensitive value (market cap, price, P/E, RSI, holdings %, news date, insider size, mcap bracket, volume threshold). The LLM filters via training-cutoff snapshot, not current state. Doctrine: **all numerical / quotable values come from authoritative sources** (yfinance / EDGAR / SEC / Polygon / Form-4 parquet) **and are pre-computed BEFORE the LLM call**. LLM does only reasoning + theme matching + text generation over injected facts. Also: **do not put bracket constraints (mcap range, P/E range, vol threshold) in LLM prompts** — filter post-hoc deterministically in Python. Enforcement: `tests/thematic/test_theme_mapping.py::TestGeminiMapperPromptBuilding::test_prompt_does_not_constrain_market_cap` pins that Pro prompt contains no `market cap`/`small-cap`/`mid-cap` tokens. Full empirical justification in [`feedback_llm_training_cutoff_numerical_data_2026_05_17.md`](file://~/.claude/projects/-Users-jacoren-Developer-Personal-AlphaLens/memory/feedback_llm_training_cutoff_numerical_data_2026_05_17.md).

## Project doctrine

**Keep searching screeners — never close the door** — discipline bounds the search, not closure. Don't write "no further prospecting" / "abandon factors". New hypotheses can operate on a new layer (ADR 0007); pre-reg ledger raises the Bonferroni bar for each new test.

**No passive pivot** — despite 14 paradigm failures, user has rejected the pivot to passive indexing. Active quant research continues.

## Where to find "why"

- **Architectural decisions:** `docs/adr/` (8 ADRs: pivot, queue contract, screener-agnostic backtest, ~~vendored upstream~~ *superseded*, closed-layer policy, OSS extraction, layer architecture, sunset TradingAgents)
- **Canonical closed-paradigms reference:** [`docs/research/paradigm_failures_postmortem.md`](docs/research/paradigm_failures_postmortem.md) — 14 failures + 2 inconclusive + 1 slippage-fail with αt values, dates, mechanisms, re-activation conditions
- **Per-layer kill reason:** `__closed_reason__` in each layer's `__init__.py`
- **Per-strategy design + audit docs:** `docs/research/`
- **Backtest reports archive:** `docs/backtest/`

## Known issues (LIVE)

- **Prescreener (Layer 2a) unvalidated** — 45% fundamentals weight requires PIT historicals that Polygon Starter ($29/mo) does not provide. Manual ad-hoc only, no performance guarantee.
- **GDELT theme YAML — multi-word quoted phrases only** — `alphalens/thematic/config/gdelt_themes.yaml` queries must use multi-word phrases in quotes (e.g. `"CUDA toolkit"`, NOT `"CUDA"`). GDELT DOC API rejects single-word quoted tokens with `HTTP 200 + "The specified phrase is too short."` — `_http_get_json` raises this immediately as `GdeltQueryError` (no retry, no rate-limit burn). Static lint in `tests/thematic/test_gdelt.py::TestGdeltThemesYamlWellFormed` guards against regression. Live smoke per-bucket: `GDELT_LIVE_TEST=1 .venv/bin/python -m unittest tests.thematic.test_gdelt_live -v` (~90s wall, opt-in).

Issues regarding CLOSED layers (Lean Docker setup, Layer 2d backtest workflow, themed gate Phase 2) → see `docs/research/paradigm_failures_postmortem.md` and ADR 0010.

## VPS backfills (always-on, `jacoren@`)

Long-running data acquisition jobs that don't fit on the laptop run on the dedicated Linux VPS at `/home/jacoren/AlphaLens`. systemd-user units are versioned in `deploy/systemd/` and survive logout via `loginctl enable-linger jacoren`. Inspect via `journalctl --user -u <unit>` on the VPS.

| Unit | Pattern | Script | Output cache | Wall-time | Status |
|------|---------|--------|--------------|-----------|--------|
| `form4-backfill.service` | long-running daemon (`Type=simple` + `Restart=on-failure`) | `scripts/run_form4_backfill.py` | `~/.alphalens/form4_parquet/` | ~5-10 days (SEC 10 req/s) | DONE 2026-05-08 (37MB final, 2.66M rows) |
| `av-earnings-backfill.{service,timer}` | daily oneshot (`Type=oneshot` + `OnCalendar=*-*-* 00:05 UTC` + `Persistent=true`) | `scripts/av_earnings_daily_backfill.py` | `~/.alphalens/av_cache/earnings_<T>.json` | ~21 days (AV free-tier 25/day) | LIVE (paradigm-14 PEAD v2 backfill) |
| `alphalens-thematic-daily.{service,timer}` | daily oneshot (`Type=oneshot` + `OnCalendar=*-*-* 06:30 UTC` + `Persistent=true`) wrapping `docker run --rm alphalens-pipeline` + `compose run --rm rebuild-cache` (Django stack) | `alphalens thematic {ingest,extract,map-themes,score,brief}` + `manage.py rebuild_briefs_cache` | `~/.alphalens/thematic_briefs/` + Postgres `briefs`/`days_meta` tables | ~5-15 min | LIVE — feeds Cloudflare-fronted SvelteKit dashboard via Django stack (`apps/alphalens-django/`, see `docs/django-migration` ADR) |

**Why VPS, not Mac:**
- Mac sleeps / restarts → multi-day jobs lose state; VPS is always-on
- VPS is on residential ISP with different IP than Mac (SEC 10 req/s is per-IP)
- AV daily quota resets at 00:00 UTC; cron-trigger at 00:05 UTC catches the window cleanly

**Cache durability + sync:**
- All caches live under `~/.alphalens/<area>/` on VPS (general-purpose, not paradigm-specific)
- Nextcloud sync between VPS and Mac is opt-in per script (`--rclone-remote` arg). Currently OFF — VPS cache is source of truth for VPS-side consumers
- For Mac-side use: `rsync -av jacoren@vps:.alphalens/<area>/ ~/.alphalens/<area>/`

Operator recipes: `deploy/systemd/README.md` (systemd units), `deploy/docker/README.md` (Docker stack + Cloudflare wiring).

## Environment

- API keys in `.env` (`GOOGLE_API_KEY`, `ALPHA_VANTAGE_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `POLYGON_API_KEY`, `PERPLEXITY_API_KEY`)
- Google API key also in macOS Keychain as `google-api-key`
- LLM config: Gemini 3 Pro (guru pilot, low thinking budget)
- Runtime data (outside repo, survives git ops):
  - `~/.alphalens/candidates.db` — Layer 1 candidate queue (historical log; no live drain)
  - `~/.alphalens/watchdog/` — portfolio.yaml, EDGAR dedup, digest, launchd logs
  - `~/.alphalens/lean/{data,results,logs}/` — Lean OHLCV cache (also used by backtest replay)
  - `~/.alphalens/guru_cache/` — guru pilot LLM response cache
  - `~/.alphalens/form4_parquet/` — VPS Form-4 backfill output (hive-partitioned)
  - `~/.alphalens/av_cache/` — VPS AV EARNINGS daily backfill output (per-ticker JSON)
