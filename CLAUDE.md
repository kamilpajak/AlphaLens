# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repo.

## Project status (2026-04-25 → 2026-05-16)

**PARALLEL TRACK ADDED 2026-05-16:** Obok kontynuowanej factor-paradigm-search dochodzi DRUGA równoległa ścieżka — budowa **thematic event-driven research assistant** augmentującego user's istniejący workflow w WhatsApp investing group. Factor-paradigm research NIE jest zamknięty: paradigm #14 PEAD v2 audyt nadal in-flight (~3 weeks AV cache backfill), paper-trade observations (v9D αt 2.45, pc_abnormal αt 2.65) continue, Layer 1 EDGAR watchdog live, literature review autonomous, methodology bundle `phase-robust-backtesting` MIT-licensed durable artifact. Nowa parallel track to **buy-side decision-support tool** — Mega-cap news (S&P 100 + sector leaders) → LLM theme extraction (Gemini 2.5 Flash) → second-order beneficiary mapping (Gemini 3 Pro) + verification gates (ETF holdings + 10-K via companyfacts_parquet + recent press + Cohen-Malloy insider activity) → small-cap quantitative screen REUSING validated paradigm scorers (paradigm #11 Cohen-Malloy form-4 αt 2.71, paradigm #13 FCFF yield αt 1.18 every-phase positive — jako MULTI-SIGNAL CORROBORATION hooks, NIE standalone strategies) → short WhatsApp-format brief → user cherry-picks → group discusses → each member decides. Tool to augmentation NIE replacement human workflow. Friend's verified multi-year track record + group decision-making = validation layer (NIE mandatory 6-month solo paper-trade gate). MVP scope ~1.5-2 tygodnie engineering + lightweight Claude-maintained feedback ledger. Paradigm #15 idiosyncratic_momentum NIE reused w MVP (IS αt 0.02 = noise; whole price_factor_search class dead). Design memo `docs/research/thematic_event_tool_v1_design_2026_05_15.md`. Project memory `project_thematic_tool_pivot_2026_05_16.md`. **Three same-day adversarial-rejected designs** (HXZ profitability, insider_form4_quality_gated, autonomous thematic tool v1) wykryły systematic kill-switch bias w zen+Perplexity "be brutal" prompting — dokumentowane w `feedback_adversarial_reviewer_bias_2026_05_16.md`. Reviewer treated jako technical critique input, NIE jako go/no-go authority.

**Continuing paradigm-search track:**

**AlphaLens** = research/learning infrastructure dla retail quant active alpha experimentation. Po **14 paradigm failures phase-robust** (Layer 2b/2c/2d/2e/2f/2g + tri-factor + mom+lowvol_combo + regime-gate rescue + quality+momentum + vol-target overlay + insider_pc_compound 2026-05-11 + ev_fcff_yield 2026-05-13 + idiosyncratic_momentum 2026-05-14), **2 INCONCLUSIVE retrospectives** (v9D, pc_abnormal_volume), oraz **1 SLIPPAGE-FAIL 2026-05-12** (`insider_form4_opportunistic_2026_05_08_v2` — gross αt=+2.71 PASS_MARGINAL 2026-05-09 ale slippage diagnostic FAIL: net αt @ H=50bps half-spread = +1.27 OOS / +1.95 FL → G1 knockout violated both windows; paper-trade SUSPENDED, Layer 4 stays rejected). **Paradigm #15 idiosyncratic_momentum** JOINT FAIL 2026-05-14 (`idiosyncratic_momentum_2026_05_14_v1`, class `price_factor_search_2026_04_29` n=5; αt mean 0.02/0.71/1.58 across IS/OOS/FL — mechanism monotonically strengthening but below doctrine 3.5; β_market 0.97-1.17 (no BAB low-vol confound), turnover 19-36%/mo (far below feared 60-80%); 30.4min audit, $0.04 spend; pattern matches paradigm #13 ev_fcff_yield). **Paradigm #14 PEAD v2** pre-registered 2026-05-13 (`pead_v5_pss_2026_05_13`, class `event_drift_search_2026_05_03`, strict n=3, critical |t|=2.39; doctrine 3.5 binds); **Phase A/B/C/D infrastructure complete 2026-05-14** across 7 PRs (#114-#120): AV EARNINGS client + cache, A3 PIT validation 5/5 PASS, B0 cost-model audit locking α2 sub-leveraged N_FIXED=150 per Little's-Law, VPS systemd backfill, B1 PSS rank scorer, B2 daily-rebalance adapter, **C invested-days-only Carhart-4F with NW HAC maxlags=20** (first in project: `fit_carhart_4f_invested_only` w `attribution/factor_analysis.py` z `invested_mask` safe contract), D experiment scaffold (`scripts/experiment_pead_pss_v2.py`) + SmokeProfile registered. Phase E (runpod audit launch) gated on VPS AV cache backfill ~21 dni @ AV free-tier 25/day quota. Methodology bundle (pre-reg + multi-phase + Bonferroni) jest durable artifact, Layer 1 watchdog + literature review zostają live. **Search dla coraz lepszych screenerów pozostaje open-ended** — każdy nowy test podnosi Bonferroni bar dla następnego (ledger discipline), ale "no further prospecting" NIE jest pozycją projektu. Layer architecture w ADR 0007 (5 warstw: screener → selection-gate → engine → risk-overlay → attribution) — kolejne hipotezy mogą operować na nowej warstwie. **Capital deployment off-table:** żaden paradygmat nie ma standing PASS po slippage-fail insider_form4; tylko full PASS by unlockował deployment per pre-reg `capital_deploy_clause`.

**Live production:** Layer 1 SEC EDGAR watchdog (launchd `detect` only — `worker` archived per ADR 0008) + literature_review weekly+monthly Perplexity scan.
**Wszystko inne:** CLOSED, ARCHIVED, RESEARCH_ONLY lub IN-FLIGHT (paradigm #14) — kod zostaje jako reusable framework + anti-pattern catalog. Methodology bundle (preregistration ledger + multi_phase + audit driver) extracted do `kamilpajak/phase-robust-backtesting` (MIT). TradingAgents subtree usunięty 2026-04-30 (ADR 0008).

Pełny rozliczenie: `docs/research/paradigm_failures_postmortem.md` (paradigm failures across 3 architectural layers) + `docs/research/insider_form4_opportunistic_phase_b_postmortem_2026_05_09.md` (PASS_MARGINAL → SLIPPAGE-FAIL) + `docs/research/paradigm14_pead_v2_design_2026_05_13.md` (LOCKED, in-flight Phase A-D done) + `docs/research/paradigm14_pead_cost_model_audit_2026_05_14.md` (B0 audit). Decyzje architektoniczne: `docs/adr/` (8 ADRs).

## Layer status

Lifecycle status każdej warstwy żyje w jej `__init__.py` jako `__status__` constant (enforced przez `tests/test_layer_status.py`):

Layout zorganizowany jako 11 top-level slotów (Phase 1-6 reorg 2026-04-30, ADR 0007):

| Path | Status | Notatka |
|------|--------|---------|
| `alphalens/core/` | ACTIVE (namespace) | plumbing — candidates, queue, registry (Layer 3 runner/worker removed per ADR 0008) |
| `alphalens/watchdog/` | ACTIVE | Layer 1 — `detect` live w launchd, `worker` archived per ADR 0008 |
| `alphalens/literature_review/` | ACTIVE | Monthly + weekly Perplexity scan, live w launchd |
| `alphalens/screeners/prescreener/` | RESEARCH_ONLY | Layer 2a — unvalidated, manual ad-hoc |
| `alphalens/screeners/momentum_lowvol/` | RESEARCH_ONLY | Layer 2 mom + low-vol adapter — strategy FAIL'd as failure 7 but scorer reused as BASE for Layer 4 vol-target overlay test |
| `alphalens/gates/` | RESEARCH_ONLY | Layer 2 selection-gates (was `regime_gate/`); single occupant `wrapper.py` until concrete classifier added |
| `alphalens/backtest/` | ACTIVE | Layer 3 engine — engine, walk-forward removed (moved to attribution), multi_phase, multiple_testing, weighting, theme_analysis, llm_scorers, historical_validation, metrics (engine-side primitives + Sharpe consumed downstream) |
| `alphalens/overlays/` | RESEARCH_ONLY | Layer 4 risk-overlays (was `risk_overlay/`); single occupant `vol_target.py` |
| `alphalens/attribution/` | ACTIVE | Layer 5 — cost_model, factor_analysis, regime, decision_matrix, diagnostics, report, walk_forward |
| `alphalens/data/` | ACTIVE (namespace) | data infrastructure — `data/store/` (PIT SoT for as-of-t reads), `data/{alt_data,fundamentals,macro}/` (RESEARCH_ONLY clients), `data/factors.py` (Fama-French CSV loader) |

**Methodology bundle** (preregistration ledger, multi_phase audit, multiple_testing thresholds, audit_multi_phase driver) is consumed via the external dep `phase-robust-backtesting>=0.2.0` — see [ADR 0006](docs/adr/0006-phase-robust-backtesting-extraction.md). Local copies were deleted on 2026-05-06; AlphaLens has no in-repo source for these. `alphalens audit <strategy>` (CLI command at `alphalens_cli/commands/audit.py`) resolves a short strategy name to a file path and delegates in-process to `phase_robust_backtesting.audit_multi_phase.run_audit`.
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
.venv/bin/alphalens archive themed status --days 90      # historical themed monitoring
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

## Workflow conventions

**TDD always** — production code zawsze red→green→refactor, nawet 2-liniowe fixy (MultiIndex flatten, off-by-one). Nie ma "just one quick fix" — write test first.

**Quality over speed** — nie downgrade'uj modeli/data sources żeby uniknąć rate limits. Czekać/cachować/throttlować, nie obniżać precyzji.

**runpod = primary compute** — eksperymenty (audity, holdouts, smoke N>50) idą na runpod.io CPU pody. Local Mac zostaje na code editing + tiny sanity checks. OOM-class issues defaultowo "ship to runpod" zamiast laptop-fit refactor.

**Proceed continuously between phases** — approved plan = green light dla wszystkich faz; chain N→N+1 bez per-phase confirmation. Stop tylko na blocker albo destructive action.

**Cache iVolatility downloads** — persist raw API responses do `~/.alphalens/ivolatility_cache/` PRZED processingiem; nigdy re-fetch on retry/iteration ($399/mo metered).

**gh CLI repo scope** — zawsze `--repo kamilpajak/AlphaLens` przy `gh pr comment/view/create`. Incident 2026-04-24: comment trafił na TauricResearch/TradingAgents#19 zamiast kamilpajak/AlphaLens#19.

**No Keychain writes** — User control nad macOS Keychain jest sacred. Czytanie OK; nigdy delete/add bez explicit ask.

**Audit design memos post-session** — po sesjach z multi-memo: scan `docs/research/v*_design_*.md` i update **Status:** żeby pasował do reality (DRAFT/LOCKED/REJECTED/SUPERSEDED) przed closing.

**PR descriptions: surface known issues / limitations / deferrals** — zauważone limitations (silent-fail mode, edge case, scope cut, "worth fallback later" punkty) idą do osobnej sekcji `## Known issues` / `## Behaviour notes` w PR body. Precedent: Phase A #128 GDELT IP-throttling + RSS dominance, Phase B #129 novelty cold-start, Phase C #130 NPORT-P series-name + CIK resolution + Form-4 pre-2010 thin coverage. To pozwala kolejnym sesjom pickup'ować follow-up bez ponownego odkrywania wszystkiego (potwierdzone empirycznie 2026-05-17 review Phase A/B/C dla planu napraw — udokumentowane limitations zaprowadziły do clean fix planu). Override globalnej zasady "keep PR bodies short" — known-issues sekcja zostaje.

**One canonical HTTP client per external vendor (SEC EDGAR pinned 2026-05-19)** — every SEC EDGAR HTTP call in the repo MUST go through `alphalens/data/alt_data/sec_edgar_client.py::SecEdgarClient`. Sites that already have an injected client (watchdog `SECEdgarSource`, `CIKLoader`) keep DI; module-level functions (thematic `tenk_grep`, `etf_holdings`) call `get_default_sec_client()`. UA env var: `SEC_EDGAR_USER_AGENT` (one var, fallback `ALPHALENS_DEFAULT_USER_AGENT`). Enforcement: `tests/test_no_raw_sec_http.py` fails red on `urllib.request.urlopen` / `requests.get(` in any file that also mentions a SEC URL fragment. Rationale: SEC's 10 req/s fair-access cap is per-IP, so any uncoordinated shadow client risks a 403 that takes down EVERY SEC consumer at once (paradigm-14 PEAD backfill, Layer 1 watchdog, thematic verification, EDGAR fundamentals). Same policy will extend to Alpha Vantage + Gemini in follow-up PRs per the 2026-05-19 vendor audit. Design memo: `docs/research/sec_edgar_client_consolidation_2026_05_19.md`.

**Zen pre-MERGE codereview is mandatory for both Python pipeline AND `web/` frontend** — global rule z `~/.claude/CLAUDE.md` ("Pre-merge Code Review (zen)") aplikuje się do KAŻDEGO non-trivial PR w tym repo, niezależnie od stacka. Workflow: push → open PR → `mcp__zen__codereview` z `gemini-3-pro-preview` + `thinking_mode="high"` → apply findings jako additional commits na otwartym PR (preserve review trail) → wait CI green na latest commit → merge. Mixed-stack PR (web/ + Python) → jeden combined zen pass wystarczy, nie dwa. Skippable tylko dla doc-only / single-line typo / pure comment changes. Doctrine clarified 2026-05-18 po PR #153 web/ scaffold (memory: `feedback_zen_pre_merge_applies_to_web_2026_05_18.md`).

**Polish primary, English for tech terms** — w prozie/rozmowach polski jako primary; angielski tylko dla nazw technicznych bez polskiego odpowiednika.

**Pre-audit smoke before any audit > 1h compute** — `alphalens preaudit <strategy>` runs (1) per-DataDep coverage check against `~/.alphalens/` and (2) tiny end-to-end smoke subprocess (cap=300, 1-quarter window, ephemeral `--out`). Catches: missing data, coverage gap, hash drift, CLI passthrough breakage, end-to-end pipeline failure. Does NOT catch: OOM-at-scale, MooseFS I/O contention under N workers, time-varying signal corrosion. Driven by 2026-05-11 incident — full audit FAILED at phase 0 because pod `/workspace` had post-2018-only iVol SMD coverage; `_run_precheck` correctly classified as pre-screen FAIL but burned ~27 min before the operator noticed. `scripts/launch_dual_audits.sh` now prepends `alphalens preaudit insider_pc_compound --skip-smoke` as a fail-fast gate. New strategy onboarding requires adding a `SmokeProfile` to `alphalens/preaudit/profiles.py::SMOKE_PROFILES` (enforced by `tests/test_preaudit_profiles.py`). Full postmortem: `docs/research/insider_pc_compound_audit_launch_postmortem_2026_05_11.md`.

## Research methodology

**Adversarial review pre-compute** — przed jakimkolwiek runem >1h compute: zen + perplexity adversarial review zlocked design memo. Pipeline złapał FATAL flaws na 2 designach jednej sesji (v5 quantile-LP, v8 LGBM-quantile, v0 Cohen-Malloy 5y misread). Don't skip nawet na "obvious next" experiments.

**Layer 4 overlay design pre-screen (mandatory)** — przed briefing reviewers na ANY Layer 4 overlay test (vol-target, drawdown-control, CPPI, time-stop, etc.):
1. **Pre-screen cyclicality EXCESS over benchmark baseline** — call `alphalens.attribution.signal_vol_regime.classify_cyclicality_excess(strategy_summary, benchmark_summary)` on the base portfolio's daily returns vs IWM benchmark, both classified against the SAME IWM 60d realized vol regime. If verdict.proceed is False (strategy-specific counter-cyclical, excess R_mean ≤ -1.0), write REJECTED memo without registering. **Important:** raw `classify_cyclicality()` on R2000 long-only strategies will ALMOST ALWAYS return EXTREME counter-cyclical because the IWM benchmark itself is EXTREME counter-cyclical (R≈-2.0 measured 2018-2023) — vol-clustering decay puts post-stress recovery returns in Q4-Q5 of trailing-vol regimes. Use the excess-over-baseline variant to distinguish strategy-specific from universe-mechanical cyclicality.
2. **Cross-check factual base claims** — any MaxDD/Sharpe/return statistic in the brief MUST be verified against dumped artifacts (`~/.alphalens/audit/<strategy>/phase_*_returns.parquet`) via `alphalens.backtest.metrics.max_drawdown` + independent inline computation. Do NOT pass numbers from memory or postmortem prose — they may be hallucinations or stale.
3. **Quote excess-cyclicality verdict verbatim w memo §4 (Hypothesis section)** — auditable artifact that the screen ran. Test enforcement: `tests/test_overlay_design_compliance.py`.

Empirical justification: PR #88 (2026-05-10) caught false MaxDD prior + strong (excess) counter-cyclical mechanism. Refined 2026-05-10 (this same session) after 5-strategy verification + IWM baseline control: 4 of 5 tested strategies showed counter-cyclical pattern, AND IWM baseline itself is EXTREME counter-cyclical (R=-2.01) — universe-mechanical artifact, not strategy-specific. Only insider_form4 (excess R=-2.67) and pc_abnormal (excess R=-2.48) are GENUINELY strategy-specific counter-cyclical (warrant overlay rejection); v9D (excess R=-0.27) ≈ baseline; mom+lowvol (excess R=+4.71) is LESS counter-cyclical than baseline. Use `feedback_universe_baseline_cyclicality_2026_05_10.md` for full data.

**Burnt-holdout multiplicity compounds** — pure model-class swap na identycznych features+holdout+selection NIE cleansuje multiplicity. Use program-level Bonferroni count gdy data inputs unchanged. "Fresh class" Bonferroni licznik tylko-intra-class jest statistical self-deception.

**Data-vendor PIT validation gate (mandatory dla nowych źródeł)** — przed użyciem nowego data providera (Alpha Vantage, SimFin, Polygon, iVolatility itp.) w pre-rejestracji: ≥5 sector-diverse anchor events × 2-source triangulation (Perplexity URL surfacing → Playwright/operator URL inspection) → ≤±2¢ OR ≤±1% delta vs source-quoted ground-truth. Wynik **HALT condition** — fail blokuje audit launch, escalate do alternative vendor. Pierwszy raz wdrożone w paradigm-14 PEAD v2 §3.1 jako gate (5) PASS criteria; precedent po insider_form4 SLIPPAGE-FAIL i v1 PEAD adversarial review za hallucination-prone consensus estimates. Numeric extraction via Perplexity alone NIE wystarcza — operator/Playwright must inspect minimum 1 contemporaneous source URL per event.

**α2 sub-leveraged weighting + Little's-Law cost-model audit (event-driven strategies)** — przed weighting decision dla event-driven daily-rebalance: B0-style cost-model audit derives concurrent-position peak via Little's Law `L = λW`, sets `N_FIXED = peak_concurrent + 50% safety margin`. Weights = `1/N_FIXED` per active, gross ∈ [0,1], **no forced rebalancing** (peak overlap absorbed by pre-allocated capacity). Compares to legacy `gross=1 equal-weight` which forces deleveraging churn during peak season → ~16× cost amplification. Required artifact: `docs/research/<paradigm>_cost_model_audit_<DATE>.md`. Empirical p95 N_FIXED validation gate before final lock (per `paradigm14_pead_cost_model_audit_2026_05_14.md` §5.3).

**Literature ≠ oracle** — projekt eksploruje genuinely novel combinations (multi-source × PIT × interaction × live EDGAR @ retail scale); literature aggregate distributions to NIE są informative priors. Methodology bundle (pre-reg + multi-phase + Bonferroni) = observation protocol, nie gate.

**True PIT universe mandatory dla paradigmów >100 tickers (2026-05-14)** — Plan C survivorship retrospective REJECTED post zen adversarial review jako procrastination-disguised-as-rigor (continuation `7e79f785`); zamiast tego adoptujemy Perplexity-cited 20-40 bps/y snapshot-bias prior jako default. Każdy paradigm z universe > 100 tickers MUSI używać true PIT panel od pre-reg day-one: intersected snapshot rosters z `data/universes/sp{500,400,600}_pit/` × delisted-ticker augmentation z `~/.alphalens/survivorship/{delisted_2007_2018,delisted_2021_2026}.parquet`. Implementation contract: nowa funkcja `load_sp1500_pit_for_date_augmented(asof, include_delisted=True)` w `alphalens/data/universes/sp1500_pit.py` (do zaimplementowania razem z paradigm #16). NIE rerun'ujemy completed paradigmów (1-15) retrospectively — verdicts stand. Survivorship-snapshot prior stosujemy do priorów PRZED nowymi audytami: typowy retail long-only αt subtract ~0.3 t-stat z reported, gdy universe to current-snapshot fallback. Decyzja: `docs/research/plan_C_survivorship_retrospective_2026_05_14.md` rejection block.

**LLM training-cutoff blindness for numerical/real-time data (2026-05-17)** — żadnej numerical lub time-sensitive wartości (market cap, price, P/E, RSI, holdings %, recent news date, insider buy size, mcap brackets, volume thresholds) NIE prosimy od LLM (Gemini Flash, Pro, jakikolwiek). LLM filtruje przez training-cutoff snapshot, nie current state. Doktryna: **wszystkie numerical/quotable wartości pochodzą z authoritative source** (yfinance/SimFin/SEC/Polygon/Form-4 parquet) **i są pre-computed PRZED LLM call**. LLM tylko reasoning + theme matching + text generation z wstrzykniętymi faktami. Również: NIE umieszczamy bracket constraints (mcap range, P/E range, vol threshold) w LLM promptach — filter post-hoc deterministycznie w Pythonie. Empiryczny dowód 2026-05-17: probe Gemini 3 Pro `gemini-3-pro-preview` zwrócił QUBT mcap $0.05B (snapshot 2024-05-20, conf 0.85) vs real $1.78B na 2026-04-14 — 35× off. Default mcap bracket $500M-$10B w prompcie systematycznie wykluczał QUBT/RGTI/QBTS (post-2024 quantum rally), choć wszystkie były w bracketcie. Fix: `alphalens/thematic/verification/mcap_filter.py` (yfinance attribute access `fast_info.market_cap`, NIE `.get('market_cap')` które zwraca None) + orchestrator `map_themes(market_cap_range=...)` post-filtruje przed gates. Test enforcement: `tests/thematic/test_theme_mapping.py::TestGeminiMapperPromptBuilding::test_prompt_does_not_constrain_market_cap` pinuje że prompt Pro NIE zawiera tokenów `market cap`/`small-cap`/`mid-cap`.

## Project doctrine

**Keep searching screeners — never close the door** — discipline bounds the search, nie closure. Nie pisać "no further prospecting" / "abandon factors". Kolejne hipotezy mogą operować na nowej warstwie (ADR 0007), pre-reg ledger podnosi Bonferroni bar dla każdego nowego testu.

**No passive pivot** — mimo 14 paradigm failures (Layer 2b/2c/2d/2e/2f/2g + tri-factor + mom+lowvol_combo + regime-gate rescue + quality+momentum + vol-target overlay + insider_pc_compound + ev_fcff_yield + idiosyncratic_momentum), user odrzucił pivot do passive indexing. Active quant research trwa.

## Where to find "why"

- **Architectural decisions:** `docs/adr/` (8 ADRs: pivot, queue contract, screener-agnostic backtest, ~~vendored upstream~~ *superseded*, closed-layer policy, OSS extraction, layer architecture, sunset TradingAgents)
- **Why each layer was closed:** `docs/research/paradigm_failures_postmortem.md` + per-layer `__closed_reason__` w `__init__.py`
- **Backtest reports archive:** `docs/backtest/`
- **Per-strategy design + audit docs:** `docs/research/`

## TradingAgents removal (2026-04-30)

Vendored TradingAgents subtree + Layer 3 LLM runner removed per [ADR 0008](docs/adr/0008-sunset-tradingagents-integration.md). Worker (`com.alphalens.watchdog.worker.plist`) archived. Layer 1 watchdog still detects EDGAR events and writes to `~/.alphalens/candidates.db`, but no consumer drains the queue today; ad-hoc inspection is via direct SQL against the sqlite file. If TA is needed in the future, clone it into a separate working directory and run it manually.

## Known issues (LIVE)

- **Prescreener (Layer 2a) unvalidated**: 45% fundamentals weight wymaga PIT historicals których Polygon Starter ($29/mo) nie dostarcza. Manual ad-hoc tylko, no performance guarantee.
- **GDELT theme YAML — multi-word quoted phrases only**: `alphalens/thematic/config/gdelt_themes.yaml` queries muszą używać multi-word phrase w cudzysłowach (np. `"CUDA toolkit"`, NIE `"CUDA"`). GDELT DOC API odrzuca single-word quoted tokens z `HTTP 200 + "The specified phrase is too short."` — co `_http_get_json` raise'uje teraz immediately jako `GdeltQueryError` (no retry, no rate-limit burn). Static lint w `tests/thematic/test_gdelt.py::TestGdeltThemesYamlWellFormed` chroni przed regresją. Live smoke per-bucket: `GDELT_LIVE_TEST=1 .venv/bin/python -m unittest tests.thematic.test_gdelt_live -v` (~90s wall, opt-in, nie odpalany domyślnie).

**Recently RESOLVED:** OSS phase-robust-backtesting G4 cost-stress no-op bug fixed upstream in v0.2.3 (2026-05-14, PR #2 to `kamilpajak/phase-robust-backtesting`). `_RESULT_LINE` regex now captures optional `α-net 4F=...% t-net=...` block; `_AGGREGATED_KEYS` includes `alpha_t_net`. Future paradigm #14 + #15 audit launches via `alphalens audit <strategy>` → OSS `run_audit` path now have a functional G4 gate. Legacy experiment scripts emitting only gross tokens degrade to pre-fix no-op for that row, detectable via `has_net_regression=False` field.

Issues dotyczące CLOSED warstw (Lean Docker setup, Layer 2d backtest workflow, themed gate Phase 2) → patrz `launchd/archived/README.md` + `docs/research/paradigm_failures_postmortem.md`.

## VPS backfills (always-on, `jacoren@`)

Long-running data acquisition jobs that don't fit on the laptop run on the dedicated Linux VPS at `/home/jacoren/AlphaLens`. systemd-user units are versioned in `deploy/systemd/` and survive logout via `loginctl enable-linger jacoren`. Inspect via `journalctl --user -u <unit>` on the VPS.

| Unit | Pattern | Script | Output cache | Wall-time | Status |
|------|---------|--------|--------------|-----------|--------|
| `form4-backfill.service` | long-running daemon (`Type=simple` + `Restart=on-failure`) | `scripts/run_form4_backfill.py` | `~/.alphalens/form4_parquet/` | ~5-10 days (SEC 10 req/s) | DONE 2026-05-08 (37MB final, 2.66M rows) |
| `av-earnings-backfill.{service,timer}` | daily oneshot (`Type=oneshot` + `OnCalendar=*-*-* 00:05 UTC` + `Persistent=true`) | `scripts/av_earnings_daily_backfill.py` | `~/.alphalens/av_cache/earnings_<T>.json` | ~21 days (AV free-tier 25/day) | LIVE (paradigm-14 PEAD v2 backfill) |
| `alphalens-thematic-daily.{service,timer}` | daily oneshot (`Type=oneshot` + `OnCalendar=*-*-* 06:30 UTC` + `Persistent=true`) wrapping `docker compose run --rm pipeline run_thematic_day.sh` | `alphalens thematic {ingest,extract,map-themes,score,brief}` + `scripts/export_briefs_to_json.py` | `~/.alphalens/thematic_briefs/` + `~/AlphaLens/web-data/{days.json,days/<date>.json}` | ~5-15 min (Polygon news + Gemini API) | NEW 2026-05-19 — feeds the Cloudflare-fronted SvelteKit dashboard at `~/AlphaLens/web/` (image `alphalens-web` exposing 127.0.0.1:8080) |

**Why these run on VPS, not Mac:**
- Mac sleeps / restarts → multi-day jobs lose state; VPS is always-on.
- VPS is on residential ISP with different IP than Mac (SEC 10 req/s is per-IP).
- AV daily quota resets at 00:00 UTC regardless of timezone; cron-trigger at 00:05 UTC catches the window cleanly.

**Cache durability + sync:**
- All caches live under `~/.alphalens/<area>/` on VPS (general-purpose, not paradigm-specific).
- Nextcloud sync between VPS and Mac is opt-in per script (`--rclone-remote` arg). Currently OFF for both backfills — VPS cache is the source of truth for downstream consumers running on VPS.
- For cross-machine consumption (Mac-side B1 dev, audits), use `rsync -av jacoren@vps:.alphalens/<area>/ ~/.alphalens/<area>/` or enable rclone sync in the systemd unit.

Operator deployment recipe lives in `deploy/systemd/README.md`. Web + thematic Docker stack operator recipe lives in `deploy/docker/README.md`.

## Environment

- API keys w `.env` (GOOGLE_API_KEY, ALPHA_VANTAGE_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, POLYGON_API_KEY, PERPLEXITY_API_KEY)
- Google API key też w macOS Keychain pod `google-api-key`
- LLM config: Gemini 3 Pro (guru pilot, low thinking budget)
- Runtime data (poza repo, survives git ops):
  - `~/.alphalens/candidates.db` — Layer 1 candidate queue (historical log; no live drain)
  - `~/.alphalens/watchdog/` — portfolio.yaml, EDGAR dedup, digest, launchd logs
  - `~/.alphalens/lean/{data,results,logs}/` — Lean OHLCV cache (also used by backtest replay)
  - `~/.alphalens/guru_cache/` — guru pilot LLM response cache
  - `~/.alphalens/form4_parquet/` — VPS Form-4 backfill output (hive-partitioned, see `## VPS backfills`)
  - `~/.alphalens/av_cache/` — VPS AV EARNINGS daily backfill output (per-ticker JSON, see `## VPS backfills`)
