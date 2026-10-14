# intent-replay

This file loads only once Claude reads, writes or edits a file under `apps/intent-replay/`, and
it is NOT re-injected after `/compact`.

- **Never hand-assemble a run from an armed pick.** Do the join with the script:
  `cd apps/alphalens-research && ../../.venv/bin/python -m scripts.replay_from_pick <trades.json> <TICKER:YYYY-MM-DD> --out <dir> --format json`
  (get `<trades.json>` from `alphalens broker trades --env live --format json`). It writes the
  document, the run configuration, the bars and a provenance sidecar into `--out`. Hand-assembly
  got three values wrong over two days: the FX rate, `costs.exit_edge_min_bps`, and the calendar
  MIC. On a Mac there is no LIVE broker journal, so pass `--entry-trails <entry_trails.jsonl>` —
  `entry_trail_bps` is the one value the trades record does not carry.
- **Derived from the document, imported from the daemon, read from the record — never written as a
  literal.** This is the rule the script exists to enforce, so a hand-edited config breaks it:
  the calendar from `instrument.mic`; the day-1 anchor from `meta.source` (a manual pick's
  `trade_date` IS day 1, a brief's is the session before); the fee card from `costs.fee_card_for`
  by venue; the cost and FX constants by IMPORTING `costs` / `execution`; the rate, its as-of, its
  source and the instrument currency off the record; the direction token from the engine's own
  `Fx.pair_unit()`. The bar window obeys it too: grid and ticker from `instrument.{mic, ticker}`,
  the required end from `config.entry_deadline` plus one session of that venue's calendar, the
  anchor from the record's `exits[-1].venue_time`, the action-window padding from
  `corporate_actions.ACTION_WINDOW_{PRE,POST}_CALENDAR_DAYS`, and the series' granularity
  MEASURED from the series (its modal `t` spacing), never stated.
- **The fields the door computes come from `supplied_derived_paths`**, never from a list written
  out somewhere, so `authored_document` cannot drift from the `derived_field_supplied` refusal it
  exists to avoid.
- **The eight run-configuration keys** (`entry_deadline`, `walk_start`, `entry_trail_bps`,
  `ceiling_price`, `time_stop_t`, `oco`, `fx`, `costs`) and every refusal code:
  [`README.md`](README.md), `## Run configuration` (line 13) and `## Refusal codes` (line 549).
  Nothing is defaulted; a missing key is a refusal.
- **The bar window, the half-open session grid, and why the right edge refuses instead of
  clamping:** [`docs/research/replay_bars_fetch_design_2026_10_10.md`](../../docs/research/replay_bars_fetch_design_2026_10_10.md)
  (LOCKED 2026-10-10) — §5 the grid, §6 the window, §11 the refusals. Every rule there carries the
  measurement that produced it.
- **Replay fidelity, as measured:** [`docs/research/replay_vs_live_panel_2026_10_11.md`](../../docs/research/replay_vs_live_panel_2026_10_11.md)
  (2026-10-11; 7 terminal LIVE records, 4 of them closed). The outcome word agreed on 7 of 7. The
  money did NOT match: ΔR was +0.0456 ASTS, +0.0770 EWTX, −0.0918 SMMT, −0.0734 VST, and the
  replay exited early by 1.0 to 106.5 minutes on 4 of 4. Never claim the replay reproduces the
  LIVE record exactly, and apply no single-signed correction to an R figure — the timing gap is
  one-directional, the money gap is not.
