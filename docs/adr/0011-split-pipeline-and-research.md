# ADR 0011 — Split `alphalens-research` into pipeline + research workspace members

- **Status:** Accepted
- **Date:** 2026-05-23
- **Supersedes:** none

## Context

Through 2026-05, the single workspace member `apps/alphalens-research/` held
roughly 38 kLOC of Python. About a quarter of that was live production —
the SEC EDGAR watchdog running every 15 minutes on launchd, the daily
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

- `watchdog/` — Layer 1 SEC EDGAR poller (launchd).
- `thematic/` — daily VPS pipeline.
- `literature_review/` — monthly + weekly Perplexity scans (launchd).
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

The split landed in three stacked PRs against `feature/django-migration`
(parked away from `main` while the Django stack is shadow-deployed on
the VPS — see [ADR 0009](0009-django-replaces-fastapi.md)):

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
