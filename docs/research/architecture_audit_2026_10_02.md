# Architecture audit — AlphaLens

**Status:** LOCKED
**Date:** 2026-10-02
**Baseline:** `main` @ `e49a5c1b`
**Harness:** `apps/alphalens-research/scripts/arch/` (tests in `apps/alphalens-research/tests/arch/`)
**Scope:** read-only diagnosis. No production code changed, nothing deleted.

**Amended 2026-10-03:** the context map was confirmed with the owner. §1.3 and
§3 are rewritten on the strength of that pass, which dissolved two of the nine
candidates and reframed two more; §3.1 records the method and marks every claim
as the owner's assertion or as a measurement. No other number in this memo
changed.

**Amended 2026-10-03 (second pass):** findings 5 and 6 are answered. The owner
settled the `data/` tier boundary — it is a shared tier, recorded as an
amendment to [ADR 0011](../adr/0011-split-pipeline-and-research.md) — and §6.2
now names each of the 12 test-only modules with the measurement that says
whether it is a kept seam or code without a consumer. The bucket totals
(12 modules / 1 676 LOC, 11 / 1 649) were recounted by a second path and
reproduce exactly. The six modules with no consumer were then removed, which
§6.2 records together with the one module the removal orphaned.

**Amended 2026-10-04:** finding 1 is answered, and the number it was stated
with was wrong. §5.1 read a **local mirror** that had stopped updating on
2026-09-05 — it said so at the time, and the caveat turned out to matter: the
live store read from the VPS on 2026-10-04 (file `2026-10-02.parquet`) carries
**178 columns, not 164**. The §5.1 table is left as measured; read "164" there
as "the mirror on 2026-09-05", and 178 as the live figure. Of 112 brief
parquets on the Mac, 31 carry at least one column the current writers no longer
produce, all dated 2026-08-18 or earlier, so a width quoted without its date
says little. The finding itself survived the correction intact: the gap was
larger than published, not smaller.

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

**Seven contexts, confirmed with the owner on 2026-10-03.** §3.1 records that
pass, including the two candidates it dissolved. This table is the answer to
"where am I?".

| Context | Lives in | Entered by | Writes |
|---|---|---|---|
| **Execution** (bracket keeper) | `alphalens_pipeline/brokers/**`, `broker_contract`, `alphalens_cli/commands/broker.py` | `alphalens broker manage` / `arm` / `auth` / `price-reader` daemons + timers | `broker_orders/` journals |
| **Thematic selection** | `thematic/**`, `experts/**`, `scorers/`, `feedback/**`, `alphalens-feedback`, Django `edge/` | `alphalens thematic {ingest,extract,map-themes,score,brief}` in the daily container; `alphalens feedback backfill-shadow-returns`; hourly edge mirror | `thematic_briefs/`, `thematic_news/`, `buffett_qual/`, `population_ladders/`, Postgres `edge_ladderoutcome` |
| **Data acquisition + PIT store** | `data/**` | Form-4 and grouped-daily timers, plus lab callers | `form4_parquet/`, `grouped_daily_history/`, `companyfacts_parquet/` |
| **EDGAR detection** | `edgar_detector/` | `alphalens edgar detect`, every 15 min | `edgar-detect/` |
| **Literature scanning** | `literature_scanner/` | weekly + monthly timers via a shell wrapper | `docs/research/literature_review/` (commits to `main`) |
| **Research lab** | `alphalens_research/**` + `scripts/` | by hand, plus 8 systemd-run scripts | `audit/`, ad-hoc |
| **Intent replay** | `apps/intent-replay` | by hand (`python -m intent_replay`) | nothing — it stamps no state |

**Two things in this repo are not contexts**, though an earlier draft of this
memo listed them as such:

- **Publication** (`alphalens-django/{briefs,market,config,auth_cf}`, `apps/web`)
  is the **face** of thematic selection, not a model of its own. Measured: the
  whole briefs app defines exactly one computed serializer field (`top_theme`,
  which picks the first theme off a list) and the `Brief` model declares no
  derived property at all. It publishes what the pipeline decided.
- **Feedback / edge** is thematic selection's **measuring arm**. Its subject is
  the candidate population — every candidate of every brief — and not the
  trades the owner actually took. Owner statement, 2026-10-03: `/edge` answers
  "does the pipeline select well?".

**One context is missing from the repo.** The outcome of the owner's real
trades lives nowhere in it; he reads it off the broker's own web interface. This
is a gap, not a mis-drawn boundary, and it carries a vocabulary trap: the word
"edge" on the dashboard means *the quality of the machine's selection*, never
*how the owner's money is doing*. Status: undecided by the owner (#1689).

**One entry point per context is a premise, not a proof.** Each context above
has exactly one way in (§1.2), and that is useful evidence about the deployment.
It is the weakest of the three signals for a *semantic* boundary: one process
can host two models, and one model can have two interfaces. §3 reports the
boundaries this evidence could not settle on its own.

One boundary is still not where its directory name suggests:

- **`alphalens_pipeline/data/` holds research-tier code that writes a
  production store.** 11 of its modules (1 649 LOC) are imported only by
  scripts, the lab and tests — never by anything the deployment runs (§6). It
  does not follow that nothing live consumes their output. Owner statement,
  2026-10-03: insider transactions feed the briefs. Verified: the nightly
  writer `apps/alphalens-research/scripts/run_form4_daily_incremental.py`
  imports `alphalens_pipeline.data.alt_data.form4_incremental`, while the live
  reader `alphalens_pipeline/thematic/sources/form4_store.py` opens
  `~/.alphalens/form4_parquet/` **by path and imports nothing from `data/`**.
  Writer and reader share a hive-partitioned layout and a column set, with no
  gate and no import edge. That is the whole content of #1679: the code may
  move to the lab, the store contract may not move with it.

The `paper.calendar` anomaly this memo reported at publication time is
**resolved**: #1687 moved the calendar, session and bar primitives to
`alphalens_pipeline/market/`, so the repo's most-shared primitive no longer
lives in a package named after a decommissioned feature. §2.4's fan-in figure
was measured before that move.

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

| View | Cycles as audited | Cycles today |
|---|---:|---:|
| Runtime, observed edges (**the number to quote**) | **4** | **3** |
| Runtime + type-only | 4 | 3 |
| Runtime, **top-level only** | **0** | **0** |
| With implicit parent-package edges | 20 | — |

Two readings matter.

**There are no import-time cycles at all.** Every cycle exists only through a
function-scope import. The lazy imports are doing real work: they are what
keeps a genuine mutual dependency from being an import-time one.

**The four real cycles as audited — three remain:**

| Cycle | Verdict |
|---|---|
| `brokers.automanager.control_loop` ↔ `brokers.automanager.live_exit_engine` | Real, and the biggest one. Both halves are in the file §4 is about. |
| `thematic.trade_setup.{builder, config_version, model}` | Real 3-cycle. |
| `broker_contract.exit_geometry.{policy, registry}` | REMOVED. `registry` held the `ExitGeometryPolicy` its own entries are built from, so `policy` imported it back at top level while `registry` imported the policies inside two function bodies. The carrier moved into `policy`; `registry` now holds only the name tables and resolvers, and imports `policy` one way. Pinned by a `RULES` row in `tests/test_module_dependencies.py`. |
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

The top entry is the finding: the repo's most-shared primitive lives under a
name that says it belongs to a decommissioned feature, and it is referenced
nearly as often as the next three put together.

Two entries in this table are research-tier (`backtest.metrics`,
`attribution.factor_analysis`) and rank high because the 133 research scripts
import them. That is expected and healthy — it is a library being used as one.
It also means this table mixes two populations, which is the reason it is not
labelled a public-API ranking.

---

## 3. Bounded contexts — the evidence, and what the owner settled

Three independent signals were measured. They agree more than they disagree,
which is the useful result. What they could not decide was decided in one pass
with the owner on 2026-10-03, recorded in §3.1.

**Import clustering** (§2) separates the contexts cleanly. **Runtime ownership**
(§1.2) gives each context exactly one entry point — useful evidence about the
deployment, and the weakest of the three for a semantic boundary (§1.3).

**Co-change coupling** — how often two contexts appear in the same commit, as a
share of the smaller one's commits, over 1 155 classified commits since
2026-04-01, measured against the nine-candidate labelling this memo started
with:

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
describes.

Both extremes of that table turned out to mean something, and neither meant
what the number alone suggested:

- **publication ↔ feedback at 34.7% is one thing, not two coupled things.**
  The owner pass dissolved both labels: feedback is thematic selection's
  measuring arm, and publication is its face. A dashboard and the measurement
  it displays are not two contexts with a suspiciously busy edge between them.
- **execution ↔ thematic not registering at all is a satisfied design goal.**
  Owner statement: the bracket keeper is deliberately client-agnostic, and the
  brief and the WhatsApp group are two of its clients. The absence is the
  property, not a gap.

### 3.1 The owner pass, 2026-10-03

A bounded context is where the domain language changes, which is a judgement
and not a graph property. The pass put that judgement where it belongs.

**Method.** The candidate table was NOT shown first. Presenting nine finished,
named candidates anchors the only domain expert available and invites him to
adjust the proposal rather than draw his own line. So the owner first narrated
one concrete end-to-end case — a real trade, from where the idea came from to
how he learned the outcome — and then one exception, with directory names,
package names and this memo's context labels withheld. The candidate cards came
second, as hypotheses to attack. Each claim below is marked **[owner]** for his
assertion or **[measured]** for a check run against the code.

**What the pass changed.** Four candidates were put under attack; three did not
survive.

| Candidate | Outcome |
|---|---|
| Feedback / edge as its own context | **Dissolved.** `/edge` answers "does the pipeline select well?" **[owner]**, and its subject is every candidate of every brief rather than the owner's own trades **[owner]**. Merged into thematic selection as its measuring arm. |
| Publication as its own context | **Dissolved.** The briefs app defines one computed serializer field and no derived model property **[measured]**, so it publishes what the pipeline decided and owns no concept of its own. |
| `data/` as research-tier | **Reframed, not dissolved.** Insider transactions feed the briefs **[owner]**; the acquisition code is imported only by scripts, the lab and tests while the live reader opens the store by path **[measured]**. Research-tier code, production store, no import edge between them. |
| Execution as a stage after selection | **Reframed.** It is a client-agnostic service whose client is the moment a human decided to trade **[owner]**, not any upstream source. |

**What the pass added that no measurement had.** Three things, each of which an
import graph and a commit history are structurally unable to see:

1. **A missing context.** The realized outcome of real trades lives nowhere in
   the repo; the owner reads it off the broker **[owner]**. §1.3 and #1689.
2. **A rail that belongs to the machine, not to the portfolio.** Arming was
   once refused for want of queue slots, and the owner raised the limit thinking
   "the machine is being over-cautious, I know how much of this I want"
   **[owner]**. So the keeper does not own the rule "how many positions do I
   hold at once" — that rule lives with the owner, outside the software.
3. **A number whose author is unknown.** In the trade walked through, the owner
   could not say whether the stop loss came from the group's message or was
   chosen while the document was written **[owner]**, and the document records
   no difference. #1690.

**What the pass did not settle.** Whether the dashboard can show anything the
pipeline does not compute was asked of the owner and should not have been — it
is a fact about the code, and it was settled by measurement instead (one
computed field, no derived properties). The realized-performance question
(#1689) is genuinely open and is recorded as undecided rather than answered.

### 3.2 Relationship map

Seven names are an index. The map is the edges between them: who produces what,
what fact crosses, who owns its shape, and what is assumed. Every edge below is
out-of-band — a store, not a function call — which is why §2 cannot see any of
them.

| Edge | Fact that crosses | Shape owner | Gate | Open assumption |
|---|---|---|---|---|
| human decision → **execution** | a whole trade: levels, size, exits | `broker_contract` schema + the `arm` door | yes — schema, codec, fixed point, key rules | which numbers the author chose is not recorded (#1690) |
| **data** → **thematic** (`form4_parquet/`) | insider transactions, hive-partitioned by `transaction_year` | nobody; the writer's layout is the contract | none | writer and reader agree on the column set by convention only |
| **data** → **thematic** (`grouped_daily_history/`) | split-adjusted whole-market closes | the backfill script | none | must stay `adjusted=true`; the monitor's raw-close cache must not be merged into it |
| **thematic** stages → each other, and → the lab (`thematic_briefs/`) | the brief: 164 columns | the writing stage | **none for 77 of 164 columns** (§5.1) | a renamed or dropped column breaks readers silently |
| **thematic** → its own face (Postgres `briefs`, `days_meta`) | the published brief, 87 shared columns | Django model + OpenAPI schema | yes — `test_schema_parity.py`, `test_openapi_parity.py` | the gated boundary is not the one where the coupling lives |
| **thematic** measuring arm → its face (`population_ladders/` → `edge_ladderoutcome`) | per-candidate ladder outcomes | the feedback writer | mtime-gated rebuild, no schema gate | `/edge` means selection quality, never the owner's P&L |
| **execution** → *nothing* | realized outcome of a real trade | — | — | the edge does not exist; the owner reads the broker (#1689) |
| all of the above → **market primitives** | what a trading session is | `alphalens_pipeline/market/` | the dependency gate forbids the reverse direction | a "generic" calendar must not quietly decide broker or eligibility policy |

The last row is the one to watch. A shared primitive with the repo's highest
inbound reference count is a generic capability only as long as it stays
generic; the moment it decides something a context owns, every consumer
inherits that decision through an API that looks neutral.

**Parked split blueprint.** `docs/research/bracket_keeper_repo_split_stage1_design_2026_08_02.md`
was written at `7d6783f9`, two months and several hundred commits ago. Its
central claim still holds: the execution context's only residual upward edge of
substance was the shared calendar, which #1687 has since moved into
`alphalens_pipeline/market/`. The blueprint's edge table should be re-verified
line by line before it is executed, but it has not rotted — and the owner pass
independently confirmed its premise, that the keeper serves clients rather than
sitting downstream of selection.

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

**Amendment 2026-10-04 — the live figure, and a sharper version of the
finding.** Re-read from the VPS source of truth (`2026-10-02.parquet`, read
2026-10-04): **178 columns**. The table above is the 2026-09-05 mirror and is
left as measured.

The gap was also understated in a second way. The pipeline → API boundary is
**not** gated against a parquet rename either: `briefs/ingest/parquet.py`
requires exactly two of the 178 columns, `ticker` and `theme`, and reads every
other one with `row.get(col) if col in row.index else None`, so a renamed
column silently becomes the model field's default. The `test_schema_parity.py`
and `test_openapi_parity.py` gates protect the model and the published schema,
not the parquet. The `test_expert_columns_match_frozen_*_tuple` guards compare
two **Django-side** copies of a column list, because `alphalens-django` does
not depend on `alphalens-pipeline` and so cannot import the pipeline's tuples.

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

**Answered 2026-10-03** (finding 6). The 12 modules are named below, each with
the measurement that settles which of the two it is — a seam kept on purpose,
or code whose consumer never arrived. Every module has exactly ONE test module
and nothing else; the discriminator is therefore not "who imports it" but
"does the thing it was written for exist".

The remedy named in finding 6 — "extend the `UNWIRED_ALLOWED` pattern" — does
not apply to nine of the twelve, and that is itself worth recording.
`deadcode_broker.SCOPE_PREFIXES` covers `broker_contract/`, `brokers/`,
`paper/` and `commands/broker.py` only, so an entry for a `data/` or
`thematic/` module would be configuration for a file the tool never reports.
Widening that scope repo-wide is a separate change to a pinned tool, not a
cheap fix.

**Kept on purpose — 6 modules, 1 109 LOC.**

| Module | LOC | Why it has no importer | What retires the entry |
|---|---:|---|---|
| `brokers/automanager/service.py` | 360 | the client-manager boundary; the acceptance suite drives the real loop through it | already in `UNWIRED_ALLOWED` |
| `brokers/automanager/fill_source.py` | 110 | the seam the streaming `FillSource` plugs into; owner decision 2026-09-17 (#1484) | already in `UNWIRED_ALLOWED` |
| `brokers/automanager/yfinance_price_feed.py` | 94 | interim and fallback price feed; same owner decision (#1484) | already in `UNWIRED_ALLOWED` |
| `data/alt_data/ticker_cik_refresher.py` | 50 | regenerates the checked-in `ticker_cik_map.yaml` from SEC's master table; run by hand when the map goes stale, so an importer would be the defect | a timer generating the map instead |
| `data/universes/ishares_refresher.py` | 107 | the same shape for iShares holdings as PIT universe snapshots (IJH / IJR / IVV) | as above |
| `feedback/broker_fills.py` | 388 | loader and contract validator for the `broker-fills-v1` export. Its docstring states the restraint: loading and validation only, because the selection A/B over that data is pre-registered as Cluster #22 and has not run | Cluster #22 runs, or is withdrawn |

**No consumer, measured — 6 modules, 567 LOC.**

| Module | LOC | The measurement |
|---|---:|---|
| `data/alt_data/form4_filter.py` | 26 | filters Form-4 rows to Layer-2d-eligible purchases. The live insider path performs that filter itself: `thematic/screening/insider_signal.py:143` drops every row whose `transaction_code != "P"`. A second implementation with no caller |
| `data/alt_data/plan_10b5_1.py` | 109 | parses 10b5-1 adoption dates out of Form-4 footnotes for the same closed design. The string `10b5` appears in exactly two files in the repository: this one and the row above |
| `data/spread.py` | 125 | two published daily-OHLC spread estimators. Its docstring names `RealisticCostModel.primary_one_way_bps` as the consumer of its output; that model does not import it, and neither does anything else in the pipeline, the lab or `scripts/` |
| `data/fundamentals/cache.py` | 71 | disk TTL cache for Alpha Vantage fundamentals features. The prescreener keeps its own in-memory `_fundamentals_cache` instead; the only other mention in the tree is one docstring line in `alphavantage_client.py` |
| `data/store/fundamentals_pit.py` | 112 | point-in-time fundamentals store for backtest replay. Named in `data/store/__init__.py`'s package docstring, imported by nothing |
| `thematic/sources/edgar_adapter.py` | 124 | 8-K adapter for the thematic ingest. `thematic/news_ingest.py` imports `edgar_press_release` instead, and `edgar_adapter` has no reference anywhere outside its own test |

**The second table was acted on, 2026-10-03.** The owner decided to remove all
six, with their tests, on the same grounds as finding 9 (`data.macro.scorer`).
Measured after the removal with the same harness: 646 -> 640 modules, and the
test-only bucket 12 -> 7 modules / 1 320 LOC.

One consequence is worth recording because it was not visible before the
removal. `data/store/fundamentals_pit.py` was the only production importer of
`data/fundamentals/fetcher.py` (211 LOC, the Alpha Vantage fundamentals
fetcher), so that module has moved INTO the test-only bucket. It is the same
question again, one level up, and it was left open rather than folded into the
removal; `fetcher.py` now carries that fact in its own docstring.

The same check on `fetcher.py` makes the §2 caveat about `__init__` re-exports
concrete. Its docstring names `data/fundamentals/gate.py` as the consumer of
`extract_features`' output, and that is a DICT-SHAPE agreement, not an import:
`gate` does not import `fetcher`, and nothing calls `fundamental_gate_score`
outside its own test. `gate` nevertheless measures as reachable, because
`data/fundamentals/__init__.py` re-exports it — reachable through a facade, with
no caller. Fan-in is static inbound references, never evidence of use.

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
| 1 | 77 of 164 brief-store columns cross no schema gate; the gated boundary (pipeline → API) is not where most columns live | §5.1 | a renamed column breaks readers silently; this is the most likely source of a quiet production defect | ANSWERED (#1676, shipped #1704) — but **not** in the shape proposed here. A frozen expected set mirroring `test_schema_parity.py` was rejected on measurement: 132 of the then-178 columns were **already** declared in code beside the stage that writes them, so a frozen list would have been a second home for the truth, detached from the code, frozen at a figure already 14 columns stale. What shipped declares the 46 undeclared names beside their writers and **assembles** `BRIEF_STORE_COLUMNS` from the thirteen per-stage declarations. Each declaration is read by its own writer to CHECK the frame and deliberately never imposed on it — a projection through the tuple re-invents a dropped column as all-null under the declared name, which both hides the defect in the data and blinds the gate (measured). Power is bounded and published: of 178 names the gate has rename power over 114, 42 have the declaration AS the writer (the tuple is iterated to stamp the frame, so nothing can diverge) and 22 are declared but read by no writer. Still open: the Django half cannot be closed from the pipeline side, and whether a mismatch should be fatal rather than logged is #1705 |
| 2 | `control_loop.py` is 11 130 lines with no internal boundary, on the live order path | §4.1 | every change needs the whole file in head; 130 commits in 6 months | IN PROGRESS (#1677) — the partition map is measured by exclusive reachability from the 11 tick stages and the 10 wiring roots, with **zero functions shared between wiring roots**; step 1 moved the stream rail out (609 lines, 11 130 → 10 521) and pinned the direction one-way. The order was forced: the helpers the big clusters still need are a journal access layer, so it came out first — step 2 the path, parsers, folds and append (331 lines, → 10 154), step 3 the line writers, the remaining folds and the journal-bound counters (803 lines, → 9 313, measured as the maximal set of journal functions closed under calls). Step 2 also cut the biggest cycle as a side effect. What is now unblocked, re-measured after step 3: `_make_place_pick` (2 629 LOC in 61 functions) and `_run_entry_watch_pass` (1 440 LOC in 36) |
| 3 | No gate in CI measures file or function size | §4.2 | the next 11 000-line file grows the same way | enable `PLR0915`/`PLR0912` with a baseline, or a file-length check |
| 4 | `paper.calendar` has the repo's highest inbound count (41) under a name that says it belongs to the feature ADR 0012 decommissioned (the package itself is `ACTIVE` — see §1.3 correction) | §2.4 | misleads every reader; blocks the keeper split | DONE — moved to `alphalens_pipeline.market.calendar`; the module keeps its symbol names |
| 5 | `alphalens_pipeline/data/` is substantially research-tier (11 modules, 1 649 LOC referenced only by lab/scripts/tests) | §6.1 | ADR 0011's framing is wrong for `data/`, so new code lands on the wrong side | DONE — owner decision 2026-10-03: `data/` is a SHARED tier serving the live services and the lab, recorded as an amendment to [ADR 0011](../adr/0011-split-pipeline-and-research.md). A move was rejected on two measured grounds: it would split one vendor's surface across two apps, and the consumer set moves whenever a study starts or ends |
| 6 | 12 live-tier modules (1 676 LOC) are referenced only by tests | §6.2 | speculative generality indistinguishable from a kept seam | ANSWERED — §6.2 now names all 12: **6 are kept on purpose** with the observation that retires each, **6 have no consumer** (567 LOC) and are a deletion question. The remedy as originally written does not work for nine of them: `deadcode_broker` is broker-scoped, so an entry there would guard a file it never reports |
| 7 | `_reset_remote_quote_source_for_tests` is a test hook in production code on the money path | §4.1 | small, but it is a live file | fold into the injected deps |
| 8 | 4 real import cycles, all of them existing only via lazy imports | §2.2 | hidden mutual dependencies that no import-time check sees | PARTLY DONE — `exit_geometry.{policy,registry}` cut (the numeric carrier moved out of the registry; direction pinned by a `RULES` row), leaving 3. The other three are untouched; `control_loop` ↔ `live_exit_engine` belongs to finding 2's arc |
| 9 | `data.macro.scorer` (83 LOC) is referenced by nothing at all | §6.1 | dead weight | delete, after one check |
| 10 | `CLAUDE.md` says 12 ADRs; `docs/adr/` holds 17 | §9 | the index nobody trusts gets trusted less | doc-only PR |
| 11 | Root `pyproject.toml` carries `per-file-ignores` for `apps/alphalens-pipeline/tests/*`, which does not exist | §9 | harmless, but it is a rule guarding nothing | doc/config PR |
| 12 | `just lint` omits `alphalens-broker-contract` and `alphalens-feedback`, which CI does lint | §9 | local lint is greener than CI | one-line `justfile` fix |

**Issues filed:** only where the owner must decide — findings 1, 2, 4, 5, the
§7 test-app question, and the two decisions the owner pass turned up (#1689 the
missing realized-outcome context, #1690 a pick that does not record which of
its numbers the author chose). The rest are rows here, deliberately: the board
grew +38 net in 8 weeks and the rule that followed was to fix rather than file.

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

**A fifth correction, and the only one that is not arithmetic.** The owner pass
(§3.1) overturned three of this memo's readings. None of them was a wrong
number; each was a correct number read as something it did not say.

| As published | What it actually is | Why no measurement could have caught it |
|---|---|---|
| "Nine contexts, each with one owner and one way in" | Seven. One entry point per context is evidence about the deployment, not about the domain language | A runtime boundary and a language boundary are different objects; the harness only sees the first |
| The 34.7% publication ↔ feedback pair is "a three-layer data contract" between two contexts | One thing — selection's measuring arm and the face that displays it | Co-change cannot distinguish "two contexts that change together" from "one context split across two directories" |
| "`alphalens_pipeline/data/` is substantially research-tier, not live infrastructure" | The acquisition code is research-tier; the store it writes is a production input to the briefs | The join between them is a filesystem path, so the import graph correctly reported no edge — and the absence of an edge was read as the absence of a consumer |

The method is what produced these. The candidate table was deliberately held
back until after the owner had narrated a real case in his own words; three of
the four candidates then attacked did not survive. Had the table been shown
first, the likely outcome was agreement with it.

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
- **The context map is now confirmed** (§3.1, 2026-10-03) — but one question
  inside it is open rather than answered: whether the realized outcome of real
  trades should live in this repo at all (#1689). The map records the gap and
  not a plan.
