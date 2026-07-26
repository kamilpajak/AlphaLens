# /edge ladder-chart context band — capacity root (PR-2)

**Status:** DRAFT
**Date:** 2026-07-26
**Author:** Kamil Pająk
**Scope:** `apps/alphalens-pipeline/alphalens_pipeline/feedback/` (chart enrich pass) + one CLI/monitor constant retune, pipeline-only
**Review:** zen `deepseek/deepseek-v4-pro` thinking=high mandatory (shared feedback surface), both PRs
**Follows:** [`edge_chart_staleness_starvation_fix_2026_07_25.md`](edge_chart_staleness_starvation_fix_2026_07_25.md) (PR #904, §9 deferred capacity root). Anchors verified against `origin/main` (post-#904).

---

## 0. Problem

PR #904 killed the exit-marker *starvation* by decoupling the Polygon-free marker core from the budgeted daily CONTEXT band. It did not remove the underlying capacity waste: every nightly `feedback backfill-shadow-returns` run re-fetches the daily context band for every non-frozen row, and `_memoized_daily_fetch` (`ladder_chart.py:680-699`) is a per-RUN dict — nothing persists across nights. ~150-200 ongoing rows re-fetch an **immutable** lead-in every night, chronically starving the 15-min chart reserve so many rows thrash to `reused`/`in_trade_only`. PR-2 removes that waste at the root.

## 1. Decision

**SCOPE = B (capacity + reserve retune), staged into two PRs. residual-23 OUT. UNAVAILABLE state OUT.**
**APPROACH = A3 reuse-first** — flip the per-row context decision from *budget-gated* to *need-based*, reusing the persistence + splice machinery #904 already shipped, plus a zero-cost **deep-first-fetch** refinement. **No new cache file, no seed, no systemd timer, no new client.**

The steady-state waste is not a missing cache — #904 already persists the entire context band in `chart_payload_json.bars` on the store parquet, and already shipped the reuse path (`_reused_context_from_prior` `ladder_chart.py:974-992`, `_split_reused_context` :471-497, `CONTEXT_REUSED` :467, the reuse branch in `_payload_for_row` :1079-1089, and the `context_allowed` gate at :946). That reuse path fires **today only when the deadline is exhausted** (`context_allowed = deadline is None or not deadline.should_stop()`, :946). When budget exists, every non-frozen ongoing row re-fetches an **immutable** lead-in it already has on disk. Verified immutable: for an ongoing row `horizon_session` = the newest cached minute bar = last_closed (`_horizon_session` :1099-1120), so the trailing band is empty (:439) and the lead-in is sessions strictly before the fixed `arrival_session` (:438) — pure history. The fix is therefore a **predicate change**, not new infrastructure: reuse the disk band when a usable prior exists; fetch only genuine new work (new arrivals + one-time maturations). Steady-state ongoing re-fetch → ~0.

**Why NOT A1 (new per-ticker daily cache):** it re-implements persistence that already exists in the payload, and — verified fatal in the naive form — the lead-in **grows backward every night**: `_lead_in_sessions = min(90, max(20, 2·hold))` (:330-332) while `TIME_STOP_DAYS = 42` (`population_ladder_monitor.py:325`) caps hold at 42, so `2·hold` maxes at 84 and **never reaches the 90 cap before maturation**. A front-extending per-ticker cache therefore fetches ~1 call per ongoing row per night for the whole mid-hold population — it does *not* kill the cost. It only works if it over-fetches to the cap on first sight, at which point it is strictly more surface (new dir + covered-range gap arithmetic + basis/path regression test + a ~194-fetch cold warm-up) than reuse-first for the identical outcome.

**Why NOT A2 (new whole-market adjusted=false grouped store):** heaviest surface — new systemd unit + `AlphalensJobStale` alert + runbook, deep-history seed, retention/prune, entitlement-cliff handling — and it is a **third** whole-market grouped store sitting adjacent to the CLAUDE.md "never merge the two grouped caches" doctrine. Whole-market *breadth* buys nothing for a per-ticker *depth* need.

**Why NOT reuse the monitor's existing grouped cache — verified FATAL:** the monitor's `population_ladders/grouped/` cache does **not contain lead-in sessions**. `_candidate_new_sessions` (`population_ladder_monitor.py:1367-1405`) writes only the in-trade window `(last_priced, min(position_expiry, last_closed)]` plus the *one* session immediately before it (:1402-1404), and only for cheap-eligible OPEN/NO_FILL priors. Lead-in coverage is at most 1 incidental session deep — it cannot serve a 20-90 session lead-in.

## 2. Design

**No new cache.** The band lives in the store parquet's `chart_payload_json.bars` column (adjusted=false raw daily candles, immutable historically — a later split never restates a past as-traded bar, the same invariant the minute cache relies on). The only fetch path stays the canonical `PolygonClient.get_agg_range(timespan="day")` (default `adjusted=false`) in `_default_daily_bar_fetch` (`ladder_chart.py:664-677`) — it **never** touches the adjusted=true O'Neil `grouped_daily_history`.

**Mechanism — flip the gate in `_build_nonfrozen_payloads` (`ladder_chart.py:940-971`).** Replace the single blanket `context_allowed` computation (:946) with a **per-row `needs_fetch` predicate**, then pass `context_allowed = needs_fetch and (deadline is None or not deadline.should_stop())`:

- **FETCH** when: (a) the row is a **terminal that is not yet frozen** — it must fetch its one-time trailing 15-session band once, after which `_is_frozen_terminal_ok` (:702-749) freezes it (an ongoing prior never had a trailing band, so this is the single unavoidable maturation fetch); OR (b) the row is **ongoing with no usable prior OK-context payload** (brand-new arrival / first sight / self-heal of a blanked payload).
- **REUSE** (no fetch, no budget consumed — take the existing `daily_bar_fetch=None` + `reused_context_bars=...` branch, `_payload_for_row` :1079-1089) when the row is **ongoing with a usable prior**. "Usable prior" = parses AND `status == "OK"` AND `context == "OK"` (add a small `_prior_context_is_ok` twin of `_prior_payload_is_ok` :995-1003). **Key on prior `context == "OK"`, NOT on non-empty `bars`** — a delisted/empty-band ongoing ticker stamps `context == "OK"` (:540, empty fetch is not a failure) but is terminal-only-freeze so it never freezes (:724); keying on bars would re-fetch it forever, keying on context reuses it.

The reused lead-in comes from the prior payload; `_split_reused_context` (:471-497) already drops any reused bar now inside `[arrival, current_horizon]` so the **fresh minute fold wins** and the in-trade band still advances nightly (Polygon-free) even while daily fetches are zero.

**Deep-first-fetch refinement (kills the last latent re-fetch path — MANDATORY, not optional).** When a fetch *does* run, request the lead-in at **`LEAD_IN_CAP` (90) depth regardless of current hold**, so the reused band never needs deepening as `2·hold` grows. Thread a `deep_lead_in: bool` through `_payload_for_row` → `build_chart_payload` → `_resolve_context_bars` (:500-546) → `_context_bars` (:381-440); when set, compute `lead_in = LEAD_IN_CAP` instead of `_lead_in_sessions(hold_sessions)` (:400). `_context_bars` already issues **one** `get_agg_range` over the widened `[start, end]` (:427); 90 daily bars is the same single call (« Polygon's 50000 limit). Display may still clip to `min(90, max(20, 2·hold))`; simplest is to render what was fetched.

**Composition with #904 machinery — all preserved, unchanged:**
- **Frozen-skip** (`_is_frozen_terminal_ok` :702-749, `_chart_frame_payloads` :778-791): frozen terminals are still preserved verbatim and never enter `_build_nonfrozen_payloads`; an all-frozen file still skips the rewrite (:876-882).
- **Oldest-matured-first** (`_maturation_sort_key` :886-894, sort :937): still governs the residual fetching rows (new arrivals + maturations) so a long-held maturer's trailing band is never starved behind newer rows.
- **Anti-downgrade guard** (:957-968): untouched — a fresh non-OK build still preserves a prior OK string byte-identical; reuse rows go through the identical spliced-context path, so `CONTEXT_OK`/`reused`/`in_trade_only` semantics and terminal freezing are unchanged.
- **Marker core** is still rebuilt Polygon-free for **every** non-frozen row (the `needs_fetch` predicate gates only the cosmetic context branch, exactly as `context_allowed` does today) — no exit marker can ever be deferred.

Fix stays in the pipeline; Django only reads `chart_payload_json`.

## 3. Scope decisions

- **Reserve retune — YES, but staged as PR-2b (separate, revertible) with the premise corrected.** The naive framing ("shrink `_CHART_RESERVE_S` to hand wall-clock to the monitor") is a **verified category error**: the CLI gives the chart pass its own full-total deadline (`chart_deadline = _RunDeadline(total_s)`, `feedback.py:110/136`) and *withholds* the 15-min reserve from the **upstream trio** (`deadline = total − reserve`, :106-109); the monitor (`run_population_ladder_monitor(deadline=deadline)`) already stops on the **count cap** `_MAX_FETCHES_PER_RUN = 250` (`population_ladder_monitor.py:107`) — the comment states it replays "~31min of its 60min slice" (:105) with `~400` rows and `~40%` stale frontier (:101-102). So it is **count-starved, not time-starved**; shrinking the reserve alone is a no-op for it. The correct retune is two coupled edits: **(1) `_CHART_RESERVE_S_DEFAULT` 15min → ~5min floor** (:120) — the enabler; post-fix the chart needs < 20 fetches (< ~4min at 5 req/min), and #904 makes any deferral graceful (markers + in-trade candles are always Polygon-free), so a ~5min floor absorbs a spike night; do **not** zero it. **(2) `_MAX_FETCHES_PER_RUN` 250 → ~400** (:107) — the actual monitor lever, which needs the freed wall-clock (upstream slice 60→70min) and **must be validated against `TimeoutStartSec = 90min`** before locking. Sequence: land PR-2 first, let the cache warm 1-2 nights, confirm the journal fetch-count drop, then land PR-2b. The PR-2b body must state the count-vs-time reframe explicitly, or the change reads as doing nothing for the monitor.
- **UNAVAILABLE state — NO.** Zero capacity benefit. The delisted/empty case is already correct: an empty fetch stamps `CONTEXT_OK` and (for terminals) freezes (:428-433, :749), and refinement (b) above makes the ongoing case reuse without re-fetching. A new state ripples to the payload contract + Django read + SPA render + freeze gate, and if designed to "retry on relist" it **re-introduces the re-fetch-empty-forever cost #904 just killed**. File as a display ticket only if empty-context ever confuses users.
- **residual-23 — OUT.** Architecturally orthogonal and mechanically un-fixable by any daily/context cache. It is a missing in-trade **minute** session *inside* `[arrival, horizon]` (the minute cache trails maturity by one session; Polygon Basic serves only past-day minute aggs). The context band is **daily** bars strictly *outside* that window — it can never supply the minute session. Belongs in the minute-cache maturation seam, a separate PR.

## 4. Cost

| | Steady-state Polygon daily-context calls / night | One-time warm-up |
|---|---|---|
| **Before (post-#904)** | one `get_agg_range(day)` per non-frozen row; ~150-200 ongoing rows re-fetching an immutable lead-in nightly, budget-starved by the 15-min reserve (~75 slots at 5 req/min) so many thrash to `reused`/`in_trade_only` | — |
| **A3 reuse-first (chosen)** | **~0 marginal** — only new arrivals (~5-10) + trades maturing tonight (~few) = typically **< 20/night**; the ~194 ongoing rows drop to 0 (reuse from disk); terminals already 0 (frozen) | **transition night only**: each ongoing row lacks reuse eligibility and fetches once (~194, one-time), absorbed by the existing 75-min chart deadline over 1-2 nights |
| A1 naive (rejected) | ~150/night (lead-in front-extends every night, hold never hits cap) | ~194 |
| A2 / grouped-seed (rejected) | ~0-1/night | new timer + ~130-155-session deep seed near the entitlement cliff |

Cache footprint: **zero new files** — the band already lives in `chart_payload_json.bars`.

## 5. Test plan (unittest.TestCase, red-before-green)

Extend `apps/alphalens-research/tests/test_ladder_chart_payload.py`, reusing `_StubDeadline` (:1213) / `_BudgetDeadline` (:1229). New class `TestReuseFirstContextPolicy` (mirror `TestFreeTierAlwaysRebuiltPastDeadline` :1410 and `TestContextBudgetOrdering` :1711):

1. `test_established_ongoing_makes_zero_daily_fetches_with_budget_available` — ongoing row + prior OK-context payload, deadline **never stops** → assert the injected `daily_fetch` call count == **0** across N nights. *(The invariant that fails red today: budget-available always re-fetches.)*
2. `test_brand_new_ongoing_fetches_once_then_reuses` — first appearance fetches exactly once, subsequent nights fetch 0.
3. `test_first_fetch_captures_full_lead_in_cap` — the single fetch requests `LEAD_IN_CAP` depth; assert the persisted lead-in spans the full cap even at `hold≈0`, and the reused band still serves that depth after `2·hold` would have grown past the first-night value (no re-fetch).
4. `test_terminal_not_frozen_fetches_trailing_once_then_freezes` — maturing terminal fetches its trailing band once, then `_is_frozen_terminal_ok` freezes it (extends `TestFrozenGateRequiresFullContext` :1611).
5. `test_delisted_ongoing_reuses_and_never_refetches` — ongoing row whose prior payload has `context=="OK"` but an empty lead-in (delisted) → **reuse, 0 fetches** (guards "key on context, not bars").
6. `test_blanked_prior_self_heals_with_one_refetch` — a NO_DATA prior (no usable OK context) → exactly one re-fetch, then reuse.
7. `test_reuse_preserves_markers_when_minute_cache_transiently_empty` — anti-downgrade guard still holds on the reuse path (complements `TestAntiDowngradeGuard` :1572).
8. `test_reuse_fetch_is_adjusted_false_and_never_reads_oneil_store` — positive-control basis assertion: the fetch is `timespan="day"` adjusted=false and the code never references `grouped_daily_history`.

PR-2b: `test_chart_reserve_default_is_five_minutes` + `test_max_fetches_default_is_400` (constant pins), and a wall-clock note (validated on the VPS, not a unit test) that the enlarged monitor slice fits `TimeoutStartSec=90min`.

## 6. Risks / rollback + effort

- **Reuse chain-of-custody:** ongoing context depends on the prior payload persisting; a blanked payload loses context until one self-healing re-fetch. Bounded (not a leak), covered by test 6 + the #904 anti-downgrade guard.
- **Lead-in depth freeze:** resolved by the deep-first-fetch refinement (test 3); without it the band would freeze at the ~20 floor — a visible regression, so the refinement is **mandatory**.
- **Transition-night spike:** ~194 one-time fetches the first night; the existing 75-min chart deadline absorbs it over 1-2 nights. Land PR-2 first and let it stabilize one full cycle **before** PR-2b shrinks the reserve.
- **PR-2b count-cap raise** is the one part needing empirical wall-clock validation against `TimeoutStartSec=90min`; ship it separately-revertible.
- **Rollback:** PR-2 is a localized predicate change in one seam (`_build_nonfrozen_payloads`) plus a threaded flag — revert restores the blanket `context_allowed` gate with no data migration (the payload column shape is unchanged). PR-2b is two constant edits.
- **Effort: S-M** for PR-2 (predicate + deep-fetch flag + tests); **S** for PR-2b (two constants + VPS validation). **Two PRs.** Zen deepseek-v4-pro `thinking_mode=high` pre-merge review is mandatory for both — shared feedback pipeline surface.

## 7. Provenance

Scope + approach decided via a multi-agent workflow (4 analysis lenses → 3 adversarial judges → deciding architect). The panel corrected two initial framings: (1) "reuse the monitor's existing grouped cache" is fatal (no lead-in depth), and (2) the reserve retune is a count-cap problem, not a wall-clock problem. Superseded candidate A1/A2 kept above under "Why NOT" for the audit trail.
