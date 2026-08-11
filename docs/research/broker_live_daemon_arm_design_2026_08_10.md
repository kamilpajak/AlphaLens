# LIVE broker-manager instance + standing-LIVE authorization — design (Track B front 3)

- **Status:** LOCKED (2026-08-10) — all ten §8 operator decisions taken PER
  RECOMMENDATION (operator, 2026-08-10): 10k zł frame · 100 zł/pick ·
  MAX_OPEN=1 · 1.0R daily · fee floor 100 bps at the 10k frame ·
  trailing_atr attended → native before first unattended night ·
  LIVE sole elevated holder (SIM yields prices for the soak) · `[live]`
  prefix in the shared chat · soak ≥3 clean attended round-trips + drills ·
  grant decommission stays procedural (runbook note)
- **Date:** 2026-08-10
- **ADR:** [ADR 0017 (Proposed)](../adr/0017-standing-live-authorization.md)
- **Predecessors:** ADR 0014 (SIM-only rail), ADR 0015 (attended day-bound
  unlock; declares this arm out of scope), ADR 0016 (per-env state
  separation, DEPLOYED — LIVE boot structurally blocked pending this design),
  memos `saxo_live_order_rail_lift_design_2026_08_10.md`,
  `broker_env_state_separation_design_2026_08_10.md`.
- **Method note:** synthesized from three independent lens drafts
  (risk-first / ops-first / product-first) cross-examined by two
  code-verifying adversarial reviews; every load-bearing claim below was
  verified against `main` `de293a8a`.

## 0. Goal and non-goals

Goal: a second, ADDITIVE daemon instance (`ALPHALENS_BROKER_ENVIRONMENT=live`,
ADR 0016) that places and manages REAL-MONEY orders on Saxo LIVE, unattended,
with the same manager loop that has been soaking on SIM — under a standing
authorization model that replaces the attended day-bound unlock of ADR 0015.

Non-goals (explicitly out of front 3): a cross-process shared price reader
(standing need, deferred — see §6), a second Saxo app registration, any YAML
config layer, the 2B repo split (PARKED), any change to selection (R2
symmetric separation), any weakening of the research-side
`capital_deploy_clause` (ADR 0014 two-gate framing: this is bounded-risk
product validation of the execution layer, not an alpha-deployment decision).

## 1. Standing authorization (ADR 0017 core)

The ADR 0015 unlock is date-bound and attended-only by doctrine; it survives
verbatim for probes and is not reused, widened, or made non-expiring.

**Mechanism: an explicit constructor capability + an account-bound standing
grant.** A LIVE order requires ALL of, simultaneously:

1. `ALPHALENS_BROKER_ENVIRONMENT=live` — instance identity (ADR 0016),
   pinned in-unit; the SIM unit pins `sim` in-unit. CORRECTED 2026-08-11:
   `EnvironmentFile=` overrides ALL `Environment=` lines (in-unit
   included), so the pins hold only because the var is BANNED from the
   shared file — the §3 strip is load-bearing and the boot-assert is the
   tripwire (it caught leaked `MAX_OPEN`/`ALLOW_ORDERS` on first boot).
2. **Constructor widening (the one named ADR 0015 modification):**
   `SaxoClient.__init__` gains a keyword-only `standing_live_authorized:
   bool = False`; the guard becomes: refuse any `base_url != SIM_BASE_URL`
   unless `_live_orders_unlocked()` (attended probe path, unchanged) **or**
   `standing_live_authorized is True` **and the constructor itself
   verifies the §1.3 account-bound grant** (`ALPHALENS_SAXO_LIVE_STANDING`
   present and equal to `SAXO_LIVE_ACCOUNT_KEY`). The grant check lives IN
   the constructor, not only in the factory, because Python cannot stop an
   arbitrary caller from passing the keyword — with the in-constructor
   check, bypassing the factory still requires the environment grant (the
   rail test pins direct-construction refusal without it). `from_env` /
   `get_default_saxo_client` NEVER pass it — they stay unconditionally SIM,
   byte-identical, and `test_from_env_ignores_unlock_still_sim_only` holds
   untouched. Without this widening no factory can construct a LIVE client
   at all (the guard at `client.py:131-138` is hard).
3. **Account-bound standing grant:** `ALPHALENS_SAXO_LIVE_STANDING=<live
   account key>`, pinned in-unit, and required by the LIVE factory to equal
   the resolved `SAXO_LIVE_ACCOUNT_KEY`. Deliberately NOT a bare `=1`: a
   leaked truthy value or a partially-copied unit file is structurally
   inert, because the grant names the specific resource it authorizes
   (the house CLI doctrine: resource-bound confirmation over `-y`).
4. `ALPHALENS_BROKER_ALLOW_ORDERS=1` — the existing per-placement-site arm,
   unchanged, pinned in-unit (never in the shared env file, where it would
   arm SIM too).
5. No KILL — global `broker_orders/KILL` or instance `broker_orders/live/KILL`
   (ADR 0016 layered KILL, unchanged).
6. LIVE auth chain alive — dead/stale chain ⇒ `ensure_alive` returns
   `alive=False` ⇒ the tick skips placement (fail-safe).

**Locks that survive verbatim:** `LIVE_TRADING_ENABLED = False` stays as a
doctrine flag — it is read nowhere at runtime (verified), so it guards
nothing and flipping it is not part of this design; `SAXO_ENV != sim` still
fails loud in `from_env`; lock (d) — no LIVE URL literal under `brokers/` —
is preserved by IMPORTING the URL (§2).

**Single-misconfiguration analysis.** No single wrong value reaches LIVE:
identity, the constructor capability (code path, not env), the account-bound
grant, and allow-orders are four independent conditions, three of them
in-unit pins. The dangerous single mistakes are of the OTHER kind — a LIVE
unit missing a RISK pin — and are closed by the §3 boot-assert. Residuals
(honest): (a) the standing grant does not self-expire; a decommissioned
LIVE unit left enabled retains authority until disabled — mitigated by
KILL and `systemctl disable`, procedurally; (b) the constructor keyword is
reachable by any future caller (Python cannot restrict callers) — mitigated
structurally by the in-constructor grant check above and by the rail test,
procedurally by review; (c) LIVE-path tests must NEVER construct against
real credentials — the rail test exercises refusal paths with sentinel
providers only (ADR 0015 test pattern), and no test may set the grant pair
to real values.

## 2. LIVE client construction

**Composition root.** The ADR 0016 D7 hard-raise in `build_default_deps`
(`control_loop.py:1182-1189`) becomes a branch: `env == live` → build via
the new LIVE factory; `env == sim` → today's path byte-identical. Everything
downstream (D4 legacy guard, journals, deps) is untouched.

**Factory** `create_saxo_broker_live_from_env()` — NEW function in
`saxo/broker.py`, parallel to (not inside) the SIM factory, so the registry
`"saxo"` path and `get_default_saxo_client()` keep zero LIVE capability:

- Base URL **by import, never literal**: `from
  ....data.alt_data.saxo_marketdata_client import LIVE_API_BASE_URL`
  (symbol verified at `saxo_marketdata_client.py:23`, outside `brokers/` —
  the lock (d) source scan sees only an import).
- Asserts the §1 grant (`ALPHALENS_SAXO_LIVE_STANDING ==
  SAXO_LIVE_ACCOUNT_KEY`) and the §3 boot-assert BEFORE constructing.
- `SaxoClient(live_provider, base_url=LIVE_API_BASE_URL,
  standing_live_authorized=True)` — the only caller of the new keyword.
- `SaxoBroker(client, account_key=os.environ["SAXO_LIVE_ACCOUNT_KEY"])` —
  a DISTINCT env name. Reusing `SAXO_ACCOUNT_KEY` (SIM, shared env) would
  silently point LIVE at the SIM account when the in-unit override is
  forgotten; the distinct name makes that a loud `KeyError`.

**Token provider.** The `saxo_auth_live` chain (app `bracket-keeper`,
operator-confirmed full trading permissions) is reused. What genuinely needs
building (narrow — two of three previously-assumed gaps already exist):

- `LiveTokenProviderAdapter` implementing the `TokenProvider` Protocol
  (`get_access_token → access_token`, `refresh_now → force_refresh`) over
  `saxo_marketdata_auth.LiveTokenProvider`, plus an injected
  `NotificationPort` so a dead LIVE chain pages a `[live] chain lost`
  Telegram alert (mirroring SIM `_chain_lost`) instead of degrading
  silently.
- **`invalidate()` / 401 semantics — a real gap found in review:**
  `LiveTokenProvider` keeps no in-memory state and has no `invalidate`; a
  401 on a LIVE ORDER call would re-read the same disk token and tight-loop.
  The adapter must implement SIM-style rejected-token memory
  (`tokens.py:397-404` pattern): after `invalidate()`, `get_access_token`
  must not return the rejected token — re-read the store; if the store still
  holds the rejected token, refresh under the flock. **And the revoked-chain
  case must terminate, not storm:** a refresh that itself fails with
  `invalid_grant`/401 (refresh token revoked — password reset, permission
  withdrawal) latches the chain DEAD (SIM `_chain_lost` pattern: alert
  once, raise, never auto-retry the token endpoint) — one failed refresh is
  the terminal signal, so a revoked chain can never produce a refresh storm
  against Saxo.
- **NOT built (already exists, verified):** adopt-before-refresh.
  `LiveTokenProvider.access_token()` already runs under the per-host flock,
  re-reads the store every call, adopts a sibling's rotation when margin
  remains, and persists rotations atomically. The safety invariant to state
  and test is "**flock-serialized adopt-then-refresh**" — NOT "a single
  designated refresher" (the daemon's own `access_token()` legitimately
  rotates when past margin; the flock is what makes N same-host consumers
  safe).

`SessionKeeper` integrates unchanged over the adapter (`ensure_alive` per
tick; the tick never force-refreshes — verified `control_loop.py:1286`).

## 3. Per-instance config + the boot-assert

**The most dangerous verified fact in this design:** the code defaults of
the safety rails are permissive — `DEFAULT_MAX_OPEN = 3`,
`DEFAULT_PORTFOLIO_GROSS_FRAC = 1.0` (100% gross!),
`DEFAULT_DAILY_LOSS_LIMIT_R = 3.0` (`safety.py:33-35`), and sizing equity is
the RAW live account snapshot. A LIVE unit missing one pin would trade 100%
gross of the real balance. Two mechanisms close this jointly; each alone is
insufficient:

1. **Strip `ALPHALENS_BROKER_*` rails from the shared `/etc/alphalens/env`
   entirely** and pin them per-unit (SIM unit gains explicit pins too, so
   the strip does not silently disarm SIM). No silent cross-instance
   inheritance. (Stripping ALONE would make the permissive code defaults
   apply — hence:)
2. **LIVE boot-assert:** `env=live` REFUSES to boot unless ALL SIX of
   `ALPHALENS_BROKER_MAX_OPEN`, `ALPHALENS_BROKER_PORTFOLIO_GROSS_FRAC`,
   `ALPHALENS_BROKER_DAILY_LOSS_LIMIT_R`, `ALPHALENS_BROKER_SIZING_EQUITY`,
   `ALPHALENS_BROKER_EXIT_POLICY`, and `ALPHALENS_BROKER_MAX_FEE_BPS` are
   explicitly set AND within live bounds (caps table below). Unset and
   set-to-dangerous both fail loud at boot, before any broker I/O.
   `EXIT_POLICY` is asserted explicit-set (not defaulted) because a
   copy-paste unit missing the pin would silently run `setup_static` —
   positions stay protected (never-naked is policy-independent: the
   disaster stop and planned exits do not depend on the policy), but the
   soak would silently validate the wrong exit mechanism. `MAX_FEE_BPS`
   must be set and positive — an unset fee floor is a permissive default
   exactly like the rails.

**In-unit pins for the LIVE unit (initial values; operator finalizes §8):**

| Var | Initial LIVE value | Note |
|---|---|---|
| `ALPHALENS_BROKER_ENVIRONMENT` | `live` | identity (ADR 0016) |
| `ALPHALENS_SAXO_LIVE_STANDING` | `<live account key>` | §1 grant, account-bound |
| `SAXO_LIVE_ACCOUNT_KEY` | `<live account key>` | distinct from SIM's var |
| `ALPHALENS_BROKER_ALLOW_ORDERS` | `0` at first deploy → `1` at attended arm | inert-first rollout (§7) |
| `ALPHALENS_BROKER_MAX_OPEN` | `1` | boot-assert bound: ≤ 2 |
| `ALPHALENS_BROKER_PORTFOLIO_GROSS_FRAC` | `0.25` | NOT 0.03 — a 100 zł-risk pick at a ~5% stop is ~2 000 zł notional ≈ 0.20 of the 10k frame; 0.03 would CAPACITY-refuse every pick. Bound: ≤ 0.5 |
| `ALPHALENS_BROKER_DAILY_LOSS_LIMIT_R` | `1.0` | bound: ≤ 2.0 |
| `ALPHALENS_BROKER_SIZING_EQUITY` | `10000` (zł frame) | §4; bound: > 0 |
| `ALPHALENS_BROKER_EXIT_POLICY` | `trailing_atr` (§6) | startup-once; boot-assert: explicit-set |
| `ALPHALENS_BROKER_MAX_FEE_BPS` | `100` (§4 — NOT 50) | boot-assert: set and > 0 |
| `ALPHALENS_BROKER_STREAMING_ENABLED` | `0` | order-WS early-wake needs its own LIVE re-validation |
| `ALPHALENS_SAXO_LIVE_PRICES` | `1` | LIVE daemon = sole elevated holder (§6) |
| `ALPHALENS_LIVE_MARKET_EXITS` | `1` (CORRECTION 2026-08-11 — omitted from the original table; without it no TP tranche fires while the trailed stop works) | TP tranches are live-market sells off the feed (broker holds only the disaster stop) |
| `ALPHALENS_TEXTFILE_DIR` | `/var/lib/node_exporter/textfile` | per-instance `.prom` via job label |

`SAXO_LIVE_APP_KEY/SECRET/REDIRECT_URL` keep their LIVE-specific names in
the shared env (only the LIVE factory and marketdata auth read them).
`TELEGRAM_*` stay shared; labeling in §5.

## 4. Sizing + risk budget

- **Equity:** `ALPHALENS_BROKER_SIZING_EQUITY` pinned; effective sizing
  equity = **`min(pinned, account_snapshot)`** — survives both a pin set
  too high and a balance below the frame. Raw-snapshot sizing is rejected
  (would silently scale picks to the full real balance).
- **Per-pick risk:** ≈ 100 zł (1% of the 10k zł frame) via the brief's
  `suggested_size_pct` against the pinned equity; `MAX_OPEN=1` bounds the
  book to one such position.
- **Fee floor (structural gap today) — with the honest fee equation:**
  `compute_setup_plan` floors qty with no notional check. Saxo LIVE PL:
  commission 0.08% min **$1 per fill** + 0.25% FX **per conversion** (a
  PLN account converts on the buy AND on the sell). Round-trip cost at
  notional N (USD):

  `fee_rt(N) ≈ 50 bps (FX, size-independent) + 2 × max($1, 0.0008·N) / N`

  Anchor points: N=$500 → 50 + 40 = **~90 bps**; N=$1000 → 50 + 20 =
  ~70 bps; N=$1250+ → ~66 bps asymptote (commission ad-valorem from
  ~$1250). **Consequence (adversarial-review catch): a 50 bps floor is
  UNSATISFIABLE — 50 bps is already the FX base alone**, so the flagship
  configuration (100 zł risk, ~5% stop → ~$500 notional, ~90 bps) would
  fee-reject every pick and deadlock the soak. The floor and the frame
  must be chosen jointly (operator decision §8.5):
  - **(a) recommended for the soak:** `MAX_FEE_BPS=100` at the 10k zł
    frame — accepts ~0.9% round-trip cost drag as the price of
    execution-fidelity validation at the intended 100 zł risk (the soak
    validates EXECUTION, not P&L; the drag is measured and reported in
    `exec_quality/live/`);
  - **(b) cost-optimal:** keep a tighter ~70 bps floor by requiring
    N ≥ ~$1000 — reachable only with tighter stops (≤2.5% at 100 zł risk)
    or a bigger risk/frame, which changes the operator's stated budget.

  Mechanics regardless of the number: a pick below the floor is
  SKIPPED-AND-ALERTED, never silently sized UP (sizing up would breach
  the risk budget); the refusal is a fee fact — it never feeds back into
  selection (R2).
- **Deadlock tripwire (go/no-go input, §7):** if the first N armed picks
  are all fee-rejected, the floor/frame pair is inconsistent with the
  brief's stop widths — operator adjusts per §8.5 or aborts; the per-pick
  fee-rejection alert makes this visible from pick one.
- **FX:** PLN account, USD instruments — the existing
  `build_fx_conversion` + `sizing_buffer_pct` path fires as-is; the fee
  floor is computed AFTER the FX buffer, on the real filled notional.
- **`DAILY_LOSS_LIMIT_R` accounting:** before the first armed session,
  verify the daily-loss input for `env=live` reads the LIVE account's
  realized P&L (a breaker reading the wrong account is worse than none).

## 5. Deploy topology

- **`alphalens-broker-manager-live.service`** — clone of the SIM unit
  (host venv, `Type=simple`, `Restart=on-failure`, same ExecStart), with
  the §3 in-unit pin block after `EnvironmentFile=`. Header documents the
  layered KILL (global halts both; `live/KILL` halts LIVE only). Purely
  additive; SIM unit untouched except gaining its own explicit rail pins
  (§3.1).
- **LIVE-chain refresh timer:** version the currently-unversioned VPS unit
  under its PRODUCTION name `alphalens-saxo-marketdata-refresh.{service,timer}`
  into `deploy/systemd/` (repo↔prod gap closed; no rename — the prod name
  is load-bearing operator knowledge). Add
  `ExecStopPost=…alphalens-emit-job-metrics saxo-marketdata-refresh` (it
  emits nothing today → unmonitored) + an `AlphalensJobStale`/`MetricMissing`
  pair. The timer is the keep-alive floor; per-tick consumers adopt under
  the flock (§2 invariant framing).
- **Prometheus LIVE rules** (hand-synced: cp → promtool → HUP): copy the
  `-sim` blocks to `broker-manager-live` / `live-price-stream-live`,
  keeping the anti-false-positive gates (`subscribed_uics > 0`; the
  three-way stale AND). Land value-based `HeartbeatStale` with the unit;
  land `absent()`-based `Missing` rules ONLY once the LIVE unit is
  enabled-and-expected-up (ADR 0016 D5 precedent — an absent-rule for a
  not-yet-running instance pages immediately).
- **Telegram:** inject a `[live]` / `[sim]` prefix at the one composition
  root that knows the environment (the `build_default_deps(notify=…)` call
  site in `alphalens_cli/commands/broker.py`); without it two daemons
  produce indistinguishable alert streams. Separate chat/topic for LIVE =
  operator decision (§8).
- **README:** rewrite the stale §9 "$100 live escape" sketch
  (`ALPHALENS_BROKER_LIVE=1`, $1000) to this design; document the
  LIVE runbook (bootstrap, arm, rollback ladder §7).

## 6. Exit execution + price feed on LIVE

**Exit policy sequencing** (both paths LIVE-validated 2026-08-10, n=2):

1. **Attended soak: `trailing_atr` (bot-amend)** — continuity: it is the
   exact geometry+code path soaked in the production SIM hybrid; no new
   exit mechanism meets real money on day one.
2. **Before the first UNATTENDED night: switch to native
   `TrailingStopIfTraded`** — the server-side ratchet survives daemon
   death, network loss, and restarts; bot-amend's stop is static between
   ticks and during outages, which is unacceptable protection for an
   unattended 24/7 instance. Precondition: native must first be exercised
   INSIDE the manager loop attended (the n=2 probe validated the venue
   mechanics, not the manager wiring).

Netting constraints stand: no position-attached exits, self-contained OCO
only, `{stop}→{OCO}` = cancel-then-place with an unavoidable naked window.
LIVE mitigation for the window: on restart, the FIRST reconcile action
re-asserts disaster protection before any other work (existing STOP-ONLY
protection ordering — verify it holds on the LIVE branch).

**Price feed — sole-elevated-holder topology.** LIVE real-time prices
require the session elevated to `FullTradingAndChat`; under `OrdersOnly`
Saxo silently serves 15-min-delayed data (trust `DelayedByMinutes==0`; the
entitlements endpoint lies). The elevation slot is **per Saxo login, not
per app** (a second app registration does NOT help — verified against the
stream code's own eviction semantics), and `get_shared_price_stream` is a
per-process singleton. Two elevated consumers on one login ping-pong-demote
each other (ReclaimLimiter 4×/hr) with SILENT price-staleness as the
failure mode. Therefore: **the LIVE daemon becomes the sole elevated
holder; the SIM instance sets `ALPHALENS_SAXO_LIVE_PRICES=0` for the
duration of the soak** (SIM's cost is stale prices on virtual money —
zero). The LIVE daemon's order client and price stream share the one
login/session. A cross-process shared reader serving both instances is a
STANDING need deferred beyond front 3 (not YAGNI-never), revisited when SIM
needs its LIVE prices back. Add a `DelayedByMinutes>0` detection log/gauge
so silent demotion pages.

## 7. Rollout / soak (front 4 preview)

1. **Deploy inert:** LIVE unit boots with `ALLOW_ORDERS=0` — constructs
   the LIVE client (loud unlock warning, base URL only), keeps the chain
   alive, reads the account, reconciles, journals under
   `broker_orders/live/`, places NOTHING. Verify: heartbeat under
   `broker-manager-live`, `DelayedByMinutes==0`, dry sizing plan computed.
2. **Fire drills before arming:** chain-loss (invalidate token → `[live]`
   alert), both KILL layers, manual-flatten recipe rehearsed
   (cancel `StopIfTraded` → market SELL summed per-lot → cancel entry
   buys), fee-floor rejection visible.
3. **Attended arm:** flip `ALLOW_ORDERS=1` in-unit + restart, operator
   present; `arm --env live` one liquid US name; MAX_OPEN=1.
4. **Go/no-go for the first unattended night:** ≥3 clean attended
   round-trips spanning entry→OCO exit and ≥1 trail event; native trailing
   exercised in the manager loop; all §5 telemetry green; daily-loss
   breaker verified against LIVE P&L; no fee-rejection deadlock (§4).
5. **Rollback ladder (least → most drastic; ordering is load-bearing):**
   `touch broker_orders/live/KILL` (instance placement stop; reconcile +
   protection continue) → global KILL → `ALLOW_ORDERS=0` + restart
   (disarm, keep protection) → **manual flatten — CANCEL the resting
   stop/OCO orders FIRST, then market-sell, then cancel entry buys**
   (the §7.2 recipe; selling with a live stop still resting risks the
   stop firing after the flatten and double-selling into an unintended
   SHORT) → `systemctl --user stop` LAST — stopping the daemon while
   positions are open removes exit management (the resting disaster stop
   remains, exits do not); never the first move.

## 8. Operator decisions (recommendation each)

1. **Sizing frame** — pin `ALPHALENS_BROKER_SIZING_EQUITY` ≈ 10 000 zł;
   effective = min(pin, snapshot). *(rec: as stated)*
2. **Per-pick risk** — ~100 zł (1%). *(rec: as stated)*
3. **MAX_OPEN** — 1 for the whole soak. *(rec: 1)*
4. **DAILY_LOSS_LIMIT_R** — *(rec: 1.0R)*
5. **Fee floor × frame (joint decision — §4 equation):** (a) 100 bps at
   the 10k zł frame (accept ~0.9% measured drag, soak validates
   execution); or (b) ~70 bps requiring notional ≥ ~$1000 (tighter stops
   or bigger budget). *(rec: (a) for the soak — 50 bps is unsatisfiable,
   it is the FX base alone)*
6. **Exit sequencing** — trailing_atr attended → native before first
   unattended night. *(rec: as stated)*
7. **Price-feed topology** — LIVE daemon sole elevated holder; SIM
   `SAXO_LIVE_PRICES=0` during soak. *(rec: yes; revisit shared reader
   when SIM needs LIVE prices back)*
8. **Telegram** — separate LIVE chat/topic vs `[live]` prefix in the
   shared chat. *(rec: prefix now — mandatory either way; topic optional)*
9. **Soak protocol** — ≥3 clean attended round-trips + drills before
   unattended. *(rec: as stated)*
10. **Standing-grant hygiene** — the grant does not self-expire; agree the
    decommission procedure (disable unit + remove pin). *(rec: note in
    runbook; no extra mechanism)*

## 9. Build plan sketch (post-approval, own PRs, TDD)

1. PR-A: constructor widening + `LiveTokenProviderAdapter` (incl. 401/
   invalidate semantics) + `create_saxo_broker_live_from_env` + boot-assert
   + new rail test `test_saxo_live_daemon_rail.py` (SIM rail test file
   untouched).
2. PR-B: composition-root branch (replaces ADR 0016 D7 raise) + fee floor
   + `min(pinned, snapshot)` sizing + `[live]`/`[sim]` alert labels.
3. PR-C: deploy artifacts — LIVE unit, versioned
   `alphalens-saxo-marketdata-refresh`, Prometheus rules, README rewrite
   (§5), SIM unit rail pins.
4. Front 4: the §7 soak.
