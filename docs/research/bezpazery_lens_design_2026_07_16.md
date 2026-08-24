# /edge what-if exit lens — "bezpazery" ATR bracket (v1) — 2026-07-16

**Status:** LOCKED (parameters pinned in this memo BEFORE the registry entry is
evaluated on any store data).

**Lens:** `atr_bracket_1p5` · kind `atr_bracket` · display-only · `in_sample` ·
4th of `MAX_REGISTERED_LENSES = 5`.

## 1. Source and intent

Source of inspiration: the friend bot **betlejem5** EMM executor bracket doctrine
(fixed ATR-multiple stop + fixed ATR-multiple take-profit with a cost floor and a
52-week-high ceiling). This lens is an INSPIRATION, **not a replication** — two
deliberate deviations from the source are documented in §3. The question it
answers on /edge: "what would the SAME picks + SAME price paths have realized
under a simple symmetric ATR bracket instead of the production TP ladder + deep
disaster stop?"

## 2. Pre-registered geometry — section id: `betlejem5_comparative bezpazery v1 (bracket 1.5xATR, floor 0.6%, ceiling 52w-high)`

The string above is the `preregistered_ref` carried verbatim by the lens record
(`breakeven_lenses.py`), the Django mirror (`edge/api/summary.py`
`_LENS_PREREGISTERED_REF`), and the research-side parity test.

Pinned v1 parameters (fixed here, before registration):

| Parameter | Value |
|---|---|
| `stop_atr_mult` | **1.5** — bracket stop = blended_entry − 1.5 × ATR_abs |
| `tp_atr_mult` | **1.5** — ATR take-profit target = blended_entry + 1.5 × ATR_abs |
| `tp_floor_frac` | **0.006** — cost floor: TP never below blended_entry × 1.006 |
| ceiling | trailing **52-week high**, reconstructed as `asof_close / (1 + technical_pct_off_52w_high / 100)` (pct ≤ 0 by construction; pct = 0 → ceiling = asof_close) |
| TP formula | `tp = min(ceiling_52w, max(blended × 1.006, blended + 1.5 × ATR_abs))` |
| TP tranches | single tranche at 100% (`tranche_pct = 100.0`) |
| stop behaviour | static — no ratchet, no trail, no break-even move |
| ATR source | `trade_setup["atr"]` (absolute, per-share; = `technical_atr_pct`/100 × asof_close, stamped by the setup builder) — same source + null-guard as the fill-anchored lens |
| R denomination | the lens's OWN stop distance (risk = blended − bracket_stop), not the production ladder's risk unit |

Entry anchoring: the SAME production ladder entry tiers (NOT the fill-anchored
single-E1 collapse). Fills are re-derived over the cached RTH minute path under
the lens-family contract — no entry-TTL, no position time-stop
(`replay_ladder_breakeven` precedent). The multi-tier anchor is the
alloc-weighted blended entry over the FINAL fill set from walk-1
(`_blended_entry`), the same final-blend convention the break-even lens and the
stored MFE use. Deeper tiers filling after the anchor is set do not move the
stop/TP — the bracket is frozen at the walk-1 blend.

## 3. Deliberate deviations from the betlejem5 source

1. **No day-flatten.** The source flattens same-day at the session close. This
   lens uses the identical bar window as every other registered lens (arrival
   open → min(position_expiry_session at TIME_STOP_DAYS = 42,
   last_closed_session)), replays with no intra-replay expiries, and marks a
   position neither stopped nor TP'd by the last bar to the last close
   (horizon-open remainder path). Rationale: cross-lens comparability on /edge
   dominates source fidelity — every lens must see the same horizon.
2. **Ladder-tier entries instead of MKT-at-open.** The source enters at market
   on arrival. This lens keeps the production resting-limit entry tiers so the
   counterfactual isolates the EXIT geometry (the registry doctrine: same picks,
   same entries, different exit).

An exact-source variant (day-flatten + MKT-at-open) would be a 5th registration
hitting the `MAX_REGISTERED_LENSES = 5` cap exactly; deliberately NOT registered
now — the slot stays open (ADR 0013 R4 one-in-one-out beyond the cap).

## 4. Resolved open questions (pinned before registration)

1. **Ceiling at/below the cost floor** (`ceiling ≤ blended × 1.006`, e.g. a name
   right at its 52w high): the bracket is degenerate (TP at/below entry+costs) —
   the lens returns **None** ("bracket not constructible"), consistent with the
   lens null semantics. The floor-wins alternative (quietly abandoning the
   ceiling) is rejected.
2. **Missing `technical_pct_off_52w_high`** (history < 252 sessions, or old
   brief parquets predating the column): TP is **UNCAPPED**, not null — the
   ceiling is a safety cap, not a core parameter. This introduces mild
   heterogeneity (capped vs uncapped rows pool under one lens_id); accepted,
   because the stricter null alternative would make the lens's n a proxy for
   data coverage. Only a missing/non-finite/≤0 ATR nulls the lens.
3. **Walk-2 R denominator drift.** The replay reuses `replay_ladder` with a
   modified setup (`_with_tp_tranches` + `_with_disaster_stop`), so risk =
   (walk-2 realized blend) − bracket_stop. On multi-tier paths the walk-2
   realized blend can drift from the walk-1 anchor blend (the higher bracket
   stop can end the walk before deeper tiers fill), so realized risk is not
   exactly 1.5 × ATR. Accepted: the reuse inherits SL-first same-bar ambiguity,
   filled-frac TP re-basing, and NO_FILL→None for free; an exactly-1.5-ATR
   denominator would need a custom second walk. Documented in the docstring.
4. **ATR anchor caveat.** `trade_setup["atr"]` is anchored to `asof_close`, not
   to the entry fill price, so the bracket width deviates slightly from a
   literal "technical_atr_pct/100 × entry price" reading. Same convention as the
   fill-anchored lens (precedent followed on purpose).
5. **Naming.** Code identifiers stay English: lens_id `atr_bracket_1p5`, label
   `ATR bracket 1.5 (bezpazery)` — the homage lives in the display label only.
6. **Retroactive coverage.** FORWARD-ONLY: frozen terminal rows keep their
   stamped lens map (PR #747); the diagnostics backfill fills only fully-empty
   `breakeven_realized_r_json` columns and never per-key merges. Extending it to
   per-key merge is a separate behaviour change (own tests) — noted as a
   follow-up option, not part of this change.

## 5. Null / skip conditions (map value `None`, keyed like every lens)

Unparseable / absent / non-OK setup or missing disaster stop (`parse_ladder`);
no bars; ATR missing / non-finite / ≤ 0; nothing fills in walk-1; risk ≤ 0;
bracket not constructible (ceiling ≤ blended × (1 + `tp_floor_frac`), §4.1).
Missing 52w-high distance is NOT a null (§4.2 — uncapped).

## 6. Status, budget, first look

Display-only counterfactual on /edge (`{lens_id: r}` in
`breakeven_realized_r_json`); never the headline `realized_r`; `in_sample` until
a clean forward sample crosses the N-gate. Populates FORWARD-ONLY from its
deploy date. Charged as **1 policy look** against the ~2026-09 exit walk-forward
multiplicity budget (`edge_hypothesis_budget_2026_07.md` §4.1 annex; ADR 0013
R4: every registered lens counts). **First honest look = the September exit
walk-forward** — no peeking at the accruing forward sample before it.

## 7. Amendment 2026-08-24 — the entry anchor is now explicit (issue #1114)

**Status of this amendment:** LOCKED. The pre-registered geometry of §2 is
**unchanged** — same 1.5×ATR stop, same 1.5×ATR take-profit, same 0.6% cost
floor, same 52-week-high ceiling. Only the ENTRY ANCHOR the bracket is placed
around is now stated instead of implied.

### 7.1 What was wrong

`replay_ladder_atr_bracket` anchored the bracket on the alloc-weighted blend
over the tiers that TOUCHED in the bar walk. The live rail
(`paper/sizing.py::build_exit_geometry_spec`) anchors on the blend over ALL
intended tiers. The two are equal only when every tier fills; on a partial fill
they are far apart. On SMG, 2026-08-24: planned blend 55.5957, realised-anchor
blend 59.786017 (the top tier's limit), a gap of 4.19 on the anchor and the same
gap on the derived stop.

The lens is the instrument used to judge this policy, so an `/edge` figure was
describing a policy the live rail does not run, and the flattery grew exactly in
the cases that go wrong.

### 7.2 What changed

`replay_ladder_atr_bracket` now takes a REQUIRED, defaultless `anchor`
(`"planned"` or `"realised"`) and raises on anything else. Both anchors are
registered as separate lenses:

| lens_id | anchor | mirrors |
|---|---|---|
| `atr_bracket_1p5` | `realised` | the lens's own historical behaviour |
| `atr_bracket_1p5_planned` | `planned` | the live rail |

The historical id keeps the realised anchor because it carries every value
already stamped on the live rows, and the daily path never recomputes a stamped
row. Renaming it would strand that history under a key no longer in the
registry.

### 7.3 Consequences for reading `/edge`

- Every `atr_bracket_1p5` value stamped before 2026-08-24 describes the
  **realised-fill** anchor, not the live policy. That includes the exploratory
  read recorded in issue #1115.
- `atr_bracket_1p5_planned` populates **forward-only**. Comparing the two means
  before it accrues its own N compares cohorts, not anchors.
- The registry is now 5 of `MAX_REGISTERED_LENSES = 5`. Any further lens is
  one-in-one-out (ADR 0013 R4).

### 7.4 The take-profit floor — settled, not one-sided

Issue #1114 records the 0.6% floor as a second divergence the lens applies and
production does not. That is wrong in BEHAVIOUR and was refuted by running it:
both sides reach the floor through the one shared leaf
`broker_contract/exit_geometry/levels.py::atr_bracket_levels`, production via
`AtrBracketPolicy.decide_placement_geometry`. The floor was one-sided only in
TELEMETRY — the live geometry stamp never named it — which this change fixes by
adding `anchor_mode` and `tp_floor_frac` to the stamp. The identity is pinned by
a test where the floor binds on both sides.

### 7.5 The two anchors do NOT gate the same cohort

An earlier draft of this amendment said the two lenses report over the same
cohort because they share the walk-1 no-fill gate. The premise is true and the
conclusion does not follow. The null conditions of §5 are applied to EACH
lens's own anchor, and two of them are anchor-dependent:

- ceiling ≤ anchor × (1 + `tp_floor_frac`) → not constructible (§4.1);
- anchor − 1.5 × ATR ≤ 0 → bracket stop at/below zero.

Because the planned blend sits BELOW the realised blend on every partial fill,
the two gates fall on different rows, and not at random. Worked examples, both
pinned as tests in `tests/feedback/test_atr_bracket_anchor_mode.py`
(`TestTheAnchorAlsoMovesTheCohort`):

| shape | planned lens | realised lens |
|---|---|---|
| tiers 100 / 60 at 50-50, only the top fills, ATR 2, 52w ceiling 100.5025 | value (floor 80.48 < ceiling) | `None` (floor 100.6 > ceiling) |
| tiers 10 / 2 at 50-50, only the top fills, ATR 4 | `None` (stop 6.0 − 6.0 = 0) | value (stop 4.0) |

Reading rule for the September walk-forward: **compare the two lenses only on
rows where BOTH are non-null.** A difference of means over each lens's own
non-null set mixes an anchor effect with a cohort effect, and the cohort
difference concentrates on the ceiling-capped rows — the ones the pair exists to
compare. This is a reading caveat, not a defect to code around: each lens keeps
the null conditions it was pre-registered with, and coupling their null sets
would make one lens's value depend on the other lens's geometry.

### 7.6 Not modelled: the realised AVERAGE FILL anchor

The replay fills a tier AT its limit, so `"realised"` is the blend of tier
LIMITS, never of broker fill prices. A third anchor exists in the live journal —
the realised average fill (SMG: 59.9261, giving a target of 63.9581 against the
realised-limit 63.818017) — and is what issue #1112 step 4 would move the live
rail onto. Adding it is a third `AnchorMode` value, which is cheap precisely
because the anchor is now a named argument with no default.
