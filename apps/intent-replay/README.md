# intent-replay

A research backtester that takes a `TradeIntent` document (the same JSON that
`alphalens broker arm` accepts) plus price bars, and reports what the document
would have done.

It is a research tool. It stamps nothing, charges no multiplicity budget, and
carries no accrued history. Every value it needs is read from the document or
stated in the run configuration; nothing is inherited from a deployment, an
environment variable or a production constant. A missing value is a refusal,
never a default.

## Run configuration

Everything the replay needs beyond the document is stated in one JSON block
(spec section 5.2), parsed by `intent_replay.config.RunConfig.from_jsonable`
and rendered back into the result by `to_jsonable`:

| key | form | meaning |
|---|---|---|
| `entry_deadline` | `{kind, value, unit, source, formula}` | the entry-order cutoff, epoch ms UTC, with the rule that produced it; never null |
| `walk_start` | the same shape | the first timestamp of the walk, with the rule that produced it |
| `entry_trail_bps` | integer >= 1, or `null` | the native entry-trail distance. `null` is OFF and is NOT the neutral run: on both deployments the flag is set, so `null` replays an entry ladder production does not use. No upper bound here, but the live rail caps the flag at 150 and reads anything outside `[0, 150]` as 0 — the three-limit ladder, the opposite policy — so a larger value is a run no deployment will make |
| `ceiling_price` | number > 0, or `null` | the take-profit cap |
| `time_stop_t` | epoch ms UTC, or `null` | the position time stop |
| `oco` | `false` | v1 models no OCO pair; `true` is refused |
| `fx` | one key, or four, see below | the currency the instrument settles in, and the conversion when it is not the account's |
| `costs` | four keys, see the spec | the threshold the take-profit cost gate compares against |

`from_jsonable` takes the account currency as a required keyword — the
document's own `spec.size.currency`, which the command reads off the admitted
document. It is not a key of the block: a block able to state it would be able
to disagree with the document it replays.

**The `fx` key set is conditional, and the condition is a value inside it.**
`fx.instrument_currency` is always required, as a `{kind, value, unit, source,
formula}` object whose value is a three-letter ISO 4217 code and whose unit is
`iso_4217`. When that code EQUALS the account currency, those are all the keys
there are. When it differs, three more are required:

| key | form | meaning |
|---|---|---|
| `fx.mid_rate` | `{kind, value, unit, source, formula}`, value > 0 | the mid rate, instrument currency per one unit of account currency. The unit SPELLS the direction — `USD_per_EUR` — and is checked by string equality against the two codes |
| `fx.round_trip_cost_rate` | `{value, unit}`, unit `fraction` | the conversion cost of one round trip, which the cost gate adds. In basis points the term is the rate itself at every notional |
| `fx.sizing_buffer_pct` | `{value, unit}`, unit `percent`, in `[0, 100)` | the settlement-drift haircut the live drain withholds before dividing the budget. The walk withholds it too |

Stating one of those three when the codes AGREE is `unknown_key`; omitting one
when they differ is `missing_key`. Either way it is refused, never accepted and
left inert.

There is no `fx_applies` key. Whether a conversion applies is DERIVED from the
two codes, and the result publishes the derived flag in its own top-level `fx`
block. A caller used to be able to state `false` on a cross-currency document
and nothing compared the claim against anything.

A missing key is `config_incomplete` (`details.keys` names every missing key).
A stated value nothing can use — the wrong type, a non-finite number, a wrong
unit, an unknown key, `oco: true`, a currency that is not a three-letter
uppercase code, or a sizing buffer at or above 100 — is `config_invalid`.
The full reason table is with the refusal codes below. Nothing is defaulted.

## Command line

```
intent-replay run DOCUMENT --config PATH --bars PATH [--format json|ndjson]
intent-replay schema [COMMAND] [--format json]
```

`DOCUMENT` is a TradeIntent JSON file, the same bare document `alphalens broker
arm` takes, or `-` to read it from stdin. `--config` names the run configuration
block above. `--bars` names the price input: ONE JSON array of
`{t, open, high, low, close}` objects, `t` in epoch milliseconds UTC, strictly
increasing, with no key a bar does not model and no key missing. The four prices
must agree with each other: the high is not below the low, and the open and the
close are inside `[low, high]`. A bar is refused rather than repaired, because a
quadruple that contradicts itself would book trades at prices the bar never
carried — an open of 50.00 under a low of 67.00 fills a rung at 50.00. It is required,
because there is no bar series to invent and a run without one would answer
about nothing. `--format` takes `json` or `ndjson` (spec section 5.3: no human
renderer in v1). `intent-replay schema` prints a JSON description of the command
tree, its options, exit codes and failure codes, so a script or an agent need not
parse `--help`.

```json
[{"t": 1790170200000, "open": 68.0, "high": 68.2, "low": 67.9, "close": 68.0}]
```

The command runs the gates of the arming door in the door's order: derived
fields, the published input JSON Schema, completion, the codec, the fixed
point, `validate_intent`. Which document each gate judges is load-bearing and
is the same split the arming door makes. The first two and the fixed point
judge what the AUTHOR wrote, because the input shape does not describe the
fields a door derives and because the fixed point exists to catch what the
author sent and did not get back; the codec and `validate_intent` judge the
completed document.

The document is then INTERPRETED, which is where the fifth gate runs. The
interpreter reads the entry ladder into resting rungs (each with its share of
`spec.size.notional_acct` in the account currency, after the stated
`fx.sizing_buffer_pct` comes off the total — no share quantity, because the
venue's quantity lattice is not stated), the declared exit
levels, the take-profit ladder and the declared reaction, and it records every
path it read. `intent-replay` then refuses a path no class covers
(`path_unclassified`) and an entry tier whose mode it does not model
(`entry_mode_unsupported`). Nothing here says which declared level would REST
at a broker: the walk places `spec.disaster_stop`, because that is the level
that rests at the broker in both deployments, and carries
`exit.initial_levels.stop` without placing it.

Then the configuration block is parsed, and the bars last: each bar's shape, the
ordering of the sequence, and whether the window covers the stated `walk_start`.
Then the walk runs and the result is printed. `--format json` writes exactly one
JSON value on stdout; `--format ndjson` writes two transport lines. Both leave
stderr empty. The next section describes what is in the result. A refusal is
exactly one JSON object on stderr, the last line, with stdout empty; exit status
`0` accepted, `2` usage, `130` interrupted with nothing written, `1` everything
else.

Three choices inside the walk are worth stating here, because all three are
models and not copies of a live system. The stop decision is taken at each bar's
HIGH: the minimum-distance clamp in `broker_contract.stop_decision` is anchored
on the price handed to it, the daemon polls many times inside one bar and its
ratchet keeps the best level any poll produced, so the high is the only choice
that reproduces the level the spec publishes for its own example. A take-profit
does not rest at the broker in this model, so a bar that gaps above a tranche
fills AT the tranche's level, not at the open; the gap rule reaches only the legs
that really rest there — a rung, the disaster stop, and a trailing entry order
once it is placed.

And the third is the **native entry trail**, which is the weakest-evidenced part
of this tool and has its own section below. It is a model of the BROKER's order
type rather than of our code, resting on a vendor's documented behaviour plus a
handful of live fires, and it is why `native_entry_trail_is_a_broker_model`
appears in `divergences` on every run that states a distance.

A third choice is forced on the walk, and since 2026-09-29 it IS published —
as a count, never as a claim. On one bar a rung and a take-profit can both be
touched: the low reaches the rung, the high reaches the tranche, and the bar
does not say which came first. The literature calls such a bar an SNU, a
"situation which is not unique" (arXiv:1412.5558). The walk fills entries
first, the same order it uses for every other pair, and `snu_bars` counts the
bar when that order decided money.

**No pessimism is claimed for this one, and that is a correction.** Measured on
the published template's ladder with `entry_trail_bps: null`, so these are
limit-ladder numbers and a reader who reproduces them from the design document's
canonical block — which states a distance — will get different ones. Rungs 68.00
and 66.50 carrying 60% and 40% of 1500, one tranche of 100% at 68.50, disaster
stop 63.00, with a bar
`open 67.00 / high 68.60 / low 66.40` and then a bar falling to 62.00: filling
first nets **+37.898054**, taking profit first nets **+19.852941**. The gap,
18.045113, is exactly the deeper rung's profit.

An earlier version of this paragraph printed **-84.52** for the second reading.
That number came from running the tranche before ANY rung had filled, which is
not an order the bar permits, and it is retracted. It also said "no order of
steps can keep both rows and also put take-profits before rungs" — also false;
making the order conditional on whether the bar reaches the stop does exactly
that. What is true is narrower and is why the convention stops here: a
take-profit whose clamp sweeps the whole position ENDS the trade, so the two
readings can differ in whether the position survives, and then neither is a
bound. Section 4.4 has the three bands and the measurements.

`ceiling_price` is accepted in the configuration block and not read. A document
that carries a take-profit ceiling is refused by `validate_intent`
(`ceiling_price_unsupported`), and on the live side only the producer-side
bracket builder reads one, so a replay that capped tranches with it would apply
a rule no deployment applies.

The published templates in `apps/alphalens-broker-contract/examples/manual-pick/`
carry no `meta.trade_date`. The arming door fills it from its clock and the
venue's calendar; the replay has neither, so the date is stated in the document.
Add one key to a template before replaying it:

```json
"meta": {"source": "manual", "trade_date": "2026-09-23"}
```

**Replaying an armed pick.** A journal line already carries the date. Turn it
back into a document by removing only what the door derived (the recipe below
is run literally by the tests):

<!-- replay-copy-recipe -->
```bash
set -o pipefail
jq -cR 'fromjson? | select(.ticker == "KO" and .status == "armed") | .intent
        | del(.intent_id, .meta.armed_ts, .spec.tp_tranches[]?.r_multiple)' \
  ~/.alphalens/broker_orders/sim/picks.jsonl | tail -n 1 > ko.json
```

This differs from the copy recipe in the contract README, which also deletes
`meta.trade_date` and `meta.generation` because the door gives a NEW pick a new
date; the replay wants the date the pick was armed under.

In this version `run` can refuse with every code in the table below except
`path_unclassified`. The bar codes became reachable from the command when it
started taking `--bars`; the engine owns the SHAPE of a bar, so a file that
parses and is not a JSON array of bars is `bars_invalid`, while a file that is
not one JSON document at all is the CLI's own `bars_malformed`.
`path_unclassified` is wired in and no document can provoke it today: once
the interpreter has read the paths it reads, every path of the published input
schema is classified, the one path that is not
(`spec.tp_tranches[].r_multiple`) is refused as a derived field, and any other
key an author adds is refused by the fixed point. It is a tripwire for the day
the contract grows a field — which is exactly what it is for.

## Result

An accepted document prints the result envelope. With `--format json` that is
exactly one JSON value; with `--format ndjson` it is one `result` line and one
`summary` line, each carrying `"schema": "intent_replay.stream/v1"`. The
`result` line's `data` is byte-identical to what `--format json` prints, so a
caller can move between the two without a second parser.

The envelope has ten keys, in this order:

| key | what it holds |
|---|---|
| `schema` | `intent_replay.result/v1` |
| `intent_id` | the sentinel `REPLAY`; a replayed document has no arming identity |
| `instrument` | the document's own `ticker` and `mic` |
| `window` | the bar series that was handed in: `from_t`, `to_t`, `bars` |
| `config` | the configuration block, echoed key for key |
| `divergences` | the places this run is known to differ from the daemon |
| `intrabar_rule` | the name of the order the walk resolves a bar in |
| `outcome` | one of `closed_stop`, `closed_tp`, `closed_time_stop`, `open`, `no_fill` |
| `summary` | the nine measures below |
| `trace` | every event the walk emitted, in order |

**`intrabar_rule` names the rule; it does not bound the cash.** The value is the
constant `entries_then_stop_then_ladder`. On some bars the tape does not say
which of two things happened first, and the walk has to decide. `intrabar_rule`
says how it decided, and `snu_bars` says how often it had to. `pnl_cash` is one
outcome under one stated rule. It is not a conservative number, not a lower
bound, and not a worst case. Whether a best/worst envelope is even well defined
for a laddered document is an open question (issue #1616); this version promises
nothing about it.

**`snu_bars` counts bars, and the count only goes one way.** A positive count
means at least one bar's ordering changed the money. A count of zero means no
such bar was DETECTED, which is weaker than none having occurred, so it is not a
certificate. One shape is known to go uncounted and is measured: a bar on which
the position OPENS through a rung below the open, where nothing is held yet, so
the cost gate has no entry price to measure a tranche against. Under a stated
trail distance the inventory is not the same, because what "below the open"
classifies is then a TRIGGER and the sign of the test inverts; the figures below
are the limit ladder's.

That shape is worth **18.045113** — but read the construction before replaying
it. The figure comes from a SINGLE rung, the published template's deeper one
(600 at 66.50) with a tranche at 68.50, against a bar 67.00 / 68.60 / 66.40. It
is pinned by `test_a_bar_that_opens_the_position_is_a_known_blind_spot`.
Replaying the WHOLE two-rung template against that same bar gives a different
answer — 37.898054, with `snu_bars` reading 1, because the 68.00 rung fills
first and the bar is then detected. The blind spot is still reachable on the
full template, but only for a bar that opens ABOVE 68.00, where every rung it
can reach lies below the open. All four figures measured 2026-09-29.

The honest use of the count is deciding whether to trust the result at all,
never adjusting it.

The count is a frequency, never a size: two runs can both report 1 while the bar
decided 18.05 in one and 30.92 in the other. It also has no upper limit. A
tranche that sells only PART of the position leaves the run alive, so a later
bar can raise the count again — and with a stated trail distance the dominant
source is not that tranche but the trail itself, which can meet the convention
on any bar that makes a new low and retraces far enough. How often that is, and
what bounds it, is in the entry-trail section below.

**`pnl_cash` is gross.** The `costs` block decides which take-profit tranches
fire. It never reduces the cash.

**`notional_spent` is what the walk booked, and with a trail it can land on
either side of the declared budget.** The quantity is fixed from the rung's
LIMIT when the order is composed, as in the drain, so a trailing fire above the
limit spends MORE than the rung's share and one below it spends less. Without a
trail the fill price is `min(open, limit)`, so the spend can only come in at or
under the budget.

The whole-share gap is a separate matter and points one way. The live drain buys
whole shares; the replay works in fractional units. That is a deliberate scope
cut rather than a fact the replay lacks, so it is not a `divergences` entry. It
is not small either. Section 5 of the design document
measures the gap at 17.50 on a 1500 budget, 130.00 on 8000 with rungs 120.00 and
115.00, and 60.81 on 1000 with rungs 196.13 and 175.40 — where the entry anchor
also moves by 0.56 in price, about 30 basis points, and the R denominator moves
with it. Those figures are measured at a rate of 1.0 and no sizing buffer.

The stated buffer is a SECOND component of the residual and is not a gap: the
drain withholds it too. The two do not simply add, because the lattice bites on
whatever budget is left. On the 1500 ladder above, at a rate of 1.0, adding a
1 per cent buffer takes the lattice gap from 17.50 to 69.00; at a rate of 1.08
it takes it from 69.50 DOWN to 53.30. So a buffer can make the lattice gap
smaller, and any figure here has to travel with its rate and its buffer.

**`window` describes the series you handed in, not the part the walk read.** It
can be wider on both sides. Bars before `walk_start` are skipped, and the walk
stops as soon as the position closes, so later bars are never looked at. Both
still count in `window`.

**`filled_fraction` can be slightly above 1.0, and one reason is not rounding.**
`validate_intent` accepts an entry ladder whose allocations sum to within 1e-6
of 100, so a document stating 50.0 and 50.000001 is a valid document and its run
reports `1.00000001`. Measured 2026-09-29. The replay does not clamp the value,
because clamping would hide that case; `/edge` clamps to `[0, 1]`, this does
not. Floating-point noise can also push it above 1.0, but only in the last few
digits, which is seven orders of magnitude smaller and is not the reason for the
decision.

### The summary

Nine keys. `filled_fraction` and `snu_bars` are bare numbers. Every other
measure carries its unit.

`notional_spent` and `pnl_cash` carry the document's own `spec.size.currency`.
`avg_entry_price` and the R denominator carry `fx.instrument_currency`: they are
prices in the INSTRUMENT's currency, which the configuration states. They used
to carry the symbolic token `instrument_currency`, because no fact named the
currency; the token is retired. `pnl_pct_of_spent` carries
`percent`, so 4.58 means 4.58%. `r_multiple`, `mfe` and `mae` carry `R`.

`r_multiple` always carries its denominator as an object with five fields:
`kind` (`placed_stop`), `value`, `unit`, `source` (`spec.disaster_stop`) and
`formula` (`avg_entry_price - placed_stop`). The level that rests at the broker
is `spec.disaster_stop` on both deployments, in every document, including one
that supplies its own `exit.initial_levels`.

**When the denominator is not positive, three measures are null.** Not
"negative": exactly 0.0 is reachable, by a bar that opens at the disaster stop
and therefore fills there. `r_multiple.value`, `mfe` and `mae` all become null,
and the `denominator` object is still carried, so a reader sees why. This is not
a paper case — without the rule, a run that lost 109.375 reports +1.16 R.

Two null shapes, because the design document prints two. `r_multiple` keeps its
object, because the denominator inside it is the answer to "why is this null".
Every other measure — `mfe`, `mae`, `avg_entry_price`, `pnl_pct_of_spent` —
becomes a bare `null`, because a wrapper holding nothing but a unit answers
nothing.

**`mfe` is never negative and `mae` never positive, but only to within one unit
in the last place.** The structural argument is about fill prices: every fill
sits inside its own bar and the extremes are tracked from the first fill onward.
But `avg_entry_price` is `cash / units`, and that division can land one ulp below
a fill price the trough then equals. Measured 2026-09-29: one run in 200 000
produced `mae` at `+6.447756222868429e-16`. A check written as `mae <= 0` would
go red at that rate.

**The denominator is not held away from zero, and that is the document's
geometry rather than a defect.** A denominator of 1e-10 produces an R on the
order of 1e10. It cannot reach infinity: the denominator is a difference of two
prices of the same size, so its smallest non-zero value is one unit in the last
place, which is 7.105427357601002e-15 at a price near 63 (measured
2026-09-29). Section 5 of the design document works the resulting cap out.
`allow_nan=False` on the writer means an infinity or a NaN cannot reach stdout:
the writer REFUSES to emit it and raises instead. That is a statement about the
writer, not a proof that every number is finite.

### divergences

Each entry names a place where the replay is known to differ from the live
daemon for a reason no configuration value can close, because the replay lacks a
FACT rather than a setting. Four entries exist, and each is printed only on the
runs it applies to:

| entry | printed when |
|---|---|
| `daemon_trail_guards` | the declared policy moves the stop at all; it covers both stop arms |
| `daemon_reanchor_latch_is_journal_lifetime` | the reaction is `reanchor_on_fill` |
| `native_entry_trail_is_a_broker_model` | the run states an entry-trail distance |
| `take_profit_observation_time` | the resolved take-profit ladder is not empty |

`native_entry_trail_is_a_broker_model` covers more than its name suggests, and
the entry-trail section above has the list. The server owns the ratchet and the
fire once the order rests, so there is nothing local to disagree with — but the
watch that PLACES the order is ours, and the replay lacks the quotes, the session
boundaries and the account state its gates read. Two of those omissions move the
answer in the tool's favour.

A fifth entry, `cost_gate_prices_the_account_currency`, was RETIRED when the FX
keys landed. It named two differences under one name. The currency half is
closed: the gate now prices the instrument's currency, from the stated rate. The
whole-share half remains and is a scope cut rather than a missing fact, so it
does not belong here — above the fee card's knee the two thresholds agree, and
below it the replay's is too low by 1.05, 15.24 and 38.10 basis points at the
points the design document measures, so it fires tranches the daemon would
decline. Nothing in the result reports that any more.


### The native entry trail

With `entry_trail_bps` stated, nothing rests at a rung. The first bar to reach it
places a trailing buy order that follows the running low down and fires on a
rebound of the stated distance. On both deployments the flag is set, so this is
the path every armed pick takes and `null` is not the neutral run.

**What is modelled is the BROKER's order type, not our code.** Once the order
rests, the server owns the ratchet and the fire. There is no local implementation
to compare against, so the parity test cannot reach this path at all, and the
model rests on a vendor's documented behaviour plus a handful of live fires. That
is the weakest evidence anywhere in this tool, and it is why every run stating a
distance prints `native_entry_trail_is_a_broker_model`.

The rules, each stated so a reader can check it against a bar:

| | |
|---|---|
| arming | the first bar whose low reaches the rung, referenced on `min(open, limit)` — the touch happens at the first print when the bar gapped through the level, and at the level otherwise |
| trigger | `trough + distance`, where `distance` is ABSOLUTE and frozen at the arm. The wire field is a price distance to the market, computed once, so the trigger is not `trough × (1 + d)`. The two agree at the touch and part as the trough falls — 21 bps at a 30% drawdown on a 50 bps distance |
| the order inside a bar | the trigger is tested against the trough the bar INHERITED, and only then does the bar's own low ratchet it down. Had the low come first the buy would pay less, so this is the worse reading where a worse one is well defined |
| the arming bar | can fire, at `reference + distance`. It takes that level however high it opened: the order is placed at the touch, which cannot precede the open |
| a later bar | that opens through the trigger fills at the OPEN. By then the order rests, so the gap rule reaches it |
| the resting stop | a bar that opens ABOVE it fires and is then stopped out — a loss, and the worse reading. A bar that opens BELOW it closed the position at the first print, and buying after that is a re-entry this tool does not model |
| depth | a bar whose FIRST price is already below the NEXT-LISTED rung hands the move to that rung, and the shallower one never arms. Listed, not cheapest: `validate_intent` does not require the ladder to descend |
| the deadline | an armed or barred rung expires with the others, through the one published cause |
| quantity | from the rung's LIMIT, as in the drain. A trailing fire changes when and at what price a rung executes, not how much it buys |

**How often the convention decides, measured by running it.** The construction
is stated because the number is a function of it: the last 20 sessions of the
split-adjusted daily store, every one of the 11 654 tickers with a complete
history, an eight-bar watch, ONE rung placed as a pullback trap at the first
bar's open less `p`, a 50 bps distance, and no take-profit ladder so `snu_bars`
carries this source alone.

| trap `p` | mean `snu_bars` | watches reporting 0 | watches that fired |
|---|---|---|---|
| 0.5% | 0.67 | 34.2% | 69.8% |
| 1.0% | 0.57 | 44.1% | 57.0% |
| 2.0% | 0.44 | 57.1% | 43.8% |

The trap depth belongs in that table for a reason worth stating on its own,
because it makes the arming bar the COMMON case and not an edge one. A rung at
`open × (1 − p)` carries a trigger at `rung × (1 + d)`, and the bar's own open
lies ABOVE that trigger exactly when `p > d / (1 + d)` — 0.4975% at 50 basis
points. So every trap deeper than half a percent arms on a bar whose open has
already passed the trigger, and such a bar IS counted: the order was not resting
at the open, so an open above the trigger settles nothing.

Zero from this source is common without being the rule, and the frequency is a
function of the bar's granularity and of the trap depth rather than a property of
the model; on minute bars, whose range is two orders smaller, it falls again.

**Where the fill price stands against the record.** The convention fills at the
trigger. Of the fires frozen in `tests/incident_1317_fixture.py`, **one of the
five LIVE ones** executed above the trigger its order carried, by 28.4 bps; the
LIVE median is −23.8 bps and none filled exactly at the trigger. The other 18
rows are SIM, whose fills are synthetic, so they say nothing about the real
matching engine and are not pooled here. The historical figures are measured
against the FIRST computed trigger rather than the ratcheted one, so they bound
the error from one side only and cannot be turned into a correction.

**What the model does NOT carry.** Each of these is a fact the replay lacks
rather than a setting it could be handed, and the two marked FLATTERS move the
answer in the tool's favour:

| | |
|---|---|
| the trail rides the whole entry window | **FLATTERS.** Live the order is a DayOrder: it dies at every session close, and re-arming returns the tier to watching, needing a fresh touch AND a new low of the whole watch. Every live fire on record happened in the session of its touch |
| the server ratchets from PLACEMENT, not from the touch | **FLATTERS.** The replay's trough includes the whole arming bar, so its trigger is systematically lower |
| the touch is sampled about every 45 s | missed touches measured at 2.9% overall, 4.5% on the second rung, 6.7% on the third |
| the reference is a BID and the fill pays an ASK | the replay has trade prices, not quotes. One measured midday spread on a name that needed nine retries: 29 bps, against a 50 bps distance |
| wrong-side rejections and their retries | 6 of 34 fires, median 68 bps worse against the first trigger versus −28 bps for the rest. The issue that measured it calls that a hint and not a result |
| the coarse ratchet step | a deliberate scope cut, not a missing fact: the step is a tenth of the distance floored at one tick, which is 11% of the distance at the median and up to 26% on cheap names, and it holds the live trigger higher than the modelled one |
| the exit-region refusal, the watch-capacity cap, the day-1 gap gate, the cash floor at fire, partial fills, sibling retirement | live gates that change WHICH rungs fill. None is modelled |
| the `StopLimitPrice` ceiling | not modelled because it does not bind: 8 of 23 recorded fires executed above it, by up to 53.5 bps |

A stated distance has no upper bound here, because section 5.2 publishes the key
as an integer `>= 1` and a deployment rail is not a document fact. But the live
reader caps the flag at 150 and treats anything outside `[0, 150]` as 0 — the
three-limit ladder, the opposite policy — so a larger value describes a run no
deployment will make. The absent bound is about the POLICY: a value so wide it
cannot become a float is refused all the same, because the walk multiplies the
distance by a price and that conversion would raise.

## Refusal codes

One code per failure mode, with a closed `details.reason` vocabulary where a
mode has more than one rule inside it. The object is `broker_contract.failure`'s:
`code`, `message`, `retryable`, `details`, `suggestions`. Nothing this tool
refuses is retryable. The `owner` column names the half that defines the code:
`engine` (a module of this package that raises it as a typed exception), `contract`
(`broker_contract`), `CLI` (`intent_replay.cli`, which maps the engine's
reasons onto it).

| code | owner | retryable | meaning |
|---|---|---|---|
| `bars_empty` | engine | no | No bars were supplied. |
| `bars_unordered` | engine | no | The bars are not strictly increasing in time. |
| `bars_invalid` | engine | no | A bar that cannot be compared, or bar input that is not the published shape: a non-finite price, prices that contradict each other (a high below the low, or an open or close outside `[low, high]`), input that is not a JSON array, a bar that is not an object, a missing key, a key a bar does not model, or a field of the wrong type. `details.reason` names which, with `details.index` and `details.field` where they apply. |
| `window_too_short` | engine | no | The bars do not cover the stated `walk_start`; `details.reason` says which side. |
| `config_incomplete` | engine | no | A required configuration value was not stated; `details.keys` names every missing key. |
| `config_invalid` | engine | no | A stated configuration value nothing can use; `details.keys` and `details.reason` name it. A file that parses but is not an object is refused here too, with `<root>` standing for the whole block. |
| `path_unclassified` | engine | no | The document carries a path the replay neither interprets, translates nor lists as out of scope; `details.paths` names them. |
| `entry_mode_unsupported` | engine | no | An entry tier declares an `entry_mode` v1 does not model; `details.tiers` names them. Only `pullback` rests as a rung a bar walk can test, and the arming door ADMITS `immediate`, so this refusal is the interpreter's. |
| `intent_invalid` | contract | no | The document is internally inconsistent (`validate_intent`); `details.reason` names the rule, as at the arming door. |
| `intent_malformed` | CLI | no | The document is not the published input contract; `details.reason` names which rule, see below. |
| `config_malformed` | CLI | no | The configuration file could not be PARSED: not a UTF-8 JSON document, or an object in it repeats a key. `details.reason` names which, `details.path` the file. |
| `bars_malformed` | CLI | no | The bar file could not be PARSED: not a UTF-8 JSON document, or an object in it repeats a key. `details.reason` names which, `details.path` the file. A file that parses and is not the published bar shape is `bars_invalid`. |
| `usage` | CLI | no | The invocation is malformed (a bad option or value), or a file it names cannot be read (`details.path`). |

`intent_malformed` carries the reasons of the arming door that apply to a
replay, plus the parser's two. The door's venue, pick-key and generation
refusals do not apply (spec section 4.3), and `meta.schema_version` is not
read (section 4.3.1).

| code | `details.reason` | what the author did |
|---|---|---|
| `intent_malformed` | `not_json` | the bytes are not a UTF-8 JSON document |
| | `duplicate_key` | an object repeats a key; a JSON parser keeps the LAST silently, so the value sent first would vanish; `details.keys` lists them |
| | `derived_field_supplied` | the document carries `intent_id`, `meta.armed_ts` or a `r_multiple`, which a door computes; `details.paths` lists them |
| | `schema_violation` | the document fails the published input JSON Schema; `details.path` locates it |
| | `trade_date_required` | no `meta.trade_date`: the replay has no clock and no calendar to fill it from, so the author states it |
| | `trade_date_malformed` | `meta.trade_date` is not a date |
| | `undecodable` | the shape passes but the decoder refuses it (e.g. `generation: 1.0`) |
| | `key_discarded` | a key the decoder would DROP, so the replay would not carry what was sent; `details.paths` lists them. A `meta.trade_date` that parses but is not spelled `YYYY-MM-DD` lands here, as at the door |
| `config_malformed` | `not_json` | the configuration file is not a UTF-8 JSON document |
| | `duplicate_key` | an object in the configuration file repeats a key; `details.keys` lists them |
| `bars_malformed` | `not_json` | the bar file is not a UTF-8 JSON document |
| | `duplicate_key` | an object in the bar file repeats a key; `details.keys` lists them |

`config_invalid` has its own vocabulary. It answers a value the caller DID
state, so the key is present and nothing can use it. A key the caller did not
state is `config_incomplete` instead, and missing keys win: when keys are both
missing and unusable, only the missing ones are reported.

| code | `details.reason` | what the caller stated |
|---|---|---|
| `config_invalid` | `unknown_key` | a key this block does not model. The contract's decoder DROPS such a key with only a warning, so a misspelt `entry_trail_bp` would otherwise switch entry trailing off in a run whose author believes the distance was stated |
| | `wrong_type` | the value is not of the key's type; a stated `null` where none is allowed is included |
| | `numeric_not_finite` | a stated number the walk's arithmetic cannot carry: NaN or infinite, where every comparison on it would silently pass, or an integer too wide to convert to a float. `entry_trail_bps` has no upper BOUND, but the walk multiplies it by a price, so a value that cannot become a float is refused here rather than raising |
| | `not_positive` | a distance or a price that must be above zero is not |
| | `negative` | a cost below zero |
| | `unit_mismatch` | the unit is not the one the walk compares against |
| | `empty_string` | a provenance field or a currency code with no text |
| | `oco_unsupported` | `oco` is stated `true`; v1 models no OCO pair, and the block travels in the result, so accepting it would describe a policy the run did not apply |
| | `not_a_currency_code` | a stated currency is not a three-letter uppercase ISO 4217 code. The arming door holds the document's own code to that shape, and the two are compared |
| | `buffer_out_of_range` | `fx.sizing_buffer_pct` is at or above 100. At 100 the budget is zero and the walk divides by it; above 100 the budget is negative and the run books a profit on a short the document never declared |

Design: `docs/superpowers/specs/2026-09-23-intent-replay-design.md`.
Implementation plan: `docs/superpowers/plans/2026-09-25-intent-replay-step1.md`.
