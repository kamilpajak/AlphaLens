# Saxo LIVE order rail lift — keyed day-bound unlock (design)

**Status:** LOCKED (approved 2026-08-10, pre-market)
**Date:** 2026-08-10
**Author:** operator + assistant session
**Related:** ADR 0014 (SIM-only rail), ADR 0015 (this lift), `docs/research/trailing_sl_exit_policy_bot_amend_design_2026_08_09.md` (Tor A), `torb_live_probe.py` (the consumer), PR #1004 (LIVE market-data feed), PR #1009 (`broker marketdata-auth`)

## 1. Problem

The Tor B LIVE battery (`torb_live_probe.py`) must answer three questions the
SIM structurally cannot: does a native `TrailingStopIfTraded` ratchet its
level on live ticks, does a narrow-distance amend reposition the level, and
what is the real implementation shortfall on a market SELL (SIM fills are
synthetic @ ref, zero spread). Answering them requires placing REAL orders
with REAL money on the LIVE gateway.

Today that is structurally impossible — by design. ADR 0014 installed four
independent locks (`tests/brokers/test_saxo_sim_only_rail.py`):

- **(a)** the `SaxoClient` constructor refuses every base URL ≠ SIM;
- **(b)** `LIVE_TRADING_ENABLED = False`, "flipped only by a future ADR";
- **(c)** `from_env` with `SAXO_ENV != sim` fails loudly (no env switch to LIVE);
- **(d)** no LIVE gateway URL string may exist anywhere in the `brokers/`
  package sources outside the `_LIVE_URL_MARKERS` tuple.

This memo is the design for that "future ADR" (ADR 0015): the narrowest lift
that lets an ATTENDED, per-process, self-expiring probe reach LIVE, while the
daemon and every default construction path remain SIM-only — structurally,
not by convention.

## 2. Reframing: execution product, not alpha deployment

The operator's goal is to validate the EXECUTION LAYER as a standalone
product (future separate repo; clients: AlphaLens, Betlejem5, …). The
pre-registration `capital_deploy_clause` gates deployment of *AlphaLens
alpha*, not verification that the execution engine faithfully executes a
`TradeIntent` on the real rail. Execution-fidelity validation with a
throwaway 1-share position is Gate-1 engineering, and is in scope.
Continuous LIVE trading of real picks remains out of scope (§7).

## 3. Design — keyed day-bound unlock

### 3.1 Lock (d) survives verbatim: the LIVE URL never enters `brokers/`

`SaxoClient` already takes `base_url` and `token_provider` by injection. The
LIVE gateway URL is supplied by the CALLER (the probe script, outside the
package). No new URL string appears under `brokers/`; the source scan is
untouched.

### 3.2 Lock (a) becomes conditional — the only code change

The constructor keeps refusing every base URL ≠ SIM **unless** the
environment carries a resource-bound, self-expiring confirm:

```
ALPHALENS_SAXO_LIVE_ORDERS_UNLOCK=<today's UTC date, YYYY-MM-DD>
```

- **Day-bound = self-expiring.** A forgotten variable in a shell profile or
  unit file cannot re-arm LIVE on a later day. Mirrors the CLI-conventions
  rule: dangerous operations take a resource-bound token, never a bare `-y`.
- A wrong, stale, or future date raises the same
  `SaxoLiveEnvironmentBlockedError` as today.
- A successful unlock logs a LOUD warning (base URL only — never tokens).

### 3.3 Locks (b) and (c) survive verbatim — daemon isolation is structural

- `LIVE_TRADING_ENABLED` stays `False`: it documents the STRUCTURAL default
  (LIVE is not enabled by default); the runtime unlock is orthogonal and
  per-process. The rail test's assertion is unchanged.
- `from_env` / `get_default_saxo_client` remain UNCONDITIONALLY SIM-only —
  even with the unlock env set. The daemon constructs its client exclusively
  through `from_env`, therefore it has NO code path to LIVE regardless of
  environment contents. LIVE is reachable only by explicitly constructing
  `SaxoClient(live_token_provider, base_url=<LIVE URL>)`, which only the
  probe does.

### 3.4 Token — no new auth work

The probe constructs `LiveTokenProvider` (from
`data/alt_data/saxo_marketdata_auth.py`, store
`~/.alphalens/saxo_auth_live/token_store.json`, app `bracket-keeper`) and
injects it as the client's `token_provider` (interface already matches:
`get_access_token` / `invalidate`). The operator confirmed 2026-08-10 that
this LIVE app carries full equity-trading permissions, so the same OAuth
chain (and the existing `alphalens-saxo-marketdata-refresh` keep-alive
timer) serves order placement. Unverifiable until a live attempt: whether
LIVE order placement additionally demands an elevated session
(`FullTradingAndChat`) or a second factor; the probe surfaces this as a loud
failure, not a silent skip.

### 3.5 Existing safeties remain additive

A LIVE order therefore requires FOUR simultaneous explicit acts:

1. `ALPHALENS_SAXO_LIVE_ORDERS_UNLOCK=<today UTC>` (per-process env);
2. `ALPHALENS_BROKER_ALLOW_ORDERS=1` (§P2 rail, checked per order method);
3. `TORB_LIVE_CONFIRM=1` (probe's own gate);
4. explicit construction with the LIVE base URL + LIVE token provider
   (no factory produces this).

Probe-side: `--qty` hard max 3 (default 1), one liquid low-priced name,
`try/finally` flatten.

## 4. SIM/LIVE parallelism

### Horizon 1 (this PR): SIM experiments ∥ LIVE probes — fully parallel

SIM and LIVE are separate Saxo worlds (gateways, logon systems, accounts,
money, tokens). Per-process env means the probe's unlock does not exist for
the daemon; and the daemon's `from_env` path cannot return LIVE anyway. The
SIM daemon keeps managing picks all day while the operator runs a LIVE probe
in another terminal. No interference by construction.

### Horizon 2 (future ADR): a LIVE *daemon* — out of scope here

Continuous LIVE management would be a SECOND daemon instance and needs
per-environment state separation first: `~/.alphalens/broker_orders/`
journals are single-path today (SIM and LIVE daemons would collide), the
`KILL` gate is global, heartbeat gauges/unit names are singletons, and the
pick-source-to-instance mapping (which client feeds which instance) is
execution-product architecture (ties into the parked 2B repo move). Design
it AFTER the probe answers the native-ratchet/slippage questions.

## 5. Rail-test evolution (never weakening)

New cases in `test_saxo_sim_only_rail.py`:

- unlock with wrong / yesterday's / tomorrow's date + LIVE URL → refused;
- unlock with today's UTC date + non-SIM URL → accepted (new positive);
- `from_env` WITH the unlock set → still SIM-only (daemon-isolation pin);
- locks (b) and (d) assertions unchanged.

## 6. Rejected alternatives

- **Flip `LIVE_TRADING_ENABLED` globally** — opens LIVE to every client
  consumer including the daemon; exactly the isolation failure this design
  exists to prevent.
- **`SAXO_ENV=live` env switch** — ADR 0014 deliberately forbade an env
  switch (operator `.env` confusion guard); it stays forbidden. The unlock
  differs in kind: it is date-bound, refuses by default, and only widens the
  constructor guard — it never redirects a default construction path.
- **Separate `SaxoLiveClient`** — duplicates the entire retry/throttle/401
  seam for a one-shot probe; YAGNI.

## 7. Out of scope

- Continuous LIVE trading / LIVE daemon (Horizon 2, future ADR).
- Any change to pick selection or the symmetric selection/execution
  separation.
- Lifting anything for unattended use — the unlock is attended-only by
  doctrine (ADR 0015 records this).

## 8. Activation contract (operational, 2026-08-10)

Building this PR does NOT schedule a LIVE run. Executing `torb_live_probe.py`
today requires, in order: (1) zen pre-merge review clean on this PR;
(2) the 15:30 SIM battery (CRUX-1/2) green; (3) the LIVE feed probe
confirming `DelayedByMinutes == 0`; (4) the operator's explicit go. If any
gate fails, the lift ships and WAITS.
