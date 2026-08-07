# Live-Market Execution — INC-2: Saxo LIVE price feed — design

**Status:** DRAFT (awaiting operator spec review → implementation plan)
**Date:** 2026-08-07
**Parent memo:** `live_market_execution_model_design_2026_08_05.md` §3.3 + §5 INC-2 (the "price streaming" gap).
**Related:** [ADR 0014](../adr/0014-broker-agnostic-execution-layer.md) (broker-agnostic execution + the SIM-only rail), `live_market_execution_inc3_plan_2026_08_05.md` (the engine this feeds).
**Baseline:** `origin/main` `74552259`.

One-line: replace the polled yfinance price source with a **Saxo LIVE streaming feed** as the single price source for the live-market exit engine, behind an honest freshness gate. No fallback in the decision path — Saxo or veto.

---

## 1. How it works today / the problem

The live TP-tranche engine shipped and is wired into the daemon behind
`ALPHALENS_LIVE_MARKET_EXITS` (#993/#998). Its price signal comes from
`YfinancePriceFeed`, which calls `fast_info.last_price` once per managed uic per
~45 s tick. Three defects:

1. **It fabricates freshness.** `PricePoint.asof` is stamped with the local
   clock (`yfinance_price_feed.py`, `asof=self._clock()`), not with any tick
   time. If Yahoo freezes, a ten-minute-old number reports as brand new,
   forever. The engine has no way to detect it.
2. **It is the wrong price type.** `last_price` is a last trade. The executable
   reference for selling a long position is the **bid**. A last trade above the
   tranche target does not mean the bid is.
3. **It is ~1 min behind** and comes from an unofficial scraper of an
   undocumented endpoint.

Saxo SIM could not replace it — SIM serves stock market data as `NoAccess`.

## 2. What changed (probed 2026-08-07, LIVE, market open)

A funded LIVE account with the free "U.S. Stocks Level 1" subscription and a
LIVE OpenAPI app (`bracket-keeper`) now delivers genuine real-time data. All of
the following is measured, not documented-and-assumed:

| Probe | Result |
|---|---|
| REST `infoprices` on AAPL/NVDA, polled 6 s | price moves every poll, `LastUpdated` lag **0.1–1.4 s**, `DelayedByMinutes=0`, `PriceTypeBid=Indicative`, bid/ask spread 1–8 c |
| WS `wss://live-streaming.saxobank.com/oapi/streaming/ws/connect` | connects with `Authorization: BEARER`; 46 s run = 34 data messages, 0 control |
| `POST /trade/v1/infoprices/subscriptions` | HTTP **201** + full snapshot; `DELETE` → 202 |
| In-tree `parse_stream_frames` on LIVE frames | **decodes unchanged** — the binary envelope is identical to SIM |
| `RefreshRate` floor | **1000 ms, hard** (requested 0 / 100 / 500 → all assigned 1000) |
| Streamed payloads | **deltas** — unchanged fields are omitted (rows arrive with one or both sides absent) |

Two traps found, both load-bearing for this design:

- **A fresh OAuth session is `OrdersOnly`, which silently serves 15-min-delayed
  prices.** It needs `PATCH /root/v1/sessions/capabilities {"TradeLevel":
  "FullTradingAndChat"}` (→ 202, `DataLevel` `Standard`→`Premium`). The session
  capability survives a token refresh; only a fresh `auth` drops back.
- **`/port/v1/users/me/entitlements` lies about stock access.** It reported no
  `Stock` under `RealTimeTopOfBook` for NASDAQ/NYSE both before and after the
  upgrade that demonstrably produced 0-delay stock quotes. **Never gate on it.**
  The only trustworthy check is `DelayedByMinutes` on a real quote.

## 3. Goal

Give the exit engine a price it can trust, and make every untrustworthy price
structurally unable to fire an order.

The failure asymmetry drives everything: **no price costs a missed take-profit;
a wrong price causes a bad sell.** The disaster stop rests server-side either
way, so vetoing is always safe and always cheap. Therefore every ambiguity
resolves to `None`.

## 4. Decision: Saxo or veto (no fallback in the decision path)

Adversarially reviewed with Perplexity, which initially argued for a hard
"broker feed or veto" and then conceded two of three counter-arguments (the
SIM-stakes calibration, and that this is a change from a status quo that
already fires on ~1-min data, not a greenfield hazard). It did not concede that
an honest timestamp plus a volatility buffer makes an unofficial last-trade feed
fit for real-money exits, and the surviving failure mode it named is real:

> A stale price beyond the target proves a qualifying trade existed during the
> interval. It does not prove the bid is still above the target now.

**Operator decision: no fallback.** Saxo fresh, or the engine does nothing.

A yfinance WebSocket path was measured (v1.5.1 ships `yf.WebSocket`) and
explicitly rejected. It is better than assumed — real event timestamps, 1.1–1.4 s
lag — but it carries **no bid/ask** (fields exist in the protobuf schema and
arrive empty, confirmed live), comes from a reverse-engineered endpoint with no
data-quality declaration, and would cost a second full streaming client. Rejected
on price type and provenance, not on latency.

`YfinancePriceFeed` **stays in the tree, unused.** It must still be updated to
the new `PricePoint` shape, and that update fixes its honesty: `event_time`
takes the real 1-minute bar timestamp. It then becomes structurally incapable of
firing, because a ~60 s age fails the freshness gate on its own — no special-case
ban required, and it doubles as a live proof that the gate works.

## 5. Architecture

The SIM-only rail (ADR 0014) has four locks, one of which forbids any LIVE URL
string anywhere in the `brokers/` package sources. That is not a preference to
respect — it is a red test. It fixes the layout:

| Component | Location | Responsibility |
|---|---|---|
| `SaxoMarketDataClient` | `alphalens_pipeline/data/alt_data/saxo_marketdata_client.py` | The only place LIVE URLs exist. OAuth + refresh, session-capability PATCH + read, ticker→uic resolution, subscription create/delete. |
| `SaxoPriceStream` | `alphalens_pipeline/data/alt_data/saxo_price_stream.py` | One long-lived daemon thread: connect, merge deltas, hold the per-uic quote cache, reconnect with backoff + circuit breaker, re-authorize in place on token rotation. |
| `SaxoLivePriceFeed` | `brokers/automanager/saxo_live_price_feed.py` | The `PriceFeed` adapter: read the cache, apply the freshness gate, return `PricePoint` or `None`. **Contains no URLs**, so the rail stays intact. |

`SaxoClient` is not touched. It refuses LIVE base URLs by construction and must
keep doing so; the new client is a separate surface with a separate token store.

**Lifetime.** `live_exits_feed_factory` is called every tick, but a WebSocket
must outlive ticks. The stream is a long-lived object owned by the daemon; the
factory only reconciles the subscription set against this tick's ticker map and
returns a thin view over the cache. Per-tick allocation stays cheap; the
connection persists.

**Instrument identity.** Managed positions come from SIM and carry SIM uics.
The feed does **not** assume SIM and LIVE uics match: it resolves ticker →
LIVE uic through LIVE reference data and caches it. If they do coincide, nothing
is lost; if they do not, we avoid subscribing to the wrong instrument, which
would be a silent catastrophe.

**Contract change (`broker_contract/price_feed.py`).** `PricePoint` gains both
quote sides, the provider event time, the local receipt time and a source tag,
and loses the single fabricated `price`/`asof` pair. `plan_tranche_exits` stays
a **pure scalar function** — the caller selects the side (bid for a long exit)
before calling it. The tested decision core does not move. Existing consumers
(`run_live_exits`, `YfinancePriceFeed`, tests) are updated in the same commit;
no compatibility shim, per project doctrine.

## 6. The freshness gate

`latest(uic)` returns a `PricePoint` only when **all** hold, else `None`:

- `DelayedByMinutes == 0` — its own condition, never folded into the age check;
- event age (from `LastUpdated`, never from receipt time) `<= 3 s`;
- both sides present, finite, positive;
- `bid <= ask`;
- `(ask - bid) / mid <= 0.02` — a relative spread ceiling, because the project
  has no per-instrument spread table and inventing one is not worth it. 2 % is
  far above the 1–8 c (≈0.003–0.03 %) measured on liquid names, so it catches
  broken quotes without vetoing normal ones;
- no duplicate / sequence regression;
- the instrument is in a live session.

3 s is roughly twice the measured worst lag (1.4 s) and, against a 1 Hz push,
still detects a dead stream within seconds. Recovery requires **two consecutive
healthy updates** before the feed reports live again.

The delayed-flag condition is not defensive padding. It is the only signal for
the demotion failure below, where everything else looks perfectly healthy.

## 7. Failure behaviour

Guiding rule, to be stated in code: **every doubt ends as `None`.** There is no
path in which this module guesses a price.

- **Disconnect** — reconnect with exponential backoff; meanwhile the cache ages
  and the gate vetoes on its own. No separate "disconnected" flag is needed —
  the age already says it. After **6 consecutive failures** the breaker opens,
  alerts once, and stops hammering — reusing the existing streaming reader's
  tuning (`max_consecutive_failures=6`, backoff 1 s → 30 s) rather than
  inventing a second convention.
- **Auth expiry** — token refreshed early; the stream is re-authorized in place
  without dropping the socket. A failed refresh kills the stream, degrading to
  the disconnect case: veto, not a bad price.
- **Session demotion (the dangerous one)** — verified 2026-08-07: when the
  operator logs into SaxoTraderGO, the API session drops to `OrdersOnly` and
  **prices keep flowing, keep moving, and are 15 minutes old**. No error, no
  exception, no gap; on a calm market they look nearly right. Caught only by the
  delayed flag. Response: **rate-limited reclaim** — re-`PATCH` on detected
  demotion, at most **4 times per rolling hour**, then back off, alert and stay
  vetoed until the next hour opens the budget again.
  SaxoTraderGO shows the loser an explicit banner with a resume button, so a
  reclaim never leaves the human confused; the rate limit means that if the
  operator keeps pressing resume, the human wins.
- **Malformed frame** — the parser raises, the thread counts it as a connection
  failure. A half-decoded frame is never routed.
- **New position mid-tick** — its subscription is added lazily at the next tick,
  so the first tick may veto. Correct: no price means wait, never guess.
- **Closed position** — subscription deleted, so the 200-instrument cap is not
  leaked away.

The daemon tick never sees an exception from the stream thread.

## 8. Testing

- **Hermetic** — delta merging (a one-sided message must not blank the other
  side), every gate condition independently, ticker→uic resolution and caching,
  cache ageing under a dead stream, the reclaim rate limiter.
- **Live probe, opt-in** — in the established `tests/live/` shape: asserts
  structure and non-emptiness, never values, behind its own env flag so it never
  blocks CI. Without it we would ship green tests over a dead feed, which has
  happened in this project before.
- **Acceptance** — the existing FakeBroker suite is unchanged; one scenario
  added: the feed vetoes every tick → no tranche fires, the stop is untouched.
- **Rail enforcement** — the new client is added explicitly to the canonical
  Saxo HTTP surfaces in `test_no_raw_saxo_http.py`, with its rationale. It is
  not smuggled past the check via an injected session.

## 9. Rollout

Four reversible steps:

1. Contract + client + stream + adapter, flag default OFF, nothing wired.
2. LIVE credentials to `/etc/alphalens/env`; OAuth bootstrap **on the VPS** (the
   token store is never copied — the rotating refresh token permits exactly one
   holder). Its own refresh timer, separate from the SIM one.
3. Switch the default feed factory from yfinance to Saxo, still behind
   `ALPHALENS_LIVE_MARKET_EXITS`.
4. Observe.

**Operational rule:** exactly one LIVE session may hold the elevated capability.
The VPS daemon owns it. Any Mac-side probe session must be stopped first, or it
will demote production by the same mechanism the operator's platform login does.

## 10. Out of scope

- **Entries (INC-4).** The feed serves exits only for now. Entries will need the
  ask side, which the contract already carries.
- **Real-money execution.** Unchanged: trading stays on SIM behind the ADR 0014
  rail. This design consumes LIVE market data only; the LIVE app's trading
  permission stays unused and lifting the rail remains a separate ADR.
- **Divergence monitoring.** An independent second source cross-checking Saxo is
  a defensible idea on its own merits and should earn its own PR. It is not a
  reason to keep a fallback in the decision path.

## 11. Decisions on record

**Reclaim policy — CONFIRMED by the operator 2026-08-07.** The daemon reclaims
the elevated session from the operator's platform, rate-limited per §7. The
operator uses SaxoTraderGO only occasionally, so the unattended bot is the right
default holder; SaxoTraderGO's banner tells the operator what happened, and the
4/hour cap means that repeatedly pressing resume hands the capability back to the
human. Revisit only if the operator's platform usage becomes routine.

**No fallback in the decision path — operator decision (§4).** Saxo fresh, or
veto.

**yfinance kept but unused — operator decision (§4).** Updated to the new
contract, wired to nothing.
