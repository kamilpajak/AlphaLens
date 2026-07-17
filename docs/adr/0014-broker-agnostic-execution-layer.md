# ADR 0014 — Broker-agnostic execution layer (Saxo first, SIM-only)

- **Status:** Accepted
- **Date:** 2026-07-17
- **Supersedes:** — (successor to, NOT a reversal of, ADR 0012)
- **Related:** ADR 0012 (decommission paper-trading + broker chain),
  ADR 0013 (trade-side layer architecture T1–T8)

## Context

1. **ADR 0012 tore the previous broker chain down — and anticipated this
   layer.** That decommission removed the Alpaca client AND a prior Saxo
   client + token manager/store/reauth, the paper orchestration, the
   `alphalens paper` / `alphalens saxo` CLI groups, and 9 systemd units. The
   load-bearing removal rationale was that Alpaca paper could not do honest
   server-side OCO ("we attach" vs "the exchange brackets"). ADR 0012
   explicitly kept the broker-free primitives
   (`paper/{calendar,sizing,brief_loader,constants}`) as reusable seams. A
   REAL broker with native server-side bracket/OCO semantics is exactly the
   capability whose absence justified the teardown — Saxo's nested-Orders
   bracket (one POST, exchange-side OCO between stop and take-profit)
   provides it. This ADR is therefore NOT a reversal of 0012; it is the
   successor 0012 anticipated.

2. **What changed since 0012:**
   - (a) the operator decided on **Saxo** as the actual brokerage (real
     account, SIM environment available, WSE + US venues in one API);
   - (b) the exit-geometry work (reward:risk upside-down finding, breakeven
     replay, the ladder what-if registry) produced concrete, versioned exit
     semantics worth executing honestly rather than only replaying;
   - (c) ADR 0013 defined the trade-side layer map T1–T8 and explicitly
     scoped broker execution OUT ("there is none") — creating a named,
     rule-bound slot for this layer to occupy downstream of T5 SETUP.

3. **Constraints inherited:**
   - **R2** (ADR 0013): SETUP/execution outputs never feed SELECTION — no
     broker output (fills, rejections, balances) may enter the T2 funnel.
   - **R3** (ADR 0013): execution carries its own
     `execution_config_version` poolability key; bumps are cohort
     boundaries (lands with P2, the first increment that produces
     execution output).
   - **T8 no-pooling:** live fills are a NEW measurement source, keyed
     separately from the broker-free price-path replays — never silently
     merged.
   - **One-canonical-HTTP-client-per-vendor** doctrine (CLAUDE.md) extends
     to Saxo mechanically.
   - Saxo offers **no retail service-account auth** — the OAuth
     refresh-chain daemon is a known re-solve of the removed
     `alphalens-saxo-refresh` unit (deferred to P4).

## Decision

Introduce `alphalens_pipeline/brokers/` with:

- a small broker-agnostic `Broker` Protocol (`contract.py`): account,
  positions, instrument resolution, bracket placement, order status/cancel —
  nothing else (no streaming, no quotes, no modify, no analytics, no
  multi-account);
- a lazy factory registry (`registry.py`, `get_default_broker()`,
  `ALPHALENS_BROKER` env default);
- Saxo as the first adapter (`saxo/broker.py`) behind a canonical
  `SaxoClient` (`saxo/client.py` — house transport shape: injected
  session/sleep DI, dual-layer retry, 0.5s throttle, `x-request-id` on every
  call, Bearer-only token discipline);
- auth via a pluggable `TokenProvider` (`saxo/tokens.py`): the 24h
  Developer-Portal SIM token today (`SAXO_SIM_TOKEN`), OAuth refresh in P4
  with zero client changes.

**SIM-only structural rail:** the client refuses any base URL other than the
SIM gateway (`LIVE_TRADING_ENABLED = False` + constructor guard + a stray
`SAXO_ENV != sim` fails loudly + pinning tests in
`tests/brokers/test_saxo_sim_only_rail.py`). Lifting the rail requires a
separate future ADR.

**Phased delivery:** P1 reads → P2 placement (own design memo + adversarial
review; precheck gate `POST /trade/v2/orders/precheck` with
`FieldGroups=["Costs"]` before every real POST, `ManualOrder=false`,
max-2-related constraint) → P3 reconciliation → P4 OAuth → (future ADR)
LIVE. Phase details in
`docs/research/saxo_broker_layer_design_2026_07_17.md`.

## Consequences

Positive:

- A second broker (IBKR, ...) = one adapter package + one registry entry;
  zero consumer churn.
- The exit-geometry research gains an honest execution path when P2 lands
  (server-side OCO, the ADR 0012 bar).
- House enforcement extends mechanically: `tests/test_no_raw_saxo_http.py`,
  module-dependency rules (`thematic`/`feedback` must not import
  `brokers`), the `SAXO_LIVE_TEST=1` live probe.

Negative / accepted costs:

- The 24h-token expiry makes P1 a manually-refreshed, operator-attended
  tool — no unattended VPS scheduling until P4.
- Saxo's max-2-related-orders means the multi-tier / multi-tranche ladder
  cannot map 1:1 onto a single bracket — P2 must design the decomposition
  and pay its own review.
- SIM equity data is indicative/delayed, and WSE SIM coverage is unverified
  (early validation task — the live probe resolves a WSE ticker
  deliberately).
- New maintenance surface: Saxo API drift, the ManualOrder mandate.

## Relation to `capital_deploy_clause`

This ADR builds execution TOOLING; it does NOT reverse or weaken the
pre-registration `capital_deploy_clause` (capital deployment off-table — no
standing paradigm PASS). Execution tooling ≠ a capital deployment decision:
SIM-only is enforced structurally in code, and even a future LIVE-enabling
ADR only removes the technical rail — deploying capital additionally
requires the research-side clause to be satisfied on its own terms. Two
independent gates, deliberately not collapsed.
