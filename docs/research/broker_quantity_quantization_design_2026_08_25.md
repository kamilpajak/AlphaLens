# Broker-agnostic quantity quantization

**Status: LOCKED 2026-08-25.**
Supersedes the lot-handling conclusion of
[`saxo_fx_leg_gpw_design_2026_07_18.md`](saxo_fx_leg_gpw_design_2026_07_18.md)
§4 item 6 (see §7 below). Related: #1122 (the layer decision), #1125 / PR #1126
(one definition of the share epsilon), ADR 0014 (broker-agnostic execution).

---

## 1. Why this exists

`broker_contract/fx.py` states the rule the execution layer lives by:

> the ADAPTER reports, never the contract decides.

The quantity chain does not honour it. Whole-share arithmetic — a property of
**Saxo**, not of the system — is hard-coded in the two layers that must not know
the venue: the contract (`sizing.py` `math.floor`) and the pipeline (`round()`
in `live_exit_engine`). The value `QTY_PRECISION = 0.5` carries the same
assumption under a name that reads as universal.

This memo records what that costs, what replaces it, and what the replacement
costs in turn.

## 2. Measured, not argued

Run against the real functions on 2026-08-25. Three distinct failure classes.

### A. A fractional venue's capability would be silently unused

`sizing.py` `qty = max(0, math.floor(tier_notional / limit))`:

| tier notional | limit | whole-share result | fractional would be |
|---|---|---|---|
| $40 | $59.786 | **0 shares** | 0.669 |
| $25 | $67.62 | **0 shares** | 0.370 |
| $100 | $59.786 | 1 share | 1.673 |

A fractional broker still gets zero and the position never opens. The exit
ladder is worse: `round(reference_qty * tranche_pct)` on 0.669 shares yields
`[0, 0, 0]` — a fractional position has no way out.

### B. Actively wrong, not merely inert

`available = round(owned)` rounds **up**. Replaying the realizer arithmetic:

| exit qty | owned | sells | disaster stop |
|---|---|---|---|
| 1 | 0.669 | 1 | **CANCELLED** |
| 1 | 0.51 | 1 | **CANCELLED** |
| 2 | 1.6 | 2 | **CANCELLED** |

At 0.669 shares held the rail tries to sell 1 it does not have, **and** cancels
the disaster stop, because `round(0.669) - 1 == 0` reads as "nothing remains".
The broker would likely reject the sell. The stop is gone either way.

### C. The "is this quantity real" test is size-dependent

A 0.3-share tranche is not "real" (`0.3 > 0.5` is false); a 0.669-share tranche
is. So the arm-time contract admits or refuses the same trade depending on how
small the fraction happened to come out. That is not a threshold.

## 3. The design

### Quantity rules are not a capability

A capability Protocol (`SupportsOcoExit`, `SupportsAmendStop`) is right for facts
a venue may genuinely **lack**. Every venue has a quantity lattice; an adapter
that cannot state one cannot trade. Modelling a never-absent fact as optional is
what permits silent defaulting — today's bug.

The right precedent is `currency`:

> Authoritative instrument currency comes ONLY from Saxo's own instrument data
> — never MIC-inferred, never guessed. A row without CurrencyCode is a refusal.

Quantity rules are stamped on `InstrumentRef` at resolve time, or the resolve
fails. `resolve_instrument` already caches; `get_instrument_details` is today
called **uncached at eight placement sites**, so folding the fetch into resolve
*reduces* HTTP.

### Quantize at the point of decision; the adapter only verifies

A price is not a decision input downstream, so quantizing it at the wire is
safe. A quantity **is** one: `qty == 0` means "not plannable", tranche size
drives the #1112 cost gate, the arm gate refuses on tranche shape. Quantizing
only at the wire would mean every upstream decision is taken on a number the
venue cannot express.

So there is exactly **one quantizer**, in the contract, against a validated
lattice passed in. The adapter's `_verify_quantity` **raises, never adjusts**:
`_quantize_price` may adjust within 25 bps because a sub-25-bps price move is
economically inert, but a share is not. An off-lattice quantity arriving at the
wire is an upstream defect.

Two quantizers is drift, and drift is the present bug: the contract floors, the
pipeline rounds, the adapter is silent.

### Five concepts, not one number

`quantity_step` (the only mandatory lattice) · `min_quantity` ·
`quantity_precision` (decimal places) · `min_notional` · `round_lot`.

Precision does not imply step: two decimals permit `1.03`, a step of `0.05` does
not. `round_lot` is **advisory** — a US equity may have a 100-share round lot and
still accept odd lots — so it is carried and journaled but never enforced.

### Floor the magnitude, never round to nearest

Established practice across ccxt (`TRUNCATE`), Hummingbot (floor division) and
LEAN (`AdjustByLotSize`, absolute-then-restore-sign). And never floor a *signed*
negative: `floor(-1.23) == -2` increases a sale. There is no rounding-up
primitive anywhere in the redesigned rail.

### Float, with exact integer lattice arithmetic

`precision` is the vendor's own statement that quantities carry at most that
many decimals, so it is the exponent that makes the lattice exact:

```
units = int(round(qty * 10**precision)) // int(round(step * 10**precision))
```

`Decimal` is rejected **for this codebase**: quantities already round-trip
through JSONL and parquet as floats and arrive from the vendor as floats, so a
Decimal layer adds a lossy boundary at every journal read unless every record
format changes — and `Decimal.quantize()` handles power-of-ten steps only, so
arbitrary steps would still need hand-written lattice arithmetic. Full cost, no
operation. The precondition (`step * 10**precision` is integral) is validated
once, pipeline-side, rather than assumed at fifty call sites.

### The fact that makes a 54-site change reviewable

`same_quantity(a, b, lattice)` is `abs(a - b) < lattice.step / 2`.

On a whole-share venue half a step is **0.5** — the exact number in
`constants.py` today. `QTY_PRECISION` was never an arbitrary epsilon; it is
`step/2` at `step = 1.0`. The replacement is therefore behaviour-preserving on
Saxo **by derivation, not by luck**.

### Four questions, four predicates

The ~45 epsilon comparisons and ~9 bare-zero comparisons ask four different
questions while sharing one number and a freely chosen operator:

| question | predicate | sites | behaviour on Saxo |
|---|---|---|---|
| is this a real quantity? | `is_tradable` | ~18 | identical |
| are these the same quantity? | `same_quantity` | ~6 | identical |
| does a cover / exceed b? | `covers` / `exceeds` | ~20 | identical |
| how many can I sell? | `quantize_down` | 3 | **the bug fix** |

Only the last class changes behaviour. That is the point.

### Two ladder problems with opposite invariants

They must not be conflated.

- **Exit split** must sum to the whole position or the position never closes.
  Allocate in integer step units, distribute the remainder by largest fractional
  remainder, and make the final tranche a **close-remaining intent** resolved at
  fire time from live owned — which removes the stranded-residue class outright.
- **Entry ladder** must never exceed the notional budget, and each tier has a
  *different price*, so share units are not a common currency across tiers.
  Redistributing a remainder would push the last tier past the gross guard.
  Floor each tier and **report** the leftover, turning today's invisible loss
  into an operator-visible number.

## 4. What this does NOT buy

It removes the impossibility of trading a fractional venue. It does not make one
work. That additionally needs the `FractionalOrderEnabled` order path and a
re-derived fee model — `MIN_COMMISSION_USD` is per order, which is brutal at
fractional sizes, and the #1112 cost gate would refuse nearly every tranche.
Explicitly out of scope.

## 5. Costs accepted

- **A research cohort boundary.** New policy constants enter
  `execution_config_version()`, and ADR 0013 R3 makes that forward-only: rows
  either side must never pool in any live-fill analysis. Not undone by reverting
  code.
- **Test surface.** ~1868 tests under `tests/brokers/`; roughly 400-600 need a
  lattice fixture threaded through, ~40-60 need assertions rewritten. The lever
  is one `WHOLE_SHARE_LATTICE` fixture that lets every venue-agnostic test keep
  its exact current expectations.
- **`exec_quality` parquet** `planned_qty` moves int64 → float64 and the `int()`
  truncation goes; existing snapshots need a one-time rewrite.

## 6. Staging

| | scope | reversible |
|---|---|---|
| P0 | this memo | trivially |
| P1 | close the acceptance-harness false green (test double deletes sub-half-share positions) | yes |
| P2 | the `tranche_pct` 100x unit defect — rename, never re-interpret | yes |
| P3 | the inert `broker_contract/quantity.py` leaf | yes, dead code |
| P4 | adapter verification at the wire; no-op today | yes |
| P5 | the redesign | **indivisible** |

P5 cannot ship half-on: six modules import `QTY_PRECISION`, and the two answers
("is 0.669 real") meet inside `execute_tranche_exit` — the planner would say
sell 0.669 while the protection pass says owned is 0.

## 7. Supersession

[`saxo_fx_leg_gpw_design_2026_07_18.md`](saxo_fx_leg_gpw_design_2026_07_18.md)
§4 item 6 concluded:

> Integer shares (WSE LotSizeType NotUsed, MinimumTradeSize 1, IncrementSize 1,
> FractionalOrderEnabled false) — the existing floor already satisfies this; no
> new lot handling.

That reasoning was correct **for the question it asked** — whether the GPW FX
leg needed lot handling. It is superseded as a general statement for two reasons
found by measurement, neither visible from the FX-leg question:

1. "The existing floor already satisfies this" is true only while the venue
   reports `IncrementSize 1`. The floor does not *read* that field — it assumes
   it. Nothing in the code would notice a venue that reported otherwise.
2. The floor is not the only quantizer. The exit path uses `round()`, which
   rounds **up**, and that is a live-money defect on any fractional holding
   regardless of lot handling.

The linked memo is left unedited; this document carries the newer conclusion.
