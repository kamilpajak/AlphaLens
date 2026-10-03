# ADR 0011 — Split `alphalens-research` into pipeline + research workspace members

- **Status:** Accepted
- **Date:** 2026-05-23
- **Supersedes:** none

## Context

Through 2026-05, the single workspace member `apps/alphalens-research/` held
roughly 38 kLOC of Python. About a quarter of that was live production —
the SEC EDGAR detector running every 15 minutes on launchd, the daily
thematic pipeline that produces the briefs cache for the Cloudflare-fronted
dashboard, the weekly + monthly Perplexity literature review, the data
clients keeping shared rate-limit budgets for SEC / AV / Gemini / Polygon,
and the paper-trade refresh job. The remaining three quarters was the
research lab — screeners, the backtest engine, attribution, overlays,
gates, preaudit, diagnostics — with most layers marked `RESEARCH_ONLY` or
`CLOSED`.

Two concrete problems followed from the unified naming:

1. **The package name lies.** A reader new to the repo (or the operator
   six months later) sees `alphalens_research.thematic` and reasonably
   assumes "research playground." It is, in fact, the script that ships
   briefs to production every morning at 06:30 UTC.
2. **There was no enforced direction between live and lab.** Several
   reverse-imports had crept in — paper-trade reaching back into
   screeners, the backtest engine reaching into a data-store module,
   `core/registry.py` (CLI orchestration) sitting next to runtime
   plumbing — making it possible for a lab refactor to accidentally
   touch the live ingest path.

Pre-work in PR1 (commit `c12d03f` plus zen finding `590775a`) carved
reusable scorers into a fresh `scorers/` namespace, moved
`survivorship_pit.py` into `diagnostics/`, and moved `core/registry.py`
into `screeners/`. That untangled the existing reverse-import set so a
clean directory-level split was possible.

## Decision

Split `apps/alphalens-research/` into two workspace members joined by a
one-way dependency edge.

```
apps/alphalens-pipeline/      ← live infra + services + CLI binary
apps/alphalens-research/      ← lab tier
apps/alphalens-django/        ← briefs REST API (unchanged)
apps/web/                     ← SvelteKit dashboard (unchanged)
```

**Pipeline side** (`apps/alphalens-pipeline/alphalens_pipeline/`):

- `edgar_detector/` — Layer 1 SEC EDGAR poller (launchd).
- `thematic/` — daily VPS pipeline.
- `literature_scanner/` — monthly + weekly Perplexity scans (launchd).
- `data/` — PIT store, vendor clients (SEC, AV, Gemini, Polygon),
  universes (S&P 500/400/600 PIT yamls), factors.
- `core/` — candidates queue plumbing.
- `scorers/` — reusable validated-scorer library carved out of research
  per [`feedback_validated_paradigm_scorer_reuse_2026_05_16`].

The CLI binary `alphalens` is registered in
`apps/alphalens-pipeline/pyproject.toml`. Research-side commands
(`audit`, `preaudit`, `preregister`) lazy-import the lab tier inside
command bodies, so the pipeline package has zero top-level imports from
research. This pattern was already documented in CLAUDE.md
("Lazy CLI imports") for startup-cost reasons; the workspace split
generalises it.

**Research side** (`apps/alphalens-research/alphalens_research/`):

- `screeners/`, `gates/`, `backtest/`, `overlays/`, `attribution/`,
  `preaudit/`, `diagnostics/`, `paper_trade/`.

`alphalens-research` declares `alphalens-pipeline` as a workspace
dependency via `[tool.uv.sources]`.

**Direction enforcement** (`apps/alphalens-research/tests/test_module_dependencies.py`):

- `alphalens_research.*` MAY import from `alphalens_pipeline.{data, core, scorers}` — lab consumes infra.
- `alphalens_pipeline.*` MUST NOT import from `alphalens_research.*` at top level.
- The CLI command modules under `alphalens_cli.commands.{audit, preaudit, preregister}` are the documented exception: they lazy-import research inside function bodies.

The enforcement uses an `ast.NodeVisitor` walk so the rule fires on
`import X`, `from X import Y`, and any of those forms nested inside
`if TYPE_CHECKING:`, `try/except`, or `with` blocks (zen finding from
the PR2 review hardened this past a `tree.body`-only scan).

**Test layout decision (pragmatic):** all tests stay in
`apps/alphalens-research/tests/`. uv's workspace install gives every
member full visibility into every other, so a test in research/tests/
can exercise pipeline code freely. Splitting the test tree across both
apps would force two discover invocations + duplicate pytest fixtures
without giving anything CI cares about — the same enforcement tests
catch DAG violations regardless of which directory holds them.

## Consequences

**Positive.**

- The name on the directory tells the truth about what runs in
  production vs what lives in the lab. A reader can tell at a glance
  which code change touches the live ingest path.
- The DAG is enforced, not aspirational. Future refactors that try to
  reach from pipeline into research fail loudly in CI rather than
  drifting back into a tangled state.
- The carved-out `alphalens_pipeline.scorers/` library makes the
  reusable-scorer-from-failed-paradigm pattern explicit. New tools
  (e.g. the thematic event-driven assistant) pick from a published
  surface rather than dredging through closed paradigms.
- Per-app installs become viable for narrow CI matrices later
  (e.g. lint-only on the django app) without restructuring.

**Negative.**

- Three workspace members instead of two means three `pyproject.toml`
  files to keep in step on shared tooling versions (ruff, coverage,
  bandit). `[dependency-groups].dev` at workspace root handles the
  shared dev tools, but a member-specific tweak still requires
  touching the right `pyproject.toml`.
- `Dockerfile.pipeline` now copies from `apps/alphalens-pipeline/`
  rather than `apps/alphalens-research/`. Anyone with a forked
  CI/deploy will hit one mechanical-rename round.
- The CLI's `audit` and `preaudit` commands carry a duplicated
  `_DEFAULT_SMOKE_TIMEOUT_S` constant on the pipeline side because
  `typer.Option` evaluates defaults at import time, and the CLI
  cannot import research at top level. Parity is pinned by
  `apps/alphalens-research/tests/test_preaudit_cli_default_in_sync.py`.

## How the rollout happened

The split landed in three stacked PRs on the Django-migration integration
arc (since merged to `main` — see [ADR 0009](0009-django-replaces-fastapi.md)):

- **PR #193** (`c12d03f`, `590775a`) — pre-work: scorer carve-out,
  `survivorship_pit.py` relocation to `diagnostics/`,
  `core/registry.py` relocation to `screeners/`. Zero directory-level
  changes; all reverse-imports cleared.
- **PR #194** (`d9fdd91`, `413bfd9`) — mechanical directory split,
  workspace config, enforcement-test extension, CLI lazy-import
  hardening, SHA256 component-hash re-lock for paradigm-14 PEAD v2
  pre-registered audit components.
- **PR #195** (this PR) — Docker/systemd/runpod path updates, PIT
  roster relocation to pipeline-side (fixes a silently-broken
  `DEFAULT_DATA_ROOT` from PR2), `CLAUDE.md` / `README.md` restructure,
  this ADR.

[`feedback_validated_paradigm_scorer_reuse_2026_05_16`]: ../../.claude/projects/-Users-jacoren-Developer-Personal-AlphaLens/memory/feedback_validated_paradigm_scorer_reuse_2026_05_16.md

## Amendment 2026-10-03 — `data/` is a shared tier, not live infrastructure

The Decision section lists `data/` under "Pipeline side … live infra + services".
For half of that package the description is wrong, and the architecture audit
of 2026-10-02 measured it: of the 31 live-tier modules the deployment never
reaches, **11 modules / 1 649 LOC are referenced only by the research tier** —
research scripts, the lab and tests — and every one of them is under
`alphalens_pipeline/data/`
([`docs/research/architecture_audit_2026_10_02.md`](../research/architecture_audit_2026_10_02.md)
§6.1, finding 5):

```
293  data.factors                        140  data.universes.sp1500_pit
234  data.fundamentals.sue               128  data.alt_data.yfinance_cache
221  data.alt_data.av_earnings_client    111  data.macro.signals
208  data.alt_data.ivolatility_smd_cache  97  data.store.history
 96  data.alt_data.pit_universe_loader    86  data.alt_data.pit_universe
 35  data.alt_data.russell_universe
```

**Decision: `data/` is the project's single data-acquisition and PIT-store
tier, shared by the live services and the lab.** A new data client or store
reader goes there whatever reads it today. The alternative — moving the eleven
modules to `alphalens_research/` so that `alphalens-pipeline` holds only
deployed code — is rejected for two measured reasons.

1. **It would split one vendor's surface across two apps.**
   `data.alt_data.av_earnings_client` is research-only and imports the
   canonical live `data.alt_data.alphavantage_client`. The repo's
   "one canonical HTTP client per external vendor" rule exists so a reader
   finds every call to a vendor in one place; moving the research half to the
   lab puts Alpha Vantage access in two members. `data.fundamentals.sue` has
   the same shape against three `data/` modules.
2. **The consumer set is not stable, so a boundary drawn on it would move.**
   These modules are read by studies, and a module has a caller while a
   paradigm is being audited and none once it closes. Both directions are
   already on the record: `data.universes.sp1500_pit` is named in `CLAUDE.md`
   as the implementation contract for the next paradigm (work not yet started),
   while `data.store.history` is named in three merged pre-registration
   parameter files under `docs/research/preregistration/` (work already done).
   A boundary that has to be redrawn whenever a study starts or ends is not a
   boundary.

**What this costs.** The Positive consequence "the name on the directory tells
the truth about what runs in production" is now narrower than written:
`alphalens-pipeline` means *live services plus the shared data tier*, not
*everything here runs in production*. A reader who needs the stronger
statement has to measure it rather than read it off the directory —
`apps/alphalens-research/scripts/arch` computes reachability from the real
entry points, and audit §6.1 is its output.

**What does not change.** The direction rule stands exactly as enforced:
`alphalens_research.*` may import `alphalens_pipeline.{data, core, scorers}`,
and `alphalens_pipeline.*` must not import `alphalens_research.*` at top level
(`apps/alphalens-research/tests/test_module_dependencies.py`). Declaring the
tier shared changes which side new code lands on, not which direction imports
may point.

**Still open, deliberately.** `data/` writes stores that a live reader opens by
path and never imports — `thematic/sources/form4_store.py` reads
`~/.alphalens/form4_parquet/` with no import edge to the writer and no schema
gate between them (#1676, #1679). A shared tier makes that contract more
load-bearing, not less.
