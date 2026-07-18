# Saxo FX leg for GPW (XWAR/PLN) sizing — design memo

**Status:** LOCKED (operator decisions recorded below, 2026-07-18)
**Date:** 2026-07-18
**Related:** [`saxo_broker_layer_design_2026_07_17.md`](saxo_broker_layer_design_2026_07_17.md) (P1/P2/P3 decision records),
[`saxo_first_fill_experiment_2026_07_18.md`](saxo_first_fill_experiment_2026_07_18.md) (KO@XNYS runbook),
[ADR 0013](../adr/0013-trade-side-layers.md), [ADR 0014](../adr/0014-broker-agnostic-execution-layer.md)

> **NO code changes ship with this memo.** It is the design that unblocks the
> `routing.py` "XWAR stays explicit-only until the PLN/FX-leg sizing question
> is designed" deferral (P2 memo §8 Q3). Implementation starts only after the
> operator answers §7.

## 1. Context — why this memo exists

- **GPW candidates exist in briefs**, but the execution layer cannot size
  them: `compute_setup_plan` divides an account-currency notional by an
  instrument-currency price with no conversion in between.
- **XWAR is resolvable since P1** — `_MIC_TO_SAXO_EXCHANGE_ID` maps
  `XWAR→WSE` (codes confirmed against the live `/ref/v1/exchanges` read) and
  `CDR @ XWAR` resolves to Uic 53932 on this SIM (P1 memo, WSE-on-SIM
  coverage CONFIRMED). Routing deliberately keeps XWAR out of the US probe
  order; it enters only via explicit `--exchange XWAR`.
- **The CLI currently does not refuse non-USD venues — known issue from
  P2.** `alphalens broker submit --exchange XWAR` today would read EUR
  equity (`AccountSnapshot.currency == "EUR"` on this SIM), compute an
  "EUR" notional, and divide it by PLN tier limits — a silent ~4.3× oversize.
  Nothing in the submit path compares instrument currency against account
  currency, because `InstrumentRef` does not carry a currency at all.
- The thematic pipeline is untouched by design: the theme-mapper prompt is
  US-only by construction
  (`alphalens_pipeline/thematic/mapping/theme_mapper.py:82` — "Output 5 to
  15 candidate U.S.-listed common stocks"), so XWAR is an explicit-operator
  path, not a production candidate flow.

## 2. Saxo mechanics findings (live-read evidence, SIM)

All reads performed against the SIM environment via the existing
`saxo/client.py` read-only escape hatch (`get_json`); no orders placed.

1. **Instrument search rows carry `CurrencyCode`.** The
   `/ref/v1/instruments` search response that `resolve_instrument` already
   consumes includes `CurrencyCode` per row — live-verified `CDR@XWAR`
   returns `CurrencyCode: PLN`. Stamping instrument currency costs ZERO
   extra API calls.
2. **FX pair resolution:** `/ref/v1/currencypairs` maps `EUR→PLN` to
   **Uic 1343** (live-verified). The lookup is one-directional — always look
   up from the base side; fall back to a Keywords search if the pair is not
   listed under the base.
3. **FX infoprices work on this SIM even on weekends.** `GET
   /trade/v1/infoprices?Uic=1343&AssetType=FxSpot` returns Quote
   Bid/Ask/Mid + `PriceTypeBid/Ask` + `MarketState` (SBFX source, no
   exchange entitlement needed) — unlike ALL stock infoprices, which are
   NoAccess on this unlinked SIM.
4. **`LastUpdated` is NOT a data-age signal.** Live probe on a CLOSED
   Saturday market showed `LastUpdated` equal to the request second.
   Freshness must be judged from `PriceType` instead (`OldIndicative` is the
   documented weekend/no-market state).
5. **Weekend EURPLN spread observed ~0.32%** (Bid 4.3331 / Ask 4.3469) —
   sizing off Ask would systematically undersize; mid is the sizing input.
6. **Precheck carries a second, independent rate.** `POST
   /trade/v2/orders/precheck` returns `EstimatedCashRequired` +
   `EstimatedCashRequiredCurrency` + `InstrumentToAccountConversionRate`
   (direction: instrument→account, i.e. PLN→EUR).
7. **Settlement-time conversion:** this SIM account has
   `IsCurrencyConversionAtSettlementTime=true` — the EUR debit is fixed at
   the T+2 settlement rate, not at order time. Order-time sizing is
   inherently exposed to ±2 days of EURPLN drift.
8. **`ClosedPosition` does NOT expose the settlement rate.** The
   `ConversionRateInstrumentToBaseSettledOpening/Closing` fields are
   **BOOLEANS** (live-verified gotcha), not rates. Effective conversion is
   only reconstructable as
   `ProfitLossOnTrade / ProfitLossOnTradeInBaseCurrency`.
9. **WSE order mechanics:** integer shares (`LotSizeType` NotUsed,
   `MinimumTradeSize` 1, `IncrementSize` 1, `FractionalOrderEnabled`
   false); BANDED tick scheme from instrument details (16 bands; CDR's
   current range ≤499.9 → tick 0.1 PLN); Market orders are DayOrder-only on
   WSE; continuous trading 09:00–16:50 CEST with the closing auction in the
   last 10 minutes.
10. **Account side is already covered:** `/port/v1/balances` returns
    `Currency` ("EUR" on this SIM, CashBalance 1,000,000), already surfaced
    as `AccountSnapshot.currency`.

## 3. Currency-assumption inventory (scout findings, file:line)

Where the codebase currently assumes one currency, verified at HEAD
(`60f3f523`):

| Site | Assumption |
|------|------------|
| `alphalens_pipeline/paper/sizing.py:169`, `:202` | docstrings pin `paper_equity: live paper-account equity in USD` |
| `alphalens_pipeline/paper/sizing.py:219` | `total_notional = final_size_pct / 100.0 * float(paper_equity)` — account-currency notional, no conversion seam |
| `alphalens_pipeline/paper/sizing.py:230` | `qty = max(0, floor(tier_notional / limit))` — silently assumes notional and tier limit share a currency |
| `alphalens_pipeline/paper/sizing.py:276-280` | `setup_plan_gross_notional` (instrument ccy, sums qty×limit) is compared by callers against `GROSS_SAFETY_FRAC × equity` (account ccy) |
| `alphalens_pipeline/paper/constants.py:33` | `DEFAULT_PAPER_EQUITY_USD` — USD-named program convention (test-only consumer) |
| `alphalens_pipeline/brokers/contract.py:59-73` | `InstrumentRef` has NO currency field |
| `alphalens_pipeline/brokers/contract.py:78` | `AccountSnapshot.currency` exists — the account side needs no new broker read |
| `alphalens_pipeline/brokers/saxo/broker.py:163` | account currency read from balances but never consumed downstream |
| `alphalens_pipeline/brokers/saxo/broker.py:176-218` | `resolve_instrument` discards the search row's `CurrencyCode` |
| `alphalens_cli/commands/broker.py:374-375` | `--equity` help text: "Sizing equity in account currency" (contract already stated, math not enforced) |
| `alphalens_cli/commands/broker.py:430` | `sizing_equity = broker.get_account().total_value` — EUR on this SIM, fed straight into `compute_setup_plan` |
| `alphalens_cli/commands/broker.py:448` | equity echo has no currency label |
| `alphalens_cli/commands/broker.py:466-472` | precheck `EstimatedCashRequired` echoed without its currency |
| `alphalens_pipeline/brokers/reconcile.py:68` | `_DEFAULT_EXCHANGE_MIC = "XNYS"` TTL-sweep fallback |
| `alphalens_pipeline/brokers/reconcile.py:126-147` | `compute_realized_r` — pure price ratio, currency cancels (NOT an FX bug; confirmed untouched) |
| `alphalens_pipeline/brokers/reconcile.py:473` | `ProfitLossOnTrade` recorded without a currency label |
| `alphalens_pipeline/brokers/routing.py:12-15` | XWAR explicit-only "until the PLN/FX-leg sizing question is designed" — the deferral this memo lifts |
| `alphalens_pipeline/paper/calendar.py:40-41` | docstring names this exact missing layer: "per-exchange currency / FX (XNYS in USD; XWAR in PLN — sizing math would need an FX leg)" |
| `alphalens_pipeline/thematic/mapping/theme_mapper.py:82` | briefs are US-only by prompt construction — no multi-venue candidate flow to design for |

## 4. Design

### 4.1 Architecture

The FX leg lives in exactly TWO places, mirroring betlejem5's NotionalSizer
seam: (1) a Saxo-ADAPTER-side rate fetch, (2) a broker-AGNOSTIC conversion
applied at the sizing notional→qty boundary. Nothing else learns about
currencies beyond labels.

LAYERING:

- **Broker Protocol (`contract.py`) stays frozen and currency-naive.**
  `BracketOrderRequest` remains prices+qty in instrument currency; the
  decomposer (`execution.py::decompose_setup_plan`) stays pure
  price-driven. ONE additive dataclass field: `InstrumentRef.currency: str`
  (ISO code, e.g. `"PLN"`), populated by the Saxo adapter from the
  `/ref/v1/instruments` search row's `CurrencyCode` in `resolve_instrument`
  (`broker.py:176-218` — the search response already carries it, zero extra
  API calls; live-verified CDR@XWAR returns CurrencyCode PLN). This is
  authoritative instrument currency, NEVER inferred from MIC.
  `AccountSnapshot.currency` (`contract.py:78`, `broker.py:163`) already
  supplies the account side — no new broker read.
- **Rate fetch = concrete `SaxoBroker` method, NOT a Protocol member.**
  `SaxoBroker.get_fx_rate(base: str, quote: str) -> FxRateQuote` — resolve
  pair Uic via `/ref/v1/currencypairs` (EUR→PLN = Uic 1343, live-verified;
  lookup is one-directional so always look up from the base side, fall back
  to Keywords search), then `GET /trade/v1/infoprices AssetType=FxSpot`,
  return Quote.Mid + Bid/Ask + PriceTypeBid/Ask + MarketState + fetch
  timestamp. FX infoprices work on this SIM even weekends (SBFX source, no
  exchange entitlement — unlike ALL stock infoprices which are NoAccess).
  CLI capability-checks via `hasattr` → `BrokerCapabilityError` for brokers
  without it. This keeps the frozen Protocol untouched (hard requirement 3).
- **Conversion = new frozen dataclass `FxConversion`** (account_currency,
  instrument_currency, rate: float meaning instrument-ccy-per-account-ccy,
  source, price_type, bid, ask, asof) consumed by
  `compute_setup_plan(…, fx: FxConversion | None = None)` in
  `paper/sizing.py`. The conversion happens between sizing.py's
  total_notional line (:219) and the qty division (:230): account-ccy
  notional × rate → instrument-ccy notional → ÷ tier.limit. Prices are
  NEVER converted — they stay instrument-native for the broker POST, WSE
  tick quantization, and realized_r (exactly the betlejem5 doctrine:
  convert the notional first, divide by native price).
- **Policy constants live in `execution.py`**, NOT sizing.py — because
  `tests/brokers/test_execution_config_version.py` namespace-sweeps
  execution.py, so any new `_UPPER_CASE` FX constant is AUTOMATICALLY
  forced into both the hash and `_COVERED_CONSTANTS` (R3 poolability for
  free; sizing.py has no version stamp of its own and gains none).
  Dependency direction is preserved: execution.py already imports from
  paper.sizing; sizing does not import execution — the CLI orchestrates
  (reads constants from execution.py, fetches the rate from the adapter,
  builds `FxConversion`, passes it into `compute_setup_plan`).
- **Poolability/config-version impact:** new constants join
  `execution_config_version()` → the digest changes → forward-only cohort
  boundary at deploy (existing KO-era rows keep the old token, correct:
  they were sized under an FX-unaware policy). Journal record gains fx keys
  → `_STAMP_SCHEMA` bumps `"1"`→`"2"` per submission_log.py's frozen-shape
  doctrine (key ADD = schema bump, by the book).
- **Orchestration (`alphalens_cli/commands/broker.py` submit):**
  get_account → resolve_instrument (now carrying `.currency`) → if
  `instrument.currency == account.currency`: fx=None, byte-exact today's US
  path → else: `get_fx_rate(account_ccy, instrument_ccy)` → build
  `FxConversion` or refuse → `compute_setup_plan(fx=…)` → precheck
  cross-check (§4.3) → place.
- **Pipeline/parquet/Django: ZERO changes.** Briefs are US-only by prompt
  construction (`theme_mapper.py:82`); XWAR enters only via explicit
  `--exchange XWAR`. Consistent with ADR 0013 R2 — no execution data flows
  upstream.

### 4.2 Sizing flow — budget (EUR) → instrument-ccy notional → integer qty

1. **Equity read (EUR):** CLI submit takes
   `sizing_equity = broker.get_account().total_value` with
   `AccountSnapshot.currency = "EUR"` (`broker.py:163`). The `--equity`
   override is documented as account-currency by contract (help text
   already says so, `commands/broker.py:374-375`).
2. **Instrument resolve (currency stamped):**
   `resolve_instrument("CDR", "XWAR")` → InstrumentRef(uic 53932, exchange
   WSE, currency="PLN" from the search row's CurrencyCode). Currencies
   differ → FX leg activates. (KO@XNYS resolves currency="USD" == "USD"…
   on a USD account; on THIS EUR SIM account even US names need the FX
   leg — see open question 1.)
3. **Rate fetch (adapter):** `SaxoBroker.get_fx_rate("EUR", "PLN")` →
   `/ref/v1/currencypairs` maps EUR→{PLN, Uic 1343} →
   `GET /trade/v1/infoprices?Uic=1343&AssetType=FxSpot` →
   FxRateQuote(mid=4.34, bid=4.3331, ask=4.3469,
   price_type="Indicative"/"Tradable", asof=now). PriceType outside the
   accepted set, fetch error, or unresolvable pair →
   `TradeSetupNotPlannableError`, no order, no fallback.
4. **Account-ccy notional (unchanged math):** inside `compute_setup_plan`,
   `total_notional_account = final_size_pct/100 × paper_equity` — EUR.
   (SetupPlan keeps this as `total_notional_account_ccy` for the journal.)
5. **THE conversion (the one new line of math):**
   `total_notional_instrument = total_notional_account × fx.rate ×
   (1 − _FX_SIZING_BUFFER_PCT/100)`. E.g. EUR 5,000 × 4.34 × 0.99 ≈
   PLN 21,483. When fx is None (same currency) this is the identity —
   today's US path byte-exact. Prices are NOT converted, ever.
6. **Per-tier qty (existing code, now currency-consistent):**
   `tier_notional = total_notional_instrument × alloc_pct/100`;
   `qty = max(0, floor(tier_notional / tier.limit))` — PLN ÷ PLN. Integer
   shares (WSE LotSizeType NotUsed, MinimumTradeSize 1, IncrementSize 1,
   FractionalOrderEnabled false) — the existing floor already satisfies
   this; no new lot handling. Zero-qty tiers keep the existing skip-log
   policy.
7. **Gross guard, single currency:** `setup_plan_gross_notional` (PLN) ≤
   `GROSS_SAFETY_FRAC × paper_equity × fx.rate` — same rate object, no
   refetch.
8. **Tick quantization (downstream, price-space, untouched logic):** tier
   limits snap to the WSE BANDED tick scheme from instrument details (16
   bands; CDR's current range ≤499.9 → tick 0.1 PLN), guarded by the
   existing `_MAX_TICK_ADJUSTMENT_BPS=25` hard-fail — bps math is
   currency-free, but note the 25bps calibration was reasoned for US
   ticks; CDR at ~500 PLN border (tick 0.2, ~4bps) still fits comfortably,
   so the constant stands.
9. **Precheck gate (EUR side closes the loop):** precheck returns
   `EstimatedCashRequired` in `EstimatedCashRequiredCurrency` (expect EUR)
   + `InstrumentToAccountConversionRate`. Assert currency == account
   currency; assert rate agreement within
   `_FX_PRECHECK_RATE_DIVERGENCE_MAX_PCT`; compare EstimatedCashRequired
   against CashAvailableForTrading (both EUR, live-verified). Any failure →
   refuse, journal the refusal.
10. **Place + journal:** `BracketOrderRequest` unchanged (PLN prices +
    qty); journal record stamps both currencies, the verbatim rate +
    provenance + precheck rate, sizing_equity, under `_STAMP_SCHEMA "2"`
    and the new `execution_config_version` token.

### 4.3 Rate guardrails (verbatim)

1. REFUSE-TO-SIZE on missing rate (betlejem NotionalSizer doctrine,
   verbatim): if instrument.currency != account.currency and no
   FxConversion is available (fetch failed, pair unresolvable, PriceType
   unacceptable), raise TradeSetupNotPlannableError — NEVER a silent 1.0
   fallback, never a static hardcoded rate (betlejem's markets.json
   fx_to_usd static map is explicitly NOT stolen; every sizing uses a live
   quote). Same-currency path is a strict no-op preserving today's US
   sizing byte-exact. Policy constant: `_MISSING_FX_RATE_POLICY = "reject"`
   in execution.py (auto-joins execution_config_version).
2. STALENESS: the rate is fetched SYNCHRONOUSLY inside the submit flow, per
   submission — no cross-invocation caching, so wall-clock age is seconds
   by construction; a belt constant `_FX_RATE_MAX_AGE_S = 300` rejects any
   in-process reuse older than 5 min. CRITICAL: Saxo's LastUpdated is NOT a
   data-age signal — live-probe showed LastUpdated == request-second on a
   CLOSED Saturday market — so freshness is judged from PriceType instead:
   accepted set `_FX_ACCEPTED_PRICE_TYPES = ("Tradable", "Indicative")`;
   OldIndicative (documented weekend/no-market state), NoAccess, or absent
   PriceType → REFUSE. Since GPW submits happen inside 09:00-16:50 CEST
   anyway, EURPLN will be Tradable/Indicative in practice; the check exists
   for the operator-error case (weekend dry-run against a stale market).
3. MID-ONLY SIZING: use Quote.Mid, never Ask/Bid (weekend spread observed
   ~0.32%: 4.3331/4.3469 — sizing off Ask would systematically undersize;
   Saxo itself converts at mid ± markup). Bid/Ask are still captured on
   FxRateQuote and journaled for spread diagnostics. Constant:
   `_FX_RATE_SOURCE = "saxo-fxspot-infoprice-mid"`.
4. NO MIC-INFERRED CURRENCY: instrument currency comes ONLY from Saxo's
   instrument-data CurrencyCode stamped onto InstrumentRef.currency at
   resolve time. No `_MIC_TO_CURRENCY` map (the scout's alternative is
   rejected: a map is a second source of truth that can drift; Saxo already
   tells us). If CurrencyCode is missing from the search row →
   InstrumentNotFoundError-class refusal, not a guess.
5. PRECHECK CROSS-CHECK (second independent rate source): the mandatory
   precheck (already `_PRECHECK_REQUIRED = True`) response carries
   EstimatedCashRequiredCurrency + InstrumentToAccountConversionRate.
   Submit asserts (a) EstimatedCashRequiredCurrency ==
   AccountSnapshot.currency (else refuse — the account model is not what we
   think), and (b) |InstrumentToAccountConversionRate⁻¹ vs our sizing rate|
   divergence ≤ `_FX_PRECHECK_RATE_DIVERGENCE_MAX_PCT` (proposed 2.0%) —
   beyond that the infoprice snapshot and Saxo's own conversion rate
   disagree materially → refuse placement. Mind the direction: precheck's
   rate is instrument→account (PLN→EUR), ours is account→instrument.
6. SETTLEMENT-DRIFT HONESTY: IsCurrencyConversionAtSettlementTime=true
   means the EUR debit is fixed at T+2 settlement rate, not order time —
   order-time sizing is inherently ±2 days of EURPLN drift. Guardrail is a
   sizing BUFFER, not precision: `_FX_SIZING_BUFFER_PCT` (proposed 1.0%
   haircut on the converted instrument-ccy notional, covering the ≤0.25%
   global conversion markup + drift) applied before the qty floor. The
   floor-to-integer-shares then absorbs the rest. Buffer value is an open
   question (§7); the constant lives in execution.py either way.
7. GROSS GUARD IN ONE CURRENCY: setup_plan_gross_notional (instrument ccy,
   sums qty×limit) must no longer compare raw against GROSS_SAFETY_FRAC ×
   equity (account ccy) — the planner converts equity through the SAME
   FxConversion.rate before the compare. One rate per plan; no second fetch
   (two fetches could straddle a tick and disagree with the journal).
8. RATE PROVENANCE IN THE JOURNAL, VERBATIM: every submission record gains
   sizing_currency, instrument_currency, fx_rate (null when currencies
   equal — a real null is auditable, a fake 1.0 masquerades as a quote),
   fx_rate_bid, fx_rate_ask, fx_rate_price_type, fx_rate_source (e.g.
   "saxo-fxspot-uic-1343-mid"), fx_rate_asof (fetch timestamp UTC),
   sizing_equity, and precheck_conversion_rate (Saxo's
   InstrumentToAccountConversionRate, the cross-check value).
   `_STAMP_SCHEMA` bumps to "2". The journal is the only place the sizing
   rate survives — ClosedPosition does NOT expose the settlement rate (the
   ConversionRateInstrumentToBaseSettled* fields are BOOLEANS, a
   live-verified gotcha), so post-hoc slippage analysis reconstructs
   effective rate from ProfitLossOnTrade vs ProfitLossOnTradeInBaseCurrency
   against the journaled sizing rate.
9. R3 ENFORCEMENT IS AUTOMATIC: all of `_FX_RATE_SOURCE`,
   `_FX_ACCEPTED_PRICE_TYPES`, `_FX_RATE_MAX_AGE_S`,
   `_MISSING_FX_RATE_POLICY`, `_FX_CONVERSION_POINT`
   ("notional-before-qty"), `_FX_PRECHECK_RATE_DIVERGENCE_MAX_PCT`,
   `_FX_SIZING_BUFFER_PCT` are module-level `_UPPER_CASE` in execution.py —
   the namespace-sweep test fails the build unless each joins
   `execution_config_version()` and `_COVERED_CONSTANTS`. Rows sized under
   different FX policy can never silently pool.

### 4.4 Surface changes (implementation PR scope, NOT this PR)

- `apps/alphalens-pipeline/alphalens_pipeline/brokers/contract.py` —
  `InstrumentRef` gains `currency: str` (additive field; Protocol methods
  untouched). `AccountSnapshot` unchanged (already has currency).
  `BracketOrderRequest` UNCHANGED — stays prices+qty, currency-naive.
- `apps/alphalens-pipeline/alphalens_pipeline/brokers/saxo/broker.py` —
  `resolve_instrument` populates `InstrumentRef.currency` from the search
  row's CurrencyCode (refuse if absent); new concrete method
  `get_fx_rate(base, quote) -> FxRateQuote` via `/ref/v1/currencypairs` +
  `/trade/v1/infoprices` (FxSpot); NOT added to the Broker Protocol.
- `apps/alphalens-pipeline/alphalens_pipeline/brokers/saxo/client.py` —
  thin read-only additions if not already covered by `get_json`:
  `get_currency_pairs()`, `get_fx_infoprice(uic)` (the P-probe already
  exercised both endpoints through the `get_json` escape hatch).
- `apps/alphalens-pipeline/alphalens_pipeline/paper/sizing.py` —
  `compute_setup_plan` gains keyword-only `fx: FxConversion | None = None`;
  conversion applied between the total_notional line and the per-tier qty
  division; `SetupPlan` gains `total_notional_account_ccy` + fx provenance
  fields (or carries the FxConversion); docstrings drop the "USD" pin in
  favour of "account currency"; new frozen dataclass `FxConversion` (may
  live in sizing.py or a small `paper/fx.py` — it is pure data, no I/O,
  betlejem's static-compute-core testability). Refuse path reuses
  `TradeSetupNotPlannableError`.
- `apps/alphalens-pipeline/alphalens_pipeline/brokers/execution.py` — new
  policy constants `_FX_RATE_SOURCE`, `_FX_ACCEPTED_PRICE_TYPES`,
  `_FX_RATE_MAX_AGE_S`, `_MISSING_FX_RATE_POLICY`, `_FX_CONVERSION_POINT`,
  `_FX_PRECHECK_RATE_DIVERGENCE_MAX_PCT`, `_FX_SIZING_BUFFER_PCT` + they
  join the `execution_config_version()` dict; `_STAMP_SCHEMA` "1"→"2"
  (journal shape change). `decompose_setup_plan` UNTOUCHED (pure
  prices+qty). `tests/brokers/test_execution_config_version.py`'s namespace
  sweep enforces coverage automatically.
- `apps/alphalens-pipeline/alphalens_pipeline/brokers/submission_log.py` —
  record shape gains sizing_currency, instrument_currency, fx_rate (null on
  same-ccy no-op), fx_rate_bid, fx_rate_ask, fx_rate_price_type,
  fx_rate_source, fx_rate_asof, sizing_equity, precheck_conversion_rate
  (documented as the `_STAMP_SCHEMA`-2 shape per the frozen-shape doctrine
  at :11).
- `apps/alphalens-pipeline/alphalens_cli/commands/broker.py` — submit
  orchestration: currency compare → `get_fx_rate` (capability-checked
  `hasattr`) → FxConversion → `compute_setup_plan(fx=…)`; precheck
  currency + rate-divergence asserts; DISPLAY: equity echo (:448) labeled
  with `AccountSnapshot.currency`, plan echo shows both notionals
  ("EUR 5,000.00 → PLN 21,483 @ 4.3400 mid"), precheck summary (:466-472)
  labels EstimatedCashRequired with EstimatedCashRequiredCurrency verbatim.
- `apps/alphalens-pipeline/alphalens_pipeline/brokers/reconcile.py` —
  `compute_realized_r` (:126-147) CONFIRMED UNTOUCHED:
  `r = (close−entry)/(entry−stop)` is a pure price ratio, all three inputs
  instrument-currency (journal setup prices + Saxo instrument-ccy
  ClosePrice), currency cancels. Changes are label/diagnostic-only:
  ProfitLossOnTrade in verdict details (:473) labeled with
  instrument_currency from the journal record; NEW diagnostic when
  ProfitLossOnTradeInBaseCurrency is present — effective settlement rate :=
  ProfitLossOnTrade / ProfitLossOnTradeInBaseCurrency, recorded next to the
  journaled sizing fx_rate as the realized-vs-sized FX cross-check
  (remember: ConversionRateInstrumentToBaseSettledOpening/Closing are
  BOOLEANS, not rates — never read them as numbers). Optional hardening:
  the :68 `_DEFAULT_EXCHANGE_MIC="XNYS"` TTL-sweep fallback is now
  safe-by-construction since submit always stamps mic; note it, don't
  change it.
- `apps/alphalens-pipeline/alphalens_pipeline/brokers/routing.py` — NO
  functional change in this PR; update the docstring (:12-15) from "until
  the FX-leg sizing question is designed" to point at this memo. XWAR
  stays explicit-only (`--exchange XWAR`).
- UNTOUCHED, by design: Broker Protocol method set,
  `decompose_setup_plan`, `BracketOrderRequest`, brief parquet schema,
  theme_mapper (US-only prompt stands), Django/SPA,
  `paper/constants.py::DEFAULT_PAPER_EQUITY_USD` (test-only consumer;
  renaming is a follow-up under open question 1), `paper/calendar.py`
  (already exchange-parametrized; its docstring :40-41 naming this exact
  missing FX layer gets updated to point here).
- Tests (TDD-first, `unittest.TestCase` per CI discipline): sizing FX
  no-op byte-exactness (same-ccy plan identical to pre-change golden),
  EUR→PLN conversion + buffer + floor cases, refuse-on-missing-rate /
  refuse-on-bad-PriceType / refuse-on-stale, gross-guard single-currency
  compare, journal schema-2 shape, config-version drift pin for each new
  constant (the sweep test forces this anyway), reconcile effective-rate
  diagnostic, CLI precheck currency/divergence refusal paths.

## 5. Validation plan — GPW first-fill experiment (CDR@XWAR)

The acceptance test for this design. Explicitly a SEPARATE attended session
AFTER the KO@XNYS first-fill experiment; do not combine (KO validates the
bracket plumbing, this validates ONLY the FX delta on top of proven
plumbing, so any anomaly attributes to the FX leg).

**Setup:** attended, during WSE CONTINUOUS trading 09:00–16:50 CEST only
(NOT "09:00–17:00" — the last 10 minutes are the closing auction where
Limit orders behave differently; also Market orders are DayOrder-only on
WSE, and summer is CEST/UTC+2). Tiny size: qty 1–2 shares of CDR (~500 PLN
≈ EUR 115–230 at ~4.34), Limit order priced to fill promptly,
`ALPHALENS_BROKER_ALLOW_ORDERS=1`, EUR SIM account (CashBalance 1,000,000,
IsCurrencyConversionAtSettlementTime=true).

**Observation checklist** (each item journaled or screen-captured):

1. SIZING: EURPLN rate fetched from Uic 1343, PriceType ∈ {Tradable,
   Indicative} during market hours; CLI echoes "EUR X → PLN Y @ rate mid";
   journal record carries fx_rate/bid/ask/price_type/source/asof +
   sizing_currency=EUR + instrument_currency=PLN + sizing_equity, under
   `_STAMP_SCHEMA` 2 and the new execution_config_version token.
2. NEGATIVE CONTROLS (before the real order): (a) dry-run with the FX
   fetch monkeypatched/blocked → TradeSetupNotPlannableError, NO order,
   refusal visible; (b) weekend/off-hours dry-run earlier → OldIndicative
   or wide-spread handling per policy. These prove refuse-to-size fires in
   the wild, not just in unit tests.
3. TICK + QTY: tier limit snapped to CDR's WSE band (tick 0.1 in the
   ≤499.9 range); integer qty; no `_MAX_TICK_ADJUSTMENT_BPS` trip.
4. PRECHECK: EstimatedCashRequiredCurrency == "EUR" (assert observed
   verbatim); EstimatedCashRequired plausible vs qty×limit×~(1/4.34);
   InstrumentToAccountConversionRate within the divergence bound of the
   sizing rate — RECORD Saxo's rate next to ours; note any
   PreTradeDisclaimers (cross-currency orders may require dm/v2
   acknowledgement — if one blocks placement, that is a FINDING, not a
   failure).
5. FILL + CONVERSION BOOKING: after fill, `/port/v1/balances` — EUR
   CashAvailableForTrading reduced; whether the EUR cash debit books
   immediately or at T+2 settlement (IsCurrencyConversionAtSettlementTime=
   true says settlement — observe actual SIM behaviour, it is
   undocumented); `/port/v1/exposure/currency/me` — does a PLN line appear
   pre-settlement?
6. CLOSEDPOSITIONS CURRENCY FIELDS (close the position same session or
   next): ProfitLossOnTrade in PLN vs ProfitLossOnTradeInBaseCurrency in
   EUR; reconstruct effective conversion rate = ratio, compare to
   journaled sizing fx_rate and to precheck's
   InstrumentToAccountConversionRate; observe ProfitLossOnTradeConversion
   (FX component isolated); confirm
   ConversionRateInstrumentToBaseSettledOpening/Closing behave as BOOLEANS
   and flip at settlement (re-poll at T+2).
7. RECONCILE: compute_realized_r on the journaled PLN entry/stop + Saxo
   PLN close produces a sane R (all-instrument-currency inputs — the
   scale-free claim verified on a real cross-currency row); TTL sweep uses
   the XWAR calendar (mic stamped on the record).

**Hard caveats stamped on the experiment memo:** SIM fill realism for
NoAccess exchanges is UNDOCUMENTED (stock infoprices are NoAccess on
unlinked SIM — the fill price may be synthetic), so this session validates
PLUMBING + CURRENCY BOOKKEEPING ONLY; do NOT calibrate a conversion-cost
model from SIM deltas (whether SIM applies the 0.25% markup is
undocumented — ask openapisupport@saxobank.com or measure on live).
Success = all 7 observations recorded with no silent currency misread; any
rate divergence beyond bounds or an unexpected currency field value is a
stop-and-write-up, not a patch-and-retry.

## 6. Out of scope

- Multi-venue candidate generation: theme_mapper stays US-only by prompt;
  no exchange/currency columns on the brief parquet; no Django/SPA changes
  (ADR 0013 R2 — no execution data upstream).
- routing.py changes: XWAR remains explicit-only via `--exchange XWAR`;
  adding XWAR to any probe order is a follow-up decision AFTER the
  first-fill experiment passes (only the comment pointing at the old
  blocker is updated).
- Conversion-COST MODELING: no attempt to model the 0.25% (or
  entity-tiered up to 0.60%) markup as a cost-model input — SIM markup
  behaviour is undocumented; real calibration waits for live fills or a
  Saxo support answer. The sizing buffer is a safety haircut, not a cost
  estimate.
- Currency SUB-ACCOUNTS: this SIM has a single EUR account; opening PLN
  sub-accounts (a live-retail feature) and any cash-management between
  them is not designed.
- FX HEDGING of the T+2 settlement drift or of open PLN P&L exposure —
  explicitly accepted as noise at 1-2-share experiment scale.
- Broker Protocol extension for FX: get_fx_rate stays a Saxo-adapter
  concrete method; promoting it to the Protocol waits for a second broker
  that needs it (extract-on-second-use doctrine).
- Non-Saxo FX rate sources (ECB reference rates, yfinance EURPLN=X) — one
  canonical rate source per the one-client-per-vendor doctrine; Saxo
  prices its own conversion, so Saxo's rate is the only defensible sizing
  input.
- Renaming DEFAULT_PAPER_EQUITY_USD / auditing its test-only consumers —
  folded into open question 1's resolution, separate small PR.
- Journal BACK-migration: existing schema-1 records are not rewritten;
  readers treat absent fx keys as the same-currency no-op era
  (forward-only, standard cohort-boundary handling).
- Live (non-SIM) account work, market-data entitlement linking, and the
  SIM stock-quote NoAccess limitation — acknowledged, not solved.

## 7. Open questions FOR THE OPERATOR — these BLOCK implementation

### Operator decisions (2026-07-18 — unblocks implementation)

1. **Budget currency = ACCOUNT currency** (Q1, the proposed convention):
   the budget IS whatever ``AccountSnapshot.currency`` says; the story is
   "same-currency = no-op" (byte-exact preservation of today's sizing when
   currencies match), and on THIS EUR SIM account USD instruments take the
   FX path too. The USD-program alternative is rejected.
2. **GPW implementation approved** (Q2): the FX+venue plumbing is worth the
   attended-session cost; the CDR@XWAR validation session stays a SEPARATE
   slot after KO@XNYS.
3. **``_FX_SIZING_BUFFER_PCT`` = 1.0** (Q3, as proposed); the buffer applies
   ONLY when an FxConversion is active — the same-currency path takes 0%.
4. Q4 (entity fee schedule): unanswered; does not block — the buffer covers
   the worst documented markup tier.
5. Q5 (divergence bound): 2.0% adopted as proposed; divergence is a HARD
   refuse (not a journal-warning) on SIM.
6. Q6 (reconcile surface): permanent-but-nullable adopted as proposed
   (``effective_settlement_rate`` verdict detail, null for same-ccy rows).
7. Q7 (pre-trade disclaimers): NOT built into the submit flow; a blocking
   DisclaimerToken during the experiment is a FINDING handled manually in
   the Saxo UI.

> The questions below are retained verbatim for the record; the design
> assumed the stated proposals and the decisions above lock them.

1. **BUDGET CURRENCY CONVENTION (the big one):** the program's mental model
   is USD (DEFAULT_PAPER_EQUITY_USD, all research in USD terms) but the SIM
   account is EUR. Proposed convention: the budget IS the account currency,
   whatever AccountSnapshot.currency says — sizing_equity comes from the
   broker in EUR, suggested_size_pct is a ratio so nothing upstream cares,
   and the journal stamps sizing_currency so analyses never guess.
   Consequence the OPERATOR must accept: on THIS account even KO@XNYS is
   cross-currency (EUR budget, USD instrument) — the FX leg would activate
   for US names too, changing the "US = no-op" story to "same-currency =
   no-op". Alternative: keep a USD program convention and convert EUR
   equity→USD first (two rates per PLN trade — rejected as needless
   complexity, but it preserves USD-denominated comparability across
   accounts). OPERATOR DECIDES; the design above assumes account-currency
   convention.
2. **IS GPW EXECUTION WANTED NOW AT ALL?** The selection A/B
   (mechanical-vs-LLM V-forward) and the EDGE maturation are the live
   research; GPW adds a venue with zero production candidates (briefs are
   US-only) purely to exercise the FX+venue plumbing. Honest framing: this
   is infrastructure optionality, not research need. Operator should
   confirm the attended-session cost is worth it before the KO
   experiment's follow-up slot is spent on CDR.
3. **SIZING BUFFER VALUE (`_FX_SIZING_BUFFER_PCT`):** proposed 1.0%
   (covers ≤0.25% global conversion markup + ~2 days EURPLN drift, and
   EURPLN daily vol is ~0.3-0.5%). Too big wastes budget at floor-qty
   granularity on small notionals; too small risks precheck/cash
   rejections. Also: should the buffer apply to the SAME-currency path (0%
   today) — proposed no (buffer only when fx active).
4. **ENTITY FEE SCHEDULE:** which Saxo entity backs this SIM/eventual-live
   account, and is its conversion markup the global 0.25% or tiered (UK
   moved to 0.60/0.40/0.20% Nov 2025)? The trading-conditions API exposes
   CurrencyConversion.Markup per instrument/account — worth one read-only
   probe; affects only the buffer sizing and future cost modeling, not the
   design.
5. **PRECHECK DIVERGENCE BOUND (`_FX_PRECHECK_RATE_DIVERGENCE_MAX_PCT`):**
   proposed 2.0% — wide enough for infoprice-mid vs Saxo-conversion-rate
   methodology differences plus intraday moves, tight enough to catch a
   wrong-pair or inverted-rate bug (which would show ~1800% off for
   EURPLN). Is 2% the right tradeoff, and is divergence a hard refuse or a
   journal-warning on SIM?
6. **RECONCILE VERDICT SURFACE:** should the reconstructed effective
   settlement rate (ProfitLossOnTrade / ProfitLossOnTradeInBaseCurrency)
   be a permanent verdict-details field (grows the verdict shape for a
   diagnostic that is null for all US rows) or a
   first-fill-experiment-only observation? Proposed: permanent but
   nullable — cheap, and it is the ONLY empirical FX-slippage signal
   available (ClosedPosition does not expose the rate).
7. **PRE-TRADE DISCLAIMERS:** if the CDR precheck returns a blocking
   DisclaimerToken (plausible on cross-currency/foreign-exchange orders),
   does the operator want dm/v2 acknowledgement built into the submit
   flow, or is a manual one-time acknowledgement in the Saxo UI acceptable
   for the experiment?
