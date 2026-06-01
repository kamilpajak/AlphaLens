"""Pipeline-side feedback analytics — consumes the shared ledger.

The dependency-free CORE of the feedback ledger (the SQLite ``Decision``
store + VIX ``regime``) was extracted to the standalone ``alphalens-feedback``
workspace package so the slim Django image can read/write the shared
``~/.alphalens/feedback.db`` without the heavy pipeline dependency tree
(prod incident 2026-06-01 — the Django image failed to build because it
imported the store but could not install ``alphalens_pipeline``). Import the
store/regime from ``alphalens_feedback`` now, NOT from here.

What stays here (pipeline-coupled, NOT needed by Django):
- ``outcome_join`` — joins ``Decision`` rows to paper-trade ledger fills.
- ``shadow_return`` — arrival-price counterfactual return from Polygon bars.
- ``execution_modes`` — Perold break-even LIMIT-vs-MARKET classification.

These import ``alphalens_pipeline.paper`` (ledger + calendar) and the shared
store from ``alphalens_feedback`` — which is exactly why they cannot live in
the leaf ``alphalens-feedback`` package (that would re-introduce the heavy
dependency into the Django image).
"""

__status__ = "ACTIVE"
