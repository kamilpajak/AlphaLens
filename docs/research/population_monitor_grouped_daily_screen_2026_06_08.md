# Population monitor — grouped-daily two-tier fetch screen

**Status:** LOCKED (adversarial-reviewed 2026-06-08; refinements R1-R7 in §9)
**Date:** 2026-06-08
**Author:** backend
**Problem owner:** the `/edge` "ongoing [156]" backlog — most ongoing rows show no status (PENDING) because the nightly population-ladder monitor cannot price them.

## 1. Problem

`alphalens_pipeline/feedback/population_ladder_monitor.py` replays every brief
candidate's trade ladder nightly against Polygon **minute** bars. Mechanics
(verified in code):

- **One minute fetch per ongoing candidate per night** (`_extend_bar_cache`
  tail-fetches new bars; terminal rows are frozen, 0 fetches).
- Polygon **Basic (free) = 5 req/min** → `PolygonClient` throttles 13 s/call.
- `_MAX_FETCHES_PER_RUN = 150` (a single budget shared across the whole run) fits
  ~150 fetches in the 45-min `TimeoutStartSec`.

Two independent failures:

1. **Throughput ceiling.** With 5 req/min and an ongoing population already > 150
   and growing (~6× briefs/day), the run physically cannot refresh everyone in one
   night. Deferred candidates carry a blank classification → the `/edge` PENDING
   rows.
2. **Starvation.** Candidates are processed in fixed parquet order, newest brief
   date first; the global 150 budget is consumed by the newest dates, so the same
   older tail is deferred **every** night (no least-recently-priced rotation).

Decision (user, 2026-06-08): stay on the **free** Polygon tier and solve it with
engineering rather than a paid plan. ($29/mo Starter = unlimited calls was the
alternative; rejected to keep $0.)

## 2. Key external facts (Polygon / Massive, verified 2026-06-08 via Perplexity)

- **Grouped Daily** `GET /v2/aggs/grouped/locale/us/market/stocks/{date}` returns,
  in **ONE call**, the daily OHLCV+VWAP (`t,o,h,l,c,v,vw`) for the **entire** US
  equity market for that date. `adjusted` defaults **true** (split-adjusted) →
  we MUST pass `adjusted=false` to match the existing raw minute bars and the
  absolute ladder levels. `include_otc` defaults false (fine). Stable by evening
  after the close (continuously recalculated; our 06:30 UTC run is well after).
- A 60-trading-day minute window ≈ 23 400 bars < the 50 000/page limit → one
  minute call still covers a full hold window.

## 3. Core idea — two-tier fetch

Replace "minute-fetch every ongoing candidate every night" with:

- **Tier 1 (cheap daily screen, O(days) not O(candidates)):** fetch grouped-daily
  ONCE per needed trading date (covers ALL candidates at once), cache it date-keyed.
  Use each candidate's new-day daily High/Low to decide whether anything could
  have changed.
- **Tier 2 (precise minute resolve, only when needed):** do today's incremental
  minute fetch + `replay_ladder` ONLY for candidates whose daily screen says a
  ladder level may have been touched (or a time event fired, or a split, or a
  brand-new / partial-state row). Everyone else is updated cheaply from the daily
  close — no minute fetch.

The number of minute fetches per night drops from "every ongoing candidate" to
"candidates with an actual level touch on a new day" — typically a small minority.
Grouped-daily adds only O(new trading days) calls (a handful nightly; ≤ ~75 after
long downtime), trivially within 5 req/min. **This also dissolves starvation:**
the daily screen runs for everyone (it is not budget-bounded), so no candidate is
left blank.

## 4. Correctness contract (THE part to attack)

**Invariant.** Between two consecutive ladder-level touches, an ongoing position's
`classification`, `entries_filled` and `tps_hit` are INVARIANT; only the
mark-to-market (`open_r`, `forward_return`, `holding_days_elapsed`) moves with the
latest close. Therefore a trading day whose `[low, high]` contains **no** relevant
ladder level cannot change the classification, and the mark can be advanced from
the daily close alone.

**Relevant levels per prior state** (a "touch" = the day's `[low, high]` reaches one):
| Prior state | Lower levels (low ≤ ?) | Upper levels (high ≥ ?) |
|---|---|---|
| `NO_FILL` (entry window still open) | any entry-tier limit price | — |
| `OPEN` (filled, 0 TP) | `disaster_stop` | lowest **un-hit** TP price |
| `PARTIAL_TP_OPEN` | (always resolve — see below) | (always resolve) |

`needs_minute_resolve` = OR of:
1. **brand-new** row (no prior) — must establish `reference_close` (arrival VWAP,
   minute-only) + initial fills.
2. prior `classification == PARTIAL_TP_OPEN` — its mark mixes realized tranches +
   open remainder; not reconstructible from stored scalars. Rare (~6%); resolve.
3. **level touch** on any new day for the prior state (table above).
4. **position-expiry (time-stop)** session falls in the new-day range — TIME_STOP
   marks the remainder to the *expiry bar's* close, which needs the precise
   minute close, so resolve (one-time, then terminal/frozen).
5. **split-class day**: any new day with `|c/prev_c − 1| > _SPLIT_SCREEN_THRESHOLD`
   (0.18, R4) → resolve. The 0.18 screen catches common splits (2:1=0.5, 3:2=0.5,
   5:4=0.25, 1.25:1=0.20 and their reverses) and funnels into the carry guard so an
   unadjusted split never corrupts `open_r`. The cheap OPEN path carries the SAME
   guard (R4), so it is no longer less protected than the resolve path. A true
   corporate-actions feed is an explicit out-of-scope follow-up — the 0.18 screen +
   level-touch resolve + carry guard are the in-PR defence, not a precise split
   model. When `prev_c` is unavailable from cache (and the prior row's `last_close`
   is also missing) the screen FAILS CLOSED (resolve).

**Cheap (no-resolve) path** updates from the latest new daily close `c*`:
- `last_close ← c*`.
- **NO_FILL + entry window now closed** (`entry_expiry_session ≤ last_closed_session`)
  and no fill touched → flip to **terminal NO_FILL** (deterministic; today's
  `_terminal_row` does the same flip — no fetch needed).
- **OPEN** (0 TP): `open_r = (c* − blended_entry) / (blended_entry − disaster_stop)`.
  This is EXACT, not approximate: with no TP hit, `replay_ladder` marks the whole
  filled position to last_close, i.e. `_realized_r_with_frac` returns
  `(last_close − blended)/risk` — identical. `forward_return = (c* − reference_close)/reference_close`
  uses the STORED (frozen, minute-only) `reference_close` — NEVER grouped `vw`.
  `holding_days_elapsed` recomputed from first-fill → last session.
  `open_return_pct_of_book = open_r × realized_risk_pct` is RECOMPUTED from the
  refreshed `open_r` (it moves with the mark, NOT carried).
- GUARD: when `|c*/last_close_prev − 1| > _SPLIT_SCREEN_THRESHOLD` the cheap path is
  skipped and the prior row is carried verbatim (R4).
- **Unchanged** (carried from last resolve): `classification`, `entries_filled`,
  `tps_hit`, `blended_entry`, `sl_hit`, `mfe/mae(_pct)`, `sequence_str`,
  `ratchet_realized_r`, `reference_close`, and all `_SIZE_COLUMNS` EXCEPT
  `open_return_pct_of_book` (which moves to the cheap-update set above).

**Why deferring the minute fetch loses no data.** The per-(ticker, arrival) minute
cache is incremental + forward-only. When a resolve is finally needed (a touch,
time-stop, or maturity), the tail-fetch pulls **every** minute since the last
cache point, and `replay_ladder` runs over the COMPLETE minute history → mfe / mae
/ sequence are exact for the whole window at that moment. So mfe/mae are only
stale during the quiet no-touch interim and become exact at every resolve,
including the terminal one. The headline `open_r` stays exact throughout (daily
close mark). This is the explicit, bounded substrate-staleness tradeoff.

## 5. Fairness (starvation fix), independent of the screen

Add `last_priced_session` to each row. The minute-resolve queue (only the touch
set) is still capped by `_MAX_FETCHES_PER_RUN`, but candidates are ordered
**least-recently-priced first**, so a one-night flood (e.g. market-wide SL on a
crash day) drains fairly over a couple of nights instead of starving a fixed tail.
After a flood, the touched names resolve to terminal (SL_HIT) and freeze, so the
queue self-drains. The daily screen itself is never budget-bounded.

## 6. Surfaces / files

- `polygon_client.py`: add `get_grouped_daily(date, *, adjusted=False, include_otc=False)`
  → `{TICKER: {t,o,h,l,c,v,vw}}` (one canonical-client method; no shadow HTTP).
- `population_ladder_monitor.py`:
  - grouped-daily cache (date-keyed parquet under `population_ladders/grouped/`),
    prefetched once per run for the union of needed new sessions.
  - `_screen_candidate(prior, new_daily_bars, cutoffs, last_closed_session)` →
    `(needs_resolve: bool, cheap_row | None)`.
  - wire into `_candidate_row`: screen first; resolve only on demand; cheap-update
    otherwise; least-recently-priced ordering for the bounded resolve set.
  - add `last_priced_session` column; back-fill None on carry (schema-stable like
    `_SIZE_COLUMNS`).

## 7. Out of scope / deferrals

- Django/edge ingest + web already handle the columns (PENDING UI shipped #488);
  no API change needed — this only changes how rows get *populated*.
- mfe/mae interim staleness on long-quiet positions (documented §4).
- A market-wide flood still bounded by the minute budget for one night (fairness
  drains it; never starves).

## 8. Test plan (TDD)

- `get_grouped_daily`: shape, adjusted=false param, pagination/empty.
- `_screen_candidate`: each state × {no-touch, lower-touch, upper-touch,
  entry-expiry-flip, position-expiry, split-day, brand-new, partial} → correct
  resolve/cheap decision + exact OPEN open_r vs a reference `replay_ladder` run
  (property: cheap open_r == full-replay open_r when no level touched).
- Monitor integration: a no-touch night issues 0 minute fetches but advances
  open_r/forward_return; a touch night resolves precisely; least-recently-priced
  ordering under a tight budget; grouped-daily cache reuse.

## 9. Refinements R1-R7 (adversarial-review hardening, 2026-06-08)

The first eight sections describe the screen; the four-lens adversarial review
surfaced seven correctness refinements that the locked implementation carries.

**R1 — RTH alignment (the structural fix).** The minute replay and the
grouped-daily screen originally lived in DIFFERENT price domains: the minute
window ran 09:30 ET..+480 min (= 17:30 ET, 90 min into post-market) and
`get_agg_range` sent no session filter, so it included pre/post-market prints;
grouped-daily H/L is regular-session only. That mismatch was the root of four
distinct findings (after-hours stop false-negative, pre-market entry
false-negative, daily-vs-minute boundary disagreement, adjusted-mismatch family).
Fix: align the two domains at the source by making the minute replay **RTH-only**.
(1) Replace the flat `_HORIZON_SESSION_SPAN_MIN = 480` with a per-session RTH span
(`_session_rth_span_min` — 390 regular, the calendar's actual open→close for
half-days). (2) `_filter_bars_to_rth` drops every bar outside `[session_open,
session_close]` of each session the window spans, applied inside `_replay_candidate`
after the tail-fetch and before `replay_ladder`. Result: grouped-daily `[low,high]`
is a TRUE superset of the minute path, so a no-touch screen can never silently drop
an after-hours stop or a pre-market entry. **Pre/post-market fills are deliberately
NOT modelled** — resting-limit geometry is an RTH construct. The cache still stores
every fetched bar (a faithful raw record); RTH filtering is a replay-time view.

**R2 — eps touch band.** A small relative guard band `_TOUCH_EPS = 0.0025` (25 bps)
on the touch gate: a level is touched when `daily_low ≤ level*(1+eps)` (lower) or
`daily_high ≥ level*(1−eps)` (upper). Absorbs residual consolidated-vs-primary /
odd-lot endpoint disagreement so a daily low one tick above the stop (true minute
low one tick below) still resolves to SL_HIT. Conservative (bias toward resolving).

**R3 — missing-bar fail-closed.** Any required NEW session absent from its complete
grouped map for the ticker (halt / illiquid / upstream gap) → resolve. The cheap
path NEVER advances `open_r`/`last_close`/`forward_return` from a missing or phantom
close. A grouped fetch is issued only for calendar-confirmed sessions; a 0-result
on a confirmed session is an upstream gap (not cached, not screened — carry/resolve),
distinct from a complete cached session where a ticker is simply absent (resolve).

**R4 — split threshold 0.18 + cheap-path guard.** The split screen is keyed on
`|c*/prev_c − 1| > _SPLIT_SCREEN_THRESHOLD` (0.18, lowered from 0.60) so a 2:1 split
forces a carry-guarded resolve rather than corrupting `open_r`. The cheap OPEN path
carries the SAME guard on `|c*/last_close − 1|`, so it is no longer less protected
than the resolve path. A true corporate-actions feed remains out of scope.

**R5 — un-filled entry-tier touch extension.** The OPEN/NO_FILL lower-touch test
covers the disaster_stop OR **any un-filled entry-tier limit while the entry window
is still open** (`entry_expiry_session > last_priced_session`); once the window
closes those tiers drop out. This prevents a new daily low that reaches E2/E3 (but
not the stop or a TP) from being cheap-marked against a stale blended_entry /
filled_fraction — the only path that could change fills without a "touch".

**R6 — stored `reference_close` + `last_priced_session`.** Two STRICTLY-ADDITIVE
columns (back-filled to None on a carried old-format row, same discipline as
`_SIZE_COLUMNS`). `reference_close` is the arrival-window VWAP written at the
brand-new minute resolve and then FROZEN (never grouped `vw`, which is the wrong
window); the cheap path recomputes `forward_return` from it with ZERO minute access.
`last_priced_session` is the latest priced session — it drives the fair ordering and
the R7 periodic resolve.

**R7 — periodic forced resolve (K=5, budget-exempt, precedence over cheap flips).**
A candidate is force-resolved whenever `last_priced_session` is more than
`_PERIODIC_RESOLVE_SESSIONS = 5` sessions behind `last_closed_session` (defence
against endpoint drift and any future domain skew). The forced resolve (a) takes
PRECEDENCE over every cheap terminal flip — if the periodic trigger fires, the cheap
NO_FILL-terminal flip is NOT taken that night; (b) is EXEMPT from the main
`_MAX_FETCHES_PER_RUN` budget via a separate reserved `_FORCED_RESOLVE_BUDGET` so a
flood night cannot starve it. K=5 = max one trading week of mis-classification
latency against ~5 extra fetches per long-quiet candidate per week — negligible
versus the 5 req/min budget once the cheap path eliminates nearly all fetches.

**Fair ordering (starvation fix, §5 made precise).** The bounded minute-resolve set
is ordered (deferred-touch oldest `last_priced_session` first, then brand-new, then
least-recently-priced), so a touched-but-deferred OPEN row cannot be overtaken
indefinitely by fresh brand-new rows. Forced (R7 + brand-new) items draw from the
reserved sub-budget. A crash night on top of a full brief-inflow day drains all
touches within a couple of nights; the report surfaces `resolve_queue_depth` +
`oldest_deferred_touch_age` for a dead-man-switch.
