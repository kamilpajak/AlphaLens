# Per-environment broker state separation (SIM / LIVE) — design

- **Status:** LOCKED (2026-08-10)
- **ADR:** [ADR 0016](../adr/0016-per-environment-broker-state-separation.md)
- **Predecessors:** ADR 0014 (SIM-only rail), ADR 0015 (keyed day-bound unlock —
  its Consequences section names this exact work as the LIVE-daemon
  precondition), `docs/research/saxo_live_order_rail_lift_design_2026_08_10.md`
  (Horizon 2), `docs/research/broker_manager_extraction_and_exit_geometry_2026_07_31.md`
  (pure-executor model; 2B repo split PARKED).
- **Scope tag:** Track B front 2 of 4 (1 = feed reliability #1018 DONE; 3 =
  LIVE instance client wiring + ADR 0017; 4 = attended soak).

## 1. Goal

Make it structurally possible to run TWO broker-manager instances on one
host — the existing SIM daemon and a future LIVE daemon — with **zero shared
mutable state and zero cross-talk**: separate journals, separate pick
inboxes, separate KILL gates, separate Prometheus job namespaces. This PR
delivers the separation only; booting a LIVE instance stays structurally
blocked until front 3 (its client-construction path + ADR 0017).

## 2. Current state (scouted 2026-08-10, `b326f3e0`)

Environment coupling is implicit and global. Findings, with anchors:

1. **No path seam.** Six independent `Path.home()` joins:
   `control_loop.py:94-97` (`_ALPHALENS_HOME`, `_BROKER_ORDERS_DIR`,
   `KILL_FILE_PATH`), `control_loop.py:1331`
   (`STANDALONE_STOP_JOURNAL_PATH`), `submission_log.py:61`
   (`DEFAULT_SUBMISSIONS_PATH`), `picks.py:42` (`DEFAULT_PICKS_PATH`),
   `safety.py:19` (`DEFAULT_KILL_PATH` — a **duplicate** definition of the
   KILL path), `exec_quality.py:48-49` (`EXEC_QUALITY_PARQUET`).
2. **One metric namespace.** `job="broker-manager"` hardcoded in
   `control_loop.py` (heartbeat `:101`, kill-active `:109`, stream-age
   domain `broker-manager-stream` `:137`); the live-price-stream reader
   hardcodes `_GAUGE_JOB="live-price-stream"`
   (`data/alt_data/saxo_price_stream.py:87`). A second instance would
   overwrite the same `alphalens_domain_*.prom` files.
3. **Picks carry no environment dimension.** `picks.jsonl` line =
   `{ticker, date, armed_ts, status, intent}`; correlation key
   `(ticker, date)`. SIM and LIVE picks would collide in one inbox.
4. **Precedent to mirror.** Token stores are already env-separated and
   env-var-overridable (`SAXO_TOKEN_STORE_PATH` → `~/.alphalens/saxo_auth/`;
   `SAXO_LIVE_TOKEN_STORE_PATH` → `~/.alphalens/saxo_auth_live/`), and the
   textfile dir is env-var-driven (`ALPHALENS_TEXTFILE_DIR`).
5. `broker_contract` is a pure leaf (no paths, no env reads) — untouched.

## 3. Design

### D1 — instance identity: `ALPHALENS_BROKER_ENVIRONMENT`

One new env var, `ALPHALENS_BROKER_ENVIRONMENT ∈ {"sim", "live"}`, default
`"sim"`. Any other value fails loud (`ValueError`) at first resolution.
Named deliberately far from `ALPHALENS_BROKER` (adapter selection,
`registry.py:25`) and from `SAXO_ENV` (vendor rail guard) — three different
knobs, three different names.

### D2 — one path seam: `brokers/automanager/state_paths.py`

New module, the ONLY place that joins state paths. API (all functions, not
import-time constants, so tests and instances resolve fresh; every
env-scoped function takes `env: str | None = None` — `None` resolves via
`broker_environment()`, an explicit value lets the CLI target the OTHER
instance, e.g. `arm --env live`, and is validated identically):

```python
def broker_environment() -> str                             # "sim" | "live", validated
def broker_orders_root(env: str | None = None) -> Path      # ~/.alphalens/broker_orders/<env>
def submissions_path(env: str | None = None) -> Path        # <root>/submissions.jsonl
def picks_path(env: str | None = None) -> Path              # <root>/picks.jsonl
def standalone_stops_path(env: str | None = None) -> Path   # <root>/standalone_stops.jsonl
def kill_file_path(env: str | None = None) -> Path          # <root>/KILL   (per-instance)
def global_kill_file_path() -> Path                         # ~/.alphalens/broker_orders/KILL
def exec_quality_parquet(env: str | None = None) -> Path    # ~/.alphalens/exec_quality/<env>/tranche_fills.parquet
def metrics_job(env: str | None = None) -> str              # "broker-manager-<env>"
def stream_metrics_job(env: str | None = None) -> str       # "broker-manager-<env>-stream"
def price_stream_metrics_job(env: str | None = None) -> str # "live-price-stream-<env>"
def assert_no_legacy_flat_state() -> None                   # raises BrokerStateLayoutError
```

The six scattered definitions funnel through this seam; `safety.py`'s
duplicate KILL constant is deleted. Modules keep their existing DI shape
(e.g. `deps.kill_file`) — only the DEFAULTS change to seam calls resolved at
`build_default_deps` / call time, not import time.

### D3 — KILL: per-instance + global (defense in depth)

- Per-instance: `broker_orders/<env>/KILL` — stops that instance only.
- **Global (legacy path preserved):** `broker_orders/KILL` at the parent
  level halts placement in EVERY instance. The operator muscle-memory
  command `touch ~/.alphalens/broker_orders/KILL` keeps meaning "stop
  everything". Both files are checked wherever one is checked today
  (`safety.check`, `run_once`); KILL semantics (no new placements,
  reconcile + protective actions continue) are unchanged.

### D4 — legacy-layout guard (fail-loud, not fail-empty)

At daemon startup (`build_default_deps`) and CLI journal commands:
`assert_no_legacy_flat_state()` raises `BrokerStateLayoutError` if any of
`submissions.jsonl` / `picks.jsonl` / `standalone_stops.jsonl` exists FLAT
under `broker_orders/` (pre-migration layout). Rationale: a daemon started
against an empty per-env root while the broker holds positions would
reconcile against empty journals — protection logic degrades to
adopt/alert paths. Refusing to start with a migration hint is strictly
safer. (Solo project, no back-compat shims per house doctrine — the VPS
migration is a 3-command runbook step, §6.)

### D5 — metrics: per-instance job labels, unchanged gauge names

- Heartbeat + kill-active: `job="broker-manager-sim"` (LIVE:
  `broker-manager-live`) → textfile `alphalens_domain_broker-manager-<env>.prom`.
- Stream-age domain: `broker-manager-<env>-stream`.
- Live-price-stream reader gauges: the `SaxoLivePriceStream` job label
  becomes a constructor parameter injected by the composition root
  (`build_default_deps`) from `price_stream_metrics_job()`; the module
  keeps no import of `brokers/` (dependency direction preserved). Default
  parameter value stays `"live-price-stream"` for standalone/test use.
- `deploy/monitoring/prometheus/rules/alphalens.yaml`: the four exprs
  pinned to `job="broker-manager"` / `job="live-price-stream"`
  (`:804,:815,:831-832,:846-848`) move to the `-sim` jobs. **No LIVE alert
  rules yet** — `absent()`-style rules for a not-yet-deployed instance
  would page immediately; they land with front 3.

### D6 — pick-source-to-instance mapping

A pick belongs to exactly one instance: `alphalens broker arm` gains
`--env {sim,live}` (default `sim`) choosing which instance inbox
(`<env>/picks.jsonl`) the intent is persisted into. The daemon drains only
its own inbox (its `picks_path()`), so cross-instance drain is impossible
by construction. All other journal-touching CLI commands (`manage`,
`reconcile-fills`, status/read paths) resolve via the seam — i.e. follow
`ALPHALENS_BROKER_ENVIRONMENT` — so one shell var flips an entire operator
session between instances consistently.

### D7 — LIVE instance boot: structurally blocked (front 3 gate)

`broker manage` refuses to start when `broker_environment() == "live"`
with an explicit error naming ADR 0016/front 3. Why: today the daemon's
only client path is `from_env` → unconditionally SIM (ADR 0015 lock). A
"live" instance booted now would trade SIM while journaling/alerting under
LIVE labels — a mislabeled-state hazard worse than absence. Front 3
replaces this guard with the real LIVE client construction, governed by
ADR 0017 (the standing-LIVE authorization model; the ADR 0015 attended
day-bound unlock is explicitly NOT reusable for a daemon — writing it into
a unit file is a doctrine violation per ADR 0015 §5).

### D8 — systemd

- `alphalens-broker-manager.service`: add explicit
  `Environment=ALPHALENS_BROKER_ENVIRONMENT=sim` (self-documenting; also
  survives an `/etc/alphalens/env` mistake because EnvironmentFile
  overrides drop-ins but NOT in-unit `Environment=` lines that come after —
  ordering verified during the 08-10 incident: **EnvironmentFile wins over
  drop-ins**, so the canonical sim pin lives in the unit file itself and
  the runbook forbids setting `ALPHALENS_BROKER_ENVIRONMENT` in
  `/etc/alphalens/env`).
- NO `alphalens-broker-manager-live.service` yet (front 3, once boot is
  actually possible).
- `deploy/systemd/README.md`: migration runbook (§6) + the env-var
  doctrine note above.

## 4. Out of scope

- LIVE client construction / LIVE daemon unit / ADR 0017 (front 3).
- LIVE alert rules (front 3).
- Repo split (2B PARKED), persistence/transport reimpl (Stage 2).
- Any change to placement/reconcile/exit logic — this PR moves WHERE state
  lives, never WHAT is written.

## 5. Test plan

- `state_paths` unit tests: default sim; explicit live; invalid value
  raises; per-env roots; global vs instance KILL paths; legacy-layout
  guard (flat file present → raises; clean layout → passes; empty dir →
  passes); job-name derivations.
- Funnel regression: existing journal/KILL/heartbeat pinning tests
  (`test_control_loop.py` kill/heartbeat classes, `test_picks.py`,
  `test_routing_and_submission_log.py`, `test_observability_textfile.py`,
  `test_stream_metric_docs.py`) updated to the seam — semantics
  unchanged, paths now env-scoped.
- New: sim/live isolation test — arm to live inbox, sim drain sees
  nothing (and vice versa); global KILL gates both; instance KILL gates
  only its own.
- New: `manage` under `ALPHALENS_BROKER_ENVIRONMENT=live` refuses to
  boot (red first).
- Full research suite + ruff + pyright per house CI gates.

## 6. VPS migration runbook (operator, with front-2 deploy)

```bash
systemctl --user stop alphalens-broker-manager
mkdir -p ~/.alphalens/broker_orders/sim ~/.alphalens/exec_quality/sim
mv ~/.alphalens/broker_orders/*.jsonl ~/.alphalens/broker_orders/sim/
[ -f ~/.alphalens/exec_quality/tranche_fills.parquet ] && \
  mv ~/.alphalens/exec_quality/tranche_fills.parquet ~/.alphalens/exec_quality/sim/
sudo rm -f /var/lib/node_exporter/textfile/alphalens_domain_broker-manager.prom \
           /var/lib/node_exporter/textfile/alphalens_domain_broker-manager-stream.prom \
           /var/lib/node_exporter/textfile/alphalens_domain_live-price-stream.prom
cp deploy/monitoring/prometheus/rules/alphalens.yaml ~/monitoring/prometheus/alphalens.rules
docker exec prometheus promtool check rules /etc/prometheus/alphalens.rules
docker exec prometheus kill -HUP 1
git -C ~/AlphaLens pull && ~/.local/bin/uv sync
systemctl --user daemon-reload && systemctl --user start alphalens-broker-manager
# verify: new .prom files carry job="broker-manager-sim"; heartbeat fresh
```

A leftover `broker_orders/KILL` at the parent level is now the GLOBAL kill
— do not move it; its absence is the normal state.
