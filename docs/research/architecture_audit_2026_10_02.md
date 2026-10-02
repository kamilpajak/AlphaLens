# Architecture audit — AlphaLens

**Status:** LOCKED
**Date:** 2026-10-02
**Baseline:** `main` @ `e49a5c1b`
**Harness:** `apps/alphalens-research/scripts/arch/` (tests in `apps/alphalens-research/tests/arch/`)
**Scope:** read-only diagnosis. No production code changed, nothing deleted.

**Acted on since (2026-10-02):** finding #4 shipped —
`alphalens_pipeline.paper.calendar` moved to
`alphalens_pipeline.market.calendar`. Every count in this memo is the
measurement at baseline `e49a5c1b` and is left as measured; read the module
name in §0, §2.4 and finding #4 as the pre-move name.

---

## 0. How to read this

Every number below is produced by committed code and can be re-run. Where a
number came from a one-off read instead, it says so and carries the read
timestamp.

Three numbers quoted while planning this audit were **wrong**, and §9 says how.
They are corrected in place here. The corrections matter more than the original
figures, because two of them pointed at the wrong problem.

The headline conclusion contradicts the premise the audit started from.
AlphaLens is **not** a tangled monolith. The import graph is almost acyclic,
the documented layering holds, and the largest file's functions are individually
simple. What it has instead are three specific problems, in descending order of
cost:

1. **The largest unit of organisation is the file, and some files are 10 000+
   lines.** They are flat collections of small functions with no internal
   boundary, so the only way to find out what relates to what is to read names.
2. **Most of the coupling is invisible to any import-based tool**, because
   stages pass state through parquet columns and Postgres tables rather than
   function signatures. 77 of the 164 columns in the brief store cross no
   schema gate at all.
3. **The architecture's rules exist but are scattered across ~40 test files.**
   They work. Nobody can hold them in their head, and no document lists them.

---

## 1. Orientation — the map

### 1.1 Size, and where it actually is

Counted two ways and in agreement (`git ls-files '*.py' | xargs wc -l` against
the harness's own walk):

| Slice | Files | LOC | Share |
|---|---:|---:|---:|
| All tracked Python | ~1 450 | **430 044** | 100% |
| `apps/alphalens-research/tests/` | 749 | **243 442** | 57% |
| `apps/alphalens-research/scripts/` | 236 | 47 018 | 11% |
| Everything else (all production code, all apps) | ~470 | ~139 600 | 32% |
| `apps/web` (TypeScript + Svelte, separate) | 115 | 18 092 | — |

**The single largest architectural fact in this repository is that 57% of its
Python is one directory of tests, and that directory holds the tests for every
workspace member** — the broker, the contract, `intent-replay`, Django-adjacent
helpers and the research lab alike. §7 covers the consequences.

Production code by app:

| App | LOC | What it is |
|---|---:|---|
| `alphalens-pipeline` | 91 534 | live infrastructure + the CLI binary |
| `alphalens-research` (library only) | ~24 100 | the lab: backtest, attribution, screeners, diagnostics |
| `alphalens-django` | 14 151 | the briefs/edge REST API |
| `alphalens-broker-contract` | 4 754 | shared, dependency-free contract leaf |
| `intent-replay` | 4 681 | research backtester over a TradeIntent document |
| `alphalens-feedback` | 214 | shared feedback primitives |

Inside the pipeline, where the mass is:

| Subpackage | LOC |
|---|---:|
| `brokers/` | 27 110 |
| `thematic/` | 20 277 |
| `data/` | 16 477 |
| `feedback/` | 11 210 |
| `alphalens_cli/` | 7 377 |
| `experts/` | 3 578 |
| everything else (`edgar_detector`, `events`, `market`, `paper`, `scorers`, `core`, `observability`, `literature_scanner`) | ~5 500 |

### 1.2 How anything starts

The deployment has **27 entry points from 24 origins**, read off
`deploy/systemd/*.service`, the console script, the compose services and
Django's string-loaded configuration (`scripts/arch/entrypoints.py`). Nothing
else runs in production.

| Origin | Count | Notes |
|---|---:|---|
| systemd units | 19 | 20 unit files; several share the CLI root |
| Django | 5 | WSGI, URLconf, settings, 2 management commands |
| console scripts | 3 | `alphalens`, `intent-replay`, `python -m intent_replay` |

`alphalens_cli.main` imports every command module at top level, so **one root
covers the whole CLI surface** however it is invoked — from a unit file, from a
shell wrapper, or as the pipeline container's entrypoint.

### 1.3 The contexts, named

Nine contexts, each with one owner and one way in. This table is the answer to
"where am I?".

| Context | Lives in | Entered by | Writes |
|---|---|---|---|
| **Execution** (bracket keeper) | `alphalens_pipeline/brokers/**`, `broker_contract`, `alphalens_cli/commands/broker.py` | `alphalens broker manage` / `arm` / `auth` / `price-reader` daemons + timers | `broker_orders/` journals |
| **Thematic selection** | `thematic/**`, `experts/**`, `scorers/` | `alphalens thematic {ingest,extract,map-themes,score,brief}` in the daily container | `thematic_briefs/`, `thematic_news/`, `buffett_qual/` |
| **Feedback / edge** | `feedback/**`, `alphalens-feedback`, Django `edge/` | `alphalens feedback backfill-shadow-returns`, hourly edge mirror | `population_ladders/`, Postgres `edge_ladderoutcome` |
| **Data acquisition + PIT store** | `data/**` | Form-4 and grouped-daily timers, plus lab callers | `form4_parquet/`, `grouped_daily_history/`, `companyfacts_parquet/` |
| **EDGAR detection** | `edgar_detector/` | `alphalens edgar detect`, every 15 min | `edgar-detect/` |
| **Literature scanning** | `literature_scanner/` | weekly + monthly timers via a shell wrapper | `docs/research/literature_review/` (commits to `main`) |
| **Publication** | `alphalens-django/{briefs,market,config,auth_cf}`, `apps/web` | gunicorn behind the tunnel; Cloudflare Pages | Postgres `briefs`, `days_meta` |
| **Research lab** | `alphalens_research/**` + `scripts/` | by hand, plus 8 systemd-run scripts | `audit/`, ad-hoc |
| **Intent replay** | `apps/intent-replay` | by hand (`python -m intent_replay`) | nothing — it stamps no state |

Two context boundaries are **not** where their directory names suggest:

- **`alphalens_pipeline/data/` is substantially research-tier, not live
  infrastructure.** 11 of its modules (1 649 LOC) are referenced only by
  scripts, the lab and tests — never by anything the deployment runs (§6).
  ADR 0011 frames the pipeline app as live infrastructure; for `data/` that is
  only partly true.
- **`alphalens_pipeline/paper/` is a decommissioned context holding the
  repository's most-shared primitive.** ADR 0012 decommissioned paper trading,
  yet `paper.calendar` has the highest static inbound reference count in the
  repo (41). The name misleads and the package status contradicts its use.

---

## 2. Dependency map

### 2.1 The graph

Production corpus (the 8 roots in `graph.PRODUCTION_ROOTS`), measured at
`e49a5c1b`:

| Measure | Value |
|---|---:|
| Modules | 642 |
| Edges, all kinds, observed | 1 455 |
| …runtime (not `TYPE_CHECKING`) | 1 439 |
| …`TYPE_CHECKING`-only | **16** |
| …function-scope (lazy) | 241 |
| …runtime **and** top-level | 1 198 |

The `TYPE_CHECKING` split was added because the review warned it inflates
coupling. On this repository it does not: **16 edges out of 1 455, 1.1%.** The
correction was worth making and its effect is immaterial. Saying so is the
point — the alternative is a plan that claims a fix for a problem it never had.

The lazy-import share is the opposite case: **241 edges (17%) exist only inside
function bodies.** `PLC0415` is ignored repo-wide, so this is sanctioned, and
it is load-bearing — see §2.2.

### 2.2 Cycles

| View | Cycles |
|---|---:|
| Runtime, observed edges (**the number to quote**) | **4** |
| Runtime + type-only | 4 |
| Runtime, **top-level only** | **0** |
| With implicit parent-package edges | 20 |

Two readings matter.

**There are no import-time cycles at all.** Every one of the four cycles exists
only through a function-scope import. The lazy imports are doing real work:
they are what keeps a genuine mutual dependency from being an import-time one.

**The four real cycles:**

| Cycle | Verdict |
|---|---|
| `brokers.automanager.control_loop` ↔ `brokers.automanager.live_exit_engine` | Real, and the biggest one. Both halves are in the file §4 is about. |
| `thematic.trade_setup.{builder, config_version, model}` | Real 3-cycle. |
| `broker_contract.exit_geometry.{policy, registry}` | Real 2-cycle inside the shared contract leaf. |
| `data.fundamentals.{ff_industries, sic_index}` | Real 2-cycle, research-tier. |

The 20-cycle figure is what you get by counting the implicit `pkg.mod → pkg`
edge, which is real at runtime but turns every package whose `__init__`
re-exports a submodule into a cycle. It is a modelling choice, it is off by
default in the harness, and `tests/arch/test_graph.py` pins that.

### 2.3 Does the declared layering hold?

Yes. `tests/test_module_dependencies.py` enforces **28 rules** across 5
packages, with 8 positive controls and a test that makes a stale exemption red.
The audit confirms it binds: the only cross-tier `pipeline → research` imports
are the documented lazy ones inside CLI command bodies.

`import-linter` + `grimp` would express these rules declaratively instead of as
a 1 221-line AST walk. **Recommendation: do not switch.** The existing gate has
a property import-linter does not — `test_exemptions_still_exist` fails when an
exemption stops hiding a real violation, which is what has kept the allowlist
from widening. Trading that for nicer syntax is a downgrade.

### 2.4 Static inbound references

The most-referenced modules are the de-facto shared surface. **This is not a
public-API measure** — a high count can be a popular helper or an `__init__`
re-export hub, and an API is a contract rather than an edge count.

| Module | Inbound |
|---|---:|
| `alphalens_pipeline.paper.calendar` | **41** |
| `alphalens_research.backtest.metrics` | 31 |
| `alphalens_research.attribution.factor_analysis` | 28 |
| `alphalens_pipeline.data.factors` | 25 |
| `alphalens_pipeline.data.alt_data.yfinance_cache` | 24 |

The top entry is the finding: the repo's most-shared primitive lives in the
package ADR 0012 decommissioned, and it is referenced nearly as often as the
next three put together.

Two entries in this table are research-tier (`backtest.metrics`,
`attribution.factor_analysis`) and rank high because the 133 research scripts
import them. That is expected and healthy — it is a library being used as one.
It also means this table mixes two populations, which is the reason it is not
labelled a public-API ranking.

---

## 3. Bounded contexts — the evidence, and what it cannot settle

Three independent signals were measured. They agree more than they disagree,
which is the useful result.

**Import clustering** (§2) separates the contexts cleanly. **Runtime ownership**
(§1.2) gives each context exactly one entry point, with the two exceptions
named in §1.3.

**Co-change coupling** — how often two contexts appear in the same commit, as a
share of the smaller one's commits, over 1 155 classified commits since
2026-04-01:

| Pair | Coupling |
|---|---:|
| publication ↔ feedback | 34.7% |
| data ↔ thematic | 23.5% |
| deploy ↔ execution | 21.2% |
| data ↔ execution | 19.6% |
| **execution ↔ thematic** | **does not register** |

Treat this as corroboration only, never as a boundary. A solo author bundles
related work into one commit, and the measurement shows that artifact plainly:
`docs` co-occurs with everything, because a design memo ships with the code it
describes. The one pair worth looking at is publication ↔ feedback, which is
the `/edge` dashboard: Django serializers and the feedback pipeline change
together because they share a data contract (§5), not because they share code.

That `execution ↔ thematic` does not register is a real positive result: it
independently confirms what the parked split blueprint claims, that the broker
is already decoupled from selection.

**What none of this can settle.** A bounded context is where the domain
language changes, which is a judgement, not a graph property. The table in
§1.3 is the audit's best reading of the evidence; it needs one pass with the
owner to name each context in his own vocabulary and confirm the two
boundaries that contradict their directory names. That pass is the remaining
work on this question.

**Parked split blueprint.** `docs/research/bracket_keeper_repo_split_stage1_design_2026_08_02.md`
was written at `7d6783f9`, two months and several hundred commits ago. Its
central claim still holds at `e49a5c1b`: the execution context's only residual
upward edge of substance is `paper.calendar`, which §2.4 independently
identifies as the repo's most-shared primitive. The blueprint's edge table
should be re-verified line by line before it is executed, but it has not rotted.

---

## 4. Hot spots

Ranked by four signals, reported separately. No composite score: the units are
unlike and any weighting would be invented.

| File | LOC | Commits since 2026-04-01 | Fan-out | Money path |
|---|---:|---:|---:|---|
| `brokers/automanager/control_loop.py` | **11 130** | **130** | **43** | **yes** |
| `feedback/population_ladder_monitor.py` | 3 271 | 47 | 14 | no |
| `alphalens_cli/commands/broker.py` | 3 159 | 68 | 35 | places nothing (AST-gated) |
| `brokers/saxo/broker.py` | 2 310 | 43 | 15 | yes |
| `feedback/ladder_replay.py` | 1 910 | 23 | — | no |

### 4.1 `control_loop.py` — what it actually is

The size suggests a tangle. It is not one.

| Measure | Value |
|---|---:|
| Lines | 11 130 |
| Functions and methods | 301 |
| Lines inside functions | 9 866 (88%) |
| Functions with cyclomatic complexity > 15 | **4** |
| Deepest function | 21 (`_build_managed_exits`, 127 lines) |
| Functions at complexity 1–5 | 204 of 301 |
| Module-level mutable bindings | 4, of which **2 are genuine state** (`set()` log throttles, one function each); the other two are a `Mapping`-typed lookup table and `__all__` |
| **Order-placing / amending call sites** | **5, in 5 functions** |
| Module-level functions | 265 |
| Classes | 19, of which 14 have no methods (records) |
| `__all__` | 7 names |

The money path is narrow: five call sites in 11 130 lines. The complexity
distribution is healthy. There is no shared-mutable-state problem.

The structure is explicit and already has a seam:

- `LoopDeps` — a **31-field** dependency record, 158 lines, no methods.
- `build_default_deps` — the composition root: **243 lines wiring 34 keyword
  arguments**, and it lexically reaches 114 functions (4 538 LOC, 41% of the
  file) that the tick only reaches through the record.
- `run_once` — 79 lines. `run_daemon` — 89 lines.

**So the file is a package that was never split into files.** The interface
between its parts already exists as `LoopDeps`; what is missing is only the
module boundaries. A decomposition would group those 114 wired functions into
modules and keep `build_default_deps` as the wiring — which is also why the
parked split blueprint flags `build_default_deps` as the risky surface: it is
the one place that knows everything.

16 functions (355 LOC) are reachable only through injected factories and
callbacks, so no call-graph walk sees them. One of them,
`_reset_remote_quote_source_for_tests`, is a test hook living in production
code on the money path.

**Not done here, by design:** no code was moved. Decomposing a live
order-placing file is its own arc with its own review.

### 4.2 The complexity gap

Nothing in this repository gates function size.

- SonarCloud's `S3776 ≤ 15` is **per function**, so an 11 130-line file whose
  functions each comply passes it. `control_loop.py` has only 4 functions over
  the threshold, so Sonar is near-silent on the largest file in the tree.
- The Sonar job runs `continue-on-error: true`, so it blocks nothing anyway.
- `ruff` ignores `PLR0915` (statements), `PLR0912` (branches) and `PLR0911`
  (returns) repo-wide, by project choice.
- `sonar.exclusions` drops `**/tests/**` and both `scripts/**`, which is 68% of
  the Python in the repo.

This is a coherent set of choices for a solo research repo. It is worth knowing
that it adds up to: **file size is unmeasured by every gate in CI**, which is
how an 11 130-line file grows without anything objecting.

---

## 5. Out-of-band contracts — the coupling no import graph sees

This pipeline passes state between stages through on-disk columns, not function
signatures. Everything in §2 is blind to it.

### 5.1 The brief store

Read from one real file: `~/.alphalens/thematic_briefs/2026-09-04.parquet`,
file mtime `2026-09-05T10:56:47`. This is a **local mirror**, not the VPS
source of truth, and it is ~4 weeks old, so treat the exact counts as
indicative and the structure as the finding.

| Measure | Value |
|---|---:|
| Columns in the parquet | 164 |
| Fields on the Django `Brief` model | 99 |
| In both | 87 |
| **Parquet-only** | **77** |
| Model-only (Django derives them) | 12 |

The 77 parquet-only columns by family: `options_` 16, `buffett_` 14,
`channel_` 11, `oneil_` 9, `brief_` 5, `shadow_` 5, `si_` 4, plus singles.
The 16 `options_*` match what `CLAUDE.md` documents, which is an independent
check that the read is measuring the right thing.

**The gated boundary is the wrong one.** `briefs/tests/test_schema_parity.py`
freezes a 107-column legacy contract against the `Brief` model, and
`test_openapi_parity.py` freezes the published schema. So the pipeline → API
boundary is gated. The pipeline-stage → pipeline-stage and pipeline → lab
boundary, where **77 of 164 columns live**, has no schema gate at all. A stage
that renames or stops writing one of those columns breaks its readers silently.

### 5.2 Stores as contracts

| Store | Written by | Read by |
|---|---|---|
| `broker_orders/*.jsonl` | execution | execution, tests (17 files), lab, CLI |
| `thematic_briefs/` | thematic | Django ingest, lab, scripts, tests |
| `population_ladders/` | feedback | Django edge mirror, scripts, `alphalens-feedback` |
| `form4_parquet/` | data (systemd scripts) | scripts (6), pipeline |
| `grouped_daily_history/` | data (systemd timer) | thematic `score` |
| Postgres `briefs`, `days_meta`, `edge_ladderoutcome` | Django ingest / mirror | the API and SPA |

`broker_orders/` is referenced from 17 test files — the journal format is, in
practice, this repository's most heavily specified contract, and it is specified
by tests rather than by a schema.

### 5.3 Method limitation, stated

A first attempt to count columns by pattern-matching subscript assignments in
pipeline source found 233 names and **missed the `buffett_*` and `oneil_*`
families entirely** (they are not written through literal subscripts). That
instrument is a lower bound, not a census, and its number is not published here.
The store read in §5.1 replaced it. A full data-contract map — every store,
every column, its writer, its readers, and whether a gate protects it — is the
largest piece of work this audit leaves open.

---

## 6. Dead code

Four separate queries. They are not summed and they are not four independent
votes: static reachability and symbol scanning share the same blind spot
(dynamic dispatch), so agreement between them is not corroboration.

### 6.1 Unreachable from the deployment

642 modules; 287 reachable from the 27 entry points. The raw "unreachable"
figure is **not a dead-code number** — most of it is research scripts and the
lab, which are their own entry points and are not meant to be reachable from
systemd. Asked per tier, with "who else references it" as the second axis:

**Live-tier modules the deployment never reaches: 31, totalling 4 542 LOC.**

| Bucket | Modules | LOC | What it means |
|---|---:|---:|---|
| Referenced by **nothing at all** | **1** | **83** | `data.macro.scorer` — the only genuine orphan in the live tier |
| Referenced **only by tests** | 12 | 1 676 | seams and probes; 3 are already documented as deliberate in `deadcode_broker.UNWIRED_ALLOWED` |
| Referenced only by **research tier** (scripts / lab / tests) | 11 | 1 649 | research-tier code filed under live infrastructure — the §1.3 finding |
| Referenced by live code that is itself unreached | 7 | 1 134 | an unreached cluster of data plumbing |

### 6.2 Reachable only from tests

The 12 modules above. Three carry documented reasons. The rest are candidates
for a question, not for deletion: a module only a test calls is either a seam
kept on purpose or speculative generality, and the two look identical from
outside.

### 6.3 Symbol level

`vulture --min-confidence 60` over production code: **260 findings**, of which
7 are at 100% confidence and **all 7 are false positives** — `__exit__`
protocol parameters (`exc_type`, `tb`) and argparse callback signatures. The
tool's confidence labels are not probabilities of deadness in this codebase.
This is the lowest-yield of the four queries and should not be the headline.

### 6.4 Cold scripts

**149 of 236** research scripts have not been touched since 2026-07-01. Last
touch is not execution telemetry: a one-shot experiment script is *supposed* to
go cold, and eight of these are run by systemd. Coldness here is a reading
queue, not a verdict.

### 6.5 What is deliberately kept

Before anything in §6 is called dead it has to clear the standing decisions:
the three CLOSED screeners stay in tree by owner decision, `__status__` markers
record lifecycle on 41 packages and 3 modules, and `broker_contract/trade_intent/legacy.py`
registers four allowances with the observation that retires each. The audit
deletes nothing and recommends no deletion; §6.1's single genuine orphan is the
only entry where the question is simple.

---

## 7. The test suite's own architecture

243 442 LOC in 749 files under `apps/alphalens-research/tests/`, holding the
tests for every workspace member. Consequences, stated plainly:

- **A research-tier app gates the live money path.** The broker, the contract
  and `intent-replay` are all verified from the research app's test directory.
- **~40 structural gates live there**, including the 28-rule import gate, 11
  vendor-HTTP gates, the AST gate that stops any CLI module placing an order,
  registry↔README table parity, and a CI-config self-parity cluster. This is
  where the architecture is actually written down.
- **The biggest test file is 10 493 lines** (`test_control_loop.py`), covering
  the 11 130-line file in §4. Its class and method names are the closest thing
  to a specification that file has.
- `just test` and the CI `research` job both depend on that one directory.

This is a real choice with real benefits — one suite, one runner, no
duplication — and it is the kind of choice worth making deliberately rather
than by accumulation. Splitting a test app out is an owner decision, filed as
such.

---

## 8. Findings, ranked

Cost of leaving it against cost of fixing it. Nothing here was changed.

| # | Finding | Evidence | Leaving it | Fixing it |
|---|---|---|---|---|
| 1 | 77 of 164 brief-store columns cross no schema gate; the gated boundary (pipeline → API) is not where most columns live | §5.1 | a renamed column breaks readers silently; this is the most likely source of a quiet production defect | a column-contract gate on the parquet writer, mirroring `test_schema_parity.py` |
| 2 | `control_loop.py` is 11 130 lines with no internal boundary, on the live order path | §4.1 | every change needs the whole file in head; 130 commits in 6 months | split into modules behind the existing `LoopDeps` seam; its own arc, own review |
| 3 | No gate in CI measures file or function size | §4.2 | the next 11 000-line file grows the same way | enable `PLR0915`/`PLR0912` with a baseline, or a file-length check |
| 4 | `paper.calendar` has the repo's highest inbound count (41) while `paper/` is the package ADR 0012 decommissioned | §2.4 | misleads every reader; blocks the keeper split | DONE — moved to `alphalens_pipeline.market.calendar`; the module keeps its symbol names |
| 5 | `alphalens_pipeline/data/` is substantially research-tier (11 modules, 1 649 LOC referenced only by lab/scripts/tests) | §6.1 | ADR 0011's framing is wrong for `data/`, so new code lands on the wrong side | decide the boundary, then move or document |
| 6 | 12 live-tier modules (1 676 LOC) are referenced only by tests | §6.2 | speculative generality indistinguishable from a kept seam | extend the `UNWIRED_ALLOWED` pattern: a reason per module, or remove |
| 7 | `_reset_remote_quote_source_for_tests` is a test hook in production code on the money path | §4.1 | small, but it is a live file | fold into the injected deps |
| 8 | 4 real import cycles, all of them existing only via lazy imports | §2.2 | hidden mutual dependencies that no import-time check sees | each is small; `exit_geometry.{policy,registry}` is in the shared contract leaf and worth doing first |
| 9 | `data.macro.scorer` (83 LOC) is referenced by nothing at all | §6.1 | dead weight | delete, after one check |
| 10 | `CLAUDE.md` says 12 ADRs; `docs/adr/` holds 17 | §9 | the index nobody trusts gets trusted less | doc-only PR |
| 11 | Root `pyproject.toml` carries `per-file-ignores` for `apps/alphalens-pipeline/tests/*`, which does not exist | §9 | harmless, but it is a rule guarding nothing | doc/config PR |
| 12 | `just lint` omits `alphalens-broker-contract` and `alphalens-feedback`, which CI does lint | §9 | local lint is greener than CI | one-line `justfile` fix |

**Issues filed:** only where the owner must decide — findings 1, 2, 4, 5 and the
§7 test-app question. The rest are rows here, deliberately: the board grew +38
net in 8 weeks and the rule that followed was to fix rather than file.

---

## 9. Corrections to this audit's own planning numbers

Three figures quoted while planning were wrong. Each was re-derived and the
mechanism confirmed by reproducing the old number on purpose.

| Planning figure | Correct figure | Mechanism |
|---|---|---|
| 455 modules, 951 edges | 642 modules, 1 455 edges (production corpus); 455 modules on the narrower 5-root corpus reproduces **exactly** | The prototype walked 5 roots, omitting `alphalens_feedback`, research `scripts/` and Django. Corpus difference, not a counting bug. |
| **5 import cycles**, 3 of them guessed to be `__init__` re-export artifacts | **4 real cycles**, and **0** at import time | The prototype counted the bare `from pkg import …` prefix as an edge, which adds implicit parent-package edges and manufactured exactly one of the five (`brokers.saxo` + 3 submodules). Reproducing the prototype's semantics returns 5 with the same composition, confirming the mechanism. **Two of my three "artifact" guesses were wrong**: `thematic.trade_setup` and `exit_geometry` are real cycles. This is why §2.2 confirms each by reading rather than by graph shape. |
| **39 unreachable pipeline modules / 6 280 LOC** | **31 live-tier modules / 4 542 LOC**, of which **1 module / 83 LOC** is referenced by nothing | The prototype's root set was the CLI alone. Eight systemd units run research scripts, so modules like `form4_incremental` were reported unreachable while running nightly. |

A residual 10-edge gap between the prototype's 951 and the reproduction's 961
is the one component substituted during reproduction: the prototype resolved
relative imports in `__init__.py` files one level too high.

**A fourth correction, found during the audit itself.** The harness lives at
`apps/alphalens-research/scripts/arch/`, which is inside a production root, so
the first runs measured the harness as part of the corpus and the module count
moved three times (644 → 645 → 646) as the harness grew. It is now excluded by
exact path, unconditionally, and two tests pin it — one that the harness's own
modules are absent, and a control that an unrelated directory named `arch`
elsewhere would still be measured. Every figure in this report is from after
that fix.

**The lesson worth keeping:** the two planning numbers that moved most were the
two that pointed at a problem. "5 cycles" suggested a tangle; there is none at
import time. "39 unreachable modules" suggested dead code; there is 83 lines of
it. A number computed once, by code nobody tested, is not evidence — which is
the same failure this project recorded in the #1227 power-gate postmortem.

---

## 10. What was not measured

- **No VPS read.** §5.1 is a local mirror, ~4 weeks stale. The Postgres tables
  and the current column sets were not read. A column census against the live
  stores is the main open item.
- **No full data-contract map.** §5 maps stores to contexts and one store to
  its columns. Every store × every column × writer × readers × gate is the
  remaining work, and it is the highest-value remaining work.
- **`apps/web` got a size count only.** 18 092 LOC across 115 files, its own
  Storybook doctrine, not analysed.
- **Cognitive complexity** is McCabe cyclomatic here, not Sonar's cognitive
  metric. The two are not interchangeable; §4.2 compares like with like by
  avoiding Sonar's number rather than mixing them. Entering a context manager
  is not counted as a decision, matching standard implementations; the figures
  in §4.1 are identical either way (`control_loop.py` has 4 `with` items).
- **The context map is unconfirmed.** §3 says what the evidence supports; the
  owner naming each context is the step that completes it.
