"""Pipeline-side feedback analytics — the broker-free population replay.

The VIX ``regime`` helper lives in the standalone ``alphalens-feedback``
workspace package (pure stdlib, importable by the slim Django image). Import it
from ``alphalens_feedback.regime``, NOT from here.

What stays here (pipeline-coupled, NOT needed by Django) — the broker-free
feedback analytics:
- ``ladder_replay`` — the pure price-path replay engine for the 3E/3TP/1SL
  ladder.
- ``population_ladder_monitor`` — the all-candidate full-hold replay (TP / SL /
  time-stop) over the briefs. This is the SOLE feedback signal: it reads briefs
  + Polygon only and writes the ``~/.alphalens/population_ladders`` parquets,
  never a decision/click ledger.
- ``bar_window`` — the shared VWAP-anchor / Polygon bar-fetch primitives the
  replay engine consumes.

The legacy paper-ledger-coupled metrics (``outcome_join``, ``shadow_return``,
``execution_modes``, ``execution_telemetry``) were removed with the broker chain
(ADR 0012). The per-decision ``ladder_backfill`` driver + the SQLite ``Decision``
``store`` it read were removed with the Track-A click ledger (#465): once clicks
stopped, no row was ever written to the ``decisions`` table, so the per-decision
replay had no input. Feedback measurement is now fully broker-free,
parquet-only, population-wide price-path replay.
"""

__status__ = "ACTIVE"
