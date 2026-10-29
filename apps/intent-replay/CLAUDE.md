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
  (2026-10-11; 7 terminal LIVE records, 4 of them closed) measured ΔR at +0.0456 ASTS, +0.0770
  EWTX, −0.0918 SMMT, −0.0734 VST, with the replay exiting early by 1.0 to 106.5 minutes on 4 of
  4. Its "the outcome word agreed on 7 of 7" does NOT hold: `VCYT:2026-09-29` was still live when
  that panel was taken, closed 2026-10-27 as `take_profit` where the replay publishes
  `closed_stop`, and measures ΔR −0.5648 and ΔP&L −7.57 pp — six times the panel's own
  |ΔR| ≤ 0.092. Re-measured 2026-10-28 over 8 broker-backed touched picks: ONE flip of 8, and the
  other 7 stay within ΔR 0.0322.
- **The outcome word can differ because the two sides read DIFFERENT price series, and that is by
  DESIGN.** Touch detection is bid-referenced on the daemon (`_point_sample_bids` in
  [`apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/entry_watch.py`](../alphalens-pipeline/alphalens_pipeline/brokers/automanager/entry_watch.py),
  which reads no other field of a `PricePoint`) and trade-referenced in the replay (`_reachable`
  in [`intent_replay/walk.py`](intent_replay/walk.py), which compares a bar's LOW). On VCYT the
  bid reached the 44.01 rung on 2026-10-06 and no trade of that session did, so the replay armed
  one session later and 0.63 cheaper, which put the 0.5R trail trigger under the path's peak.
  The gap RE-TIMES arming and never creates or destroys a fill: over all 11 journaled LIVE
  touches the tape reached the rung somewhere inside the entry window 11 times out of 11.
  Do NOT close this with a configuration key, a seeded touch or a spread tolerance — the replay
  answers to the MARKET, so the difference is REPORTED. The rule is §2.1 of
  [`docs/superpowers/specs/2026-09-23-intent-replay-design.md`](../../docs/superpowers/specs/2026-09-23-intent-replay-design.md):
  "A knob cannot supply a missing fact; it can only hide that the fact is missing."
- Never claim the replay reproduces the LIVE record exactly, and apply no single-signed correction
  to an R figure — the timing gap is one-directional, the money gap is not, and the outcome word
  itself can disagree.
