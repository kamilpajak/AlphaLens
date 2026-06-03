# Saxo OpenAPI client + 24/7 token auto-renewal — locked design

Status: LOCKED 2026-06-03. Produced by adversarial design workflow (research → draft → 5 failure-mode reviewers → synthesis). Implement exactly this.

## Honest 24/7 verdict (goes verbatim into PR ## Known issues)

True ZERO-TOUCH 24/7 is NOT guaranteeable on retail Saxo, and the OAuth contract is the reason - but the limitation is event-driven, not a fixed clock. Verified facts: the Code/PKCE grant has NO hard session ceiling, so an UNINTERRUPTED refresh chain (refresh before each ~2400s window) can in principle live indefinitely. The 24h death only applies to the SIM-only Developer-Portal 24H token, which this design refuses to use. So the literal '24h hard cap kills the loop' fear is FALSE for our grant.

What we CAN guarantee: as long as (a) the VPS stays up, (b) chrony/timesyncd keeps the clock disciplined, (c) the 5-min keep-alive fires, and (d) no human-side event fires, the chain self-sustains hands-free for days/weeks/months.

What we CANNOT automate away - the unavoidable manual browser re-login - triggered by, in rough order of likelihood: (1) any VPS/maintenance gap longer than the ~40-min refresh window (a 30-min kernel reboot or a slow `docker compose pull` IS enough) - the persisted refresh token expires and the next call gets invalid_grant; (2) the user revoking app access in SaxoTraderGO; (3) Saxo disclaimer re-confirmations (this is exactly why Saxo recommends weekly re-auth); (4) a SIGKILL in the narrow 2xx->journal-active write window where the response was also lost in transit (rare, and our journal makes it loud not silent). None of these has an unattended recovery path - re-auth needs a human at a browser by OAuth design.

The honest promise: 'Zero-touch indefinitely WHILE the VPS stays up and no revoke/disclaimer/long-outage event fires. A manual browser re-login is an unavoidable, unpredictable, event-driven requirement - typically rare (weeks apart) but possible at any time.' The best the subsystem can GUARANTEE is to detect that event within ~30 minutes via the timestamp-staleness rule (AlphalensSaxoRefreshStale), to fire WHILE the token is usually still alive so re-login is calm, to fail CLOSED (sticky reauth flag halting trading) rather than spin, and to make the failure scream rather than hide. The one operational mitigation that meaningfully reduces frequency is the proactive weekly re-auth hygiene (AlphalensSaxoFullAuthAging), which absorbs disclaimer-driven terminations before they break a live session. This belongs in the PR `## Known issues` verbatim - we must not market this as 'rock-solid 24/7'.

## Scope

SHIP IN THIS PR (auth + renewal foundation, single-writer + read-only probe):
1. `SaxoTokenStore` — durable-rename persistence (temp-write+fsync+os.replace+PARENT-DIR fsync), 0o600 via `os.open(O_RDWR|O_CREAT,0o600)`, cross-process flock, intent-journal field for crash recovery. Every write (including the sticky reauth flag) goes through the full rename path — never an in-place mutation.
2. `SaxoTokenManager` — load/proactive-refresh/rotate/double-checked-under-lock/reauth detection, with DEADLINE-bounded retry, monotonic in-process token-life guard, env-record interlock, locally-expired-token short-circuit, and intent-journal recovery.
3. `SaxoClient` — httpx core with a `_transport` seam + a hard `_redact` boundary (token endpoint never logs raw body/headers), error hierarchy.
4. `alphalens saxo {auth,refresh,status,probe}` CLI.
5. ONE read-only probe (`GET /port/v1/users/me`) — proves the whole token→bearer→authenticated-2xx loop with zero financial blast radius.
6. `deploy/systemd/` SINGLE-WRITER keep-alive unit (`alphalens-saxo-refresh`) + Prometheus rules + `test_monitoring_alerts.py` coverage.
7. Enforcement tests (`test_no_raw_saxo_http.py`, `test_saxo_metrics_allowlist.py`, deploy-unit assertions).

DECISION — COLLAPSE TO SINGLE-WRITER (resolves the central reviewer disagreement). ONLY the `alphalens-saxo-refresh` keep-alive process ever calls `/token` with grant_type=refresh_token. All other consumers (the future order/probe paths) are READ-ONLY on the token file: they read the current access token; if it is fresh they use it; if it is missing/expired they fail loud with `SaxoReauthRequiredError` and NEVER refresh. This removes the entire rotation-race class regardless of whether flock coordinates across the host-venv/container split (rotation-race Finding 2, restart-bootstrap Finding 3). The flock remains as a belt over a single suspender (guards an operator running `alphalens saxo refresh` by hand while the timer also fires) and is exercised by a real 2-process test, but correctness no longer DEPENDS on cross-container flock semantics. The draft's contradiction (every consumer can refresh under lock, yet the keep-alive is 'the ONLY job whose sole purpose is...') is resolved in favor of single-writer.

OUT (explicit follow-up track 'Saxo BrokerClient adapter'): order placement (submit_limit/stop/market, OCO/exit-ladder, get_position/get_order/cancel_order, BrokerClient Protocol conformance). Order placement is irreversible and money-moving; it must not ship alongside unproven auth plumbing. The renewal subsystem has a self-contained correctness story a reviewer can fully reason about, wired only to a read-only probe.

OUT-of-scope reviewer items, named explicitly: (a) dedicated trading UID isolation (secret-leak Finding 4 part 3) — accepted-risk for now, documented, not fixed in this PR; revisit before the order layer. (b) full SIGTERM-completes-write handler (restart-bootstrap Finding 5) — the intent-journal makes the crash window RECOVERABLE, so the SIGTERM handler is a nice-to-have, deferred; we ship the journal + generous TimeoutStopSec instead. (c) dynamic systemd cadence reprogramming from live refresh_token_expires_in — we ship a SHORT FIXED cadence (5 min) that is robust to the 2400s uncertainty without needing self-rewriting timers; the manager still READS the live field and the metrics expose it.

## Module layout

### `apps/alphalens-pipeline/alphalens_pipeline/data/alt_data/saxo_client.py`

HTTP gateway (httpx, `_transport` seam). Builds /token + gateway requests, classifies errors via STRICT predicate (permanent only on status in {400,401} AND JSON body AND error=='invalid_grant'|'invalid_client'; everything else transient). Owns the `_redact()` boundary: the /token request body+headers and any response body are NEVER passed raw into an exception message or log — only status_code + parsed OAuth error/error_description fields. Error hierarchy: SaxoClientError -> {SaxoTransientError, SaxoReauthRequiredError, SaxoTokenContractError, SaxoLockUnavailableError, SaxoConfigError, SaxoEnvironmentMismatchError, SaxoBootstrapNeededError}. `from_env`+`get_default_saxo_client` singleton+atexit close+`_reset_default_client_for_tests`. Does NOT mirror the polygon `f"...{resp.text[:200]}"` raise idiom (secret-leak Finding 1).

### `apps/alphalens-pipeline/alphalens_pipeline/data/alt_data/saxo_token_manager.py`

Policy brain. needs_refresh (wall-clock cross-restart OR in-process monotonic-deadline, whichever trips first); proactive margin; deadline-bounded retry against refresh_token_expires_at; double-checked-refresh under lock; SINGLE-WRITER refresh; intent-journal write-ahead + crash recovery; reauth detection with distinct reason (expired_locally vs server_rejected); env-record interlock (record['environment'] must equal requested env); locally-expired-token short-circuit (never POST a wall-expired RT); min-rotation-interval guard against NTP-step double-rotation. Pure logic over injected clock+monotonic+transport+store. Heaviest unit-tested unit.

### `apps/alphalens-pipeline/alphalens_pipeline/data/alt_data/saxo_token_store.py`

Durability + concurrency primitive. Token record JSON under a NON-synced secret dir (see persistence). Atomic write: NamedTemporaryFile(dir=final_dir) -> write -> flush -> os.fsync(fd) -> os.replace -> os.fsync(parent_dir_fd). 0o600 via os.open(O_RDWR|O_CREAT,0o600). Cross-process flock on a SEPARATE .lock inode (bounded acquire). FAILS LOUD (SaxoLockUnavailableError) when the lock file/dir cannot be created — does NOT degrade-to-no-op like sec_rate_coordinator (restart-bootstrap Finding 3). Every write (incl. reauth flag, intent journal) uses the full rename path — no in-place mutation. Short-read/corrupt-JSON guards return a typed error, never a silent empty token. Same-dir tempfile asserted in test.

### `apps/alphalens-pipeline/alphalens_cli/commands/saxo.py`

`alphalens saxo {auth,refresh,status,probe}`. auth = interactive Authorization-Code+PKCE bootstrap (--manual default for headless VPS, reads pasted redirect URL via non-echoing stdin; NO --code/--secret/--token argv options — shell-history/ps leak ban, secret-leak Finding 6). refresh = the single-writer keep-alive ExecStart. status = chain health (ages/booleans/expiry-deltas ONLY, never any token substring). probe = read-only GET /port/v1/users/me end-to-end smoke. SAXO_ENV required (no silent sim default); empty-string rejected; live requires SAXO_ALLOW_LIVE affirmative.

### `apps/alphalens-research/tests/test_saxo_token_manager.py`

Hermetic manager tests — injected clock+monotonic+transport+fake store. All token-lifecycle, skew, classification, recovery, env-interlock, reauth cases.

### `apps/alphalens-research/tests/test_saxo_token_store.py`

Real-file store tests — 0o600 mode, atomic rename, parent-dir fsync (spy asserts 2 fsync calls), corrupt/short-read guards, same-dir tempfile, real 2-process flock race (exactly-one-refresh).

### `apps/alphalens-research/tests/test_saxo_client.py`

HTTP core + redaction boundary (sentinel-token-never-in-exception-or-log, with a positive control that an unredacted build FAILS).

### `apps/alphalens-research/tests/test_no_raw_saxo_http.py`

Enforcement — host fragments logonvalidation.net + gateway.saxobank.com AND raw httpx/requests shapes, conjunction, positive control, exempt only saxo_client/saxo_token_manager (mirrors test_no_raw_openrouter_http.py).

### `apps/alphalens-research/tests/test_saxo_metrics_allowlist.py`

Static scan of saxo emit call sites: every metric key matches the closed allow-list with only environment in {sim,live} as a label and a numeric/$expr value; literal tokens refresh_token/access_token/Bearer/client_secret/code= never appear in any emitted value (secret-leak Finding 2). Positive control with a bad label must fail.

### `apps/alphalens-research/tests/test_deploy_systemd_units.py`

Asserts: saxo-refresh pins an explicit --env; non-trading units (edgar-detect, thematic-build, literature-scan-*, django) do NOT bind-mount the saxo token dir nor pass SAXO_APP_SECRET/SAXO_REFRESH_TOKEN; token dir constant appears in the rsync/rclone exclude list in deploy docs (secret-leak Findings 4,5).

### `deploy/monitoring/prometheus/rules/alphalens.yaml`

+ AlphalensSaxoReauthRequired, AlphalensSaxoRefreshStale (the load-bearing staleness rule), AlphalensSaxoReauthMetricMissing, AlphalensSaxoBootstrapNeeded, AlphalensSaxoChainStateMissing(live, critical).

## Token lifecycle

LAZY-ON-CALL READ + SINGLE-WRITER PROACTIVE REFRESH. Decision rationale: the deployment is oneshot-heavy (edgar-detect 15min, thematic-build 6x/day), so a long-lived in-process refresh loop cannot bridge the gaps and would race other processes — a background thread buys nothing. Instead, ONE dedicated `alphalens-saxo-refresh` oneshot is the sole writer of the chain; all other consumers read the file and use the current access token (or fail loud). The implicit 'loop' is the keep-alive timer.

EXPIRY MATH (verified contract): access_token TTL=1200s, refresh_token TTL=2400s, mandatory rotation (each refresh issues a NEW refresh_token, old immediately invalidated). NEVER hardcode 1200/2400 — read expires_in / refresh_token_expires_in from the live response; fall back to 1200/2400 + a warning only if a field is absent (token contract). Store ABSOLUTE WALL epochs (expires_at), not deltas, because a oneshot can fire minutes after the last write and relative TTL is meaningless across restarts.

WALL vs MONOTONIC (clock-skew Finding 2 — the draft named the split but only applied it to throttle smoothing). When a token is minted/loaded, ALSO stamp an in-process monotonic deadline: `_access_mono_deadline = time.monotonic() + clamp(expires_at - now, 0, expires_in)`. needs_refresh trips if EITHER the wall check OR the monotonic check trips. Wall handles cross-restart persistence; monotonic catches a forward NTP step / long GC pause between check-and-use that would otherwise serve an expired token. A min-rotation-interval guard (refuse to re-rotate if now - rotated_at < 60s while the access token is still valid) suppresses a backward-NTP-step spurious double-rotation (clock-skew Finding 6).

SAFETY MARGINS with an EXPLICIT SKEW BUDGET (clock-skew Finding 1 — the draft's 120s had no documented skew tolerance and evaporated under one-way skew + slow RTT). Decompose: ACCESS_SAFETY_MARGIN_S = MAX_TOLERATED_CLOCK_SKEW_S(60) + MAX_TOKEN_RTT_S(30) + SCHEDULER_JITTER_S(15) + guard => 300s (refresh the 1200s access token at ~15min, 4x faster than the 2400s refresh wall — free at a 5-min cadence). REFRESH_SAFETY_MARGIN_S=300 is the hard backstop. systemd-timesyncd/chrony is a DOCUMENTED DEPLOY PRECONDITION; MAX_TOLERATED_CLOCK_SKEW_S is an asserted constant so the assumption is auditable.

KEEP-ALIVE CADENCE: SHORT FIXED 5 min (OnUnitActiveSec=5min, OnBootSec settled-after-network, RandomizedDelaySec=0 on this deadline-bound timer), NOT the draft's 15 min. Rationale: with refresh_window=2400s and margin=300s, a 5-min cadence fits >=6 attempts inside one window, so even two consecutive missed fires + skew + cold-start + RTT leave large slack (network-partition Finding 1, clock-skew Finding 4). 5 min is also robust if the real refresh window is shorter than the example 2400s (handles the 2400-uncertainty risk WITHOUT a self-rewriting dynamic-cadence timer, which is deferred). Persistent=true so a missed fire runs on boot.

RETRY = DEADLINE-BOUNDED, NOT COUNT-BOUNDED (network-partition Finding 1, CRITICAL — the draft copied polygon's count-bounded 5+15+30s schedule + 'retry next tick' which can run the grant off the 5-min cliff during a normal broker outage). The retry loop guard is `while clock() + next_backoff < (refresh_token_expires_at - HARD_FLOOR_S(30))`. Once the next backoff would cross the deadline, STOP immediately and raise SaxoTransientError loudly — never sleep past the deadline. The chain is NOT marked dead on transient exhaustion (refresh token still valid, the next 5-min fire retries).

CLASSIFICATION (network-partition Finding 2 + hard-cap Finding 5): mark reauth_required (permanent, fail-closed) ONLY on status in {400,401} AND a parseable JSON body AND error in {invalid_grant, invalid_client}. Everything else — non-JSON/HTML proxy bodies, temporarily_unavailable, invalid_request, 5xx, 429, connect/timeout — is TRANSIENT, retried under the deadline budget, never sets the sticky flag. This prevents both a transient masquerading as permanent (self-inflicted halt) and a real death wrapped in an off-shape body being retried forever with no page. A `alphalens_saxo_refresh_failures_total{class=transient|permanent|unclassified}` counter makes an unclassified spike visible.

REACTIVE 401 (clock-skew Finding 3, CRITICAL): a 401 from the gateway routes through the SAME single-writer refresh path (it does NOT inline a bare refresh) — but since only the keep-alive writes, a read-only consumer that gets a 401 simply re-reads the file (a fresh token may already be there) and retries ONCE; a second 401 raises SaxoAuthError (no loop). If the token was rotated <5s ago (monotonic) a 401 is NOT a stale-token problem -> raise, don't churn.

LOCALLY-EXPIRED SHORT-CIRCUIT (restart-bootstrap Finding 2, CRITICAL): before POSTing, if now >= refresh_token_expires_at, do NOT POST (it can only burn into invalid_grant) — set reauth_required directly with reason=expired_locally (distinct from server_rejected) so the alert annotation tells the operator 'downtime' vs 'revocation'.

## Persistence & locking

TOKEN STORE FORMAT: one small JSON record per env. Fields: schema_version, environment(sim|live), access_token, refresh_token, previous_refresh_token, access_token_expires_at(wall epoch), refresh_token_expires_at(wall epoch), rotated_at(wall epoch), reauth_required(bool sticky), reauth_reason(none|expired_locally|server_rejected|lost_rotation), journal_state(active|refreshing), journal_attempted_at. JSON not sqlite — one tiny record, sqlite's WAL/lock surface fights the flock; flat-file+flock is the proven sec_rate_coordinator pattern.

LOCATION — NOT under ~/.alphalens/ (secret-leak Finding 5, HIGH). The draft put it under the shared, container-fanned-out, rsync'd SoT root; the documented `rsync -av jacoren@vps:.alphalens/` and Nextcloud opt-in would exfiltrate a live brokerage bearer off-host (and the synced copy is stale-on-next-rotation = exposure with zero benefit). Store at `/etc/alphalens/saxo/token_<env>.json` (or `~/.config/alphalens-saxo/`), 0o600, STRUCTURALLY OUTSIDE the sync root. Env override SAXO_TOKEN_STORE_DIR. Only the saxo-refresh unit and the future order unit bind-mount this single dir into their container; edgar-detect/thematic-build/literature-scan/django MUST NOT (carve-out asserted by test_deploy_systemd_units). Token dir constant also added to the rsync/rclone exclude list in deploy docs.

ATOMIC ROTATION + DURABLE RENAME (rotation-race Findings 1,5 + clock-skew Finding 5): (1) NamedTemporaryFile(dir=final_dir, delete=False) — dir== final dir asserted in test so os.replace is a same-filesystem rename(2), never a cross-fs copy+unlink that opens a torn-read window over overlayfs. (2) write full JSON, flush, os.fsync(fd). (3) os.replace(tmp, final). (4) os.open(parent_dir, O_DIRECTORY) + os.fsync(dir_fd) + close — the rename itself is not durable on power-loss/hard-reset without a PARENT-DIRECTORY fsync (the draft + sec_rate_coordinator both omit this; here losing the RT = forced re-login, so we pay it; wrapped try/except, log-never-raise on filesystems that reject dir fsync). EVERY write — including the sticky reauth flag and the journal — uses this full path; no in-place field mutation (a crash mid-in-place-write tears the file).

CROSS-PROCESS LOCK: flock on a SEPARATE token_<env>.lock inode, bounded non-blocking acquire (mirrors sec_rate_coordinator _acquire). CRITICAL DEPARTURE from the coordinator: the Saxo lock FAILS LOUD (SaxoLockUnavailableError), it does NOT degrade-to-no-op (restart-bootstrap Finding 3). The coordinator can degrade because a missed rate-gate just risks a 403; here an unsynchronized refresh BURNS a rotating token. A missed refresh is recoverable within the window; a double-rotation is not — so when the lock infra is unavailable (dir not mounted yet, file uncreatable) we refuse to refresh.

LOCK HOLD POLICY — IN-FILE LEASE, lock NOT held across the network (network-partition Finding 3, HIGH; resolves the draft's deliberate inversion of the coordinator's 'never hold across sleep'). The draft held the flock across the /token call AND its backoff sleeps, which under a broker outage parks every other process behind one slow holder until they time out and silently skip renewal. Instead: acquire -> re-read -> if a peer just rotated return (double-check) -> write a short-TTL `journal_state=refreshing`+attempted_at lease -> RELEASE lock -> POST /token (with deadline-bounded retries) -> re-acquire -> write rotated token + journal_state=active -> release. A peer seeing a fresh unexpired lease waits briefly; a peer seeing an EXPIRED lease (holder died mid-backoff) takes over. Single-writer (only the keep-alive refreshes) makes this contention rare, but the lease keeps the crash/manual-invocation case correct. Set the /token HTTP timeout (connect 5s + read 10s = 15s) WELL BELOW the lock-acquire timeout so a hung TLS call always releases via finally-unlock (network-partition + rotation-race Finding 4).

CRASH-SAFETY GUARANTEES (rotation-race Finding 3, CRITICAL — the draft declared the 2xx->os.replace window 'unavoidable' and wrongly waved off the previous-token fallback): the intent journal makes it RECOVERABLE. On startup, if journal_state==refreshing, the chain MAY or MAY NOT have rotated. The manager tries the journaled refresh_token ONCE: 2xx -> recovered (the prior POST never reached Saxo or its response was lost); invalid_grant -> the RT was consumed and the new one was lost -> set reauth_required reason=lost_rotation + alert. This strictly cannot lose (the invalid_grant branch is exactly where the no-journal design already lands) and sometimes wins. We keep BOTH refresh_token and previous_refresh_token so recovery has the candidate available. EXACT guarantee: a torn JSON body is never readable (atomic rename); a power-loss after replace cannot lose the rotation (parent-dir fsync); a SIGKILL in the write window is recoverable-or-loud via the journal; an unsynchronized double-refresh cannot happen (single-writer + lease + fail-loud lock). The ONE residual hazard we cannot fully close: a SIGKILL between the /token 2xx and the journal-active write where the response is also lost in transit AND the RT was consumed — that lands on reason=lost_rotation (loud, manual re-login), not silent.

ENV INTERLOCK (secret-leak Finding 3, CRITICAL + restart-bootstrap Finding 1): on every load, assert record['environment'] == requested env, else SaxoEnvironmentMismatchError (never silently read/create the wrong chain). This catches an env-var flip or a path mixup, including the `-e SAXO_ENV` empty-string-forward incident the repo already hit.

## Metrics & alerts

```
GAUGES (textfile path, job=saxo-refresh, allow-list only, environment in {sim,live}, numeric values only):
  alphalens_saxo_chain_state{environment}                                  # 0 healthy / 1 reauth_required / 2 bootstrap_needed / 3 corrupt
  alphalens_saxo_reauth_required{environment}                              # 0|1 (kept for back-compat with the binary alert)
  alphalens_saxo_refresh_token_expires_at_timestamp_seconds{environment}
  alphalens_saxo_token_chain_last_refresh_timestamp_seconds{environment}   # the load-bearing freshness signal
  alphalens_saxo_metrics_fetched_at_timestamp_seconds{environment}         # companion *_fetched_at (mirrors alphalens_vix_cache_fetched_at)
  alphalens_saxo_token_chain_last_full_auth_timestamp_seconds{environment} # weekly-reauth hygiene
  alphalens_saxo_refresh_failures_total{environment,class}                 # class=transient|permanent|unclassified
  alphalens_saxo_refresh_skipped_degraded_total{environment}               # lease-wait/degrade visibility (never-silent)
  alphalens_saxo_positions_unmanaged{environment}                          # set by the FUTURE exit manager on auth-death (contract gauge, named now)

ALERTS (route: telegram):
  - alert: AlphalensSaxoReauthRequired
    expr: alphalens_saxo_reauth_required > 0
    for: 0m
    labels: {severity: critical, route: telegram, unit: saxo-refresh}
    annotations: {summary: "Saxo {{$labels.environment}} token chain BROKEN - manual re-login required",
      description: "Refresh hit invalid_grant. reauth_reason distinguishes expired_locally (downtime) vs server_rejected (revoke/disclaimer). Run `alphalens saxo auth --env {{$labels.environment}}` on the VPS. Trading HALTED until then."}

  - alert: AlphalensSaxoRefreshStale            # THE load-bearing rule (hard-cap Finding 1, network-partition Finding 4) - catches a stalled timer the ReauthRequired/MetricMissing pair is blind to
    expr: time() - alphalens_saxo_token_chain_last_refresh_timestamp_seconds > 1800   # > 6 missed 5-min fires, still inside the ~40min refresh life so re-login is calm
    for: 0m
    labels: {severity: critical, route: telegram, unit: saxo-refresh}
    annotations: {summary: "Saxo {{$labels.environment}} refresh has not advanced for >30min - keep-alive may be dead while the token is still alive"}

  - alert: AlphalensSaxoBootstrapNeeded
    expr: alphalens_saxo_chain_state == 2
    for: 5m
    labels: {severity: warning, route: telegram, unit: saxo-refresh}
    annotations: {summary: "Saxo {{$labels.environment}} has no token chain - run `alphalens saxo auth --env {{$labels.environment}}`"}

  - alert: AlphalensSaxoChainStateMissing       # live blind-spot promoted to critical (restart-bootstrap Finding 6)
    expr: absent(alphalens_saxo_chain_state{environment="live"})
    for: 10m
    labels: {severity: critical, route: telegram, unit: saxo-refresh}

  - alert: AlphalensSaxoFullAuthAging           # weekly-reauth hygiene; absorb disclaimer terminations proactively
    expr: time() - alphalens_saxo_token_chain_last_full_auth_timestamp_seconds > 518400   # 6 days
    for: 0m
    labels: {severity: warning, route: telegram, unit: saxo-refresh}

DROPPED from the draft: AlphalensSaxoRefreshTokenExpiringSoon (expr expires_at - time() < 1200). It fires post-mortem under a stall (frozen expires_at goes negative only after death) and false-positives CRITICAL on every cold start. AlphalensSaxoRefreshStale replaces it and fires WHILE the token is still alive. AlphalensSaxoReauthMetricMissing is superseded by AlphalensSaxoChainStateMissing (tri-state, live-critical).
```

## Bootstrap CLI

`alphalens saxo auth --env <sim|live> [--manual]` (one-time, interactive, Authorization Code + PKCE/S256 - PKCE mandated on the VPS so no long-lived app secret on disk):
  1. Generate state + PKCE code_verifier/code_challenge(S256). Require SAXO_ENV explicitly (no silent sim default); live additionally requires SAXO_ALLOW_LIVE=1.
  2. Print the .../authorize?response_type=code&client_id=$SAXO_APP_KEY&redirect_uri=$SAXO_REDIRECT_URI&code_challenge=...&state=... URL (host derived from the hardcoded per-env endpoint table, never env-string hosts).
  3. --manual (DEFAULT on the headless/firewalled VPS): operator opens the URL in any browser, logs into Saxo, approves; pastes the full redirect URL back via NON-ECHOING stdin (getpass-style) - never as an argv flag (no --code option; avoids shell-history + ps leakage). Validate state. (--loopback for local dev: a 127.0.0.1:<port> http.server catches ?code=&state=.)
  4. POST /token grant_type=authorization_code with code + code_verifier -> first access_token + refresh_token.
  5. Atomically write the token record (0o600, /etc/alphalens/saxo/token_<env>.json), environment stamped, reauth_required cleared, journal_state=active, last_full_auth_timestamp set.

Companion commands: `alphalens saxo status` (chain health - ages/booleans/expiry-deltas only, no token material, no network call), `alphalens saxo refresh --env <e>` (the single-writer keep-alive ExecStart), `alphalens saxo probe --env <e>` (read-only GET /port/v1/users/me end-to-end smoke). Env: SAXO_APP_KEY, SAXO_REDIRECT_URI, SAXO_ENV (required), SAXO_ALLOW_LIVE (live only); SAXO_APP_SECRET MUST be absent under PKCE. Remote recovery runbook: ssh to VPS, run `alphalens saxo auth --env live --manual`, paste the redirect URL - documented in deploy/systemd/README.md as the >30min-downtime recovery step.

## Failure register

- **[CRITICAL] rotation-race / unsynchronized concurrent refresh burns the rotating token (flock may not coordinate host-venv vs in-container)**
  - Fix: Collapse to SINGLE-WRITER: only alphalens-saxo-refresh ever calls /token; all other consumers read-only and fail loud. Correctness no longer depends on cross-container flock. Lock remains as a belt (manual-invocation guard) with an in-file lease; FAILS LOUD, never degrades to no-op.
  - Regression test: Real 2-process spawn against a shared store + a stub /token offering exactly one rotation -> assert exactly one /token POST and both processes end on the same final refresh_token; second test: lock infra unavailable -> raises SaxoLockUnavailableError with zero POSTs.
- **[CRITICAL] Crash (OOM/SIGKILL/redeploy) in the /token-2xx -> os.replace window loses the only refresh token while the old is already server-invalidated -> forced manual re-login**
  - Fix: Write-ahead intent journal (journal_state=refreshing + the candidate RT, fsync'd) BEFORE the POST; on restart try the journaled RT once. 2xx=recovered, invalid_grant=set reauth_required reason=lost_rotation+alert. Strictly cannot lose, sometimes wins. previous_refresh_token retained.
  - Regression test: Transport returns 2xx with RT2, store raises OSError on os.replace (simulated crash) -> assert on-disk state is journal=refreshing with RT1 intact -> restart manager -> assert it retries the journaled RT exactly once -> on mocked 2xx assert recovery, on mocked invalid_grant assert reauth_required + reason=lost_rotation + gauge=1.
- **[CRITICAL] Transient broker 5xx near the refresh backstop -> count-bounded retry sleeps then defers to next tick -> grant dies DURING retry**
  - Fix: Deadline-bounded retry: while clock()+next_backoff < refresh_token_expires_at - 30. Stop and raise SaxoTransientError before crossing the deadline; never sleep past it. 5-min keep-alive cadence fits >=6 attempts in one window. Chain NOT marked dead on transient exhaustion.
  - Regression test: refresh_token_expires_at = now+290 (inside the 300s backstop), transport returns 503 on every /token, injected clock advances by each backoff -> assert total advance < (290-30), raises SaxoTransientError (NOT SaxoReauthRequiredError), reauth_required stays false, no sleep would cross expiry.
- **[CRITICAL] 401 reactive path fires an un-serialized second refresh -> rotation race -> dead chain**
  - Fix: Single-writer means read-only consumers never refresh on 401 — they re-read the file (keep-alive may have rotated) and retry ONCE; second 401 raises SaxoAuthError, no loop. Token rotated <5s ago => treat 401 as genuine auth error, don't churn. invalid_grant from any refresh hits the identical reauth/metric/alert path (one code path).
  - Regression test: Two managers share one store; consumer gets 401, keep-alive also due -> assert exactly one /token POST, consumer retries once and succeeds; persistent 401 -> raises SaxoAuthError not a loop; invalid_grant on refresh -> SaxoReauthRequiredError + reauth_required=true + gauge=1.
- **[CRITICAL] Routine VPS downtime >40min -> persisted refresh token already expired at boot -> invalid_grant -> halt with no unattended recovery**
  - Fix: Before POSTing, if now >= refresh_token_expires_at do NOT POST (can only burn) -> set reauth_required reason=expired_locally (distinct from server_rejected) so the alert says 'downtime, re-auth' vs 'revoked'. Document: any maintenance window >~30min requires a Saxo re-auth; provide a remote --manual SSH runbook. This is intrinsic to OAuth rotation and cannot be automated away.
  - Regression test: Stored refresh_token_expires_at = now-60 -> get_access_token raises SaxoReauthRequiredError, ZERO /token POSTs, reauth_reason=='expired_locally'.
- **[CRITICAL] SIM/LIVE mixup: env-var selection with silent sim default + `-e SAXO_ENV` empty-string forward -> live chain run under sim intent (and future live orders under sim intent)**
  - Fix: SAXO_ENV required (no default); empty-string rejected (not coerced to sim); LIVE requires a second affirmative SAXO_ALLOW_LIVE=1; endpoints hardcoded per env (never env-string hosts). Env interlock: record['environment'] must equal requested env else SaxoEnvironmentMismatchError (uses the field the draft stored but never checked).
  - Regression test: (a) record environment=='live' + manager env='sim' -> SaxoEnvironmentMismatchError, no network, no write. (b) SAXO_ENV='' -> from_env raises, does NOT default sim. (c) SAXO_ENV=live without SAXO_ALLOW_LIVE -> raises.
- **[CRITICAL] /token error body / request header leaks the refresh token into journald via the polygon `f'...{resp.text[:200]}'` raise idiom the draft says to mirror**
  - Fix: Hard _redact boundary in saxo_client: the /token request body+headers and response body NEVER reach an exception/log raw — only status_code + parsed OAuth error/error_description. SaxoClientError subclasses override __str__/__repr__ to embed only the redacted summary. Do NOT reuse the polygon raw-text raise idiom for saxo_client.
  - Regression test: MockTransport returns a 400 echoing refresh_token=SENTINEL_RT and an Authorization: Bearer SENTINEL_RT header -> assert the raised exception str() contains neither SENTINEL_RT nor 'refresh_token=', AND a capturing handler over the module logs nothing containing SENTINEL_RT; positive control: an unredacted build FAILS the test.
- **[CRITICAL] Stalled keep-alive timer reads 'healthy': ReauthRequired gauge only set when the job runs, MetricMissing absent() blind to a stale-0 textfile left on disk -> dead chain looks fine**
  - Fix: Add the load-bearing AlphalensSaxoRefreshStale rule keyed on time() - last_refresh_timestamp > 1800 (a frozen timestamp grows unboundedly under a stalled emitter — exactly the repo's AlphalensJobStale pattern). Emit a *_fetched_at companion gauge. Drop the draft's expires_at-countdown ExpiringSoon (it fires post-mortem under stall and false-positives on cold start).
  - Regression test: promtool rule test: last_refresh_ts frozen at T, time advances to T+1900 (31min) -> AlphalensSaxoRefreshStale fires from the staleness rule; absent()-MetricMissing does NOT fire (series present-but-stale), proving staleness not absent() catches a stalled timer. Negative control: ts advancing every 5min never fires.
- **[CRITICAL] 'Trading is HALTED' is an alert annotation, not a mechanism — auth death leaves open positions unmanaged with no protective exits; the 20-min still-valid-access-token tail is thrown away**
  - Fix: Make SaxoReauthRequiredError a distinct, non-swallowed exception the future exit manager is CONTRACTUALLY required to treat as 'positions now unmanaged' -> emit alphalens_saxo_positions_unmanaged + (if an access token is still valid in its 20-min tail) attempt one best-effort protective flatten/ensure-stops BEFORE going dark. NOTE: the exit manager itself is OUT of scope for this PR (order layer); we ship the exception contract + the gauge name + the documented requirement now so the order PR cannot silently _safe_call-swallow it.
  - Regression test: Contract test: invalid_grant while the access token has 15min of life + an open position in a fixture ledger -> assert the position-management path does NOT blanket-raise immediately but permits exactly one protective-exit attempt with the still-valid token, then raises. (Test ships now against the contract; the exit-manager impl lands with the order PR.)
- **[HIGH] Margin math built on locally-computed absolutes from a skewable wall clock; fixed 120s access margin has no stated skew budget -> token used at/after expiry**
  - Fix: Decompose ACCESS_SAFETY_MARGIN_S = MAX_TOLERATED_CLOCK_SKEW_S(60)+RTT(30)+JITTER(15)+guard = 300s; assert MAX_TOLERATED_CLOCK_SKEW_S as an auditable constant; require chrony/timesyncd as a deploy precondition.
  - Regression test: VPS clock +90s ahead of Saxo, token minted expires_in=1200 against Saxo time -> advance VPS clock to the real Saxo death moment -> assert needs_refresh is already True (the margin absorbed 90s skew). Fails at 120s, passes at the skew-aware margin.
- **[HIGH] needs_refresh is wall-only; forward NTP step / long pause between check and use serves an expired token**
  - Fix: In-process monotonic deadline stamped at mint/load; needs_refresh trips on wall OR monotonic. The repo's documented file=wall, in-proc=monotonic split, now applied to token life (not only throttle smoothing).
  - Regression test: Inject wall+monotonic clocks; get_access_token caches token; advance monotonic +1200, step wall BACKWARD -600 -> assert needs_refresh True (wall says fresh, monotonic says dead).
- **[HIGH] Lock held across network+backoff -> VPS-wide convoy stall; degraded path silently skips renewal**
  - Fix: In-file short-TTL lease pattern: lock NOT held across the POST/backoff (release after writing the lease, re-acquire to commit). /token HTTP timeout (15s) well below the lock-acquire timeout. Single-writer makes contention rare anyway. A refresh_skipped counter records any degrade so it is never silent.
  - Regression test: 2-proc spawn: stub /token sleeps 3s then 503 for holder A; B starts 0.2s later with a still-valid access token -> B returns within < lease TTL (does not block on A's full backoff), B issues no /token POST (saw A's lease), a counter records the lease-wait path. Second test: A SIGKILLed mid-backoff -> B after lease TTL takes over and rotates.
- **[HIGH] fsync(fd) without parent-directory fsync -> power-loss after os.replace loses the rotation**
  - Fix: os.fsync(parent_dir_fd) after os.replace (durable-rename recipe); try/except log-never-raise for filesystems that reject dir fsync.
  - Regression test: Spy os.fsync -> assert >=2 fsync calls (the temp file fd AND an O_DIRECTORY fd) on the persist path.
- **[HIGH] Code-grant app secret co-located in shared /etc/alphalens/env fanned out to every container; no UID isolation -> any AlphalensLens process (incl. hostile-HTML thematic pipeline) can read the live RT**
  - Fix: Mandate PKCE on the VPS (no Code-grant fallback in prod) so no long-lived SAXO_APP_SECRET exists on disk. Only saxo-refresh + the future order unit receive SAXO_* and the token-dir bind-mount; non-trading units are carved out. Dedicated trading UID is named as ACCEPTED RISK / deferred to the order PR (out of scope here).
  - Regression test: test_deploy_systemd_units: no non-trading unit bind-mounts the saxo token dir or passes SAXO_APP_SECRET/SAXO_REFRESH_TOKEN; saxo-refresh under PKCE does NOT carry SAXO_APP_SECRET; from_env raises if a long-lived secret is present together with the PKCE path (mutually exclusive).
- **[HIGH] Token file under ~/.alphalens/ -> documented rsync/Nextcloud recipes exfiltrate the live RT off-host**
  - Fix: Store at /etc/alphalens/saxo/ (or ~/.config/alphalens-saxo/), structurally outside the sync root; add the token dir to the rsync/rclone exclude list in deploy docs.
  - Regression test: Repo test asserts the token-dir constant is NOT under any sync-recipe path and appears in the documented exclude list; token dir is gitignored / never under the repo tree.
- **[HIGH] World-readable 0o644 metric file + 'caller formats raw labels' convention -> trivial token-in-label footgun**
  - Fix: Closed allow-list: saxo-refresh emits only a fixed set of gauge names with only environment in {sim,live} as a label and numeric/$expr values; never token material/error strings/prefixes. Static enforcement test.
  - Regression test: test_saxo_metrics_allowlist: AST/regex-scan saxo emit sites; every metric key matches the allow-list regex and the literals refresh_token/access_token/Bearer/client_secret/code= never appear in any emitted value; positive control with a bad label fails.
- **[HIGH] No-file / stale-backup / corrupt states collapse into the wrong-or-absent signal on a fresh deploy**
  - Fix: Tri-state alphalens_saxo_chain_state{env} = 0 healthy / 1 reauth_required / 2 bootstrap_needed / 3 corrupt with per-state alerts (AlphalensSaxoBootstrapNeeded runbook = `alphalens saxo auth`). A present file with reauth_required=false but a past refresh_token_expires_at is treated as bootstrap/stale, never healthy, never POSTed.
  - Regression test: No token file -> SaxoBootstrapNeededError + gauge chain_state==2 (not 1, not absent). Restored-backup record reauth_required=false + past expiry -> raises bootstrap/reauth, zero /token POSTs.
- **[MEDIUM] Ambiguous non-invalid_grant 4xx from /token (Saxo 'denied without details') mis-classified transient -> spin forever silently**
  - Fix: Classify by status+body: permanent ONLY on {400,401}+JSON+error in {invalid_grant,invalid_client}; everything else transient under the deadline budget. failures_total{class} counter surfaces an unclassified spike.
  - Regression test: /token returns 400 empty body, 400 HTML body, 503 -> only the JSON invalid_grant/invalid_client cases set reauth_required; others retry under the deadline; positive control 503 -> transient, flag stays false.
- **[MEDIUM] auth --manual / status / journald stdout -> auth codes/tokens in shell history & journal**
  - Fix: auth --manual reads the redirect URL via non-echoing stdin, never argv; no --code/--secret/--token CLI options; status prints only ages/booleans/deltas (no token substring); refresh runs at default (non-DEBUG) log level under the _redact boundary.
  - Regression test: status against a sentinel-token fake store -> captured stdout contains none of the token chars; the saxo typer command defines no value-taking option matching code|secret|token; auth uses a non-echoing read.

## Test plan

- Manager: valid access token outside margin -> returns cached token, ZERO /token calls (clock+transport injected).
- Manager: access token inside ACCESS_SAFETY_MARGIN_S(300) -> exactly one refresh, new token returned.
- Manager: rotation persisted -> after refresh the store holds the NEW refresh_token, previous_refresh_token = the old one, old is not the active RT.
- Manager: refresh response missing refresh_token -> SaxoTokenContractError (never silently keep the invalidated old RT).
- Manager: response uses live expires_in / refresh_token_expires_in (not hardcoded 1200/2400); absent fields fall back to defaults + a warning.
- Manager skew: VPS clock +90s ahead of Saxo, expires_in=1200 against Saxo time, advance to real Saxo death -> needs_refresh already True (margin absorbed skew); fails at 120s margin, passes at 300s.
- Manager monotonic: cache token, advance monotonic +1200, step wall BACKWARD -600 -> needs_refresh True (forward-pause/NTP-step caught by monotonic, not wall).
- Manager min-rotation guard: clock returns T, refresh; step wall to T-90 -> ZERO additional /token POSTs (backward-step spurious double-rotation suppressed).
- Manager deadline retry: refresh_token_expires_at=now+290, transport 503 every call, injected clock advances by each backoff -> total advance < 260, raises SaxoTransientError NOT reauth, reauth_required stays false, no sleep crosses expiry.
- Manager classification: /token 400 empty body / 400 HTML body / 503 -> transient (retry, flag false); only {400,401}+JSON+error in {invalid_grant,invalid_client} -> reauth_required + SaxoReauthRequiredError.
- Manager locally-expired: stored refresh_token_expires_at = now-60 -> raises SaxoReauthRequiredError, ZERO POSTs, reauth_reason=='expired_locally'.
- Manager 401 reactive (single-writer): consumer gets 401, re-reads file (keep-alive rotated) and retries once -> success with one POST total; persistent 401 -> SaxoAuthError, no loop; token rotated <5s ago + 401 -> raises, no churn.
- Manager env interlock: record environment=='live' + manager env='sim' -> SaxoEnvironmentMismatchError, no network, no write.
- Manager bootstrap states: no file -> SaxoBootstrapNeededError + chain_state gauge==2; restored backup reauth_required=false + past expiry -> raises bootstrap/reauth, ZERO POSTs.
- Manager journal recovery: transport 2xx returns RT2 then store raises OSError on os.replace -> on-disk journal=refreshing with RT1 intact -> new manager retries journaled RT once -> mocked 2xx = recovered; mocked invalid_grant = reauth_required + reason=lost_rotation + gauge=1.
- Store: write->read round-trips the record; file mode is exactly 0o600.
- Store: NamedTemporaryFile dir == final dir asserted (same-fs rename); simulated crash between temp-write and rename leaves the OLD file intact and parseable.
- Store: corrupt/truncated JSON on read -> typed error (NOT a silent empty token that would force re-auth).
- Store durability: spy os.fsync -> persist path calls fsync >=2 times, the second on an O_DIRECTORY fd (parent-dir fsync recipe pinned).
- Store 2-process race: two children both refresh against a shared file + a stub /token offering one rotation -> exactly ONE /token POST, both end on the same final refresh_token (lease + flock end-to-end).
- Store lock fail-loud: lock dir uncreatable (parent is a file) -> SaxoLockUnavailableError, ZERO POSTs (never an unsynchronized refresh).
- Store lease takeover: holder SIGKILLed mid-backoff (lease TTL elapses) -> second process observes the expired lease and rotates successfully.
- Client redaction: MockTransport 400 echoing refresh_token=SENTINEL_RT + Authorization: Bearer SENTINEL_RT -> raised exception str() and all module logs contain neither SENTINEL_RT nor 'refresh_token='; positive control: an unredacted build FAILS.
- CLI: status against a sentinel-token fake store -> stdout contains none of the token chars; saxo command defines no value-taking option matching code|secret|token; auth uses non-echoing stdin.
- Config: SAXO_ENV='' -> from_env raises (no sim coercion); SAXO_ENV=live without SAXO_ALLOW_LIVE -> raises; SAXO_APP_SECRET present with the PKCE path -> raises (mutually exclusive).
- Metrics allow-list: AST/regex scan of saxo emit sites -> every key matches the allow-list regex, environment in {sim,live} only, no token literals in any value; positive control bad label fails.
- Deploy units: saxo-refresh pins explicit --env; non-trading units do NOT bind-mount the saxo token dir nor pass SAXO_APP_SECRET/SAXO_REFRESH_TOKEN; token-dir constant in the rsync/rclone exclude list.
- Prometheus (promtool): last_refresh_ts frozen at T, time->T+1900 -> AlphalensSaxoRefreshStale fires from the staleness rule, absent()-MetricMissing does NOT fire (present-but-stale); negative control ts advancing every 5min never fires.
- Contract (exit-manager, ships now, impl later): invalid_grant while access token has 15min life + an open position -> position path permits exactly one protective-exit attempt with the still-valid token, then raises (does not blanket-raise immediately).

## Open questions (resolved with defaults unless flagged)

- Dedicated trading UID vs accepting 'all AlphalensLens processes run as jacoren and can read the live RT' (secret-leak Finding 4 part 3). Deferred to the order PR but it is a real money-system risk - decide before live orders.
- Exit-manager protective-flatten in the 20-min access-token tail (hard-cap Finding 3): we ship the SaxoReauthRequiredError contract + positions_unmanaged gauge now, but the actual best-effort flatten lives in the OUT-of-scope order layer. Confirm the user wants an automated flatten-on-auth-death at all, vs a pure alert-and-let-the-human-act posture.
- previous_refresh_token retention window: we keep exactly one prior RT for journal recovery. Should we keep a short ring (e.g. last 2) to also recover a double-crash, or is one enough? One is the minimal strictly-cannot-lose choice.
- Self-rewriting dynamic cadence: we ship a fixed 5-min timer (robust to the 2400s uncertainty). If Saxo ever tightens the refresh window below ~1500s, is a manager-written next-deadline (short fixed timer that no-ops outside margin) worth the added complexity? Deferred.
- Reviewer disagreement on lock-hold: rotation-race Finding 4 and network-partition Finding 3 both push for NOT holding the lock across the network; the draft argued FOR holding it. We resolved via the in-file lease (release across the POST). Confirm the lease TTL (proposed: max_token_rtt + retry budget, ~45-60s) is acceptable vs a simpler held-lock now that single-writer makes contention rare.
- Whether `alphalens saxo status` should be allowed to run from inside the non-trading containers at all (it reads the token dir) - simplest is to forbid the token-dir bind-mount everywhere except the trading units, so status only runs host-side or in the trading unit.
