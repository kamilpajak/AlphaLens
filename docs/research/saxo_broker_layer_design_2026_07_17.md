# Broker-agnostic Saxo execution layer — design memo

**Status:** LOCKED (for P1 scope; P2+ each get their own design memo + adversarial review)
**Date:** 2026-07-17
**ADR:** [ADR 0014](../adr/0014-broker-agnostic-execution-layer.md)
**Increment shipped by this memo:** P1 (reads + contract + SIM-only rail);
§P2 decision record added with the P2 order-placement increment

## 1. What this is

A broker-agnostic execution layer under
`apps/alphalens-pipeline/alphalens_pipeline/brokers/`, with Saxo Bank
(OpenAPI, SIM environment) as the first adapter. Pipeline side per ADR 0011
— live infra, NOT under `data/alt_data/` because Saxo is an execution
vendor, not a data vendor; but `saxo/client.py` copies the alt_data client
shape (`polygon_client.py`) exactly.

Position in the ADR 0013 trade-side map: a NEW downstream consumer of T5
SETUP's frozen `trade_setup` JSON (via the existing
`paper/sizing.py::compute_setup_plan` → `SetupPlan`), implementing T7
termination semantics live in later phases. Bound rules: R2 (no
broker/execution output ever feeds T2 SELECTION — enforced in
`tests/test_module_dependencies.py`), R3 (P2 introduces its own
`execution_config_version` poolability key), T8 no-pooling (live fills are a
NEW measurement source, keyed separately from broker-free replays).

## 2. Contract rationale (`brokers/contract.py`)

- `typing.Protocol` (`@runtime_checkable`), not ABC — composition over
  inheritance; a second broker (IBKR) implements it without importing any
  Saxo code. Behavioral semantics a Protocol cannot pin live in the shared
  conformance mixin (`tests/brokers/test_broker_contract.py`).
- Frozen dataclasses: `InstrumentRef`, `AccountSnapshot`, `Position`,
  `OrderState` (+ `OrderStatus` enum), `BracketOrderRequest`, `PlacedOrder`.
- Error taxonomy: `BrokerError` > `BrokerAuthError` / `BrokerRateLimitError`
  / `InstrumentNotFoundError` / `OrderRejectedError` /
  `BrokerCapabilityError`. Vendor exceptions never escape the adapter.
- **YAGNI cuts (deliberate):** NO streaming, NO quotes/prices, NO order
  modify (PATCH is a full-replace footgun — cancel+replace in P2 if
  needed), NO portfolio analytics, NO multi-account handles.
- The placement/status/cancel SIGNATURES are frozen now; in P1 they raise
  `BrokerCapabilityError` citing ADR 0014 P2 — so P2 does not churn the
  contract.

## 3. Saxo endpoint map (P1 reads)

| Wrapper | Endpoint | Notes |
|---|---|---|
| `get_user()` | GET `/port/v1/users/me` | live-probe round-trip target |
| `get_client_info()` | GET `/port/v1/clients/me` | ClientKey, cached on first call |
| `get_accounts()` | GET `/port/v1/accounts/me` | AccountKey list |
| `get_balances(client_key, account_key=None)` | GET `/port/v1/balances` | cash / total / margin |
| `get_positions(client_key)` | GET `/port/v1/positions` | FieldGroups: PositionBase, PositionView, DisplayAndFormat |
| `search_instruments(keywords, asset_types, exchange_id)` | GET `/ref/v1/instruments` | resolve backbone |
| `get_instrument_details(uic, asset_type)` | GET `/ref/v1/instruments/details/{Uic}/{AssetType}` | tick-size scheme — P2 price validation |
| `get_exchanges()` | GET `/ref/v1/exchanges` | one-time ExchangeId confirmation |
| `get_json(path, params)` | any | public escape hatch (house pattern) |

Transport: raw `requests`, NO third-party SDK (research verdict: no official
Python SDK; saxo-apy dead/off PyPI; hootnot stale since 2021 with
ManualOrder-era drift risk). Dual-layer retry: 429 → `Retry-After` /
`X-RateLimit-Session-Reset` clamped to [1, 120]s; 5xx → (5, 15, 30);
network → (5, 15); 401 → `TokenProvider.invalidate()` + one retry with a
fresh token, then `SaxoAuthError`. Proactive throttle 0.5s min-interval
(Saxo cap: 120 req/min per session per service group — 0.5s is the exact
ceiling, safe for a read-only daily driver). Every request carries
`x-request-id` (uuid4, stable across retries of one logical request) — on
reads harmless; P2's 15s duplicate-order 409 dedup and the
never-blind-retry-a-POST rule inherit it for free.

Instrument resolution (`saxo/broker.py`): MIC → Saxo ExchangeId map
(`XNYS→NYSE`, `XNAS→NASDAQ`, `XWAR→WSE` — codes confirmed against the live
SIM gateway 2026-07-17, see §6), exact-symbol match
on Saxo's lowercase-MIC-suffixed display symbol (`KO:xnys`), miss/ambiguity
→ `InstrumentNotFoundError`, FIFO-bounded in-process cache (no disk cache
in P1).

## 4. SIM vs LIVE — the structural rail

- `SIM_BASE_URL = https://gateway.saxobank.com/sim/openapi` is the ONLY base
  URL the constructor accepts; `LIVE_TRADING_ENABLED = False` is flipped
  only by a future ADR (mirrors `capital_deploy_clause` — two independent
  gates, deliberately not collapsed; see ADR 0014 §Relation).
- There is deliberately NO `environment=` switch and NO `SAXO_ENV` env-var
  path to LIVE; a stray `SAXO_ENV != sim` makes `from_env()` fail loudly
  (operator .env confusion guard).
- Pinned by `tests/brokers/test_saxo_sim_only_rail.py`: constructor refuses
  each LIVE marker, flag is False, `SAXO_ENV=live` raises, and a source
  scan proves no LIVE URL string exists in the package outside the marker
  tuple.

Auth today: 24h Developer-Portal SIM token in `SAXO_SIM_TOKEN`
(`StaticTokenProvider`); expires daily, regenerated manually until P4. The
`TokenProvider` protocol is the P4 seam (OAuth code grant, rotating refresh,
atomic persistence to `~/.alphalens/saxo/token_store.json`, Telegram alert
on refresh-chain loss — the removed `alphalens-saxo-refresh` unit's job)
with ZERO client changes.

## 5. Ladder → bracket decomposition (DEFERRED to P2 — the central question)

Saxo allows max 2 related orders per entry (1 stop-type + 1 limit). The
`trade_setup` ladder has up to 3 entry tiers × multiple TP tranches sharing
one disaster stop. Candidate mappings — (a) one bracket per entry tier with
tier-local TP + full stop; (b) entry-only orders + position-attached exits
after fill (EoD-netting accounts only); (c) reduced-fidelity
single-TP-per-tier — are P2's design question and pay P2's review. P1
deliberately freezes only `BracketOrderRequest` as the decomposition unit.

## 6. WSE-on-SIM validation

Research flags WSE coverage on the SIM environment as the most likely gap.
The live probe (`tests/live/test_saxo_live.py`, `SAXO_LIVE_TEST=1`)
resolves `CDR @ XWAR` and classifies a miss as PERMANENT so the gap is
recorded before the P2 scope locks. **Result: LIVE-VERIFIED 2026-07-17** —
all three venues resolve on the SIM gateway, exact-symbol match:

| Ticker | MIC | Saxo Uic |
|---|---|---|
| NVDA | XNAS | 1249 |
| KO | XNYS | 307 |
| CDR | XWAR | 53932 |

WSE-on-SIM coverage is CONFIRMED (no gap); XWAR nonetheless stays
explicit-only for order routing until the PLN/FX-leg sizing question (§8
Q3) is designed — see §P2.

## 7. Phase plan

- **P1 (THIS increment, independently shippable):** brokers/ package
  skeleton + contract.py (full frozen Protocol incl. placement signatures) +
  registry.py + SaxoClient (house shape, raw requests, x-request-id,
  dual-layer retry, 0.5s throttle) + tokens.py StaticTokenProvider on
  SAXO_SIM_TOKEN + SaxoBroker reads (account/positions/resolve; placement
  raises BrokerCapabilityError) + SIM-only rail (LIVE_TRADING_ENABLED=False,
  constructor guard, pinning test) + test_no_raw_saxo_http.py with positive
  control + hermetic unit tests + tests/live/test_saxo_live.py
  (SAXO_LIVE_TEST=1, shape-only, no orders) + `alphalens broker
  account|positions|resolve` CLI + ADR 0014 + this memo + .env.example
  section + module-dependency rules. Ship value: verified SIM connectivity,
  instrument resolution incl. WSE-coverage-on-SIM validation, frozen
  contract.
- **P2 (own design memo + adversarial review + zen, SIM-only):** order
  placement. SetupPlan → BracketOrderRequest decomposition (Saxo
  max-2-related constraint vs multi-tier entry / multi-tranche TP — the
  central design question), precheck gate (POST /trade/v2/orders/precheck,
  FieldGroups=[Costs]) before every real POST, tick-size/decimals validation
  via get_instrument_details, ManualOrder=false, client_request_id dedup +
  never-blind-retry-POST, cancel_order/get_order/list_open_orders
  implementations, `alphalens broker submit|cancel|orders` CLI,
  execution_config_version key per ADR 0013 R3. Ship value: end-to-end SIM
  bracket orders with server-side OCO.
- **P3 (independently shippable):** reconciliation + measurement. Daily poll
  of /port/v1/orders + /port/v1/positions vs expected plans (expiry sweep on
  order_ttl_days via paper/calendar trading_days_elapsed), fill records
  persisted under a NEW measurement key (T8 no-pooling with broker-free
  replays), Telegram alerts on divergence, `alphalens broker reconcile` CLI.
  No systemd unit yet if still on 24h token (attended runs); unit lands with
  P4.
- **P4 (independently shippable):** registered-app OAuth.
  RefreshingTokenProvider (authorization-code grant, localhost redirect
  listener bootstrap via `alphalens broker auth`, rotating refresh with
  atomic newest-token persistence to ~/.alphalens/saxo/token_store.json,
  renew well before refresh_token_expires_in, Telegram alert on
  refresh-chain loss), optional refresh daemon systemd unit (re-solving the
  removed alphalens-saxo-refresh), SAXO_APP_KEY/SAXO_APP_SECRET/
  SAXO_TOKEN_STORE_PATH env keys, VPS runbook (incl. Akamai-403
  IP-reputation caveat), enable the Saxo probe in weekly CI live-probes job
  (needs non-expiring auth — impossible before this phase). Zero SaxoClient
  changes: TokenProvider seam only.
- **P-LIVE (future, requires its OWN ADR — not scheduled):** lift the
  SIM-only rail. Prereqs: Saxo Direct-Clients LIVE app approval lead time,
  separate LIVE app registration/keys, market-data entitlements audit, and —
  independently — the research-side capital_deploy_clause; ADR 0014
  explicitly does not pre-authorize this.

## P2 decision record (order placement — shipped 2026-07-17)

### Live netting read (the fact that decides the decomposition)

`GET /port/v1/clients/me` on the SIM account, 2026-07-17, verbatim:
`PositionNettingMode="Intraday"`, `PositionNettingProfile="FifoRealTime"`
(`AllowedNettingProfiles` includes `FifoEndOfDay`). Saxo docs: position-
attached related orders (the PositionId form) "only work for clients on
End-of-Day netting mode" — so the fill-then-attach-exits pattern (§8 Q1
option b) is DEAD on this account, not an assumption. Exits MUST be
order-attached at entry time. Intraday/FifoRealTime also means an exit fill
nets against the entry position immediately (FIFO) and the pair moves to
`/port/v1/closedpositions` — P3 reconciliation reads positions AND
closedpositions; never set `IsForceOpen` on exits (it would suppress the
netting that closes the trade).

### Decomposition decision: one bracket per non-zero entry tier

`decompose_setup_plan(setup_plan, instrument)` in `brokers/execution.py`
maps a sized `SetupPlan` onto up to 3 `BracketOrderRequest`s — each placed
as ONE single-shot `POST /trade/v2/orders` (entry Limit parent + Sell Limit
TP child + Sell StopIfTraded child). Saxo related-order limits: max 2
children per entry, exactly one Limit + one stop-type, Amount identical
across all three.

Per-bracket mapping: quantity = `tier.qty`; entry = `tier.limit_price`;
stop = `disaster_stop` (same PRICE on every bracket, tier-sized Amount —
children activate only when their tier fills, so aggregate stop coverage
always equals filled quantity exactly; economically equivalent to the
replay's single shared stop); TP = `tp_tranches[min(tier_index, len-1)]`
(clamped to the last tranche; `None` → stop-only bracket); TTL =
`order_ttl_days` (0 sentinel → planner default 7) as trading-day date-only
GTD on the venue calendar; exits GoodTillCancel; `client_request_id` =
fresh uuid4 per bracket. Zero-qty tiers are skip-logged, never POSTed.

### Fidelity kept / lost

| Kept | Lost |
|---|---|
| Exact tier entry limits + per-tier quantities | `tranche_pct` scale-out WITHIN a tier — each tier's whole qty exits at one target (Saxo allows one limit child; per-tranche sub-brackets would multiply stop orders past filled qty) |
| Disaster-stop price exact; stop coverage == filled qty at all times | Tranches > tiers: intermediate targets unused; tiers > tranches: deep tiers reuse the last target (clamp) |
| Trading-day TTL on the entry (calendar GTD); exits outlive it (GTC) | No ratchet / TP1→BE stop moves in v1 (PATCH exists for P3+; the in-flight-lens memo shows the ratchet barely binds) |
| Server-side OCO from the moment any fill exists | Partial fill of one tier: Saxo child-amount behaviour undocumented — OPEN, observe via the SIM order probe before P3 relies on it |

Alternatives rejected: (a) position-attached exits — blocked by the live
Intraday netting mode; switching the account to FifoEndOfDay rejected
(mutates account config to fit code, changes netting timing vs the replay
economics, still capped at 2 related orders, hidden account state);
(b) one aggregate bracket — destroys the ladder geometry; (c) two-step
attach (`{OrderId, Orders:[...]}`) — races the entry fill, spends extra
throttle quota; (d) full tranche replication via a monitoring loop — needs
a resident reconciler (P3/P4) and loses server-side OCO → naked-risk
windows.

### Retry / idempotency policy

`x-request-id` = the bracket's `client_request_id` is Saxo's REAL
idempotency (15s duplicate window); `ExternalReference` is correlation
only. POST is NEVER blind-retried: only provably-unsent network failures
(connect-phase) and 429 (provably not accepted) retry, always with the SAME
id; 5xx / ambiguous post-send errors raise immediately carrying the id so
the operator reconciles (`broker orders`) before re-running. DELETE is
idempotent → normal ladder. Precheck (`/trade/v2/orders/precheck`,
`FieldGroups=["Costs"]`) runs before EVERY real POST; non-Ok blocks
placement (precheck validates but reserves nothing — the real POST can
still fail: 400-with-OrderId → auto-cancel the entry (cascade) then
`OrderRejectedError`; 202 TradeNotCompleted → `BrokerError` with the
OrderId + reconcile instructions, no automatic action).

### MIC routing (hard req 4)

`brokers/routing.py::resolve_us_instrument`: explicit MIC wins; otherwise
probe XNYS→XNAS via the cached exact-symbol resolve and require EXACTLY ONE
match (none/both → `InstrumentNotFoundError`; never guess). XWAR stays
explicit-only until PLN/FX-leg sizing (§8 Q3) is designed. An upstream
parquet/Polygon exchange stamp was rejected for P2: forward-only schema
churn, and MIC is an execution concern (ADR 0013 R2 — no execution data
upstream); the RESOLVED MIC is stamped on the submission record instead.

### Poolability + safety rails

`execution_config_version()` (`execution-v1-<12hex>` over every policy
constant, ADR 0013 R3) is stamped on every line of
`~/.alphalens/broker_orders/submissions.jsonl` and echoed by the CLI; a
bump is a forward-only cohort boundary, and live fills stay a NEW T8
measurement source never pooled with broker-free replays (this discharges
the ADR 0014 "lands with P2" line as written — no ADR amendment needed).
Rails: `ALPHALENS_BROKER_ALLOW_ORDERS=1` gate inside
`SaxoBroker.place_bracket_order` (cancel deliberately ungated —
remediation always works); CLI `broker submit` dry-run by default with
`--execute` + interactive confirm (`--yes` for scripted use); SIM-only rail
untouched; import rails extended (paper must not import brokers);
attended-only order probe `SAXO_LIVE_ORDER_TEST=1` excluded from
`just probe-live` and CI by meta-assertion.

## 8. Open questions (for the user)

1. **Ladder→bracket decomposition fidelity** (decides P2, but the user
   should weigh in before P2 design): Saxo allows max 2 related orders per
   entry (1 stop-type + 1 limit). The trade_setup ladder has up to 3 entry
   tiers × multiple TP tranches sharing one disaster stop. Options: (a) one
   bracket per entry tier with tier-local TP + full stop (simplest, honest
   OCO, but stop qty duplication across tiers needs care), (b) entry-only
   orders + position-attached exits after fill (EoD-netting accounts only —
   is the user's Saxo account EoD or Intraday netting?), (c)
   reduced-fidelity single-TP-per-tier. Which fidelity level is acceptable
   vs the ADR-0012 'honest server-side OCO' bar?
2. **Account selection:** does the user's Saxo (SIM and eventual LIVE)
   profile have multiple AccountKeys (e.g. cash vs margin, multi-currency)?
   If yes, introduce SAXO_ACCOUNT_KEY env var in P1; if single-account,
   get_account() can auto-pick — need the user to check the SIM account
   structure. *(P1 ships both: auto-pick for single-account, loud failure
   asking for `SAXO_ACCOUNT_KEY` for multi-account.)*
3. **WSE scope for P1 validation:** should the SIM live probe validate a
   specific Warsaw ticker (which one?) now, or is US-only acceptable for P1
   with XWAR deferred until the FX-leg (PLN sizing) question is designed?
   Research flags WSE-on-SIM coverage as the most likely gap — worth one
   manual check with the 24h token before locking the P2 scope. *(P1 probe
   uses CDR @ XWAR as the placeholder; swap if the user prefers another.)*
4. **Where does P1 run:** Mac-only attended CLI (implied by the 24h
   manually-pasted token), or does the user want it runnable on the VPS
   already? VPS adds the Akamai 403 IP-reputation risk and re-adding
   SAXO_SIM_TOKEN to /etc/alphalens/env (which ADR 0012 purged) — operator
   runbook step, user's call.
5. **Package `__status__`:** spec says ACTIVE (matches every existing
   pipeline package and the SIM rail is the safety mechanism, not the
   status field) — confirm the user doesn't prefer RESEARCH_ONLY until the
   first P2 SIM order as an extra semantic marker.
6. **Env var name SAXO_SIM_TOKEN** (chosen over SAXO_24H_TOKEN — describes
   scope, survives P4 where the SIM OAuth tokens are also SIM-scoped):
   confirm, since the operator types it into .env daily until P4.
7. **P4 priority:** the 24h token forces a daily manual regeneration for
   any recurring P3 reconciliation. If the user intends daily
   reconciliation soon after P2, consider swapping P3 and P4 (OAuth before
   reconciler) — the mandated order P2→P3→P4 works but makes P3
   attended-only. User preference?
8. **Timing of the .env.example DEAD ALPACA_* block removal:** fold into
   the P1 PR (same file touched) or keep as separate cleanup per the
   small-PR doctrine? Default: separate, but user may prefer bundling.
   *(P1 keeps the DEAD block untouched.)*
9. **Second broker horizon:** is IBKR concretely planned (affects whether
   ALPHALENS_BROKER env-selected default is worth wiring in P1 — currently
   yes, it is 3 lines) or purely hypothetical?
