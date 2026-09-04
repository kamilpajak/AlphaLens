# Does the entry-trail G1 ceiling bind? — measurement, 2026-09-04

**Verdict: no.** 8 of the 23 entry-trail fires on record executed ABOVE the
`StopLimitPrice` ceiling their own order carried, by up to 53.5 bps. Saxo
documents that field for `OrderType = StopLimit` only. Issue: #1317.

**Status:** measurement complete; the G1 remedy is an open decision.

## Why this was asked

`entry_trailing_design_2026_08_12.md` §3 G1 (CRITICAL) adopted "option B": put a
`StopLimitPrice` on the same native `TrailingStopIfTraded` order so a fire
"never [becomes] a naked market order through a gap". The premise came from the
§4b addendum of 2026-08-13, which read a RESTING order back and found the field
retained.

On 2026-09-04 the AMBA LIVE fire filled at 59.00 against an armed ceiling of
58.9504. A same-day adversarial review of the first diagnosis withdrew two
overstated claims and left exactly one order as evidence. This memo is the
measurement that replaces it.

## Method

The armed ceiling was never written to the journal — it existed only in the
daemons' log line. So the table is a JOIN on the broker order id between two
sources, both read on 2026-09-04 from the VPS:

- **ceiling and trigger** — `entry-trail <label>: armed native trailing order
  <id> @ trigger T (ceiling C)`, from
  `journalctl --user -u alphalens-broker-manager.service -u alphalens-broker-manager-live.service --since 2026-08-01`;
- **realized fill** — the terminal `fired` records in
  `~/.alphalens/broker_orders/{sim,live}/entry_trails.jsonl`.

Every fire on record joins (23 of 23, no unmatched rows). The join is frozen in
`apps/alphalens-research/tests/incident_1317_fixture.py` because journald
rotates: without the snapshot the measurement cannot be repeated.

**A ceiling must not be reconstructed.** The obvious shortcut —
`would_be_trigger x (1 + CEILING_EPS_FRAC)` off the `fired` blob — reproduces
the arm line exactly on 22 of the 23 rows and is wrong by 0.3324 on BAH, because
`would_be_trigger` is derived from the MINIMUM trough ever journaled while the
arm used the trough as it stood at arm time. Taking the shortcut would have
reported BAH's overshoot as 92 bps instead of 47.

## The 23 fires

| crid | env | order id | trigger | ceiling | fill | vs ceiling |
|---|---|---|---|---|---|---|
| MRVI-2026-08-26-entry-t0 | SIM | 5039891030 | 8.4018 | 8.4186 | 8.2000 | -259.7 bps |
| MARA-2026-08-25-entry-t0 | SIM | 5039891044 | 11.6178 | 11.6410 | 11.5200 | -103.9 bps |
| PSNL-2026-08-26-entry-t0 | SIM | 5039891053 | 16.6629 | 16.6962 | 16.7300 | **+20.2 bps** |
| IMCR-2026-08-26-entry-t0 | SIM | 5039891931 | 36.4714 | 36.5444 | 36.7400 | **+53.5 bps** |
| MARA-2026-08-25-entry-t1 | SIM | 5039893228 | 11.0952 | 11.1174 | 11.0500 | -60.6 bps |
| IBRX-2026-08-25-entry-t0 | SIM | 5039893270 | 8.0903 | 8.1064 | 8.0800 | -32.6 bps |
| PL-2026-08-26-entry-t0 | SIM | 5039895886 | 20.5322 | 20.5732 | 20.4600 | -55.0 bps |
| MRVI-2026-08-26-entry-t1 | SIM | 5039899803 | 7.8591 | 7.8748 | 7.8000 | -95.0 bps |
| SAIC-2026-08-26-entry-t0 | SIM | 5039902058 | 125.6652 | 125.9165 | 126.0900 | **+13.8 bps** |
| GME-2026-08-27-entry-t0 | SIM | 5039898744 | 18.1001 | 18.1363 | 18.0200 | -64.1 bps |
| IOVA-2026-08-26-entry-t0 | SIM | 5039932464 | 7.8088 | 7.8245 | 7.7700 | -69.7 bps |
| LPX-2026-08-27-entry-t0 | SIM | 5039933050 | 69.6465 | 69.7858 | 69.8400 | **+7.8 bps** |
| PFSI-2026-08-25-entry-t0 | SIM | 5039932448 | 73.1137 | 73.2600 | 73.1200 | -19.1 bps |
| IBRX-2026-08-25-entry-t1 | SIM | 5039948009 | 7.7888 | 7.8043 | 7.7900 | -18.3 bps |
| PL-2026-08-26-entry-t1 | SIM | 5039954464 | 19.2457 | 19.2842 | 19.1800 | -54.0 bps |
| BAH-2026-08-26-entry-t0 | SIM | 5039978418 | 74.5006 | 74.6497 | 75.0000 | **+46.9 bps** |
| PL-2026-08-26-entry-t2 | SIM | 5040006542 | 18.3412 | 18.3779 | 18.3900 | **+6.6 bps** |
| RHI-2026-09-03-entry-t1 | SIM | 5040027337 | 42.1497 | 42.2340 | 42.3400 | **+25.1 bps** |
| OLN-2026-08-16-entry-t0 | LIVE | 5435139849 | 18.7031 | 18.7405 | 18.5574 | -97.7 bps |
| SMG-2026-08-19-entry-t0 | LIVE | 5436761165 | 60.0689 | 60.1890 | 59.9261 | -43.7 bps |
| GME-2026-08-27-entry-t0 | LIVE | 5438283280 | 18.1001 | 18.1363 | 18.0200 | -64.1 bps |
| RHI-2026-09-02-entry-t0 | LIVE | 5439436793 | 42.8733 | 42.9590 | 42.8700 | -20.7 bps |
| AMBA-2026-09-04-entry-t0 | LIVE | 5440084826 | 58.8327 | 58.9504 | 59.0000 | **+8.4 bps** |

LIVE 1 of 5, SIM 7 of 18.

## The second line of evidence

Saxo's own documentation, read 2026-09-04:

- order-events reference — `StopLimitPrice`: "Secondary price level for
  StopLimit orders", available when `OrderType = StopLimit`.
  `TrailingStopDistanceToMarket`: "Distance to market for a trailing stop
  order", available for `TrailingStop` / `TrailingStopIfTraded`.
- `POST /trade/v2/orders` placeable-order-type schema lists `TrailingStop` and
  `TrailingStopIfTraded`; there is no placeable `TrailingStopLimit`.

So the field belongs to a different order type. On the trailing type it is
accepted, stored and echoed on read-back — which is exactly what the §4b probe
saw — and not applied at execution.

## Caveats, stated separately

- **The SIM rows are weak on their own.** SIM fills are synthetic: the
  2026-08-07 probe filled a deep-through limit and a near-touch limit at the
  SAME reference price. The 18 SIM rows show SIM's engine ignoring the field,
  which is not a statement about the real matching engine.
- **The LIVE row is a single order.** AMBA, 5 ticks over. It is corroborated by
  two independent reads (the audit `ExecutionPrice` and the position `avg`, with
  consistent P&L arithmetic), but it is n=1.
- **Documentation is not an observation.** It says what the field is for, not
  what the gateway did.

Each of the three is insufficient alone; together they point one way, and none
of them points the other. That is the basis for acting.

## What changed in code

Placement is UNCHANGED — the ceiling is still sent. What changed is that the
question is now answerable without a manual join:

- `PlacedOrder.stop_limit_price` reports the tick-quantized limit the adapter put
  on the wire, so the caller journals what was SENT;
- the `trail_armed` line carries `ceiling`, and the fold exposes it as
  `EntryTrailTierState.armed_ceiling`;
- every `fired` line carries `ceiling` plus a `ceiling_breach` block;
- a breach raises one throttled alert on its own key
  (`entry-trail:ceiling-breach:<crid>`), worded as a measurement.

`ceiling_breach: null` with a non-null `ceiling` means the fill was at or under
the cap. `ceiling: null` means the comparison could not be made — NO VERDICT,
which is not a pass. Every fire before 2026-09-04 is in that state.

**One caveat before counting breaches out of the journal.** There are TWO
writers of a `fired` line. The reconcile pass resolves the disappeared order
through the audit log, so it has an execution price and can produce a verdict.
The G6 cancel-then-verify path — a tier that suspended or expired while its
resting order turned out filled — learns the fill from the open-orders re-read,
which carries a QUANTITY and no execution price. Those lines therefore stamp a
known `ceiling` beside a permanently null `ceiling_breach`. They are real fills
that the breach measurement cannot speak about, not clean ones, and a count over
the journal must exclude them from the denominator rather than treat them as
passes.

## Open decision (not taken here)

1. Accept market fires, bounded by the mandatory DayOrder rule (no trailing
   order lives through an overnight or auction gap; the residual is an intraday
   gap or halt-reopen).
2. Revisit V3' — a resting `StopLimit`, whose `StopLimitPrice` IS the documented
   one, amended by the bot. §2 rejected it for d_eff inflation; that trade-off
   was weighed against a ceiling that does not exist.
3. Bound the pay-up some other way.

Every future LIVE fire now measures itself, so this decision accumulates
evidence at no additional risk.

## Consequence to keep in view

`entry_trail_geometry.entry_fill_estimate` returns this ceiling, and the #1112
cost gate (`arms_inside_exit_region`) prices against it as an upper bound on the
fill. On this sample that bound was exceeded on 35% of fires. The docstring no
longer claims enforcement; changing what the gate prices against belongs with
the remedy decision, not here.

## Method lesson

A PREPARED state was read as a verdict about a TERMINAL one: the §4b probe read
a resting order and was taken as evidence about how it would FILL. Recorded in
`CLAUDE.md` under "A check that cannot refute you has tested nothing" (PR #1318).
