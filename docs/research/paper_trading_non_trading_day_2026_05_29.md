# Paper trading on non-trading days — design memo

**Status:** PARTIAL (PR-A shipped 2026-05-29; PR-B/C/D queued).
**Author:** Kamil Pająk
**Companion code:** `apps/alphalens-pipeline/alphalens_pipeline/paper/calendar.py`,
guards in `apps/alphalens-pipeline/alphalens_cli/commands/paper.py`.

## 1. Problem statement

The thematic-build timer fires daily at 06:30 UTC; news ingest and brief
generation run regardless of NYSE state (news doesn't stop on weekends).
The paper-trade harness inherited this calendar-day cadence:

* `paper plan` writes PLANNED rows for every brief.
* `paper submit` pushes GTC limit orders to Alpaca on any day.
* `paper reconcile` polls Alpaca state on any day.
* Entry-TTL and time-stop are measured in **calendar days**
  (`reconciler.py:244`, `exit_manager.py:432`).

Three observed failure modes:

* **P1 — Stale-ladder gap risk on weekends.** A brief generated Sat
  uses Fri-close prices. The trade-setup ladder (`E0`/`E1`/`E2`) is
  anchored to Fri-close. If `submit` runs Sat, the GTC ladder is
  queued for Mon open. Weekend news producing a gap-down through E2
  fills the entire ladder at prices the ladder's pull-back logic never
  agreed to commit at.

* **P2 — Calendar-day TTL/time-stop drift.** The trade-setup memo
  reads "N days" as **trading days** (5d/wk). Calendar-day math
  inflates the fill window by ~28.5% on average and worse around long
  weekends, breaking parity with the documented intent.

* **P3 — Wasted Alpaca polling.** Reconciling on a Saturday issues
  ~100 GETs that always no-op (Alpaca state is frozen since Fri close).

## 2. External research (Perplexity, 2026-05-29)

Read transcript: `tmp` (not committed). High-signal kernels:

* **No queue-priority advantage for pre-open submission.** NYSE
  Opening Cross (Rule 7.35) is a single-price auction at 09:30 ET; all
  pre-market orders enter the auction together regardless of submit
  time. Saturday and Monday 09:35 are mechanically equivalent on
  match priority. The "operator-races" concern from the initial
  scoping is unfounded.
* **Trading days > calendar days for event-drift windows.** PEAD
  (Bernard-Thomas), news-momentum (Chan-Jegadeesh-Lakonishok 1996),
  and time-series momentum (Moskowitz-Ooi-Pedersen 2012) literature
  define holding periods and event windows exclusively in trading
  days.
* **`exchange_calendars` is the current Python default** (Quantopian
  successor, maintained under gerrymanoim). `pandas_market_calendars`
  is still functional but lags on half-day intraday precision.

Perplexity also recommended deferring submission to ~09:45 ET on
liquidity-quality grounds (wide opening-15min spreads). That advice
is **wrong for limit-order pull-back strategies**: limits don't pay
the spread, and the opening cross gives passive buyers price
improvement when the cross prints below the limit. We want our
ladder *working* during the opening auction, not waiting for the
shakeout to settle. See §4.

## 3. Design decisions

| Decision | Choice | Rationale |
|---|---|---|
| Brief generation cadence | unchanged (daily) | News doesn't stop; weekend brief = research reading material. |
| `paper plan` cadence | unchanged (daily) | Plan = research artefact, decoupled from market action. |
| `paper submit` on non-trading days | **skip** with deferral message | P1. Re-anchor ladder on next session's brief. |
| `paper reconcile` on non-trading days | **skip** with deferral message | P3. State cannot have changed since previous close. |
| Operator override | `--allow-closed-market` flag (default off) | Manual ad-hoc work (smoke tests, post-incident sweeps). |
| Entry-TTL units | **trading days** (PR-B) | P2. Aligns with trade-setup memo intent. |
| Time-stop units | **trading days** (PR-B) | Internal consistency over PEAD-attention-decay analogy. |
| Submit timing | participate in opening cross (PR-D) | Limit orders + opening auction = price improvement; opening volatility *is* the entry. See §4. |
| Calendar library | `exchange_calendars` (XNYS default) | Maintained, half-day precision, multi-exchange API. |
| Multi-exchange future | API parameterised on MIC code | Polish (XWAR), Tokyo (XTKS), HKG (XHKG) become per-call argument changes, not refactors. |

## 4. Opening shakeout — feature, not bug

The "avoid opening volatility" literature (Madhavan, Hasbrouck on
effective-spread widening in first 15 min) applies to **market-order
execution strategies** where the taker eats the spread. The pull-back
ladder strategy is the opposite — it's a **passive limit-order
provider** placed at prices the strategy proactively chose. Three
reasons to participate in the opening auction:

1. **Limits don't pay the spread.** A buy limit at $48.50 fills at
   $48.50 regardless of bid-ask width around that price.
2. **Opening Cross gives passive buyers price improvement.** If the
   opening print clears at $47.80 with our buy limit at $48.50, we
   fill at $47.80 — better than our limit. Deferring to 09:45 throws
   away this discount.
3. **The wicks ARE the entry.** Pull-back limits at -1% / -2.5% / -4%
   are designed precisely to catch panic-driven overshoots that
   reverse within the first 15-30 min. Deferring lets the price
   retrace away from E1/E2 before our orders are working.

Open practical question (resolved in PR-D research): how does Alpaca
paper route `tif="gtc"` orders submitted pre-open into the NYSE
opening cross? If GTC bypasses the cross, we may need `tif="opg"`
(on-the-open) for the first day of a freshly-submitted ladder.

The "thin small-cap" risk (LULD halts, wide opening auction matches
for very illiquid names) is real but defers to a per-ticker
liquidity-floor guard — not the universal "defer to 09:45" rule.

## 5. PR sequencing

* **PR-A (this PR) — calendar module + CLI guards.** Adds
  `paper/calendar.py` (multi-exchange XNYS / XWAR / etc), introduces
  the market-closed guard on `submit` and `reconcile` (default on,
  `--allow-closed-market` opt-out). No behaviour change to TTL or
  time-stop math; legacy `(observed.date() - planned.date()).days`
  stays in `reconciler.py` until PR-B.
* **PR-B (#302) — switch TTL + time-stop to trading days. SHIPPED 2026-05-29.**
  Replaced `.days` arithmetic in `reconciler.py::_sweep_expired_entries`
  and `exit_manager.py::_time_stop_should_fire` with `trading_days_elapsed`.
  Updated `constants.py`: `DEFAULT_ORDER_TTL_DAYS` 10 → 7 trading days,
  `TIME_STOP_DAYS` 60 → 42 trading days. Updated
  `thematic_trade_setup_v1_design_2026_05_27.md` and
  `paper_trading_capital_sizing_2026_05_28.md` to read "trading days"
  explicitly. `reconcile_orders` gained an optional `observed_at`
  parameter for test determinism.
* **PR-C — SPA banner.** `BriefHeader.svelte` reads a new
  `/v1/market/status` endpoint (returns `is_trading_day`,
  `is_half_day`, `next_open_iso`) and renders a persistent banner on
  closed days: "Market closed. Submission deferred until Mon
  2026-06-01 09:30 ET (in 1h 24m). Ladder based on Fri close."
* **PR-D — submit cadence + opening-cross research.** Adds VPS
  systemd timer that fires submit + reconcile on weekday market open;
  one-shot Alpaca research note documenting how `tif="gtc"`
  participates (or doesn't) in the opening cross + decides whether to
  default to `tif="opg"` for the first day.

Track F in `project_alphalens_ideal_shape_2026_05_29.md` consumes
PR-D output for the cron migration.

## 5.1 Weekend news flow into Monday brief (not changing)

Question that comes up adjacent to "should we skip submit on weekends":
**are weekend news visible to Monday's brief?** Yes, by construction —
no PR needed:

1. `alphalens-thematic-build.timer` is `OnCalendar=*-*-* 06:30 UTC` —
   daily, with no weekday gate. Sat 06:30 and Sun 06:30 still fire.
2. `news_ingest` writes `~/.alphalens/thematic_news/{YYYY-MM-DD}.parquet`
   per calendar day. Polygon news / GDELT / RSS all return weekend
   items unchanged.
3. `extract` parses each daily parquet into the `thematic_events`
   cache, also keyed by calendar day.
4. `mapping/catalyst_resolver.py::_load_window` reads a `DEFAULT_LOOKBACK_DAYS = 30`
   window of cached events at `asof`. Monday's `asof=Mon` lookup loads
   files for `[Mon-30d, Mon]` — Sat and Sun parquets are included.

So a Saturday-released M&A leak surfaces in Saturday's brief (visible
in SPA over the weekend for cherry-picking) AND propagates into
Sunday's + Monday's briefs through the 30-day lookback.

Known limitations (not in scope for PR-A; planned follow-ups):

* **No gap-detection on the ingest cache (HIGH). PR-E SHIPPED 2026-05-29.**
  Same failure mode as PR #259's stale pipeline image (a day of
  GDELT-padded titles shipped before anyone noticed). Shipped:
  `alphalens thematic verify-cache --days N [--alert]` command +
  `alphalens_pipeline.thematic.verify_cache` module. Wired as a
  pipeline-image `ExecStartPost=` slot in
  `alphalens-thematic-build.service` BEFORE the Django `rebuild-cache`
  step — a missing-day digest halts the chain so partial data never
  hits Django. Distinguishes "no-news-day (0-row parquet)" from
  "missing-day (no parquet at all)" — only the latter alerts. Telegram
  digest dispatched via the existing `TelegramHandler` when both
  `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set.
* **Cross-day dedup not run (LOW).** Tier 1 clustering operates
  per-day inside `news_ingest`; an article published Sat then
  re-syndicated Mon shows up twice in the 30-day lookback.
  Polygon's per-URL dedup catches the bulk; GDELT / RSS residuals
  are marginal noise in event counts, not brief outcomes.
  **Defer:** re-evaluate if 2+ duplicate candidate cards observed in
  production briefs (cross-day Tier 1 clustering would move into
  `catalyst_resolver._load_window`).
* **Ingest latency on weekends (MEDIUM).** Sat 06:30 UTC = 02:30 ET
  — before any Saturday US news cycle. Sat-afternoon ET news lands
  in Sun's parquet, not Sat's. Effectively the Saturday brief misses
  a full ET day of news (Sat 02:30 onward). Monday's brief still
  sees everything through the 30-day lookback, so the impact is on
  the Saturday/Sunday SPA read-experience, not the Monday execution
  path. **Planned PR-F:** 4×/day pipeline cadence (06:30 / 12:30 /
  18:30 / 23:30 UTC) per Track F in
  `project_alphalens_ideal_shape_2026_05_29.md`. Polygon Starter
  rate limit (15 req/min, ~21.6 K/day) gates this; verify quota
  budget before PR-F.

## 6. Multi-exchange notes (forward-looking)

The user has flagged future expansion to Polish (XWAR) and Asian
(XTKS / XHKG / XSHG) markets. `paper/calendar.py` exposes all helpers
as `helper(d, exchange="XNYS")` so adding a venue is a per-call
argument change. Plug-in points beyond the calendar:

* **Per-ticker exchange routing.** Today `planner.py` implicitly
  assumes every ticker trades on XNYS. Future: a `ticker → exchange`
  map (probably keyed off the brief's ticker metadata).
* **Per-exchange broker client.** Alpaca is US-equities-only. XWAR
  routes through IBKR / DM BOŚ; Tokyo through IBKR / SBI; etc. The
  `get_default_alpaca_client` indirection is the seam.
* **Per-exchange FX.** Sizing math in `planner.py` is currency-agnostic
  today (USD implicit). PLN / JPY / HKD positions need an FX leg in
  the gross-guard math.

The calendar module is the smallest of those three layers; pinning
the parametric API now keeps the next two refactors honest.

## 7. Test coverage

* `apps/alphalens-research/tests/paper/test_calendar.py` — 32 tests
  covering is_trading_day, is_half_day, next_trading_open,
  previous_trading_day, trading_days_elapsed across XNYS + XWAR.
* `apps/alphalens-research/tests/paper/test_cli_market_closed_guard.py`
  — 8 tests: submit / reconcile skip on weekend, holiday, with /
  without `--allow-closed-market`, with proceed-on-trading-day
  positive control.

## 8. Edits log

* **2026-05-29 — initial PR-A draft (this commit).** Calendar module +
  CLI guards land. PR-B/C/D queued.
