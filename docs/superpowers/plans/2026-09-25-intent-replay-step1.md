# intent-replay Step 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Only PR 1 is planned in detail; each later PR gets its own plan when reached.

**Goal:** Build a research backtester that takes a `TradeIntent` document — the same JSON `alphalens broker arm` accepts — plus price bars, and reports what the document would have done. It stamps nothing, charges no multiplicity budget, and carries no accrued history.

**Architecture:** A new dependency-light leaf `apps/intent-replay/`, split into an ENGINE half (`bars`, `config`, `interpreter`, `walk`, `trace`, `measures`, `envelope` — stdlib plus `broker_contract` only) and an ADAPTER half (`door`, `cli` — may additionally use `jsonschema`). The stop decision is COPIED into `broker_contract/stop_decision.py`, deliberately leaving two implementations standing, held together by a parity property test until step 2 retires one.

**Tech Stack:** Python 3.13, `unittest` (NOT pytest), Hypothesis for the parity property, `jsonschema` for gate 1, `broker_contract` dependency-free leaf. No pandas, no calendars, no vendor clients.

**Spec:** `docs/superpowers/specs/2026-09-23-intent-replay-design.md`, LOCKED 2026-09-25 (PR #1566). The spec is the authority; where this plan and the spec disagree, the spec wins and this plan is wrong.

**Status:** revision 2. Revision 1 was adversarially reviewed on 2026-09-25 (4 dimensions, 8 agents); it had 4 blocking defects, each re-verified here by running it. Three turned out to be defects in the SPEC and were fixed on PR #1566: the JSON Schema gate was unimplementable under a per-package dependency rule, `intent_malformed` is a CLI-owned code a leaf cannot name, and none of the three published examples decodes.

**Baseline:** `origin/main` `995bba5d` plus PR #1566.

---

## Global Constraints

- **Tests are `unittest.TestCase` subclasses.** pytest-style bare functions are silently skipped by this repo's CI discovery. Never write them.
- **A new test directory needs `__init__.py`.** Without it `unittest discover` collects ZERO tests and exits 0 — the PR looks green and tests nothing. Every existing test subdirectory in that tree is a package.
- **TDD, always.** Red → green → refactor, even for two-line changes. Write the failing test, run it, see it fail for the stated reason.
- **The engine half imports nothing third-party.** Not pandas, not `jsonschema`, not `alphalens_pipeline`, not `alphalens_research`. The AST gate is the ONLY barrier enforcing this — `dependencies = []` is `broker_contract`'s barrier, not this package's — so every rule it carries needs a positive control that fails when the rule is deleted.
- **Every value the replay needs is read from the document or STATED in the run configuration** (spec §2.1). Nothing is inherited from a deployment, an environment variable or a production constant. A missing required value is a refusal (`config_incomplete`), never a default.
- **The replay refuses what the door refuses, and adds one gate of its own.** An unclassified input path is `path_unclassified`, never an approximation.
- **English only** in code, comments, docstrings and identifiers. Math notation is fine.
- **No backward compatibility.** Solo project, zero external users. Rename and delete in the same commit.
- **Conventional Commits** (`type(scope): description`), DCO `-s`, author `kamilpajak@users.noreply.github.com`.
- **The live daemon is not touched by step 1.** Adding an uncalled module to a package the daemon imports executes nothing. Step 2 edits `position_manager` and is a separate plan.
- **Test command** (from repo root): `.venv/bin/python -m unittest discover -s apps/alphalens-research/tests -t apps/alphalens-research -k <pattern> -v`

---

## Scope

Step 1 of spec §7 only: the contract copy, the replay, the CLI. Out of scope: `/edge` wiring, the lens registry, epic #1526 (spec §9 makes them disjoint), and step 2.

**No effort estimate.** Spec §8 says effort is unestimated and no line of the interpreter exists. The PR count is a decomposition, not a schedule.

---

## Nine PRs, with one owner per refusal code

Ordered so a runnable command exists at PR 4. The code column exists because revision 1 claimed PR 4 made "every refusal code reachable", which was false — PR 4 owns the two DOCUMENT codes (`intent_invalid`, owned by the contract, and `intent_malformed`, owned by this tool's CLI: the leaf never names it, §5.4), plus the two invocation codes it added (`config_malformed`, `usage`). It owns none of the engine's own codes.

| # | what it lands | refusal codes it owns |
|---|---|---|
| 1 | package, `bars.py`, the AST gate + its new rule kind, six repo-wide gate configs | `bars_unordered`, `bars_empty`, `bars_invalid`, `window_too_short` |
| 2 | `stop_decision` COPIED into the contract + the parity property test | — |
| 3 | `config.py` + the §4.3.1 classification bookkeeping | `config_incomplete`, `config_invalid`, `path_unclassified` |
| 4 | `door.py`: template completion, the four door gates, a CLI that decodes and refuses | `intent_invalid`, `intent_malformed`, `config_malformed`, `usage` |
| 5 | interpreter: pullback rungs → pending orders | `entry_mode_unsupported` |
| 6 | bar walk + tie convention + `ambiguous_bars` + trace | — |
| 7 | measures, envelope, `divergences`, both output formats | — |
| 8 | the native entry-trail model | — |
| 9 | §6.2 golden cases, §6.3 walk properties, §6.4 door agreement | — |

Two notes on that column:

- **`window_too_short` fires when the bars do not cover `walk_start`** — decided 2026-09-25 and now in spec §5.4, with two `details.reason` values: `ends_before_walk_start` and `begins_after_walk_start`. Revision 2 of this plan found the code published with no trigger and proposed only the first reason; the spec adds the second because a walk that silently starts late reports rungs as unfilled over a stretch it never saw. The check takes `walk_start` as an argument, so it belongs to PR 1 even though the configuration that supplies the value arrives in PR 3.
- **`entry_mode_unsupported` is not a door gate.** The door ACCEPTS `immediate` (`broker_contract/trade_intent/validate.py:229`), so the code belongs to the interpreter.

---

## PR 1 — the package, the bars contract, and the one barrier

### Files

| path | what |
|---|---|
| `apps/intent-replay/pyproject.toml` | new member; `dependencies = ["alphalens-broker-contract", "jsonschema>=4.0.0"]` |
| `apps/intent-replay/intent_replay/__init__.py` | empty. **No `__status__`** — see step 2 below |
| `apps/intent-replay/intent_replay/bars.py` | the `Bar` value type and the bar-sequence checks |
| `pyproject.toml` (root) | `[tool.uv.workspace] members` plus the member comment block |
| `uv.lock` | regenerate with `uv lock` **in the same commit** |
| `pyrightconfig.json` | `include` **and all three `executionEnvironments[].extraPaths` blocks** |
| `.github/workflows/ci.yml` | the `ruff check`, `ruff format --check` and `bandit -r` path lists |
| `sonar-project.properties` | `sonar.sources`, as `apps/intent-replay/intent_replay` |
| `apps/alphalens-research/tests/test_module_dependencies.py` | `PACKAGE_DIRS`, the new rules, **and a new rule kind** |
| `apps/alphalens-research/tests/intent_replay/__init__.py` | **required**, see Global Constraints |
| `apps/alphalens-research/tests/intent_replay/test_bars.py` | the bars contract tests |

### Steps

- [ ] **1. Declare the member, and join all six repo-wide gates.** `test_ci_gate_workspace_parity.py` derives its expectation from `[tool.uv.workspace] members` and asserts every member appears in pyright's `include`, in three `ci.yml` lint steps, and in `sonar.sources`. Running its own helper against a proposed `apps/intent-replay` reports it missing from all five, in three test methods. `uv lock --check` is a separate blocking CI step that runs before any lint or test; the last member added (#957) relocked in the same commit. Verify with `uv lock --check` and `python -m unittest tests.test_ci_gate_workspace_parity -v`.
- [ ] **2. Create the package with no `__status__`.** `test_layer_status.py` discovers only under `LAYER_ROOTS`, all of which are inside `alphalens_research`, so a package at `apps/intent-replay/` is never visited. Neither `broker_contract` nor `alphalens_feedback` carries the marker. Adding one would be a convention nothing checks.
- [ ] **3. Confirm the package is importable from a worktree.** `uv sync` **from the worktree**, then `python -c "import intent_replay"`. This repo's rule is that a worktree needs its own `uv sync`; a member that imports fine in the checkout that created it can still fail in CI.
- [x] **4. Write the bars red phase** (ten tests, below), run it, see each fail for its stated reason.
- [ ] **5. Implement `bars.py`** until green.
- [ ] **6. Add the direction rules and the new rule KIND** to the shared AST walker (below).
- [ ] **7. Write the AST-gate positive controls** in the neighbouring style, permanently in the tree (below).
- [ ] **8. Check the two gates spec §6.5 names for the first PR:** coverage ≥ 80% on changed lines, Sonar S3776 ≤ 15.
- [ ] **9. Assert a non-zero test count**, not just a zero exit, from `unittest discover` over the new directory.
- [ ] **10. Zen pre-merge review with `deepseek/deepseek-v4-pro`, `review_validation_type: external`.** PR 1 touches six repo-wide config files plus an AST walker twenty-odd other rules share, so it is a shared-surface PR. Revision 1 asserted PR 2 was the only one; that was wrong.

### Red phase (step 4) — ten tests

1. `test_bar_sequence_refuses_unordered` — `t` not strictly increasing raises `bars_unordered`.
2. `test_bar_sequence_refuses_duplicate_timestamps` — equal `t` refuses. A different input reaching the same rule; spec §4.5 names both.
3. `test_bar_sequence_refuses_empty` — `bars_empty`.
4. `test_an_unsorted_sequence_is_not_silently_sorted` — hands a DESCENDING sequence and asserts the refusal rather than a sorted result. Revision 1 had this as a fourth test that could not produce an observation test 1 would miss; the difference is that this one asserts the absence of a REPAIR. The existing `/edge` replay sorts silently, and spec §4.5 deliberately deviates.
5. `test_bar_rejects_non_finite_prices` — NaN or infinite OHLC refuses at construction. NaN survives `json.loads` and every comparison in this project, so the value type is where it stops.
6. `test_window_too_short_when_bars_end_before_walk_start` — the last bar precedes the given `walk_start`; refuses with `details.reason == "ends_before_walk_start"`.
7. `test_window_too_short_when_bars_begin_after_walk_start` — the first bar follows it; refuses with `details.reason == "begins_after_walk_start"`. This is the case a naive implementation passes: it has bars, so it walks them. The test asserts the refusal, not a walk that starts late.
8. `test_window_covering_walk_start_is_accepted` — the positive control: a sequence whose first bar is at or before `walk_start` and whose last bar is after it passes the check. Without it the two refusals above could be satisfied by a check that refuses everything.
9. `test_window_check_refuses_empty` — the window check refuses an empty sequence with `bars_empty` rather than raising `IndexError`; added when the first implementation showed the plan's "empty goes first" ordering was a sentence nothing pinned.
10. `test_an_ordered_sequence_is_returned_unchanged` — the positive control for the ordering check. Test 5 now refuses with `bars_invalid`, a ninth code added to spec §5.4 on 2026-09-25; the plan's earlier list had no code for a non-finite price.

### The direction rules, and the walker change they need (step 6)

Spec §3.1 has six rows and one does not fit the existing machinery. The walker matches on a single `forbidden_prefix` string per rule, and `test_rules` raises `KeyError` on a rule without it. "Anything third-party except `jsonschema`" is an ALLOWLIST, which that schema cannot express.

So add a second rule kind — `allowed_prefixes` — and have `test_rules` dispatch on which key a rule carries. It needs `sys.stdlib_module_names` as the stdlib source plus a relative-import discriminator. The alternative, enumerating known-bad prefixes (`pandas`, `numpy`, `alphalens_research`), is the shape that cannot catch the NEXT import, and the engine's purity is the whole placement argument of spec §3.

### The AST-gate positive controls (step 7)

Revision 1 proposed adding a bad import to the tree and removing it before commit. That is a manual demonstration dressed as a test: the red came from a fixture no reviewer can see, and once removed nothing in the tree can produce the flagging observation again. The file being edited already answers this **ten times** — `grep -c positive_control` returns 10, each writing a synthetic module into a `TemporaryDirectory` and keeping it forever; `test_automanager_saxo_boundary_positive_control` also pins the negative arm.

In that style, permanently:

- [ ] the new rules exist in `RULES` and resolve to a non-empty file list (mirroring `test_a_rule_that_resolves_to_nothing_is_an_error`);
- [ ] a synthetic module with `from alphalens_pipeline.data.factors import x` inside a `def` body is flagged — the lazy-import form a dependency list cannot see;
- [ ] a synthetic ENGINE module importing `pandas` is flagged; one importing `math` and `broker_contract.trade_intent.codec` is not;
- [ ] a synthetic ADAPTER module importing `jsonschema` is NOT flagged, and the same import in an engine module IS. This is the per-module split and the one rule with no precedent in the file.

### What could still go wrong

- **`pyrightconfig.json` needs `include` AND three `extraPaths` blocks**, though the parity test only checks `include`. The file's own header says so. A change satisfying the test but not the header passes CI and breaks editor type resolution.
- **A new package can drag the diff-coverage gate.** The root pyproject deliberately has no `[tool.coverage]` section, so measurement is unscoped and a new first-party package is measured automatically. A package that is mostly type definitions has few executable lines and every one shows up in the diff.

---

## PR 2 — the note that makes it risky

`stop_decision` is COPIED into the contract, not extracted. Spec §7 is explicit: step 1 leaves two implementations standing, and calling it an extraction makes the safety argument sound stronger than it is. The parity test holds them together until step 2 retires one.

The minimal view is spec §3.2's nine fields. **If the copy needs a tenth — an order leg, a journal handle, a calendar — the boundary is wrong and the work STOPS** rather than widening the contract. That is a stop condition, not a discussion point. The ninth field, `already_reanchored`, was found by the PR 2 plan review on 2026-09-25: the spec's table had been read off both daemon arms but omitted the re-anchor arm's idempotence latch (`reanchored_by_uic`). It crosses as the latch predicate's RESULT, a bool, the same shape as the two order-state booleans below; the owner decided that on #1573.

Three of those nine fields are not policy: `has_sole_standalone_stop` and `amend_in_backoff` ask about resting broker orders and a refused amend, and a replay has neither. The replay passes the values meaning "no broker obstacle" and reports the resulting optimism as the `daemon_trail_guards` divergence (spec §3.2).

---

## Later-PR prerequisites nobody would think to look for

Named here so they are not discovered as red builds.

- **`FIRST_PARTY_PREFIXES`** in `tests/test_pipeline_runtime_deps_declared.py` lists the five first-party import names. `intent_replay` joins it in whichever PR first has `alphalens_pipeline` import it; without that the test treats `intent_replay` as a PyPI distribution that must be declared.
- **PR 4 must COMPLETE a template before decoding.** None of the three published examples decodes: their `meta` carries only `source`, so the codec fails on missing `armed_ts` and `trade_date`. `intent_id` and `meta.armed_ts` are out of scope and take a sentinel; `meta.trade_date` is translated and cannot be invented. Spec §6.4 carries the table. The round-trip gate of §4.3 step 3 runs on the COMPLETED document, not the author's.
- **PR 9's §6.4 asserts two different things**: the two pullback examples are accepted, and `immediate-plus-pullback.json` is refused with exactly `entry_mode_unsupported` naming tier 0 in `details.tiers`. Do not fix that red by dropping the file or softening the code — either quietly undoes a spec §8.1 decision.
- **PR 7 owns both output formats.** Revision 1 dropped `ndjson` and the multi-document stream, which spec §0 and §5.3 both require.
- **PR 7 owns `divergences`**, including `daemon_trail_guards`, which revision 1 named nowhere.
- **PR 3 landed three codes, not two.** `config_invalid` (a stated value nothing can use) was split from `config_incomplete` (a value not stated) on the `bars_invalid` argument, and spec §5.4 carries both. PR 3 also decided, with the owner: a null `entry_deadline` is refused, `oco: true` is refused in v1, and `spec.entry_tiers[].tag`, `spec.tp_tranches[].tag` and `account_id` are out of scope. The FX gap (the costs block carries no rate, and the replay cannot detect a cross-currency run) is recorded on #1576/#1577/#1578 and decided in its own issue; until then no later PR prices a cross-currency run with a constant or an implicit 1:1.
- **PR 5 must produce the gate's `read` set from a RECORDING accessor, never from a hand-typed list.** `intent_replay.classification.check_classified(document, read)` subtracts the listed classes and `read`; the interpreter is the only source of `read`, and a list typed by hand is exactly the trusted list the gate exists to refute. The paths the interpreter must prove by reading are the `interpreted` rows of the three §4.3.1 tables minus the two in `REFUSED_BY_DOOR`; `tests/intent_replay/test_classification.py` reads them off the spec.
- **PR 4 loads the configuration file with a duplicate-key refusal BEFORE `RunConfig.from_jsonable`** (`json.loads` keeps the last of a repeated key; the arming door already refuses `duplicate_key` with an `object_pairs_hook`), and completes a template with the three fields of spec §6.4 only — it does not fill tags, since the gate's universe is the completed wire document and a filled tag is a path the author did not write.
- **PR 4's success path is deliberately incomplete.** A valid document before the envelope exists has nothing to print, so PR 4 prints the refusal shape only and exits 0 with empty `stdout` on an accepted document. Say so in its PR body rather than leave a reviewer to find it.
- **PR 4 landed, and what it hands on.** `door.admit(document)` returns `Admitted(intent, document)`; `document` is the COMPLETED wire document and is the classification gate's universe for PR 5, never the author's. `meta.trade_date` is stated in the document (spec §6.4), so every test that pushes a template through the door adds one. `test_door.py` asserts that the door ADMITS `immediate-plus-pullback`; PR 5 turns that assertion into `entry_mode_unsupported` naming tier 0. `discarded_paths` and `supplied_derived_paths` now live in `broker_contract.trade_intent.codec` (second use); the arming door imports them.
- **PR 6 adds `--bars` as an `Option` on the `run` `Command` in `intent_replay.cli.COMMANDS`.** Help, the `schema` manifest and the manifest-vs-parser test grow from that one tuple; the bar file's shape is PR 6's decision (`bars.py` has no jsonable form). The `test_module_dependencies.py` walker now resolves a level-0 `from X import y` to `X.y` when `X/y.py` exists, so `from intent_replay import door` is seen by the forbid rules.
- **PR 7's envelope `intent_id` echoes the PR 4 sentinel `REPLAY`** (spec §4.3.1: the envelope may echo, nothing reads; the §5 example says so). Per-line identity in `ndjson` is `sequence`, never `intent_id`: two variants of one pick would collide on the door's `TICKER:DATE:manual`. The stream's input shape (a configuration per document) is PR 7's decision; `run` takes one document until then, and `--format ndjson` is accepted but indistinguishable from `json`.

---

## Issues and the board

Genuinely multi-PR, so an epic with sub-issues rather than a bullet list in a comment. Revision 1 named no issues at all.

- [ ] one epic, `Build intent-replay (step 1)`, linking the spec and PR #1566;
- [ ] nine sub-issues, one per PR, each naming the refusal codes it owns;
- [ ] **a tenth issue for step 2**, which revision 1 left as prose. It needs a retirement PREDICATE, not a date: the parity test is deleted when the daemon calls the leaf. Until then two implementations of the stop decision stand, and every future change to stop behaviour must be made twice or the parity test goes red. The issue says that rather than leaving step 2 as an intention.
- [ ] board status set on creation; every transition mirrored.

---

## Risks

From the spec, unchanged:

- **The entry-trail model is of a VENDOR's order type.** Once the trailing order rests, the broker owns the ratchet and the fire (`entry_trail_watcher.py:146-148`), so the parity test cannot reach PR 8 at all and the model rests on documented behaviour plus one live probe — the weakest evidence anywhere in the design.
- **The extraction boundary is a claim.** See PR 2.
- **This tool cannot validate a policy.** It replays documents over past bars and is in-sample by construction. It answers "what would this have done", never "does this work". No output of it is evidence of an edge.

From the plan:

- **Nine PRs, no numbers until PR 7.** If the chain proves too long, the first compression to consider is merging PRs 5 and 6.
- **PR 1 is bigger than revision 1 thought** — six config files plus a shared walker change. It is still the right first PR, because each is a prerequisite for the second, but it is not the small warm-up it was described as.

---

## Verification that has power to refute

1. **The schema-coverage check, promoted to a test in PR 3.** Enumerate `required` recursively through `$defs` in `trade-intent-input-v3.schema.json`; assert every path is in the spec §4.3.1 tables or read by the interpreter. It has already fired twice: once on six unclassified paths, and the corrected walk is what produced the completeness table.
2. **Every AST-gate rule has a positive control that fails when the rule is deleted.** Not "a fixture existed once".
3. **A non-zero test count, not a zero exit code.** A directory without `__init__.py` exits 0 having run nothing; only asserting the count can refute that.

What none of them can do: none checks that a class assigned is the RIGHT class. That becomes checkable at PR 5, when the runtime gate can disagree with the published table.
