# ADR 0017 — Standing-LIVE authorization for the unattended broker-manager daemon

- **Status:** Proposed — pending the operator decisions in design memo §8;
  becomes Accepted with the front-3 implementation PRs
- **Date:** 2026-08-10
- **Supersedes:** — (extends ADR 0015; the attended day-bound unlock
  survives verbatim for probes)
- **Related:** ADR 0014 (SIM-only rail), ADR 0015 (keyed day-bound unlock),
  ADR 0016 (per-environment state separation), design memo
  `docs/research/broker_live_daemon_arm_design_2026_08_10.md`

## Context

1. ADR 0015 authorized attended, throwaway-size LIVE probes and explicitly
   declared a continuous LIVE daemon out of scope, naming two
   preconditions: per-environment state separation (delivered — ADR 0016,
   deployed) and a standing authorization model to replace attended-ness
   (this ADR). Writing the day-bound unlock into a unit file remains a
   doctrine violation — a daemon must not depend on a self-expiring,
   human-rotated token.

2. The execution-fidelity questions that justified LIVE probes are
   answered (native trailing ratchets server-side; narrow-amend
   repositions; measured shortfall 0.00). The remaining gap between the
   soaked SIM manager and a real-money manager is authorization,
   configuration isolation, and rollout — not order mechanics.

3. Governance framing (operator-decided): the execution layer is being
   validated as a standalone product with multiple future pick-source
   clients. A bounded-risk LIVE instance validates the PRODUCT; it is not
   an alpha-deployment decision — the research-side
   `capital_deploy_clause` remains a separate, unweakened gate (ADR 0014
   two-gate framing).

## Decision

**A LIVE instance is authorized by a standing, structural conjunction —
never by a single flag, never by the attended unlock:**

1. **Constructor capability (the one ADR 0015 modification, named):**
   `SaxoClient.__init__` accepts a non-SIM base URL also when the new
   keyword-only `standing_live_authorized=True` is passed — and in that
   case the constructor ITSELF verifies the account-bound grant (3), so a
   caller bypassing the factory still needs the environment grant (Python
   cannot restrict who passes a keyword; the check therefore lives in the
   client). `from_env` and `get_default_saxo_client` never pass it and
   remain unconditionally SIM; the attended day-bound unlock path is
   untouched.
2. **A separate LIVE factory** (`create_saxo_broker_live_from_env`) is the
   only caller of that keyword, invoked only by the `env=live` branch of
   the composition root (which replaces the ADR 0016 D7 boot block). The
   registry `"saxo"` path keeps zero LIVE capability. The LIVE gateway URL
   is imported from outside `brokers/` — the ADR 0014 lock (d) source scan
   survives verbatim.
3. **Account-bound standing grant:** `ALPHALENS_SAXO_LIVE_STANDING` must
   equal the resolved `SAXO_LIVE_ACCOUNT_KEY` (a distinct, LIVE-only env
   name). A bare boolean arm is rejected: a leaked truthy value or a
   partially copied unit file must be structurally inert.
4. **Safety-rail boot-assert:** `env=live` refuses to boot unless all six
   of `MAX_OPEN`, `PORTFOLIO_GROSS_FRAC`, `DAILY_LOSS_LIMIT_R`,
   `SIZING_EQUITY`, `EXIT_POLICY`, and `MAX_FEE_BPS` are explicitly set
   within live bounds — because the code defaults are permissive (gross
   1.0, sized off the raw account snapshot, exit policy silently
   `setup_static`), "forgot one pin" must fail loud, not trade wrong.
   Correspondingly, `ALPHALENS_BROKER_*` rails leave the shared
   `/etc/alphalens/env` and are pinned per-unit on BOTH instances.
5. **Existing arms unchanged and still required:** `ALLOW_ORDERS=1` per
   placement site, layered KILL (ADR 0016), chain-alive gating. Sizing is
   bounded by `min(pinned equity, account snapshot)` plus a round-trip
   fee floor that skips-and-alerts rather than sizing up.

**Safety comes from the bounded worst case, not from a human watching:**
with every gate open, the instance can hold `MAX_OPEN` positions of
~1%-of-frame risk each with a resting server-side disaster stop, under a
daily loss breaker — that is the designed 24/7 blast radius.

## Consequences

- The attended-only doctrine of ADR 0015 is narrowed, not repealed: probes
  keep the day-bound unlock; the daemon gets a standing grant whose
  authority is account-bound and revocable (KILL, disarm, disable).
- The SIM instance and every default construction path remain structurally
  LIVE-incapable; the LIVE-capable surface is one factory + one keyword,
  both covered by a new rail test file.
- The grant does not self-expire — decommissioning a LIVE unit is a
  procedural step (disable + remove pins), accepted and documented in the
  runbook.
- Price-feed topology constraint (per-login elevation): during the soak
  the LIVE daemon is the sole elevated holder and the SIM hybrid yields
  its LIVE prices; a cross-process shared reader is a standing need
  deferred beyond front 3.
- Rollback is layered and ordered (instance KILL → global KILL → disarm →
  manual flatten → stop-unit last); stopping the daemon first is
  explicitly the wrong move while positions are open.
