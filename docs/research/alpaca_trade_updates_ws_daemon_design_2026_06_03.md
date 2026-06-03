# Alpaca `trade_updates` WebSocket daemon — design memo (PR-5)

**Status:** DRAFT
**Date:** 2026-06-03
**Track:** Track A feedback / paper-trading PR-5 (live submitter) — durable fix for the naked-protection window
**Scope of code:** `apps/alphalens-pipeline/alphalens_pipeline/paper/`, `apps/alphalens-pipeline/alphalens_pipeline/data/alt_data/`; tests in `apps/alphalens-research/tests/`

> **One-line:** Filled paper ENTRY orders can sit with no protective stop for up to ~30 min, because the OCO exit ladder is only attached on the next reconcile poll after the fill. The durable fix is an always-on `trade_updates` WebSocket daemon that triggers the existing reconcile path sub-second. The "cheap bracket-order interim fix" was investigated and **rejected** (§8) — it does not apply to our plan shapes and would not even close the window for the one case it could touch.

---

## 1. Context / Problem (code-confirmed)

The paper harness submits multi-tier limit-GTC BUY entry ladders once per day and only **later** attaches the protective OCO exit ladder (take-profit limits + a shared disaster stop). The attach happens inside the reconcile pass, gated on observing the first entry fill:

- `reconciler.reconcile_orders` (`paper/reconciler.py:401`) walks open ledger orders, polls `broker.get_order`, appends fills, transitions status, then calls `process_plan_exit`.
- `exit_manager.process_plan_exit` attaches the OCO ladder **only after** a reconcile poll has recorded the first ENTRY fill — the attach branch is gated at `exit_manager.py:1037`:

  ```python
  if not should_fire_time_stop and snap.net_open_qty > 0 and not snap.has_exit_ladder:
      # cancel still-open entry tiers, size to live broker position,
      # then broker.attach_exit_ladder(...)  -> SELL-limit TP(s) + shared SL
  ```

The reconcile timer (`alphalens-paper-reconcile.timer`) fires **every 30 min** (Mon–Fri 14:00–21:30 UTC). So between an entry fill and the next reconcile poll, the filled position has **no protective stop on the broker** — a naked window of up to ~29 minutes.

The worst case is structural, not a tail: entry tiers are limit-GTC submitted ~5 min before the 13:30 ET opening cross (`alphalens-paper-submit.timer` at 13:25 UTC). A gap-down at the open can fill an entry tier and then keep falling while no SL exists, and the first reconcile that could attach the SL does not run until 14:00 UTC. A fill at 13:31 UTC is naked for ~29 minutes during the single most volatile part of the session.

This is a real, currently-live exposure on the **paper/test** account. It is not a data-correctness bug (the ledger eventually reconciles correctly via `UNIQUE(alpaca_fill_id)` idempotency) — it is a **risk-management latency** bug: protection is attached on a 30-min poll cadence instead of at fill time.

### 1.1 Plan shapes (why the broker bracket cannot apply)

`brief_trade_setup` → `SetupPlan` (`paper/sizing.py`) carries:

- `entry_tiers` — **0..3** monotone-descending limit tiers (`thematic/trade_setup/ladder.py:24`, `max_tiers=3`), one Alpaca order each.
- `tp_tranches` — **0..3** ascending take-profit targets (`ladder.py:65`, `max_tranches=3`).
- `disaster_stop` — one shared stop for the whole position.

Both ladders are data-driven and routinely multi-element. Live ledger (`~/.alphalens/paper_ledger.db`, 37 plans at time of writing): entry-tier counts `{2: 5, 3: 32}` (**never 1**), TP-tranche counts `{1: 3, 2: 5, 3: 29}`. This shape is exactly why the broker-side single-parent bracket cannot be the fix (§8), and why the WS daemon — which triggers the existing whole-position OCO attach regardless of tier count — is the general answer.

---

## 2. The fix, and what was rejected

| | **Durable: WS daemon** (this memo, §3–§7) | Rejected: single-TP bracket (§8) | Cheap interim stop-gap (§8.2) |
|---|---|---|---|
| Mechanism | always-on `trade_updates` WS triggers the existing reconcile path on the fill event | Alpaca BRACKET attaches TP+SL atomically when the parent entry fills | denser reconcile-poll burst around the opening cross (systemd-timer only) |
| Naked window | seconds (network → handler → reconcile) | claimed zero — but see §8: actually does not close it | shrinks worst case to the burst interval; does NOT fully close it |
| Applicability | all plans (any tier/tranche count) | **none** — 0/37 live plans qualify, and mechanically wrong even if one did | all plans |
| New infra | one systemd-user daemon + one Prometheus gauge + one alert | none | none (timer cadence change) |
| Verdict | **build, for PR-5** | **do not build** | optional, if a stop-gap is wanted before PR-5 |

The daemon is the complete answer and lands with the PR-5 live submitter. Neither the daemon nor a stop-gap is urgent on the paper/test account; the daemon is **mandatory before any real-capital submitter**.

---

## 3. Scope — trading-stream ONLY, not market-data

The daemon subscribes to the Alpaca **trading** stream (`trade_updates`) via `alpaca.trading.stream.TradingStream(api_key, secret_key, paper=True)` → `subscribe_trade_updates(handler)` → `run()`. It does **NOT** open a market-data stream (quotes/bars/trades).

Reasons:

1. **One purpose: react to fills.** We need order lifecycle events (`new`, `partial_fill`, `fill`, `canceled`, `expired`, `rejected`) to trigger the exit attach. We do not need price ticks — TP/SL prices come from the plan, not from live quotes.
2. **One connection.** Alpaca enforces a single concurrent market-data-stream connection per account (`406 connection limit exceeded` on a second). The trading stream is a separate endpoint, so subscribing to it does not touch the market-data connection budget — but there is also no reason to open the market-data stream at all here.
3. **No price-derived decisions.** All exit prices are deterministic from `brief_trade_setup` (per the project doctrine: no LLM/real-time numbers in decision logic). A market-data stream would add a data source that the exit logic must not consult.

`trade_updates` events arrive as **binary frames** on the paper endpoint (msgpack); the alpaca-py `TradingStream` decodes them — the daemon never parses frames by hand. The subscribe action is the SDK's `subscribe_trade_updates`; no `listen`-style raw protocol handling is written by us.

---

## 4. Design

### 4.1 Deployment shape

An always-on systemd-**user** unit, modeled on `alphalens-form4-backfill.service`:

- `Type=simple` + `Restart=on-failure` + `RestartSec` backoff + `StartLimitIntervalSec`/`StartLimitBurst` loop cap.
- `WorkingDirectory=%h/AlphaLens`, `EnvironmentFile=/etc/alphalens/env` for the Alpaca keys.
- `After=network-online.target` / `Wants=network-online.target`.
- `WantedBy=default.target`, persistence via `loginctl enable-linger`.
- `StandardOutput=journal` / `StandardError=journal`.

Proposed unit: `alphalens-paper-trade-stream.service`. It runs a long-lived process (`alphalens paper trade-stream --use-test-account`) hosting the `TradingStream`.

The daemon is a **NEW** module `paper/trade_stream.py` plus a CLI command in `alphalens_cli/commands/paper.py`. The AlpacaClient gains the stream factory in `data/alt_data/alpaca_client.py` (one canonical Alpaca surface — no shadow client per the one-client-per-vendor rule).

### 4.2 Single-writer discipline (the core invariant)

**The WS daemon NEVER mutates the ledger directly.** On a relevant `trade_updates` event it calls the existing reconcile entry point — `reconciler.reconcile_orders(ledger_path=…, broker=…, account=…)` — and that function remains the sole ledger writer. The daemon is a *trigger*, not a second reconciler.

Why this matters:

- `reconcile_orders` opens its own ledger connection, polls the broker for current order state, appends fills under `UNIQUE(alpaca_fill_id)`, transitions status, and runs `process_plan_exit` with the attach-once `has_exit_ladder` gate. All of that idempotency and exit-attach logic already exists and is tested.
- If the daemon also wrote fills, we would have two writers racing on the same ledger with no lock — exactly the clobber the project bans (and the same single-writer lesson the Saxo token-manager just shipped). Instead the WS event just says "something happened, reconcile now" and the proven path does the work.
- The 30-min poll timer calls the **same** `reconcile_orders`. So WS-triggered and timer-triggered reconciles are interchangeable; the only difference is latency. A fill observed by both the WS event and the next poll resolves to one fill row (UNIQUE constraint) and one exit attach (`has_exit_ladder` flips True on the first attach, the second pass is a no-op).

### 4.3 Event handling

The async handler:

1. Receives a `trade_updates` event.
2. Filters to events that can change protection state: `fill`, `partial_fill` (entry filled → may need attach), and terminal entry states that free capacity. Status-only / `new` events are ignored (no reconcile needed).
3. Resolves the affected `account`/`profile` (the daemon runs one account; `--use-test-account` → `test`).
4. Calls `reconcile_orders(ledger_path, broker, account)` for that account.

Concurrency: reconcile calls are **serialized** inside the daemon (a single in-process lock / single-flight). If three fills arrive in one second, they coalesce into at most one in-flight reconcile plus one queued — never N concurrent `reconcile_orders` on the same ledger. The reconcile is cheap and idempotent, so coalescing loses nothing.

The handler wraps `reconcile_orders` in a try/except that logs and continues — a single bad reconcile must not kill the stream.

### 4.4 Auth + endpoint

Static API key + secret from `/etc/alphalens/env` (same keys the REST `AlpacaClient` already uses, `test`/`main` profile). `paper=True` selects the paper trading-stream endpoint. No new credential, no service token.

---

## 5. Backstop — the 30-min poll stays

The WS daemon does **not** replace the reconcile timer. `alphalens-paper-reconcile.timer` keeps firing every 30 min and remains responsible for everything that is wall-clock-driven rather than event-driven:

- **Entry-TTL sweep** — cancels limit entries past `order_ttl_days`.
- **42-day time-stop** — liquidates aged positions; there is no broker event for "N days elapsed".
- **`gross_guard`** — portfolio-level exposure check.
- **#404 phantom-guard / ledger↔broker desync** (`exit_manager.py:1006`) — terminal `RECONCILED_FLAT` when the broker is flat with no exit orders.
- **Gap recovery** — any fill the WS daemon missed (daemon dead, dropped frame, reconnect gap) is still caught within ≤30 min by the next poll.

So the system degrades gracefully: if the daemon is down, behavior reverts exactly to today's 30-min poll cadence (the known, currently-live state) — never worse.

**Full REST resync on (re)connect:** every time the stream connects or reconnects, the daemon runs one `reconcile_orders` immediately, before processing new events. This closes the window where fills happened while the socket was down. Reconnect is also where any stale connection is replaced — the SDK's reconnect path plus our on-connect resync means a dropped-and-restored socket cannot leave a fill unattended longer than the poll cadence.

---

## 6. Failure register

| Failure | Effect | Detection | Mitigation |
|---|---|---|---|
| Daemon process dead | no event-driven attach → reverts to 30-min poll latency (today's state); silent if nobody watches | staleness alert (§7) + systemd `Restart=on-failure` | poll backstop bounds exposure to ≤30 min; alert fires; systemd restarts |
| Stale connection (socket open, no events delivered) | fills not triggered in real time | last-event-age gauge climbs → alert | on-connect resync + poll backstop; reconnect replaces socket |
| Dropped events (frame lost, brief disconnect) | a fill missed by the stream | next poll OR on-reconnect resync | `reconcile_orders` re-polls broker state — it does not depend on having seen every event |
| Duplicate events (same fill twice, reconnect replay) | none | n/a | `UNIQUE(alpaca_fill_id)` (broker `execution_id` → `fills.alpaca_fill_id`) makes the second append a no-op; `has_exit_ladder` makes the second attach a no-op |
| Reconcile raises inside handler | one event un-acted | handler try/except logs | next event or next poll reconciles; stream stays up |
| Two writers race | — | — | by construction impossible: daemon never writes the ledger, only triggers `reconcile_orders` (single writer) |

**Idempotency anchors (existing, reused):** `fills.alpaca_fill_id` UNIQUE (`reconciler.py`) and `process_plan_exit`'s `has_exit_ladder` attach-once gate (`exit_manager.py:1037`). The daemon adds no new idempotency primitive — it relies on these.

---

## 7. Monitoring

Mirror the established staleness pattern (#358 nightly shadow-returns 48h, #376 VIX-cache 72h):

- **Gauge:** `alphalens_paper_trade_stream_last_event_at_timestamp_seconds` — wall-clock of the last received `trade_updates` event (or last successful connect heartbeat, so a quiet-but-healthy stream does not look stale). Emitted via the existing domain-metric textfile path.
- **Connection gauge (optional):** `alphalens_paper_trade_stream_connected` (1/0) set on connect/disconnect.
- **Alert:** `AlphalensPaperTradeStreamStale` — fires when last-event/heartbeat age exceeds the threshold. Use a heartbeat-based age (not raw last-event), because a legitimately idle weekday before the open would otherwise false-fire. Distinct alertname, no `job=` label, so it stays out of the cron-job staleness enums (same rule as #376).
- **Connection-down alert:** `AlphalensPaperTradeStreamDown` when `connected==0` longer than a reconnect-grace window.

Per the #358/#376 precedent, a non-cron alert needs its **own** regression-pin tests (cron-keyed glob tests key on `AlphalensJobStale`/`MetricMissing` only and will not cover this).

---

## 8. Bracket-order fix — considered and REJECTED

**The idea:** route the entry through `AlpacaClient.submit_bracket_order` (`alpaca_client.py:398`) so Alpaca attaches TP+SL atomically the instant the parent entry fills — zero poll latency, zero new infra. On its face this looked like a cheap interim win. It does not survive contact with the real code or with Alpaca's bracket mechanics. **Three independent kill reasons, any one sufficient:**

### 8.1 Empirically dead — 0/37 live plans qualify

An Alpaca BRACKET wraps **exactly one** parent order and supports **exactly one** TP leg (`alpaca_client.py:346-347,426`; both TP and SL legs mandatory, `:436` raises `ValueError` otherwise). So a plan could only be bracketed if `len(entry_tiers) == 1 AND len(tp_tranches) == 1`. Against the live ledger (`~/.alphalens/paper_ledger.db`, 37 plans): entry-tier counts `{2: 5, 3: 32}` — **never a single tier** — and 0/37 plans satisfy both conditions. The qualifying set is empty in production and near-empty by construction (the builder always appends two volatility fallbacks; the TP side defaults to a 3-element R-multiple fallback when there is no overhead resistance). Routing through `submit_bracket_order` would be dead code.

### 8.2 Mechanically wrong even for a hypothetical qualifying plan

Alpaca bracket child legs (TP/SL) stay **held/inactive until the parent is COMPLETELY filled**, and they inherit the parent's full original quantity (no partial-fill resizing). Our entries are limit-GTC, and **partial fills at the 13:30 ET opening cross are exactly the scenario the fix targets**. A partially-filled bracket parent therefore leaves the position **fully naked** with held (inactive) children — it does not even close the naked-SL window it exists to fix. (An earlier draft of this analysis had this mechanic backwards, stating a bracket would "protect the filled qty"; that is false — children fire only on complete fill. The adversarial review caught and corrected it.)

### 8.3 Architecturally wrong — breaks the broker-agnostic boundary and the blended-entry invariant

- The `BrokerClient` protocol (`paper/broker.py:134`) **deliberately excludes** `submit_bracket_order`: "`attach_exit_ladder` is the broker-neutral OCO-ladder exit intent … no OCO/bracket mechanics leak here." Wiring the Alpaca bracket would leak vendor-specific bracket mechanics into the harness — directly against the exchange-agnostic design that the Saxo multi-market endgame depends on.
- A **per-tier bracket** (one bracket per entry tier) cannot express the plan's targets: every TP `r_multiple` and the `disaster_stop` are computed relative to the **qty-weighted blended entry across all tiers** (`ladder.py:78` `R = blended_entry - stop`; `model.py:41`), which is only known after fills. A bracket attached at submit time off one tier's price cannot represent that.
- It also collides with `#401` cancel-unfilled-on-first-fill: cancelling an unfilled tier-parent would cancel its broker-owned children, silently discarding the multi-tranche TP ladder, and with `#404`'s single-whole-position-ladder assumption (`has_exit_ladder` / `net_open_qty` reason over one ladder the harness submitted, not N broker-originated child groups it never inserted).

### 8.4 Cheap stop-gap that *does* work (optional, no code)

If a pre-PR-5 mitigation is wanted on paper, the honest one is a **denser reconcile-poll burst around the opening cross** (e.g. every 2–5 min from 14:00–14:30 UTC, on top of the existing 30-min cadence). It is a systemd-timer cadence change only — no code path, no new infra — and it shrinks the worst-case naked window from ~30 min to the burst interval. It does **not** fully close the window (the residual is the burst interval), and it adds more REST polls (well within limits). The WS daemon remains the real fix.

### 8.5 Decision pin

`apps/alphalens-research/tests/paper/test_bracket_not_applicable.py` records this decision as executable guards (the blended-entry-relative TP invariant and the broker-agnostic-protocol boundary). **Re-open conditions:** the bracket question only re-surfaces if BOTH (a) plan geometry starts producing single-tier+single-TP plans AND (b) the entry mechanism moves away from partial-fillable limit-GTC orders (so the §8.2 held-until-complete-fill mechanic no longer leaves a partial fill naked). A geometry change alone does not re-open it.

---

## 9. Test plan (TDD red → green)

Tests live in `apps/alphalens-research/tests/` (workspace split). External Alpaca calls mocked; no live socket in unit tests.

**Decision guard (this PR — bracket rejection, §8):**
1. `test_tp_tranches_are_blended_entry_relative` — pins that `build_tp_tranches` computes `r_multiple = (target − blended_entry)/(blended_entry − stop)`, i.e. relative to the blended entry across all tiers, documenting in-code why a per-tier bracket cannot express the plan's targets.
2. `test_broker_protocol_excludes_bracket` — pins that the `BrokerClient` protocol does not expose `submit_bracket_order` (broker-agnostic boundary, `broker.py:134`), so a future change that wires the Alpaca bracket into the harness trips this guard.

**WS daemon (PR-5):**
3. A `fill` event handler calls `reconcile_orders` exactly once with the daemon's account/broker.
4. A `new`/status-only event does NOT trigger reconcile.
5. **WS-then-poll double-call interleave:** a fill delivered via WS (handler runs `reconcile_orders` → fill appended, exit ladder attached), then the 30-min poll runs `reconcile_orders` again on the same ledger → the second pass appends NO new fill (`UNIQUE(alpaca_fill_id)`) and attaches NO second ladder (`has_exit_ladder` True). Assert: one fill row, one exit group, identical outcome regardless of order (poll-then-WS symmetric).
6. Concurrent fills coalesce: multiple events in flight produce at most one in-flight reconcile (single-flight lock), never two concurrent `reconcile_orders` on one ledger.
7. On-connect resync: connecting/reconnecting runs one `reconcile_orders` before the first event.
8. Handler exception is caught and logged; the stream loop is not torn down.
9. Last-event/heartbeat gauge is updated on event receipt and on connect.

**Negative control (PR-5):** the daemon module contains no direct ledger-write call (assert it only calls `reconcile_orders`) — protects the single-writer invariant from regression.

---

## 10. Honest verdict

- **Not urgent today.** The exposure lives on the **paper/test** account. The naked window is a paper-PnL artifact, not real capital, and the 30-min poll already bounds it.
- **The daemon is a PR-5 prerequisite, not a standalone task.** It only earns its keep once a **live** submitter exists. Building it now, ahead of live submission, would add an always-on process and an alert for a sandbox-only risk. Build it as part of PR-5 (live submitter); if a pre-PR-5 stop-gap is wanted, use the dense-reconcile burst (§8.4).
- **The bracket fix is rejected, not deferred** — see §8. It is empirically dead (0/37), mechanically wrong under partial fills, and breaks the broker-agnostic boundary. The decision is pinned by the §9 guard tests.
- **Paper vs live difference to watch:** the paper trading stream is a faithful simulation, but fill timing and partial-fill granularity on the paper feed can differ from live. The daemon's correctness rests on `reconcile_orders` re-polling broker truth (not on trusting event payloads), so a paper/live event-shape difference does not change the ledger outcome — only the trigger latency.

---

## 11. Relation to the Saxo multi-market endgame

The harness is exchange-agnostic by rule (every calendar/submitter/TTL/sizing helper takes an `exchange` MIC, default `XNYS`). The WS daemon is **Alpaca-specific** (US equities, paper). When a second venue lands (Saxo for XWAR/XTKS/XHKG/XSHG), the durable fix is **per-broker**: each broker client exposes its own trade-stream factory, and a per-venue daemon (or a multiplexing supervisor) triggers the same broker-neutral `reconcile_orders`. The single-writer invariant generalizes cleanly — `reconcile_orders` already takes a `broker` and an `account`; a Saxo broker client implementing the same `BrokerClient` surface plugs in without touching the reconcile/exit logic.

This is also why the bracket fix is the wrong direction (§8.3): it leans on Alpaca's `BRACKET` order class, which the `BrokerClient` protocol deliberately keeps out (`broker.py:134`). Saxo's atomic-protection primitive (native OCO / if-done) is different, so a bracket path would have to be rebuilt per venue — whereas the trigger-the-reconcile daemon is the reusable shape across brokers. The Saxo work (single-writer token manager, just merged) already set the precedent: a single-writer trigger plus a fail-loud backstop is the pattern to repeat here.
