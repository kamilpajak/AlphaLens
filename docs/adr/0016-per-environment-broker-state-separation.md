# ADR 0016 — Per-environment broker state separation (two-instance model)

- **Status:** Accepted
- **Date:** 2026-08-10
- **Supersedes:** — (extends ADR 0014/0015; changes state layout, not the
  order rail)
- **Related:** ADR 0014 (SIM-only rail), ADR 0015 (keyed unlock — names
  this work as the LIVE-daemon precondition), design memo
  `docs/research/broker_env_state_separation_design_2026_08_10.md`

## Context

1. **ADR 0015 validated the LIVE order rail and stopped.** Attended probes
   (2026-08-10) LIVE-validated both exit-execution paths (native trailing
   ratchet, narrow-amend reposition, shortfall 0.00). The next product
   step — a continuous LIVE daemon — was declared OUT OF SCOPE pending
   "per-environment state separation (`broker_orders/` journals, `KILL`
   gate, heartbeat/unit names, pick-source-to-instance mapping)".

2. **Today every piece of daemon state is a global singleton**: one
   `~/.alphalens/broker_orders/` tree resolved by six scattered
   `Path.home()` joins (including a duplicated KILL-path constant), one
   hardcoded Prometheus `job="broker-manager"` namespace, one pick inbox
   with no environment dimension. Two instances on one host would corrupt
   each other's journals, clobber each other's gauges, and drain each
   other's picks.

3. The execution layer is being validated as a standalone product
   (multiple future pick-source clients; repo split PARKED as 2B). State
   namespacing by instance is a prerequisite for that shape regardless of
   the LIVE arm.

## Decision

**Two-instance model: same binary, instance identity via
`ALPHALENS_BROKER_ENVIRONMENT ∈ {sim, live}` (default `sim`), all mutable
state namespaced per instance.**

1. One path seam (`brokers/automanager/state_paths.py`) owns every state
   path and metric job name. Journals, pick inbox, and instance KILL live
   under `~/.alphalens/broker_orders/<env>/`; execution-quality telemetry
   under `~/.alphalens/exec_quality/<env>/` (T8 no-pooling: SIM and LIVE
   fills are distinct measurement sources and must never share a file).
2. **KILL is layered**: `broker_orders/<env>/KILL` stops one instance;
   the legacy `broker_orders/KILL` (parent level) is retained as the
   GLOBAL kill honored by every instance — the operator's memorized
   emergency command keeps working, and gains scope rather than losing it.
3. **Prometheus jobs are per-instance** (`broker-manager-<env>`,
   `broker-manager-<env>-stream`, `live-price-stream-<env>`); gauge names
   are unchanged. Alert rules follow the sim jobs now; LIVE rules land
   only when a LIVE instance exists to be absent.
4. **A pick belongs to exactly one instance** (`broker arm --env`,
   default sim). The daemon drains only its own inbox — cross-instance
   placement is impossible by construction, not by convention.
5. **Fail-loud legacy guard:** the daemon and journal CLI refuse to run
   against a pre-migration flat layout (`BrokerStateLayoutError`), because
   starting with empty journals while the broker holds positions would
   silently degrade protection to adopt/alert paths. Migration is a
   one-time operator `mv` (runbook in the memo §6).
6. **LIVE instance boot stays structurally blocked**: `broker manage`
   refuses `ALPHALENS_BROKER_ENVIRONMENT=live` until the LIVE
   client-construction path exists. The ADR 0015 day-bound unlock is
   attended-only and explicitly NOT the mechanism for a daemon; the
   standing-LIVE authorization model is a separate future ADR (0017),
   informed by the probe findings.

## Consequences

- A LIVE daemon becomes an ADDITIVE deployment (new unit + ADR 0017
  client wiring), not a modification of the SIM instance — the soaked SIM
  path is never touched by LIVE rollout.
- SIM keeps running exactly as before, with a one-time state `mv` and a
  Prometheus job rename (`broker-manager` → `broker-manager-sim`); stale
  `.prom` files must be removed at deploy or the old alert keeps reading
  a frozen gauge.
- The duplicated KILL-path constant and the six scattered path joins
  collapse into one seam — future state moves (repo split 2B, transport
  Stage 2) touch one module.
- Cost: every operator command is now instance-scoped; muscle memory must
  add `--env live` (or the session env var) when the LIVE instance
  arrives. Mitigated by safe defaults (everything defaults to sim) and
  the global KILL.
- Risk accepted: a wrong `ALPHALENS_BROKER_ENVIRONMENT` in a unit file
  mislabels an instance. Mitigated by the in-unit `Environment=` pin, the
  runbook ban on setting it in `/etc/alphalens/env` (EnvironmentFile
  overrides drop-ins — 2026-08-10 incident lesson), and the live-boot
  block (6).
