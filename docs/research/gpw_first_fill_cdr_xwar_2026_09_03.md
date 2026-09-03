# GPW first-fill experiment — CDR@XWAR on Saxo SIM (Milestone V)

**Date:** 2026-09-03, attended session 09:02–09:52 CEST (WSE continuous trading).
**Design:** `docs/research/saxo_fx_leg_gpw_design_2026_07_18.md` §5 (the acceptance
test for the #1238 venue arc). **Account:** SIM EUR (CashBalance ~977k EUR,
`IsCurrencyConversionAtSettlementTime=true`, EndOfDay netting). **Instrument:**
CD Projekt (CDR), Uic 53932, XWAR, PLN. **Verdict: PASS** — all 7 checklist
observations recorded, no silent currency misread, no stop-and-write-up anomaly.

## Timeline

| CEST | Event |
|------|-------|
| 09:03 | Branch `feature/venue-xwar-enable` deployed to the VPS, all three broker units restarted |
| 09:0x | `arm-manual CDR --mic XWAR --env sim --dry-run` probe: resolve + compile OK |
| ~09:15 | Entry #1: BUY 2 @ limit 233.0 (`step_a_entry.py --naked`) — stayed WORKING (CDR rallied ~+2.3% off the open; delayed ref quote was 15 min stale) |
| 09:33 | Standalone `precheck_bracket_order` @ 237.0 — observation 4 captured |
| ~09:40 | Entry #2: BUY 2 @ limit 240.0 — **filled @ 237.50** (order 5039998272 later CANCELLED cleanly) |
| ~09:44 | Close: SELL 2 @ limit 232.0 (`step_c_close.py`) — **filled @ 237.00** |
| 09:45–09:50 | Reconcile, closed-positions read, negative FX controls |

## Observations vs checklist

1. **SIZING / journal stamps** — the G2 scratch drivers (`step_a_entry.py` /
   `step_c_close.py`) bypass the sizing path (explicit qty) and journal records
   with **all schema-2 FX fields null** (`fx_rate*`, `instrument_currency`,
   `sizing_currency`, `sizing_equity` — a documented driver limitation, not a
   defect). The production stamp path is evidenced by (a) the RHI@LIVE
   `watch_open` of 2026-09-02 carrying `fx_rate 0.2676` +
   `instrument_currency USD` + `sizing_currency PLN` through the same
   venue-agnostic code, and (b) the #1238 PR-chain unit tests pinning the XWAR
   stamps. The `arm-manual CDR --mic XWAR` compile echoed correct tiers / R-DSL
   TPs / notional-over-frame sizing.
2. **NEGATIVE CONTROLS** — a REAL `broker.get_fx_rate("EUR","PLN")` quote
   (mid 4.32795, Tradable/Tradable) was accepted by `build_fx_conversion`; three
   degraded variants derived from that same live quote were each REFUSED with
   `TradeSetupNotPlannableError`: `PriceTypeBid=OldIndicative` (accepted-set
   refusal), `Mid=None` (no-usable-mid refusal), and a 600 s-old `asof`
   (staleness refusal, max 300 s). Refuse-to-size demonstrably fires on live
   data, not only in unit tests.
3. **TICK + QTY** — limits 233.0 / 240.0 / 232.0 all legal on CDR's WSE band
   (tick 0.1 in the ≤499.9 range), integer qty, zero tick adjustments, no
   `_MAX_TICK_ADJUSTMENT_BPS` trip, no venue reject.
4. **PRECHECK** — `EstimatedCashRequiredCurrency: "EUR"` observed verbatim.
   `InstrumentToAccountConversionRate 0.2311` (→ 4.3271 EURPLN) vs our sizing
   quote 4.32795 — divergence 0.02 %, inside the bound. Arithmetic exact:
   2 × 237 PLN × 0.2311 + 17.33 EUR commission = 126.87 EUR
   (`EstimatedTotalCostInAccountCurrency`); `EstimatedCashRequired` 144.17 EUR
   carries ~14 % margin buffer. **No `PreTradeDisclaimers`** — the
   cross-currency order did NOT require a dm/v2 acknowledgement on SIM.
5. **FILL + CONVERSION BOOKING** — filled 2 @ 237.50 (plausible vs the delayed
   236.6/236.9 reference — the SIM fill was not at our 240 limit, i.e. not
   naively synthetic). **EUR cash did NOT move** (977 302.79 before = after);
   instead `/port/v1/exposure/currency/me` grew a **PLN line: −550.00**
   (2×237.5 + 75 commission) — the conversion genuinely waits for T+2
   settlement, and the pre-settlement liability is visible as PLN exposure.
   This answers the design memo's open question.
6. **CLOSEDPOSITIONS CURRENCY FIELDS** — the read WORKED on this EOD-netting
   account (note: the daemon's reconcile path has seen
   `ClosedPositionNotAccessibleInEndOfDayNettingMode` on a different call
   shape — not reproduced here). Closed row: ClosingPrice 237.0,
   `ProfitLossOnTrade −1.00 PLN` vs `ProfitLossOnTradeInBaseCurrency
   −0.231007 EUR` — the ratio reconstructs **4.3289**, equal to the position's
   `ConversionRateOpen` (0.2310055⁻¹) to 7 significant digits.
   `ProfitLossCurrencyConversion 0.0` (no FX move intra-experiment).
   `ConversionRateInstrumentToBaseSettledOpening/Closing` behave as **booleans,
   both `false`** pre-settlement; the T+2 flip is a follow-up re-poll
   (2026-09-05). Post-close exposure: PLN −151.00 = 2 × 75 commission + 1 PLN
   trade loss — the books balance to the złoty.
7. **RECONCILE** — entry 5039998479 verdicts **`FILLED(closed)` — "round trip
   closed (FIFO pair)"** on the cross-currency row; the abandoned first entry
   verdicts CANCELLED; the naked close reconciles `r=None` by design (no stop
   on record). The close-side row carries the expected "audit log says FILLED
   but no open position or closed pair matched" note — the manual
   opposite-close pattern has no pair of its own (known shape, step_c
   docstring).

## Additional findings

- **SIM WSE fee card is fictional:** 75 PLN commission per side on a 474 PLN
  trade (~15.8 %). Same phenomenon as SIM US ($0.02/sh min $15). Per the design
  memo: do NOT calibrate any conversion/fee model from SIM deltas.
- **LIVE REST infoprice serves GPW quotes delayed 15 min without the P1
  entitlement** (`DelayedByMinutes: 15`, `PriceSource: WSE`, `MarketState`
  correct) — a usable reference feed for XWAR while SIM equity infoprices stay
  `NoAccess` (price-dark, as designed around).
- **G2 tooling interaction:** the scratch drivers journal submission records
  keyed `(ticker, brief_date)`; a same-day `arm-manual` pick for the same
  ticker then resolves as PLACED and never drains — the production
  watch-routing could not be exercised for CDR today. Inherent to sharing one
  env between experiment tooling and the daemon; documented, not fixed.
- A 15-minute-delayed reference quote is a real operational constraint: CDR
  rallied ~+2.3 % in the first 10 minutes and the first "marketable" limit
  (delayed ask + 0.6 %) was ~1.6 % under the market by the time it rested.

## Caveats (stamped per design memo)

SIM fill realism for NoAccess exchanges is undocumented — this session
validates PLUMBING + CURRENCY BOOKKEEPING only. No conversion-cost model may
be calibrated from these numbers. LIVE XWAR remains gated on the #1235
prerequisites (P1 entitlement, `ALPHALENS_SAXO_STREAM_SESSION_VENUES`, fee
confirmation).

## Follow-ups

- Re-poll the closed CDR pair on/after 2026-09-05 (T+2): confirm the
  `ConversionRateInstrumentToBaseSettled*` booleans flip and the EUR cash
  debit books.
- #1252: rename the journal `brief_date` key to `trade_date` (operator
  confusion observed live during this session).
