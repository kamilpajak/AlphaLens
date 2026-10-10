# Design: `replay_from_pick` fetches its own bars, on the venue's session grid

Status: **DRAFT**, 2026-10-10. Code facts come from `origin/main` at `3a182918`, which the worktree HEAD equals; the commit before it is `bd724cb2` (#1746), which matters in §6. Every number below carries the command or the file that produced it. The measurements marked **M1-M13** were run read-only during the design pass with `/Users/jacoren/Developer/Personal/AlphaLens/.claude/worktrees/grid/.venv/bin/python`, against `~/.alphalens/population_ladders/bars/` (881 parquet files, 322 tickers), `~/.alphalens/grouped_daily_history/` (497 parquet files), the committed LIVE trades fixture `apps/alphalens-research/tests/brokers/automanager/fixtures/trades/live_2026_10_03/`, and — for **M6** and **M11** — the real `intent-replay` engine. Where a fact is missing rather than known, the word is **GAP**, never a guess. Nothing in the repository or in `~/.alphalens/` was modified.

> **Read this first: section 5's chosen rule (D4) was REFUTED after the rest of this memo was
> written, and the revision is not in this file yet.**
>
> D4 keeps the bar stamped at the venue close and rewrites it to a single price equal to that bar's
> own open, on the premise that the open is the closing-auction print. Three measurements taken
> after the memo was finished defeat it, all run read-only against
> `~/.alphalens/population_ladders/bars/` and `~/.alphalens/grouped_daily_history/`.
>
> **1. Ranked against an independent oracle, D4 loses to dropping the bar.** Section 5.2 ranks the
> options by whether the close bar's extremes leave the range *the rest of the same minute series*
> traded. That denominator is identically zero when the bar is dropped, so the metric cannot rank
> the option section 5 rejects. Re-ranked against the official daily `[low, high]` of the
> grouped-daily store, which is not a re-rendering of the minute series, over the 15 572
> (ticker, session) pairs where no adjustment factor applies:
>
> | treatment of the close-stamped bar | sessions outside the official daily range | worst excursion |
> |---|---:|---:|
> | keep the whole bar | 168 (1.079%) | 1457.5 bps |
> | reduce it to its open (D4) | 30 (0.193%) | 202.0 bps |
> | **drop it (a half-open grid)** | **14 (0.090%)** | **34.7 bps** |
>
> **2. The premise is false on a measured case.** For CRSR 2026-08-06 the official daily close is
> 10.6100. The close-stamped bar reads `o 10.8900 h 12.8000 l 10.6100 c 12.7900`, so the auction
> print is that bar's LOW and not its open: the company reported after the close and the next print
> jumped to 12.80. D4 therefore writes 10.8900, which is 264 basis points away from the real
> closing price, while the pre-close bar's own close is 10.6100 exactly. Section 5.2 notes this case
> and sets it aside because it does not change the outcome flip; it does change the quantitative
> claim built on the same run.
>
> **3. The reduction destroys reachable range, not only post-market noise.** The walk tests a rung
> with `bar.low <= limit`, so raising the low to the open makes any level between the raw low and
> the open unreachable. Measured on the real `walk()` with one rung at 68.00: the whole bar
> `o 68.5 h 69.0 l 67.0` fills 13.2353 units and reports `open`, while the same bar reduced to the
> single price 68.5 reports `no_fill` with zero units and an empty trace. The same mechanism
> suppresses a stop fire.
>
> **What this does not overturn.** The incident in section 1, the case for the command fetching its
> own bars (D1-D3), the window rule and its refusal (section 6), the provenance sidecar (section 9)
> and the surfaces inventory (section 13) stand as written. The two measurements that favour keeping
> the close bar also stand, and they are about a different quantity: that bar's open matches the
> official daily close in 93.48% of sessions against 23.92% for the pre-close bar's close, so a
> half-open grid marks a still-open position one minute early, at a median absolute cost of 0.0322%.
> A backtester triggers on highs and lows, so the range is what can flip an outcome, and that mark
> is the smaller of the two errors.
>
> **Why this file is committed in this state.** It carries three measured and refuted options - the
> half-open grid, a `min()` window clamp, and this reduction - and that record is what keeps the
> next design from walking the same loop. The revision reinstating the half-open grid is the next
> commit on this branch.

---

## 1. The problem, with the incident

`apps/alphalens-research/scripts/replay_from_pick.py` turns one closed LIVE pick into the two inputs `intent-replay` takes: an authored TradeIntent document and a run configuration. It exists because assembling those by hand got three of seven values wrong over two days (#1714; its own module docstring, `replay_from_pick.py:1-10`).

**Bars are the one input it still does not build.** The script never opens a bar file. `args.bars` appears exactly once in 568 lines, at `replay_from_pick.py:554`, where it is interpolated into a suggested command:

```python
"--bars",
args.bars or "<BARS.json>",
```

So the operator reads the placeholder `<BARS.json>` off the human renderer (`replay_from_pick.py:563`) and builds the file by hand.

On 2026-10-08 a hand-built minute-bar file for `SMMT:2026-09-23` carried a bar stamped `2026-09-28 21:30:00Z` — ninety minutes after that session's XNAS close of `2026-09-28T20:00:00Z` (**M2**). The walk fired the take-profit on it. Nothing refused it.

**No existing check could have caught it**, for a reason the engine's own spec states. `docs/superpowers/specs/2026-09-23-intent-replay-design.md:411-414`, §4.1 "The leaf knows no calendar": "The replay knows nothing about sessions, exchanges or trading hours. It walks exactly the bars it is given." The engine's published bar refusals (`apps/intent-replay/README.md:499-502` and `:510`) are `bars_empty`, `bars_unordered`, `bars_invalid`, `window_too_short` and `bars_malformed`, over the closed reason vocabulary at `bars.py:45-70`: `decreasing`, `duplicate`, `numeric_not_finite`, `ends_before_walk_start`, `begins_after_walk_start`, `not_a_list`, `wrong_type`, `missing_key`, `unknown_key`, `incoherent`. Not one is session-aware. A bar ninety minutes after the close is a well-formed bar.

The run was published as a finding ("the replay is pessimistic on take-profit exits by 1.05%"). The venue audit then showed the real take-profit sold 58 seconds after the NEXT session's open: the fixture's own record for `SMMT:2026-09-23` carries `exits[-1].venue_time.value = 2026-09-29T13:30:58.853Z` against an XNAS open of `13:30:00Z` (**M3**).

**The file is lost, and that is the larger cost.** Its session filter, its adjustment basis, its bar count and the treatment of its last bar of each session can no longer be verified at all. Nobody can now say whether the file held one stray bar or hundreds, so the retraction cannot be bounded. Two things follow, and they shape the whole design:

* every construction this command applies to a vendor series must be recorded in a sidecar, in enough detail to be undone (§9);
* a hand-built bar file must leave the normal workflow, because the normal workflow is where the construction went unrecorded.

---

## 2. What is being built

The script fetches the minute bars itself, on the venue's own session grid, reduces the one bar per session that straddles the close to a single price, writes the resulting series in the published bar shape next to the document and the configuration, and writes a provenance sidecar beside them. A gate on the existing `--bars` flag was rejected as the remedy, because the script does not open that file today and the incident invocation did not pass it — but `--bars` survives as an offline override, and when it is given the file is validated against the same grid and reduced by the same rule rather than trusted.

### Command surface

```
USAGE
  python -m scripts.replay_from_pick TRADES_JSON PICK_KEY [options]

OPTIONS
  --out DIR              where the four files are written (default: .)
  --entry-trails PATH    the entry-trail journal (default: the report env's own)
  --entry-trail-bps N    state the distance instead of reading it; 0 means OFF
  --bars PATH            use this bar file instead of fetching; it is validated
                         against the venue's session grid and reduced by the
                         same close-bar rule
  --bar-cache DIR        the bar cache root (default: ~/.alphalens/replay_bars)
  --format human|json    default human
```

Required: `TRADES_JSON` and `PICK_KEY` only, exactly as today. No flag is required; fetching is what happens when `--bars` is absent.

Written into `--out`, with `stem = PICK_KEY.replace(":", "_")` (`replay_from_pick.py:524`):

| file | new? | written when |
|---|---|---|
| `<stem>.document.json` | no | always |
| `<stem>.config.json` | no | always |
| `<stem>.bars.json` | yes | **always** — it is the series the walk reads, which on both paths is the reduced one |
| `<stem>.bars.provenance.json` | yes | always |

`<stem>.bars.json` being written on the `--bars` path too is a change from the previous draft, and it is forced by the reduction: the operator's file is no longer the series that is walked, so `replay_argv` must point at the series that is. The supplied file is never overwritten; its own sha256 is recorded separately (§9).

Three keyword-only seams are added to `main()` with production defaults, so tests need no network: `bar_fetch`, `actions_lookup`, `now`. The module entry point (`replay_from_pick.py:567-568`) passes none of them, and §12 gives that wiring its own source gate.

---

## 3. The decisions, as given

These are the owner's premises, recorded so a later reader does not reopen them.

**D1 — fetching is the default; `--bars` is an offline override and is validated.** A gate on `--bars` alone fixes nothing, because the script never opens that file (`:554` is its only use) and the incident invocation did not pass it. Deleting the hand-built file from the normal workflow is the remedy; policing it is not. Consequence: the help text's promise at `replay_from_pick.py:415-416` ("The report decides comparability, so the command needs no broker of its own and runs offline") becomes conditional, and §8 narrows it further than D1 alone implies.

**D2 — minute bars.** The live daemon polls roughly every 45 seconds (`alphalens broker manage --poll-seconds 45`, CLAUDE.md:333), and a daily bar cannot reproduce a trailing stop: one low per session carries no path. Window: from `config.walk_start` to the pick's real exit time plus a margin; for a pick that never filled, to `config.entry_deadline`. The exit time is in the trades report the script already parses but does not read today. §6 states the exact rule.

**D3 — a disk cache, keyed by (ticker, window, granularity, adjusted).** Polygon is metered at 5 requests per minute on the free tier (CLAUDE.md's vendor table; `polygon_client.py:104`, `rate_limit_per_min: int = 5`), and re-running a replay is normal work. The cache may live under `~/.alphalens/`: the script already reads the entry-trail journal from there through `state_paths.entry_trails_path` (`state_paths.py:153`), so this does not break its layer.

**D4 — the bar stamped at the venue close is KEPT and written as ONE price: its own open.** The minute stamped at the close spans `close .. close + 59s`. The session ends AT the close, so that minute holds the closing-auction print and then up to 59 seconds of post-market trading, mixed into one OHLC quadruple. The fetch keeps the bar — membership is unchanged — and sets `open = high = low = close = that bar's own open`. §5 gives the measurement, the model error it removes and what it costs.

**Provenance sidecar.** The bars are written with a sidecar naming the vendor, `adjusted`, the session filter, the window, the as-of, the close-bar reduction including every reduced bar's original quadruple, and a sha256 of the series. Motivated by a real loss: the SMMT file's filter and its close-bar treatment cannot be reconstructed, so the published 1.05% cannot be audited even now that it is known to be wrong.

---

## 4. Composition — the blocks it stands on

### Block 1 — `bar_window.BarFetch` / `_default_bar_fetch`

`apps/alphalens-pipeline/alphalens_pipeline/feedback/bar_window.py:44` and `:47-53`:

```python
BarFetch = Callable[[str, dt.datetime, dt.datetime], Sequence[dict[str, Any]]]

def _default_bar_fetch(ticker, start, end) -> Sequence[dict[str, Any]]:
    from alphalens_pipeline.data.alt_data.polygon_client import get_default_polygon_client
    return get_default_polygon_client().get_agg_range(ticker=ticker, start=start, end=end)
```

The script passes the ticker and two timezone-aware UTC instants, and receives raw Polygon agg dicts: `t`, `o`, `h`, `l`, `c`, `v`, `vw`, `n` (`polygon_client.py:221-223`). `get_agg_range` defaults to `timespan="minute"`, `adjusted=False`, `sort="asc"`, `limit=50000` (`polygon_client.py:211-214`).

**Where the ticker comes from.** `document["instrument"]` is `{"mic", "ticker"}` — measured on the fixture's `SMMT:2026-09-23` plan, which prints `{'mic': 'XNAS', 'ticker': 'SMMT'}` (**M3**). That is the same block `:140` already reads the MIC from, so the venue and the symbol travel together from one source. The trades record also carries `ticker` at its top level, and its own `instrument` block carries `uic`, `exchange_mic`, `instrument_currency` and `sizing_currency` and **no symbol at all** (**M3**); nothing in this design reads either.

**What this block does not do, and the script therefore must:**

* **It has no consumers.** Measured: `grep -rn "bar_window" --include "*.py"` finds two importers, both of `IMPLAUSIBLE_RETURN_THRESHOLD` only (`population_ladder_monitor.py:71`, `ladder_replay.py:1723`); `grep -rn "_default_bar_fetch"` finds `bar_window.py:47` plus four *independent local copies* (`population_ladder_monitor.py:406`, `sector_excess.py:113`, `ladder_chart.py:707`, `benchmark_excess.py:131`), none importing it. This change gives the seam its first consumer and **renames `_default_bar_fetch` to `default_bar_fetch`** (and its `__all__` entry at `bar_window.py:61`), because a cross-package import of a module-private name is worse than a public one and there are zero importers to break. There is no `__all__` gate and no private-import gate in the tree, so the rename is free.
* **It cannot express `adjusted`.** `get_agg_range` takes `adjusted: bool = False` (`polygon_client.py:212`) but the callable is three positional arguments and line 53 never forwards it. So `adjusted=False` is what the seam can say, which is the basis this design wants anyway (§8), and the cache key and the sidecar record it as a constant rather than as a choice.
* **No shape translation, and the converter must be ADDITIVE.** The published bar shape is closed: `_BAR_KEYS = ("t", *_PRICE_FIELDS)` (`bars.py:72-74`), and `unknown_key` refuses any extra key (`bars.py:215-223`). `v`, `vw` and `n` would each be refused, and `o/h/l/c` would each be `missing_key`. No existing site produces the replay's shape: `ladder_chart.py:383-390` emits `time` as an ISO string plus `volume`, and `ladder_replay.py:238` emits a 4-tuple. **The script writes its own converter, building the output from `bars._BAR_KEYS` rather than deleting keys it knows about** — Polygon returns `otc: true` on OTC tickers, and a subtractive converter would pass it straight through into an `unknown_key` refusal. `t` is kept as an `int` (`bars.py:224-232` refuses a non-`int` `t`).
* **No session filter, no close-bar reduction, no cache, no granularity check, no right-edge coverage check.**

### Block 2 — the repo's RTH idiom, which this design ADOPTS at the close and corrects at the span

Three places in the tree implement "filter bars to RTH", and all three are **inclusive at the close**:

| site | what it says |
|---|---|
| `ladder_chart.py:182-201` `_rth_window_ms` | docstring `:183`: "``(open_ms, close_ms)`` epoch-ms RTH bounds for ``session`` (close inclusive)" |
| `ladder_chart.py:143-156` `_session_date_for_ts` | `:154`: `if open_ms <= ts_ms <= close_ms:` |
| `population_ladder_monitor.py:740-777` `_filter_bars_to_rth` (with `_rth_window_utc` at `:727-737`) | `:728` "(close inclusive)"; `:775`: `if any(lo <= ts <= hi for lo, hi in windows)`; docstring `:756`: "A bar is kept when its start ``t`` falls in ``[session_open, session_close]``" |

**The agreement is exact, not approximate.** **M12**: on 40 randomly sampled real bar files (`random.seed(1714)`), the set of timestamps `_filter_bars_to_rth(bars, arrival, horizon, "XNYS")` keeps is **identical to the inclusive grid's — 0 mismatches of 40 files**. So a file an operator built with the repo's own helper passes the new grid unchanged, which is what makes the `--bars` override usable at all.

What this design does **not** reuse is the function, for one measured reason: both helpers compute the close as `open_ms + span_min * 60_000` with `_RTH_FULL_SESSION_SPAN_MIN = 390` (`population_ladder_monitor.py:187`) returned on the non-half-day branch (`:718`) and multiplied at `:736`; `ladder_chart.py:193-200` does the same with its own literal. **M2** measures the damage on the venues that matter today:

| MIC | date | helper close | calendar close | real span, minutes | agrees |
|---|---|---|---|---|---|
| XNAS | 2026-10-02 | 20:00:00Z | 20:00:00Z | 390 | yes |
| XNYS | 2026-10-02 | 20:00:00Z | 20:00:00Z | 390 | yes |
| XTKS | 2026-10-02 | 06:30:00Z | 06:30:00Z | 390 | yes, by coincidence (see below) |
| XWAR | 2026-10-02 | 13:30:00Z | **15:00:00Z** | **480** | no, 90 minutes short |
| XETR | 2026-10-02 | 13:30:00Z | **15:30:00Z** | **510** | no, 120 minutes short |
| XNAS half-day | 2026-11-27 | 18:00:00Z | 18:00:00Z | 210 | yes |
| XNAS half-day | 2026-12-24 | 18:00:00Z | 18:00:00Z | 210 | yes |
| XNAS | 2026-10-30 | 20:00:00Z | 20:00:00Z | 390 | yes |
| XNAS | 2026-11-02 | 21:00:00Z | 21:00:00Z | 390 | yes |
| XNAS | 2026-09-26 (Sat) | `ValueError: datetime.date(2026, 9, 26) is not a session on XNAS; resolve it first` | same | — | — |

XTKS agreeing at 390 makes this worse, not better: its nominal span really is 390 minutes, of which 60 are a lunch break — measured, `has_break=True` with `session_break_start → session_break_end` of **02:30→03:30Z** on XTKS and **04:00→05:00Z** on XHKG, while XNAS and XWAR report `has_break=False` (**M2**). A US-plus-Tokyo test suite therefore stays green while XWAR loses 90 minutes of every session. That is the same venue-blindness family as the XNYS-for-XNAS error #1714 was built to stop.

**And the defect runs in both directions, which the previous draft described as short-only.** **M2** (`r8_span.py`), over 30 MICs at their next session on or after 2026-10-12, of which 29 resolve (XNSE is not an `exchange_calendars` calendar): 17 are short (XETR / XPAR / XLON / XAMS / XBRU / XLIS / XMIL / XMAD / XSWX / XSTO / XHEL by 120 minutes, XWAR / XCSE / XSES / XJSE by 90, XOSL by 50, XNZE by 15) and **four are LONG** — the helper's close runs PAST the real one:

| venue | real close | `open + 390` | direction |
|---|---|---|---|
| XSHG | 07:00Z | 08:00Z | **+60 minutes past the close** |
| XTAI | 05:30Z | 07:30Z | **+120 minutes** |
| XASX | 05:00Z | 05:30Z | **+30 minutes** |
| XBOM | 10:00Z | 10:15Z | **+15 minutes** |

The long direction matters more than the short one. It breaks `_filter_bars_to_rth`'s own stated contract that the grouped-daily `[low, high]` is "a TRUE superset of the minute path" (`:748-750`), and it is the direction in which a repo-built `--bars` file would be **refused** by the new grid. CLAUDE.md already names XSHG among planned venues. §16 Q1 puts the whole span defect to the owner with both halves on the table.

So what is reused is the idea — ask the calendar for the bound instead of assuming it — and the script composes `session_open_utc` and `session_close_utc` directly (`market/calendar.py:326`, `:345`). Both read the per-session auction time from `exchange_calendars`, so half-days resolve with no special case, and both raise `ValueError` for a non-session date. Promoting a corrected helper into `feedback/bar_window.py` would also be layer-clean, but there is no second consumer, so it stays in the script (extract on second use).

### Block 3 — the script's own existing derivations

Already present and unchanged:

* the venue: `mic = document["instrument"]["mic"]` (`replay_from_pick.py:140`), used for every calendar call;
* `walk_start = session_open_utc(day1, exchange=mic)` where `day1` comes from `meta.source` (`:150`, `:210-216`);
* `entry_deadline = pick_window(trade_date, ttl_days, mic).window_end` (`:153`, `:199-209`), and `pick_window` computes that as `session_close_utc(last_session, exchange=mic)` (`pick_window.py:69`).

`advance_trading_sessions` is already imported (`:32`). Added from the same module: `session_close_utc` (`calendar.py:345`), `session_on_or_after` (`:231`), `previous_trading_day` (`:212`) and `is_trading_day` (`:124`). `session_not_closed` (`:364`) is deliberately **not** used — §6 says why.

**What block 3 does not do: it never reads the exit.** What the script reads off the trades record today is `trade["instrument"]` (`:301`), `trade["sizing_fx"]` (`:313`), `trade["plan"]` (`:351`, handed to `authored_document`), `trade["pick_key"]` (`:332`, `:445`) and `trade.get("replay_exclusions")` (`:329`). `stated_facts` alone reads only `instrument.*` and `sizing_fx.*` (`:292-320`). The exit is in none of them: `grep -n "exits\|venue_time" apps/alphalens-research/scripts/replay_from_pick.py` returns **nothing**.

The exit instant is at `trades[i].exits[j].venue_time.value`, a `Measured` (`trades.py:406-424`) rendered RFC 3339 with milliseconds and `Z` by `format_time` (`trades.py:399`), flattened onto the exit by `_Exit.to_dict` (`trades.py:684-689`). Three facts about it, all measured (**M3**):

* `exits` is produced in ascending `fill.when` order (`trades.py:1980`), so **the trade's real exit is `exits[-1]`**. The fixture contains a record with two exits — `ALB:2026-09-08-g2`, at `2026-09-14T13:59:41.231Z` and `...41.250Z` — so `exits[0]` is already wrong on real data, although that particular record is excluded by `legacy_plan_shape`.
* `venue_time` can be null with `null_reason` `NULL_NOT_JOURNALED = "not_journaled"` (`trades.py:101`, `:431`, `:439`), and `_replay_exclusions` does not exclude such a record: `trades.py:2962-2992` branches on the exit reason and on `attributed_qty` and never on the exit time. So the script refuses a null exit time itself, naming `null_reason`, exactly as `_measured` already does (`replay_from_pick.py:256-266`).
* No record in the fixture actually carries one: 0 of 35 records have a null exit `venue_time`. The refusal is therefore untested by the data and must be tested by a constructed record.

### Block 4 — the corporate-action lookup

`feedback/corporate_actions.py`: `PolygonCorporateActionsLookup` (`:128`, `lookup(ticker, start: date, end: date) -> CorporateActionsAnswer` at `:149`), wrapped in `CachedCorporateActionsLookup(inner, cache_path)` (`:209-217`), with the window padding constants `ACTION_WINDOW_PRE_CALENDAR_DAYS = 3` and `ACTION_WINDOW_POST_CALENDAR_DAYS = 1` (`:101-102`). The lookup needs a `pre_ex_close` function (`:138`); §8 states what the script supplies and what that costs.

---

## 5. The session grid, and the one bar that is reduced to one price

Two rules, in this order. The first decides MEMBERSHIP, the second decides CONTENT.

```
(1)  keep a bar iff  open_ms <= t <= close_ms  for some session in the window's session list,
     where open_ms / close_ms come from session_open_utc / session_close_utc for that session and MIC

(2)  for each kept bar with  t == close_ms  of its session:
         open = high = low = close = that bar's own open
```

The two calendar helpers name what the bounds are. `session_open_utc` docstring (`calendar.py:330`): "UTC datetime of ``exchange``'s opening auction on session date ``d``." `session_close_utc` (`calendar.py:349`): "UTC datetime of ``exchange``'s closing auction on session date ``d``." Polygon stamps a bar at its START (`polygon_client.py:221`: "``t`` (ms epoch, bar START)"), and the engine reads it the same way (`bars.py:89`: "``t`` is epoch milliseconds, UTC, and is taken to be the bar's OPEN time"). So the bar stamped at the close is the minute 16:00:00-16:00:59 ET. **The session ends at 16:00:00 exactly.** That minute therefore holds the closing-auction print, at its first instant, and then up to 59 seconds of trading that belongs to no session.

### 5.1 Membership is inclusive at both auctions

#### The lower bound must be inclusive

`check_window_covers` refuses a series whose first bar is later than `walk_start` (`bars.py:287-295`, reason `begins_after_walk_start`), and `walk_start` is exactly `session_open_utc(day1)` (`replay_from_pick.py:210-216`). A gate that dropped `t == open` would make every run refuse itself. It is also the normal case: **M1** finds 17 088 of 17 163 real (file, session) pairs carry a bar stamped exactly at the session open, 99.56%.

#### The upper bound must be inclusive, for two reasons that survive the reduction

**1. It is what lets `_expire` fire on the deadline millisecond.** `walk.py:171` is `if bar.t < deadline or not state.pending: return`, so a walk that is never handed a bar at `t == deadline` never fires `entry_expired` at all. The deadline instant *is* a session close (`pick_window.py:69`), so the two decisions meet on one millisecond. **M6**, the real engine on `SYM:2026-09-30`'s own document and configuration (deadline `2026-10-09T20:00:00Z`), flat tape above the single entry limit of 40.34 so nothing fills, six bar series:

| series | bars | outcome | trace | `entry_expired` at | `walked.to_t` |
|---|---|---|---|---|---|
| half-open, ends at the deadline session 2026-10-09 | 3 120 | `no_fill` | `[]` | **never fires** | 2026-10-09T19:59:00Z |
| inclusive, whole close bar, same end | 3 128 | `no_fill` | `['entry_expired']` | **2026-10-09T20:00:00Z** | 2026-10-09T20:00:00Z |
| **inclusive, close bar REDUCED, same end** | 3 128 | `no_fill` | `['entry_expired']` | **2026-10-09T20:00:00Z** | 2026-10-09T20:00:00Z |
| half-open, +2 sessions to 2026-10-13 | 3 900 | `no_fill` | `['entry_expired']` | 2026-10-12T13:30:00Z | 2026-10-13T19:59:00Z |
| inclusive, whole close bar, +2 sessions | 3 910 | `no_fill` | `['entry_expired']` | **2026-10-09T20:00:00Z** | 2026-10-13T20:00:00Z |
| **inclusive, close bar REDUCED, +2 sessions** | 3 910 | `no_fill` | `['entry_expired']` | **2026-10-09T20:00:00Z** | 2026-10-13T20:00:00Z |

The reduction changes nothing here, which is the point: `_expire` reads `bar.t`, not the bar's prices. Half-open either never fires the event or fires it **65.5 hours** late at the next session's open (2026-10-09 is a Friday; the deadline is `2026-10-09T20:00:00Z` and the late fire is `2026-10-12T13:30:00Z`, measured in `r5_window.py`), misattributing the instant the ladder died.

**2. It is what makes a repo-built `--bars` file acceptable.** **M12**: 40 real files, the inclusive grid's kept set identical to `_filter_bars_to_rth`'s, 0 mismatches. A half-open bound would refuse roughly one bar per session in any file built with the repo's own helper, and the refusal's own advice would be to surrender the override. That is the shape of a guard that gets switched off.

**3. It still refuses the incident.** The fatal bar was `2026-09-28T21:30:00.000Z`, epoch `1790631000000`, ninety minutes past that session's close of `2026-09-28T20:00:00.000Z` (`1790625600000`, open `1790602200000`). The predicate refuses it either way. Applied over the XNAS session list 2026-09-23 → 2026-10-06: `2026-09-28T21:30:00Z` off-grid; `2026-10-02T20:00:00Z` kept (it is that session's closing minute, and it is reduced); `2026-10-02T19:59:00Z` kept; `2026-09-23T13:30:00Z` kept.

**GAP, and it bounds what claim 3 is worth.** `2026-10-02T20:00:00Z` is `SMMT:2026-09-23`'s own `entry_deadline` (`1790971200000`, **M7**), so it is the terminal bar of the exact run whose retracted number this memo is about. Whether SMMT's own close-stamped bar on that session carried a post-close excursion cannot be measured here: SMMT is not in the local bar store (322 tickers; of the 12 comparable picks only VCYT appears, and only for one July window — **M1**). So "it still refuses the incident" is a claim about the 21:30Z bar alone. It is NOT a claim that this design would have produced the right published number.

### 5.2 Content: why the close-stamped bar is written as one price

#### The measurement

**M10**, over every (file, session) pair in `~/.alphalens/population_ladders/bars/` carrying both a pre-close bar and a close-stamped bar — **n = 15 677 pairs**; every such pair carries a pre-close bar, so the two constructions coincide. A "contamination" is a close-stamped bar whose high or low lies outside the range the rest of that session traded. Excursions are normalised by the rest-of-session bound that was crossed (`rest_h` for a high excursion, `rest_l` for a low); `r1_close.py` + `r2_denom.py`:

| treatment of the close-stamped bar | contaminations | share | median | p90 | max |
|---|---:|---:|---:|---:|---:|
| keep the whole bar | 182 | 1.161% | 0.1614% | 2.6975% | **14.5752%** |
| **keep only its open — the auction print (D4)** | **43** | **0.274%** | 0.0499% | 1.9218% | **1.9801%** |

The reduction removes **139 of 182 contaminations, 76.4%**, and cuts the worst case from 14.58% to under 2%. The 43 that remain are cases where the auction print ITSELF lies outside the rest of the session's range — real prints of a real session, which belong in the tape. (Normalising the second row by the auction print rather than by the bound it crossed gives median 0.0500%, p90 1.9482%, max 2.0201%; the figure quoted when this decision was taken. The conclusion is the same at either normalisation, and the worst case is bounded near 2% under both.)

**Per run the gap is larger than per session.** A replay window is not a session: **M10** over the 871 bar files that are real windows, median 13 sessions each (`r5_window.py`):

| | whole bar | reduced to its open |
|---|---:|---:|
| windows with ≥1 contaminated session | 149 of 871 (**17.11%**) | 35 of 871 (**4.02%**) |
| windows carrying a >2% excursion | 25 (2.87%) | **0 (0.00%)** |
| windows carrying a >5% excursion | 6 (0.69%) | **0 (0.00%)** |

Under the reduction no window in the store carries an excursion above 2%.

#### It flips a published outcome, measured on the real engine

**M11.** Real CRSR minute bars from `~/.alphalens/population_ladders/bars/CRSR_2026-08-03.parquet`, window 2026-08-03 → 2026-08-06 (`pick_window(2026-08-03, 3, XNAS).window_end = 2026-08-06T20:00:00Z`), a document derived from the fixture's real SMMT plan with the ticker and prices moved onto CRSR: take-profit 11.95, which sits above the session's own high of 11.1717 and below the close-stamped bar's high of 12.80. The close-stamped bar is `{open 10.89, high 12.80, low 10.61, close 12.79}`; the rest of the session traded `[10.45, 11.1717]`, so the whole move above 11.1717 is inside that one minute. **GAP:** what moved the price after the close is not established here, only that it did. Three runs of `intent_replay run` on the same document and configuration, differing only in that bar:

| treatment | outcome | last trace event | `r_multiple` | `pnl_cash` | `mfe` | `walked.to_t` |
|---|---|---|---:|---:|---:|---|
| keep the whole bar | **`closed_tp`** | `tp_fired` @ 20:00:00Z, price 11.95 | **+0.9141 R** | +227.45 PLN | 1.5781 | 2026-08-06T20:00:00Z |
| drop it (half-open) | `open` | `horizon_open` @ 19:59:00Z, 10.61 | −0.1328 R | −33.05 PLN | 0.5156 | 2026-08-06T19:59:00Z |
| **reduce it (D4)** | `open` | `horizon_open` @ 20:00:00Z, **10.89** | **+0.0859 R** | +21.38 PLN | 0.5156 | **2026-08-06T20:00:00Z** |

Whole bar against half-open is the 1.047 R flip. The reduction removes **0.828 R** of it — the part that was a post-close print firing a take-profit at a level the regular session never reached — and leaves **0.219 R**, which is the correct effect of marking the still-open position at the close instant instead of a minute earlier. It also restores the uncontaminated `mfe` (0.5156 against 1.5781) while keeping `walked.to_t` on the close instant, which half-open does not.

#### It removes a model error the previous draft could only declare

The engine treats the two exit legs differently, in its own words:

* `walk.py:497-498` — "The stop RESTS at the broker, so a bar that opens already through it executes at the open", filling at `price = min(bar.open, state.stop)` (`:512`). A broker's resting stop is a regular-session order; a post-market print cannot fill it. Letting one do so is flatly wrong.
* `walk.py:707-710` — "A take-profit does not rest at the broker in this model, so the gap rule of section 4.4 does not reach it: it fills AT its level, and the distance between that and where a live fill would have landed is the ``take_profit_observation_time`` divergence the envelope reports" — a divergence the envelope already publishes (`envelope.py:93`, `:117`).

So the whole-bar cost is two different costs. **M10**, the direction split over the 139 contaminations the reduction removes (the ones whose auction print is itself inside the session's range):

| direction | sessions | share of 15 677 | median | max |
|---|---:|---:|---:|---:|
| low extended — reaches a resting stop the session never reached | 37 | 0.236% | 0.1293% | **0.9901%** |
| high extended — reaches a polled take-profit | 103 | 0.657% | 0.3145% | **14.5752%** |

The flatly-unrealistic side is bounded under 1% of price in this store; every large magnitude sits on the side the engine already declares a divergence for. Under a single-price bar both legs see the same one price, so the resting-stop error disappears entirely, **with no change to the engine**. One alternative was considered and rejected: keep the vendor's quadruple and let only the resting legs see its open. That would make one bar mean two things inside one walk, which the engine has no shape for and which would require an engine change — and `apps/intent-replay` is a client-agnostic leaf this design does not touch (§14).

Two arithmetic consequences of a one-price bar, stated because a reader will ask:

* `high == low`, so a reduced bar can reach a stop and a take-profit at once only if the declared take-profit sits at or below the stop. For an ordinary long ladder it therefore cannot raise row 1 of the engine's §4.4 table, and the CRSR runs report `snu_bars: 0`.
* The engine already blesses the shape: `bars.py:133-134`, inside `Bar._check_coherence` (`:121`), reads "The bounds are INCLUSIVE: a bar that opened on its low, or never moved at all, is an ordinary bar." No engine refusal is near it, which **M11** confirms — all three runs exited 0.

#### What the bar's own open is, and how often it is the official close

**M4**, over the same 15 677 close-stamped pairs, against the official daily close in `~/.alphalens/grouped_daily_history/` (`r4_close_price.py`). All 15 677 fall in the 67 sessions where the two stores overlap; 15 578 of them have the ticker present in the daily store for that session, and the shares below are over those 15 578:

| candidate for the session's final price | matches the official close exactly | median abs deviation |
|---|---|---|
| **the close-stamped bar's OPEN — what D4 keeps** | **14 562 (93.48%)** | 0.0000% |
| the pre-close bar's close — what a half-open grid leaves as last | 3 727 (23.92%) | 0.0338% |
| neither | 844 (5.42%) | — |

(3 555 pairs, 22.82%, match on both, because the auction printed at the preceding minute's close.) The 5.42% is expected and is not an argument either way: the daily store is written `adjusted=true` (CLAUDE.md:369) while the minute store is raw, so a split between the two fetch dates separates them permanently. The earlier draft published these shares against "n = 766 pairs over the 66 sessions where the two stores overlap" — a construction that iterated one session per bar FILE while its own sentence described every session inside every file. The wide construction above is the one the sentence describes; the conclusion is unchanged, a four-to-one gap either way.

So the bar's open is the official closing price in 93.48% of pairs. Where it is not, it remains the FIRST print of the minute that begins at the close instant, which is the earliest and therefore least post-market price the bar carries. CRSR 2026-08-06 is one such case: the bar's open is 10.89 while the official daily close is 10.61 — and the reduction still removes the flip, because what it removes is the 12.80 high, not the choice between 10.89 and 10.61.

#### What this costs, stated and not argued away

It writes a bar the vendor did not send in that exact shape. §14 says what pays for that, and does not pretend the objection was never valid.

### 5.3 Granularity is a SEPARATE gate, and it is phrased on the measured spacing

The session grid cannot double as a granularity check. **M8**, applying the inclusive per-session predicate to every file in the repo's own daily store `~/.alphalens/grouped_daily_history/`: **492 of 497 session files are KEPT.** Each file carries one single `t` per row, stamped at nominal 16:00 New York local time, DST-aware — which on a full session *equals* the actual close, so the grid admits it at the close exactly as it admits a minute bar, and the reduction then collapses a daily bar to its own open. The 5 refused are all half-days, where the nominal stamp misses the real early close: 2024-11-29, 2024-12-24, 2025-11-28 and 2025-12-24 stamped `21:00:00Z` against an `18:00:00Z` close, and 2025-07-03 stamped `20:00:00Z` against `17:00:00Z`.

**M13** then measures which coarse series a gate can actually catch. Over 10 XNAS sessions (`r9_gran.py`), with the previous draft's rule — "spans two or more sessions and carries at most one bar in each":

| series | bars | kept | bars per session | modal `t` spacing | one-bar-per-session gate |
|---|---:|---:|---|---|---|
| daily at the session open | 10 | 10 | 1 | 1 440 m | **fires** |
| daily at the session close | 10 | 10 | 1 | 1 440 m | **fires** |
| hourly | 70 | 70 | 7 | 60 m | silent |
| 30-minute | 140 | 140 | 14 | 30 m | silent |
| 5-minute | 790 | 790 | 79 | 5 m | silent |
| 1-minute | 3 910 | 3 910 | 391 | 1 m | silent |

D2's reason — one low per session carries no path — applies to every coarse granularity, not only to one-per-session. So the gate is phrased on the **modal `t` spacing of the whole kept series**, which separates all six rows above:

```
bars_wrong_granularity / not_one_minute
  refuse a kept series of two or more bars whose modal t-spacing is not 60 000 ms
```

**It must be the WHOLE series, not per session, and that is measured.** **M10/M13** (`r10_modal.py`), over the real store: a per-SESSION modal-spacing rule would over-fire on **26 of 17 163 pairs (0.15%)** — sparse sessions whose commonest gap is 2, 3, 4 or 9 minutes. The same rule over the whole kept series of each file fires on **0 of 881 real windows**: every one has a modal spacing of exactly 60 000 ms. A series of fewer than two bars has no spacing to measure and is exempt, which keeps the one-bar-at-`walk_start` case legal.

The grid itself stays count-blind and completeness-blind. **M1**: across all 17 163 pairs, **zero** carry exactly one kept bar, and the bars per pair run 13 (floor) / 359 (median) / **391** (max, reached by 3 330 pairs — 391 and not 390, because `[open, close]` inclusive at one-minute spacing is 391 minutes). A 390-bar completeness check would refuse **13 134 of 17 163 pairs (76.5%)**, or **13 833 (80.6%)** if phrased at 391. Either way it is a check that fires on three quarters of real data, which is why there is none.

A one-bar series inside ONE session stays legal on every other count too: `validate_sequence` refuses only an empty sequence (`bars.py:236-244`), and a single bar at `walk_start` covers it on both sides (`bars.py:266-296`).

### 5.4 Fetch filters; a supplied file is refused on membership and reduced on content

This asymmetry is deliberate and is the rule most likely to be mis-implemented. It turns on the distinction between MEMBERSHIP and CONTENT.

* **Membership, fetch path:** a raw Polygon window legitimately contains minutes outside regular trading hours. **M1** measures how many: 6 364 368 bars fetched against 5 611 172 kept, a ratio of **1.1342** — 13.4% of the store's bars are outside RTH, spread thin across pre-market (08-13Z) and post-market (20-23Z). Off-grid bars are **dropped**, and the count goes into the sidecar as `counts.dropped_off_grid`.
* **Membership, `--bars` path:** the operator asserted that this file is the input. Off-grid bars are **refused**, not filtered, because silently dropping bars from a file somebody built would change a published number without saying so — a quieter version of the incident, not a fix for it.
* **Content, BOTH paths:** the close-bar reduction is applied, and the sidecar records the rule, the count, and **every reduced bar's original `{open, high, low, close}`**, so the unreduced series can be rebuilt from the sidecar plus the written bars file. A reduction is therefore not silent in the sense the off-grid drop would have been: it is stated, enumerated and invertible. Refusing an unreduced supplied close bar instead was rejected, because **M12** says that would refuse every file built with the repo's own three RTH helpers — the files the override exists to accept.

In bulk the whole treatment is small. **M1**: 5 611 172 bars kept inclusive against 5 595 495 half-open — the inclusive close adds exactly 15 677 bars, one per close-stamped pair, 0.28% of the kept series, and those 15 677 are precisely the bars the reduction rewrites.

### 5.5 Legitimate inputs the predicate must not refuse

A guard that over-fires is worse than none, so each row has a test in §12.

| input | passes | why |
|---|---|---|
| a bar at `t == open` | yes | the lower bound is inclusive; 99.56% of real pairs carry one (**M1**) and `check_window_covers` requires the first bar at or before `walk_start` (`bars.py:287-295`) |
| **a bar at `t == close`** | **yes, kept and reduced** | it is the closing-auction minute; its open is the official close in 93.48% of pairs (**M4**) and keeping it is what lets `_expire` fire on the deadline millisecond (**M6**) |
| **a reduced bar, `open == high == low == close`** | **yes** | `bars.py:133-134`: "a bar that opened on its low, or never moved at all, is an ordinary bar"; **M11** runs three such series through the engine at exit 0 |
| a file built with this repo's own RTH filter | yes | **M12**: 0 mismatches on 40 real files; its close-stamped bars are reduced, not refused |
| another venue, e.g. XWAR | yes | the grid asks `session_close_utc` per session instead of adding 390 minutes; without that XWAR loses 90 minutes and XETR 120, while XSHG gains 60 and XTAI 120 (**M2**) |
| a half-day | yes | `exchange_calendars` stores the actual per-session close, so XNAS 2026-11-27 grids to `14:30Z-18:00Z` with no special case (**M2**) |
| a window spanning a DST change | yes | the grid is built per session: XNAS 2026-10-30 grids `13:30Z-20:00Z`, 2026-11-02 grids `14:30Z-21:00Z` (**M2**); nothing is a fixed offset from the window start |
| a one-bar series at `walk_start` | yes | one bar; it has no spacing, so the granularity gate is exempt by construction |
| a sparse session | yes | no completeness check, and the granularity rule reads the whole series, not the session: a per-session rule would refuse 26 real pairs (**M13**) |
| a daily series | **no, but not by the grid** | the grid KEEPS it (**M8**: 492 of 497 real daily files pass); the modal spacing is 1 440 m, so `bars_wrong_granularity/not_one_minute` refuses it |
| an hourly / 30-minute / 5-minute series | no | modal spacing 60 / 30 / 5 minutes (**M13**); the previous draft's one-bar-per-session rule let all three through |
| a post-market bar in a FETCHED window | yes, as a drop | the vendor returns 13.4% of them (**M1**); dropping with a count is correct |
| the same bar in a SUPPLIED file | no, refused | membership asymmetry, above |

### 5.6 One input that used to work and now does not

**M9**, over the whole store: 75 of 17 163 pairs (0.437%) carry no bar at the session open, and in **44** of those a pre-market bar exists in the same file; the first RTH bar arrives 1 minute late at the median and 23 at the worst. Before this change an operator could include that pre-market bar and satisfy `check_window_covers`. Under the grid it is dropped on the fetch path and refused on the supplied path, so the run refuses with `bars_unusable/walk_start_uncovered`.

This is a deliberate tightening, not an oversight. Replaying a pre-market print as the opening minute is exactly what `population_ladder_monitor._filter_bars_to_rth` declines to do — "Pre/post-market fills are DELIBERATELY not modelled — resting-limit geometry is an RTH construct" (`:752-753`), which is also the sentence that argues for §5.2. The refusal says what happened ("the venue printed no opening bar for this session") instead of handing the operator the engine's `begins_after_walk_start` to map back onto a fetch fact. Only day 1 can trip it, since `check_window_covers` looks at the first bar alone, so the per-run rate is the 0.437% above. No override flag is added.

---

## 6. The window, and why its right edge is a REFUSAL rather than a `min()`

### The rule, stated once

```
exit_instant   = exits[-1].venue_time.value       (absent -> the pick never filled)
anchor         = max(entry_deadline, exit_instant)
anchor_session = session_on_or_after(anchor.date(), mic)

required_end   = entry_deadline                                      # the ladder's whole declared life
desired_end    = session_close_utc(advance_trading_sessions(anchor_session,
                                   _WINDOW_MARGIN_SESSIONS, mic), mic)        # margin = 2
last_servable  = previous_trading_day(session_on_or_after(now_utc.date(), mic), mic)
servable_end   = session_close_utc(last_servable, mic)

if servable_end < required_end:  REFUSE   bars_window_unservable / deadline_not_yet_servable
window_start   = walk_start
window_end     = min(desired_end, servable_end)
```

One function computes both the filled and the never-filled case, because a module that computes one quantity on two sides and does it twice ends up describing two different panels.

### Why the anchor is `max(deadline, exit)`

Measured on the four comparable closed picks (**M3**):

| pick | MIC | walk_start | entry_deadline | real exit (`exits[-1].venue_time`) |
|---|---|---|---|---|
| SMMT:2026-09-23 | XNAS | 2026-09-23T13:30:00Z | 2026-10-02T20:00:00Z | 2026-09-29T13:30:58.853Z |
| ASTS:2026-09-23 | XNAS | 2026-09-23T13:30:00Z | 2026-10-02T20:00:00Z | 2026-09-29T13:36:34.887Z |
| EWTX:2026-09-25 | XNAS | 2026-09-25T13:30:00Z | 2026-10-06T20:00:00Z | 2026-09-30T17:26:32.675Z |
| VST:2026-09-21 | XNYS | 2026-09-21T13:30:00Z | 2026-09-30T20:00:00Z | 2026-10-01T13:59:21.248Z |

VST's real exit is **after** its entry deadline, and the other three are before it. Taking the exit alone would truncate three windows while the ladder was still live; taking the deadline alone would truncate VST.

### Why `required_end` is hard and the margin is soft

The two edges of the window are not the same kind of fact.

* **`required_end = entry_deadline` is not negotiable.** It is the whole interval the entry ladder was legally alive over, and the script cannot know in advance whether the replay will fill: that is the output. A window that stops before it answers a question nobody asked. With an inclusive close — reduced or not — a window that reaches it exactly produces the right published answer: **M6**, rows 2 and 3, `outcome: no_fill` with `trace: ['entry_expired']` at `2026-10-09T20:00:00Z`.
* **The margin beyond it is best-effort, and truncating it is safe because it is VISIBLE.** The replay can enter later than reality — that is the divergence a trailing-entry question is about — so its own exit can fall later than the real one. Two sessions is bounded and cheap. If the vendor cannot serve the margin, the window is simply shorter: a never-filled run has already published `entry_expired` (**M6**), and a filled run publishes the terminal state `open`, "The bars ran out with the position still open." (`trace.py:91`, `:197`), which reads as "re-run with more bars". The sidecar records `margin_sessions_requested` and `margin_sessions_served`, so a shortened margin is stated rather than inferred.

### Why the cap is the vendor's CALENDAR DAY, not the last closed session

The vendor restriction is calendar-day based. `polygon_client.py:236-239`:

> "NOTE (operational): Polygon's free / Basic plan serves only PAST-day minute aggregates, not the current session. Callers must request windows that have fully closed."

A cap built on `session_not_closed` instead tracks the wall clock and crosses into the current calendar day after the close. **M5**:

| `now` | `session_not_closed` | `previous_trading_day` of it | that cap | `previous_trading_day(session_on_or_after(now.date()))` |
|---|---|---|---|---|
| 2026-10-09T08:14Z | 2026-10-09 | 2026-10-08 | 10-08T20:00Z | **2026-10-08** |
| 2026-10-09T12:00Z | 2026-10-09 | 2026-10-08 | 10-08T20:00Z | **2026-10-08** |
| 2026-10-09T21:00Z | 2026-10-12 | **2026-10-09** | **10-09T20:00Z** | **2026-10-08** |

The right-hand column is this design's rule, and it is stable across the session's close: an evening run never asks for a session the vendor will not serve. **GAP:** the vendor note does not say in which timezone its day boundary falls, and measuring it needs a live call this design pass did not make. This design uses the UTC date and does not rely on being right about it — the post-fetch check below closes the hole whatever the boundary is. §16 Q3 records the objection that follows from the GAP.

### The refusal

```
code      bars_window_unservable
reason    deadline_not_yet_servable
exit      7            (the reserved transient status; time fixes it, re-running is safe)
retryable true
```

Human message, for `SYM:2026-09-30` run at `now = 2026-10-09T08:14:03Z`:

> `replay_from_pick: bars_window_unservable: SYM:2026-09-30's entry ladder was live through session 2026-10-09 (entry_deadline 2026-10-09T20:00:00.000Z), and the price vendor serves only past calendar days. The last session it can serve today is 2026-10-08 (close 2026-10-08T20:00:00.000Z), which stops short of the deadline. Re-run on 2026-10-10 or later. Replaying the short window would publish outcome no_fill with an empty trace, which is what a ladder that genuinely expired also publishes.`

`--format json` object:

```json
{"schema": "alphalens.research.replay_from_pick.error/v1",
 "code": "bars_window_unservable", "reason": "deadline_not_yet_servable",
 "message": "...", "retryable": true,
 "details": {"pick": "SYM:2026-09-30", "exchange": "XNAS",
             "entry_deadline_t": 1791576000000,
             "entry_deadline_session": "2026-10-09",
             "last_servable_session": "2026-10-08",
             "last_servable_end_t": 1791489600000,
             "servable_from_date": "2026-10-10",
             "now": "2026-10-09T08:14:03.000Z"},
 "suggestions": [{"argv": ["python", "-m", "scripts.replay_from_pick", "<trades.json>",
                           "SYM:2026-09-30", "--out", "<DIR>"]}]}
```

`servable_from_date` being a Saturday is harmless: at 2026-10-10, 10-11 and 10-12 the rule gives `last_servable = 2026-10-09`, which clears the gate (**M7**).

### And the same check again AFTER the fetch

Predicting the vendor's day boundary is a claim; checking the series is a measurement. So the kept series must also *reach* `required_end`: the last kept bar's `t` must be at or after `entry_deadline`. If it is not, the run refuses with the same code and reason `vendor_served_short`, naming the last bar it actually got. This runs on the `--bars` path too, where it is the single most valuable new guard: a supplied file that stops before the ladder's deadline is refused rather than replayed. It is the symmetric partner of the engine's own left-edge `check_window_covers`.

### How often the refusal fires, and what the earlier draft's `min()` did instead

**M7**, the corrected rule applied to all 12 comparable fixture records at `now = 2026-10-09T08:14:03Z`:

| pick | MIC | entry_deadline | desired end | servable end | verdict |
|---|---|---|---|---|---|
| ASTS:2026-09-23 | XNAS | 10-02T20:00Z | 10-06T20:00Z | 10-08T20:00Z | run, margin 2/2, end 10-06 |
| BE:2026-09-29 | XNYS | 10-08T20:00Z | 10-12T20:00Z | 10-08T20:00Z | run, margin 0/2, end 10-08 |
| **BE:2026-09-30** | XNYS | **10-09T20:00Z** | 10-13T20:00Z | 10-08T20:00Z | **REFUSE** |
| DAVE:2026-09-25 | XNAS | 10-06T20:00Z | 10-08T20:00Z | 10-08T20:00Z | run, margin 2/2, end 10-08 |
| EWTX:2026-09-25 | XNAS | 10-06T20:00Z | 10-08T20:00Z | 10-08T20:00Z | run, margin 2/2, end 10-08 |
| **OSCR:2026-09-30** | XNYS | **10-09T20:00Z** | 10-13T20:00Z | 10-08T20:00Z | **REFUSE** |
| PLTR:2026-09-29 | XNAS | 10-08T20:00Z | 10-12T20:00Z | 10-08T20:00Z | run, margin 0/2, end 10-08 |
| QBTS:2026-09-21 | XNAS | 09-30T20:00Z | 10-02T20:00Z | 10-08T20:00Z | run, margin 2/2, end 10-02 |
| SMMT:2026-09-23 | XNAS | 10-02T20:00Z | 10-06T20:00Z | 10-08T20:00Z | run, margin 2/2, end 10-06 |
| **SYM:2026-09-30** | XNAS | **10-09T20:00Z** | 10-13T20:00Z | 10-08T20:00Z | **REFUSE** |
| VCYT:2026-09-29 | XNAS | 10-08T20:00Z | 10-12T20:00Z | 10-08T20:00Z | run, margin 0/2, end 10-08 |
| VST:2026-09-21 | XNYS | 09-30T20:00Z | 10-05T20:00Z | 10-08T20:00Z | run, margin 2/2, end 10-05 |

**3 of 12 refuse, all three never-filled with a deadline on the current day. 3 run with a truncated margin and say so. 6 get the full margin.** All three refusals clear on 2026-10-10 with no code change, and the verdicts are identical at `now = 21:00Z` (**M5/M7**). Nothing proceeds silently truncated: the gate is `servable_end >= required_end`, and `min(desired, servable) >= required` follows from it.

Under the earlier draft's rule — `min()` with no refusal, and a half-open close — **6** of the 12 silently ran a window that never reached the deadline: the three above by a whole session, plus BE:2026-09-29, PLTR:2026-09-29 and VCYT:2026-09-29 by exactly 60 seconds, because half-open leaves the last bar at `close − 60 000` (**M7**, `m7b.py`). Six of the eight never-filled records. The inclusive close of §5 repairs exactly the three one-minute cases, and the refusal covers the other three. Both corrections are needed; neither is sufficient alone.

### The reader-side signal that complements the refusal

`bd724cb2` (#1746) added `walked_from_t` / `walked_to_t` / `walked_bars` to the walk result (`walk.py:120-122`) and publishes them as the envelope's `walked` block (`envelope.py:162-164`), the fifth of twelve keys (`apps/intent-replay/README.md:223`; **M6** confirms the twelve: `schema`, `intent_id`, `instrument`, `window`, `walked`, `config`, `fx`, `divergences`, `intrabar_rule`, `outcome`, `summary`, `trace`). So the condition

```
outcome == "no_fill"  AND  walked.to_t < config.entry_deadline
```

is the published, machine-readable statement "this run ran out of tape before the ladder's life ended", and the engine's README already explains why `walked` exists for exactly this kind of reading (`:298-309`). **M6** confirms both halves: the truncated run reports `walked.to_t = 2026-10-09T19:59:00Z` against a deadline of `2026-10-09T20:00:00Z`, and an empty `trace`, where the full runs report `walked.to_t` at or past the deadline and `trace: ['entry_expired']`. Two independent signals, one per key. The script's refusal stops the run from being produced; these two let a reader catch a run produced some other way.

### Worked window, under the corrected rule

`SMMT:2026-09-23`, `now = 2026-10-09T08:14:03Z`, measured (**M7**):

| | |
|---|---|
| `walk_start` | `2026-09-23T13:30:00Z`, epoch `1790170200000` |
| `exits[-1].venue_time` | `2026-09-29T13:30:58.853Z` |
| `entry_deadline` = `required_end` | `2026-10-02T20:00:00Z`, epoch `1790971200000` |
| `anchor` = max of the two | `2026-10-02T20:00:00Z`, session `2026-10-02` |
| `desired_end` = +2 XNAS sessions | `2026-10-06T20:00:00Z`, epoch `1791316800000` |
| `last_servable` / `servable_end` | `2026-10-08` / `2026-10-08T20:00:00Z`, epoch `1791489600000` |
| `servable_end >= required_end`? | yes — no refusal |
| `window_end` = `min(desired, servable)` | `2026-10-06T20:00:00Z`, margin **2 of 2 served** |
| sessions in the window | 10 (2026-09-23 through 2026-10-06, none a half-day) |
| kept-bar ceiling, inclusive | 10 × 391 = **3 910** bars |
| of those, reduced to one price | **10**, one per session |
| raw fetched bars, at the measured ratio 1.1342 (**M1**) | ≈ **4 434**, against a one-call limit of 50 000 (`polygon_client.py:214`) |

The figure is 391 minutes per session, not 390, precisely because the close bar is kept.

---

## 7. Order of operations, which is load-bearing

```
1  _assert_comparable                  (replay_from_pick.py:322-333)   — no quota on an excluded pick
2  entry-trail distance                (:451-470)
3  exit instant                        exits[-1].venue_time, or absent
4  window                              §6
5  servable-window check               REFUSE bars_window_unservable / deadline_not_yet_servable
6  corporate-action lookup             cache first, so a repeat run makes no call   §8
7  bar cache read                      §10
8  fetch                               bar_fetch(ticker, window_start, window_end)
9  translate                           build from bars._BAR_KEYS
10 session-grid membership             filter (fetch) / REFUSE bars_off_grid (--bars)   §5.1, §5.4
11 close-bar reduction                 one price per close-stamped bar, recorded       §5.2
12 granularity gate                    bars_wrong_granularity / not_one_minute         §5.3
13 left-edge coverage                  first kept bar vs walk_start
14 right-edge coverage                 REFUSE bars_window_unservable / vendor_served_short
15 write bars, then the sidecar
```

Steps 1 and 2 run before any vendor call, exactly as they do today, and step 5 now joins them, so an excluded pick and an unanswerable window both refuse before quota is spent. Step 11 sits after membership and before the granularity gate: membership must be settled before the bar's content is rewritten, and the reduction cannot change a modal spacing, so the gate reads the same number either side of it.

---

## 8. The corporate-action guard

**The lookup is asked on every run, and both of its unhappy answers refuse.** It carries exactly one threshold, which it inherits.

```
start = window_start.date() - ACTION_WINDOW_PRE_CALENDAR_DAYS      # 3, corporate_actions.py:101
end   = window_end.date()   + ACTION_WINDOW_POST_CALENDAR_DAYS     # 1, corporate_actions.py:102
CachedCorporateActionsLookup(PolygonCorporateActionsLookup(pre_ex_close=...),
                             <bar-cache>/corporate_actions_cache.json).lookup(ticker, start, end)
  found         -> REFUSE, exit 1, bars_corporate_action, the lookup's own detail named
  none found    -> write the bars; sidecar corporate_actions.verdict = "none"
  lookup raised -> REFUSE, exit 7, retryable, corporate_actions_unanswered
```

The guard is unaffected by D4 in one direction and sharpened by it in the other. Unaffected: the lookup is about the window, not about any one bar. Sharpened: a close-stamped bar is now one price, so an ex-date step down cannot arrive as a widened `[low, high]` on the close minute — it arrives, as it should, in the next session's prices, where the guard is the only thing that catches it.

### The one threshold, named, because the composition carries it

`PolygonCorporateActionsLookup._first_material_dividend` (`corporate_actions.py:178-205`) applies, at `:201`:

```python
if cash / close > SPECIAL_DIVIDEND_PRE_EX_CLOSE_FRACTION:
```

with `SPECIAL_DIVIDEND_PRE_EX_CLOSE_FRACTION = 0.10` declared at `corporate_actions.py:83`. That is a size threshold on a quantity, inside this composition, and a design that claimed to add none would be wrong about its own parts. What is true is narrower and is the part that matters: **this design adds no threshold on a RETURN**, and it adds no fourth number of its own beyond `_WINDOW_MARGIN_SESSIONS = 2`, which is a count of sessions and not a test on a measured price. The close-bar reduction adds no number at all: it is a shape rule, not a magnitude.

The inherited 0.10 is the right threshold to inherit, for a reason the module itself records at `:79-82`: "Ordinary quarterly dividends never come near it." Its denominator is the pre-ex close from the raw grouped-daily store, so it compares like with like.

**Known residual, stated rather than solved:** a dividend *below* 0.10 of the pre-ex close does not refuse the run, and raw (`adjusted=false`) bars do step down by it on the ex-date, which a resting stop can be hit by. **GAP:** the size of an ordinary dividend as a fraction of the close is not measured in this store in this pass, so the magnitude of that residual is unknown. It is bounded above by 10% of the close by construction.

### Why no return threshold

1. **The quantity the 0.60 is defined on does not exist in a replay.** `IMPLAUSIBLE_RETURN_THRESHOLD = 0.60` (`bar_window.py:33`) is applied to a *trade* return against a frozen anchor: `population_ladder_monitor.py:788` uses `abs(forward_return)` against the frozen arrival-window minute VWAP, `ladder_replay.py:1683-1687` uses `abs(exit_mark / arm_blended - 1)`. A replay has no such anchor — its entry price is an output of the run. Choosing a threshold here would mean inventing a quantity to apply it to.
2. **Size thresholds on returns have measured zero detection power in this store.** `feedback/split_audit.py:10-14` on the old band, "fail the row when a close ratio leaves `(0.55, 1.8)`": it "caught zero splits and produced three false positives (one real -47% day, reported identically by a second vendor), while being structurally blind to 3-for-2, 4-for-3 and 5-for-4 — roughly a fifth of US splits. It cannot be fixed by widening: catching a 3-for-2 means failing every -33% earnings day." The same module records the method that does work — persistence, not size: on MQ, the one real artefact in the store (1-for-4 reverse), the cross-vendor ratio is exactly 0.2500 for 20 sessions and exactly 1.0000 for the following 22, while over 13 203 artefact-free comparisons it deviates from its own median by 0.000000 at the 99th percentile (`split_audit.py:25-32`).
3. **Raw bars are the correct basis, so in most windows there is nothing to detect.** `get_agg_range` defaults to `adjusted=False` (`polygon_client.py:212`), and the document's limits, stops and take-profits are prices the author wrote from live unadjusted quotes. `split_audit.py:37-40` states the arithmetic: a uniform rescaling of every close in a window cancels out of the return, so only a window that *crosses* a break carries a fabricated step.
4. **A window that does cross an action is a run to refuse, not a value to rescale.** Real brokers cancel resting orders on corporate actions, and the Saxo rail this project trades on does. A mechanically adjusted resting ladder models an order state that would not have existed. That is the `SPLIT_INVALIDATED` class (`corporate_actions.py:62-66`), and refusing matches how the script already treats `replay_exclusions` (`replay_from_pick.py:322-333`).

### Why a failed lookup refuses, when the block it comes from carries the row forward

The block's disposition tree says `lookup failed → carry, counted` (`corporate_actions.py:18`), and the lookup is fail-closed by construction: a missing pre-ex close raises rather than defaulting (`:195-199`, docstring `:131-135`). **"Carry, counted" is a DEFERRAL, not a permission.** The monitor sweeps the whole population nightly, so carrying a row means "ask again in 24 hours" — the row is not published as clean and it is not published as dirty; it is held.

A one-shot laboratory command has no nightly sweep, so its only honest analogue of a deferral is a refusal the operator can re-run: exit 7, `retryable: true`, reason `lookup_failed`, with the message saying the question could not be answered and that re-running will ask again. Writing the bars with a `lookup_failed` verdict, as an earlier draft did, would open a gate the composed block closes — it publishes a run whose basis was never checked, which is the shape of the incident this whole design exists for.

The consequence is that only **one** verdict is ever written into a sidecar: `"none"`. That is the design, not an accident. "Asked and clean" is the written file; "found an action" and "could not ask" are both refusals, each with its own code. A reader holding a sidecar therefore knows the lookup answered, without having to trust a field that might have meant three things.

### What it costs, worst case

Per run: 1 aggregate call, 1 splits call, 1 dividends call, and **one whole-market grouped-daily call per dividend record whose ex-date is not already cached** — `_first_material_dividend` calls `pre_ex_close(upper, ex_date)` once per record, inside the loop, before testing materiality (`corporate_actions.py:184-199`), and the monitor's own `_grouped_pre_ex_close` fetches a whole-market payload per session (`population_ladder_monitor.py:1609-1620`). Two dividends in one window is two such calls. Everything is cached: FOUND forever, NONE-FOUND for `NONE_FOUND_CACHE_TTL_DAYS = 14` (`corporate_actions.py:95`, applied `:258`), keyed `TICKER:start:end` (`:229`). So a repeat run of the same pick inside 14 days makes no vendor call for this question at all.

**Known limit, with its cause named.** `get_grouped_daily`'s path is `/v2/aggs/grouped/locale/us/market/stocks/{date}` (`polygon_client.py:271`, `:297`) — US equities only. A non-US ticker is absent from the response, `pre_ex_close` returns `None`, and `_first_material_dividend` raises, so a dividend-paying XWAR / XETR pick can only ever reach `corporate_actions_unanswered`. **GAP:** all 12 comparable fixture records are XNAS or XNYS (**M3/M7**), so no non-US pick exercises this path today and its frequency is unmeasured.

### The other thresholds in the tree

| where | quantity | span | what tripping it does |
|---|---|---|---|
| `corporate_actions.py:83`, `0.10` | `cash_amount / pre_ex_close`, one dividend record | one ex-date | the dividend counts as a corporate action — **this design inherits it** |
| `bar_window.py:33`, `0.60` | trade return against a frozen arrival VWAP, or exit mark against blended entry | up to 42 sessions | triggers the corporate-actions lookup (demoted from verdict to trigger by #1090, `corporate_actions.py:1-27`) |
| `population_ladder_monitor.py:207`, `_SPLIT_SCREEN_THRESHOLD = 0.18` | `abs(c_star / prev_c - 1)`, one consecutive-session close ratio, used at exactly one site (`:2062`) | 1 session | forces a minute resolve instead of the cheap daily path — a path choice, not a verdict |
| `scripts/whatif_trailing_entry.py:81`, `scripts/whatif_trailing_tp.py:111`, `0.60` | `abs(m / entry - 1)` (`whatif_trailing_entry.py:294`; `whatif_trailing_tp.py:322`, `:367`) | position life | drops the row — copied literals, each carrying a `# mirror bar_window.IMPLAUSIBLE_RETURN_THRESHOLD` comment |
| `feedback/split_audit.py` | cross-vendor level ratio by median persistence | 5 sessions each side of a candidate step | fails the span — the only method with a demonstrated true positive |

Two corrections to the record while reading these, neither touched by this change: the comment on `IMPLAUSIBLE_RETURN_THRESHOLD` says "5-session window" (`bar_window.py:30`) while its live consumer runs a horizon of up to 42 sessions, and `_SPLIT_SCREEN_THRESHOLD`'s comment claims a second application that `_cheap_update_row`'s own docstring disclaims.

---

## 9. The provenance sidecar

File: `<out>/<stem>.bars.provenance.json`, beside `<stem>.bars.json`. Written on both paths. Every epoch below is the real one for the §6 worked example, computed from the calendar (**M7**); the `close_bar_reduction.reduced` entry shown is the real CRSR quadruple from **M11**, put here because the store holds no SMMT bars (§5.1 GAP).

```json
{
  "schema": "alphalens.research.replay_bars/v1",
  "pick": "SMMT:2026-09-23",
  "ticker": "SMMT",
  "mic": "XNAS",
  "source": "polygon",
  "source_path": null,
  "source_sha256": null,
  "bars_path": "/tmp/smmt/SMMT_2026-09-23.bars.json",
  "vendor": {
    "name": "polygon",
    "endpoint": "/v2/aggs/ticker/{ticker}/range/1/minute",
    "client": "alphalens_pipeline.data.alt_data.polygon_client.PolygonClient"
  },
  "granularity": {
    "modal_spacing_ms": 60000,
    "label": "1m",
    "source": "measured modal t-spacing of the kept series"
  },
  "adjusted": false,
  "window": {
    "from_t": 1790170200000,
    "to_t": 1791316800000,
    "from_utc": "2026-09-23T13:30:00.000Z",
    "to_utc": "2026-10-06T20:00:00.000Z",
    "from_formula": "config.walk_start (session_open_utc(2026-09-23, XNAS))",
    "to_formula": "min(session_close_utc(advance_trading_sessions(max(entry_deadline, exits[-1].venue_time).date(), 2, XNAS), XNAS), session_close_utc(last_servable_session, XNAS))",
    "required_end_t": 1790971200000,
    "required_end_formula": "config.entry_deadline",
    "margin_sessions_requested": 2,
    "margin_sessions_served": 2,
    "last_servable_session": "2026-10-08"
  },
  "session_filter": {
    "predicate": "open_ms <= t <= close_ms",
    "calendar": "exchange_calendars via alphalens_pipeline.market.calendar",
    "exchange": "XNAS",
    "sessions": [
      {"session": "2026-09-23", "open_t": 1790170200000, "close_t": 1790193600000, "half_day": false},
      {"session": "2026-09-24", "open_t": 1790256600000, "close_t": 1790280000000, "half_day": false}
    ]
  },
  "close_bar_reduction": {
    "applied": true,
    "rule": "open = high = low = close = the bar's own open",
    "why": "the minute stamped at the close spans close..close+59s; the session ends at the close",
    "bars_reduced": 10,
    "range_narrowed": 1,
    "reduced": [
      {"t": 1790971200000, "session": "2026-10-02",
       "from": {"open": 10.89, "high": 12.8, "low": 10.61, "close": 12.79},
       "to": 10.89,
       "outside_rest_of_session": true}
    ]
  },
  "counts": {"fetched": 4434, "kept": 3910, "dropped_off_grid": 524,
             "sessions": 10, "close_stamped": 10},
  "corporate_actions": {
    "verdict": "none",
    "window": {"start": "2026-09-20", "end": "2026-10-07"},
    "detail": null,
    "source": "polygon /v3/reference/{splits,dividends}"
  },
  "cache": {"hit": false,
            "path": "~/.alphalens/replay_bars/bars/SMMT_1790170200000-1791316800000_1m_raw.json"},
  "fetched_at": "2026-10-10T08:14:03.512Z",
  "written_at": "2026-10-10T08:14:03.904Z",
  "sha256": "<sha256 of the bytes of bars_path>"
}
```

`sessions` and `reduced` are abbreviated here; the real file lists all 10 of each, because those two lists are precisely the facts the lost SMMT file could not answer. The `counts` are the shape — `fetched` and `dropped_off_grid` are derived from the measured 1.1342 ratio (**M1**) against the 3 910-bar ceiling rather than from a run — and the real ones are written per run.

**What a reader can verify with it, without the tool:**

* that the number came from these bars: `shasum -a 256 <bars_path>` must equal `sha256`;
* that no bar lay outside trading hours: read `session_filter.sessions` and check each `t` against the stated predicate, which is spelled out including its inclusiveness;
* **that a reduced series is a reduced series**: `close_bar_reduction.applied` and `granularity.modal_spacing_ms` are both in the file, so a reader never has to re-derive either from the bars — and `granularity` is measured, so it cannot assert `1m` about an hourly file;
* **what the reduction changed, and how to undo it**: every reduced bar's `t`, session, original `{open, high, low, close}` and resulting single price are listed. `range_narrowed` is the count of reduced bars whose original high or low lay outside the rest of their session's range, which is the discriminating number — it runs at 1.161% of sessions (**M10**), where `close_stamped` is near-constant at one per session (91.34% of real pairs carry one, **M1**) and therefore carries no signal on its own;
* that the basis was raw: `adjusted: false`, with the vendor and endpoint named;
* that the window covered the ladder's whole life: `window.required_end_t` against `window.to_t`, in one object;
* whether the margin was shortened: `margin_sessions_requested` against `margin_sessions_served`, with `last_servable_session` saying why;
* **whether an exit landed on a reduced bar**: join the exit's `t` from the engine's own `trace` against `close_bar_reduction.reduced[].t`. In **M11** the whole-bar run's `tp_fired` carries `t = 2026-08-06T20:00:00Z`, which is exactly the reduced bar's `t` — that join is the audit this sidecar exists for, and it is a per-run answer rather than a population rate;
* that the series was not a stale cache claimed as fresh: `cache.hit` with `fetched_at` carried from the cache entry, never restamped;
* that corporate actions were asked about and answered: `verdict` is `"none"`, the only value a written sidecar carries (§8).

With `--bars`: `source` is `"supplied"`, `source_path` is the operator's path, `source_sha256` is the sha256 of that file's bytes, `vendor` is `null`, `window.from_t` / `to_t` are the supplied file's own first and last `t`, `session_filter.sessions` is the grid the validation built over that span, `counts.dropped_off_grid` is `0` by construction (any off-grid bar refuses the run), `close_bar_reduction` is filled exactly as on the fetch path, and `sha256` is the sha256 of the **written** bars file — the reduced series the replay actually reads. Two hashes rather than one, because on that path the input and the walked series are different files.

---

## 10. The cache

**Root.** `~/.alphalens/replay_bars/`, overridable with `--bar-cache DIR`. The default resolves through `state_paths._alphalens_home()` (`state_paths.py:87`) rather than its own `Path.home()` join, so it sits inside the seam the #1696 operator-state guard already wraps — `_GUARDED_BUILDERS` lists `(state_paths, "_alphalens_home")` at `home_isolation.py:112-115`. A private cross-package import is acceptable here for exactly that reason: a future broker-tier test that runs this command without an override gets a loud `OperatorStateReadError` (`home_isolation.py:120`) instead of reading and writing the operator's real cache. There is no private-import gate in the tree.

**Layout and key.**

```
~/.alphalens/replay_bars/
  bars/<TICKER>_<from_t>-<to_t>_<granularity>_<raw|adj>.json   # the raw vendor payload
  grouped/<YYYY-MM-DD>.json                                    # one whole-market daily session
  corporate_actions_cache.json                                 # CachedCorporateActionsLookup
```

The key is `(ticker, from_t, to_t, granularity, adjusted)` — D3's key, spelled into the file name so a human can read it. **The cache holds the RAW vendor payload, before translation, before the grid filter and before the reduction.** That is deliberate: a cached file must stay a faithful record of what the vendor sent, so that a later change to either rule re-derives rather than inherits — the same separation `_filter_bars_to_rth` states for its own cache, "The cache itself keeps every fetched bar (a faithful raw record); this filter applies at replay time only" (`population_ladder_monitor.py:753-754`). It also follows the standing rule for metered vendors (CLAUDE.md on the iVolatility cache: persist raw API responses before processing, never re-fetch on retry). `granularity` is `"1m"` in the key — the vendor request's granularity, which is a request parameter and not a measurement — and `adjusted` is the constant `raw`; both stay in the key so that a later day-bar variant can never read a minute file as if it were its own.

**Staleness: there is none, by key design.** Four rules, each with a precedent:

1. **No TTL.** A key's window ends at a session the vendor has already published, so the answer cannot change. Presence means complete — the rule `_read_grouped_cache` states for its own key (`population_ladder_monitor.py:602-608`). This is deliberately *not* the monitor's forward-only tail-extend (`_extend_bar_cache`, `:548-582`): that exists because a brief's window grows at the horizon end, and it is why the window belongs in the key here rather than in a growing file.
2. **Atomic write:** temporary file plus `os.replace`, the idiom of `_write_cached_bars` (`population_ladder_monitor.py:536-546`).
3. **Corrupt means ignore and refetch, with a warning, never crash** — `_read_cached_bars` (`population_ladder_monitor.py:519-534`).
4. **Successes only.** A failed or empty fetch is never written, so a later run re-pays rather than inheriting a hole. Same rule as the Buffett qualitative cache (CLAUDE.md, `~/.alphalens/buffett_qual/`).

**A cache miss with no network.** The fetch raises, nothing is written, and the command refuses with `bars_fetch_failed`, reason `vendor_error`, exit **7** (transient, retryable), and a suggestion naming the offline route:

```
suggestions: [{"argv": ["python", "-m", "scripts.replay_from_pick", "<trades.json>", "<PICK>",
                        "--out", "<DIR>", "--bars", "<BARS.json>"]}]
```

Exit 7 is the reserved transient status in `~/Developer/CLAUDE.md`'s CLI section; the script has no 7 today (`replay_from_pick.py:430`) and gains it here. Without it, a Polygon timeout falls into the existing `except (OSError, ValueError)` at `:518` and reports 1, "the pick cannot be replayed", which is a false statement about the pick.

---

## 11. Refusals

All refusals print nothing on stdout. In `--format human` (the default) the message goes to stderr as prose, prefixed with the code: `replay_from_pick: <code>: <message>`. In `--format json` the command prints **one JSON object as the last line of stderr**, the rule the broker CLI already follows:

```json
{"schema": "alphalens.research.replay_from_pick.error/v1",
 "code": "bars_off_grid", "reason": "after_close",
 "message": "...", "retryable": false,
 "details": {...}, "suggestions": [{"argv": [...]}]}
```

The **four** existing print-and-return sites (`replay_from_pick.py:491-496`, `:499-501`, `:504-508`, `:518-520`) are routed through one `_fail(code, reason, message, status, **details)` helper and gain codes (`usage`, `report_unreadable`, `pick_not_in_report`, `not_comparable`, `entry_trail_unknown`), so the envelope is uniform. Their human wording is unchanged apart from the code prefix. The existing tests assert with `assertIn` at `:584`, `:585`, `:602`, `:620`, `:635`, `:652`, `:665` and `:685`, and with one `assertNotIn("--entry-trail-bps", err)` at `:666` — so the prefix changes no meaning, and the new messages must keep `--entry-trail-bps` out of the not-comparable refusal. Argparse's own errors keep their own text and status 2.

New codes, one per failure MODE with a closed `reason` vocabulary — the shape `broker arm` uses, because fifteen top-level codes for six modes is a worse table to read:

| code | reasons | exit | retryable | details |
|---|---|---|---|---|
| `bars_off_grid` (`--bars` only) | `after_close`, `before_open`, `not_a_session` | 1 | no | `count`, `offenders` (a list of `{t, session, session_open_t, session_close_t, reason}`), `exchange`, `path` |
| `bars_window_unservable` | `deadline_not_yet_servable`, `vendor_served_short` | 7 | **yes** | `pick`, `exchange`, `entry_deadline_t`, `entry_deadline_session`, `last_servable_session`, `last_servable_end_t`, `servable_from_date`, `last_bar_t`, `now` |
| `bars_unusable` | `empty_window`, `walk_start_uncovered` | 1 | no | `window`, `walk_start`, `first_t`, `bars` |
| `bars_wrong_granularity` | `not_one_minute` | 1 | no | `modal_spacing_ms`, `bars`, `sessions`, `path` |
| `bars_fetch_failed` | `vendor_error` | 7 | **yes** | `ticker`, `window`, `vendor_message` |
| `bars_corporate_action` | `split`, `special_dividend` | 1 | no | `detail`, `lookup_window` |
| `corporate_actions_unanswered` | `lookup_failed` | 7 | **yes** | `ticker`, `lookup_window`, `vendor_message` |
| `bars_unreadable` (`--bars` only) | `not_json`, `not_a_list`, `bar_not_object`, `t_missing`, `t_not_int` | 2 | no | `path`, `index` |
| `exit_time_unknown` | `not_journaled`, `non_finite` | 1 | no | `pick_key`, `null_reason`, `path` |

Three notes on the shape of this table:

* `bars_unreadable` is exit 2 because the operator named a path that is not the published shape: a usage error, not a statement about the pick. The script checks only `t`'s presence and type; price coherence, ordering and the closed key set stay the engine's job (§14).
* `details.offenders` is a list, not one triple, because the incident had two off-grid bars in two different sessions; a single `session` field would have described the first bar's session while `last_t` pointed at a bar from another.
* **There is no refusal for an unreduced close bar in a supplied file, and that is the design.** The reduction is a stated construction recorded in the sidecar, not a rule the input must already satisfy. Refusing instead would refuse every file built with the repo's own three RTH helpers (**M12**), which is the one input the `--bars` override exists to accept.

**The incident's refusal.** Message template, then the incident's own instants:

> `replay_from_pick: bars_off_grid: /tmp/smmt.bars.json carries <N> bars outside XNAS trading hours. The first is at 2026-09-28T21:30:00.000Z, 1h30m after that session's close 2026-09-28T20:00:00.000Z (session 2026-09-28, 13:30:00.000Z-20:00:00.000Z). Supplied bars are refused rather than filtered; drop the flag to fetch the window instead.`

`details` for that refusal, with the real epochs:

```json
{"reason": "after_close", "count": 1,
 "offenders": [{"t": 1790631000000, "reason": "after_close", "session": "2026-09-28",
                "session_open_t": 1790602200000, "session_close_t": 1790625600000}],
 "exchange": "XNAS", "path": "/tmp/smmt.bars.json"}
```

`<N>` is the count the file held; for the lost SMMT file it is unknowable, and that is the point of §9. Note what is **not** in this refusal: the bar at `2026-10-02T20:00:00.000Z` (epoch `1790971200000`), which an earlier draft refused. It is that session's closing minute, §5.1 keeps it, and §5.2 reduces it — and §5.1's GAP says what that does and does not settle about the incident.

---

## 12. The test plan

All tests live in `apps/alphalens-research/tests/test_replay_from_pick.py` (704 lines today), as `unittest.TestCase` classes in the file's existing style: sentence-shaped class names ending in `Test` with a docstring naming the error they stop, and sentence-shaped method names. They reuse the file's own helpers `_trades()` (`:45-58`), `_report` (`:529-532`) and `_run` (`:534-538`) — **after `_run` is widened to forward the three seams**, which it cannot do today (`:534-538` is `main(list(args))` with no keywords). Every fetch-path test injects a fake `BarFetch` and a fake `now`; a test that forgets is caught by the no-live-network guard (`tests/_net_guard.py`, installed at `tests/__init__.py:8-10`), not by Polygon. Every test passes `--bar-cache` into a `TemporaryDirectory`, the same discipline the existing tests apply to `--entry-trails`.

### Class A — `TheBarsAreFetchedNotHandBuiltTest` (fake `BarFetch`)

| method | asserts | production change that makes it fail |
|---|---|---|
| `test_it_writes_a_bars_file_the_replay_argv_points_at` | `<stem>.bars.json` exists and is the `--bars` value in `replay_argv` | keeping `args.bars or "<BARS.json>"` (`:554`) |
| `test_the_fetch_window_starts_at_walk_start` | the fake's recorded `start` equals `config["walk_start"]["value"]` | anchoring the fetch at `trade_date` midnight |
| `test_the_window_end_comes_from_the_last_exit_not_the_first` | on a two-exit copy of SMMT (the real `ALB:2026-09-08-g2` shape), the end anchors on the later exit | reading `exits[0]` |
| `test_a_pick_whose_exit_is_after_its_deadline_fetches_past_the_deadline` | for VST (exit `2026-10-01T13:59:21Z`, deadline `2026-09-30T20:00Z`) the end is at or after the exit | `window_end = entry_deadline` |
| `test_the_ticker_comes_from_the_document_block_the_mic_comes_from` | the fake receives `SMMT`, read from `document["instrument"]["ticker"]` | reading `instrument["symbol"]`, which no block carries (**M3**) |
| `test_a_null_exit_time_is_refused_rather_than_guessed` | on a constructed record with `venue_time.value: null`, status 1, `exit_time_unknown`, message names `null_reason` | falling back to `entry_deadline`; no fixture record exercises this (**M3**: 0 of 35) |
| `test_the_vendor_payload_is_translated_to_the_published_bar_shape` | the fake returns `t,o,h,l,c,v,vw,n`; the written bars carry exactly `t,open,high,low,close` and `intent_replay.bars` parses them | passing `o/h/l/c` through, refused as `missing_key`/`unknown_key` |
| `test_an_unmodelled_vendor_key_cannot_reach_the_bars_file` | the fake adds `otc: true`; it does not appear in the output | a subtractive converter that drops only `v`/`vw`/`n` |
| `test_a_vendor_failure_exits_7_and_says_it_is_retryable` | the fake raises; status 7, `retryable` true in the JSON envelope | letting it fall into `except (OSError, ValueError)` at `:518`, which reports 1 |
| `test_an_empty_window_is_refused_and_nothing_is_written` | the fake returns `[]`; status 1, `bars_unusable/empty_window`, no bars file | writing the empty list and leaving `bars_empty` to the engine |
| `test_a_grid_with_no_bar_at_walk_start_is_refused_here` | bars starting one minute after the open (the real 44-of-75 shape, **M9**); status 1, `walk_start_uncovered` | skipping the check, so the engine refuses later with `begins_after_walk_start` |
| `test_the_cache_holds_the_raw_payload_not_the_walked_series` | the cached JSON carries `o/h/l/c/v` and the unreduced close bar; the written bars file does not | caching after the filter and the reduction, which freezes both rules into the cache |

### Class B — `TheSessionGridIsTheVenueSOwnTest` (fake `BarFetch`; one test per legitimate input of §5.5)

| method | asserts | production change that makes it fail |
|---|---|---|
| `test_a_bar_stamped_at_the_session_close_is_kept` | with bars at 19:59:00Z and 20:00:00Z, **both** survive and `dropped_off_grid` is 0 | `<` at the close, which costs the official close in 93.48% of pairs (**M4**) and makes `_expire` silent (**M6**) |
| `test_a_bar_stamped_at_the_session_open_is_kept` | the opening bar survives | `>` at the open, after which every run fails `walk_start_uncovered` |
| `test_the_grid_is_the_venue_s_own_hours_not_a_390_minute_assumption` | on an XWAR copy of the document, a bar at 14:30Z (inside XWAR, outside open+390) is kept | `open_ms + 390 * 60_000`, that is, composing `_rth_window_ms` |
| `test_a_venue_whose_real_close_is_before_open_plus_390_drops_nothing_extra` | on an XSHG copy, a bar at 07:30Z (outside XSHG's 07:00Z close, inside open+390) is **dropped** | the same 390-minute composition, in its LONG direction (**M2**: XSHG +60, XTAI +120) |
| `test_a_half_day_grid_ends_at_the_early_close` | over XNAS 2026-11-27, 17:59:00Z and 18:00:00Z are kept and 18:01:00Z dropped | a constant full-session close |
| `test_a_window_spanning_the_dst_change_grids_each_session_on_its_own_hours` | over XNAS 2026-10-30 and 2026-11-02, a 13:30Z bar on 11-02 is dropped and a 20:30Z bar kept | one open/close pair reused for the whole window |
| `test_a_bar_on_a_day_that_is_not_a_session_is_dropped` | a Saturday bar (the real `KRP` 2026-08-15 shape, 2 pairs in the production cache, **M1**) does not survive | bucketing by UTC date without asking the calendar |
| `test_a_sparse_session_is_not_refused` | 13 bars for one session still produce a run (status 0) | any completeness or minimum-count check |
| `test_a_sparse_session_whose_commonest_gap_is_two_minutes_is_not_refused` | a session whose modal gap is 120 000 ms inside a window whose whole-series modal gap is 60 000 ms runs | phrasing the granularity gate per session, which refuses 26 real pairs (**M13**) |
| `test_a_one_bar_series_at_walk_start_is_not_refused` | a single bar at `walk_start` produces a run | a `len(bars) > 1` guard, or a granularity gate that does not exempt a series with no spacing |
| `test_a_daily_series_is_refused_by_granularity_not_by_the_grid` | a one-bar-per-session series over 3 sessions, each bar at that session's own open: the grid keeps all three (**M8**: 492 of 497 real daily files pass) and `bars_wrong_granularity/not_one_minute` refuses, with `details.modal_spacing_ms` 86 400 000 | relying on the grid to refuse daily bars, which the measurement says it does not |
| `test_an_hourly_series_is_refused_too` | 7 bars per session over 10 sessions: status 1, `not_one_minute`, `modal_spacing_ms` 3 600 000 | the one-bar-per-session rule, which is silent on hourly, 30-minute and 5-minute series (**M13**) |

### Class C — `TheCloseStampedBarIsKeptButReducedTest` (fake `BarFetch`)

This class is the whole of D4. Each row names the production change that reintroduces the defect it covers.

| method | asserts | production change that makes it fail |
|---|---|---|
| `test_the_close_stamped_bar_survives_the_grid` | the written series contains a bar at each session's `close_t`, and `counts.close_stamped` equals the session count | dropping it, i.e. a half-open close |
| `test_the_close_stamped_bar_carries_one_price` | for every written bar whose `t` is a session close, `open == high == low == close` | leaving the vendor quadruple in place |
| `test_the_one_price_is_the_bar_s_own_open` | the fake returns `{o 10.89, h 12.80, l 10.61, c 12.79}` at the close (the real CRSR quadruple, **M11**); all four written fields are 10.89 | using the bar's `close`, its mid, or its vwap — each of which carries post-market trading |
| `test_a_post_market_excursion_in_that_minute_cannot_fire_a_take_profit` | with a take-profit at 11.95, above the session's own high of 11.1717 and below the raw close bar's 12.80, the written series does not reach 11.95 and the engine's outcome is `open`, not `closed_tp` | keeping the whole bar, which flips this exact case by 0.828 R on the real engine (**M11**) |
| `test_a_post_market_excursion_in_that_minute_cannot_fire_a_resting_stop` | the mirror case, a low excursion below the disaster stop: no `stop` event | keeping the whole bar, which fills a resting broker stop at a post-market print — flatly wrong per `walk.py:497-498`, `:512` |
| `test_only_the_close_stamped_bar_is_reduced` | the bar at `close − 60 000` keeps its four distinct prices | reducing by position (the last bar of the series) instead of by `t == close_ms` |
| `test_the_last_bar_of_the_series_is_not_special` | a window whose final session contributes no close-stamped bar leaves its final bar untouched, and `bars_reduced` is one short of the session count | the same positional bug, in the direction that silently flattens a real intraday bar |
| `test_a_reduced_bar_still_lets_the_ladder_expire_on_the_deadline_millisecond` | for `SYM:2026-09-30` the engine's `trace` carries `entry_expired` at `config.entry_deadline`, not at the next session's open | dropping the close bar; **M6** rows 1 and 4 show the event going silent or firing 65.5 hours late |
| `test_the_engine_accepts_a_one_price_bar` | `intent_replay run` over the written series exits 0 | no production change — this is a pin on `bars.py:133-134`, which blesses a bar that "never moved at all"; it turns red if the engine's coherence rule narrows |

### Class D — `TheWindowMustCoverTheLadderSWholeLifeTest` (fake `BarFetch`, injected `now`)

| method | asserts | production change that makes it fail |
|---|---|---|
| `test_a_deadline_the_vendor_cannot_serve_yet_is_refused_naming_the_session` | `SYM:2026-09-30` at `now=2026-10-09T08:14Z`: status 7, `bars_window_unservable/deadline_not_yet_servable`, message names `2026-10-09` and `2026-10-10`, no bars file, **no vendor call** | a `min()` clamp, which runs a window ending `2026-10-08T20:00Z` and publishes `no_fill` with an empty trace (**M6**) |
| `test_the_servable_cap_does_not_move_after_the_close` | the same pick at `now=2026-10-09T21:00Z` still refuses; `last_servable_session` is `2026-10-08` | computing the cap from `session_not_closed`, which returns `2026-10-09` there (**M5**) |
| `test_a_pick_whose_deadline_is_servable_runs_with_whatever_margin_fits` | `BE:2026-09-29` at the same `now`: status 0, `margin_sessions_requested` 2 and `margin_sessions_served` 0 | refusing on a short margin, which would refuse 6 of 12 instead of 3 (**M7**) |
| `test_a_series_the_vendor_served_short_is_refused_after_the_fetch` | the fake returns bars ending one session before `entry_deadline`; status 7, reason `vendor_served_short`, `details.last_bar_t` names the last bar | trusting the pre-fetch cap, which depends on a vendor day boundary this design does not know (**GAP**, §6) |
| `test_a_supplied_file_that_stops_before_the_deadline_is_refused` | a grid-clean file ending at `2026-10-08T20:00Z` for `SYM:2026-09-30`; status 7, `vendor_served_short` | applying the right-edge check on the fetch path only |
| `test_a_never_filled_pick_window_reaches_its_deadline_instant` | for `QBTS:2026-09-21` the last written bar's `t` equals `config.entry_deadline` | a half-open close, under which `_expire` never fires (**M6**, row 1) |

### Class E — `ASuppliedBarsFileIsValidatedNotTrustedTest` (no `BarFetch`, no network)

| method | asserts | production change that makes it fail |
|---|---|---|
| `test_an_off_grid_bar_in_a_supplied_file_is_refused_naming_its_session_boundary` | the incident file rebuilt with its bar at `2026-09-28T21:30Z`: status 1, `bars_off_grid`, stderr names both that instant and `2026-09-28T20:00:00.000Z` | today's behaviour, which never opens the file, gives status 0 |
| `test_a_file_built_with_this_repo_s_own_rth_filter_is_accepted` | a file whose every session ends with its close-stamped bar — what `_rth_window_ms` and `_filter_bars_to_rth` produce (**M12**: 0 mismatches on 40 real files) — runs, with `dropped_off_grid` 0 | a half-open close, or refusing an unreduced close bar; either sends the operator to surrender the override |
| `test_a_supplied_close_stamped_bar_is_reduced_and_the_file_is_not_touched` | the written `<stem>.bars.json` carries the reduced bar; the supplied path's bytes are byte-identical before and after | reducing in place, which rewrites an operator's file |
| `test_the_replay_argv_points_at_the_written_series_not_the_supplied_file` | `replay_argv`'s `--bars` value is `<stem>.bars.json` | pointing at `args.bars`, which would replay the UNREDUCED series while the sidecar claims a reduction |
| `test_two_off_grid_bars_in_two_sessions_are_both_named_in_the_json` | `details.offenders` carries two entries with two different `session` values | one `session`/`session_open_t`/`session_close_t` triple for the whole refusal |
| `test_a_supplied_file_is_not_silently_filtered` | one off-grid bar refuses; no filtered copy is written | reusing the fetch path's membership filter for a supplied file |
| `test_a_supplied_file_that_is_not_the_published_shape_exits_2` | not an array, and a `t` that is a string: status 2, `bars_unreadable` | parsing with no shape check, which raises a traceback |
| `test_a_supplied_file_gets_a_sidecar_with_both_hashes` | `source_sha256` equals `hashlib.sha256(supplied.read_bytes())` and `sha256` equals the same of the written file; the two differ whenever `bars_reduced > 0` | one hash, which cannot distinguish the input from the walked series |

### Class F — `TheProvenanceSidecarMakesAPublishedNumberRecheckableTest` (fake `BarFetch`)

| method | asserts | production change that makes it fail |
|---|---|---|
| `test_the_sidecar_carries_the_session_filter_the_run_used` | the predicate string `open_ms <= t <= close_ms`, the exchange, and every session's `open_t`/`close_t`/`half_day` | recording only the exchange, which is the SMMT loss again |
| `test_the_predicate_string_matches_the_code_that_filtered` | the sidecar's predicate string is built from the same module constant the filter uses, not written twice | two spellings of one rule, which is the defect the engine's own `walked` docstring warns about (`envelope.py:151-156`) |
| `test_the_sidecar_states_that_the_series_is_reduced` | `close_bar_reduction.applied` is true and `rule` is the single-price rule, so a reader needs no bar arithmetic to tell a reduced series from a raw one | omitting the block, which makes a reduced file indistinguishable from a vendor file — the SMMT loss in a new costume |
| `test_every_reduced_bar_is_listed_with_the_quadruple_it_replaced` | `close_bar_reduction.reduced` has one entry per reduced bar, each with `t`, `session`, `from.{open,high,low,close}` and `to`, and the raw series can be rebuilt from it plus the written bars | writing only `bars_reduced`, which states the construction without making it invertible — the objection §14 has to answer |
| `test_the_narrowed_count_is_the_discriminating_one` | on a series where exactly one close bar's raw range left its session's range, `range_narrowed` is 1 while `counts.close_stamped` equals the session count | publishing only `close_stamped`, which is near-constant (**M1**: one per session in 91.34% of pairs) and so carries no signal |
| `test_the_granularity_is_measured_not_asserted` | the sidecar's `modal_spacing_ms` is computed from the written series; on an hourly supplied file the run refuses rather than writing `"1m"` | a literal `"1m"`, which would assert one-minute bars about an hourly file |
| `test_the_sidecar_states_the_required_end_beside_the_window_end` | `window.required_end_t` equals `config["entry_deadline"]["value"]` and `window.to_t >= required_end_t` | writing the window without the quantity it has to reach |
| `test_a_shortened_margin_is_stated_not_inferred` | for `BE:2026-09-29`, `margin_sessions_requested` 2 and `margin_sessions_served` 0, with `last_servable_session` set | one `clamped: true` flag, which a reader cannot join to `entry_deadline` |
| `test_the_sha256_is_the_sha256_of_the_bars_file_bytes` | equals `hashlib.sha256(bars_path.read_bytes()).hexdigest()` | hashing the in-memory list, which no reader can reproduce with `shasum` |
| `test_a_cached_run_does_not_claim_a_fresh_fetch` | second run: `cache.hit` true and `fetched_at` identical to the first run's | stamping `now()` as `fetched_at` on every run |

### Class G — `TheCorporateActionGuardIsAskedOnEveryRunTest` (fake `BarFetch` plus fake lookup)

| method | asserts | production change that makes it fail |
|---|---|---|
| `test_a_split_inside_the_window_refuses_the_run` | status 1, `bars_corporate_action`, the lookup's `detail` in the message | gating the lookup behind a return threshold, so a normal window never asks |
| `test_the_lookup_window_is_padded_with_the_module_s_own_constants` | the fake receives `window_start - 3 days` and `window_end + 1 day` | literal `0`/`0`, or re-spelling 3 and 1 instead of importing `ACTION_WINDOW_PRE_CALENDAR_DAYS` / `ACTION_WINDOW_POST_CALENDAR_DAYS` |
| `test_a_lookup_that_cannot_answer_refuses_rather_than_writing_the_bars` | the fake raises `CorporateActionsLookupError`: status 7, `corporate_actions_unanswered`, retryable true, **no bars file** | `except: verdict = "none"` or `verdict = "lookup_failed"` plus a write, both of which publish a run whose basis was never checked |
| `test_only_a_clean_verdict_is_ever_written` | across the three branches, the only sidecar written carries `verdict: "none"` | a sidecar vocabulary with three values, two of which mean "do not trust this run" |
| `test_the_lookup_is_asked_before_any_bar_is_fetched` | on a refusing lookup, the fake `BarFetch` recorded zero calls | ordering the lookup after the fetch, which spends metered quota on a run that refuses |

### Class H — `TheHelpTextIsThePublishedSurfaceTest` (no fake, no network)

| method | asserts | production change that makes it fail |
|---|---|---|
| `test_every_flag_the_parser_takes_appears_in_the_help_text` | every option string on the parser appears in `_USAGE` | adding `--bar-cache` to the parser only. Nothing pins `_USAGE` today — `grep -rn "_USAGE" apps/alphalens-research/` returns 10 lines, of which seven are `EXIT_USAGE` elsewhere (`tests/intent_replay/test_cli.py:47`, `:450`, `:532`, `:573`, `:615`; `scripts/probe_saxo_live_entitlement.py:39`, `:149`) and three are in this script: `:408` (a comment), `:411` (the definition) and `:478` (`description=_USAGE`). No test in that file invokes `--help` (zero matches); and because the parser uses `usage=argparse.SUPPRESS` with `RawDescriptionHelpFormatter` (`:477`, `:479`), argparse generates no option list, so an unlisted flag is invisible |
| `test_the_exit_codes_block_lists_every_status_the_command_returns` | `7` appears in the `EXIT CODES` block | adding exit 7 to the code and not to `:429-430` |

### Class I — `TheFetchSeamIsWiredToProductionTest` (an AST walk, no fake, no network)

Every class above injects a fake, and the network guard makes the production default untestable behaviourally. A `main()` whose `bar_fetch` default was left `None`, or pointed at the wrong callable, would pass all of them. This repository's own remedy for that shape is a source gate over the module — `tests/brokers/test_broker_cli_places_nothing.py`, whose own docstring states the reason: "That flag was declared by the caller, so a future command could opt out by passing ``False``. An AST walk over every CLI module cannot be opted out of."

| method | asserts | production change that makes it fail |
|---|---|---|
| `test_the_bar_fetch_default_is_the_canonical_polygon_seam` | the AST of `main`'s signature gives `bar_fetch` a default that is the `Name` `default_bar_fetch`, and the module imports that name from `alphalens_pipeline.feedback.bar_window` | a `None` default, or a local copy of the fetcher, or a fifth private `_default_bar_fetch` |
| `test_the_actions_lookup_default_builds_the_cached_polygon_lookup` | the default expression is a `Call` to `CachedCorporateActionsLookup` whose first argument is a `Call` to `PolygonCorporateActionsLookup` | a `None` default, which would make the guard silently absent in production while every test passes |
| `test_no_injected_seam_defaults_to_none` | none of `bar_fetch`, `actions_lookup`, `now` has a `Constant(None)` default | the shape this class exists to catch |

---

## 13. Surfaces to change

### `apps/alphalens-research/scripts/replay_from_pick.py`

| line | today | change |
|---|---|---|
| `:415-416` | "The report decides comparability, so the command needs no broker of its own and runs offline." | make it conditional and precise: the command fetches its bars unless `--bars` is given, and asks the price vendor about corporate actions on every run unless that answer is already cached |
| `:419` | `--out DIR  where the two files are written (default: .)` | four files |
| `:422` | `--bars PATH  bars to put in the suggested command` | an offline override, validated against the venue's session grid and reduced by the same close-bar rule |
| `:418-423` | OPTIONS block | add `--bar-cache DIR` |
| `:425-427` | OUTPUT block | add `bars_path`, `bars_provenance_path` and the `bars` block |
| `:429-430` | `0 ok, 1 ..., 2 usage, 4 ...` | add `7 a transient vendor failure or a window the vendor cannot serve yet; re-run` |
| `:432-434` | EXAMPLES | the example no longer needs a hand-built file |
| `:481-487` | parser | add `--bar-cache` |
| `:473` | `def main(argv)` | add keyword-only `bar_fetch`, `actions_lookup`, `now`, each with a production default (§12 class I) |
| `:491-496`, `:499-501`, `:504-508`, `:518-520` | four refusal sites | route through one `_fail(code, reason, message, status, **details)` helper that renders prose or the JSON envelope |
| `:531-556` | the `answer` dict | add `bars_path`, `bars_provenance_path` and `bars` as **top-level** keys |
| `:545-555` | `replay_argv` | always the written `<stem>.bars.json`, on both paths; the `<BARS.json>` placeholder goes |
| `:50` | `SCHEMA = "alphalens.research.replay_from_pick/v1"` | unchanged: added keys are optional additions inside a major version |
| `:32` imports | `advance_trading_sessions`, `session_open_utc` | add `session_close_utc`, `session_on_or_after`, `previous_trading_day`, `is_trading_day` |

The `bars` block must not go inside `configuration` (`:544`): that object is the engine's eight-key run configuration, and `RunConfig.from_jsonable` refuses an unknown key — the existing test at `:558` round-trips it.

### `apps/alphalens-research/tests/test_replay_from_pick.py`

**This file must be edited in the same commit.**

| line | what breaks | change |
|---|---|---|
| `:534-538` | `_run` is `main(list(args))` with no keyword seams, so §12's tests cannot reach `bar_fetch` / `actions_lookup` / `now` | widen it to forward them, with the fakes defaulted per test |
| `:553` (`test_it_writes_the_two_files_the_replay_takes`) | asserts status 0 with no `--bars`; under D1 it takes the fetch path, reaches `default_bar_fetch` → `get_default_polygon_client()` and dies on the network guard | inject a fake `BarFetch` and a `now`, and pass `--bar-cache` |
| `:580` (`test_the_next_command_it_prints_is_runnable`) | same | same, and assert the real bars path in `replay_argv` instead of `<BARS.json>` |
| `:699` (`test_an_unknown_distance_is_refused_rather_than_run_with_the_trail_off`, second half) | same | same |
| `:666` | `assertNotIn("--entry-trail-bps", err)` on the not-comparable refusal | keep the new `_fail` message free of that token |

Measured at HEAD: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.test_replay_from_pick` → `Ran 32 tests in 0.261s / OK`. Those three assertions are the ones that turn red on the first commit of this design.

### `apps/alphalens-research/tests/test_replay_from_pick_seams.py` — new

The AST gate of §12 class I. A separate file because it walks the source rather than running the command, which is the shape `tests/brokers/test_broker_cli_places_nothing.py` already established.

### `apps/alphalens-pipeline/alphalens_pipeline/feedback/bar_window.py`

`:47` and `:61` — rename `_default_bar_fetch` to `default_bar_fetch`. Zero external importers, verified by grep (§4).

### `CLAUDE.md`

* `:182-188` — the recipe gains nothing new in the common case (the command already runs as printed), but the surrounding prose must say that the bars come with it.
* `:191` — "The command reads the report, not the broker, so it runs offline." Becomes: it reads the report and the price vendor; it runs without a bar fetch only with `--bars`, and without any vendor call only when the corporate-action answer for the window is already cached.
* `:192` — the "derived / imported / read — never written as a literal" rule gains the bar window: the grid and the ticker from `instrument.{mic, ticker}`, the window's required end from `config.entry_deadline`, its anchor from the record's own `exits[-1].venue_time`, the padding constants imported from `corporate_actions`, and the series' granularity MEASURED from the series rather than stated.
* a sixth rule: **the session grid is INCLUSIVE at both auctions and read from the calendar, never `open + 390 minutes`** — with the two discriminators named, XWAR (90 minutes short) and XSHG (60 minutes long) for the span, and `_expire` on the deadline millisecond for the bound.
* a seventh rule: **the bar stamped at the venue close is kept and written as ONE price, its own open.** That minute runs past the session's end, so its high and low can carry post-market trading the regular session never saw; the whole-bar treatment flipped a real outcome by 0.828 R and left 25 of 871 real windows exposed to a >2% excursion, against none under the reduction. The reduction is recorded per bar in the sidecar, with the quadruple it replaced, so it can be undone.
* an eighth rule: **the window's right edge is a refusal, not a clamp.** A window that cannot reach `entry_deadline` refuses and names the session to wait for; a `no_fill` with `walked.to_t < entry_deadline` is the published signature of a truncated run.
* `:364-371` — the runtime-data list gains `~/.alphalens/replay_bars/` (bars, grouped, corporate-actions cache; raw payloads only, immutable per key, successes only).

### `apps/intent-replay/README.md`

**No change.** It specifies the bar file's shape (`:69-86`) and the refusal table (`:499-510`) and says nothing about where bars come from; `grep -i "polygon\|vendor\|replay_from_pick"` returns nothing relevant. Adding provenance there would make a client-agnostic leaf describe one client. Its `walked` section (`:298-309`) already documents the reader-side signal §6 relies on, and `bars.py:133-134` already blesses the one-price bar §5.2 writes, so the reduction needs nothing from the leaf either.

### Repo gates that notice

| gate | file:line | notices? | what satisfies it |
|---|---|---|---|
| no raw Polygon HTTP | `tests/test_no_raw_polygon_http.py:35-40` (`SCAN_DIRS` includes `apps/alphalens-research/scripts` at `:39`) | **yes** | compose `bar_window`; name no URL and call no HTTP library |
| network guard | `tests/_net_guard.py`, installed at `tests/__init__.py:8-10` | **yes** | the fetch is an injected `BarFetch`; every test injects it; the production default is covered by the AST gate instead |
| operator-state guard (#1696) | `tests/brokers/automanager/home_isolation.py:112-115` | partly | resolve the cache root through `state_paths._alphalens_home()`, and pass `--bar-cache` in every test |
| no Polish characters | `tests/test_no_polish_chars.py:25-31` (includes `apps/alphalens-research/tests`, not `scripts`) | **yes, for the new tests** | English only |
| module dependencies | `tests/test_module_dependencies.py:37-43` | no — `scripts/` is not in `PACKAGE_DIRS` | nothing; and it is why the join belongs in the script, which may import the pipeline |
| production file-size ratchet | `tests/test_production_file_size.py:63-71` | no — `scripts/` is not in `PRODUCTION_ROOTS` | nothing |
| no environment reads | `tests/intent_replay/test_no_environment_reads.py` (engine only) | no | nothing, because no engine file changes |

---

## 14. What this design deliberately does not do

* **No calendar in the engine.** Spec §4.1 (`docs/superpowers/specs/2026-09-23-intent-replay-design.md:411-419`): a calendar means `exchange_calendars` plus pandas. The spec's own phrasing is "`dependencies = []` rules it out" (`:419`), and the tree today reads `dependencies = ["alphalens-broker-contract", "jsonschema>=4.0.0"]` (`apps/intent-replay/pyproject.toml:20`) — two pure-Python dependencies, neither of which is pandas or `exchange_calendars`. The constraint that holds is the real one: **the leaf takes on no heavyweight data dependency**, so every calendar-aware step stays in the research-tier script, the same split the spec already uses for `walk_start` and the entry deadline (`:421-423`).
* **No change to `apps/intent-replay` at all, and the close-bar rule is why that is worth saying.** The model error §5.2 names — a resting broker stop filled by a post-market print — could also have been fixed inside the walk, by letting the resting legs see only the bar's open while the polled ladder saw its full range. That is the distinction `walk.py` already draws in prose (`:497-498` against `:707-710`), so the idea is not foreign to the engine. It is rejected because it makes one bar mean two things inside one walk, which the published `Bar` has no shape for, and because it puts a venue-session concept into a leaf whose first spec sentence is that it knows no calendar. The reduction achieves the same correction in the client, where the calendar already lives.
* **No change to the published run configuration.** The eight keys stay the eight keys; `RunConfig` refuses an unknown one, and bars provenance is not a run value the engine reads. It lives at the top level of the command's answer and in the sidecar.
* **No bump of `SCHEMA`.** Added keys are optional additions inside a major version, which is what the project's CLI contract rule permits.
* **No shared refactor of the `get_agg_range` consumers or the four `_default_bar_fetch` copies.** They convert to four different shapes for four different purposes (a 4-tuple, a daily fold, a chart candle, highs and lows only). A shared converter would have one caller that wants the replay shape. Extract on the second use; today's change is the first. The one exception is the rename in §4, which has zero importers.
* **No fix of the `_rth_window_*` 390-minute span.** The close bound is no longer the complaint — this design agrees with it. The span is a real latent defect for any non-US venue, in both directions (**M2**: XWAR 90 and XETR 120 minutes short, XSHG 60 and XTAI 120 long), and correcting it changes already-published `/edge` ladder outcomes, which needs a backfill decision (§16 Q1). So it is an issue, not a silent edit in this PR.
* **No break-venue arm in the predicate.** `session_open_utc` / `session_close_utc` return one interval per session, so on a break venue the grid admits the lunch break: 60 non-trading minutes per XTKS session (02:30-03:30Z) and per XHKG session (04:00-05:00Z), measured for 2026-10-02 (**M2**). The exposure is latent rather than live: `get_agg_range` runs against `/v2/aggs/ticker/{ticker}` on a US-equities plan, and no break venue appears in the fixture (**M3**). A break-aware bound would need a calendar helper that does not exist and has no second consumer. **GAP:** whether Polygon would serve an XTKS ticker at all on this plan is not measured, so the practical reach of this residual is unknown.
* **No `adjusted=true` option.** The document's limits, stops and take-profits are prices the author wrote from live unadjusted quotes; adjusted bars would replay the ladder against a basis the trade never saw.
* **No daily-bar fallback, and not for the reason an earlier draft gave.** A single low per session cannot reproduce a trailing stop (D2). The grid does **not** refuse daily bars — **M8** measures 492 of 497 real daily session files passing it — so the refusal is the explicit `bars_wrong_granularity` gate of §5.3, now phrased on the measured modal spacing so that hourly, 30-minute and 5-minute series are refused too (**M13**).
* **No re-implementation of the engine's bar validation.** Ordering, price coherence and the closed key set are refused by `bars.py` with published codes. The script checks only what the engine cannot know: the session grid, the granularity, the close-bar reduction, and whether the kept series covers `walk_start` at one end and `entry_deadline` at the other.
* **No automatic fetch for a pick the report excludes.** `_assert_comparable` (`:322-333`) already runs first and keeps running first. Spending metered quota on a record the report itself says cannot be compared is wasted money, and the refusal is more useful than the bars.
* **No new exit status per refusal code.** Statuses stay coarse (0, 1, 2, 4, 7), with the detail in `code`, the same rule the broker CLI follows.
* **No alerting, no Telegram, no metrics.** This is a hand-run laboratory command.

### The one thing this design DOES do that it would rather not: it writes a price the vendor did not send

An earlier draft rejected clipping the close bar on exactly this ground, and the objection was real, not a strawman. This command exists because a hand-built bar file carried an unrecorded construction and produced a number that cannot be audited. A design whose own answer is "apply a different unrecorded construction" would be the incident wearing a new hat. So the objection has to be answered, not dropped.

Two things pay for it, and both are load-bearing.

**1. The construction is stated, enumerated and invertible.** The sidecar carries `close_bar_reduction` with the rule, the count, the number of reduced bars whose raw range left their session, and — for every reduced bar — its `t`, its session, the full `{open, high, low, close}` it replaced and the single price that replaced it (§9). A reader holding the sidecar and the bars file can reconstruct the raw series exactly. That is strictly more than the raw vendor file would have given them, because the raw file does not say which bar is a close bar or which calendar decided. And it is the difference the SMMT incident turns on: that file's own session filter and close-bar treatment cannot be reconstructed **at all**, which is why its retracted 1.05% cannot be audited even today, two days after it was known to be wrong. The cache keeps the raw payload too (§10), so a later change to the rule re-derives from the vendor's bytes rather than from a rewritten series.

**2. Nothing belonging to the session is discarded.** This is the part that distinguishes the reduction from clipping in general, and it is not an argument about magnitudes. The session ends AT the close instant. The minute stamped there therefore holds exactly one thing the session owns — the closing print, at its first instant — and after that, up to 59 seconds of trading in a market the session no longer includes. Keeping the bar's open keeps the session's last price; dropping its high, low and close drops only what the session is not. A general clip of a bar's range to its own open would destroy real intraday information; this one cannot, because it applies to exactly the one bar per session whose range is the only range in the series that straddles the session boundary. §12 class C pins that with two tests: only a bar at `t == close_ms` is reduced, and the last bar of the series is not special.

And the alternative's price is measured, not asserted. Keeping the whole bar leaves 182 of 15 677 sessions carrying a high or a low the regular session never traded (**M10**), exposes 149 of 871 real windows and 25 of them to an excursion above 2% against **none** under the reduction, feeds a resting broker stop with post-market prints in flat contradiction of `walk.py:497-498`, and — on real CRSR bars through the real engine — turns an `open` position worth +0.0859 R into a `closed_tp` worth +0.9141 R, a swing of 0.828 R, while inflating `mfe` from 0.5156 to 1.5781 (**M11**). A stated construction that can be undone is a smaller cost than a published number that cannot be explained.

---

## 15. What an earlier draft got wrong

Three premises, each plausible, each refuted by running it. They are recorded because the next person to design this will reach for all three.

### 15.1 The half-open close, `open <= t < close`

The first draft argued from the vendor's START stamping that a bar stamped at the close is "the first post-market minute", and from `walk.py::_expire` that "a bar stamped exactly at the deadline is already outside the ladder's life". The stamping fact is correct and the inference is not: the minute that begins at the close *begins with the closing print*. Three measurements, each independent:

| | |
|---|---|
| the dropped bar's open recovers the official daily close (**M4**, n = 15 578) | **93.48%** exactly, median deviation 0.0000% — against 23.92% for the pre-close bar's close that half-open leaves as last |
| half-open contradicts this repo's own RTH idiom in all three places that implement it (**M12**) | `ladder_chart.py:183`, `:154`; `population_ladder_monitor.py:728`, `:756`, `:775` — and on 40 real files the inclusive grid's kept set is identical to `_filter_bars_to_rth`'s, 0 mismatches, so the new guard would have refused files built with the repo's own helper |
| half-open **defeats** `_expire` rather than agreeing with it (**M6**, the real engine) | a window ending at the deadline session never fires `entry_expired` at all; with a margin it fires **65.5 hours** late, at the next session's open (2026-10-09T20:00Z → 2026-10-12T13:30Z; 2026-10-09 is a Friday). Inclusive fires on the deadline millisecond, and so does the reduced form |

The `_expire` argument also compared two different decisions: whether an order may still fill at an instant, and whether an instant belongs to a session. The deadline instant is a session close (`pick_window.py:69`), and when the two meet, inclusive is what makes the walk say the right thing.

That draft also cited `scripts/diagnose_exit_geometry.py:63` and `scripts/backfill_breakeven_whatif.py:61` as a half-open precedent that "reproduces the stored `realized_r` exactly, 42 of 42 rows". **That check had no power to refute.** The close-stamped bar changes a session's range in 1.161% of sessions (**M10**), so over 42 rows of roughly 5 sessions each the expected number of discriminating sessions is about 2.4, and a discriminating session only moves `realized_r` if the extra range crosses a level. Those two scripts also hard-code EDT. A published figure of "17.5 hours late" in that draft was wrong in the same family of way: 17.5 hours is the close-to-next-open gap when the next session is the next calendar day, and the case its own table measured spans a weekend.

### 15.2 The `min()` clamp on the window end

The same draft clamped `window_end` to the close of the last closed session and defended it with "a window that still runs out is visible: the engine publishes the terminal state `open`". That holds only for runs that **filled**, and the never-filled branch is the majority: 8 of the 12 comparable fixture records (**M3**). **M6**, the real engine on `SYM:2026-09-30`: a window one session short publishes `outcome: no_fill` with `trace: []` — and `walk.py:131-134` returns `no_fill` whenever nothing filled, so a truncated run and a genuinely expired ladder say the same word. Six of the 12 comparable records ran such a window under the draft's own rule, three short by a whole session and three short by one minute, the one-minute cases being the half-open close again (**M7**). The inclusive close repairs three; the refusal of §6 covers the other three.

The clamp also failed its own purpose after the close: at `now = 2026-10-09T21:00Z` it moved the cap to `2026-10-09`, the current calendar day, while the vendor restriction it existed for is calendar-day based (`polygon_client.py:236-239` — the draft cited `:243-247`). **M5** shows the calendar-day rule holding at `2026-10-08` across 08:14Z, 12:00Z and 21:00Z.

And the facility that already existed for detecting the damage was never cited: `walked_from_t` / `walked_to_t` / `walked_bars`, added by `bd724cb2` (#1746) at `walk.py:120-122` and published as the envelope's fifth key (`envelope.py:162-164`, `README.md:223`).

### 15.3 Keeping the whole close-stamped bar

The second draft corrected 15.1 and 15.2 and then kept the close-stamped bar's full OHLC quadruple, declaring the cost (1.161% of sessions carry a post-close extreme) and explicitly rejecting any reduction because "it would write a bar the vendor never sent, which no site in this tree does". Two things refuted it.

**It flips a published outcome, on the real engine, on real bars.** **M11**: CRSR 2026-08-03 → 2026-08-06, take-profit at 11.95 between the session's own high of 11.1717 and the close bar's high of 12.80. Whole bar: `closed_tp`, `tp_fired` at 11.95, **+0.9141 R**, `mfe` 1.5781. Reduced: `open`, `horizon_open` at the auction print 10.89, **+0.0859 R**, `mfe` 0.5156. The reduction removes 0.828 R of a 1.047 R swing, and the remaining 0.219 R is the legitimate effect of marking at the close instant.

**The cost was not one cost but two, and the draft bounded both with one number.** The engine fills a resting stop at `min(bar.open, stop)` because "The stop RESTS at the broker" (`walk.py:497-498`, `:512`), while a take-profit "does not rest at the broker in this model ... it fills AT its level" with a published `take_profit_observation_time` divergence (`:707-710`; `envelope.py:93`, `:117`). Over the 139 contaminations the reduction removes, the low-extended cases — the ones that fill a resting broker order from a post-market print, which is flatly wrong — number 37 and are bounded at 0.9901% of price, while the 103 high-extended cases carry every large magnitude up to 14.5752% (**M10**). So the draft's single bound was defending the wrong thing on one side and conceding the wrong thing on the other.

The draft's own counter-argument survives and is answered rather than dropped: §14's last section states what the reduction costs and what pays for it. The draft's per-session framing also understated the exposure — 1.161% of sessions is 17.11% of real windows, because a window is a median of 13 sessions (**M10**) and its terminal bar is a close-stamped bar by construction.

### 15.4 Smaller things the drafts stated as facts and got wrong

All corrected above, each re-measured:

* `apps/intent-replay` declares two dependencies, not `[]` (`pyproject.toml:20`).
* the fetch ticker is `document["instrument"]["ticker"]`; neither the document nor the trades record carries an `instrument.symbol` (**M3**).
* the first draft's worked window published 3 900 bars from an unclamped window while its own formula clamped, and its example epochs decoded to 2025 and to 1970.
* the closing-price measurement was published as "n = 766 pairs over 66 overlapping sessions" beside a sentence describing every (ticker, session) pair. The wide construction the sentence describes gives 15 677 close-stamped pairs over 67 overlapping sessions, 15 578 of them with the ticker in the daily store (**M4**).
* "a 390-bar completeness check would refuse 13 700 of 17 163 pairs, 79.8%" measures as 13 134 (76.5%) at 390 or 13 833 (80.6%) at 391, and the maximum bars per pair is **391**, not 390 — the grid the memo adopts holds 391 minutes per full session, as its own worked example states (**M1**).
* `_RTH_FULL_SESSION_SPAN_MIN = 390` is at `population_ladder_monitor.py:187`; `:718` returns it on the non-half-day branch and `:736` multiplies it. `_trail_distance` is `:451-470`, not `:435-470` (`:435` is inside `_USAGE`). `grep -rn "_USAGE" apps/alphalens-research/` returns 10 lines, not 3.
* the 390-minute span defect was described as short-only; it runs long on four of the 29 venues that resolve (**M2**).
* the granularity gate was phrased as "at most one bar per session", which is silent on hourly, 30-minute and 5-minute series, and the sidecar asserted `granularity: "1m"` as a literal — so it would have claimed one-minute bars about an hourly file (**M13**).
* the second draft offered `counts.close_stamped` as the reader's exposure signal. It is near-constant — one per session in 91.34% of real pairs (**M1**) — so the discriminating field is `close_bar_reduction.range_narrowed`, plus the per-bar list and the join from the trace's exit `t` (§9).

---

## 16. Open questions

Three. All real, none blocking implementation.

**Q1 — the `_rth_window_*` 390-minute span.** `ladder_chart._rth_window_ms` (`:193-200`) and `population_ladder_monitor._rth_window_utc` (`:727-737`, via `_RTH_FULL_SESSION_SPAN_MIN` at `:187`) compute a session's close as `open + span_min × 60_000` with `span_min = 390` on every non-half-day. **M2** measures that as short on 17 of the 29 venues that resolve (11 by 120 minutes, 4 by 90 including XWAR, XOSL by 50, XNZE by 15) and LONG on four: 60 minutes on XSHG, 120 on XTAI, 30 on XASX and 15 on XBOM — and silently correct on XNAS, XNYS and, by coincidence of its lunch break, XTKS. `_session_rth_span_min`'s own docstring claims the opposite, that it is "Computed from the real open/close so the idiom transfers to any venue without a hard-coded table" (`:712-713`), which is false on the full-session branch. `_filter_bars_to_rth` consumes it, so any non-US venue in the population would have had its minute path truncated on one side or widened past the close on the other, and `/edge` ladder outcomes computed from it are already published. **GAP:** whether the monitored population has ever contained a non-US venue is not measured in this pass, so the defect may be entirely latent. Does the owner want the span fixed plus a recompute of the affected rows, the fix with the old rows left as they are, or neither for now? This design does not depend on the answer; it reads the calendar directly instead.

**Q2 — where the laboratory's rigour sits on a cold corporate-action cache.** §8 refuses a run whose corporate-action question cannot be answered, including on the `--bars` path, with exit 7 and "re-run when the vendor is reachable". The lookup is cached FOUND-forever and NONE-FOUND for 14 days (`corporate_actions.py:95`, `:258`), so the cost is one online run per (ticker, window) and every later run of that pick is fully offline. But on a **cold** cache a genuinely air-gapped operator cannot run at all — which is most of what `--bars` exists for under D1. Two coherent answers: refuse, as designed, so no written run ever has an unchecked basis; or let `--bars` proceed with a cold lookup cache and a sidecar stating `verdict: "not_asked"`, accepting that such a run can cross a split and not say so. The first is designed here because the incident this memo exists for was exactly a published number whose basis could not be reconstructed, but the second is a defensible reading of what an offline override is for, and the choice is the owner's.

**Q3 — whether the pre-fetch window refusal should be advisory.** §6 refuses before fetching when `servable_end < required_end`, on a premise the same section marks **GAP** (the vendor's day-boundary timezone), while also shipping the post-fetch `vendor_served_short` check and arguing that "predicting the vendor's day boundary is a claim; checking the series is a measurement". By that argument the pre-fetch gate *is* the claim. It blocks 3 of 12 fixture records from being replayed on the evening their ladder expired, with no override flag, to save one metered Polygon call (**M7**: the verdicts are identical at `now = 08:14Z`, `12:00Z` and `21:00Z`). Two coherent answers: keep the hard refusal, so no run is ever launched that cannot answer the question, and accept that the ban rests on an unmeasured premise; or demote it to a warning and let the post-fetch measurement decide, accepting one wasted vendor call per such run. The first is designed here because the refusal's message names the session to wait for and the wait is short, but the asymmetry with §6's own stated reasoning is real and the owner has not ruled on it.
