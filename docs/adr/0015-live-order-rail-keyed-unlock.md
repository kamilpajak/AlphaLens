# ADR 0015 — LIVE order rail: keyed day-bound unlock (attended probes only)

- **Status:** Accepted
- **Date:** 2026-08-10
- **Supersedes:** — (narrows, does NOT reverse, the ADR 0014 SIM-only rail)
- **Related:** ADR 0014 (broker-agnostic execution layer, SIM-only rail),
  design memo `docs/research/saxo_live_order_rail_lift_design_2026_08_10.md`

## Context

1. **ADR 0014 made LIVE structurally unreachable and named the exit.** Four
   independent locks (constructor URL guard, `LIVE_TRADING_ENABLED = False`,
   loud `SAXO_ENV` failure, no-LIVE-URL-string source scan) ensure no single
   edit can quietly open a LIVE path. Lock (b) was explicitly "flipped only
   by a future ADR". This is that ADR.

2. **What changed:** the execution layer is being validated as a standalone
   product (future separate repo; multiple pick-source clients). Three
   execution-fidelity questions are SIM-unanswerable — native
   `TrailingStopIfTraded` ratchet on live ticks, narrow-amend level
   reposition, real implementation shortfall — because SIM fills are
   synthetic at reference price with zero spread. Answering them requires an
   attended, throwaway-size LIVE order (`torb_live_probe.py`: qty ≤ 3,
   `try/finally` flatten).

3. **What did NOT change:** no validated alpha exists; the pre-registration
   `capital_deploy_clause` still bars deploying AlphaLens picks with real
   capital. Execution-fidelity validation is Gate-1 engineering and is
   independent of that bar.

## Decision

Permit LIVE order-rail access ONLY through a keyed, self-expiring,
per-process unlock — never through a default construction path:

1. The `SaxoClient` constructor accepts a non-SIM base URL **iff**
   `ALPHALENS_SAXO_LIVE_ORDERS_UNLOCK` equals the current UTC date
   (`YYYY-MM-DD`). Any other value — absent, stale, future — raises
   `SaxoLiveEnvironmentBlockedError` exactly as before. A successful unlock
   logs a loud warning (base URL only; never token material).
2. `LIVE_TRADING_ENABLED` remains `False` (the structural default), and
   `from_env` / `get_default_saxo_client` remain UNCONDITIONALLY SIM-only —
   the unlock does not exist for them. The daemon constructs exclusively
   through `from_env`, so it has no code path to LIVE regardless of
   environment contents.
3. The LIVE gateway URL and the LIVE token provider are supplied by the
   calling process (the probe), keeping the no-LIVE-URL-in-`brokers/`
   source scan intact. Auth reuses the `bracket-keeper` LIVE OAuth chain
   (`~/.alphalens/saxo_auth_live/`), operator-confirmed to carry full
   equity-trading permissions.
4. Existing safeties stay additive: `ALPHALENS_BROKER_ALLOW_ORDERS=1`
   remains required per order method; the probe's own `TORB_LIVE_CONFIRM=1`
   remains required. A LIVE order therefore takes four simultaneous
   explicit acts.
5. **Attended-only.** The unlock is set interactively for one probe process
   and dies with it. Writing it into any unit file, profile, or `.env` is a
   doctrine violation, not a supported configuration.

## Consequences

- SIM experimentation and LIVE probing run in parallel with zero
  interference: separate Saxo environments, accounts, tokens, and processes;
  isolation is structural (no shared code path), not procedural.
- The rail test evolves without weakening: refusal cases for wrong-date
  unlocks, a new positive for today's date, and a pin that `from_env` stays
  SIM-only even with the unlock set.
- A continuous LIVE daemon (real-money management as a product) is
  explicitly OUT OF SCOPE: it requires per-environment state separation
  first (`broker_orders/` journals, `KILL` gate, heartbeat/unit names,
  pick-source-to-instance mapping) and its own future ADR, informed by this
  probe's findings.
- Rollback is trivial: unset the env var (or let the date pass) and the
  rail is exactly ADR 0014 again; reverting the PR removes the unlock seam
  entirely.
