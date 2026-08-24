# Startup audit 429 burst — cold-start reconcile fan-out exceeds the /cs audit bucket

**Status:** DRAFT
**Date:** 2026-08-24
**Issue:** [#1094](https://github.com/kamilpajak/AlphaLens/issues/1094)
**Related:** memory `reference_audit_429_startup_burst_2026_08_23`; broker.py:309-318 memo-cache comment (live diagnosis 2026-07-30); 2026-07-23 overnight-spam incident (per-crid alert keys).

## §1 The defect, measured

Every broker-manager restart fires exactly 8 Telegram alerts
`audit_error: Saxo 429 persisted after 4 attempts on GET /cs/v1/audit/orderactivities`,
~70s after start, all within one second. Five reproductions Aug 19-24 (SIM unit), zero
429s anywhere else in that window. Three fully-timed restarts (all UTC, offsets from
stream-reader start):

| Restart (Started)      | Stream start | First 429 | Last 429 | First audit success | 8 alerts    |
|------------------------|--------------|-----------|----------|---------------------|-------------|
| Aug 21 07:39:35.463    | +2.9s        | +33.7s    | ~+62.6s  | +63.6s              | +67-68s     |
| Aug 23 08:13:59.929    | +2.8s        | +33.6s    | ~+62.6s  | +64.0s              | +68-69s     |
| Aug 23 22:38:56.156    | +1.4s        | +33.6s    | ~+62.6s  | +63.6s (~22:40:01)  | +67-68s     |

Per burst: 9 items enter the storm; 8 exhaust 4 attempts each (retry spacing ~1.03s =
1s floor backoff, `_RATE_LIMIT_FLOOR_S`, client.py:152), 1 recovers on attempt 2.
33 total 429 responses over ~29s. Retry-After from Saxo is absent or <=1s (backoff
always fell to floor), so the vendor header does not reflect the real ~30s-away reset.

**Mechanism.** Tick 1's P3 reconcile (reconcile.py:393) calls
`resolve_order_outcome(entry_order_id)` for every journalled bracket absent from the
open-order book — one `GET /cs/v1/audit/orderactivities` each (broker.py:1419-1461).
Terminal outcomes are memoized only in the per-instance `_order_outcome_cache`
(broker.py:318), which a restart wipes, so tick 1 re-audits all 76 brackets in
`sim/submissions.jsonl` back-to-back at the client's 0.5s global spacing (~2 req/s).
Each `BrokerError` becomes an `UNRESOLVED(audit_error)` verdict (reconcile.py:400-405)
-> `AlertOnly` -> `alert_throttled(reason, f"divergence:{crid}")` (control_loop.py:2935).
8 distinct crids = 8 alerts, batched at pass end. The dedup is working as designed.

**Passive bracket on the endpoint's limit.** With 0.5s spacing, ~60-67 requests fit in
the 33.6s before the first 429; then every audit GET 429s for ~29-30s despite ~1/s
probing; first success lands +63.6-64.0s after stream start — 60s after the tick's
first calls — in all three restarts. Best fit: a rolling ~60s window of roughly ~60
requests (~1 req/s sustained). This coheres with, but does not equal, the earlier
live diagnosis of "~10/min /cs audit bucket" (broker.py:311, 2026-07-30) — the two
brackets differ by ~6x, so **the exact quota, its dimension (audit endpoint vs /cs
service group vs session), and rolling-vs-fixed window remain unmeasured**. Successful
GETs are unlogged, so the endpoint mix of the pre-429 calls is unknown.

**Measurement probe (passive, no extra traffic):** log the `X-RateLimit-*-Limit/
-Remaining/-Reset` response headers on the 429 branch (client.py:655-666 currently
parses but never logs them). Saxo documents that the dimension name on the headers
identifies which bucket tripped. Caveat: Saxo warns headers may only appear near the
limit; if the 429s carry none, the journal bracket above is the only measurement.

**Steady state is clean:** Aug 22 (quiet day) = 1745 ticks/24h, zero 429s. After
tick 1 the terminal majority is cached, the 8 `audit_error` items re-audit silently on
tick 2, then ~0 audit GETs/tick. Only the cold-start fan-out needs shaping. The LIVE
daemon (9 submissions) is currently under the limit; the burst migrates to real-money
alerts as `live/submissions.jsonl` grows toward the bracket, so the fix belongs in
shared code.

## §2 Mechanism options

### (a) Stagger the cold-start audit fan-out across ticks

Cap unresolved-outcome audit calls per reconcile pass (e.g. N per tick); brackets over
the cap stay UNRESOLVED-without-alert this tick and are audited on subsequent ticks. At
45s cadence, ~8-16 audits/tick drains 76 brackets in 5-10 ticks (~4-8 min) while never
exceeding even the conservative ~10/min bracket.

- *Fixes:* the burst at its source — the request rate never approaches any plausible
  bucket, regardless of the bucket's true dimension. No numeric backoff needed beyond
  the cap, which the journal bracket supports (8/tick ≈ 10.7/min, already observed
  sustainable in the 2026-07-30 diagnosis).
- *Risks:* deferred brackets must not fabricate a verdict — the pass must report them
  as "deferred" (skipped, not UNRESOLVED(audit_error)), or the alert path fires anyway.
  Requires a seam in `reconcile_brackets` to distinguish "audit not attempted" from
  "audit failed". Delays genuine divergence detection on cold start by minutes —
  acceptable: the same information was previously arriving as 8 false alarms, and
  fail-closed semantics (no CancelRemaining, no terminal guess) are untouched.
- *Test shape:* fake broker counting `resolve_order_outcome` calls; a journal of K>cap
  brackets yields exactly cap calls on pass 1, remainder on pass 2; deferred brackets
  produce no verdict and no alert; a genuinely failing audit inside the cap still
  yields UNRESOLVED(audit_error).

### (b) Per-service-group slow lane in SaxoClient

Give `/cs/` audit paths their own min-interval (e.g. 6s ≈ 10/min) in `_throttle`
(client.py:584-592 currently one global interval, no path argument; `_request` has the
path in scope). Honour `Retry-After`/`X-RateLimit-*-Reset` when present (already
parsed, client.py:611-625).

- *Fixes:* protects every /cs consumer (verdict pass + entry-trail reconcile pass,
  control_loop.py:2603-2628) with one shared budget; steady state unaffected (steady
  audit rate is ~0/tick, far under any interval).
- *Risks:* **the interval is exactly the number we do not have** — 6s comes from the
  weaker 2026-07-30 bracket; the Aug journals suggest ~1/s sustained is fine. Too slow
  and tick 1 blocks for 76×6s ≈ 7.6 min inside one tick (tick watchdog/heartbeat
  interplay); too fast and the burst persists. Also per-instance only — the streaming
  client is a separate instance with its own budget (control_loop.py:3480-3495),
  acceptable today (only the tick thread hits /cs) but must be stated.
- *Test shape:* extend `test_saxo_client.py` throttle tests — audit-path requests space
  at the slow-lane interval, non-audit paths keep 0.5s; injected sleep list pins both.

### (c) Startup-grace alert gating

A 429-family audit failure (`BrokerRateLimitError`) within the first N ticks defers the
divergence verdict to the next tick — counted and logged — and pages only if it
persists M consecutive ticks. Per-position `divergence:{crid}` keys untouched.

- *Fixes:* the alert noise only. The 33-request 429 storm still happens every restart,
  which is itself a documented Saxo "Client Reputation" IP-block risk factor
  (excessive failing requests -> 403 for the whole VPS IP). So (c) alone is
  insufficient; it composes with (a) as a belt for the residual case where even the
  staggered rate trips an unknown tighter limit.
- *Risks:* suppression of true failures — the 8 alerts are genuine UNRESOLVED states;
  gating must be scoped to rate-limit errors only (not all BrokerError), time-bounded
  (first N ticks), and must escalate on persistence. Never fabricates a verdict — the
  bracket simply stays unresolved one more tick, which is already the contract.
- *Test shape:* control-loop test — rate-limit-flavoured UNRESOLVED on tick 1 alerts
  nothing and logs a deferral counter; same verdict persisting M ticks alerts once;
  non-429 audit_error alerts immediately even on tick 1.

## §3 Recommendation

**Option (a) alone, one PR.** It removes the burst at the source without inventing a
backoff number: the per-tick cap is the only constant, and the journals bracket it from
both sides (8/tick sustained was clean on 2026-07-30; 2 req/s continuous trips the
bucket at ~60 requests). Options (b) and (c) each require the unmeasured number or only
treat the symptom; hold them unless the header-logging probe (below) shows the limit is
tighter than the cap.

Named constants (shared code, `alphalens_pipeline/brokers/reconcile.py`):

- `_MAX_OUTCOME_AUDITS_PER_PASS = 8` — audits per reconcile pass; ≈10.7/min at 45s
  cadence, the rate live-observed sustainable on 2026-07-30 and ~6x under the Aug
  rolling-window bracket. Applies to the verdict pass; the entry-trail pass draws from
  the same per-tick budget so both consumers together stay under the cap.
- `VERDICT_AUDIT_DEFERRED` — a non-alerting marker (log line + counter, no
  AlertOnly) distinguishing "audit not attempted this pass" from
  `UNRESOLVED(audit_error)`. Deferred brackets are retried next pass, oldest first.

Increments with failing tests first (TDD):

1. **Cap the verdict-pass fan-out.** `test_reconcile.py`: journal of 10 disappeared
   brackets + cap 8 -> exactly 8 `resolve_order_outcome` calls, 2 deferred with no
   verdict and no alert; next pass audits the deferred 2 first. Existing
   `test_resolver_error_is_unresolved_audit_error_not_an_exception` must stay green
   (real failures inside the cap still alert).
2. **Share the budget with the entry-trail pass.** `test_control_loop.py`: with
   entry-trail armed, trail-pass audits consume the budget and the verdict pass gets
   the remainder; total audit calls per tick never exceed the cap.
3. **Passive measurement instrument.** `test_saxo_client.py`: the 429 warning line
   includes any `X-RateLimit-*` headers present on the response (currently parsed,
   never logged — client.py:655-666). No behaviour change; sharpens the bracket for
   free on the next restart and settles the dimension question.

Acceptance: next VPS restart shows zero `audit_error` alerts, journal shows staggered
audits draining over ~10 ticks, and any 429 line carries the vendor headers.

## §4 Non-goals

- **No merging or sharing of alert dedup keys.** `divergence:{crid}` per position is
  the deliberate fix for the 2026-07-23 overnight-spam incident (control_loop.py:2927-2935).
- **No numeric backoff beyond the measured bracket.** No slow-lane interval, no
  endpoint quota constant — the endpoint's limit is bracketed, not measured. The cap
  in §3 is a fan-out shaper justified by observed-sustainable rates, not a claimed quota.
- **Steady-state behaviour unchanged.** Terminal memoization stays per-instance and
  unbounded (broker.py:314-318); UNKNOWN outcomes stay uncached (self-healing,
  broker.py:1455-1460); UNRESOLVED re-audit next tick stays the contract; quiet-day
  tick cadence and alert wording untouched.
- **No persisted outcome cache** in this PR — pre-warming across restarts would also
  fix the burst but adds a durable-state surface (staleness, per-env files) for a
  problem the cap already solves; revisit only if journals grow past what staggering
  drains in acceptable time.

## §5 Open questions

1. Cold-start divergence latency: with 76 brackets and cap 8, a genuine divergence in
   the last-audited cohort is detected ~7 minutes after restart instead of ~70s. Is
   that acceptable for LIVE, or should the cap prioritise brackets with recent
   journal activity first (rather than journal order)?
2. If the header-logging increment shows the tripped dimension is the whole /cs
   service group (not audit-specific), other /cs consumers added later would share the
   bucket — at that point option (b)'s slow lane becomes worth building with the then-
   measured number. Park until the headers land.

## Amendment 1 — owner adjudication (2026-08-24)

Two §3 points are overridden before implementation; §5 Q2 stays parked.

1. **The cap is 6 per pass, not 8.** §3 justified 8/tick by citing "the rate
   live-observed sustainable on 2026-07-30" — that citation is INVERTED. The
   broker.py:311-313 comment records that ~10.7 GETs/min against a /cs audit
   bucket of ~10/min PRODUCED the rhythmic 429 bursts; that diagnosis is what
   motivated the terminal memoization in the first place. The two passive
   brackets (July: ~10/min; August: ~60 requests per rolling ~60s) disagree by
   ~6x and the bucket's dimension remains unmeasured, so the cap must sit under
   BOTH brackets: `_MAX_OUTCOME_AUDITS_PER_PASS = 6` at the 45s tick cadence =
   8 audit GETs/min. The cap is a transient-only shaper — a 76-bracket cold
   start drains in ~10 minutes; steady state (memoized terminals) spends ~0
   budget per tick.

2. **§5 Q1 is answered by default engineering: audit ordering is MOST-RECENT
   journal activity first.** The verdict pass orders disappeared brackets by
   the newest journal timestamp first, so a genuine divergence on a recent /
   live bracket is detected in the first passes while weeks-old likely-terminal
   brackets drain last. Deferred brackets carry the non-alerting
   `VERDICT_AUDIT_DEFERRED` marker (log line + pass counter, no AlertOnly, no
   verdict) and are picked up on the next pass continuing the same recency
   order (memoized terminals resolve budget-free, so previously-audited
   brackets never re-consume the budget).

§4 hard constraints unchanged: per-position `divergence:{crid}` alert keys
untouched; steady-state behaviour unchanged (terminal memoization, UNRESOLVED
re-audit next tick); no numeric backoff invented; a DEFERRED bracket never
fabricates a verdict and is at-least-as-safe as today's
`UNRESOLVED(audit_error)` for every downstream consumer (placement dedup is
journal-keyed and the protection pass is broker-state-keyed — neither reads
verdicts — so deferral removes only the alert while keeping the retry).
