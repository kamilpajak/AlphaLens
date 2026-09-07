# XPAR first-fill experiment — KER@XPAR on Saxo SIM (Milestone V, #1355)

**Date:** 2026-09-07, attended session 13:11–13:20 UTC (Euronext Paris continuous
trading, 07:00–15:30 UTC). **Design:** the XETR report's protocol
(`docs/research/xetr_first_fill_rhm_2026_09_04.md`), on the venue opened by the
#1355 arc (PR-A #1356 map + fee card, PR-B #1357 stream window, PR-C #1358
arm-manual + entitlement probe; deployed 12:47–12:50 UTC the same day).
**Account:** SIM EUR (CashBalance ~909k EUR, EndOfDay netting). **Instrument:**
Kering (KER), Uic 398681, XPAR, EUR — Saxo symbol root equals the market ticker,
no alias. **Verdict: PASS** — every checklist observation recorded, no silent
currency misread, one design-level finding (already tracked, #1354), zero
defects.

## Timeline (UTC)

| Time | Event |
|------|-------|
| 12:47–12:50 | PR-B/PR-C deployed: stream window `XNYS,XWAR,XETR,XPAR` on all three units, reader + daemons restarted, drift-check `converged` at 12:50:18 |
| 13:11 | Negative control: `arm-manual KER … --mic XAMS --dry-run` refused (`supported: XNYS, XNAS, XWAR, XETR, XPAR`) |
| 13:12:01 | `arm-manual KER --tier 230 --tier 225 --stop 210 --tp 1R:100 --notional 1000 --frame 100000 --mic XPAR --env sim` — compiled (blend 227.5, 1R 17.5, TP1 245) and armed |
| 13:12:11 | SIM daemon: `place_pick KER: routed into entry-trail watch (2 tier(s), d=50bps)` — two `watch_open` lines, one `entry-trail watch opened` submission record |
| 13:12:25 | Reader: `Saxo LIVE session reclaimed (1/4 this hour)` — the watch subscribed the delayed uic (see finding 1) |
| 13:14:39 | `broker disarm KER --date 2026-09-07 --env sim` — 2 watch tiers cancelled; daemon retracted the tranche plan; reader 17→16 uics, `any_delayed` 1→0 within one tick |
| 13:16:14 | G2 driver `step_a_entry.py --ticker KER --mic XPAR --qty 1 --entry 242.50 --naked` (limit = delayed ask 241.0 × 1.006) — order 5040071475, **filled @ 240.85**, position 5027388866 |
| 13:16:20 | SIM daemon: `alert: uic 398681: long 1.0 open but no journaled disaster-stop plan — cannot protect` (6 s after the fill; expected for a naked probe) |
| 13:17:14 | `step_c_close.py --ticker KER --mic XPAR --qty 1 --limit 239.45` (delayed bid 240.9 × 0.994) — order 5040071476, **filled @ 240.65** |
| 13:17:25 | SIM daemon: `audit log says FILLED but no open position or closed pair matched` on the close's client_request_id (the known manual-opposite-close shape) |
| 13:18–13:20 | Closed-positions, balances, exposure and a standalone precheck read |

## Observations vs checklist

1. **SIZING / journal stamps** — the production route stamped the venue end to
   end: `watch_open` lines carry `exchange_mic XPAR`, `uic 398681`,
   `instrument_currency EUR`, `sizing_currency EUR`, `fx_rate null`; the
   submission record carries `mic XPAR`, `sizing_equity 100000`,
   `est_round_trip_fee_bps 65.93`. The null FX rate is CORRECT here: the SIM
   account is EUR, so there is no conversion leg (the PLN→EUR leg exists only
   on the PLN-denominated LIVE account). The fee estimate reproduces from the
   XPAR card exactly: entry 2 tiers × max(EUR 2, 0.08 % × ~460) = 4 EUR, exit
   one TP tranche max(EUR 2, 0.08 % × 980) = 2 EUR, 6 EUR / 910 EUR gross =
   65.93 bps. (The Xetra card would give 9 / 910 = 98.9 bps — so the MIC-keyed
   lookup, not the EUR currency fallback, priced this pick. Checked because
   65.93 also equals 2 × 3 / 910, a numeric coincidence.)
2. **NEGATIVE CONTROL** — `--mic XAMS` refused at compile with the new supported
   list; XAMS remains map + card only.
3. **TICK + QTY** — limits 242.50 / 239.45 legal on the XPAR tick scheme (0.05
   in the 199.98–499.95 band), integer qty, no tick adjustment, no venue
   reject. Both fills landed INSIDE the limits (240.85 vs 242.50; 240.65 vs
   239.45), i.e. not naively synthetic at-limit fills.
4. **PRECHECK** — `precheck_bracket_order` BUY 1 @ 235 with SL 210 / TP 245:
   `PreCheckResult Ok`, `EstimatedCashRequiredCurrency "EUR"`,
   `InstrumentToAccountConversionRate 1.0`, `EstimatedTotalCost 247.0` (235 +
   12 commission), `EstimatedCashRequired 259.0`, no `PreTradeDisclaimers`.
5. **FILL + BOOKING** — same-currency pair: `CashBalance` unchanged
   (909 370.49) with `TransactionsNotBooked −24.2` (2 × 12 commission + 0.20
   trade loss) pending T+2; `/port/v1/exposure/currency/me` shows a single EUR
   line (909 346.29 = cash − 24.2) and NO foreign-currency exposure line —
   the PLN line of the CDR@XWAR experiment was the cross-currency case, absent
   here by construction.
6. **CLOSEDPOSITIONS CURRENCY FIELDS** — the read worked on this EOD-netting
   account: `OpenPrice 240.85`, `ClosingPrice 240.65`, `ProfitLossOnTrade −0.2`
   = `…InBaseCurrency −0.2`, `ProfitLossCurrencyConversion 0.0`,
   `CostOpening/Closing −12.0` each, `ClosingMethod Fifo`, and BOTH
   `ConversionRateInstrumentToBaseSettled*` booleans `true` immediately — the
   same-currency shape the #1253 re-poll predicted (the flags are a
   conversion-state marker; with nothing to convert they are trivially settled).
   No T+2 re-poll is needed for this pair: the only pending item is the cash
   booking of the 24.2 EUR, which has no open question.
7. **RECONCILE** — the entry's naked position raised the "cannot protect" alert
   within 6 s; the close carried the expected "no open position or closed pair
   matched" note (manual opposite-close pattern, no pair of its own). Reader
   gauges untouched by the drivers (16 uics / `any_delayed` 0 throughout the
   fill).

## Additional findings

- **Finding 1 — reader reclaim burn via the entry-watch scope (#1354, second
  observation).** The pullback tiers routed into entry-trail watches; the watch
  subscribed the delayed uic and the shared reader burnt one session reclaim
  14 s after the arm. The subscription was released correctly on disarm (#1353
  holds for this scope too). The reader-side question of #1354 applies to
  every scope that subscribes a delayed uic, not only `now-entry`. Until the
  LIVE entitlement lands, an XPAR arm on either instance costs reclaims while
  pending — the runbook's "prefer pullback tiers" advice limits duration, not
  the burn.
- **SIM Euronext fee card is fictional:** 12 EUR per side on a ~241 EUR trade
  (~5 %), the same phenomenon as SIM Xetra (12 EUR) and SIM US. The
  `XPAR_FEE_CARD` prior (0.08 % min EUR 2) can only be verified on LIVE with a
  read-only precheck (#1359 step 0). Never calibrate from SIM deltas.
- **The delayed-15 reference quote is usable for driver limits** (as on XETR):
  the entry limit at delayed ask × 1.006 filled 0.7 % under the limit; Kering
  moved little in the 15-minute lag.
- **LIVE entitlement:** the tracked probe during the same session reported
  `KER@XPAR delayed 15 (PAR)`, `ALO@XPAR delayed 15`, `RHM@XETR delayed 15`,
  `CDR@XWAR delayed 15`, `AAPL@XNAS inconclusive (market Closed)`, exit 4. The
  Saxo-side subscription is the owner's decision, #1359.

## Caveats (stamped per the design memos)

SIM fill realism for NoAccess exchanges is undocumented — this session
validates PLUMBING + CURRENCY BOOKKEEPING only (and, for a same-currency
account, the ABSENCE of a conversion leg). No conversion-cost or fee model may
be calibrated from these numbers. LIVE XPAR remains gated on #1359 (Euronext
real-time entitlement, fee-card precheck, Classic tier).

## Follow-ups

- #1359: LIVE Euronext Paris entitlement (owner decision) + step 0 fee-card
  precheck on LIVE.
- #1354: reader-side reclaim burn while a delayed uic is legitimately
  subscribed (entry-watch and now-entry scopes alike).
