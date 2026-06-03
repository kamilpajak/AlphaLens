"""Pipeline-side feedback analytics — consumes the shared ledger.

The dependency-free CORE of the feedback ledger (the SQLite ``Decision``
store + VIX ``regime``) was extracted to the standalone ``alphalens-feedback``
workspace package so the slim Django image can read/write the shared
``~/.alphalens/feedback.db`` without the heavy pipeline dependency tree
(prod incident 2026-06-01 — the Django image failed to build because it
imported the store but could not install ``alphalens_pipeline``). Import the
store/regime from ``alphalens_feedback`` now, NOT from here.

What stays here (pipeline-coupled, NOT needed by Django) — the broker-free
feedback analytics:
- ``ladder_replay`` — the pure price-path replay engine for the 3E/3TP/1SL
  ladder.
- ``ladder_backfill`` — the nightly driver that replays each matured decision's
  ladder over Polygon bars and stamps the outcome columns.
- ``population_ladder_monitor`` — the all-candidate full-hold replay (TP / SL /
  time-stop) over the briefs.
- ``bar_window`` — the shared VWAP-anchor / Polygon bar-fetch primitives the
  replay engines consume.

The legacy paper-ledger-coupled metrics (``outcome_join``, ``shadow_return``,
``execution_modes``, ``execution_telemetry``) were removed with the broker chain
(ADR 0012); feedback measurement is now fully broker-free price-path replay.
"""

__status__ = "ACTIVE"
