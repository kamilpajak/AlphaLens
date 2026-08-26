# alphalens/feedback — broker-free population-ladder replay

How every brief candidate's trade ladder is replayed against its real intraday
price path, and how to read the numbers that land on `/edge`. This README
stitches together the mechanics that live in the module docstrings
(`ladder_replay.py`, `population_ladder_monitor.py`) and the frozen design memos
(`docs/research/ladder_order_ideal_scenario_2026_06_03.md` §5,
`docs/research/ladder_chart_visualization_design_2026_06_09.md`) into one
operator/researcher-facing picture. It is organised around the questions a
reader of `/edge` actually asks.

## Data flow

```
brief parquet (~/.alphalens/thematic_briefs/)          selection side
       │  enumerate every verified candidate with a plannable brief_trade_setup
       ▼
population_ladder_monitor.py                           nightly on the VPS:
       │  Polygon minute bars (incremental cache)      alphalens feedback backfill-shadow-returns
       ▼                                               (alphalens-feedback-shadow-returns.timer, 06:30 UTC)
ladder_replay.replay_ladder  (pure, deterministic)
       │
       ▼
~/.alphalens/population_ladders/  parquets
       │  compose run --rm rebuild-ladder-outcomes
       ▼                                               ExecStartPost after the nightly run
Postgres edge_ladderoutcome                            + hourly self-heal (alphalens-edge-mirror.timer)
       │
       ▼
Django /v1 edge API  →  SPA /edge  (LadderChart.svelte + classification chips)
```

Naming note: the systemd unit and CLI command keep their historical names
(`feedback-shadow-returns` / `backfill-shadow-returns`) although the legacy
per-decision shadow-return replay was removed with the broker chain (#465/#467,
ADR 0012). The command now drives only this population monitor; the rename is a
deferred follow-up tracked in `deploy/systemd/README.md`.

## 1. What the replay is — and is not

It is **SIM / modeled fills**: a deterministic replay of the ladder against
Polygon minute bars, with exactly one modelling assumption — a level is
executed *at that level* the first time price touches it (entry on
`low <= limit`, TP on `high >= target`, SL on `low <= disaster_stop`). No
broker, no slippage, no commissions. The `/edge` chart says so persistently
(the `SIM · modeled fills` chip and the `(?) how?` popover in
`apps/web/src/lib/components/LadderChart.svelte`).

It is **RTH-only**: the bar window covers regular trading hours, so overnight
and pre-market moves are invisible — a gap through a level registers on the
first RTH bar that shows it.

It is **population telemetry, not trading**: every plannable candidate from the
brief parquet is replayed, never a ledger or broker position
(`population_ladder_monitor.py` docstring: telemetry-only, click-orthogonal).
It measures whether the tool's setups work, not what anyone traded.

## 2. Ladder anatomy

A setup is up to **3 resting-limit entry tiers** (E1 shallowest dip, E3
deepest), up to **3 take-profit tranches** (TP1..TP3, production always emits
equal thirds — `thematic/trade_setup/builder.py`), and **one disaster stop**.

Entry allocations are **equal-risk sized**
(`thematic/trade_setup/sizing.py`): notional per tier is proportional to
`e_i / (e_i − stop)`, so each tier loses the same amount if stopped. Since E1
sits furthest above the stop, **E1 gets the smallest allocation and E3 the
largest** — e.g. the recurring 17% / 24% / 59% split used in the examples
below (a "1000-unit" plan: 170 / 240 / 590 units).

The `/edge` chart draws **only E1's price line** by design
(`ladder_chart.py::_price_lines` uses `entries[0]`). E2/E3 exist in the data
and their fills appear as markers, but there is one "entry" line on screen.

## 3. Entry TTL — why a late touch of E2/E3 does nothing

Entries can fill only on bars **before** `entry_expiry_ms` = the session open
**7 trading days** after arrival (`DEFAULT_ORDER_TTL_DAYS = 7` in
`broker_contract/constants.py`; converted to epoch ms per the exchange
calendar in `population_ladder_monitor.py::_engine_cutoffs`; enforced by the
`break` in `ladder_replay.py::_LadderWalk._fill_entries`). This mirrors the
live broker's 7-day entry TTL.

After the cutoff, unfilled tiers are dead: a week-3 dip through E3 fills
nothing. The TTL only blocks *new* fills — TP/SL still resolve a position
already opened.

## 4. TP mechanics — one shared pool, re-based tranches

The filled tiers form **one position pool**; TP tranches sell shares of that
pool, not of "their" entry tier. Three consequences:

**TP/entry independence.** The only entry-side gate on exits is "some tier has
filled" (`_LadderWalk.step`: `if not self.filled: return`). TP detection
iterates all TP levels regardless of *which* entries filled — there is no
"TP2 waits for E2" rule.

**Tranche re-basing** (`ladder_replay.py::_realized_r_with_frac`): each
tranche's intended share is defined over the FULL plan, so with a partial fill
it is re-based:

```
share_of_filled = (tranche_weight / tp_weight_sum) / filled_frac   capped so Σ shares ≤ 1.0
```

Worked on the 17/24/59 plan with equal-third TPs:

- **E1-only fill** → `filled_frac = 0.17`. TP1's share = (1/3)/0.17 ≈ 1.96 →
  capped at **1.0**: TP1 sells the *entire* held position. TP2/TP3 are
  economically empty even if price touches them. In production this is common:
  of 142 terminal rows with one tier filled, zero sold three tranches
  (KTOS 2026-08-03, WK 2026-06-17, APPS 2026-06-13 all show TP2 "selling"
  without E2/E3 ever filling — i.e. deeper TPs touched but nothing left).
- **E1+E2 fill** → `filled_frac = 0.41`. TP1 sells (1/3)/0.41 ≈ 0.813; TP2
  sells the remaining 0.187; TP3 sells nothing. E3 never filled, yet TP2
  participates — the pool is shared.

Whatever share remains at exit is closed at the stop (SL), marked at the
expiry-bar close (TIME_STOP), or at the last close (still open). The un-sold
share is exposed as `LadderOutcome.residual_fraction`; the tranches that
actually sold as `realized_tp_ids`.

**No cancel-on-TP + retroactive re-basing.** Nothing cancels unfilled entry
tiers when a TP fills, so entries keep filling until the as-specified exit
(`exit_reached`). Realized R is computed **once at finalize** with the FINAL
`filled_frac` and FINAL blended entry (`_blended_entry`, alloc-weighted): if
TP1 fires on an E1-only position and E2 fills later, TP1's contribution is
retroactively re-based as though the position had been 0.41-filled all along.

**Per-bar ordering.** Within a bar: entries fill first, then SL, then TPs
(`_LadderWalk.step`). Minute bars hide intra-bar sequence, so a bar where both
SL and a TP are crossable resolves **SL-first** (conservative,
`TIE_BREAK_SL_FIRST`) and the row is flagged `same_bar_ambiguous` /
`ambiguous_bars` so the bias stays auditable.

## 5. Lifecycle and classifications

| Classification | Group | Meaning |
|---|---|---|
| `OPEN` | ongoing | entered, neither TP nor stop hit yet |
| `PARTIAL_TP_OPEN` | ongoing | ≥1 TP level hit, remainder still running |
| `TP_FULL` | terminal | every TP price **level** touched (see §6 caveat) |
| `PARTIAL_TP_THEN_SL` | terminal | ≥1 TP hit, then the stop |
| `SL_HIT` | terminal | stopped out, no TP first |
| `TIME_STOP` | terminal | 42-session horizon expired; remainder marked at the expiry close |
| `NO_FILL` | terminal | no entry tier ever touched inside the TTL |
| `BAD_GEOMETRY` | terminal-degenerate | stop at/above blended entry — R undefined, frozen |
| `SPLIT_INVALIDATED` | terminal-degenerate | replay window crosses a real corporate action (split / material special dividend, #1090) — ladder levels were set on pre-action prices, `realized_r` null, frozen |

**Implausible-move guard (#1090).** `|forward_return| > 0.60`
(`bar_window.IMPLAUSIBLE_RETURN_THRESHOLD`) is a *trigger*, not a verdict
(design memo `docs/research/implausible_guard_redesign_2026_08_23.md`,
Amendment 1): the monitor asks Polygon `/v3/reference/{splits,dividends}`
(cached at `<store>/corporate_actions_cache.json`; FOUND forever, NONE-FOUND
14 days) and, when nothing is found, cross-checks the window return against
yfinance ADJUSTED closes. Dispositions — `split_invalidated` (terminal
quarantine above), `extreme_validated` (outcome accepted), `lookup_failed` /
`data_quality` (prior carried) — are stamped per touched row in the additive
`guard_disposition` + `guard_config_version` columns and counted on the sweep
report (`corporate_actions.py`).

Source of truth: `ladder_replay.py::_classify` +
`population_ladder_monitor.py::_TERMINAL_SET`; mirrored web-side (with the
plain-English glosses the legend shows) in
`apps/web/src/lib/data/ladderStatus.ts`, which additionally carries two
monitor-level row statuses a `/edge` reader may see — `NO_STRUCTURE` (row has
no plannable trade setup) and `NO_DATA` (no usable price bars) — that never
come out of `_classify`.

Ongoing rows are re-replayed nightly until terminal; the session on which a
row terminalizes is stamped as `matured_at`. `TIME_STOP_DAYS = 42` **trading**
sessions (`paper/constants.py`, ≈60 calendar days) is a *measurement horizon*
anchored on the momentum signal-decay literature (Moskowitz–Ooi–Pedersen 2012;
Chan–Jegadeesh–Lakonishok 1996) — it caps how long a setup gets to prove
itself. It is NOT a live trading rule: live execution has no time stop by
design.

## 6. Reading the numbers on /edge

**Realized R** is the headline "as-specified" number: P&L of the modeled
position in units of initial risk (blended entry − stop), tranches weighted as
in §4. It is slippage-free by construction — the clean geometry number.

**`TP_FULL` counts touched levels, not shares sold.** `_classify` keys on
`len(hit_tp_ids) == len(ladder.tps)` — price touched every TP level —
independent of how many tranches had anything left to sell. With a shallow
fill (§4), TP1 can consume the whole pool and `TP_FULL` still shows when TP2/3
are later touched. Production scale: 74 of 95 `TP_FULL` rows sold fewer
tranches than levels touched; 48 have the "captured 1, touched 3" shape. The
UI is honest about this: the class chip appends **`captured/touched sold`**
(`apps/web/src/lib/edge.ts::tpCaptureLabel`) and the chart draws
touched-but-nothing-left TPs as dim **`TP_TOUCHED`** circles instead of solid
arrows (`LadderChart.svelte`).

**The excess-return column is NOT the replay.** `forward_return` is a plain
buy-and-hold move from the arrival-session opening-window VWAP to maturity (or
the last closed session while ongoing) — "independent of any ladder fill"
(`ladder_replay.py::_forward_return`); `market_excess_return` subtracts the
same-window SPY leg (`benchmark_excess.py`). This is **selection** telemetry
(was the pick good?), deliberately decoupled from replay R, which is
**execution** telemetry (did the ladder geometry capture it?). A row can show
`NO_FILL` with a large positive excess return — the pick was right, the dip
never came.

## Further reading

- `ladder_replay.py` module docstring — the fill model, the three measurement
  layers (substrate / as-specified / ratchet what-if), and the purity contract.
- `population_ladder_monitor.py` module docstring — enumeration, maturity
  gating, the Polygon cache, and carry-forward resilience.
- `docs/research/ladder_order_ideal_scenario_2026_06_03.md` — §5.0 design
  rationale (why replay-as-specified, not a smarter trader).
- `docs/research/ladder_chart_visualization_design_2026_06_09.md` — the /edge
  chart design.
- `deploy/systemd/README.md` — operator runbook for the nightly compute and
  the hourly edge mirror.
- `alphalens_research/diagnostics/fill_partition.py` plus its driver
  `apps/alphalens-research/scripts/measure_fill_partition.py` — a read-only,
  offline instrument that partitions this store's outcomes by WHICH entry tiers
  filled (first-tier only / mixed / deep-only), reports them per OPPORTUNITY
  rather than per taken trade, and prices fills at the measured trailing
  overshoot rather than the tier limit. It writes nothing back and makes no
  Polygon call. Issue #1113.
