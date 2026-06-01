# Integration / E2E test strategy for AlphaLens

**Status:** IN PROGRESS — Phases 1a (#361/#362), 1b (#355), 2 (#364), 3 (#366, L3 brief-stage golden replay + VCR cassette infra) SHIPPED. Phase 3b underway, one stage per PR: **3b-1 extract (#368)** + **3b-2 map-themes (#369, generic `VendorCassette`)** + **3b-3 ingest (this PR, partial — GDELT synthetic + Polygon/RSS live; EDGAR + full-GDELT-breadth deferred on vendor constraints)** SHIPPED; 3b-4 score still open. Phase 2b + 4 + 5 still open.
**Date:** 2026-06-01
**Author:** research session (workflow-assisted: 4 understanding agents + 3 design philosophies synthesised; adversarially reviewed by zen `deepseek-v4-pro` + Perplexity — see §10)

---

## 1. Thesis — why 3000+ green unit tests still ship prod bugs

The unit suite passes because it asserts our assumptions against our own mocks:
fabricated vendor fixtures, mocked LLM clients, config passed as an argument,
migrations on a fresh DB, code run from the source tree under the dev venv.

**Every production escape we have catalogued lives in the gap between two things
that drift apart independently and that no single unit test imports together** —
a writer parquet vs its reader, `/etc/alphalens/env` vs `.env.example`, the source
tree vs the slim Docker image, our EX-99.1 fixture vs SEC's real bytes,
`exit 0` vs rows-written.

The fix is **not more unit tests**. It is a thin pyramid of new layers *above* the
unit suite, each pinned to a specific, empirically-observed failure class — and
written so that reading the suite teaches how AlphaLens actually works.

---

## 2. The empirical escape catalogue

Thirteen production bugs that **all passed the unit suite** and broke in prod
(from project memory + git log + postmortems). They are the design input — every
test layer below traces back to one or more of these.

| # | Incident | Subsystem | Boundary | Failure class |
|---|----------|-----------|----------|---------------|
| 1 | `git diff --quiet` before `git add` drops untracked first-of-period files (#345) | literature publish wrapper | FS → repo commit gate | silent-success-noop |
| 2 | EX-99.1 parsed from `FilingSummary.xml` doctype that SEC never emits → 0 rows in prod, fixture was hand-fabricated (#332→#338) | EDGAR press-release ingest | vendor response shape | real-data-shape |
| 3 | Retired Gemini model → 404 per theme → empty brief, run exits 0 (#257) | brief generator | LLM API contract | silent-success-noop |
| 4 | Migration 0007 applied on long-running `django` but not one-shot `rebuild-cache`; `:latest` pull drift → `UndefinedColumn`, 6×/day dead (#331/#340) | Django + rebuild-cache | migration / image-pull | deploy-env-drift |
| 5 | Stale VPS pipeline image (manual local build, not GHCR) shipped old code for ~36h (#259, repeated #321-#325) | thematic pipeline image | local build vs merged PR | deploy-env-drift |
| 6 | `Path.home()` in container → `/home/django/.alphalens`, empty dir, orphan-drop deleted all Brief rows | rebuild_briefs_cache | container bind-mount | deploy-env-drift |
| 7 | `jsonschema` undeclared runtime dep, transitively present locally, stripped from slim image (#322→#326) | template engine + image | dep closure / Docker build | deploy-env-drift |
| 8 | Paper chain used `--date today` but brief is (D-1)-dated → submit finds 0 PLANNED rows, silently (#343) | paper-trade systemd chain | brief asof ↔ paper date-key | seam-contract |
| 9 | CF Pages `_redirects` 404 rule falls through to SPA fallback → `text/html` at asset URL → blank screen (#285/#286→#287) | CF Pages deploy | CDN redirect / SPA fallback | silent-success-noop |
| 10 | Prometheus live rules hand-synced, drifted from repo; `git pull` ≠ live (#312/#340) | VPS Prometheus | repo file ↔ live instance | deploy-env-drift |
| 11 | New `feedback` Django app missing from CI `--source=` list → measured 0% coverage, false green (#292) | CI coverage config | CI config ↔ code | config-parity |
| 12 | `ALTER TABLE` guarded by `PRAGMA table_info`; fresh-v2 vs upgraded-v1 reach divergent state (#351) | feedback ledger migration | migration state machine | seam-contract |
| 13 | `SCORER_CONFIG` (Django-inlined) drifts from `LEAN_DEFAULTS` | research scorer ↔ Django | embedded vs external config | config-parity |

### Failure classes, ranked by frequency × severity

1. **deploy-env-drift** (5) — code/image/config differs between dev venv and the
   running container / VPS. The dominant family.
2. **silent-success-noop** (3) — the run exits 0 but produces empty / wrong output.
   The most *damaging*, because nothing alerts.
3. **seam-contract** (2) — a data / date / schema contract across a boundary breaks.
4. **config-parity** (2) — an embedded copy of config diverges from its source of truth.
5. **real-data-shape** (1) — vendor response differs from the fixture. Rare but the
   one class **no hermetic test can ever catch.**

The through-line: failures cluster at **integration seams**, **deploy/env
boundaries**, and **silent no-ops** — exactly what hermetic unit tests mock away.

---

## 3. The test pyramid (L1 keep, L2–L5 new)

No new test framework. Everything rides the existing split:
`unittest` (pipeline + research, single workspace `.venv` lets a test import both
packages), `pytest` (Django, `briefs/tests` + DRF `APIClient`), Playwright +
`svelte-check` (web).

### L1 — Existing unit suite (unchanged)
Logic correctness of pure functions, scorers, parsers, coercers, gate tri-state,
dedup tiebreakers — what mocks *can* validate. Wide base, stays as-is. Keep the
house convention: every parity / no-raw-http test ships a docstring naming the
incident + date + class it pins.

### L2 — Hermetic boundary-contract & deploy-shape parity (NEW, default CI)
**Import BOTH sides of a boundary and assert they agree, with a deliberately-broken
positive control so the assertion can't rot to a no-op.**
Covers seam-contract + config-parity + the *code-resolvable* subset of
deploy-env-drift. Runs inside the existing `research` + Django jobs, adds < 1s.

Catches: parquet column rename across a stage hop; JSON-interop corruption
(object-shaped JSONField missing from `_OBJECT_JSON_FIELDS`); alias-map rot
(`gemini_confidence`→`llm_confidence` class); env key in one of
`{.env.example, systemd -e, Dockerfile ENV}` but not the others; new Django app
missing from CI `--source`; new systemd timer missing `ExecStopPost` metrics hook;
prometheus rule referencing a job no unit emits; literature wrapper running
`git diff` before `git add`; undeclared runtime dep; migration fresh-v2 vs
upgraded-v1 divergence; paper (D-1) date-key contract.

> Already-shipped exemplars of this style: `test_pipeline_runtime_deps_declared.py`,
> `test_lean_config_parity.py`, `test_deploy_systemd_units.py`,
> `briefs/tests/test_schema_parity.py`. L2 generalises them to every boundary.

```python
# apps/alphalens-research/tests/test_lit_wrapper_git_ordering.py
# Pins the silent-success-noop fix (PR #345): the publish wrapper MUST `git add`
# BEFORE the emptiness gate, and the gate MUST be the staged form
# (git diff --cached --quiet) — else first-of-period UNTRACKED files are never
# committed (bare `git diff --quiet` sees only tracked changes -> exit 0 ->
# "looks healthy, published nothing").
def test_git_add_precedes_staged_emptiness_gate():
    text = WRAPPER.read_text()
    add_pos = text.index("git add")
    m = re.search(r"git diff --cached --quiet", text)
    assert m, "gate must use --cached (staged) form"
    assert add_pos < m.start(), "git add must run BEFORE the emptiness gate"
    # positive control: the broken bare-diff-before-add form must be absent
    assert not re.search(r"git diff --quiet\b", text[:add_pos])
```

### L3 — Golden-master end-to-end replay (NEW, one frozen real-data day, default CI)
Replay **one checked-in day of REAL captured upstream bytes** deterministically
through the whole chain (ingest → extract → themes → map → verify → catalyst →
score → brief → Django ingest → DRF `/v1/days`) and diff the final artifacts
against a checked-in golden. **Asserts side effects, not exit codes** — the primary
weapon against silent-success-noop.

Seams are already injectable: every LLM call goes through an injected
`OpenRouterClient`; every cross-stage hand-off is a parquet under
`~/.alphalens/thematic_*`. So: point caches at `tmp_path`, inject a VCR-style
`ReplayOpenRouter`, patch yfinance/EDGAR/Polygon at `get_default_*_client` to read
captured `raw/` fixtures, freeze `asof`, run the real `generate_briefs` +
`rebuild_from_parquet` + DRF view.

**LLM replay keying (revised per review §10):** do NOT key on `sha256(model+prompt)`
alone — that misses the sampling params and model version that change output.
Use a **VCR-style cassette** keyed on the canonical JSON of the full request
descriptor (`model` + model/API version + full prompt *including system + context*
+ `temperature`/`top_p`/`max_tokens` + `response_format`/tool config), stored under
a **human-readable scenario name**, with dynamic response fields (timestamps, ids)
normalized. **Fail loud on a cache miss** (a changed prompt or param is a behaviour
change — re-record deliberately, never fall back to a live call).

**Golden scope (revised per review §10):** the golden is NOT a full-row content
dump (snapshot rot + diff fatigue kill those, especially solo). Capture
**schema + row-count + per-column aggregates + a small stable exemplar subset**
(e.g. 2-3 rows), with volatile fields excluded. Structural/aggregate assertions
over verbatim snapshots.

Catches: empty/zero-row output that exits 0 (`assert len(df) > 0` + row-count band);
in-pipeline contract regressions (column rename, dedup tiebreaker,
template-supersedes-flash precedence, `extraction_method`/`template_id` audit cols)
as a **reviewable diff**; the Django ingest-drop seam
(`/v1/days.n_candidates == len(parquet rows)` — the Path.home orphan-drop class);
DRF envelope / ISO-date / nested-JSON drift the SPA depends on; (D-1) date-keying
off-by-one. Regenerate with `JUST_GOLDEN_UPDATE=1`, review the diff in the PR.

### L4 — Env-gated live vendor probes, shape-only (NEW, opt-in + weekly schedule)
**Assert our assumptions match REALITY.** Hit real services on a representative day,
assert shape + non-emptiness only, never values. Generalises the proven
`GDELT_LIVE_TEST` pattern (one flag per vendor). **The only layer that catches
real-data-shape.** Never in the blocking PR path (real-data variance + rate limits
= flaky); runs via `just probe-live` on demand + a weekly scheduled CI lane.

Catches: SEC EX-99.1 doctype-location mismatch (the exact #332→#338 escape);
LLM model-retirement 404 → silent empty brief (assert 200, `finish_reason !=
MAX_TOKENS`, JSON conforms to `EVENT_RESPONSE_SCHEMA`); Polygon/yfinance empty for a
liquid ticker. Permanent (shape/404/empty → FAIL) vs transient (429/timeout → warn,
fail only if > 50%) classification, exactly like the GDELT test. Weekend/holiday
empty day = inconclusive (skip, not fail).

### L5 — Container & VPS deploy smoke (NEW, image-build CI + VPS post-deploy script)
Catches deploy-env-drift that **only exists inside the built image or on the VPS** —
what L2 hermetic tests provably can't reach (they run in the dev venv from source).

- **CI image-smoke** (path-filtered to pipeline/django/deploy PRs, runs *before*
  GHCR push so a broken image can't publish): `docker build` the images, run a
  throwaway container that does `python -c "import <every direct + critical-indirect
  dep>"` + `alphalens thematic ingest --help` + a `Path.home()`-vs-bind-mount check
  (mount briefs at `/mnt/briefs`, pass `ALPHALENS_BRIEFS_DIR`, assert it reads the
  env path, not `Path.home()`). Catches #7 (jsonschema) and #6 (Path.home) at real
  import time.
- **VPS `deploy/scripts/postdeploy_check.sh`** (operator gate, not CI):
  `promtool check rules` + diff repo rules vs live `/home/jacoren/monitoring/...`
  + compare running container image SHA vs the GHCR tag for the current `main`
  commit. Catches #5 (stale image) and #10 (rule drift). Fails loud on drift.

---

## 4. Framework decision

- **No new framework.** `unittest` for pipeline+research, `pytest` for Django,
  Playwright+`svelte-check` for web — unchanged.
- **Golden fixtures** = real captured bytes checked into
  `apps/alphalens-research/tests/fixtures/golden_day/{raw,llm,golden}/`, regenerated
  with `JUST_GOLDEN_UPDATE=1`, reviewed as a diff. LLM replay = VCR-style cassettes
  keyed on the full request descriptor (model + version + system+prompt + sampling
  params + tool config), human-named, dynamic fields normalized, miss fails loud
  (see L3 + §10).
- **Live-vs-hermetic gating** = per-vendor env flags (`SEC_LIVE_TEST`,
  `OPENROUTER_LIVE_TEST`, `POLYGON_LIVE_TEST`, `YFINANCE_LIVE_TEST`), exactly like
  `GDELT_LIVE_TEST`. **Live probes never run in the blocking PR path.**
- **CI matrix:** existing `research` + Django + web jobs absorb L2 and L3
  (no new job, < 2 min added); a new path-filtered `image-smoke` job (L5) on
  pipeline/django/deploy PRs before GHCR push; the existing weekly
  `schedule: cron "0 2 * * 1"` gets a new `live-probes` job from repo secrets
  (failure pages, does not block PRs).
- **Recipes:** `just test-golden` (joins `just test`), `just probe-live`,
  `just test-deploy-shape`.
- Every L2 contract test ships a **positive-control** assertion (per the existing
  CLAUDE.md no-raw-http convention) so the contract can't degrade to asserting
  nothing.

---

## 5. First tests to write (highest leverage, each tied to a real escape)

1. **L2** `test_lit_wrapper_git_ordering.py` — `git add` before `git diff --cached
   --quiet`; positive control that the bare form is absent. → #1
2. **L2** `test_template_facts_roundtrip.py` (briefs/tests) — pipeline serialise ↔
   Django coerce identity round-trip + `brief_template_facts ∈ _OBJECT_JSON_FIELDS`;
   positive control that non-dict JSON doesn't coerce to a list. → field-rename class (#13-adjacent)
3. **L2** `test_env_key_tri_source_parity.py` — set-equality across `.env.example`,
   every systemd `-e KEY`, and `Dockerfile.pipeline` ENV. → `OPENROUTER_API_KEY` class
4. **L2** `test_ci_coverage_app_parity.py` — glob `apps/alphalens-django/*/apps.py`,
   assert each is in CI `--source`. → #11
5. **L3** `test_golden_replay.py` + recorded fixtures — full-chain replay; invariants
   `len(df) > 0`, byte band, `extraction_method` present, snapshot diff. → #3 + swallowed-exception class
6. **L3** `test_golden_api.py` (briefs/tests) — `rebuild_from_parquet(tmp)` then GET
   `/v1/days/{date}`, assert `n_candidates == len(parquet rows)`. → #6 ingest-drop seam
7. **L4** `test_sec_live.py` (`SEC_LIVE_TEST`) — fetch a known recent 8-K, assert
   EX-99.1 resolves from the real `{accession}-index.htm` Type column. → #2 (the bug no hermetic test can catch)
8. **L5** image-smoke step — `docker build` pipeline image, container
   `import jsonschema, pandera, lxml; ...` + assert `ALPHALENS_BRIEFS_DIR` overrides
   `Path.home()`. → #7 + #6

---

## 6. Rollout

> **Sequencing revised per review §10.** Both reviewers argued deploy-env-drift is
> the dominant class (5/13) and that the highest-leverage first investment is the
> deploy path (immutable digest-pinned image + a real-flow smoke gate), not the
> hermetic scanners alone. So **Phase 1 now blends the cheapest L2 scanners WITH the
> L5 image-smoke + digest-deploy** — the two together kill the three recurring
> deploy escapes (#5 stale image, #7 jsonschema, #6 Path.home) plus the silent-noop
> wrapper bug, for modest infra cost.

| Phase | Deliverable |
|-------|-------------|
| **1a — L5 image-smoke + digest-pinned deploy** (dominant-class, modest CI infra) — ✅ SHIPPED #361 (image-smoke.yml + django-image-fail alert) + #362 (`deploy/scripts/postdeploy_check.sh` + sha-pin runbook) | path-filtered `image-smoke.yml`: build pipeline + django images once per commit, **import-smoke every runtime dep + run a MINIMAL real flow** (`alphalens templates validate` / a 1-item pipeline step + one DRF endpoint via compose, NOT just `--help`), assert `ALPHALENS_BRIEFS_DIR` overrides `Path.home()`; **push by immutable digest, deploy the same digest, post-deploy verify the running digest == tested digest** (kills tag-rollback drift #4/#5). |
| **1b — L2 cheapest scanners** (zero infra, all hermetic) — ✅ SHIPPED #355 | `test_lit_wrapper_git_ordering`, `test_env_key_tri_source_parity`, `test_ci_coverage_app_parity`, `test_systemd_metrics_hook_completeness` (glob-derived, replaces hardcoded `ACTIVE_SERVICES`), `test_prometheus_rule_unit_parity`. All in existing `research` job. |
| **2 — L2 cross-process contracts** — ✅ SHIPPED #364 | `test_template_facts_roundtrip` (django) + pandera hop schemas for news/events/candidates/scored in `data/schemas.py` (`dtype=None` + element-wise checks for pandas-3.0 infer_string) + `test_parquet_hop_schemas` (legacy-column tolerance); `test_brief_date_key_contract` (the (D-1) seam, #343); `TestMigrationStateConvergence` (#351). Schemas defined as contracts but NOT yet wired into the live producer write-path — deferred to Phase 3 golden-frame work to avoid false reds. |
| **2b — migrate-skew compose test** (fills a gap no layer covered, §10) — OPEN | testcontainers/compose test reproducing #331/#340: long-running `django` on image-N + one-shot `rebuild-cache` on image-N+1 (migration skew) → assert it fails loud, not silent `UndefinedColumn`. Consider a migration advisory-lock as the durable fix. |
| **3 — L3 golden-master replay** — ✅ SHIPPED #366 (brief stage) | `scripts/record_golden_brief.py` one-time live capture; VCR-style `ReplayOpenRouter`/`RecordingOpenRouter` cassettes (keyed on full request descriptor, fail-loud) + frozen scored/OHLCV/golden fixtures; golden = schema+row-count+tickers+stable exemplar; `test_golden_brief_replay` (research) + `test_golden_api` (Django) with side-effect invariants (#3 non-empty, #6 ingest-drop); `just test-golden`. **Phase 3b** (one stage/PR): 3b-1 extract (#368) reuses the LLM cassette + 3 synthetic template rows; 3b-2 map-themes (this PR) adds the generic non-LLM `VendorCassette` (Polygon firehose, trimmed to candidate rows) + frozen tenk 10-K text / Form-4 / catalyst / yfinance-mcap seams — all monkeypatched at existing orchestrator seams, zero prod change; 3b-3 ingest (this PR) adds `url_cassette.py` (GDELT `UrlJsonCassette` + RSS `FeedCassette`) and locks the cross-source merge over GDELT (synthetic — free-tier 429s a live sweep) + Polygon/RSS (live), EDGAR deferred (nested-fetch volume); 3b-4 score still open. |
| **4 — runtime output-volume metrics** (silent-noop, leverages existing observability epic, §10) | emit `alphalens_thematic_candidates_count` / brief-row-count textfile metrics from the daily build; Prometheus alert on **zero-output-with-nonempty-input** + a freshness/row-count anomaly rule. The real-world catch for a model retiring *next* month is a production dead-man-switch, not a test. |
| **5 — L4 live probes + VPS gate** | `tests/live/{sec,openrouter,polygon,yfinance}` with per-vendor flags + transient/permanent classification; weekly scheduled CI `live-probes` job; `just probe-live`; `deploy/scripts/postdeploy_check.sh` (promtool + repo↔live rule diff + digest check) wired into the runbook. |

---

## 7. The suite as living documentation

The layout is designed so reading it bottom-up teaches AlphaLens:

1. Every **L2 contract test** carries a docstring in the house style — incident +
   date + the failure class it pins (`test_pipeline_runtime_deps_declared.py` is the
   template: it narrates the 2026-05-31 jsonschema incident).
2. The **pandera schema constants** in `data/schemas.py` become the single
   authoritative answer to "what columns does each `thematic_*` parquet have, which
   are nullable" — stronger than a docstring because they can't drift from the producer.
3. The **L2 scanners turn CLAUDE.md prose checklists into executable specs** — the
   env-key tri-source test *is* the "three places a key must live" list; the
   coverage-app-parity test *is* the "add new app to `--source`" rule.
4. The **L3 golden files** (`golden/briefs_*.json`, `golden/api_days_*.json`) are the
   canonical given-when-then narrative of an end-to-end day: one JSON file shows the
   exact `brief_*` field set, tri-state gates, typed `template_facts`, and the
   `{data, meta}` API envelope — and every behaviour-changing PR renders as a
   reviewable snapshot diff = the behaviour changelog.
5. `tests/live/` documents the **REAL vendor response shapes** (SEC EX-99.1 location,
   OpenRouter `finish_reason` contract), captured-from-reality not fabricated, so they
   can't lie the way the EX-99.1 fixture did.
6. The **L3 replay test file**, read top-to-bottom, walks all pipeline stages in
   order — the most accurate seam map in the repo.

---

## 8. Anti-patterns (what NOT to do)

- **Don't re-mock the seam you're pinning.** The point of L2/L3 is to import BOTH
  real sides; mocking the Django coercer or the pipeline serialiser in a contract
  test reproduces the exact unit-suite blind spot that let every escape through.
- **Don't put live vendor probes (L4) in the blocking PR path.** Flaky red trains
  everyone to ignore red. Weekly schedule + on-demand only.
- **Don't let the golden grow to many days.** One representative day (maybe a second
  M&A-burst day later). Broad golden suites rot and produce noisy diffs nobody reviews.
- **Don't snapshot volatile fields** (`brief_generated_at`) — exclude them from the
  projection or the golden churns every run.
- **Don't assert exit codes where a side effect is the real contract.** The
  silent-success class exits 0 by definition; assert rows-written, file-staged,
  non-empty-frame, `ingest_count == parquet_rows`.
- **Don't delete positive-control assertions to "clean up"** — a parity scanner with
  no broken-input case can silently degrade to asserting nothing (the documented rot mode).
- **Don't hardcode allowlists** (the `ACTIVE_SERVICES` tuple problem): derive from
  filesystem glob with a documented exclude-set, so a new unit can't ship uncovered
  by passing a stale list.
- **Don't pin an L4 probe to a stale accession/ticker forever** — a delisted ticker
  or expired filing fails for the wrong reason. Document a refresh cadence; treat
  weekend/holiday emptiness as inconclusive.

---

## 9. Open questions

1. **VPS-side L5 checks** (Prometheus rule diff, image-SHA vs GHCR tag,
   migrate-applied vs image-schema) can't be repo-side CI — they need an agent ON
   the VPS. `postdeploy_check.sh` run manually from the runbook, or a systemd timer
   that emits a Prometheus metric (closing the loop the observability epic built)?
2. The Django **migrate-on-start vs one-shot rebuild-cache RACE** (#331/#340) is only
   reproducible with a two-container compose integration test. Worth one in CI, or is
   L5 image-smoke + the pin-`ALPHALENS_DJANGO_TAG`-to-sha discipline (#341) enough?
3. **LLM golden re-record friction:** a prompt edit invalidates the `sha256` key and
   forces re-record. Acceptable as "prompt changes are behaviour changes, review
   them", or do we need a normalized-prompt-skeleton key?
4. Should the **L4 weekly job page via Telegram Alertmanager** (fits the existing
   habit, adds a non-CI dep) or just open a GitHub issue?
5. **CF Pages `_redirects` / stale-chunk** (#9) sits outside every layer (CDN edge).
   Worth a post-deploy Playwright smoke against the live Pages URL (assert no
   "Failed to load module" console error), or does the SvelteKit `version.pollInterval`
   fix (#287) make it moot?
6. Who **refreshes the L4 SEC probe's pinned 8-K accession** when it ages out — a
   dated constant with a comment, or a helper that discovers a recent EX-99.1 filing
   dynamically?

---

## 10. External review (zen `deepseek-v4-pro` thinking=high + Perplexity research high, 2026-06-01)

Both reviewers were run adversarially on the v1 memo. They **converged** strongly;
the deltas below are folded into §3–§6 above. (Per project doctrine on
adversarial-reviewer bias: technical warnings kept, go/no-go meta-conclusions
weighted against project reality.)

**Where they pushed back on the v1 plan (changed):**

1. **Rollout order.** v1 put hermetic L2 scanners first. Both argued
   deploy-env-drift is the dominant class (5/13) and the deploy path is the
   highest-ROI first investment. → **Phase 1 now blends L5 image-smoke +
   digest-pinned deploy with the cheapest L2 scanners** (§6). Industry norm both
   cited: build one image per commit, smoke it on the *real artifact*, promote by
   **immutable digest** (not tag), post-deploy verify the running digest.
2. **`sha256(model+prompt)` replay key is too narrow.** Misses sampling params +
   model version → silent divergence. → **VCR-style cassettes** keyed on the full
   request descriptor, human-named, dynamic fields normalized, fail-loud on miss (§3 L3).
3. **Golden-master scope.** Full-row snapshots rot → diff fatigue → mechanical
   "accept" (worse solo). → golden = **schema + row-count + aggregates + small stable
   subset**, structural over verbatim (§3 L3).
4. **Underpowered smoke.** `--help`-only misses config/dep issues. → Phase 1a smoke
   **runs a minimal real flow + one DRF endpoint** (§6).
5. **Contract over-specification (Pact lesson).** → L2 asserts **consumer-relevant
   fields only**, not provider internals (§6 Phase 2).

**Gaps they found that NO v1 layer covered (added):**

6. **migrate-on-start version-skew race (#331/#340)** needs a **two-container compose
   test** (long-running on image-N + one-shot on image-N+1) — added as Phase 2b; the
   durable fix is a migration advisory-lock.
7. **Silent-success-noop is partly a *production* problem, not just a test problem.**
   SOTA = assertion-rich tests **+** pipeline row-count/freshness/anomaly metrics **+**
   dead-man-switch. AlphaLens already built the dead-man-switch infra (the
   observability epic: Prometheus staleness alerts + Telegram). The gap is
   **output-volume metrics** (alert on zero-output-with-nonempty-input), not
   last-success-time. → added as Phase 4, leveraging existing infra.

**Where they VALIDATED v1 (no change):** the seam-centric thesis; positive-control
convention (prevents assertion-rot — both endorsed); one-frozen-day discipline;
fail-loud on replay miss; never gating CI on live probes; assert side-effects not
exit codes; the suite-as-documentation framing.

**Meta-conclusion deliberately NOT adopted:** Perplexity's framing that L5 should be
*the* first and dominant layer with L2/L3 deferred. Weighed against project reality:
the cheap L2 scanners are near-zero-cost and three of them pin already-recurring
incidents (#345, #292, env-key), so deferring them buys nothing. Hence the
**blended** Phase 1 rather than an L5-only first phase.
