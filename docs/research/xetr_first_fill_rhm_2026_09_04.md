# Xetra first-fill experiment — RHM@XETR on Saxo SIM (#1280)

**Date:** 2026-09-04, attended session 14:55–15:10 CEST (Xetra continuous
trading). **Arc:** #1271 (code merged + deployed 2026-09-03: #1275 map + alias,
#1277 fee cards by MIC, #1278 tracked stream venues, #1279 `SUPPORTED_MICS`).
**Precedent:** `docs/research/gpw_first_fill_cdr_xwar_2026_09_03.md` (#1245).
**Account:** SIM EUR (CashBalance ~949k EUR, total ~1.002M EUR, EndOfDay
netting). **Instrument:** Rheinmetall, journals say `RHM`, Saxo side
`RHMG:xetr`, Uic 16135, EUR. **Verdict: PASS with one blocker and one bug.**
The alias, the venue plumbing, the same-currency booking and the reconcile all
behave; the DAEMON path (watch / now tranche / never-naked) is unreachable
until the LIVE market-data account carries a Xetra real-time entitlement, and
the attempt exposed a feed-scope leak (#1315).

## Timeline

| UTC | Event |
|-----|-------|
| 12:54 | SIM daemon env verified: `ALPHALENS_SAXO_STREAM_SESSION_VENUES=XNYS,XWAR,XETR`, `ENTRY_TRAIL_BPS=50`, frame 100000 |
| 12:55 | `arm-manual RHM --mic XAMS … --dry-run` refused (negative control, observation 6) |
| 12:55 | `arm-manual RHM --tier now@1065:50 --tier 1055:50 --stop 1000 --tp 1072.5:100 --notional 4400 --frame 100000 --mic XETR --env sim` — compiled and armed (blend 1060, 1R = 60, TP 0.21R, 4.40 % of frame) |
| 12:55:46 | First SIM tick: `now tranche RHM: no real-time quote (feed off/outage/halt/stale) — deferred` — the only line the daemon ever logged for the pick |
| 12:59 | Reader socket `quote uic 16135`: bid 1045 / ask 1045.4, `delayed_by_minutes: 15`; `quote uic 19756014` (QUBT): `delayed_by_minutes: 0` — the delay is per-venue entitlement, not a session demotion |
| 13:02:01 | G2 driver `step_a_entry.py --ticker RHM --mic XETR --qty 1 --entry 1049.9 --naked` (limit = delayed ask × 1.006) — order 5040025802, **filled @ 1039.80**, position 5027375572 |
| 13:02:11 | SIM daemon: `alert: uic 16135: long 1.0 open but no journaled disaster-stop plan — cannot protect` |
| ~13:02:30 | `broker disarm RHM --date 2026-09-04 --env sim` (reason recorded: reader serves RHMG delayed-15) |
| 13:03:43 | `step_c_close.py --ticker RHM --mic XETR --qty 1 --limit 1034.0` — order 5040026607, **filled @ 1038.80** |
| 13:06 | Standalone `precheck_bracket_order` (BUY 1 @ 1030) — observation 4 captured |
| 13:05–13:08 | Reader gauges: `subscribed_uics 16`, `any_delayed 1`; uic 16135 still subscribed 6 minutes after the disarm |
| 13:09:27 | SIM daemon restarted to release its feed scopes → 16135 gone, `any_delayed 0`, `subscribed_uics 15` |

## Observations vs the #1280 checklist

1. **RESOLVE VIA THE ALIAS** — `broker resolve RHM --exchange XETR` (SIM
   gateway) → `RHMG:xetr`, broker_id 16135, EUR, Stock. The `arm-manual`
   compile echoed `RHM @ XETR` and the G2 driver's request carried
   `broker_symbol RHMG:xetr`, `currency EUR`, `exchange_mic XETR`. The alias
   pin (#1275) holds end to end; journals speak RHM, the wire speaks RHMG.
2. **PLACEMENT / WATCH UNDER THE DAEMON POLICY — BLOCKED (entitlement).**
   The shared LIVE price reader serves RHMG with `DelayedByMinutes: 15` (the
   same shape as GPW without P1: the LIVE market-data account has no Xetra
   real-time entitlement). The now tranche's marketability gate needs an
   undelayed quote, so the pick sat in a silent `DEFER` every tick (one
   throttled alert, then `PENDING` in `broker picks`); the pullback tier's
   watch never opened either. Exactly the "inert, not dangerous" failure mode
   the #1279 runbook promises for placement — no crash, no blind fill. The
   day-1 gate did not come into play (SIM runs without the flag). The daemon
   path stays untested on Xetra until the entitlement exists.
3. **FILL COVERED BY THE STANDALONE DISASTER STOP — NOT REACHED; the
   never-naked pass behaved correctly for a foreign position.** The G2 entry
   is a naked probe with no journaled plan; within 10 s of the fill the SIM
   daemon alerted `long 1.0 open but no journaled disaster-stop plan — cannot
   protect`. That is the designed reaction to a position it did not place
   (alert, never a stop it invents), the same shape the XWAR run saw.
4. **COST STAMPS / FEE CARD — driver limitation, card verified by code.** The
   G2 drivers bypass sizing and journal `est_round_trip_fee_bps: null`, all
   FX fields null, `mic: XETR`, `uic: 16135` (documented in the XWAR report;
   not a defect). The daemon-side stamp path (`exchange_mic` on the
   `tranche_plan` line, `_FEE_CARD_BY_MIC["XETR"]` = `saxo-pl-classic-xetr`,
   0.08 % min EUR 3) is pinned by the #1277 unit tests and was not exercised
   live for the reason in 2. **SIM commission observed: 12.00 EUR per side**
   on a ~1040 EUR trade (~1.15 %) — fictional, same phenomenon as SIM WSE
   (75 PLN) and SIM US ($15 min). Precheck `Commission: 12.0`,
   `ExchangeFee: 0.0`, `StampDuty: 0.0`. Do not calibrate the EUR card from
   SIM.
5. **SIZING / FX LEG — same-currency case.** Instrument EUR on an EUR SIM
   account: precheck `EstimatedCashRequiredCurrency: "EUR"`,
   `InstrumentToAccountConversionRate: 1.0`, `EstimatedTotalCost 1042.0`
   (1 × 1030 + 12 commission), `EstimatedCashRequired 1054.0` (~1.2 %
   buffer), `PreCheckResult: Ok`, **no `PreTradeDisclaimers`**. Closed row:
   `ProfitLossOnTrade −1.00` = `ProfitLossOnTradeInBaseCurrency −1.00`,
   `ProfitLossCurrencyConversion 0.0`, and — the contrast with XWAR —
   `ConversionRateInstrumentToBaseSettledOpening/Closing` **both `true`
   immediately** (XWAR's cross-currency pair showed both `false` pending T+2,
   #1253). No `ConversionRateOpen` field at all on the same-currency row. The
   LIVE account is PLN, so a LIVE Xetra fill WILL carry the EUR→PLN leg
   (0.25 % Saxo FX); this SIM run could not exercise it.
6. **NEGATIVE CONTROL** — `--mic XAMS` refused at compile: `MIC 'XAMS' is not
   supported (supported: XNYS, XNAS, XWAR, XETR; XAMS awaits its own
   validation arc, #1238)`. Zero I/O, as designed.
7. **SIM FEES ARE FICTIONAL** — restated above (12 EUR/side). Also: the SIM
   fill printed **below the delayed bid** (1039.80 vs delayed 1043/1043.6 at
   12:46, limit 1049.9), and the close filled at 1038.80 against a 1034
   limit — SIM fills are not naively synthetic at the limit, consistent with
   the XWAR observation.

**Reconcile:** entry 5040025802 → `FILLED(closed) — round trip closed (FIFO
pair)`; the close 5040026607 → `FILLED` with the known "audit log says FILLED
but no open position or closed pair matched" note (manual opposite-close has
no pair of its own — step_c docstring). Closed row `ClosingMethod: Fifo`,
`CostOpening −12.0`, `CostClosing −12.0`.

## Additional findings

- **Feed-scope leak on disarm (#1315, real bug).** A now tranche that
  returns `DEFER` deliberately keeps its per-uic subscription
  (`now-entry:16135`), and `_release_scope()` runs only on the PLACED /
  REFUSED paths. `broker disarm` removes the pick from the drain, so the
  scope is never released. On the SHARED reader one delayed quote flips
  `QuoteCache.any_delayed()` process-wide: gauge
  `alphalens_live_price_stream_any_delayed=1` from 12:55Z to 13:09Z (arms
  `AlphalensPriceReaderDelayed`, for 5m, Telegram — a false "session
  demoted" page), and the session-reclaim retry loop fired twice (14:55:51,
  15:00:52 CEST — 2 of the 4/hour budget) for a demotion that never happened.
  Per-uic US quotes stayed `delayed_by_minutes: 0`, so the LIVE daemon's
  gates were unaffected this time. Cleared by restarting the SIM daemon
  (connection close releases every scope). Corollary for the runbook: an
  XWAR/XETR arm without the entitlement is inert for PLACEMENT but not for
  the reader.
- **Same-currency booking settles at once.** Observation 5: the
  `…Settled*` booleans are `true` on the EUR/EUR row immediately, which
  makes the pending-`false` state on XWAR (#1253) specifically the
  cross-currency case, not a SIM artefact.
- **G2 journal-key collision reproduced** (XWAR follow-up #1252): the
  driver's `(ticker, trade_date)` record shares the key with the same-day
  `arm-manual` pick, so the pick was disarmed BEFORE the driver records
  landed to keep `broker picks` honest.

## Caveats (stamped per the design memo)

SIM fill realism for NoAccess / delayed venues is undocumented — this session
validates alias + venue plumbing + same-currency bookkeeping + reconcile only.
No conversion-cost or fee model may be calibrated from these numbers. The
daemon's Xetra path (watch, now tranche, never-naked stop, cost stamps, day-1
gate anchored on the XETR calendar, the EUR→PLN FX leg on a PLN account) is
NOT validated by this run.

## LIVE XETR — what is still gated

1. **Xetra real-time market-data entitlement on the LIVE account** — the
   blocker for both the SIM daemon path (it reads the LIVE reader) and LIVE.
   Without it every Xetra quote is delayed-15 and every tier is vetoed.
2. Saxo Classic tier confirmation (fee card assumption, #1277).
3. After the entitlement: repeat this experiment on SIM through the daemon
   (mixed `now@cap` + pullback tier) to capture observations 2–4 for real,
   then the attended LIVE first fill.

## Follow-ups

- #1315 — release `now-entry:<uic>` on disarm / TTL expiry (or sweep stale
  scopes at the top of each drain pass).
- #1253 — the XWAR T+2 re-poll; compare against the immediate `true` seen here.
- Runbook line for XWAR/XETR prerequisites: add the reader-side effect of an
  unentitled arm (false delayed page + reclaim budget) until #1315 lands.
