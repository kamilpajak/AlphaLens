# Saxo SIM order/position stream — breaker re-arm + episode-latched alerting

**Status:** LOCKED — 2026-08-22. Supersedes nothing; extends the streaming reader shipped in PR #900 (`docs/research/saxo_streaming_design_2026_07_24.md`). Scope is the **SIM rail only** — the LIVE daemon has no streaming reader at all (two independent guards, §4.1). **One merge blocker stands: the `SAXO_STREAM_LIVE_TEST=1` contextId probe (§8.1) must pass before the re-arm ships.**

Every behavioural claim below carries a `file:line`. Claims marked **(probed)** were executed against the real classes on 2026-08-22, not reasoned about — per the repo's "run it before believing it" rule. Claims I could not execute are marked **unverified** and are listed in §8.

---

## 1. Incident

Journal evidence from the live VPS (`vmi2478967`), 2026-08-22, Europe/Warsaw:

```
08:42:56  saxo 504 server error (attempt 1/4); sleeping 5s
08:43:03  saxo 504 server error (attempt 2/4); sleeping 15s
08:43:21  saxo 500 server error (attempt 3/4); sleeping 30s
08:43:26  saxo stream session failed: timed out during opening handshake
08:43:39  saxo stream session failed: timed out during opening handshake
08:43:44  saxo stream session failed: server rejected WebSocket connection: HTTP 504
08:43:51  alert: reconcile failed (broker error) - verdicts skipped this tick: Saxo 500:
08:43:58  alert: saxo stream silent >45s (80s) - running on poll backstop
08:46:21  saxo stream circuit breaker tripped after 6 consecutive failures - running poll-only
08:46:27  alert: saxo stream circuit breaker tripped - running on poll backstop
09:17:10  alert: saxo stream circuit breaker tripped - running on poll backstop
  ... identical alert every ~30.5 min, 29 times, still firing 14h later at 23:02
```

Two distinct things happened, and only one is a defect.

**(1) A ~6-minute Saxo vendor outage** hit both rails. REST returned 504/500, the WebSocket handshake timed out and then was rejected with HTTP 504. The REST-side alerts (reconcile failed, live-exits position read failed, trailing peak fetch failed, protection-view build failed, all rendering `Saxo 500:`) are **correct behaviour**: skip the tick, alert, carry on. The LIVE rail self-healed and has been silent since 08:48. **No change is proposed to REST retry.**

**(2) On the SIM rail the WebSocket reconnect breaker counted 6 consecutive failures and tripped — permanently.** Saxo recovered within minutes. The SIM stream stayed dead for 14+ hours and would have stayed dead until a manual `systemctl --user restart`. That is the defect under design.

The observed 30.5-minute cadence maps exactly onto the mechanism: `_ALERT_REPEAT_INTERVAL_S = 1800.0` (control_loop.py:6164) plus up to one 45 s poll-grid step.

**What protection actually lost.** Nothing. `_make_stream_tick` carries no protection — it pushes the bearer, emits a gauge and alerts. The poll backstop in `run_once` is untouched by a tripped breaker. The stream is a pure wake-latency optimisation: sub-second early wake degrades to ≤ `poll_seconds` (45 s). **This is a degradation and an observability failure, never an outage** — which is precisely why the fix must be small and must not destabilise the protective loop.

---

## 2. Root cause — two independent defects

### 2.1 The breaker is terminal for the life of the process

```
streaming.py:412  _trip_breaker() sets self._is_streaming = False
streaming.py:497  async def _supervise(): while not self._stop and self._is_streaming:
streaming.py:506  if self._stop or not self._is_streaming: return
```

The reader coroutine returns, `_thread_main` returns (streaming.py:464-470, whose only wrapper is a bare `except Exception` that logs), the daemon thread ends. Every assignment to `_is_streaming` in the repo:

- `streaming.py:230` — `= True`, constructor, once.
- `streaming.py:412` — `= False`, trip.

There is **no re-arm path anywhere**. Everything else that reads the flag is a read-only property delegation (`streaming.py:240-242`, `streaming_trigger.py:143-147`, `control_loop.py:3035`).

Three secondary facts that constrain any fix, all verified:

- `start()` early-returns `True` when `self._thread is not None` (streaming.py:262-263) and `_thread` is never set back to `None`. **Calling `start()` again after a trip is a silent no-op that reports success.**
- `_trip_breaker` best-effort DELETEs the context's subscriptions inside `contextlib.suppress(Exception)` (streaming.py:413-414) — during the failure that trips the breaker, that DELETE is the call most likely to fail silently.
- The systemd unit is `Restart=on-failure` (`deploy/systemd/alphalens-broker-manager.service:56`) and the trip does not exit the process, so systemd never restarts anything.

### 2.2 A permanent, unchanging state routed through a fixed-interval re-alert throttle

```
control_loop.py:3034-3046  _make_stream_tick(): if not trigger.is_streaming ->
                           alert_throttled(..., "stream-breaker") and return
control_loop.py:6164       _ALERT_REPEAT_INTERVAL_S = 1800.0
control_loop.py:6183-6196  _AlertThrottle.emit dedups by (uic, reason)
```

A level with a duration, paged through a repeat interval, is a metronome at *any* interval — 30 min gives 29 messages; 6 h would still give 2 per day forever. The condition is a LEVEL and belongs on a level instrument.

There is also a second-order defect hiding behind the early return at `control_loop.py:3046`: it returns **before** the bearer push (`:3047-3053`) and **before** `emit_gauge` (`:3055`). So while tripped, (a) the retained `_current_token` freezes at the trip instant while `alphalens-saxo-refresh` rotates the real token every ~20 min, and (b) the liveness gauge stops being written and node_exporter re-serves its last healthy-looking value forever — any `age > N` rule is structurally incapable of firing on the failure it was written for (`emit_domain_metrics` overwrites the whole domain file atomically and never unlinks it, `observability/textfile.py:83-130`).

### 2.3 The observability gap is the same gap

No rule anywhere in `deploy/monitoring/` references `alphalens_broker_manager_stream_*` (grep returns nothing). `control_loop.py:152-154` asserts the age gauge is "Watched by an `AlphalensBrokerStreamStale` rule"; that rule does not exist, and `deploy/systemd/README.md:1129` still lists it as a to-do. **The 29 Telegram messages were the only signal the incident produced.** The noise problem and the blindness are one problem.

---

## 3. The six design questions and the decisions taken

### Q1 — Where does the re-arm live? **The main thread, in `_make_stream_tick`.**

The daemon's tick already runs every ~45 s and already reads `trigger.is_streaming` (control_loop.py:3035). This mirrors the neighbouring implementation on the axis that matters: `_default_live_exits_feed_factory` polls `is_running()` on the daemon's own tick and rebuilds the price stream when it is False (control_loop.py:876-883 → saxo_price_stream.py:989-1004), with the rationale written down at saxo_price_stream.py:990 — avoid "a dead stream sitting there silently serving no fresh quotes for the rest of the process".

Three alternatives rejected:

- **Rebuild the whole `StreamTrigger`** (the verbatim price-stream mirror). `_build_stream_handles` returns `trigger.wake_event` to `run_daemon` (control_loop.py:3369), which waits on that specific `Event` object. A rebuilt trigger owns a NEW Event, so every early wake after the first rebuild is silently lost — the stream would look alive and deliver zero latency benefit. The price stream can be rebuilt precisely because nothing holds a handle to its internals.
- **Cooldown-and-continue inside `_supervise`.** That coroutine plus `_run_one_connection` and `_recv_and_route` all carry `# pragma: no cover - exercised by live probe` (streaming.py:492, 519, 540). A re-arm there ships untested, cannot see the session window, and gives no point at which the bearer can be refreshed before reconnecting.
- **A new watchdog thread.** Adds a thread to a protective daemon for zero gain; the hook already exists.

### Q2 — What shape of breaker? **Half-open, delivery-confirmed, with the ladder owned by the tick.**

The half-open property costs **zero new state on the client**: `rearm()` deliberately does NOT reset `_consecutive_failures`. At the first trip the streak sits at 6 and `_register_failure` trips on `>= max_consecutive_failures` (streaming.py:371-376), so the re-armed reader gets a budget of exactly ONE connect. **(probed)**:

```
after trip: is_streaming False cf 6
next step after re-arm w/o streak reset: ReconnectStep(give_up=True, backoff_s=0.0)
after delivered frame, step:             ReconnectStep(give_up=False, backoff_s=1.0)
```

Cooldown, one trial, deliver-a-frame closes it, fail re-opens it with a longer cooldown — the classic half-open breaker, implemented by adding nothing and preserving finding #1's delivery-only reset rule (streaming.py:383-409) verbatim.

The COOLDOWN LADDER, however, lives entirely in the tick closure, **not** on the client. Deriving it from `_consecutive_failures` (as the winning proposal originally did) has two fatal problems: the reader thread zeroes that counter on any delivered frame, so a main-thread dwell can never stop the ladder resetting; and it puts two owners on one decision. The client exposes reads only.

### Q3 — What bounds a multi-hour outage, and what damps flapping?

**Multi-hour outage:** the ladder is 60 → 120 → 240 → 480 → 900 → 900 s, saturating after 5 failed trials (~30 min). Steady state is **one trial connect per 15 minutes** — about 56 handshakes across the observed 14 h, versus the ~6600 a cooldown-free price-stream mirror would produce. The price stream tolerates a cooldown-free rebuild because it never pages; this rail does.

**Flapping** is damped at three layers:

1. A trial that connects but never delivers gets exactly one connect (Q2) and re-opens on the next rung.
2. The ladder resets to the floor **only** after a delivery-confirmed dwell of 300 s, held in the tick closure — so a deliver-once-then-die flapper climbs 60 → 120 → 240 instead of looping at the floor.
3. A connection-life gate on the streak reset (§4.2) makes the one-heartbeat-then-drop endpoint trip the breaker instead of spinning under it.

### Q4 — Alert policy. **Telegram gets EDGES, once per EPISODE. Prometheus owns every level.**

Firm, not a menu. Full policy in §4.5. The per-episode Telegram budget is: 1 OPEN page, 0 pages while open, ≤1 flap CRITICAL, 1 CLOSE page. Yesterday's incident under this policy: **2 messages instead of 29**, and the operator would have learned it recovered.

### Q5 — Session/trading-window awareness. **Keep the socket up 24/7. Keep the trip page unconditional. Emit `in_session` as a gauge only.**

The two judges disagreed here — judge 1 said never gate the page, judge 2 said gate the Telegram page on the window.

**Decision: the trip page stays unconditional, and the weekend quiet comes from the episode latch rather than from a calendar** — because the incident's harm was 29 repeats of one unchanging state, which the latch removes entirely, whereas a calendar gate would have delivered **zero** signal for the 14 hours actually under design.

The socket is not slept either, and that is a separate, evidence-based refusal. PR #1067's session gate belongs to the PRICE stream because "outside market hours no quote frames flow" is a measured property of quote feeds (saxo_price_stream.py:707-720). For an ORDER/POSITION stream I cannot make that claim: positions and resting 7-day ENTRY orders exist over the weekend, frames are event-driven not tick-driven, and whether Saxo pushes corporate-action or housekeeping frames off-hours is **unverified** (§8.5). Holding the socket costs nothing the poll backstop does not already cover; silencing it on an unverified assumption is exactly the story-not-evidence failure the repo forbids.

What the window DOES get: a `stream_in_session` 0/1 gauge, built from the existing `_make_stream_session_window` (control_loop.py:735-793, already on the exchange-parametrized `paper.calendar` helpers, already unit-tested against an injected clock). It is emitted and **not** referenced by any shipped rule, so making a rule session-aware later is a one-line YAML change rather than a code change.

### Q6 — Observability in 30 seconds. **Six gauges in one atomic write, three Prometheus rules, one CLI probe.**

Detail in §4.6. The headline correction: the *existing* age gauge alone can never carry this signal, because the tick returns before emitting it while tripped. A separate, actively-written level gauge is required.

---

## 4. The design

### 4.1 Scope — SIM rail only, structurally

`_build_stream_handles` returns `(None, None, None)` for `ENV_LIVE` **before** the env-flag check (control_loop.py:3313-3321), so the LIVE daemon has no `StreamTrigger`, no reader thread and no `stream_tick` hook — its `run_daemon` runs the byte-identical poll-only path. The LIVE unit additionally pins `Environment=ALPHALENS_BROKER_STREAMING_ENABLED=0` in-unit (`deploy/systemd/alphalens-broker-manager-live.service:134`), asserted by `test_deploy_systemd_units.py:1497-1501`. Two independent guards. **Nothing in this design can reach real-money protection.**

### 4.2 Client read surface + the delivery-life gate (`brokers/saxo/streaming.py`)

New read-only properties, all main-thread-safe (plain attribute reads):

| Member | Meaning |
|---|---|
| `is_running() -> bool` | `self._thread is not None and self._thread.is_alive()`. Verbatim mirror of `SaxoPriceStream.is_running` (saxo_price_stream.py:535-542), including its docstring rationale. The order-stream client conspicuously lacks it today; `is_started` stays True forever. |
| `frames_delivered -> int` | Monotonic, incremented ONLY in `_mark_delivered`. **The real delivery proof.** |
| `trips_total -> int` | Monotonic, incremented in `_trip_breaker`. Lets the tick count a trip whose whole lifetime falls between two ticks. |
| `consecutive_failures -> int` | For the gauge. Makes the streak composition recoverable, which yesterday's journal could not do. |

**The delivery-life gate.** `_mark_delivered` (streaming.py:316-323) currently clears the failure streak on any single frame. That is exploitable by a gateway that accepts the socket, sends one `_heartbeat`, then drops: the streak never reaches 6, the breaker never trips, `is_streaming` stays True, and the reader spins a full reconnect + resubscribe + `on_trigger()` → `wake_event.set()` → full `run_once` roughly every 1.5-2 s (bounded only by `SaxoClient._MIN_REQUEST_INTERVAL_S = 0.5`, client.py:147), producing **zero** Telegram messages. This is pre-existing, but in scope: it is the same "the instrument reports healthy while the stream is useless" family, and it would silently defeat the flap escalation.

Fix, minimal and doctrine-preserving:

```python
def _mark_delivered(self) -> None:
    """A real server frame arrived. Always counts as delivery evidence
    (frames_delivered). Clears the failure streak only once the CURRENT
    connection has been alive for _min_connection_life_s: a connection that
    delivers one frame and dies inside that window has not demonstrated it can
    carry the stream, and clearing on it lets a one-heartbeat-then-drop gateway
    spin under the breaker forever. Delivery stays the ONLY reset trigger
    (finding #1) — this narrows WHICH delivery counts, never widens it."""
    self._frames_delivered += 1
    if self._monotonic() - self._connection_started_mono >= self._min_connection_life_s:
        self._reset_failures()
```

`_connection_started_mono` is stamped in `_run_one_connection` beside the existing `_last_recv_mono = self._monotonic()` (streaming.py:442). The DECISION is synchronous and hermetically testable even though the stamping site carries a coverage pragma.

### 4.3 `rearm()` — spawn-guarded, context-rotating, cold

```python
def rearm(self, context_id: str) -> bool:
    """MAIN THREAD ONLY. Re-open a tripped-or-crashed reader on a FRESH context.
    Returns True iff a new reader thread is running.

    Thread-safety: every field written here is written only after is_running()
    has confirmed the previous reader thread is dead, and the new thread is
    spawned after those writes — so no field ever has two live writers. Attribute
    assignment is atomic under the GIL, so no lock is taken and the protective
    loop can never block here.

    SINGLE-CALLER CONTRACT: start() sets self._stop = False, so a rearm() racing
    stop() could resurrect a reader mid-shutdown. Both are main-thread-only
    (run_daemon's tick and the CLI finally at broker.py:1560-1564 are the same
    thread). Do not call this from anywhere else."""
    if self._stop:
        return False                     # shutdown latch — never resurrect
    if self.is_running():
        return False                     # old reader still unwinding asyncio.run
    if self._current_token is None:
        return False                     # never trial without a bearer (§4.4)
    prior_thread, prior_streaming = self._thread, self._is_streaming
    self._retired_context_ids.append(self._context_id)
    self._context_id = context_id        # rotate (see below)
    self._last_message_id = None         # explicit COLD connect
    self._breaker_alerted = False        # so a second trip logs again
    self._thread = None                  # else start() silently no-ops
    self._is_streaming = True
    try:
        started = self.start()
    except Exception:
        self._thread, self._is_streaming = prior_thread, prior_streaming
        raise
    if not started:                      # StaticTokenProvider refusal
        self._thread, self._is_streaming = prior_thread, prior_streaming
        return False
    return True
```

**Context rotation is mandatory, not optional.** `_trip_breaker` DELETEd the context's subscriptions best-effort (streaming.py:413-414) and that DELETE is exactly the call most likely to have failed during the outage. The neighbouring price stream's reliability contract mandates a FRESH contextId per connection after the 2026-08-10 incident ("idle WS killed by Saxo, reconnects died into a subscription-less context, breaker tripped", saxo_price_stream.py:32-36). Reusing the context here would be repeating that incident. `_context_id` becomes mutable; ids are minted by an injected `context_id_factory` moved out of `control_loop.py:3342`, keeping the `<=50` char `[a-zA-Z0-9-]` constraint.

**Retired contexts are drained, not orphaned.** Each rotation pushes the old id onto a bounded `deque(maxlen=_STREAM_RETIRED_CONTEXT_CAP)`. On each *successful* `_subscribe` — where REST is demonstrably healthy — the reader thread `popleft()`s each retired id, best-effort DELETEs it, and re-appends on failure. `stop()` drains the same deque. `deque.append`/`popleft` are atomic, and the main thread only ever appends while the reader is provably dead.

**State deliberately NOT reset, each for a reason:**

| Field | Why it stays |
|---|---|
| `_consecutive_failures` | Keeping the streak IS the half-open mechanism (Q2, probed). |
| `_current_token` | Clearing it would turn the bounded startup `token_missing` exemption (streaming.py:395-405, the 2026-07-27 incident fix) into an unbounded free-spin. |
| `_subscription_generation` | Monotonic (streaming.py:344-347) — it is what guarantees fresh `pos-N`/`ord-N` ReferenceIds. Self-heals for free. |
| `_last_authorized_token` | Re-stamped at connect (streaming.py:441). |
| `_last_recv_mono` | Re-stamped at connect (streaming.py:442). |

### 4.4 The tick — order of operations, and what "up" means

`_make_stream_tick` is rewritten. Every tick, unconditionally, in this order:

1. **Push the bearer first.** Today the breaker branch returns at `control_loop.py:3046` *before* `push_token` at `:3051`, so `_current_token` freezes at the trip instant while the token rotates every ~20 min. A re-armed reader would connect with a dead bearer and burn its single trial on a 401. The guarded `get_bearer()` / `push_token` block moves above every branch, and `rearm()` refuses when no token has ever been pushed. This also bounds the shared-provider hazard in §7.4.
2. **Sample health.** The operator-visible state is **delivery-backed**, never `_is_streaming`:

   ```
   reader_dark = (not trigger.is_running()) or (not trigger.is_streaming)
   up = trigger.is_running() and trigger.is_streaming
        and trigger.frames_delivered > delivered_at_rearm
        and (silence := trigger.seconds_since_last_message()) is not None
        and silence <= stale_s
   ```

   Deriving `up` from `is_streaming` was the winning proposal's worst defect: `rearm()` sets that flag True *before* any evidence, so a trial that dies without delivering would page "re-armed — streaming again". Worse, a trial that fails inside a single 45 s tick gap would be invisible to the edge latch entirely, so the flap counter would count zero trips. `frames_delivered > delivered_at_rearm` (stamped at each re-arm) makes recovery mean *a frame arrived on THIS trial*; `trips_total` makes an invisible trip countable.

   `seconds_since_last_message()` alone is **not** a delivery proof and must never be used as one — **(probed)**:

   ```
   epoch before:                                   None
   epoch after bare _subscribe (no server frame):  3.58e-06
   wake set?                                       True
   ```

   `_subscribe` fires `self._on_trigger()` (streaming.py:335) → `StreamTrigger.on_trigger` → `_last_message_epoch = now` (streaming_trigger.py:161). Two subscription POSTs returning 201 read as "delivering". That is why `frames_delivered` exists.

3. **Drive the episode** (§4.5).
4. **Emit the gauges** (§4.6) — always, including while dark.

The whole block after the bearer push is wrapped in `try / except Exception: logger.warning(...)`, matching the existing bearer guard. `run_daemon` calls `on_tick()` bare (control_loop.py:2950-2951) and the CLI catches only `BrokerError` (broker.py:1564), so an unguarded raise from `rearm()` — a `RuntimeError` from `Thread.start()` under thread exhaustion, or a raising context factory — would unwind and kill the protective daemon.

### 4.5 Episode state machine and alert policy

All state lives in the tick closure, which `_build_stream_handles` constructs exactly once per daemon (control_loop.py:3221) — the same daemon-lifetime one-slot trick `deps.kill_state` uses, without a new `LoopDeps` field. The clock is `monotonic`, injected.

```
CLOSED   -> OPEN     when reader_dark and no episode is open.
                     Page ONCE (guaranteed-send). cooldown = FLOOR.
                     Note: keyed on reader_dark, which includes
                     `not is_running()` — so a reader thread that crashes
                     WITHOUT tripping (streaming.py:464-470 swallows the
                     exception, leaving _is_streaming True) is recovered by
                     the same path. This is the case the neighbouring price
                     stream keys on, and the winning proposal missed it.

OPEN     -> TRIAL    when now >= next_trial_mono. Call trigger.rearm(fresh_ctx).
                     rearms += 1; delivered_at_rearm = frames_delivered;
                     cooldown = min(cooldown * 2, CEILING);
                     next_trial_mono = now + cooldown.
                     Also: trigger.reset_liveness() (§4.6) so the hours-old
                     epoch cannot fire `stream-dead`.
                     At most ONE trial per tick.

TRIAL    -> OPEN     implicitly: `up` stays False and the reader re-trips or
                     dies. No page. The re-trip is counted by trips_total.

OPEN     -> CLOSED   when `up` has held continuously for HEALTHY_DWELL_S.
                     Page ONCE (guaranteed-send). Ladder resets to FLOOR.
```

**Per-episode Telegram budget:**

| Event | Sink | Count |
|---|---|---|
| OPEN (down edge) | `deps.alert` — guaranteed-send | exactly 1 |
| while OPEN | — | **0** |
| flap threshold reached | `deps.alert` — guaranteed-send, CRITICAL | ≤ 1 per flap window |
| CLOSE (delivery-confirmed up edge) | `deps.alert` — guaranteed-send | exactly 1 |

The edges use the **guaranteed-send** sink, not `alert_throttled`, mirroring `_alert_kill_transition` (control_loop.py:458-476) whose docstring already states the rule: "edges are rare and each transition must deliver". Routing a genuine edge through an interval throttle is what produced the metronome. `_build_stream_handles` therefore takes `base_alert` alongside `alert_throttled`; `base_alert` is already in scope at the call site (control_loop.py:3211).

**Flapping** mirrors `_AlertThrottle.record_place_failure` (control_loop.py:6200-6216) — the repo's existing escalate-once-then-silence prior art. A "trip" is a `trips_total` increment. On the `_STREAM_FLAP_ESCALATE_AT`-th trip inside a rolling `_STREAM_FLAP_WINDOW_S`, send one CRITICAL and latch. **The latch suppresses further OPEN pages only; the CLOSE page is never suppressed** — an unpaired page is worse than no page, and the operator must always see an episode end.

**The `stream-dead` metronome is closed too.** The existing silence alert at `control_loop.py:3054-3062` is a throttled level alert that today is only suppressed by the early return at `:3046`. Removing that return would relocate the metronome onto the `stream-dead` key: after 14 h dark, `seconds_since_last_message()` returns ~50400, so `alert_throttled(..., "stream-dead")` would fire every 30 min. Two changes close it: `reset_liveness()` on each re-arm, and the alert is now gated on `not episode_open` — a dark stream is already reported by its own episode page and gauge; `stream-dead` is for the dark-but-CONNECTED case only, which is exactly the price-stream Stale rule's shape.

**The reader thread's own alert sink is untouched.** `StreamTrigger`'s client factory passes no `alert=` kwarg (streaming_trigger.py:115-124), so `SaxoStreamingClient` falls back to `logger.warning` (streaming.py:218) and `_trip_breaker`'s line is journald-only. `rearm()` clears `_breaker_alerted`, so the journal gets one line per EPISODE rather than one per process. A test asserts the client factory is called **without** an `alert` kwarg, so a future implementer cannot thread the Telegram sink onto the reader thread and undo PR #900.

### 4.6 Observability

Six gauges, in ONE atomic `emit_domain_metrics(state_paths.stream_metrics_job(), {...})` call — that write overwrites the whole `alphalens_domain_broker-manager-sim-stream.prom` file (control_loop.py:165-172, `observability/textfile.py:115-130`), so a second call to the same domain would clobber the first, and **an omitted key deletes its series**.

| Gauge (suffix on `alphalens_broker_manager_`) | Type | Notes |
|---|---|---|
| `stream_reader_up` | 0/1 | `is_running() and is_streaming` — the reader claims to be working. Mirrors `alphalens_live_price_stream_reader_up`. |
| `stream_breaker_open` | 0/1 | **EPISODE-scoped**: 1 from OPEN until the delivery-confirmed CLOSE. It does NOT flicker per trial — a per-trial gauge would reset to 0 on every rung and no `for:` longer than one rung could ever fire. |
| `stream_last_message_age_seconds` | seconds | Existing name. Now written on EVERY tick, and **never omitted**: when the epoch is `None` it reports seconds since reader start. |
| `stream_consecutive_failures` | level | The number yesterday's journal could not recover. Mirrors `alphalens_live_price_stream_consecutive_failures`. A level for eyeballing — `rate()`/`increase()` on it are nonsense, and the README says so. |
| `stream_trips_total` | monotonic counter | Survives a tick gap; feeds the flap rule. |
| `stream_in_session` | 0/1 | Emitted, referenced by no shipped rule (Q5). |

`StreamTrigger` gains `reset_liveness()` (main-thread, sets `_last_message_epoch = None`) plus read-only delegations for `is_running`, `frames_delivered`, `trips_total`, `consecutive_failures`, and `rearm()`. `reset_liveness` is a new write to a field the module docstring documents as single-writer = stream thread (streaming_trigger.py:14-17); it is safe because it happens only while `is_running()` has just returned False, i.e. no concurrent writer exists at that instant. The docstring is updated to say exactly that, and a test pins it.

**Three Prometheus rules**, in the single `alphalens-cron-health` group (pinned by `test_monitoring_alerts.py:96-98`), each `route: telegram` + `unit: broker-manager`:

```yaml
- alert: AlphalensBrokerStreamBreakerOpen
  expr: >-
    alphalens_broker_manager_stream_breaker_open{job="broker-manager-sim"} == 1
    and (time() - alphalens_broker_manager_last_tick_timestamp_seconds{job="broker-manager-sim"}) < 300
  for: 20m

- alert: AlphalensBrokerStreamStale
  expr: >-
    alphalens_broker_manager_stream_last_message_age_seconds{job="broker-manager-sim"} > 300
    and alphalens_broker_manager_stream_reader_up{job="broker-manager-sim"} == 1
    and (time() - alphalens_broker_manager_last_tick_timestamp_seconds{job="broker-manager-sim"}) < 300
    unless alphalens_broker_manager_stream_breaker_open{job="broker-manager-sim"} == 1
  for: 5m

- alert: AlphalensBrokerStreamFlapping
  expr: >-
    increase(alphalens_broker_manager_stream_trips_total{job="broker-manager-sim"}[1h]) > 3
    and (time() - alphalens_broker_manager_last_tick_timestamp_seconds{job="broker-manager-sim"}) < 300
  for: 10m
```

Two shapes are load-bearing:

- **The daemon-freshness guard** (`time() - last_tick < 300`) on every rule. `emit_domain_metrics` never unlinks (`observability/textfile.py:83-130`), so if the operator stops the SIM unit, or restarts it without `ALPHALENS_BROKER_STREAMING_ENABLED=1`, or the broker/provider gate fails (`_build_stream_handles` then returns `(None, None, None)` and no `stream_tick` ever runs again), node_exporter re-serves the frozen `breaker_open 1` forever and pages a warning nothing in the system can resolve — the same unresolvable-permanent-signal defect, moved from Telegram to Prometheus. The heartbeat gauge lives in a different textfile but freezes the same way, so `time() - frozen` grows past 300 and the guard goes false. `AlphalensBrokerManagerHeartbeatStale` then fires instead, which is the correct alert for "the daemon stopped". This is the same companion-guard shape as `AlphalensLivePriceStreamReaderDown`'s `subscribed_uics > 0` (alphalens.yaml:945-953).
- **`unless` rather than `and == 0`** on the Stale rule's breaker guard, copying the documented reasoning at alphalens.yaml:977-982: the live rules file is hand-synced and may lead the daemon, so an ABSENT gauge must leave the alert behaving exactly as before.

Shipping `AlphalensBrokerStreamStale` makes the load-bearing comment at `control_loop.py:152-154` true; `deploy/systemd/README.md:1129` is corrected in the same commit, including its claim that the repo rules copy is "documentation only" (CI runs `promtool check rules` **and** `promtool test rules`, `.github/workflows/ci.yml:366-385`).

**CLI probe:** `alphalens broker stream-status [--env sim] [--format human|json]` reads the stream domain `.prom` through the existing textfile-dir resolution and prints every gauge. No broker call, no auth, safe while the daemon runs. Per repo CLI doctrine: stdout carries the result only, exactly one JSON value in JSON mode, a `schema` field, exit 4 when the file is absent.

### 4.7 What yesterday's incident would have done under this design

Trip 08:46:21 → episode OPEN, **1 Telegram line**. First trial 08:47:21 (floor 60 s) — Saxo still 5xx → no page, cooldown 120 s. Second trial 08:49:21 — Saxo recovered ~08:48 → heartbeat delivered → `up` True → dwell 300 s → CLOSE at ~08:54, **1 Telegram line**. `breaker_open` was 1 for ~8 min, so the `for: 20m` Alertmanager rule never fires — correct for a self-healed blip.

**Two messages instead of 29, the stream dark ~3 minutes instead of 14 hours, and the operator knows it came back.**

---

## 5. Constants and their rationale

No bare numbers. Every constant is named, sited next to `_DEFAULT_STREAM_STALE_S = 45.0` (control_loop.py:150) except the two client-side ones.

| Constant | Value | Rationale |
|---|---|---|
| `_STREAM_REARM_FLOOR_S` | 60.0 | Must exceed the wall-clock cost of the CLOSED-state budget it follows. **(probed)** that budget is 31 s of sleeps (1+2+4+8+16) plus 6 connect attempts, matching the journal's 08:43:26 → 08:46:21 gap — so a re-arm cycle can never spend connects faster than the failing state it replaces. 60 s is ~2× that and also exceeds both `stale_after_s = 45.0` and the 45 s poll grid, guaranteeing at most one trial per protective pass. |
| `_STREAM_REARM_CEILING_S` | 900.0 | Bounds a long outage at 4 connect attempts/hour (vs ~60/h for a flat floor retry). Cost of waiting is bounded and small: a dark stream costs at most one poll period of extra wake latency, never protection. 15 min is the worst-case dark-after-vendor-recovery window, against the 14 h observed, and is the same order as `_STREAM_SESSION_WARMUP = 15min` (control_loop.py:723) so the operator holds one number. |
| `_STREAM_HEALTHY_DWELL_S` | 300.0 | Delivery evidence required before the ladder resets to the floor. 10× `recv_timeout_s = 30.0` (streaming.py:159) ≈ 10-15 consecutive SIM heartbeats at the ~20-30 s cadence the code documents (control_loop.py:150-152) — enough to call a connection genuinely healthy. A normal reconnect never reaches this code (it never trips), so the dwell cannot affect healthy operation. |
| `_STREAM_FLAP_WINDOW_S` | 3600.0 | 4× the ceiling: a window that saw the escalation threshold has necessarily seen the ladder fail to converge. |
| `_STREAM_FLAP_ESCALATE_AT` | 3 | Mirrors `_MAX_CONSECUTIVE_PLACE_FAILURES = 3` (control_loop.py:6165) — the repo's existing escalate-once threshold on this same throttle. Three trips require three delivery-confirmed closes, which the dwell makes impossible faster than 3 × 300 s = 15 min. |
| `_STREAM_RETIRED_CONTEXT_CAP` | 8 | 5 rungs to ladder saturation plus slack. Bounded so a permanent outage cannot grow the deque without limit. |
| `StreamTuning.min_connection_life_s` | 5.0 | The streak-reset gate. 10× `SaxoClient._MIN_REQUEST_INTERVAL_S = 0.5` (client.py:147), which is what bounds one reconnect+resubscribe cycle — so a gateway that cannot keep a connection alive for 5 s cannot clear the streak. Far below the 20-30 s heartbeat cadence, so it is transparent on a healthy connection: the first heartbeat lands well after the gate opens. |
| `StreamTuning.max_consecutive_failures` | 6 (**unchanged**) | With a re-arm the budget stops being a lifetime allowance and becomes per-cycle, which is what makes 6 correct: ~31 s over 6 attempts is a good blip-vs-outage discriminator. Changing it in the same commit would confound the forward observation of the re-arm itself. |
| `AlphalensBrokerStreamBreakerOpen` `for:` | 20m | Longer than the first four ladder rungs (60+120+240+480 = 900 s) plus one dwell, so a blip healed within four trials never reaches Alertmanager. Safe to choose freely because `breaker_open` is episode-scoped and does not flicker per rung. |
| `AlphalensBrokerStreamStale` `for:` + threshold | 5m / 300s | Verbatim from `AlphalensLivePriceStreamStale` (alphalens.yaml:963-990) — same dark-but-connected condition on the same vendor. |
| `AlphalensBrokerStreamFlapping` `for:` | 10m | Verbatim from `AlphalensLivePriceStreamReaderDown` (alphalens.yaml:952). |
| daemon-freshness guard | 300s | The `AlphalensBrokerManagerHeartbeatStale` threshold (alphalens.yaml:923-932) — one number for "this daemon is still ticking". |

**No new env knob.** `ALPHALENS_BROKER_STREAM_DEBOUNCE_S` is documented in `.env.example:156` and in `streaming_trigger.py:54-58` as "read in the control loop", and `_build_stream_handles` never passes `debounce_s` (control_loop.py:3344-3351) — a documented knob on this exact surface has already rotted to dead. Tuning here is a code change with a test.

---

## 6. Implementation increments — failing tests first

Research tests MUST subclass `unittest.TestCase` (a bare pytest-style function is silently skipped by `unittest discover`). Run:

```
uv run python -m unittest discover -s apps/alphalens-research/tests -t apps/alphalens-research -v
```

The whole streaming suite is green today and runs with no I/O — **(probed)** `Ran 52 tests, OK (0.002s)`.

### INC-0 — the merge blocker (no code) — **CLEARED 2026-08-23**

Run `SAXO_STREAM_LIVE_TEST=1` and confirm that a positions/orders subscription can be created on a **fresh** contextId minted mid-process, after a prior context's `delete_all_subscriptions`. If it fails, the whole re-arm is inert and this memo goes back to DRAFT.

**Result: PASS.** Probed on the VPS 2026-08-23 10:13-10:15 CEST against SIM, as a faithful simulation of the designed `rearm()` — ONE process, ONE `SaxoStreamingClient`, `_context_id` rotated in place between cycles (not two independent contexts).

| Cycle | Context | Cold connect | pos / ord | First frame |
|---|---|---|---|---|
| 1 — healthy stream | `inc0-…a` | yes | 201 / 201 | `_heartbeat` |
| simulated `_trip_breaker()` | `delete_all_subscriptions(A)` | — | 202 / 202 | — |
| 2 — **the re-arm** | `inc0-…b` (minted mid-process) | yes | **201 / 201** | **`_heartbeat`** |
| retired-context drain | `delete_all_subscriptions(A)` while B live | — | 202 / 202 | — |
| 3 — B after the drain | `inc0-…b` | yes | 201 / 201 | `_heartbeat` |

So: rotation is accepted, the re-armed reader delivers, and draining a retired context does not disturb the live one. The subscription envelope carries `{ContextId, Format, InactivityTimeout, ReferenceId, RefreshRate, Snapshot, State}`.

**Not established by this probe:** warm reconnect with a `messageid`. Cycle 3 was intended as the warm case but `_last_message_id` stayed `None` (heartbeat frames do not advance it), so it connected cold like the others — `connect_url_has_messageid: false` in all three cycles. Immaterial to this design, which connects cold after every re-arm by construction (§4.3), but it is not evidence about the warm path and must not be cited as such.

### INC-1 — client read surface + delivery-life gate

`apps/alphalens-research/tests/brokers/saxo/test_saxo_streaming.py`, new `TestStreamDeliveryEvidence`:

- `test_is_running_is_false_before_start_and_after_the_reader_thread_ends`
- `test_frames_delivered_increments_on_every_real_frame`
- `test_frames_delivered_does_not_move_on_a_bare_subscribe_dispatch`
- `test_trips_total_increments_on_every_breaker_trip`
- `test_a_frame_inside_the_min_connection_life_does_not_clear_the_streak`
- `test_a_frame_after_the_min_connection_life_clears_the_streak`
- `test_one_frame_per_connection_storm_still_trips_the_breaker`
- `TestStreamTuningWiring.test_min_connection_life_default_and_override_reach_the_client`

Then implement. `TestStreamReconnectStormDiscipline` and `TestStreamStartupTokenRace` keep their meaning unchanged (delivery-only reset and the `token_missing` exemption are both preserved).

### INC-2 — `rearm()`, context rotation, retired-context drain

Same file, new `TestStreamRearm`:

- `test_start_after_a_trip_is_a_silent_no_op_returning_true` (pins the trap this fixes)
- `test_rearm_spawns_a_new_reader_thread_and_reopens_the_breaker`
- `test_rearm_keeps_the_failure_streak_so_the_trial_gets_exactly_one_connect`
- `test_a_delivered_frame_after_rearm_restores_the_full_six_attempt_budget`
- `test_rearm_rotates_the_context_id_and_retires_the_old_one`
- `test_rearm_clears_last_message_id_so_the_trial_connects_cold`
- `test_rearm_clears_the_breaker_alert_latch_so_a_second_trip_logs_again`
- `test_rearm_refuses_while_the_old_reader_thread_is_still_alive`
- `test_rearm_refuses_after_stop_so_shutdown_is_never_resurrected`
- `test_rearm_refuses_before_any_bearer_has_been_pushed`
- `test_a_raising_thread_spawn_leaves_is_streaming_false_so_the_next_tick_retries`
- `test_rearm_does_not_clear_the_pushed_token_so_the_startup_exemption_stays_bounded`
- `test_a_retired_context_is_deleted_on_the_next_healthy_subscribe`
- `test_retired_context_deque_is_capped`

**These tests must not spawn a live socket.** `_make_client` (test_saxo_streaming.py:187-204) injects neither `ws_connect` nor `async_sleep`, so a bare `start()` dials `sim-streaming.saxobank.com`. INC-2 therefore also adds a `thread_factory` seam (default `threading.Thread`) to `SaxoStreamingClient`, and the spawn tests use a fake factory. Any test that must drive the real loop injects `ws_connect`/`async_sleep`, porting the price stream's `_SupervisedHarness` + `_ScriptedConn` (`tests/data/test_saxo_price_stream.py:1000-1108`).

Also: `TestStreamCircuitBreaker.test_consecutive_failures_shut_stream_to_poll_only_and_alert_once` asserts today's permanence ("A further failure never re-alerts", test_saxo_streaming.py:381-382). It is **rewritten**, not relaxed — that assertion is what this design consciously contradicts, and the rewrite is the record of the decision.

### INC-3 — tick rewrite: token-first, episode latch, ladder

`apps/alphalens-research/tests/brokers/automanager/test_streaming_trigger.py`:

- `TestStreamTriggerLifecycle.test_rearm_and_read_surface_delegate_to_client`
- `TestStreamTriggerLifecycle.test_reset_liveness_clears_the_epoch`
- `TestStreamTriggerLifecycle.test_context_id_factory_reaches_the_client`
- `TestStreamTriggerSimRail.test_client_factory_is_never_given_an_alert_sink`

`apps/alphalens-research/tests/brokers/automanager/test_control_loop.py`, new `TestStreamEpisodeLatch` / `TestStreamRearmLadder`:

- `test_sustained_breaker_open_pages_once_across_sixty_ticks` (the metronome regression, red first)
- `test_a_scripted_trip_rearm_fail_cycle_pages_at_most_twice_per_hour`
- `test_recovery_pages_only_after_a_delivered_frame_and_the_dwell`
- `test_a_trial_that_dies_before_delivering_pages_nothing`
- `test_a_reader_thread_that_dies_without_tripping_is_rearmed`
- `test_edge_pages_use_the_guaranteed_send_sink_not_the_interval_throttle`
- `test_bearer_is_pushed_before_every_branch_including_the_dark_one`
- `test_bearer_read_failure_still_never_crashes_the_tick`
- `test_a_raising_rearm_never_escapes_the_tick`
- `test_first_tick_after_a_trip_arms_the_cooldown_and_does_not_rearm`
- `test_cooldown_doubles_from_sixty_and_caps_at_nine_hundred`
- `test_at_most_one_rearm_attempt_per_tick`
- `test_a_flap_inside_the_dwell_climbs_the_ladder_instead_of_resetting_it`
- `test_three_trips_in_the_flap_window_escalate_once_then_suppress_open_pages`
- `test_the_close_page_is_never_suppressed_by_the_flap_latch`
- `test_stream_dead_alert_is_silent_while_an_episode_is_open`
- `test_rearm_resets_liveness_so_an_hours_old_epoch_never_pages_stream_dead`

`TestStreamBreakerAlert.test_breaker_tripped_pages_even_with_no_message` (test_control_loop.py:6585) is **replaced** — it is the test that currently requires the repeating page. `test_live_stream_does_not_page_breaker` is kept.

**Also edited, and named here because the winning proposal claimed they were untouched:** all four `TestStreamStaleAlert` methods (test_control_loop.py:6411-6529). Three independent breakages — `emit_gauge` widens from `Callable[[float], None]` to a multi-key emit so `emit_gauge=gauges.append` becomes a TypeError; `_make_stream_tick` gains an `alert` parameter that all five existing construction sites lack; and every `_Trig` stub defines only `is_streaming` / `push_token` / `seconds_since_last_message`, so the new reads raise AttributeError even on the healthy path. INC-3 introduces one shared `_FakeStreamTrigger` in `test_control_loop.py` exposing the full read surface, and rebuilds all five stubs on it, so the next surface addition breaks one class instead of five.

### INC-4 — gauges

`TestStreamGauges` in `test_control_loop.py`:

- `test_gauges_are_written_on_every_tick_including_while_dark`
- `test_the_age_gauge_key_is_never_omitted_when_no_message_has_arrived`
- `test_breaker_open_stays_one_across_a_whole_episode_and_does_not_flicker_per_trial`
- `test_all_six_stream_gauges_land_in_one_atomic_domain_emit`
- `TestStreamGaugeDoesNotClobberHeartbeat` (existing, test_control_loop.py:6642) must stay green.
- `test_stream_metric_docs.py`: extend the existing four-method class to every new gauge base name, reusing the truncated-form negative check.

### INC-5 — Prometheus rules, promtool, docs

- `apps/alphalens-research/tests/test_monitoring_alerts.py`: `test_broker_stream_rules_exist_in_the_cron_health_group_with_route_and_unit`, `test_broker_stream_rules_form_unique_alertname_job_pairs`, `test_every_broker_stream_rule_carries_the_daemon_freshness_guard`.
- New promtool fixture **`deploy/monitoring/prometheus/rules/alphalens_broker_test.yaml`** with its own `evaluation_interval: 1m`, plus the ci.yml step to run it. A second file is required, not optional: `alphalens_test.yaml` sets `evaluation_interval: 1h` at file level (alphalens_test.yaml:19) with fixtures on a 1 h grid, so a `for: 20m` rule goes pending at one hourly evaluation and firing at the next — the 20 m and 10 m cases would be indistinguishable. And `.github/workflows/ci.yml:380-385` hard-codes `/r/alphalens_test.yaml`, so a new file is silently never executed unless the step is edited in the same commit. `test_promtool_lint_parity.py` is extended to cover both files.
- Cases: `breaker_open` held 25 m fires; held 10 m and cleared does not; a frozen `breaker_open 1` with a stale heartbeat does not; `age > 300` with `reader_up 1` fires Stale; the same suppressed while `breaker_open == 1`; `trips_total` +4 in an hour fires Flapping.
- Docs: correct `control_loop.py:152-154`; replace the §8.5 to-do at `deploy/systemd/README.md:1129`, correct its "documentation only" claim, name every gauge, add the 30-second triage recipe and the load-bearing live-rules sync step; `.env.example` gains the gauge names.

### INC-6 — `alphalens broker stream-status`

`apps/alphalens-research/tests/brokers/test_broker_stream_status_cli.py`:

- `test_json_envelope_carries_every_gauge_and_a_schema_field`
- `test_human_output_names_the_breaker_state_and_rearm_count`
- `test_missing_textfile_exits_four_with_a_stable_error_code`

---

## 7. Adversarial review — every finding and its disposition

### 7.1 FIXED — the up-edge fires at thread spawn, so a long outage is noisier than the metronome

Correct and severe. The winning proposal derived the recovery page and the `stream_up` gauge from `_is_streaming`, which `rearm()` sets True before any evidence. During a 6-hour outage that yields 27 ladder rungs × (1 false recovery + 1 re-trip) ≈ 54 guaranteed-send messages — worse than today's 2/hour. The mirror-image case is as bad: a trial that fails inside one 45 s tick gap is invisible, so a flap counter keyed on the flag counts zero trips.

**Fix (§4.4):** operator-visible state is delivery-backed. `up` requires `frames_delivered > delivered_at_rearm` (stamped at each re-arm) plus `silence <= stale_s`; trips are counted from the monotonic `trips_total`, not from an edge the tick might miss. Pinned by `test_a_trial_that_dies_before_delivering_pages_nothing` and `test_a_scripted_trip_rearm_fail_cycle_pages_at_most_twice_per_hour`.

### 7.2 FIXED — the metronome returns under the `stream-dead` key

Correct, and verified by reading the live path: with the breaker branch's early return gone, `seconds_since_last_message()` returns ~50400 after a 14 h dark stretch, which is not `None`, so `control_loop.py:3055-3062` falls through to `alert_throttled(..., "stream-dead")` — one identical line every 30 min. `_last_message_epoch` is reader-owned (streaming_trigger.py:161, 179) and nothing in a client-side `rearm()` touches it.

**Fix (§4.5):** `StreamTrigger.reset_liveness()` called at each re-arm, plus the `stream-dead` alert gated on `not episode_open`. Pinned by `test_rearm_resets_liveness_so_an_hours_old_epoch_never_pages_stream_dead` and `test_stream_dead_alert_is_silent_while_an_episode_is_open`.

### 7.3 FIXED — `AlphalensBrokerStreamDown` on a per-rung gauge can never fire

Correct arithmetic. A gauge sourced from `is_streaming` reads 1 for the ~45-90 s each trial occupies, so the longest continuous 0-stretch is `ceiling − trial ≈ 810-840 s`, under a `for: 15m` = 900 s. The rule would never fire at any rung, while the grafted Stale rule was simultaneously suppressed by `unless breaker_open == 1`.

**Fix (§4.6):** `stream_breaker_open` is **episode-scoped** — 1 from OPEN until the delivery-confirmed CLOSE, with no per-trial flicker — so the `for:` is chosen against the episode. `for: 20m` then means "at least four failed trials plus a dwell". Pinned by `test_breaker_open_stays_one_across_a_whole_episode_and_does_not_flicker_per_trial` and the promtool case.

### 7.4 FIXED (bounded) — the resurrected reader can hold the shared OAuth RLock

Partly correct, and worth bounding. The reader's subscriber is a dedicated `SaxoClient` with its own session but the **shared** thread-safe OAuth provider (control_loop.py:3268-3283), and `get_access_token()` holds an RLock across `_refresh_slow_path` (tokens.py:389-395, 424-437), which takes the token-store flock and then a POST with `timeout=30.0`.

Adjudication: this is **not a new hazard class**. In the healthy steady state today the reader already pulls tokens through that provider on every reconnect; PR #900 designed and accepted that. What the terminal breaker did was accidentally remove it after a trip. The incremental risk the adversary correctly identifies is narrower: resurrecting it *during* an outage when the token endpoint is itself degraded.

**Fix:** `rearm()` refuses when `_current_token is None`, and the tick pushes a bearer refreshed within the last 45 s **before** any re-arm decision (§4.4) — so a trial only ever runs against a token the main thread just validated, and the trial rate is capped at one per 60-900 s. Pinned by `test_rearm_refuses_before_any_bearer_has_been_pushed` and `test_bearer_is_pushed_before_every_branch_including_the_dark_one`.

### 7.5 FIXED — an unguarded `on_tick()` plus pre-set state can kill or wedge the daemon

Correct on both paths. `run_daemon` calls `on_tick()` bare (control_loop.py:2950-2951) and the CLI catches only `BrokerError` (broker.py:1564), so a `RuntimeError` from `Thread.start()` or a raising context factory unwinds the protective loop. And a naive swallow would leave `_thread = None` with `_is_streaming = True` — a permanently dead stream that every instrument reports as healthy.

**Fix (§4.3, §4.4):** `rearm()` restores `_thread` and `_is_streaming` on any exception or a False `start()`, so the next tick retries; the whole episode block in the tick is wrapped in the same best-effort try that already guards `get_bearer()`. Pinned by `test_a_raising_thread_spawn_leaves_is_streaming_false_so_the_next_tick_retries` and `test_a_raising_rearm_never_escapes_the_tick`.

### 7.6 FIXED — keying the re-arm on the breaker flag misses a crashed reader

Correct, and it is exactly the case the neighbouring price stream keys on. `_thread_main` wraps `asyncio.run(self._supervise())` in a bare `except Exception` that only logs (streaming.py:464-470), so anything escaping outside the per-connection try ends the thread with `_is_streaming` still True. The tick's guard would never fire and `rearm()` would refuse on its own `_is_streaming` check.

**Fix (§4.5):** the episode opens on `reader_dark = (not is_running()) or (not is_streaming)`, so a thread that dies for any reason is recovered by the same path. Pinned by `test_a_reader_thread_that_dies_without_tripping_is_rearmed`.

### 7.7 FIXED — `seconds_since_last_message()` is not a delivery proof

Correct, and **(probed)**: `_subscribe` with a stub subscriber and zero server frames stamps the epoch (`3.58e-06`) and sets the wake event, because `_subscribe` calls `self._on_trigger()` directly (streaming.py:335 → streaming_trigger.py:161). Two 201s read as "delivering", which would have manufactured the dwell's evidence and defeated the Stale rule.

**Fix (§4.2, §4.4):** `frames_delivered`, incremented only in `_mark_delivered`, is the delivery signal for the dwell, the recovery edge and `up`. The epoch keeps its existing staleness role. Pinned by `test_frames_delivered_does_not_move_on_a_bare_subscribe_dispatch`.

### 7.8 FIXED — the dwell and a client-derived cooldown are two owners of one decision

Correct on both halves. A cooldown derived from `_consecutive_failures` is unreachable from a main-thread dwell, and the one-heartbeat-then-drop endpoint zeroes the streak so the breaker never trips at all — a ~1.5-2 s reconnect spin with zero Telegram, entirely outside the design's control loop.

**Fix:** the ladder moves wholly into the tick closure (§3 Q2) and `_rearm_cooldown_s` is never added to the client — one owner, injectable clock, `StreamTuning` untouched. The spin is closed separately by the connection-life gate on the streak reset (§4.2). Pinned by `test_one_frame_per_connection_storm_still_trips_the_breaker` and `test_a_flap_inside_the_dwell_climbs_the_ladder_instead_of_resetting_it`.

### 7.9 FIXED — a frozen `.prom` pages forever after the daemon stops

Correct. `emit_domain_metrics` never unlinks (`observability/textfile.py:83-130`), and `_build_stream_handles` returning `(None, None, None)` leaves no writer at all.

**Fix (§4.6):** every new rule carries a daemon-freshness guard, mirroring `AlphalensLivePriceStreamReaderDown`'s `subscribed_uics > 0` companion. Pinned by `test_every_broker_stream_rule_carries_the_daemon_freshness_guard` and a promtool case.

### 7.10 FIXED — `for:` cases are inexpressible in `alphalens_test.yaml`, and CI runs only that file

Correct on both. `evaluation_interval: 1h` is file-level (alphalens_test.yaml:19) with 1 h fixtures, and `.github/workflows/ci.yml:380-385` hard-codes the path.

**Fix (INC-5):** a separate `alphalens_broker_test.yaml` at `evaluation_interval: 1m`, plus the ci.yml step and the parity-test extension in the same commit. Rewriting the two thematic fixtures onto a new grid is avoided.

### 7.11 FIXED — the "TestStreamStaleAlert is unaffected" claim is false

Correct — three independent breakages (`emit_gauge` signature, the new `alert` parameter, the incomplete `_Trig` stubs). Named as edited in INC-3, with one shared `_FakeStreamTrigger` replacing five ad-hoc stubs.

### 7.12 FIXED — `test_sustained_breaker_open_never_pages_again` asserts the implementation back to itself

Correct: a stub whose `rearm_if_due()` returns False makes the assertion trivially true and cannot catch the trip/recover ping-pong.

**Fix:** replaced by `test_a_scripted_trip_rearm_fail_cycle_pages_at_most_twice_per_hour`, whose stub follows a scripted state list driven by re-arm calls and asserts total messages across 60 simulated ticks. That is the test that would have gone red on §7.1.

### 7.13 FIXED — rotating the contextId orphans server-side subscriptions

Correct: the trip-time DELETE is best-effort inside `suppress` (streaming.py:411-414) and is the call most likely to fail during the outage, while `stop()` deletes only the current id (streaming.py:274).

**Fix (§4.3):** a capped retired-context deque, drained best-effort on each healthy `_subscribe` and by `stop()`. Pinned by `test_a_retired_context_is_deleted_on_the_next_healthy_subscribe`.

### 7.14 FIXED — routing `base_alert` into `_build_stream_handles` sits one kwarg from undoing PR #900

Correct as a hazard, and cheap to close. `SaxoStreamingClient` accepts an optional `alert` and production is safe only because `StreamTrigger`'s factory omits it (streaming_trigger.py:115-124).

**Fix:** `test_client_factory_is_never_given_an_alert_sink`, plus a comment at the `_build_stream_handles` signature stating both sinks are main-thread-only.

### 7.15 RESOLVED BY DECISION, NOT FIXED — the judges' session-gating contradiction

Judge 1 forbade session-gating the page; judge 2 required it. The adversary is right that both branches are defective as stated (unconditional → a weekend message storm; gated → zero signal all weekend, since the Prometheus rule may not even be installed).

**Decision (§3 Q5):** keep the page unconditional and get the weekend quiet from the episode latch, which reduces a Saturday trip to one message rather than to none. `stream_in_session` is emitted so a session-aware *rule* is a later YAML change.

### 7.16 ACCEPTED AS A DEPLOY GATE — deleting the metronome before the live rules exist

Every judge and adversary flagged it and none bound it. The repo rules copy is CI-gated (`.github/workflows/ci.yml:366-385`) but the live copy is a separate hand-installed file, and today no `alphalens_broker_manager_stream_*` rule exists anywhere.

**Disposition:** this is a **gated deploy step, not documentation**. INC-5 does not merge until the three rules are installed on the monitoring host, Prometheus is HUP'd, and each rule is confirmed present in `/api/v1/rules`. Until then the change would strictly reduce observability. The runbook step is written as a checklist item, and §8.4 records that the repo runbook and project memory disagree about which copy is authoritative — which is exactly how this gets skipped.

### 7.17 REJECTED — "the breaker threshold itself is mistuned"

Raised as an open question. `max_consecutive_failures` stays 6: with a re-arm the budget becomes per-cycle rather than a lifetime allowance, which is what makes 6 correct, and changing it in the same commit would confound the forward observation of the re-arm. Revisit only with `stream_consecutive_failures` data from a real second incident.

### 7.18 REJECTED — "remove the `# pragma: no cover` on `_supervise` as part of this change"

A real coverage gap, and the injected `ws_connect`/`async_sleep` seams do make the loop hermetically drivable. But this design's re-arm lives entirely **outside** `_supervise` (which is untouched except for one attribute stamp in `_run_one_connection`), so closing that gap is a separate follow-up PR, not scope inside an incident fix on a protective daemon.

### 7.19 REJECTED — "extract a shared breaker abstraction for both Saxo readers"

The two readers duplicate four tuning values plus the backoff formula and the trip/exit control flow. `test_module_dependencies.py:229-242` forbids `alphalens_pipeline.data` importing `alphalens_pipeline.brokers` with `exemptions: set()`, so a shared home would be `data/` or the `broker_contract` leaf, and both readers would have to migrate. The duplication is already documented as deliberate (saxo_price_stream.py:23-29). A cross-package refactor does not belong inside a protective-path fix.

---

## 8. Residual risks and out of scope

### 8.1 Merge blocker — CLEARED (was: unverified)

Whether Saxo SIM accepts creating positions/orders subscriptions on a **fresh** contextId minted mid-process after a prior context's `delete_all_subscriptions` plus a multi-hour gap. Rotation is the safer of the two options (saxo_price_stream.py:32-36 mandates it after the 2026-08-10 incident) and the design fails safe — a rejected subscription raises through `_require_subscription_created` (streaming.py:126-138) and is just another failed trial. ~~**Settle it with `SAXO_STREAM_LIVE_TEST=1` before merge; do not assert it works from reading alone.**~~ **Settled 2026-08-23: PASS — see INC-0 above.** The remaining unprobed variable is the multi-hour gap: the probe rotated the context seconds after the teardown, not hours. Nothing in the result suggests the gateway holds per-context state that long, but that is inference, not evidence.

### 8.2 Tuning has never met a real multi-hour outage

The 60 s floor is derived from a probed 31 s backoff burst and the 45 s poll grid; the 300 s dwell and the 5 s connection-life gate are reasoned from the documented 20-30 s heartbeat cadence and the 0.5 s REST floor. None has been exercised against a real outage. The `stream_consecutive_failures`, `stream_trips_total` and `stream_rearms_total` gauges exist partly so the next incident produces the data to retune them.

### 8.3 Behaviour change the operator will notice

A short trip that self-heals on the first trial now produces two Telegram lines and no Alertmanager page. Someone used to "a breaker trip pages until I restart it" may read the quiet as broken alerting. The CLOSE page is what makes it legible, which is why the flap latch never suppresses it.

### 8.4 The live rules copy is hand-synced

`deploy/systemd/README.md:1129` calls the repo copy "documentation only" while CI runs promtool against it; project memory `reference_prometheus_live_rules_not_repo_mounted` says the repo copy is the source of truth and the live copy is hand-installed. Both are partly right and the combination is how a rule ships green and stays absent in production. §7.16 makes the install a gate; the README contradiction is corrected in INC-5.

### 8.5 Explicitly out of scope

- **No session gate on the order/position socket.** Whether Saxo pushes order/position frames outside XNYS hours is unverified; sleeping a protective-adjacent reader on an unverified assumption is refused (§3 Q5). If weekend reconnect volume ever becomes a real cost, `_make_stream_session_window` (control_loop.py:735-793) is reusable verbatim and the `in_session` gauge is already emitted.
- **No change to REST retry.** The REST-side alerts during the incident were correct.
- **No `# pragma: no cover` removal on `_supervise`** (§7.18) and **no shared breaker abstraction** (§7.19).
- **No new env knob** (§5).
- **Nothing on the LIVE rail** (§4.1).
