# ADR 0012 — Decommission the paper-trading + broker chain

- **Status:** Accepted
- **Date:** 2026-06-03
- **Supersedes:** none

## Context

Through 2026-05 and early 2026-06 the project built a paper-trade chain to
gather execution-quality feedback on the deterministic `brief_trade_setup`
ladder: plan → submit → reconcile → exit, an always-on Alpaca `trade_updates`
WebSocket daemon for sub-second fill detection, and a deferred Saxo live-trading
client (token chain + reauth) for a future real-broker path. The point was to
measure implementation shortfall — what a real fill cost versus the frictionless
arrival price, and whether a LIMIT or MARKET tier executed better per regime.

That goal never paid off the cost. Honest server-side OCO (a TP and a stop that
truly cancel each other at the broker) needs a live broker; Alpaca paper could
only fast-detect a fill and then attach a protective stop, which is "we attach"
not "the exchange brackets". The chain accreted a long tail of edge-case fixes
(naked-stop windows, phantom positions, wash-trade rejections, partial-fill SL
coverage) without producing the clean execution-quality signal it was built for.

Meanwhile a fully BROKER-FREE measurement path matured and shipped: deterministic
price-path replay of the 3-entry / 3-TP / 1-SL ladder over Polygon minute bars
(`ladder_replay`), plus a population monitor that replays EVERY brief candidate to
terminal (TP / SL / time-stop) over the real ~42-session hold
(`population_ladder_monitor`). These give per-trade R-multiples, MFE/MAE, and
holding-period distributions over the whole candidate population without any
broker, account, or live order — and without the weekend / holiday / fill-race
hazards the live chain carried.

## Decision

Remove the entire broker chain in one atomic change:

- **Clients** — the Alpaca client and the Saxo client + token manager / token
  store / reauth contract.
- **Paper orchestration** — `broker`, `ledger`, `planner`, `submitter`,
  `reconciler`, `exit_manager`, `gross_guard`, `reset`, `report`, and the
  `trade_stream` daemon under `alphalens_pipeline/paper/`.
- **CLI** — the `alphalens paper` and `alphalens saxo` command groups.
- **Infra** — the 9 systemd units (`paper-plan`, `paper-submit`,
  `paper-reconcile`, `paper-trade-stream`, `saxo-refresh`), all paper / Saxo /
  trade-stream Prometheus alert rules, and the `alpaca-py` runtime dependency.
- **Feedback metrics** — the paper-ledger-coupled analytics `shadow_return`,
  `outcome_join`, `execution_modes`, and `execution_telemetry`. They measured
  broker fills that no longer happen.

Keep the broker-free feedback engines (`ladder_replay`, `ladder_backfill`,
`population_ladder_monitor`) and the shared geometry / calendar helpers — the
slimmed `paper/` package (`calendar`, `sizing`, `brief_loader`, `constants`)
plus a new `feedback/bar_window.py` that holds the VWAP-anchor / Polygon
bar-fetch primitives the dying `shadow_return` module used to own.

## Consequences

- Feedback measurement is now fully broker-free price-path replay. The nightly
  `alphalens-feedback-shadow-returns` timer is retained (the unit + command name
  are kept so the existing systemd timer keeps working — a rename is a deferred
  follow-up) but now drives only the ladder + population replays.
- The `feedback.db` columns `shadow_return`, `realized_return`, `fill_status`,
  `exit_kind`, `outcome_plan_id`, and `outcome_computed_at` are orphaned: they
  keep their historical values but no code writes them any more. The schema is
  left intact (no migration) so the history stays readable.
- Regime / VIX-cache stays (it feeds the Django display and the surviving
  per-regime feedback analytics).
- VPS teardown of the systemd units and removal of the `ALPACA_*` / `SAXO_*`
  keys from `/etc/alphalens/env` is a separate operator runbook step.
- This supersedes the operational guidance in the two paper-trade design memos
  (`alpaca_trade_updates_ws_daemon_design_2026_06_03.md` and
  `saxo_client_token_renewal_design_2026_06_03.md`), which are removed.
