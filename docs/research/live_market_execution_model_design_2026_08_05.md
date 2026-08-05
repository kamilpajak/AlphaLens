# Live-market execution model — design

**Status:** DRAFT (increment-by-increment build)

The auto-manager holds only a disaster stop at the broker; entries (E) and
take-profit tranches (TP) are realized as market orders fired off a live price
stream, not as resting Limit/OCO orders. The engine consumes a source-agnostic
price feed (`broker_contract.price_feed.PriceFeed`): `latest(uic)` returns a
fresh `PricePoint` or `None`, and `None` is the stream-health veto ("do not
fire").

## Increments

- **INC-1** — market-order adapter (`SupportsMarketOrders`). Shipped (#988).
- **INC-2** — the live `PriceFeed` source (see below).
- **INC-3** — live TP-tranche exit engine over the feed. Shipped (#989), INERT.
- **INC-5** — wire the feed + engine into the daemon tick (supplies the
  uic->ticker resolver from live positions and calls `run_live_exits`).

## INC-2 — live price feed

### Original spec (SUPERSEDED 2026-08-05 — see the price-source probe below)

INC-2 was specified as a Saxo streaming quote subscription: subscribe to
`/trade/v1/prices/subscriptions` over the OpenAPI websocket, read the `Quote`
field group (Bid/Ask/Mid + `LastUpdated`), and gate on `LastUpdated` staleness.
This assumed Saxo SIM serves equity market-data. The 2026-08-05 probe found it
does not, so the source pivots to yfinance for the interim; the Saxo streaming
feed returns as INC-2b once the funded live account is linked.

### Price-source probe 2026-08-05 (live, XNYS hours, read-only)

- **Saxo SIM** — `/trade/v1/prices` -> 404; `/trade/v1/infoprices` ->
  `Quote.PriceTypeBid`/`PriceTypeAsk` = `"NoAccess"` (no Bid/Ask/Mid). SIM
  serves FX market-data only; stock market-data is `NoAccess` on the unlinked
  demo.
- **Polygon (current plan)** — real-time -> 403 `NOT_AUTHORIZED`; aggregates are
  past-day-only (0 current-session bars).
- **yfinance** — `fast_info.last_price` returns real values with ~0.9 min
  intraday lag on liquid US names, keyless.

**yfinance is the only viable live source right now.**

### Saxo live path (documented)

Real LIVE Saxo DOES stream equity Bid/Ask over the same websocket +
`/trade/v1/prices/subscriptions` + `Quote` field group as FX — gated only by the
market-data entitlement. A demo linked to a funded live account inherits live
prices (US Level 1 is free for non-professional clients). The operator will fund
+ link the live account (~2026-08-06 AM); an OpenAPI-SIM streaming probe must
CONFIRM the linked demo actually streams stock quotes before committing to a
Saxo-only source.

### INC-2 split

- **INC-2a (this increment)** — yfinance-backed `PriceFeed`: interim primary +
  permanent fallback + test double. INERT (not wired to the daemon).
- **INC-2b (after funding + linking + probe)** — Saxo streaming `PriceFeed`:
  primary source once the entitlement is confirmed; yfinance demoted to
  fallback.
