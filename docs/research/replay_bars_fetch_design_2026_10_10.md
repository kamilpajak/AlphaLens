# Design: `replay_from_pick` fetches its own bars, on the venue's half-open session grid

Status: **LOCKED**, 2026-10-10. Code facts come from the worktree HEAD `29427293`, which differs from `3a182918` only by this memo's own file; `bd724cb2` (#1746) is the commit that matters in §6. Every number below carries the command or the file that produced it. The measurements marked **M1-M15** were run read-only against `~/.alphalens/population_ladders/bars/` (881 parquet files, 6 364 368 rows, 17 163 (file, session) pairs over 67 sessions), `~/.alphalens/grouped_daily_history/` (497 parquet files), the committed LIVE trades fixture `apps/alphalens-research/tests/brokers/automanager/fixtures/trades/live_2026_10_03/`, and — for M6, M11 and M15 — the real `intent-replay` engine. Where a fact is missing rather than known, the word is **GAP**, never a guess. Nothing in the repository or in `~/.alphalens/` was modified.

The measurement scripts live in this session's scratchpad, not in the repository. Each one below is named so that a re-run has a starting point, and every measurement states the predicate it applied, which is what a re-run actually needs:

| | what it measures | script |
|---|---|---|
| **M1** | the bulk shape of the kept series under each grid | `b7_bulk.py`, `b9_ratio.py` |
| **M2** | per-session grids: spans, half-days, DST, breaks, 29 venues | `r8_span.py`, `b8_grid.py` |
| **M3** | the fixture records: instrument blocks, exits, deadlines | `m7.py` |
| **M4** | the terminal mark against the official daily close | `b2_mark.py` |
| **M5** | the servable cap across a session close | `b4_window.py` |
| **M6** | when the entry deadline fires, on the real engine | `b3_expire.py` |
| **M7** | the window verdict for all 12 comparable records | `b4_window.py` |
| **M8** | the repo's own daily store against the grid | `b6_daily.py` |
| **M9** | pairs carrying no bar at the session open | `b10_openbar.py` |
| **M10** | the close-stamped bar against the rest of its session | `b12_dir.py`, `a3.py`, `a12.py` |
| **M11** | the CRSR 2026-08-06 session, end to end | `intent-replay run` + direct store reads |
| **M12** | the repo's RTH helper against both grids, 40 real files | `b5_rth.py` |
| **M13** | granularity and modal `t`-spacing | `b11_gran.py`, `r10_modal.py` |
| **M14** | the three treatments ranked against an INDEPENDENT oracle | `b1_oracle.py`, `a9.py` |
| **M15** | what rewriting a bar to one price does to reachable range | the real `walk()` |

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

The script fetches the minute bars itself, keeps the bars that fall on the venue's **half-open** session grid — `open_ms <= t < close_ms`, so the bar stamped at the close is not part of the series — writes the resulting series in the published bar shape next to the document and the configuration, and writes a provenance sidecar beside them. A gate on the existing `--bars` flag was rejected as the remedy, because the script does not open that file today and the incident invocation did not pass it — but `--bars` survives as an offline override, and when it is given the file is filtered and validated against the same grid rather than trusted.

### Command surface

```
USAGE
  python -m scripts.replay_from_pick TRADES_JSON PICK_KEY [options]

OPTIONS
  --out DIR              where the four files are written (default: .)
  --entry-trails PATH    the entry-trail journal (default: the report env's own)
  --entry-trail-bps N    state the distance instead of reading it; 0 means OFF
  --bars PATH            use this bar file instead of fetching; it is filtered
                         onto the venue's half-open session grid, and refused
                         if it carries a bar the grid cannot place at all
  --bar-cache DIR        the bar cache root (default: ~/.alphalens/replay_bars)
  --format human|json    default human
```

Required: `TRADES_JSON` and `PICK_KEY` only, exactly as today. No flag is required; fetching is what happens when `--bars` is absent.

Written into `--out`, with `stem = PICK_KEY.replace(":", "_")` (`replay_from_pick.py:524`):

| file | new? | written when |
|---|---|---|
| `<stem>.document.json` | no | always |
| `<stem>.config.json` | no | always |
| `<stem>.bars.json` | yes | **always** — it is the series the walk reads, which on both paths is the filtered one |
| `<stem>.bars.provenance.json` | yes | always |

`<stem>.bars.json` is written on the `--bars` path too, and that is forced by the filter: a supplied file built with any of this repository's three RTH helpers carries one close-stamped bar per session (**M12**: 40 of 40 real files), so the operator's file is not the series that is walked. `replay_argv` must therefore point at the series that is. The supplied file is never modified; its own sha256 is recorded separately (§9).

Three keyword-only seams are added to `main()` with production defaults, so tests need no network: `bar_fetch`, `actions_lookup`, `now`. The module entry point (`replay_from_pick.py:567-568`) passes none of them, and §12 gives that wiring its own source gate.

---

## 3. The decisions, as given

These are the owner's premises, recorded so a later reader does not reopen them.

**D1 — fetching is the default; `--bars` is an offline override and is validated.** A gate on `--bars` alone fixes nothing, because the script never opens that file (`:554` is its only use) and the incident invocation did not pass it. Deleting the hand-built file from the normal workflow is the remedy; policing it is not. Consequence: the help text's promise at `replay_from_pick.py:415-416` ("The report decides comparability, so the command needs no broker of its own and runs offline") becomes conditional, and §8 narrows it further than D1 alone implies.

**D2 — minute bars.** The live daemon polls roughly every 45 seconds (`alphalens broker manage --poll-seconds 45`, CLAUDE.md), and a daily bar cannot reproduce a trailing stop: one low per session carries no path. Window: from `config.walk_start` to the pick's real exit time plus a margin; for a pick that never filled, past `config.entry_deadline`. The exit time is in the trades report the script already parses but does not read today. §6 states the exact rule.

**D3 — a disk cache, keyed by (ticker, window, granularity, adjusted).** Polygon is metered at 5 requests per minute on the free tier (`polygon_client.py:104`, `rate_limit_per_min: int = 5`), and re-running a replay is normal work. The cache may live under `~/.alphalens/`: the script already reads the entry-trail journal from there through `state_paths.entry_trails_path` (`state_paths.py:153`), so this does not break its layer.

**D4 — the session grid is HALF-OPEN: `open_ms <= t < close_ms`. The bar stamped at the venue close is DROPPED.** The minute stamped at the close spans `close .. close + 59s`, and the session ends AT the close, so that minute lies outside the session except for its first instant. Two earlier answers to the same question were measured and refuted — keeping the whole bar, and keeping it rewritten to a single price equal to its own open — and §15 records both with the measurement that killed each. §5 gives the three independent measurements that decided the half-open rule, and §5.4 states, quantified, the one thing it costs.

**Provenance sidecar.** The bars are written with a sidecar naming the vendor, `adjusted`, the session filter, the window, the as-of, the close-bar drop including every dropped bar's original quadruple, and a sha256 of the series. Motivated by a real loss: the SMMT file's filter and its close-bar treatment cannot be reconstructed, so the published 1.05% cannot be audited even now that it is known to be wrong.

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
* **No session filter, no cache, no granularity check, no right-edge coverage check.**

### Block 2 — the repo's RTH idiom, which this design CONTRADICTS at the close and CORRECTS at the span

Three places in the tree implement "filter bars to RTH", and all three are **inclusive at the close**. All three were re-read at HEAD:

| site | what it says |
|---|---|
| `ladder_chart.py:182-201` `_rth_window_ms` | docstring `:183`: "``(open_ms, close_ms)`` epoch-ms RTH bounds for ``session`` (close inclusive)" |
| `ladder_chart.py:143-156` `_session_date_for_ts` | `:154`: `if open_ms <= ts_ms <= close_ms:` |
| `population_ladder_monitor.py:740-777` `_filter_bars_to_rth` (with `_rth_window_utc` at `:727-737`) | `:728` "(close inclusive)"; `:756` "A bar is kept when its start ``t`` falls in ``[session_open, session_close]``"; `:775`: `if any(lo <= ts <= hi for lo, hi in windows)` |

**So this design disagrees with the repository at the close bound, and that disagreement is the whole of §5.3.** It is stated here rather than buried, because it is the reason the `--bars` path filters instead of refusing (§5.6). **M12**, on 40 randomly sampled real bar files (`random.seed(1714)`, `b5_rth.py`):

| kept set | bars kept over the 40 files | files whose kept set differs from `_filter_bars_to_rth`'s |
|---|---:|---:|
| `_filter_bars_to_rth` (the repo's own) | 291 874 | — |
| the inclusive grid | 291 874 | **0 of 40** |
| the half-open grid | 291 069 | **40 of 40** |

The half-open grid drops 805 bars those files contain, one per close-stamped session, over 805 of the 831 sessions they cover (96.87%). A design that *refused* an unexpected bar at the close would therefore refuse every file the repository itself produces. §5.6 is what that measurement forces.

What this design does **not** reuse is the function, for a second measured reason: both helpers compute the close as `open_ms + span_min * 60_000` with `_RTH_FULL_SESSION_SPAN_MIN = 390` (`population_ladder_monitor.py:187`) returned on the non-half-day branch (`:718`) and multiplied at `:736`; `ladder_chart.py:193-202` does the same with its own literal. **M2** measures the damage on the venues that matter today (`b8_grid.py`):

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

XTKS agreeing at 390 makes this worse, not better: its nominal span really is 390 minutes, of which 60 are a lunch break — measured, `has_break=True` with `session_break_start → session_break_end` of **02:30→03:30Z** on XTKS 2026-10-02, **04:00→05:00Z** on XHKG 2026-10-02 and **03:30→05:00Z** on XSHG 2026-10-12, while XNAS, XNYS, XWAR and XETR report `has_break=False` (**M2**). A US-plus-Tokyo test suite therefore stays green while XWAR loses 90 minutes of every session. That is the same venue-blindness family as the XNYS-for-XNAS error #1714 was built to stop.

**And the defect runs in both directions.** **M2** (`r8_span.py`), over 30 MICs at their next session on or after 2026-10-12, of which 29 resolve (XNSE is not an `exchange_calendars` calendar): 17 are short (XETR / XPAR / XLON / XAMS / XBRU / XLIS / XMIL / XMAD / XSWX / XSTO / XHEL by 120 minutes, XWAR / XCSE / XSES / XJSE by 90, XOSL by 50, XNZE by 15) and **four are LONG** — the helper's close runs PAST the real one:

| venue | real close | `open + 390` | direction |
|---|---|---|---|
| XSHG | 07:00Z | 08:00Z | **+60 minutes past the close** |
| XTAI | 05:30Z | 07:30Z | **+120 minutes** |
| XASX | 05:00Z | 05:30Z | **+30 minutes** |
| XBOM | 10:00Z | 10:15Z | **+15 minutes** |

The long direction matters more than the short one. It breaks `_filter_bars_to_rth`'s own stated contract that the grouped-daily `[low, high]` is "a TRUE superset of the minute path" (`:748-750`), and it is the direction in which a repo-built `--bars` file would carry bars the new grid cannot place on any session at all. CLAUDE.md already names XSHG among planned venues. §16 Q1 puts the whole span defect to the owner with both halves on the table.

So what is reused is the idea — ask the calendar for the bound instead of assuming it — and the script composes `session_open_utc` and `session_close_utc` directly (`market/calendar.py:326`, `:345`). Both read the per-session auction time from `exchange_calendars`, so half-days resolve with no special case, and both raise `ValueError` for a non-session date. Promoting a corrected helper into `feedback/bar_window.py` would also be layer-clean, but there is no second consumer, so it stays in the script (extract on second use).

### Block 3 — the script's own existing derivations

Already present and unchanged:

* the venue: `mic = document["instrument"]["mic"]` (`replay_from_pick.py:140`), used for every calendar call;
* `walk_start = session_open_utc(day1, exchange=mic)` where `day1` comes from `meta.source` (`:150`, `:210-216`);
* `entry_deadline = pick_window(trade_date, ttl_days, mic).window_end` (`:153`, `:199-209`), and `pick_window` computes that as `session_close_utc(last_session, exchange=mic)` (`pick_window.py:69`).

`advance_trading_sessions` and `session_open_utc` are already imported (`:31-34`). Added from the same module: `session_close_utc` (`calendar.py:345`), `session_on_or_after` (`:231`), `previous_trading_day` (`:212`) and `is_trading_day` (`:124`). `session_not_closed` (`:364`) is deliberately **not** used — §6 says why.

**What block 3 does not do: it never reads the exit.** What the script reads off the trades record today is `trade["instrument"]` (`:301`), `trade["sizing_fx"]` (`:313`), `trade["plan"]` (`:351`, handed to `authored_document`), `trade["pick_key"]` (`:332`, `:445`) and `trade.get("replay_exclusions")` (`:329`). `stated_facts` alone reads only `instrument.*` and `sizing_fx.*` (`:292-320`). The exit is in none of them: `grep -n "exits\|venue_time" apps/alphalens-research/scripts/replay_from_pick.py` returns **nothing**.

The exit instant is at `trades[i].exits[j].venue_time.value`, a `Measured` (`trades.py:406-424`) rendered RFC 3339 with milliseconds and `Z` by `format_time` (`trades.py:399`), flattened onto the exit by `_Exit.to_dict` (`trades.py:684-689`). Three facts about it, all measured (**M3**):

* `exits` is produced in ascending `fill.when` order (`trades.py:1980`), so **the trade's real exit is `exits[-1]`**. The fixture contains a record with two exits — `ALB:2026-09-08-g2`, at `2026-09-14T13:59:41.231Z` and `...41.250Z` — so `exits[0]` is already wrong on real data, although that particular record is excluded by `legacy_plan_shape`.
* `venue_time` can be null with `null_reason` `NULL_NOT_JOURNALED = "not_journaled"` (`trades.py:101`, `:431`, `:439`), and `_replay_exclusions` does not exclude such a record: `trades.py:2962-2992` branches on the exit reason and on `attributed_qty` and never on the exit time. So the script refuses a null exit time itself, naming `null_reason`, exactly as `_measured` already does (`replay_from_pick.py:256-266`).
* No record in the fixture actually carries one: 0 of 35 records have a null exit `venue_time`. The refusal is therefore untested by the data and must be tested by a constructed record.

### Block 4 — the corporate-action lookup

`feedback/corporate_actions.py`: `PolygonCorporateActionsLookup` (`:128`, `lookup(ticker, start: date, end: date) -> CorporateActionsAnswer` at `:149`), wrapped in `CachedCorporateActionsLookup(inner, cache_path)` (`:209-217`), with the window padding constants `ACTION_WINDOW_PRE_CALENDAR_DAYS = 3` and `ACTION_WINDOW_POST_CALENDAR_DAYS = 1` (`:101-102`). The lookup needs a `pre_ex_close` function (`:138`); §8 states what the script supplies and what that costs.

---

## 5. The session grid is half-open

### 5.1 The rule, stated once

```
keep a bar iff  open_ms <= t < close_ms  for some session in the window's session list,
where open_ms / close_ms come from session_open_utc / session_close_utc for that session and MIC
```

One rule, deciding membership. There is no second rule that rewrites a bar's content: nothing in this design writes a price the vendor did not send.

The two calendar helpers name what the bounds are. `session_open_utc` docstring (`calendar.py:330`): "UTC datetime of ``exchange``'s opening auction on session date ``d``." `session_close_utc` (`calendar.py:349`): "UTC datetime of ``exchange``'s closing auction on session date ``d``." Polygon stamps a bar at its START (`polygon_client.py:221`: "``t`` (ms epoch, bar START)"), and the engine reads it the same way (`bars.py:89`: "``t`` is epoch milliseconds, UTC, and is taken to be the bar's OPEN time"). So the bar stamped at the close is the minute 16:00:00-16:00:59 ET, and **the session ends at 16:00:00 exactly**. That minute holds the closing-auction print at its first instant, and then up to 59 seconds of trading that belongs to no session. The half-open rule drops the whole minute, closing print included; §5.4 states what that costs and why it is the smaller error.

In bulk the treatment is small and the dropped set is exactly the close-stamped bars. **M1** (`b7_bulk.py`, `b9_ratio.py`), over the 17 163 real (file, session) pairs:

| | bars |
|---|---:|
| raw rows in the 881 files | 6 364 368 |
| kept by the inclusive grid | 5 611 172 |
| **kept by the half-open grid** | **5 595 495** |
| the difference | **15 677**, one per close-stamped pair |

15 677 of the 17 163 pairs (91.34%) carry a bar stamped exactly at their session close. **No pair is emptied by dropping it**: zero pairs have fewer than two kept bars under either grid, and the bars-per-pair floor is 13 under both. The maximum per full session falls from 391 to **390** (3 463 pairs reach it), because `[open, close)` at one-minute spacing is 390 minutes.

### 5.2 Why the lower bound is inclusive

`check_window_covers` refuses a series whose first bar is later than `walk_start` (`bars.py:287-295`, reason `begins_after_walk_start`), and `walk_start` is exactly `session_open_utc(day1)` (`replay_from_pick.py:210-216`). A gate that dropped `t == open` would make every run refuse itself. It is also the normal case: **M9** (`b10_openbar.py`) finds 17 088 of 17 163 real pairs carry a bar stamped exactly at the session open, **99.56%**.

So the grid is asymmetric by construction: inclusive at the opening auction, exclusive at the closing one. That asymmetry is not an aesthetic choice. The opening print is the first instant of a minute that lies *inside* the session; the closing print is the first instant of a minute that lies *outside* it.

### 5.3 Why the upper bound is exclusive — three measurements, three different oracles

Three measurements decided this, each able to produce the observation that would have refuted it, and each independent of the others.

#### 1. Ranked against an oracle outside the minute series, dropping wins

The question "does the close-stamped bar carry prices the regular session never traded?" cannot be answered from the minute series alone, because every candidate answer changes that series (§15.3 explains why the earlier draft's metric could not see this). The independent oracle is the official daily `[low, high]` in `~/.alphalens/grouped_daily_history/`, which is a separate vendor payload and not a re-rendering of the minute bars.

The daily store is written `adjusted=true` (CLAUDE.md) while the minute store is raw, so a (ticker, session) pair whose adjustment factor is not 1 cannot be compared at all. **M14** therefore ranks under five stated adjustment-free filters (`b1_oracle.py`), and the ordering is invariant under all of them:

| adjustment-free filter | n | keep the whole bar | reduce it to its open | **drop it (half-open)** |
|---|---:|---:|---:|---:|
| F1: an exact match on the bar open, the bar close or the pre-close close | 15 160 | 137 (0.904%) | 17 (0.112%) | **8 (0.053%)** |
| F2: F1, or an exact match on the bar high or low | 15 309 | 138 (0.901%) | 17 (0.111%) | **8 (0.052%)** |
| F3: `|bar close / official close − 1| < 1%` | 15 380 | 101 (0.657%) | 21 (0.137%) | **8 (0.052%)** |
| F4: the official close lies inside the whole session's minute range | 15 676 | 163 (1.040%) | 25 (0.159%) | **8 (0.051%)** |
| F5: no filter at all | 15 677 | 164 (1.046%) | 26 (0.166%) | **9 (0.057%)** |

Each cell is the number of sessions whose kept range leaves the official daily `[low, high]`. The worst excursion is identical under F1-F4 and is the decisive spread: **1457.5 bps** keeping the whole bar, **202.0 bps** reducing it to its open, **34.7 bps** dropping it.

The run the owner decided on reported **14 (0.090%, worst 34.7 bps)** for dropping, **30 (0.193%, worst 202.0 bps)** for reducing and **168 (1.079%, worst 1457.5 bps)** for keeping, over **15 572** pairs. **GAP:** the exact adjustment-free predicate behind that 15 572 is not among the scripts kept from that round, so those three counts cannot be re-derived here. Two things are re-derived, and one earlier sentence here was wrong. The filters above do NOT bracket those counts: their maxima are 164 / 26 / 9, so all three decision-run counts lie strictly above every row. The difference is a float-equality tolerance rather than a lost predicate - re-running the same filters with an absolute `1e-9` range comparison instead of a relative one moves every one of the six cells by a constant +6, which puts 168 / 30 / 14 inside the band, and the six extra pairs (SPIR twice, AI four times) carry a 0.0 bps excursion against an identical daily range. What does reproduce exactly is all three worst excursions and the order of the three options with the ratios between them. The decision does not depend on the filter.

#### 2. The premise behind the rewrite is false on a measured case

The retired D4 rested on "the close-stamped bar's open is the closing-auction print". For **CRSR 2026-08-06** it is not. Read directly from the two stores (**M11**):

| | open | high | low | close |
|---|---:|---:|---:|---:|
| the official daily row, `grouped_daily_history/2026-08-06.parquet` | 11.08 | **11.1717** | **10.45** | **10.61** |
| the close-stamped minute bar, `population_ladders/bars/CRSR_2026-08-03.parquet` | 10.89 | 12.80 | 10.61 | 12.79 |
| the pre-close minute bar (`close − 60 000`) | 10.575 | 10.63 | 10.565 | **10.61** |
| the rest of the session, highs and lows | — | **11.1717** | **10.45** | — |

The auction print is that bar's **LOW**, not its open: the company reported after the close and the next print jumped to 12.80. So the rewrite would have written 10.89, **264 bps** away from the real closing price, while the pre-close bar's own close is 10.6100 exactly. And the half-open grid reproduces the official daily `[low, high]` on this session **exactly**: `[10.45, 11.1717]`. This single session is also the store's worst whole-bar excursion — 12.80 against the official high of 11.1717 is **+1457.5 bps**, the 1457.5 in the table above.

#### 3. Rewriting a bar to one price destroys reachable range

The walk tests a rung with `bar.low <= limit`, so raising a bar's low to its open makes any level in between unreachable. **M15**, on the real `walk()` with one rung at 68.00: the whole bar `o 68.5 h 69.0 l 67.0` fills 13.2353 units (900 / 68.00, the test helper's budget with no sizing buffer; a document carrying the 1% FX buffer spends 891.0 and buys 13.1029) and reports `open`, while the same bar as the single price 68.5 reports `no_fill` with zero units and an empty trace. The same mechanism suppresses a stop fire. A rule that silently makes declared levels unreachable is worse than one that removes a minute.

#### What the whole bar costs, in the direction the engine cares about

The engine treats the two exit legs differently, in its own words:

* `walk.py:497-498` — "The stop RESTS at the broker, so a bar that opens already through it executes at the open", filling at `price = min(bar.open, state.stop)` (`:512`). A broker's resting stop is a regular-session order; a post-market print cannot fill it. Letting one do so is flatly wrong.
* `walk.py:707-710` — "A take-profit does not rest at the broker in this model, so the gap rule of section 4.4 does not reach it: it fills AT its level, and the distance between that and where a live fill would have landed is the ``take_profit_observation_time`` divergence the envelope reports" — a divergence the envelope already publishes (`envelope.py:93`, `:117`).

**M10** (`b12_dir.py`), the 182 of 15 677 close-stamped bars (1.161%) whose range leaves the range the rest of their session traded — all 182 of which the half-open grid removes:

| direction | sessions | share of 15 677 | median | p90 | max |
|---|---:|---:|---:|---:|---:|
| high extended — reaches a polled take-profit | 119 | 0.759% | 28.3 bps | 269.8 bps | **1457.5 bps** |
| low extended — reaches a resting stop the session never reached | 62 | 0.395% | 12.8 bps | 84.2 bps | **301.6 bps** |
| both directions in one bar | 1 | 0.006% | 50.8 bps | 50.8 bps | 50.8 bps |

**Per run the exposure is larger than per session**, because a window's terminal bar is a close-stamped bar by construction. Over the 881 real windows in the store, median 15 sessions each: **149 (16.91%)** contain at least one such session, **25 (2.84%)** contain an excursion above 2% and **6 (0.68%)** one above 5%. Under the half-open grid, none.

### 5.4 What the exclusion costs, stated and quantified

**A position still open at the end of the window is marked one minute early.** `HorizonOpen` values the remaining units at the CLOSE of the last bar the walk saw (`walk.py:1000`, `price=last_close`; `trace.py:196-199`, "``price`` is the CLOSE of the last bar the walk saw"). Under the half-open grid that last bar is the pre-close minute, so the mark is that minute's close rather than the official closing price.

**M4** (`b2_mark.py`), over the 15 677 close-stamped pairs that have a row in the daily store:

| candidate for the session's final mark | matches the official close exactly | median abs deviation | p90 |
|---|---:|---:|---:|
| the close-stamped bar's OPEN (what the retired rule marked at) | 14 658 (93.50%) | 0.0000% | 0.0000% |
| **the pre-close bar's CLOSE (what half-open marks at)** | **3 752 (23.93%)** | **0.0337%** | 0.1269% |

On the adjustment-free subset (filter F1, n = 15 160) the same two rows read 96.69% and 24.75%, with the half-open mark's median absolute deviation at **0.0319%**; the decision run reported 0.0322% on its own adjustment-free filter. 3 579 pairs (22.83%) match on both, because the auction printed at the preceding minute's close.

**This is accepted, for a reason that is about what a backtester can do with a bar.** A replay triggers on highs and lows: a level is reached or it is not, and that is what flips an outcome from `open` to `closed_tp` or `closed_stop`. A mark on a still-open position changes a published number continuously and by the amounts in the table — a median of 3.4 basis points — while a range that reaches a level the session never traded changes the *outcome word*. §5.3's first measurement says the range is most faithful under half-open; this table says the mark is least faithful under it. The outcome error is the one worth removing.

Two things keep the cost visible rather than silent:

* **The envelope already publishes which bar the mark came from.** `walked.to_t` (`walk.py:121`, rendered at `envelope.py:163`, the fifth of twelve envelope keys — **M6** confirms the twelve: `schema`, `intent_id`, `instrument`, `window`, `walked`, `config`, `fx`, `divergences`, `intrabar_rule`, `outcome`, `summary`, `trace`). A reader comparing `walked.to_t` with the session close sees the one-minute offset without being told.
* **On the one session this memo runs end to end, half-open's mark is exact.** CRSR 2026-08-06's pre-close bar closes at 10.6100 and the official daily close is 10.6100 (**M11**). The 0.0337% is a distribution, not a constant, and the decisive case sits at zero.

### 5.5 Granularity is a SEPARATE gate, phrased on the measured spacing

The session grid cannot double as a granularity check, and under the half-open rule it refuses one coarse shape by accident, which must not be mistaken for a gate.

**M8** (`b6_daily.py`), applying the predicate to every file in the repo's own daily store `~/.alphalens/grouped_daily_history/`: each file carries one `t` per row, stamped at nominal 16:00 New York local time, DST-aware. On a full session that stamp **equals** the real close, so **492 of 497 files** are stamped exactly AT the close and the half-open grid keeps **0 of 497**. The other 5 are half-days where the nominal stamp misses the real early close (2024-11-29, 2024-12-24, 2025-11-28 and 2025-12-24 stamped `21:00:00Z` against an `18:00:00Z` close; 2025-07-03 stamped `20:00:00Z` against `17:00:00Z`), and those are placed after the close.

So a daily series built from this repository's own store refuses with `bars_unusable/empty_window` — by emptiness, not by granularity. **That is not a granularity gate**, because a daily series stamped at the session OPEN passes the grid intact. **M13** (`b11_gran.py`), over 10 XNAS sessions from 2026-09-28:

| series | built | kept, inclusive | kept, half-open | modal `t` spacing of the kept series |
|---|---:|---:|---:|---|
| daily at the session open | 10 | 10 | **10** | 86 400 000 ms |
| daily at the session close | 10 | 10 | **0** | none — the series is empty |
| hourly | 70 | 70 | **70** | 3 600 000 ms |
| 30-minute | 140 | 140 | **130** | 1 800 000 ms |
| 5-minute | 790 | 790 | **780** | 300 000 ms |
| 1-minute | 3 910 | 3 910 | **3 900** | 60 000 ms |

D2's reason — one low per session carries no path — applies to every coarse granularity, not only to one-per-session. The gate is therefore phrased on the **modal `t` spacing of the whole kept series**, which separates every row above that is non-empty:

```
bars_wrong_granularity / not_one_minute
  refuse a kept series of two or more bars whose modal t-spacing is not 60 000 ms
```

**It must be the WHOLE series, not per session, and that is measured.** **M13** (`r10_modal.py`), over the real store: a per-SESSION modal-spacing rule would over-fire on **26 of 17 163 pairs (0.15%)** — sparse sessions whose commonest gap is 2, 3, 4 or 9 minutes. The same rule over the whole kept series of each file fires on **0 of 881 real windows**: every one has a modal spacing of exactly 60 000 ms. A series of fewer than two bars has no spacing to measure and is exempt, which keeps the one-bar-at-`walk_start` case legal.

The grid itself stays count-blind and completeness-blind. **M1**: across all 17 163 pairs, **zero** carry fewer than two kept bars under the half-open grid, and the bars per pair run 13 (floor) / 359 (median) / 390 (max). A 390-bar completeness check would refuse **13 700 of 17 163 pairs (79.8%)** under this grid — a check that fires on four fifths of real data, which is why there is none. `validate_sequence` refuses only an empty sequence (`bars.py:236-244`), and a single bar at `walk_start` covers the window on both sides (`bars.py:266-296`).

### 5.6 A fetched bar off the grid is DROPPED; a supplied one is FILTERED at the close and REFUSED elsewhere

This is the rule most likely to be mis-implemented, and the one where the repository's own idiom decides it. The distinction is **a bar the grid can place and excludes** against **a bar the grid cannot place at all**.

* **Fetch path.** A raw Polygon window legitimately contains minutes outside regular trading hours. **M1** measures how many: 6 364 368 fetched against 5 595 495 kept, a ratio of **1.1374** — **768 873 bars, 12.08% of the payload**, spread across pre-market (08-13Z), post-market (20-23Z) and the 15 677 stamped exactly at a close. Everything off the grid is **dropped**, and the counts go into the sidecar split in two: `counts.dropped_at_close` and `counts.dropped_off_grid`.
* **`--bars` path, the close-stamped bar: FILTERED, counted, enumerated.** **M12** says a file built with any of this repository's three RTH helpers carries one close-stamped bar per session — 40 of 40 real files, 805 bars over 831 sessions. Refusing that bar would refuse the files the override exists to accept, and the refusal's own best advice would be "stop using `--bars`". That is the shape of a guard that gets switched off. So the close-stamped bar is dropped silently in the sense that the run proceeds, and *not* silently in the sense that matters: the sidecar carries the count and every dropped bar's `t`, session and full `{open, high, low, close}` (§9), so the supplied series can be rebuilt exactly from the sidecar plus the written file.
* **`--bars` path, a bar the grid cannot place: REFUSED.** Strictly after a session close, before a session open, or inside no session interval of that MIC at all. The membership test is on INTERVALS and never on a bar's UTC date, because a session can open on a date that is not itself a session: measured, XASX session 2026-10-12 opens at 2026-10-11T23:00:00Z and XNZE at 2026-10-11T21:00:00Z, both on a Sunday for which `is_trading_day` answers False. Bucketing by UTC date would drop the first hour of every XASX session. No fixture pick is non-US, so the exposure is latent - which is exactly why the rule is written as intervals here rather than discovered later. These are not grid exclusions, they are bars the operator's construction cannot account for — the incident's own shape. `bars_off_grid`, with every offender named (§11).

The asymmetry is therefore not fetch-against-supplied. It is **close-stamped against unplaceable**, and it applies the same way on both paths: the close bar is excluded on both, and an unplaceable bar is refused on the supplied path and dropped-with-a-count on the fetch path, where it is the vendor's normal output rather than somebody's assertion.

**It still refuses the incident.** The fatal bar was `2026-09-28T21:30:00.000Z`, epoch `1790631000000`, ninety minutes past that session's close of `2026-09-28T20:00:00.000Z` (`1790625600000`, open `1790602200000`) — strictly after the close, so unplaceable, so refused. Applied over the XNAS session list 2026-09-23 → 2026-10-06: `2026-09-28T21:30:00Z` refused; `2026-10-02T20:00:00Z` dropped and enumerated (it is that session's closing minute); `2026-10-02T19:59:00Z` kept; `2026-09-23T13:30:00Z` kept.

**GAP, and it bounds what that last claim is worth.** `2026-10-02T20:00:00Z` is `SMMT:2026-09-23`'s own `entry_deadline` (`1790971200000`, **M7**), so it is the terminal bar of the exact run whose retracted number this memo is about. Whether SMMT's own close-stamped bar on that session carried a post-close excursion cannot be measured here: SMMT is not in the local bar store, and of the 12 comparable picks only VCYT appears, for one July window (**M1**). So "it still refuses the incident" is a claim about the 21:30Z bar alone. It is NOT a claim that this design would have produced the right published number.

### 5.7 Legitimate inputs the predicate must not refuse

A guard that over-fires is worse than none, so each row has a test in §12.

| input | passes | why |
|---|---|---|
| a bar at `t == open` | yes | the lower bound is inclusive; 99.56% of real pairs carry one (**M9**) and `check_window_covers` requires the first bar at or before `walk_start` (`bars.py:287-295`) |
| **a bar at `t == close` in a FETCHED window** | **yes, as a counted drop** | it is one bar per session, 15 677 of 17 163 pairs (**M1**); §5.3 is why it is excluded |
| **a bar at `t == close` in a SUPPLIED file** | **yes, as a counted and enumerated drop — never a refusal** | **M12**: all three of the repo's RTH helpers produce it, on 40 of 40 real files |
| a file built with this repo's own RTH filter | yes, filtered | **M12**: 805 of its bars are dropped, 805 of 831 sessions; the run proceeds and the sidecar lists them |
| another venue, e.g. XWAR | yes | the grid asks `session_close_utc` per session instead of adding 390 minutes; without that XWAR loses 90 minutes and XETR 120, while XSHG gains 60 and XTAI 120 (**M2**) |
| a half-day | yes | `exchange_calendars` stores the actual per-session close, so XNAS 2026-11-27 grids to `14:30Z-18:00Z` with the last kept minute at `17:59:00Z`, no special case (**M2**) |
| a window spanning a DST change | yes | the grid is built per session: XNAS 2026-10-30 grids `13:30Z-20:00Z` (last kept `19:59Z`), 2026-11-02 grids `14:30Z-21:00Z` (last kept `20:59Z`) (**M2**); nothing is a fixed offset from the window start |
| a one-bar series at `walk_start` | yes | one bar; it has no spacing, so the granularity gate is exempt by construction |
| a sparse session | yes | no completeness check, and the granularity rule reads the whole series, not the session: a per-session rule would refuse 26 real pairs (**M13**) |
| a daily series stamped at each session's OPEN | **no, by granularity** | the grid KEEPS all of it (**M13**: 10 of 10); the modal spacing is 86 400 000 ms, so `bars_wrong_granularity/not_one_minute` refuses it |
| a daily series stamped at each session's CLOSE | **no, by emptiness** | the grid keeps nothing (**M8**: 0 of 497 real daily files contribute a bar), so `bars_unusable/empty_window` refuses first; this is NOT the granularity gate and must not be mistaken for it |
| an hourly / 30-minute / 5-minute series | no | modal spacing 3 600 000 / 1 800 000 / 300 000 ms (**M13**); the grid keeps 70 / 130 / 780 bars of them |
| a post-market bar in a FETCHED window | yes, as a drop | the vendor returns 12.08% off-grid bars (**M1**); dropping with a count is correct |
| a bar strictly after a session close in a SUPPLIED file | **no, refused** | the incident's shape; the grid cannot place it on any session |

### 5.8 One input that used to work and now does not

**M9** (`b10_openbar.py`), over the whole store: 75 of 17 163 pairs (0.437%) carry no bar at the session open, and in **44** of those a pre-market bar exists on the same session; the first kept bar arrives 1 minute late at the median and 23 at the worst. Before this change an operator could include that pre-market bar and satisfy `check_window_covers`. Under the grid it is dropped on the fetch path and refused on the supplied path, so the run refuses with `bars_unusable/walk_start_uncovered`.

This is a deliberate tightening, not an oversight. Replaying a pre-market print as the opening minute is exactly what `population_ladder_monitor._filter_bars_to_rth` declines to do — "Pre/post-market fills are DELIBERATELY not modelled — resting-limit geometry is an RTH construct" (`:752-753`), which is the same sentence that argues for §5.3. The refusal says what happened ("the venue printed no opening bar for this session") instead of handing the operator the engine's `begins_after_walk_start` to map back onto a fetch fact. Only day 1 can trip it, since `check_window_covers` looks at the first bar alone, so the per-run rate is the 0.437% above. No override flag is added.

---

## 6. The window, and why the deadline is a requirement on the SERIES

### 6.1 The rule, stated once

```
exit_instant   = exits[-1].venue_time.value       (absent -> the pick never filled)
anchor         = max(entry_deadline, exit_instant)
anchor_session = session_on_or_after(anchor.date(), mic)
deadline_sess  = session_on_or_after(entry_deadline.date(), mic)

required_end   = session_close_utc(advance_trading_sessions(deadline_sess, 1, mic), mic)
desired_end    = session_close_utc(advance_trading_sessions(anchor_session,
                                   _WINDOW_MARGIN_SESSIONS, mic), mic)        # margin = 2
last_servable  = previous_trading_day(session_on_or_after(now_utc.date(), mic), mic)
servable_end   = session_close_utc(last_servable, mic)

if servable_end < required_end:  REFUSE   bars_window_unservable / deadline_not_yet_servable
window_start   = walk_start
window_end     = min(desired_end, servable_end)
```

One function computes both the filled and the never-filled case, because a module that computes one quantity on two sides and does it twice ends up describing two different panels.

`required_end` is the one line the half-open grid changed, and §6.3 is why.

### 6.2 Why the anchor is `max(deadline, exit)`

Measured on the four comparable closed picks (**M3**):

| pick | MIC | walk_start | entry_deadline | real exit (`exits[-1].venue_time`) |
|---|---|---|---|---|
| SMMT:2026-09-23 | XNAS | 2026-09-23T13:30:00Z | 2026-10-02T20:00:00Z | 2026-09-29T13:30:58.853Z |
| ASTS:2026-09-23 | XNAS | 2026-09-23T13:30:00Z | 2026-10-02T20:00:00Z | 2026-09-29T13:36:34.887Z |
| EWTX:2026-09-25 | XNAS | 2026-09-25T13:30:00Z | 2026-10-06T20:00:00Z | 2026-09-30T17:26:32.675Z |
| VST:2026-09-21 | XNYS | 2026-09-21T13:30:00Z | 2026-09-30T20:00:00Z | 2026-10-01T13:59:21.248Z |

VST's real exit is **after** its entry deadline, and the other three are before it. Taking the exit alone would truncate three windows while the ladder was still live; taking the deadline alone would truncate VST.

### 6.3 The deadline millisecond is a WINDOW matter, and a late `entry_expired` is accepted

`walk.py:171-172` is, verbatim at HEAD:

```python
    if bar.t < deadline or not state.pending:
        return
```

and the deadline is `config.entry_deadline.value`, which the script derives as `session_close_utc(last_session)` (`pick_window.py:69`). Under the half-open grid **no bar carries that instant**. So the condition for `_expire` to fire is not "the window reaches the deadline" but "the kept series carries a bar at or after the deadline", and under this grid the first such bar is the next session's OPEN.

**M6** (`b3_expire.py`), the real engine on `SYM:2026-09-30`'s own document and configuration (deadline `2026-10-09T20:00:00Z`, a **Friday**; one entry limit at 40.34; a flat tape above it so nothing fills):

| half-open series | bars | outcome | trace | `entry_expired` at | lateness | `walked.to_t` |
|---|---:|---|---|---|---:|---|
| ends at the deadline session 2026-10-09 | 3 120 | `no_fill` | `[]` | **never fires** | — | 2026-10-09T19:59:00Z |
| **+1 session, to 2026-10-12** | 3 510 | `no_fill` | `['entry_expired']` | **2026-10-12T13:30:00Z** | **65.5 h** | 2026-10-12T19:59:00Z |
| +2 sessions, to 2026-10-13 | 3 900 | `no_fill` | `['entry_expired']` | 2026-10-12T13:30:00Z | 65.5 h | 2026-10-13T19:59:00Z |
| +1 session, tape DROPPED below the limit from the deadline onward | 3 510 | `no_fill` | `['entry_expired']` | 2026-10-12T13:30:00Z | 65.5 h | 2026-10-12T19:59:00Z |

Three facts, in the order a change must not break them.

**1. The window must extend by one session, and that is the whole fix.** Row 1 is the failure mode: `no_fill` with an EMPTY trace, which `walk.py:131-134` also returns for a run that simply ran out of tape. A truncated run and a ladder that genuinely expired then say the same word, which is precisely the ambiguity §6.6 exists to prevent. So `required_end` is the close of the session AFTER the deadline session. Stated on the series instead of on the window end, the requirement is grid-independent and did not change at all: **the kept series must carry a bar at or after `entry_deadline`**, which is the same sentence the inclusive draft's post-fetch check already used (§6.6).

**2. A late `entry_expired` is ACCEPTED.** It names the wrong instant — the next session's open rather than the deadline — and it names nothing else wrongly. Row 4 is why: with a tape that falls through every entry limit from the deadline instant onward, there are still **zero fills**. `_expire` is called first in `_bar_decisions` (`walk.py:871`) and empties `state.pending` before `_fill_entries` runs (`:884`), so the late-firing bar cannot resurrect a rung. The outcome word, the filled fraction and every money number are the same as they would be with a bar at the deadline. What a reader loses is the attributed instant, and `_expire`'s own docstring already tells them what the event means ("a bar stamped exactly at the deadline is already outside the ladder's life", `walk.py:163-168`), while the envelope publishes `config.entry_deadline` beside the event's `t` — so the offset is visible in the published object, not hidden.

**3. The lateness is bounded and usually one night.** **M5/M7** (`b4_window.py`), over the 250 consecutive-session gaps in XNAS's and XNYS's 2026 calendars, the distance from a session close to the next session's open:

| gap | 17.5 h | 41.5 h | 64.5 h | 65.5 h | 66.5 h | 68.5 h | 89.5 h | 92.5 h |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| sessions | **197** | 1 | 1 | **41** | 1 | 1 | 7 | 1 |

So 197 of 250 are a single overnight gap and the worst in 2026 is a holiday weekend at 92.5 h. `SYM:2026-09-30`'s deadline falls on a Friday, which is why **M6** measures 65.5 h rather than 17.5.

**What was NOT done:** re-admitting the close-stamped bar to make the event land on the deadline. That would reinstate the whole-bar treatment §5.3 refuted, and it would buy an attributed instant with an outcome word.

### 6.4 Why `required_end` is hard and the margin is soft

The two edges of the window are not the same kind of fact.

* **`required_end` is not negotiable.** The entry ladder was legally alive through `entry_deadline`, and the script cannot know in advance whether the replay will fill: that is the output. The requirement is therefore unconditional, for the same reason `required_end` is anchored on the deadline and not on the exit instant.
* **The margin beyond it is best-effort, and truncating it is safe because it is VISIBLE.** The replay can enter later than reality — that is the divergence a trailing-entry question is about — so its own exit can fall later than the real one. Two sessions is bounded and cheap. If the vendor cannot serve the margin, the window is simply shorter: a never-filled run has already published `entry_expired` (**M6**), and a filled run publishes the terminal state `open`, "The bars ran out with the position still open." (`trace.py:91`, `:197`), which reads as "re-run with more bars". The sidecar records `margin_sessions_requested` and `margin_sessions_served`, so a shortened margin is stated rather than inferred.

Note that `desired_end` already includes the session `required_end` names whenever the anchor is the deadline, because the margin is two sessions and the requirement is one. The requirement bites only on the SERVABLE side.

### 6.5 Why the cap is the vendor's CALENDAR DAY, not the last closed session

The vendor restriction is calendar-day based. `polygon_client.py:236-239`:

> "NOTE (operational): Polygon's free / Basic plan serves only PAST-day minute aggregates, not the current session. Callers must request windows that have fully closed."

A cap built on `session_not_closed` instead tracks the wall clock and crosses into the current calendar day after the close. **M5**:

| `now` | `session_not_closed` | `previous_trading_day` of it | that cap | `previous_trading_day(session_on_or_after(now.date()))` |
|---|---|---|---|---|
| 2026-10-09T08:14Z | 2026-10-09 | 2026-10-08 | 10-08T20:00Z | **2026-10-08** |
| 2026-10-09T12:00Z | 2026-10-09 | 2026-10-08 | 10-08T20:00Z | **2026-10-08** |
| 2026-10-09T21:00Z | 2026-10-12 | **2026-10-09** | **10-09T20:00Z** | **2026-10-08** |

The right-hand column is this design's rule, and it is stable across the session's close: an evening run never asks for a session the vendor will not serve. **GAP:** the vendor note does not say in which timezone its day boundary falls, and measuring it needs a live call this design pass did not make. This design uses the UTC date and does not rely on being right about it — the post-fetch check below closes the hole whatever the boundary is. §16 Q3 records the objection that follows from the GAP.

### 6.6 The refusal, and the same check again AFTER the fetch

```
code      bars_window_unservable
reason    deadline_not_yet_servable
exit      7            (the reserved transient status; time fixes it, re-running is safe)
retryable true
```

Human message, for `SYM:2026-09-30` run at `now = 2026-10-09T08:14:03Z`:

> `replay_from_pick: bars_window_unservable: SYM:2026-09-30's entry ladder was live through session 2026-10-09 (entry_deadline 2026-10-09T20:00:00.000Z). The session grid is half-open at the close, so the ladder's expiry is only observable on the NEXT session, 2026-10-12 (close 2026-10-12T20:00:00.000Z), and the price vendor serves only past calendar days. The last session it can serve today is 2026-10-08 (close 2026-10-08T20:00:00.000Z). Re-run on 2026-10-13 or later. Replaying the short window would publish outcome no_fill with an empty trace, which is what a ladder that genuinely expired also publishes.`

`--format json` object:

```json
{"schema": "alphalens.research.replay_from_pick.error/v1",
 "code": "bars_window_unservable", "reason": "deadline_not_yet_servable",
 "message": "...", "retryable": true,
 "details": {"pick": "SYM:2026-09-30", "exchange": "XNAS",
             "entry_deadline_t": 1791576000000,
             "entry_deadline_session": "2026-10-09",
             "expiry_observable_session": "2026-10-12",
             "required_end_t": 1791835200000,
             "last_servable_session": "2026-10-08",
             "last_servable_end_t": 1791489600000,
             "servable_from_date": "2026-10-13",
             "now": "2026-10-09T08:14:03.000Z"},
 "suggestions": [{"argv": ["python", "-m", "scripts.replay_from_pick", "<trades.json>",
                           "SYM:2026-09-30", "--out", "<DIR>"]}]}
```

**And the same check again after the fetch.** Predicting the vendor's day boundary is a claim; checking the series is a measurement. So the kept series must also *carry* a bar at or after `entry_deadline`. If it does not, the run refuses with the same code and reason `vendor_served_short`, naming the last bar it actually got. This runs on the `--bars` path too, where it is the single most valuable new guard: a supplied file that stops before the ladder's expiry is observable is refused rather than replayed. It is the symmetric partner of the engine's own left-edge `check_window_covers`, and — stated on the series rather than on the window end — it is the one line of §6 the grid change did not touch.

### 6.7 How often the refusal fires, and what it costs

**M7** (`b4_window.py`), the rule applied to all 12 comparable fixture records, under both grids' `required_end`, at four values of `now`:

| pick | MIC | entry_deadline | `required_end` half-open | servable end @ 10-09T08:14Z | inclusive | **half-open** |
|---|---|---|---|---|---|---|
| ASTS:2026-09-23 | XNAS | 10-02T20:00Z | 10-05T20:00Z | 10-08T20:00Z | run | run |
| BE:2026-09-29 | XNYS | 10-08T20:00Z | 10-09T20:00Z | 10-08T20:00Z | run | **REFUSE** |
| BE:2026-09-30 | XNYS | 10-09T20:00Z | 10-12T20:00Z | 10-08T20:00Z | REFUSE | **REFUSE** |
| DAVE:2026-09-25 | XNAS | 10-06T20:00Z | 10-07T20:00Z | 10-08T20:00Z | run | run |
| EWTX:2026-09-25 | XNAS | 10-06T20:00Z | 10-07T20:00Z | 10-08T20:00Z | run | run |
| OSCR:2026-09-30 | XNYS | 10-09T20:00Z | 10-12T20:00Z | 10-08T20:00Z | REFUSE | **REFUSE** |
| PLTR:2026-09-29 | XNAS | 10-08T20:00Z | 10-09T20:00Z | 10-08T20:00Z | run | **REFUSE** |
| QBTS:2026-09-21 | XNAS | 09-30T20:00Z | 10-01T20:00Z | 10-08T20:00Z | run | run |
| SMMT:2026-09-23 | XNAS | 10-02T20:00Z | 10-05T20:00Z | 10-08T20:00Z | run | run |
| SYM:2026-09-30 | XNAS | 10-09T20:00Z | 10-12T20:00Z | 10-08T20:00Z | REFUSE | **REFUSE** |
| VCYT:2026-09-29 | XNAS | 10-08T20:00Z | 10-09T20:00Z | 10-08T20:00Z | run | **REFUSE** |
| VST:2026-09-21 | XNYS | 09-30T20:00Z | 10-01T20:00Z | 10-08T20:00Z | run | run |

| `now` | inclusive `required_end` refuses | **half-open `required_end` refuses** |
|---|---:|---:|
| 2026-10-09T08:14:03Z | 3 of 12 | **6 of 12** |
| 2026-10-09T21:00:00Z | 3 of 12 | **6 of 12** |
| 2026-10-12T08:14:03Z | 0 of 12 | **3 of 12** |
| 2026-10-13T08:14:03Z | 0 of 12 | **0 of 12** |

**That doubling is the measured cost of the half-open grid on the window, and it is accepted.** All six refusals are never-filled picks whose deadline falls on the current or previous session, every one clears within two further calendar days with no code change, and the verdicts do not move across the session close (08:14Z and 21:00Z agree). The alternative is publishing `no_fill` with an empty trace for a window that could not observe the expiry — the exact statement §6.3 row 1 measures and §6.6's message names. Nothing proceeds silently truncated: the gate is `servable_end >= required_end`, and `min(desired, servable) >= required` follows from it.

### 6.8 The reader-side signal that complements the refusal

`bd724cb2` (#1746) added `walked_from_t` / `walked_to_t` / `walked_bars` to the walk result (`walk.py:120-122`) and publishes them as the envelope's `walked` block (`envelope.py:162-164`), the fifth of twelve keys (`apps/intent-replay/README.md:223`). So the condition

```
outcome == "no_fill"  AND  trace is empty
```

is the published, machine-readable statement "this run ran out of tape before the ladder's expiry could be observed", and `walked.to_t` says how far it did get. The engine's README already explains why `walked` exists for exactly this kind of reading (`:298-309`). **M6** confirms both halves: the truncated run reports `walked.to_t = 2026-10-09T19:59:00Z` with an empty `trace`, where every sufficient window reports `trace: ['entry_expired']`. Two independent signals, one per key. The script's refusal stops the run from being produced; these two let a reader catch a run produced some other way.

### 6.9 Worked window, under the half-open rule

`SMMT:2026-09-23`, `now = 2026-10-09T08:14:03Z`, every epoch measured from the calendar (**M7**):

| | |
|---|---|
| `walk_start` | `2026-09-23T13:30:00Z`, epoch `1790170200000` |
| `exits[-1].venue_time` | `2026-09-29T13:30:58.853Z` |
| `entry_deadline` | `2026-10-02T20:00:00Z`, epoch `1790971200000`, session `2026-10-02` |
| `required_end` = close of the next session | `2026-10-05T20:00:00Z`, epoch `1791230400000` |
| `anchor` = max(deadline, exit) | `2026-10-02T20:00:00Z`, session `2026-10-02` |
| `desired_end` = +2 XNAS sessions | `2026-10-06T20:00:00Z`, epoch `1791316800000` |
| `last_servable` / `servable_end` | `2026-10-08` / `2026-10-08T20:00:00Z`, epoch `1791489600000` |
| `servable_end >= required_end`? | yes — no refusal |
| `window_end` = `min(desired, servable)` | `2026-10-06T20:00:00Z`, margin **2 of 2 served** |
| sessions in the window | 10 (2026-09-23 through 2026-10-06, none a half-day) |
| kept-bar ceiling, half-open | 10 × **390** = **3 900** bars |
| of those, dropped at a close | **10**, one per session, each enumerated in the sidecar |
| raw fetched bars, at the measured ratio 1.1374 (**M1**) | ≈ **4 436**, against a one-call limit of 50 000 (`polygon_client.py:214`) |

The figure is 390 minutes per session, not 391, precisely because the close bar is dropped.

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
10 session-grid placement              REFUSE bars_off_grid for an unplaceable bar (--bars only)  §5.6
11 session-grid membership             drop t == close_ms, counted + enumerated; drop the rest  §5.1, §5.6
12 granularity gate                    bars_wrong_granularity / not_one_minute                 §5.5
13 left-edge coverage                  first kept bar vs walk_start
14 right-edge coverage                 REFUSE bars_window_unservable / vendor_served_short      §6.6
15 write bars, then the sidecar
```

Steps 1 and 2 run before any vendor call, exactly as they do today, and step 5 now joins them, so an excluded pick and an unanswerable window both refuse before quota is spent.

**Steps 10 and 11 are two steps, not one, and the split is the whole of §5.6.** Step 10 asks whether the grid can place the bar on any session at all and, on the `--bars` path, refuses if it cannot. Step 11 then applies the half-open predicate and drops what falls outside, recording the close-stamped drops separately from the rest. Collapsing them into one filter is how a supplied file ends up either silently stripped of the incident's bar or refused for carrying the repo's own.

Step 12 runs after step 11 because the gate reads the modal spacing of the KEPT series, and dropping one bar per session cannot change a modal spacing of 60 000 ms — measured, **M13**: 0 of 881 real windows under either grid.

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

The guard is independent of the grid in both directions: the lookup is about the window, not about any one bar, and an ex-date step arrives in a session's prices rather than inside one minute. The grid change neither helps it nor hurts it.

### The one threshold, named, because the composition carries it

`PolygonCorporateActionsLookup._first_material_dividend` (`corporate_actions.py:178-205`) applies, at `:201`:

```python
if cash / close > SPECIAL_DIVIDEND_PRE_EX_CLOSE_FRACTION:
```

with `SPECIAL_DIVIDEND_PRE_EX_CLOSE_FRACTION = 0.10` declared at `corporate_actions.py:83`. That is a size threshold on a quantity, inside this composition, and a design that claimed to add none would be wrong about its own parts. What is true is narrower and is the part that matters: **this design adds no threshold on a RETURN**, and it adds no number of its own except `_WINDOW_MARGIN_SESSIONS = 2`, which is a count of sessions and not a test on a measured price. The grid adds no number at all: it is a comparison against two instants the calendar supplies.

The inherited 0.10 is the right threshold to inherit, for a reason the module itself records at `:79-82`: "Ordinary quarterly dividends never come near it." Its denominator is the pre-ex close from the raw grouped-daily store, so it compares like with like.

**Known residual, stated rather than solved:** a dividend *below* 0.10 of the pre-ex close does not refuse the run, and raw (`adjusted=false`) bars do step down by it on the ex-date, which a resting stop can be hit by. **GAP:** the size of an ordinary dividend as a fraction of the close is not measured in this store in this pass, so the magnitude of that residual is unknown. It is bounded above by 10% of the close by construction.

### Why no return threshold

1. **The quantity the 0.60 is defined on does not exist in a replay.** `IMPLAUSIBLE_RETURN_THRESHOLD = 0.60` (`bar_window.py:33`) is applied to a *trade* return against a frozen anchor: `population_ladder_monitor.py:788` uses `abs(forward_return)` against the frozen arrival-window minute VWAP, `ladder_replay.py:1683-1687` uses `abs(exit_mark / arm_blended - 1)`. A replay has no such anchor — its entry price is an output of the run. Choosing a threshold here would mean inventing a quantity to apply it to.
2. **Size thresholds on returns have measured zero detection power in this store.** `feedback/split_audit.py:10-14` on the old band, "fail the row when a close ratio leaves `(0.55, 1.8)`": it "caught zero splits and produced three false positives (one real -47% day, reported identically by a second vendor), while being structurally blind to 3-for-2, 4-for-3 and 5-for-4 — roughly a fifth of US splits. It cannot be fixed by widening: catching a 3-for-2 means failing every -33% earnings day." The same module records the method that does work — persistence, not size: on MQ, the one real artefact in the store (1-for-4 reverse), the cross-vendor ratio is exactly 0.2500 for 20 sessions and exactly 1.0000 for the following 22, while over 13 203 artefact-free comparisons it deviates from its own median by 0.000000 at the 99th percentile (`split_audit.py:25-29`).
3. **Raw bars are the correct basis, so in most windows there is nothing to detect.** `get_agg_range` defaults to `adjusted=False` (`polygon_client.py:212`), and the document's limits, stops and take-profits are prices the author wrote from live unadjusted quotes. `split_audit.py:36-39` states the arithmetic: a uniform rescaling of every close in a window cancels out of the return, so only a window that *crosses* a break carries a fabricated step.
4. **A window that does cross an action is a run to refuse, not a value to rescale.** Real brokers cancel resting orders on corporate actions, and the Saxo rail this project trades on does. A mechanically adjusted resting ladder models an order state that would not have existed. That is the `SPLIT_INVALIDATED` class (`corporate_actions.py:63`, disposition at `:67` and `:340`), and refusing matches how the script already treats `replay_exclusions` (`replay_from_pick.py:322-333`).

### Why a failed lookup refuses, when the block it comes from carries the row forward

The block's disposition tree says `lookup failed → carry, counted` (`corporate_actions.py:18`), and the lookup is fail-closed by construction: a missing pre-ex close raises rather than defaulting (`:195-199`, docstring `:131-135`). **"Carry, counted" is a DEFERRAL, not a permission.** The monitor sweeps the whole population nightly, so carrying a row means "ask again in 24 hours" — the row is not published as clean and it is not published as dirty; it is held.

A one-shot laboratory command has no nightly sweep, so its only honest analogue of a deferral is a refusal the operator can re-run: exit 7, `retryable: true`, reason `lookup_failed`, with the message saying the question could not be answered and that re-running will ask again. Writing the bars with a `lookup_failed` verdict, as an earlier draft did, would open a gate the composed block closes — it publishes a run whose basis was never checked, which is the shape of the incident this whole design exists for.

The consequence is that only **one** verdict is ever written into a sidecar: `"none"`. That is the design, not an accident. "Asked and clean" is the written file; "found an action" and "could not ask" are both refusals, each with its own code. A reader holding a sidecar therefore knows the lookup answered, without having to trust a field that might have meant three things.

### What it costs, worst case

Per run: 1 aggregate call, 1 splits call, 1 dividends call, and **one whole-market grouped-daily call per dividend record whose ex-date is not already cached** — `_first_material_dividend` calls `pre_ex_close(upper, ex_date)` once per record, inside the loop, before testing materiality (`corporate_actions.py:184-201`), and the monitor's own `_grouped_pre_ex_close` fetches a whole-market payload per session (`population_ladder_monitor.py:1609-1620`). Two dividends in one window is two such calls. Everything is cached: FOUND forever, NONE-FOUND for `NONE_FOUND_CACHE_TTL_DAYS = 14` (`corporate_actions.py:95`, applied `:258`), keyed `TICKER:start:end` (`:229`). So a repeat run of the same pick inside 14 days makes no vendor call for this question at all.

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

File: `<out>/<stem>.bars.provenance.json`, beside `<stem>.bars.json`. Written on both paths. Every epoch below is the real one for the §6.9 worked example, computed from the calendar (**M7**); the `close_bar_drop.dropped` entry shown is the real CRSR quadruple from **M11**, put here because the store holds no SMMT bars (§5.6 GAP).

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
    "required_end_t": 1791230400000,
    "required_end_formula": "session_close_utc(advance_trading_sessions(entry_deadline_session, 1, XNAS), XNAS) — the grid is half-open, so the ladder's expiry is first observable on the next session",
    "entry_deadline_t": 1790971200000,
    "last_bar_t": 1791316740000,
    "margin_sessions_requested": 2,
    "margin_sessions_served": 2,
    "last_servable_session": "2026-10-08"
  },
  "session_filter": {
    "predicate": "open_ms <= t < close_ms",
    "calendar": "exchange_calendars via alphalens_pipeline.market.calendar",
    "exchange": "XNAS",
    "sessions": [
      {"session": "2026-09-23", "open_t": 1790170200000, "close_t": 1790193600000, "half_day": false},
      {"session": "2026-09-24", "open_t": 1790256600000, "close_t": 1790280000000, "half_day": false}
    ]
  },
  "close_bar_drop": {
    "applied": true,
    "rule": "a bar stamped exactly at a session close is not part of that session",
    "why": "the minute stamped at the close spans close..close+59s; the session ends at the close",
    "range_narrowed": 1,
    "dropped": [
      {"t": 1790971200000, "session": "2026-10-02",
       "bar": {"open": 10.89, "high": 12.8, "low": 10.61, "close": 12.79},
       "outside_rest_of_session": true}
    ]
  },
  "counts": {"fetched": 4436, "kept": 3900,
             "dropped_at_close": 10, "dropped_off_grid": 526,
             "sessions": 10},
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

`sessions` and `dropped` are abbreviated here; the real file lists all 10 of each, because those two lists are precisely the facts the lost SMMT file could not answer. The `counts` are the shape — `fetched` and `dropped_off_grid` are derived from the measured 1.1374 ratio (**M1**) against the 3 900-bar ceiling rather than from a run — and the real ones are written per run.

`counts.dropped_at_close` and `len(close_bar_drop.dropped)` are the same number by construction and §12 class F pins that they agree. There is deliberately no second spelling of it inside `close_bar_drop`.

**What a reader can verify with it, without the tool:**

* that the number came from these bars: `shasum -a 256 <bars_path>` must equal `sha256`;
* that no bar lay outside trading hours: read `session_filter.sessions` and check each `t` against the stated predicate, which is spelled out including its exclusiveness at the close;
* **that a filtered series is a filtered series**: `close_bar_drop.applied` and `granularity.modal_spacing_ms` are both in the file, so a reader never has to re-derive either from the bars — and `granularity` is measured, so it cannot assert `1m` about an hourly file;
* **what the filter removed, and how to undo it**: every dropped close-stamped bar's `t`, session and full `{open, high, low, close}` is listed, so the SESSION series - the bars inside the grid, plus the one per session the grid excludes - is the written file plus this list. It is NOT the raw vendor payload: the sidecar gives `dropped_off_grid` as a bare count, and off-grid bars outnumber close-stamped ones by a wide margin. Measured over 300 store files: 5 861 close-stamped against 190 056 other off-grid bars, 32 times as many and 8.55% of the whole payload. The raw series is re-derivable only from the cache (section 10), which is machine-local and is therefore not one of the four auditable files. `range_narrowed` is the count of dropped bars whose high or low lay outside the rest of their session's range, which is the discriminating number — it runs at 1.161% of sessions (**M10**), where the number of close-stamped bars is near-constant at one per session (91.34% of real pairs carry one, **M1**) and therefore carries no signal on its own;
* that the basis was raw: `adjusted: false`, with the vendor and endpoint named;
* that the window could observe the ladder's expiry: `window.entry_deadline_t` against `window.last_bar_t`, in one object, with `required_end_t` saying which session that implied;
* whether the margin was shortened: `margin_sessions_requested` against `margin_sessions_served`, with `last_servable_session` saying why;
* on the `--bars` path, what the operator actually supplied: `source_path` and `source_sha256`, which differ from `sha256` exactly when something was dropped.

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

The key is `(ticker, from_t, to_t, granularity, adjusted)` — D3's key, spelled into the file name so a human can read it. **The cache holds the RAW vendor payload, before translation and before the grid filter.** That is deliberate: a cached file must stay a faithful record of what the vendor sent, so that a later change to the grid re-derives rather than inherits — the same separation `_filter_bars_to_rth` states for its own cache, "The cache itself keeps every fetched bar (a faithful raw record); this filter applies at replay time only" (`population_ladder_monitor.py:753-754`). It also follows the standing rule for metered vendors (CLAUDE.md on the iVolatility cache: persist raw API responses before processing, never re-fetch on retry). `granularity` is `"1m"` in the key — the vendor request's granularity, which is a request parameter and not a measurement — and `adjusted` is the constant `raw`; both stay in the key so that a later day-bar variant can never read a minute file as if it were its own.

That separation is what makes the close-bar decision reversible in practice and not only on paper: the 15 677-bar difference between the two grids lives in the cache and in the sidecar, so reopening §5.3 costs a re-filter and not a re-fetch.

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

Exit 7 is the reserved transient status in `~/Developer/CLAUDE.md`'s CLI section; the script has no 7 today (`replay_from_pick.py:429-430`) and gains it here. Without it, a Polygon timeout falls into the existing `except (OSError, ValueError)` at `:518` and reports 1, "the pick cannot be replayed", which is a false statement about the pick.

---

## 11. Refusals

All refusals print nothing on stdout. In `--format human` (the default) the message goes to stderr as prose, prefixed with the code: `replay_from_pick: <code>: <message>`. In `--format json` the command prints **one JSON object as the last line of stderr**, the rule the broker CLI already follows:

```json
{"schema": "alphalens.research.replay_from_pick.error/v1",
 "code": "bars_off_grid", "reason": "after_close",
 "message": "...", "retryable": false,
 "details": {...}, "suggestions": [{"argv": [...]}]}
```

The **four** existing print-and-return sites (`replay_from_pick.py:491-496`, `:499-501`, `:504-509`, `:518-520`) are routed through one `_fail(code, reason, message, status, **details)` helper and gain codes (`usage`, `report_unreadable`, `pick_not_in_report`, `not_comparable`, `entry_trail_unknown`), so the envelope is uniform. Their human wording is unchanged apart from the code prefix. The existing tests assert with `assertIn` at `:584`, `:585`, `:602`, `:620`, `:635`, `:652`, `:665` and `:685`, and with one `assertNotIn("--entry-trail-bps", err)` at `:666` — so the prefix changes no meaning, and the new messages must keep `--entry-trail-bps` out of the not-comparable refusal. Argparse's own errors keep their own text and status 2.

New codes, one per failure MODE with a closed `reason` vocabulary — the shape `broker arm` uses, because fifteen top-level codes for six modes is a worse table to read:

| code | reasons | exit | retryable | details |
|---|---|---|---|---|
| `bars_off_grid` (`--bars` only) | `after_close`, `before_open`, `not_a_session` | 1 | no | `count`, `offenders` (a list of `{t, session, session_open_t, session_close_t, reason}`), `exchange`, `path` |
| `bars_window_unservable` | `deadline_not_yet_servable`, `vendor_served_short` | 7 | **yes** | `pick`, `exchange`, `entry_deadline_t`, `entry_deadline_session`, `expiry_observable_session`, `required_end_t`, `last_servable_session`, `last_servable_end_t`, `servable_from_date`, `last_bar_t`, `now` |
| `bars_unusable` | `empty_window`, `walk_start_uncovered` | 1 | no | `window`, `walk_start`, `first_t`, `bars`, `dropped_at_close` |
| `bars_wrong_granularity` | `not_one_minute` | 1 | no | `modal_spacing_ms`, `bars`, `sessions`, `path` |
| `bars_fetch_failed` | `vendor_error` | 7 | **yes** | `ticker`, `window`, `vendor_message` |
| `bars_corporate_action` | `split`, `special_dividend` | 1 | no | `detail`, `lookup_window` |
| `corporate_actions_unanswered` | `lookup_failed` | 7 | **yes** | `ticker`, `lookup_window`, `vendor_message` |
| `bars_unreadable` (`--bars` only) | `not_json`, `not_a_list`, `bar_not_object`, `t_missing`, `t_not_int` | 2 | no | `path`, `index` |
| `exit_time_unknown` | `not_journaled`, `non_finite` | 1 | no | `pick_key`, `null_reason`, `path` |

Four notes on the shape of this table:

* **`bars_off_grid` is for a bar the grid cannot PLACE, never for one it excludes.** Its three reasons are "strictly after a session's close", "before a session's open" and "on a date that is not a session on this MIC". A bar at `t == close_ms` matches none of them: it is placed on that session and then excluded by the half-open predicate, counted in `counts.dropped_at_close` and enumerated in the sidecar. **M12** is why that distinction has to be in the code and not only in the prose: all three of this repository's RTH helpers produce close-stamped bars, on 40 of 40 real files, so a `bars_off_grid` that fired on them would refuse the files the override exists to accept and teach the operator to drop `--bars`.
* **There is therefore no refusal anywhere for a close-stamped bar.** The exclusion is a stated construction recorded in the sidecar, not a rule the input must already satisfy.
* `bars_unreadable` is exit 2 because the operator named a path that is not the published shape: a usage error, not a statement about the pick. The script checks only `t`'s presence and type; price coherence, ordering and the closed key set stay the engine's job (§14).
* `details.offenders` is a list, not one triple, because the incident had two off-grid bars in two different sessions; a single `session` field would have described the first bar's session while `last_t` pointed at a bar from another.

**The incident's refusal.** Message template, then the incident's own instants:

> `replay_from_pick: bars_off_grid: /tmp/smmt.bars.json carries <N> bars the XNAS session grid cannot place. The first is at 2026-09-28T21:30:00.000Z, 1h30m after that session's close 2026-09-28T20:00:00.000Z (session 2026-09-28, 13:30:00.000Z-20:00:00.000Z). A bar stamped exactly at a close is dropped and listed in the sidecar; this one is not that, so it is refused. Drop the flag to fetch the window instead.`

`details` for that refusal, with the real epochs:

```json
{"reason": "after_close", "count": 1,
 "offenders": [{"t": 1790631000000, "reason": "after_close", "session": "2026-09-28",
                "session_open_t": 1790602200000, "session_close_t": 1790625600000}],
 "exchange": "XNAS", "path": "/tmp/smmt.bars.json"}
```

`<N>` is the count the file held; for the lost SMMT file it is unknowable, and that is the point of §9. Note what is **not** in this refusal: the bar at `2026-10-02T20:00:00.000Z` (epoch `1790971200000`). It is that session's closing minute, §5.1 excludes it and §9 lists it — and §5.6's GAP says what that does and does not settle about the incident.

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
| `test_the_cache_holds_the_raw_payload_not_the_filtered_series` | the cached JSON carries `o/h/l/c/v` and the close-stamped bar; the written bars file carries neither | caching after the grid filter, which freezes the close-bar decision into the cache and makes §5.3 un-reopenable without a re-fetch |

### Class B — `TheSessionGridIsTheVenueSOwnTest` (fake `BarFetch`; one test per legitimate input of §5.7)

| method | asserts | production change that makes it fail |
|---|---|---|
| `test_a_bar_stamped_at_the_session_open_is_kept` | the opening bar survives | `>` at the open, after which every run fails `walk_start_uncovered` |
| `test_the_grid_is_the_venue_s_own_hours_not_a_390_minute_assumption` | on an XWAR copy of the document, a bar at 14:30Z (inside XWAR, outside open+390) is kept | `open_ms + 390 * 60_000`, that is, composing `_rth_window_ms` |
| `test_a_venue_whose_real_close_is_before_open_plus_390_drops_nothing_extra` | on an XSHG copy, a bar at 07:30Z (outside XSHG's 07:00Z close, inside open+390) is **dropped** | the same 390-minute composition, in its LONG direction (**M2**: XSHG +60, XTAI +120) |
| `test_a_half_day_grid_ends_at_the_early_close` | over XNAS 2026-11-27, 17:59:00Z is kept and 18:00:00Z and 18:01:00Z are not | a constant full-session close |
| `test_a_window_spanning_the_dst_change_grids_each_session_on_its_own_hours` | over XNAS 2026-10-30 and 2026-11-02, a 13:30Z bar on 11-02 is dropped and a 20:30Z bar kept | one open/close pair reused for the whole window |
| `test_a_bar_inside_no_session_interval_is_dropped` | a Saturday bar (the real `KRP` 2026-08-15 shape, 2 pairs in the production cache, **M1**) does not survive | bucketing by UTC date without asking the calendar |
| `test_a_bar_in_the_first_hour_of_an_XASX_session_is_KEPT` | a bar at 2026-10-11T23:30:00Z survives on XASX, although 2026-10-11 is not itself a trading day | bucketing by UTC date, which drops the first hour of every XASX session |
| `test_a_sparse_session_is_not_refused` | 13 bars for one session still produce a run (status 0) | any completeness or minimum-count check |
| `test_a_sparse_session_whose_commonest_gap_is_two_minutes_is_not_refused` | a session whose modal gap is 120 000 ms inside a window whose whole-series modal gap is 60 000 ms runs | phrasing the granularity gate per session, which refuses 26 real pairs (**M13**) |
| `test_a_one_bar_series_at_walk_start_is_not_refused` | a single bar at `walk_start` produces a run | a `len(bars) > 1` guard, or a granularity gate that does not exempt a series with no spacing |
| `test_a_daily_series_stamped_at_each_open_is_refused_by_granularity` | one bar per session at that session's own open over 3 sessions: the grid keeps all three (**M13**: 10 of 10) and `bars_wrong_granularity/not_one_minute` refuses, `details.modal_spacing_ms` 86 400 000 | relying on the grid to refuse daily bars, which this shape shows it does not |
| `test_a_daily_series_stamped_at_each_close_is_refused_as_empty_not_as_coarse` | one bar per session at that session's close — what `grouped_daily_history` holds (**M8**: 492 of 497 files) — refuses with `bars_unusable/empty_window` and NOT with `not_one_minute` | reporting `not_one_minute` for an empty kept series, which tells the operator the wrong thing to fix, or reading the modal spacing of a series with no bars |
| `test_an_hourly_series_is_refused_too` | 7 bars per session over 10 sessions: status 1, `not_one_minute`, `modal_spacing_ms` 3 600 000 | a one-bar-per-session rule, which is silent on hourly, 30-minute and 5-minute series (**M13**) |

### Class C — `TheCloseStampedBarIsNotPartOfTheSessionTest` (fake `BarFetch`)

This class is the whole of D4. Each row names the production change that reintroduces the defect it covers.

| method | asserts | production change that makes it fail |
|---|---|---|
| `test_the_close_stamped_bar_is_dropped` | with bars at 19:59:00Z and 20:00:00Z, the written series carries the first and not the second, and `counts.dropped_at_close` equals the session count | `<=` at the close, which is the whole-bar treatment: 182 of 15 677 sessions carrying a range the session never traded, worst 1457.5 bps (**M10/M14**) |
| `test_a_post_market_excursion_in_that_minute_cannot_fire_a_take_profit` | the fake returns the real CRSR quadruple `{o 10.89, h 12.80, l 10.61, c 12.79}` at the close with a take-profit at 11.95, above the session's own high of 11.1717; the written series does not reach 11.95 and the engine's outcome is `open`, not `closed_tp` | keeping the bar, which flips this exact case on the real engine (**M11**) |
| `test_a_post_market_excursion_in_that_minute_cannot_fire_a_resting_stop` | the mirror case, a low excursion below the disaster stop: no `stop` event | keeping the bar, which fills a resting broker stop at a post-market print — flatly wrong per `walk.py:497-498`, `:512`; 62 of 15 677 sessions, worst 301.6 bps (**M10**) |
| `test_no_written_bar_carries_a_price_the_vendor_did_not_send` | every written bar's four prices appear in the fake's own payload for that `t` | any rewrite of the close bar's content — the retired reduction, which also makes a level between the raw low and the open unreachable (**M15**) |
| `test_only_a_bar_at_the_session_close_is_dropped` | the bar at `close − 60 000` survives with its four distinct prices | dropping by position (the last bar of the series) instead of by `t == close_ms` |
| `test_the_last_bar_of_the_series_is_not_special` | a window whose final session contributes no close-stamped bar keeps its final bar, and `counts.dropped_at_close` is one short of the session count | the same positional bug, in the direction that silently deletes a real intraday bar |
| `test_dropping_it_never_empties_a_session` | over a window of sparse sessions each contributing 13 bars plus a close-stamped one, every session still contributes bars | no production change — a pin on **M1** (0 of 17 163 real pairs fall below two kept bars); it turns red if the drop is ever applied before the membership filter |
| `test_the_dropped_bar_is_recoverable_from_the_sidecar` | for each dropped bar the sidecar carries its `t`, its session and the exact four prices the fake sent, and `len(close_bar_drop.dropped) == counts.dropped_at_close` | dropping silently, which is the SMMT loss in a new costume, or publishing the count without the list |

### Class D — `TheWindowMustReachTheLadderSExpiryTest` (fake `BarFetch`, injected `now`)

| method | asserts | production change that makes it fail |
|---|---|---|
| `test_the_required_end_is_the_session_after_the_deadline_session` | for `SYM:2026-09-30` the sidecar's `window.required_end_t` is `1791835200000` (2026-10-12T20:00Z), not the deadline `1791576000000` | `required_end = entry_deadline`, under which the window's last bar is `deadline − 60 000` and `entry_expired` never fires (**M6**, row 1) |
| `test_a_never_filled_pick_publishes_entry_expired_rather_than_an_empty_trace` | the engine's `trace` over the written series carries `entry_expired`; its `t` is the next session's open and the test says so in its own name | the same change; an empty trace is also what a truncated run publishes (`walk.py:131-134`) |
| `test_a_late_entry_expired_cannot_fill_a_rung` | with a tape falling through every entry limit from the deadline instant onward, `filled_fraction` is 0 and no `entry_filled` event appears | moving `_expire` after `_fill_entries` in the walk — not a change this design makes, which is why this test is a pin on `walk.py:871` against `:884` (**M6**, row 4) |
| `test_a_deadline_the_vendor_cannot_serve_yet_is_refused_naming_both_sessions` | `SYM:2026-09-30` at `now=2026-10-09T08:14Z`: status 7, `bars_window_unservable/deadline_not_yet_servable`, message names `2026-10-09`, `2026-10-12` and `2026-10-13`, no bars file, **no vendor call** | a `min()` clamp, which runs a window ending `2026-10-08T20:00Z` and publishes `no_fill` with an empty trace (**M6**) |
| `test_the_servable_cap_does_not_move_after_the_close` | the same pick at `now=2026-10-09T21:00Z` still refuses; `last_servable_session` is `2026-10-08` | computing the cap from `session_not_closed`, which returns `2026-10-09` there (**M5**) |
| `test_a_pick_whose_expiry_is_servable_runs_with_whatever_margin_fits` | `DAVE:2026-09-25` at the same `now`: status 0, `margin_sessions_requested` 2 and `margin_sessions_served` 2 | refusing on a short margin, which would refuse more than the 6 of 12 the rule refuses (**M7**) |
| `test_a_series_the_vendor_served_short_is_refused_after_the_fetch` | the fake returns bars whose last `t` is before `entry_deadline`; status 7, reason `vendor_served_short`, `details.last_bar_t` names the last bar | trusting the pre-fetch cap, which depends on a vendor day boundary this design does not know (**GAP**, §6.5) |
| `test_a_supplied_file_that_stops_before_the_deadline_is_refused` | a grid-clean file ending at `2026-10-08T20:00Z` for `SYM:2026-09-30`; status 7, `vendor_served_short` | applying the right-edge check on the fetch path only |
| `test_the_right_edge_check_reads_the_series_not_the_window_end` | a window whose stated end is past the deadline but whose last KEPT bar is not reaches the same refusal | phrasing the check on `window_end`, which is the one line of §6 the grid change would otherwise have broken |

### Class E — `ASuppliedBarsFileIsFilteredNotTrustedTest` (no `BarFetch`, no network)

| method | asserts | production change that makes it fail |
|---|---|---|
| `test_an_unplaceable_bar_in_a_supplied_file_is_refused_naming_its_session_boundary` | the incident file rebuilt with its bar at `2026-09-28T21:30Z`: status 1, `bars_off_grid/after_close`, stderr names both that instant and `2026-09-28T20:00:00.000Z` | today's behaviour, which never opens the file, gives status 0 |
| `test_a_file_built_with_this_repo_s_own_rth_filter_is_accepted_and_filtered` | a file whose every session ends with its close-stamped bar — what `_rth_window_ms`, `_session_date_for_ts` and `_filter_bars_to_rth` all produce (**M12**: 40 of 40 real files, 805 bars) — runs with status 0, `counts.dropped_at_close` equal to the session count and `counts.dropped_off_grid` 0 | refusing the close-stamped bar, which refuses every file the repository itself produces and sends the operator to surrender the override |
| `test_a_close_stamped_bar_and_an_after_close_bar_in_one_file_are_treated_differently` | one file carrying both: the run refuses `bars_off_grid` and names only the after-close bar in `offenders` | one membership predicate for both, in either direction — refusing the first or passing the second |
| `test_the_supplied_file_is_never_modified` | the supplied path's bytes are identical before and after a successful run | filtering in place, which rewrites an operator's file |
| `test_the_replay_argv_points_at_the_written_series_not_the_supplied_file` | `replay_argv`'s `--bars` value is `<stem>.bars.json` | pointing at `args.bars`, which replays the UNFILTERED series while the sidecar claims a filter |
| `test_two_unplaceable_bars_in_two_sessions_are_both_named_in_the_json` | `details.offenders` carries two entries with two different `session` values | one `session`/`session_open_t`/`session_close_t` triple for the whole refusal |
| `test_a_supplied_file_that_is_not_the_published_shape_exits_2` | not an array, and a `t` that is a string: status 2, `bars_unreadable` | parsing with no shape check, which raises a traceback |
| `test_a_supplied_file_gets_a_sidecar_with_both_hashes` | `source_sha256` equals `hashlib.sha256(supplied.read_bytes())` and `sha256` equals the same of the written file; the two differ whenever `counts.dropped_at_close > 0` | one hash, which cannot distinguish the input from the walked series |

### Class F — `TheProvenanceSidecarMakesAPublishedNumberRecheckableTest` (fake `BarFetch`)

| method | asserts | production change that makes it fail |
|---|---|---|
| `test_the_sidecar_carries_the_session_filter_the_run_used` | the predicate string `open_ms <= t < close_ms`, the exchange, and every session's `open_t`/`close_t`/`half_day` | recording only the exchange, which is the SMMT loss again |
| `test_the_predicate_string_matches_the_code_that_filtered` | the sidecar's predicate string is built from the same module constant the filter uses, not written twice | two spellings of one rule, which is the defect the engine's own `walked` docstring warns about (`envelope.py:151-156`) |
| `test_the_drop_count_and_the_drop_list_cannot_disagree` | `counts.dropped_at_close == len(close_bar_drop["dropped"])` | writing the count from one traversal and the list from another |
| `test_the_narrowed_count_is_the_discriminating_one` | on a series where exactly one dropped bar's range left its session's range, `range_narrowed` is 1 while `counts.dropped_at_close` equals the session count | publishing only `dropped_at_close`, which is near-constant (**M1**: one per session in 91.34% of pairs) and so carries no signal |
| `test_the_granularity_is_measured_not_asserted` | the sidecar's `modal_spacing_ms` is computed from the written series; on an hourly supplied file the run refuses rather than writing `"1m"` | a literal `"1m"`, which would assert one-minute bars about an hourly file |
| `test_the_sidecar_states_the_deadline_beside_the_last_bar` | `window.entry_deadline_t` equals `config["entry_deadline"]["value"]` and `window.last_bar_t >= entry_deadline_t` | writing the window without the quantity the series has to reach |
| `test_a_shortened_margin_is_stated_not_inferred` | for a record whose servable end truncates the margin, `margin_sessions_requested` 2 against a smaller `margin_sessions_served`, with `last_servable_session` set | one `clamped: true` flag, which a reader cannot join to `entry_deadline` |
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
| `:422` | `--bars PATH  bars to put in the suggested command` | an offline override, filtered onto the venue's half-open session grid and refused only for a bar the grid cannot place |
| `:418-423` | OPTIONS block | add `--bar-cache DIR` |
| `:425-427` | OUTPUT block | add `bars_path`, `bars_provenance_path` and the `bars` block |
| `:429-430` | `0 ok, 1 ..., 2 usage, 4 ...` | add `7 a transient vendor failure or a window the vendor cannot serve yet; re-run` |
| `:432-434` | EXAMPLES | the example no longer needs a hand-built file |
| `:481-487` | parser | add `--bar-cache` |
| `:473` | `def main(argv)` | add keyword-only `bar_fetch`, `actions_lookup`, `now`, each with a production default (§12 class I) |
| `:491-496`, `:499-501`, `:504-509`, `:518-520` | four refusal sites | route through one `_fail(code, reason, message, status, **details)` helper that renders prose or the JSON envelope |
| `:531-556` | the `answer` dict | add `bars_path`, `bars_provenance_path` and `bars` as **top-level** keys |
| `:545-555` | `replay_argv` | always the written `<stem>.bars.json`, on both paths; the `<BARS.json>` placeholder goes |
| `:50` | `SCHEMA = "alphalens.research.replay_from_pick/v1"` | unchanged: added keys are optional additions inside a major version |
| `:31-34` imports | `advance_trading_sessions`, `session_open_utc` | add `session_close_utc`, `session_on_or_after`, `previous_trading_day`, `is_trading_day` |

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

Measured at HEAD: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.test_replay_from_pick` → `Ran 32 tests in 0.272s / OK`. Those three assertions are the ones that turn red on the first commit of this design.

### `apps/alphalens-research/tests/test_replay_from_pick_seams.py` — new

The AST gate of §12 class I. A separate file because it walks the source rather than running the command, which is the shape `tests/brokers/test_broker_cli_places_nothing.py` already established.

### `apps/alphalens-pipeline/alphalens_pipeline/feedback/bar_window.py`

`:47` and `:61` — rename `_default_bar_fetch` to `default_bar_fetch`. Zero external importers, verified by grep (§4).

### `CLAUDE.md`

* the "Replaying a REAL pick" recipe gains nothing new in the common case (the command already runs as printed), but the surrounding prose must say that the bars come with it.
* "The command reads the report, not the broker, so it runs offline." Becomes: it reads the report and the price vendor; it runs without a bar fetch only with `--bars`, and without any vendor call only when the corporate-action answer for the window is already cached.
* the "derived / imported / read — never written as a literal" rule gains the bar window: the grid and the ticker from `instrument.{mic, ticker}`, the window's required end from `config.entry_deadline` plus one session of the venue's calendar, its anchor from the record's own `exits[-1].venue_time`, the padding constants imported from `corporate_actions`, and the series' granularity MEASURED from the series rather than stated.
* a sixth rule: **the session grid is HALF-OPEN at the close, inclusive at the open, and read from the calendar per session — never `open + 390 minutes`.** The span discriminators are XWAR (90 minutes short) and XSHG (60 long); the bound discriminator is the bar stamped at the close.
* a seventh rule: **the bar stamped at the venue close is NOT part of the session, and on a supplied file it is FILTERED, never refused.** The session ends at the close, so that minute's high and low can carry post-market trading: against the official daily `[low, high]` it leaves the real range on roughly 1% of sessions where dropping it leaves 0.05%, the worst case falls from 1457.5 bps to 34.7, and keeping it flipped a real CRSR outcome from `open` to `closed_tp`. Refusing it instead would refuse files all three of this repo's own RTH helpers produce (40 of 40 real files). Every dropped bar is listed in the sidecar with the quadruple it carried, so the raw series rebuilds exactly. The cost is a horizon mark one minute early, median 0.034% — accepted, and visible in `walked.to_t`.
* an eighth rule: **the window's right edge is a refusal, not a clamp, and the requirement is on the SERIES: it must carry a bar at or after `entry_deadline`.** Because the grid is half-open, that means the window must reach the session AFTER the deadline session, where `_expire` fires late by one overnight gap and cannot fill anything. A `no_fill` with an empty `trace` is the published signature of a window that could not observe the expiry.

### `apps/intent-replay/README.md`

**No change.** It specifies the bar file's shape (`:69-86`) and the refusal table (`:499-510`) and says nothing about where bars come from; `grep -i "polygon\|vendor\|replay_from_pick"` returns nothing relevant. Adding provenance there would make a client-agnostic leaf describe one client. Its `walked` section (`:298-309`) already documents the reader-side signal §6.8 relies on.

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
* **No change to `apps/intent-replay` at all, and the close-bar rule is why that is worth saying.** The model error §5.3 names — a resting broker stop filled by a post-market print — could also have been fixed inside the walk, by letting the resting legs see only the bar's open while the polled ladder saw its full range. That is the distinction `walk.py` already draws in prose (`:497-498` against `:707-710`), so the idea is not foreign to the engine. It is rejected because it makes one bar mean two things inside one walk, which the published `Bar` has no shape for, and because it puts a venue-session concept into a leaf whose first spec sentence is that it knows no calendar. Excluding the minute achieves the same correction in the client, where the calendar already lives.
* **No change to the published run configuration.** The eight keys stay the eight keys; `RunConfig` refuses an unknown one, and bars provenance is not a run value the engine reads. It lives at the top level of the command's answer and in the sidecar.
* **No bump of `SCHEMA`.** Added keys are optional additions inside a major version, which is what the project's CLI contract rule permits.
* **No shared refactor of the `get_agg_range` consumers or the four `_default_bar_fetch` copies.** They convert to four different shapes for four different purposes (a 4-tuple, a daily fold, a chart candle, highs and lows only). A shared converter would have one caller that wants the replay shape. Extract on the second use; today's change is the first. The one exception is the rename in §4, which has zero importers.
* **No fix of the `_rth_window_*` 390-minute span, and no change to the three RTH helpers' close bound.** This design disagrees with all three at the close (§4 Block 2) and does not edit them: `_filter_bars_to_rth` feeds already-published `/edge` ladder outcomes, so changing its kept set is a backfill decision (§16 Q1), not a side effect of a laboratory command. The span is a separate latent defect for any non-US venue, in both directions (**M2**: XWAR 90 and XETR 120 minutes short, XSHG 60 and XTAI 120 long). Both are issues, not silent edits in this PR.
* **No break-venue arm in the predicate.** `session_open_utc` / `session_close_utc` return one interval per session, so on a break venue the grid admits the lunch break: 60 non-trading minutes per XTKS session (02:30-03:30Z) and per XHKG session (04:00-05:00Z) on 2026-10-02, and 90 per XSHG session (03:30-05:00Z) on 2026-10-12 (**M2**). The exposure is latent rather than live: `get_agg_range` runs against `/v2/aggs/ticker/{ticker}` on a US-equities plan, and no break venue appears in the fixture (**M3**). A break-aware bound would need a calendar helper that does not exist and has no second consumer. **GAP:** whether Polygon would serve an XTKS ticker at all on this plan is not measured, so the practical reach of this residual is unknown.
* **No `adjusted=true` option.** The document's limits, stops and take-profits are prices the author wrote from live unadjusted quotes; adjusted bars would replay the ladder against a basis the trade never saw.
* **No daily-bar fallback, and the refusal comes from two different gates.** A single low per session cannot reproduce a trailing stop (D2). A daily series stamped at each session's CLOSE refuses as `bars_unusable/empty_window`, because the grid keeps none of it (**M8**: 0 of 497 real daily files contribute a bar); a daily series stamped at each session's OPEN passes the grid intact and refuses as `bars_wrong_granularity/not_one_minute` (**M13**). Both tests exist, and conflating them would leave the second shape unguarded.
* **No re-implementation of the engine's bar validation.** Ordering, price coherence and the closed key set are refused by `bars.py` with published codes. The script checks only what the engine cannot know: the session grid, the granularity, and whether the kept series covers `walk_start` at one end and `entry_deadline` at the other.
* **No automatic fetch for a pick the report excludes.** `_assert_comparable` (`:322-333`) already runs first and keeps running first. Spending metered quota on a record the report itself says cannot be compared is wasted money, and the refusal is more useful than the bars.
* **No new exit status per refusal code.** Statuses stay coarse (0, 1, 2, 4, 7), with the detail in `code`, the same rule the broker CLI follows.
* **No alerting, no Telegram, no metrics.** This is a hand-run laboratory command.

### The one thing this design DOES do that it would rather not: it discards a bar the vendor sent

The retired reduction was rejected partly on the ground that it wrote a price no vendor had sent. This rule does not do that — §12 class C pins it — but it does discard one minute per session, and that is a construction too. This command exists because a hand-built bar file carried an unrecorded construction and produced a number that cannot be audited. A design whose own answer is "apply a different unrecorded construction" would be the incident wearing a new hat. So the objection has to be answered, not dropped.

Three things pay for it, and all three are load-bearing.

**1. The construction is stated, enumerated and invertible.** The sidecar carries `close_bar_drop` with the rule, the count of dropped bars whose raw range left their session, and — for every dropped bar — its `t`, its session and the full `{open, high, low, close}` it carried (§9). A reader holding the sidecar and the bars file can reconstruct the SESSION series exactly - every bar the grid placed, plus the one per session it excluded. The raw vendor payload is not reconstructible from those two files, because `dropped_off_grid` is a bare count covering roughly 32 times as many bars (8.55% of the payload, measured over 300 store files); it is re-derivable from the cache alone. That is strictly more than the raw vendor file would have given them, because the raw file does not say which bar is a close bar or which calendar decided. And it is the difference the SMMT incident turns on: that file's own session filter and close-bar treatment cannot be reconstructed **at all**, which is why its retracted 1.05% cannot be audited even today. The cache keeps the raw payload too (§10), so a later change to the rule re-derives from the vendor's bytes rather than from a filtered series.

**2. What is discarded is, except for one instant, outside the session.** The session ends AT the close. The minute stamped there holds exactly one thing the session owns — the closing print, at its first instant — and after that up to 59 seconds of trading in a market the session no longer includes. This is the honest statement of the cost, and §5.4 measures it rather than waving at it: the closing print is what the horizon mark loses, median 0.034% of price, exact on 23.93% of sessions against 93.50% for the discarded bar's own open.

**3. The alternatives' prices are measured, not asserted, and both are larger.** Keeping the whole bar leaves the official daily `[low, high]` on roughly 1% of sessions against 0.05% for dropping, with a worst excursion of 1457.5 bps against 34.7 (**M14**, invariant across five adjustment filters); it exposes 149 of 881 real windows and 25 of them to an excursion above 2% (**M10**); it feeds a resting broker stop with post-market prints in flat contradiction of `walk.py:497-498`; and on real CRSR bars through the real engine it turns an `open` position into a `closed_tp` worth +0.9141 R, a swing of 1.047 R, while inflating `mfe` from 0.5156 to 1.5781 (**M11**). Rewriting the bar to one price halves the first of those and keeps a 202.0 bps worst case, rests on a premise that is false on the one session measured in detail (10.89 written against a 10.61 official close, 264 bps), and makes declared levels between a bar's low and its open unreachable (**M15**).

A stated construction that can be undone, costing a mark, is a smaller cost than a range that fires exits the session never permitted.

---

## 15. What the earlier drafts got wrong

**Three** premises about one bar, each plausible, each refuted by running it. They are recorded because the next person to design this will reach for all three, and because the third one's refutation carries the most reusable lesson in this document.

### 15.1 The half-open close argued from the wrong fact — `open <= t < close`, because the close bar is "post-market"

The first draft reached the rule this memo adopts, by an argument that does not hold. It said that because the vendor stamps a bar at its START, a bar stamped at the close is "the first post-market minute", and that `walk.py::_expire`'s own docstring makes "a bar stamped exactly at the deadline already outside the ladder's life". The stamping fact is correct and the inference is not: the minute that begins at the close *begins with the closing print*. Measured, that print is recoverable from the bar's open in **93.50%** of real pairs at a median deviation of 0.0000% (**M4**), so the minute is not post-market — it is the closing auction followed by up to 59 seconds of post-market.

The draft was also wrong about `_expire` in the operational direction. **M6**, the real engine: under half-open, a window ending at the deadline session never fires `entry_expired` at all. The draft's rule therefore produced, for a never-filled pick, `no_fill` with an empty `trace` — the same thing a run that simply ran out of tape produces. §6.3 is the correct handling: the requirement moves onto the SERIES and the window extends by one session, at a measured cost of 3 extra refusals in 12 fixture records.

That draft also cited `scripts/diagnose_exit_geometry.py:63` and `scripts/backfill_breakeven_whatif.py:61` as a half-open precedent that "reproduces the stored `realized_r` exactly, 42 of 42 rows". **That check had no power to refute.** The close-stamped bar changes a session's range in 1.161% of sessions (**M10**), so over 42 rows of roughly 5 sessions each the expected number of discriminating sessions is about 2.4, and a discriminating session only moves `realized_r` if the extra range crosses a level. Those two scripts also hard-code EDT. A published figure of "17.5 hours late" in that draft was wrong in the same family of way: 17.5 hours is the close-to-next-open gap when the next session is the next calendar day, and the case its own table measured spans a weekend — 65.5 hours (**M6**). Both numbers are real; 17.5 h is the modal gap (197 of 250 in 2026) and 65.5 h is what that case measured (**M5**).

**So the first draft reached the right rule for reasons that were partly false, and shipped a window rule that broke on it.** A correct conclusion from a refuted premise is not evidence, and it is why §5.3 re-derives the bound from three measurements the draft never ran.

### 15.2 The `min()` clamp on the window end

The same draft clamped `window_end` to the close of the last closed session and defended it with "a window that still runs out is visible: the engine publishes the terminal state `open`". That holds only for runs that **filled**, and the never-filled branch is the majority: 8 of the 12 comparable fixture records (**M3**). **M6**, the real engine on `SYM:2026-09-30`: a window one session short publishes `outcome: no_fill` with `trace: []` — and `walk.py:131-134` returns `no_fill` whenever nothing filled, so a truncated run and a genuinely expired ladder say the same word. Six of the 12 comparable records ran such a window under the draft's own rule (**M7**).

The clamp also failed its own purpose after the close: at `now = 2026-10-09T21:00Z` it moved the cap to `2026-10-09`, the current calendar day, while the vendor restriction it existed for is calendar-day based (`polygon_client.py:236-239` — the draft cited `:243-247`). **M5** shows the calendar-day rule holding at `2026-10-08` across 08:14Z, 12:00Z and 21:00Z.

And the facility that already existed for detecting the damage was never cited: `walked_from_t` / `walked_to_t` / `walked_bars`, added by `bd724cb2` (#1746) at `walk.py:120-122` and published as the envelope's fifth key (`envelope.py:162-164`, `README.md:223`).

### 15.3 Keeping the close-stamped bar, whole and then rewritten — and the metric that could not rank the alternative

The second draft kept the bar's full OHLC quadruple, declaring the cost (1.161% of sessions carry a post-close extreme) and rejecting any change because "it would write a bar the vendor never sent". The third draft accepted the cost was real, and chose to keep the bar and rewrite it to a single price equal to its own open — the retired D4. Both are refuted, and the second refutation is the one worth carrying forward.

**The whole bar flips a published outcome, on the real engine, on real bars.** **M11**: CRSR 2026-08-03 → 2026-08-06, take-profit at 11.95, between the session's own high of 11.1717 and the close-stamped bar's high of 12.80.

| treatment of the close-stamped bar | outcome | last trace event | `r_multiple` | `pnl_cash` | `mfe` | `walked.to_t` |
|---|---|---|---:|---:|---:|---|
| keep the whole bar | **`closed_tp`** | `tp_fired` @ 20:00:00Z, price 11.95 | **+0.9141 R** | +227.45 PLN | 1.5781 | 2026-08-06T20:00:00Z |
| reduce it to its open (retired D4) | `open` | `horizon_open` @ 20:00:00Z, 10.89 | +0.0859 R | +21.38 PLN | 0.5156 | 2026-08-06T20:00:00Z |
| **drop it (the chosen rule)** | `open` | `horizon_open` @ 19:59:00Z, **10.61** | **−0.1328 R** | −33.05 PLN | 0.5156 | 2026-08-06T19:59:00Z |

The official daily close for CRSR on 2026-08-06 is **10.61**, so on this session the chosen rule's mark is exact and the retired rule's is 264 bps high. The official daily `[low, high]` is `[10.45, 11.1717]`, and the rest-of-session minute range under the chosen rule is `[10.45, 11.1717]` — identical.

**The rewrite's own premise is false here, and its mechanism is worse than its premise.** The premise was that the bar's open is the closing-auction print; for this session the auction print is the bar's **LOW** (§5.3, measurement 2). And the mechanism breaks reachability: the walk tests `bar.low <= limit`, so collapsing a bar to its open makes any declared level in between unreachable — **M15**, on the real `walk()`, a rung at 68.00 against the bar `o 68.5 h 69.0 l 67.0` fills 13.2353 units (900 / 68.00, the test helper's budget with no sizing buffer; a document carrying the 1% FX buffer spends 891.0 and buys 13.1029) and reports `open`, while the same bar as the single price 68.5 reports `no_fill` with an empty trace.

**The methodological error is the reusable part: the metric that favoured the rewrite could not rank the option it rejected.**

Both of those drafts ranked the options by how often the close-stamped bar's high or low lay outside **the range the rest of the same minute series traded**. That denominator is derived from the series under test. Dropping the bar makes the rest-of-session range *be* the whole kept range, so by construction nothing can lie outside it: the metric scores "drop" as zero violations by definition. The zero looks like a perfect score and is actually no measurement at all — the metric cannot produce the observation that would have told the drafts anything about dropping, because the quantity it measures is defined in terms of the choice.

Under that metric the ranking read: whole bar 182 contaminations (1.161%), reduced 43 (0.274%), and the reduction "removes 139 of 182, 76.4%" — a real improvement over a real baseline, measured correctly, and useless for choosing between two of the three options. Re-ranked against the official daily `[low, high]`, which is a separate vendor payload and not a re-rendering of the minute bars, the order is drop < reduce < keep under every one of five adjustment-free filters, with the worst excursion falling 1457.5 → 202.0 → **34.7 bps** (**M14**, §5.3).

> **The rule, stated so the next design does not need to rediscover it: a metric whose denominator is derived from the series under test cannot rank an option that changes that series. Rank against a source that is not a re-rendering of it.**

This is the project's "a RENDERING standing in for the source" failure in a new dress, and the two drafts that fell to it were each internally rigorous. Nothing in the argument was wrong; the oracle was.

**The second draft's own counter-argument survives and is answered rather than dropped:** §14's last section states what dropping the minute costs and what pays for it. The second draft's per-session framing also understated the exposure — 1.161% of sessions is 16.91% of real windows, because a window is a median of 15 sessions and its terminal bar is a close-stamped bar by construction (**M10**).

### 15.4 Smaller things the drafts stated as facts and got wrong

All corrected above, each re-measured:

* `apps/intent-replay` declares two dependencies, not `[]` (`pyproject.toml:20`).
* the fetch ticker is `document["instrument"]["ticker"]`; neither the document nor the trades record carries an `instrument.symbol` (**M3**).
* the first draft's worked window published 3 900 bars from an unclamped window while its own formula clamped, and its example epochs decoded to 2025 and to 1970. The figure 3 900 happens to be the correct ceiling for the ten-session SMMT window under this memo's grid (§6.9), for a different reason.
* the closing-price measurement was published as "n = 766 pairs over 66 overlapping sessions" beside a sentence describing every (ticker, session) pair. The construction the sentence describes gives **15 677** close-stamped pairs over 67 overlapping sessions, all of which have a row in the daily store (**M4** — the second draft's "15 578" came from a narrower read).
* the direction split of the contaminations the reduction removed was published as "37 low-extended + 103 high-extended", which sums to 140 against the 139 the same paragraph stated. Measured: of the 139 removed, 102 are high-only, 36 low-only and 1 both (**M10**, `a12.py`). Under the chosen rule the population is all 182: 119 high-only, 62 low-only, 1 both.
* the low-extended cases were said to be "bounded under 1% of price". That bound (0.9901%) was over the 139 the reduction removed; over all 62 low-only cases the worst is **301.6 bps** (**M10**).
* "a 390-bar completeness check would refuse 13 700 of 17 163 pairs, 79.8%" was published for the inclusive grid, where it measures 13 134 (76.5%). The 13 700 / 79.8% figure is right — for the half-open grid this memo adopts (**M1**).
* the maximum bars per pair is **390** under this grid, and 391 under the inclusive one; the earlier memos quoted both at various points for the grid they were not describing.
* the store's exposure figures were quoted over "871 bar files that are real windows"; the store holds 881 files, and over all 881 the shares are 149 (16.91%) with at least one contaminated session, 25 (2.84%) above 2% and 6 (0.68%) above 5%, at a median of 15 sessions per window (**M10**).
* the bar store was described as covering 322 tickers; 322 is a double count: the store holds 295 distinct ticker labels over 881 files, of which 27 are in an older undated `<TICKER>.parquet` layout, and 295 + 27 = 322 counts those 27 twice (**M1**).
* `_RTH_FULL_SESSION_SPAN_MIN = 390` is at `population_ladder_monitor.py:187`; `:718` returns it on the non-half-day branch and `:736` multiplies it. `_trail_distance` is `:451-470`, not `:435-470` (`:435` is inside `_USAGE`). `grep -rn "_USAGE" apps/alphalens-research/` returns 10 lines, not 3. `SPLIT_INVALIDATED_CLASSIFICATION` is at `corporate_actions.py:63` and its disposition at `:67`, not `:62-66`. The `split_audit.py` passages are at `:10-14`, `:25-29` and `:36-39`.
* the 390-minute span defect was described as short-only; it runs long on four of the 29 venues that resolve (**M2**).
* the granularity gate was phrased as "at most one bar per session", which is silent on hourly, 30-minute and 5-minute series, and the sidecar asserted `granularity: "1m"` as a literal — so it would have claimed one-minute bars about an hourly file (**M13**).
* the second draft offered `counts.close_stamped` as the reader's exposure signal. It is near-constant — one per session in 91.34% of real pairs (**M1**) — so the discriminating field is `close_bar_drop.range_narrowed`, plus the per-bar list (§9).
* the third draft's `counts` block spelled the drop count twice (`close_bar_reduction.bars_reduced` beside `counts.close_stamped`); §9 keeps one spelling and §12 class F pins that the count and the list agree.

---

## 16. Open questions

Three. All real, none blocking implementation.

**Q1 — the three RTH helpers: the close bound as well as the 390-minute span.** This design now disagrees with `ladder_chart._rth_window_ms` (`:182-201`), `ladder_chart._session_date_for_ts` (`:143-156`) and `population_ladder_monitor._filter_bars_to_rth` (`:740-777`) on **two** counts, where the earlier drafts disagreed on one.

*The close bound.* All three are inclusive at the close (`:183`, `:154`, `:728`, `:756`, `:775`), and §5.3 measures that as leaving the official daily `[low, high]` on roughly 1% of sessions, worst 1457.5 bps, against 0.05% and 34.7 bps for the half-open rule (**M14**). `_filter_bars_to_rth` feeds the nightly population monitor, so `/edge` ladder outcomes computed from it already carry that exposure: 182 of 15 677 sessions in the store, 149 of 881 windows. **GAP:** whether any published `/edge` outcome actually turned on a close-stamped extreme is not measured in this pass — it needs the ladder levels joined to the per-session ranges, which this design did not do.

*The span.* All three compute a session's close as `open + span_min × 60_000` with `span_min = 390` on every non-half-day (`_RTH_FULL_SESSION_SPAN_MIN` at `:187`, consumed at `:718`, `:736`; `ladder_chart.py:193-202` with its own literal). **M2** measures that as short on 17 of the 29 venues that resolve (11 by 120 minutes, 4 by 90 including XWAR, XOSL by 50, XNZE by 15) and LONG on four: 60 minutes on XSHG, 120 on XTAI, 30 on XASX and 15 on XBOM — and silently correct on XNAS, XNYS and, by coincidence of its lunch break, XTKS. `_session_rth_span_min`'s own docstring claims the opposite, that it is "Computed from the real open/close so the idiom transfers to any venue without a hard-coded table" (`:712-713`), which is false on the full-session branch. **GAP:** whether the monitored population has ever contained a non-US venue is not measured in this pass, so that half may be entirely latent.

Does the owner want both fixed plus a recompute of the affected rows, either one fixed with the old rows left as they are, or neither for now? This design does not depend on the answer; it reads the calendar directly and takes its own bound.

**Q2 — where the laboratory's rigour sits on a cold corporate-action cache.** §8 refuses a run whose corporate-action question cannot be answered, including on the `--bars` path, with exit 7 and "re-run when the vendor is reachable". The lookup is cached FOUND-forever and NONE-FOUND for 14 days (`corporate_actions.py:95`, `:258`), so the cost is one online run per (ticker, window) and every later run of that pick is fully offline. But on a **cold** cache a genuinely air-gapped operator cannot run at all — which is most of what `--bars` exists for under D1. Two coherent answers: refuse, as designed, so no written run ever has an unchecked basis; or let `--bars` proceed with a cold lookup cache and a sidecar stating `verdict: "not_asked"`, accepting that such a run can cross a split and not say so. The first is designed here because the incident this memo exists for was exactly a published number whose basis could not be reconstructed, but the second is a defensible reading of what an offline override is for, and the choice is the owner's.

**Q3 — whether the pre-fetch window refusal should be advisory, now that it refuses twice as often.** §6.6 refuses before fetching when `servable_end < required_end`, on a premise §6.5 marks **GAP** (the vendor's day-boundary timezone), while also shipping the post-fetch `vendor_served_short` check and arguing that "predicting the vendor's day boundary is a claim; checking the series is a measurement". By that argument the pre-fetch gate *is* the claim. The half-open grid sharpens the question rather than changing it: because `required_end` is now one session later, the gate blocks **6 of 12** fixture records at `now = 2026-10-09T08:14Z` where the inclusive rule blocked 3, to save one metered Polygon call (**M7**; the verdicts are identical at 08:14Z and 21:00Z, 3 remain at 2026-10-12 and none at 2026-10-13). Two coherent answers: keep the hard refusal, so no run is ever launched that cannot answer the question, and accept that half the comparable records cannot be replayed on the day their ladder expired; or demote it to a warning and let the post-fetch measurement decide, accepting one wasted vendor call per such run. The first is designed here because the refusal's message names the session to wait for and the wait is at most two calendar days, but the asymmetry with §6.5's own stated reasoning is real, the 6-of-12 is twice what the owner saw when this question was first put, and the owner has not ruled on it.
