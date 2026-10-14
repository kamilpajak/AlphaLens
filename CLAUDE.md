# CLAUDE.md

## Orientation

- Research + decision-support repo for one investor. Capital deployment is OFF-TABLE and no strategy holds a standing PASS: never propose deploying capital or automating a pick.
- The thematic tool AUGMENTS the owner's judgement (he cherry-picks, the group discusses, each member decides); never describe it as a replacement.
- uv workspace. New code goes to `apps/alphalens-pipeline/alphalens_pipeline/<name>/` (infra, live services, data clients, scorers), `apps/alphalens-research/alphalens_research/<name>/` (lab, backtest, attribution, overlays, preaudit) or `apps/alphalens-pipeline/alphalens_cli/` (CLI commands) — never at the repo top level (ADR 0011).
- IMPORTANT: `alphalens_pipeline.*` never imports `alphalens_research.*` at top level; lazy imports inside `alphalens_cli.commands.{audit,preaudit,preregister}` bodies are the only exception. Gate: `apps/alphalens-research/tests/test_module_dependencies.py`.
- A package's lifecycle status is its own `__status__` in `__init__.py`, never a table in this file.
- Layer vocabulary (ADR 0007): 1 screener, 2 selection-gate, 3 backtest engine, 4 risk overlay, 5 attribution; every failure attributes to exactly one layer. Current paths: `apps/alphalens-research/README.md`.
- Seven bounded contexts, one entry point each: read `docs/research/architecture_audit_2026_10_02.md` section 1.3 before moving code between apps.
- `/edge` answers "does the pipeline select well?", never "how is the owner's money doing".
- Solo project, zero external users: rename, refactor and drop old behaviour in one commit — never add an alias, shim or deprecation path.

## Commands

- Tests are `unittest`, never pytest: `uv run python -m unittest discover -s apps/alphalens-research/tests -t apps/alphalens-research -v`.
- Setup is `uv sync` (one workspace venv at `./.venv`). Recipes: `just sync | test | lint | dev-django | dev-web | up | down | probe-live`.
- Run `alphalens --help`, then `alphalens <group> --help`, before quoting any subcommand. Never write a command from memory: two `literature` subcommands named in this file for months did not exist.

## Code conventions

- Code, comments, docstrings and identifiers in ENGLISH; math notation (α, ρ, ×, −) stays. Polish prose belongs in CLAUDE.md, memory, commits and postmortems. Gate: `tests/test_no_polish_chars.py`.
- Every external-vendor call goes through that vendor's canonical client under `alphalens_pipeline/data/` (plus `literature_scanner/perplexity_client.py`, `telegram_client.py`) — never raw `requests`/`httpx` against a vendor URL. One `tests/test_no_raw_<vendor>_http.py` per vendor enforces it; a new vendor ships that test with its client.
- Never ask an LLM for a numeric or time-sensitive value (price, market cap, date, threshold): compute it from yfinance / EDGAR / Polygon / the Form-4 store and inject the fact. Never put a bracket constraint in a prompt — filter in Python afterwards.
- TDD always: red, green, refactor, even for a two-line fix.
- Extract magic numbers and strings into named constants; one assertion concept per test.

## Money path (stays in this file: area rules below are dropped by /compact)

- `alphalens broker arm FILE|-` is the ONLY door onto the pick queue, and every pick is a HAND-WRITTEN document from `apps/alphalens-broker-contract/examples/manual-pick/` carrying `"source": "manual"`. No automatic brief-to-intent producer exists (removed 2026-09-24).
- Two broker daemons run on the VPS at once: `alphalens-broker-manager.service` is SIM, `alphalens-broker-manager-live.service` places REAL-MONEY orders (ADR 0017). Enumerate the units before acting; never infer the environment from a command name.
- Only `broker manage` places or amends orders; an AST gate bars every other `alphalens_cli` module from that surface and allows only cancelling.
- Before changing the arming, failure-code or JSON surface, read `apps/alphalens-broker-contract/README.md` sections "The door" and "The failure contract". This file deliberately does not restate them.

## Workflow

- Every task is a GitHub ISSUE (`gh issue create --repo kamilpajak/AlphaLens`); the Projects v2 board is a view over issues, never a second registry. Always pass `--repo kamilpajak/AlphaLens` to `gh pr` and `gh issue`.
- FIX by default. Open an issue only when the owner must decide something or the work must wait for data.
- Every open issue carries exactly one `area:{alphalens,bracket-keeper,contract}` label, plus a state label only when it applies: `waiting:decision` (must name the decision), `waiting:data`, `blocked`. Board moves, Wake lines and item IDs: `docs/workflow/issues-and-board.md`.
- Treat CLAUDE.md as code: edit it in its own small PR, never bundled into a feature branch.
- Never create a branch or a commit in the main checkout. Every other session works in its own `git worktree` off fresh `origin/main` and runs its own `uv sync`.
- Auto-memory is keyed by REPOSITORY, not by path, so every worktree writes the same `MEMORY.md`: add an index line with a TARGETED edit beside a unique nearby string, never a whole-file rewrite, and keep the index under ~24 KB.
- Any `??` in the session-start `git status` is debt: commit a finished memo the same day via a throwaway docs worktree, or delete it on purpose; update a memo's `Status:` when reality moves past it. Scratch files go to the scratchpad, never the repo root.
- **Adversarial review of your OWN diff before merge**, on every non-trivial PR, skippable only for doc-only / single-line / pure-comment changes. Trigger it right after `gh pr create` — findings then land as NEW commits inside the normal CI cycle and the merge gate stays "CI green on the LATEST commit". Never amend + force-push.
- Run `/code-review high` on the open PR, or a lens-split Workflow when the diff spans several surfaces. `/code-review ultra` is the owner's to launch, not yours.
- One DISTINCT lens per reviewer, no cap on findings, EVIDENCE on each one (a command with its output, or file:line) — a claim nobody ran is not a finding. Adjudicate every finding by RUNNING it, and judge the finding and its REMEDY separately; a review that mutates code and runs the suite beats one that reads it. A finding OUTSIDE the diff is still a finding: fix it in the same commit chain when it is in the change's blast radius, otherwise file it.
- PR bodies carry a `## Known issues` section naming every limitation, scope cut and deferral.
- runpod.io CPU pods are primary compute for audits, holdouts and smoke runs over N=50; the Mac edits code and runs tiny checks. OOM-class problems ship to runpod rather than being refactored to fit.
- Persist a metered vendor download (iVolatility, $399/mo) to disk BEFORE processing it and never re-fetch on a retry. Reuse the root that already owns your endpoint — the roots in use are listed in `docs/runtime-data-layout.md`; there is no `ivolatility_cache/` and never was.

## Research methodology

- RUN IT BEFORE BELIEVING IT. A plausible account of how the code behaves is not evidence: execute against real data (VPS Postgres, `~/.alphalens/**/*.parquet`) before asserting a mechanism.
- Before asserting, name the observation that would make the claim FALSE and check that the read you just did could have produced it. Zero false positives on a corpus is half the evidence; the other half is which shapes that corpus lacks.
- Recount a gate's key N by a second path before trusting it. Studies sharing one upstream read do not corroborate each other. Publish a number together with its construction, and never beside a threshold in a different unit.
- Adjudicate another agent's finding by RUNNING it, and adjudicate the finding and its REMEDY separately.
- A number from a single day is provisional: count from Postgres and record the read timestamp beside it.
- Adversarial review of the LOCKED design memo before any run over one hour of compute — your own lens-split pass for its internal logic, Perplexity for the literature it rests on; `alphalens preaudit <strategy>` before any audit over one hour.
- Pre-register before testing and pay Bonferroni at PROGRAM level — a model-class swap on the same features and holdout does not cleanse multiplicity. Literature is not an informative prior here.
- A new data vendor passes the PIT validation gate before pre-registration, and a Layer 4 overlay quotes its `classify_cyclicality_excess` verdict in the memo (gate: `tests/test_overlay_design_compliance.py`). Both procedures: `docs/research/methodology-gates.md`.
- A universe over 100 tickers uses the true PIT panel from day one.

## Doctrine

- Keep searching for screeners: discipline bounds the search, never closure. Never write "no further prospecting".
- The owner has rejected the pivot to passive indexing. Active quant research continues.

## Known issues worth a standing reminder

- The Layer 2a prescreener is UNVALIDATED (45% fundamentals weight needs PIT historicals the Polygon plan does not serve): manual ad-hoc only, never quote it as a performance claim.
- GDELT theme queries must use multi-word quoted phrases (`"CUDA toolkit"`, never `"CUDA"`): a single-word quoted token returns HTTP 200 with "The specified phrase is too short." Lint: `tests/thematic/test_gdelt.py`.

## Where to find why

- `docs/adr/` architectural decisions; `docs/research/paradigm_failures_postmortem.md` the paradigm ledger; a layer's own `__closed_reason__` why it was killed; `docs/research/` per-strategy design and audit memos.
- Issue + board procedures: `docs/workflow/issues-and-board.md`. Runtime data stores under `~/.alphalens/**`: `docs/runtime-data-layout.md`. Research gates as procedures: `docs/research/methodology-gates.md`.
- Live VPS units, cadences and runbooks: `deploy/systemd/README.md` ("Active units"). Container and Cloudflare topology: `deploy/docker/README.md` and `apps/web/README.md`. API keys and their tiers: `.env.example`.

## Area rules

Each loads when you read or edit a file in its area, and is NOT re-injected after `/compact` — re-read it after a compaction.

- `apps/web/CLAUDE.md` — SPA: no-wrap atomic tokens, Storybook catalog doctrine, build-before-compose.
- `apps/alphalens-research/CLAUDE.md` — lab: layer map, `__status__` requirement, preaudit smoke profiles, methodology gates.
- `apps/alphalens-pipeline/CLAUDE.md` — live tier: lazy CLI imports, per-vendor quota SCOPES, the calendar MIC argument, and the `alphalens broker` CLI surface (JSON envelope, failure-code registry, the place/amend AST gate) — that code is in `alphalens_cli/commands/broker.py`, so it is briefed here, not in the contract package.
- `apps/alphalens-django/CLAUDE.md` — DRF docstrings are published schema; regenerate the OpenAPI snapshot in the same commit.
- `apps/alphalens-broker-contract/CLAUDE.md` — the shared contract: which half of the failure-code registry lives where, the legacy register, and pointers into `README.md` for the arming door and the gate order (that README is authoritative; this file never restates it).
- `apps/intent-replay/CLAUDE.md` — replaying an armed pick with `scripts/replay_from_pick.py`.
- `docs/research/CLAUDE.md` — writing a design memo: `Status:` discipline, the gates it must clear, review before compute.
