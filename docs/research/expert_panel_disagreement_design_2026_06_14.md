# Expert-panel disagreement signal + panel chip + drawer — locked design (PR-8)

**Status:** LOCKED 2026-06-14 (epic #541 PR-8, issue #550). Built via design Workflow
(synthesis → 3 adversarial lenses [manufactured-authority / transition-shim / numeric-drawer]
→ revision). **SPLIT into PR-8a (data) + PR-8b (SPA), sequential.**

## The split
- **PR-8a (pipeline + Django)** — ships FIRST, low-risk, migration-free, starts the log-now
  corpus clock. `disagreement.py` + score-stage wiring + Django ingest assembles the `oneil`
  and `panel` blob sub-dicts. Only changes the assembled `expert_assessments` blob shape (already
  on both serializers since PR-5a); the SPA ignores the new keys until 8b. One combined zen pass.
- **PR-8b (SPA)** — purely additive client render over the blob 8a produces: the neutral +1
  coverage chip + generalized `ExpertPanel` drawer + tone helpers + the 2-rule transition shim.
  Deploys via CF Pages auto-build (no manual gate). One web zen pass.
- **Deploy runbook:** merge+deploy+VERIFY 8a (VPS pipeline image rebuild → Django GHCR pull →
  one thematic-build → confirm `blob.panel` in a live `/v1/briefs` response) BEFORE merging 8b.
  Out-of-order 8b is SAFE only because the client recompute was dropped — it degrades to the
  neutral chip / `—`, never a wrong/flipping number.

## The disagreement scalar (PR-8a)
`expert_spread = abs(buffett_quality_score − oneil_score)` over the two 0-100 composites already
on the score-stage frame. Range [0, 100]. **None (JSON null) when fewer than 2 scores present**
(tri-state — a spread of 0.0 means "agree", distinct from "could not compute"). In practice often
None (oneil_score is N-gated; buffett needs fundamentals).

**Manufactured-authority discipline (blocker #6, the load-bearing constraint):**
- The two composites are built from DIFFERENT inputs/weights/clip-caps and are **NOT known to be
  commensurable**. The gap is recorded RAW so the deferred Expert×EDGE study (N≥30, ~2026-09+)
  decides EMPIRICALLY whether it carries signal. It is NOT a calibrated disagreement magnitude.
  `disagreement.py` docstring states this verbatim. No card copy claims the lenses "agree/disagree
  on grade" — only lens-membership facts ("Buffett 31/100 value/quality, O'Neil 78/100 momentum").
- The display BANDS (CONSENSUS <20 / MIXED 20–50 / SPLIT ≥50) are **unvalidated**, live ONLY in
  the opened drawer (never the resting chip), carry an inline "unvalidated · not a buy/avoid
  signal" label at the point of color, and are **folded into `panel_config_version`** — the
  analyst correlates the RAW scalar, never the bucket.
- Panel stays display-only / OUT of the brief sort (PR-6 `_NON_EXPERT_SORT_ALLOWLIST` enforces;
  `expert_spread`/`panel_config_version` carry no `expert_` prefix so they rely on the allowlist
  guard, NOT the prefix guard — do not add them to the allowlist).

**N>2 generalization (documented, NOT wired):** dispersion = `pstdev` of present scores; for N=2
`pstdev = abs(a−b)/2` ≠ the abs-diff form → DO NOT silently switch formula families. A 3rd expert
is a NEW `panel_config_version` (`panel-v2-pstdev-3x`); correlate only within a config_version.
`expert_spread_max_pair` is added (ASCII separator `buffett|oneil`) only then — at N=2 it carries
zero info and the U+2194 glyph trips `test_no_polish_chars`.

## Persistence (PR-8a) — migration-free
Pipeline emits 2 flat parquet columns: `expert_spread` (float64, None→NaN) + `panel_config_version`
(str, `"panel-v1-absdiff-2x"`, stamped UNCONDITIONALLY incl. null-spread rows). Django ingest
assembles them into a new top-level **`panel`** key in the existing `expert_assessments` JSONField
(sibling to `buffett`/`oneil`), via the SAME `_EXPERT_COLUMNS` mechanism. **NO new Brief model
field, NO migration** (reuses JSONField 0011 — avoids reopening the PR-5b flat-field churn).
`manage.py makemigrations --check --dry-run` must stay clean (asserted in CI).

Blob shape after 8a:
```
expert_assessments = {
  "buffett": {…12 keys…},
  "oneil":   {8 oneil_* keys},
  "panel":   {"expert_spread": 42.7, "panel_config_version": "panel-v1-absdiff-2x"},
}
```
Corpus contract: the Expert×EDGE query reads ONLY `expert_assessments->'panel'->'expert_spread'`
(Postgres JSONB) — never a recomputed value.

## The 3-site Django lockstep (PR-8a)
1. `parquet.py` `_EXPERT_COLUMNS` += `"oneil"` (the 8 `ONEIL_COLUMNS`) + `"panel"` (`expert_spread`, `panel_config_version`).
2. `coerce.py` `_EXPERT_FLOAT_COLUMNS` += `expert_spread` + the 6 oneil floats; `_EXPERT_BOOL_COLUMNS`
   += `oneil_new_high_split_suspected` + `oneil_earnings_growth_near_zero_base` (tri-state restore —
   NOT float/coerce_str, else FALSE persists as the truthy string `"0.0"` and fires audit badges).
3. `test_schema_parity` / `test_ingest`: INVERT `test_oneil_columns_present_but_unread` (oneil IS
   assembled now) + add frozen `_EXPECTED_ONEIL_BLOB_COLUMNS == ONEIL_COLUMNS` + `_EXPECTED_PANEL_BLOB_COLUMNS`
   pins in the SAME commit (the only cross-boundary drift guard — Django can't import the pipeline).

## SPA (PR-8b) — summary
- **Resting chip:** ONE tone-neutral coverage-only token `panel 2 lenses` / `panel 1 lens` / `panel —`
  (no band word, no color on the card face; whitespace-nowrap single element). +1 token for ANY N.
- **Transition shim:** TOTAL 2-rule predicate `Number.isFinite(blob?.panel?.expert_spread)`: finite →
  render band/headline/dot-lane FROM the persisted scalar (pipeline is the sole formula owner); else →
  neutral chip, no headline/dot-lane, no crash. **NO client-side recompute** (would be a second formula
  source-of-truth + a value-flip in the deploy window).
- **Drawer (`ExpertPanel.svelte`):** rename `BuffettPillar`→`ExpertPillar` (+ test in lockstep). Headline
  (only when spread non-null) names raw scores + lens names + the drawer-only band (colored + "unvalidated"
  label) + `panel_config_version`. Dot-lane (only when ≥2 present): one dot per score on a 0-100 CSS track,
  colored by each expert's OWN tone helper. Per-expert sections branch on a SPA-side `EXPERT_KIND` map:
  Buffett (`qual`) = 4 ExpertPillar badges + rationale; O'Neil (`numeric`) = numeric `dl` readouts + the
  2 audit flags as badges shown ONLY on `=== true` (explains why oneil_score is None) + "price panel +
  EDGAR fundamentals · numeric-only, no LLM" footnote (no per-expert config_version — O'Neil has none).
  N>3 tab fallback documented, not built.

## Open risks
- `expert_spread` compares two un-calibrated heuristic composites — may carry no signal; that is the
  POINT of recording it raw for the deferred empirical study. Never presented as calibrated.
- All bands/cutoffs/tone helpers are unvalidated module constants, pinned by `panel_config_version`.
- Cross-service deploy is non-atomic; the split ordering (8a deployed+verified before 8b) is the
  continuity guarantee, not a client fallback.
