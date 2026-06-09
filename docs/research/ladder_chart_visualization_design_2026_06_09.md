# Ladder-chart visualization — design memo

**Status:** DRAFT
**Date:** 2026-06-09
**Surface:** `apps/web` (SvelteKit SPA) + `apps/alphalens-django` (edge API)
**Related:** [[project_population_ladder_monitor_2026_06_03]], [[project_edge_dashboard_p1_2026_06_04]], `docs/research/feedback_edge_dashboard_2026_06_04.md`

---

## 1. Problem

Each stock recommendation carries a deterministic, broker-free **ladder replay** — an
entry limit-ladder, three take-profit tranches, and one disaster stop, replayed over
historical price bars (`alphalens_pipeline/feedback/ladder_replay.py`). Today this is
shown only as numbers: the `trade.execution.setup` panel on the brief (planned geometry)
and the `/edge` outcomes table (realized R, `% book`, classification). There is no way to
*see* where the modeled entries, take-profits, and stop sit on the actual price tape.

Goal: a candlestick chart per recommendation with the entry / take-profit / stop levels
drawn as price lines and the modeled fills / exits drawn as markers — so a user can read
"the plan" on the brief and "what happened" on the EDGE review.

**Non-goal:** a real-time trading chart, a drawing/indicator platform, or anything that
implies real broker executions. There is no broker (ADR 0012); every marker is a *modeled*
fill from bar-replay, and the UI must say so.

## 2. Decision summary (approved 2026-06-09)

| # | Decision | Choice |
|---|----------|--------|
| D1 | Charting library | **TradingView Lightweight Charts** (Apache-2.0, self-hosted) — NOT the embeddable widget, NOT the full Charting Library |
| D2 | One component, two surfaces | `LadderChart.svelte` rendered on the brief (planned, no markers) and on `/edge` (replayed, with markers) |
| D3 | Brief integration | a `numbers ⇄ tape` toggle inside the existing `trade.execution.setup` panel — same data, two views, no new box |
| D4 | `/edge` integration | **inline accordion** — the outcome row expands in place to a full-width chart panel (NOT a modal, NOT a drawer) |
| D5 | Default granularity | **daily** candles; optional **intraday** (minute) zoom for the trade window |
| D6 | Marker time mapping | daily: marker on the *fill date* bar, exact intraday time in the tooltip; intraday: marker snapped to the exact minute bar |
| D7 | Honesty | persistent `SIM · modeled fills` chip, hollow/dashed markers + dashed price lines, an `(i) how?` popover with data source + RTH caveat + intrabar rule |

## 3. Why Lightweight Charts (D1)

Confirmed by an adversarial Perplexity review (2026-06-09, full notes in §9):

- **Embeddable widget is disqualified** — it pulls data from TradingView's servers (not our
  Polygon cache) and cannot place arbitrary custom buy/sell markers at a chosen price/time.
  It also carries TV branding + ToS constraints awkward for an auth-gated tool.
- **Full Charting Library is overkill** — requires application/approval + a datafeed adapter,
  and they are picky about research/trading-platform use cases.
- **Lightweight Charts fits exactly:** canvas-based, finance-native (candles, time scale, price
  scale, crosshair), self-hosted, no watermark, no network dependency. The two primitives we
  need are first-class:
  - `series.setMarkers([...])` — arrows (`arrowUp`/`arrowDown`/`circle`) with `position`
    (`aboveBar`/`belowBar`/`inBar`), `color`, and a short `text` label (`E1`, `TP1`, `SL`).
  - `series.createPriceLine({ price, color, lineStyle, title, axisLabelVisible })` — horizontal
    labeled levels for entry / each TP / stop.
  - No hard limits on marker/line count; our scale (<10 markers, <5 lines, ≤~16k minute bars
    for a 42-day window) is trivial.
- **Licensing:** Apache-2.0 — include the LICENSE/NOTICE text in the repo; **no on-screen
  attribution is required**. Fine for a private dashboard. (This corrects an earlier assumption
  that a visible logo/link was needed.)

Alternatives sanity-checked and rejected: Highcharts Stock (non-free for a buy-side tool),
ECharts / KLineCharts / ApexCharts (viable but not better for "draw trades on candles", and
heavier or less finance-native).

## 4. UI design

### 4.1 Two surfaces, one component

`LadderChart.svelte` takes the ladder geometry + optional replay outcome and renders the same
chart with different data completeness:

| Surface | Trade state | Draws | 3-tier role |
|---------|-------------|-------|-------------|
| `/brief/[date]` card | planned, no outcome | candles + entry/TP/stop price lines, **no fill markers** | L2 — "the plan on the tape" |
| `/edge` expanded row | replayed | the same lines **+ fill/exit markers** + outcome chip (class, R, `% book`) | L3 — "how the plan played out" |

This gives visual continuity "plan → outcome" and reuses the established L2-brief / L3-review
split.

### 4.2 Brief: `numbers ⇄ tape` toggle (D3)

The `trade.execution.setup` panel gains a segmented toggle (cyan, top-right next to the `ttl`
chip). The same `entry_tiers` / `disaster_stop` / TP data renders either as the current
numeric grid (`numbers`) or as the chart (`tape`). No second panel — a flip in place. Default
view stays `numbers` (the toggle is opt-in so the brief stays scannable).

### 4.3 EDGE: inline accordion (D4)

The outcome row becomes clickable (leading chevron). Click → expands **below the row**, full
table width, in a `border-grid` panel matching the TradeSetup box. Inline beats a modal/drawer
here: it keeps the row in context, fits the dense terminal layout, and multiple rows can be
expanded at once. Lazy-mount: the chart only instantiates when a row is expanded.

```
┌─ /edge ─ outcomes ──────────────────────────────────────────────────────┐
│ ▸ CRUS   [TP_FULL]   +4.1% ▓▓▓░    1d    +0.08%   consumer_electronics    │
│ ▾ SYNA   [TP_FULL]   +2.3% ▓▓░░    3d    +0.05%   semiconductors          │
│   ┌───────────────────────────────────────────────────────────────────┐ │
│   │ ladder.replay                    [TP_FULL]  R +0.23  %book +0.05    │ │  cyan label + outcome chip
│   │ ⌖ SIM · modeled fills              ┌ daily ┊ intraday ┐   (i) how?   │ │  honesty chip + toggle + popover
│   │                                                                     │ │
│   │   ╎                              ◢TP2  ····· tp ···· (green dashed)  │ │
│   │   ╎          ▕█▏  ▕█▏      ◢TP1  ▕█▏                                 │ │
│   │   ╎    ▕█▏ ▕█▏  ▕█▏  ▕█▏ ▕█▏  ▕█▏       ····· entry ··· (cyan dashed)│ │
│   │  E1▲ ▕█▏                                                            │ │
│   │   ╎                                     ····· stop ···· (red dashed)│ │
│   │   └─────────────────────────────────────────────────────────────  │ │
│   │   RTH-only modeled · overnight/pre-market moves not seen            │ │  footnote, fg-muted
│   └───────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────┘
```

### 4.4 Chart anatomy (token mapping)

- **Candles** — daily default (D5).
- **Price lines** (`createPriceLine`): entry / blended entry — `cyan` dashed, label `entry`;
  tp1/2/3 — `green` dashed, labels `tp1/2/3`; disaster stop — `red` dashed, label `stop`.
- **Markers** (`setMarkers`): entries `E1/E2/E3` — cyan `arrowUp` belowBar; TP hits — green
  `arrowDown` aboveBar `TP1..`; SL — red `arrowDown` belowBar `SL`. Modeled markers use a
  **hollow / dashed** treatment to distinguish them from any future real executions.
- **`⌖ SIM · modeled fills` chip** — persistent corner label (reuses the crosshair-watermark
  motif from TradeSetup).
- **`(i) how?` popover** (JargonTip-style): data source; RTH-only / Polygon-free limit;
  intrabar **SL-first** rule; and, when `ambiguous_bars > 0`, a note that some bars touched
  both TP and SL and were resolved SL-first (we already carry `same_bar_ambiguous` /
  `ambiguous_bars` from the replay — surface it honestly rather than implying certainty).

### 4.5 States (reuse existing patterns)

- **ONGOING / PENDING** — no exit marker; entry markers + an "open" cyan dot on the last bar +
  an MFE/MAE excursion band; the unfilled TP/stop lines stay drawn (still the plan). Same badge
  treatment as the current `/edge` ongoing rows.
- **NO_STRUCTURE** — reuse the existing dotted-border "no structured ladder" box; **no chart**.
- **NO_DATA** (Polygon gap) — same dotted-border box: "no bars for this window (RTH-only /
  Polygon free)".

### 4.6 Interaction

- **daily ⇄ intraday** segmented control (D5/D6). Daily: marker sits on the fill *date* bar,
  exact time in the tooltip, with a small "exact timing in tooltip / switch to intraday" note.
  Intraday: load minute bars for `[entry−1d, exit+1d]`, snap markers to the exact minute.
- **Crosshair tooltip** on a marker: `Modeled TP1 @ 172.30 · 2026-06-02 10:23 ET · intrabar: SL-first`.

## 5. Honesty & correctness caveats

These are load-bearing, not cosmetic — a user acts on this with real money
([[feedback_dont_oversimplify_money_entrusted_2026_06_05]]):

1. **Modeled, not executed.** Markers are bar-replay fills, no broker, no slippage. Label every
   surface `modeled / hypothetical / backtest fill`; never "executed" or bare "P&L".
2. **RTH-only / Polygon-free is material for 14–42 day swing holds.** Overnight gaps and
   pre/post-market moves are invisible: a stop hit pre-market is missed; a TP can read
   "not hit" when prices really did touch it. This affects marker *correctness*, so it is
   stated at the point of display (the footnote in §4.3), not buried.
3. **Intrabar sequencing.** With OHLC alone we cannot know whether a bar hit its high (TP) or
   low (SL) first. The replay already uses a deterministic **SL-first** rule on ambiguous bars
   and flags them (`same_bar_ambiguous`, `ambiguous_bars`). The chart exposes that flag rather
   than hiding it — a strength we already have.

## 6. Data — what exists, what is missing

Already available (verified 2026-06-09 on the VPS):

- **OHLC bars** cached per `~/.alphalens/population_ladders/bars/TICKER_DATE.parquet`
  (181 files) — daily + minute, Polygon free tier (closed RTH sessions only).
- **Ladder geometry** in the population-ladder parquet / edge Postgres: `blended_entry`,
  `full_ladder_blended_entry`, entry tiers (limit + alloc from the trade setup), TP prices,
  `disaster_stop`.
- **Crossing sequence** `sequence_str` (e.g. `E1->TP1`) — the *order* of events.
- **Outcome** `ladder_classification`, `realized_r`, `realized_return_pct_of_book`, `mfe/mae`,
  `holding_days_elapsed`, `last_priced_session`.

**The one gap — marker timestamps.** `sequence_str` carries order, not the bar timestamp of
each fill/exit. Lightweight Charts requires a marker's `time` to match an existing bar, so a
raw timestamp in a gap (weekend/overnight) would silently fail to render. Two options:

- **(a)** recompute timestamps deterministically from the bars + ladder geometry (re-run the
  `_LadderWalk` crossing logic, which already computes `bar_ts_ms` internally but does not
  serialize it) — cheap, no schema change; **preferred**.
- **(b)** add a timestamped-sequence column at nightly recompute time.

**Daily-from-minute consistency.** Compute daily candles *from* the cached minute bars rather
than mixing in vendor daily bars, so a daily candle's high/low matches the union of its minute
highs/lows (else "daily shows TP hit, intraday doesn't").

## 7. Architecture

- **Django edge endpoint** `GET /v1/edge/chart/<brief_date>/<ticker>?granularity=daily|intraday`
  → `{ bars: OHLC[], price_lines: {entry, tp[], stop}, markers: {time, kind, price, label, ambiguous}[], outcome }`.
  Markers are computed **server-side** from `sequence_str` + bars (option 6a), keeping the
  marker-timestamp logic in one place and respecting the slim-image boundary used by the rest
  of the `edge` app (the heavy compute stays pipeline-side; Django serves the projection).
- **`LadderChart.svelte`** — lazy `import('lightweight-charts')` inside `onMount` guarded by
  `if (browser)`. This avoids SSR errors (Lightweight Charts is DOM-only) and keeps the library
  out of the initial bundle — it loads only when a row is expanded or the brief toggle flips to
  `tape`.
- **Auth** — the endpoint sits behind the same Cloudflare Access / `auth_cf` path as the rest
  of `/v1/edge/*`.

## 8. Scope / PR sequence

1. **PR-1 — backend.** `/v1/edge/chart/...` endpoint + server-side marker timestamping
   (option 6a) + daily-from-minute bar derivation. Tests: marker time always lands on an
   existing bar; SL-first ambiguity flag plumbed through; NO_DATA / NO_STRUCTURE shapes.
2. **PR-2 — `LadderChart.svelte` + `/edge` inline accordion.** lightweight-charts dep, lazy
   mount, price lines + markers + states + honesty chip/popover + RTH footnote.
3. **PR-3 — brief `numbers ⇄ tape` toggle.** Wire the same component into the
   `trade.execution.setup` panel (planned-only data path).

Each PR is independently shippable: PR-1 has its own tests, PR-2 lights up `/edge`, PR-3 adds
the brief surface.

## 9. Open questions / deferred

- **Intraday data depth.** Polygon free minute history may not reach far enough for older
  briefs; if intraday bars are absent the toggle should disable with a reason rather than
  render an empty chart.
- **Full-session data later.** RTH-only is the current honest limitation; upgrading to a feed
  with pre/post would materially improve stop/TP correctness for swing holds. Out of scope.
- **Multi-trade overlay.** Not planned — if ever shown, markers would need clustering/paging
  (Perplexity flagged the perf cliff at hundreds+ visible markers).

## Appendix — Perplexity adversarial review (2026-06-09), key points

- Widget pulls TV-server data + branding/ToS → disqualified; full Charting Library = approval +
  datafeed → overkill; Lightweight Charts is the right trade-off given we own the data.
- `setMarkers` + `createPriceLine` cover the need; no hard count limits; our scale is trivial.
- Marker `time` MUST match an existing bar — a timestamp in a non-trading gap won't render.
- Daily-chart convention: marker on the fill date, exact time in tooltip; intraday zoom for
  forensic timing. Don't silently collapse intraday → daily without disclosure.
- Lightweight Charts collapses non-trading time (index-based scale) — markers must use a bar's
  own timestamp.
- Apache-2.0 = ship LICENSE/NOTICE text; no on-screen attribution required.
- Compute daily bars from minute bars for internal consistency.
- SvelteKit: instantiate only client-side (`onMount` / `if (browser)`).
- Honesty: hollow/dashed modeled markers, persistent "Simulation" label, an assumptions
  popover (data source + RTH limit + intrabar priority); never label as "executed" / "P&L".
