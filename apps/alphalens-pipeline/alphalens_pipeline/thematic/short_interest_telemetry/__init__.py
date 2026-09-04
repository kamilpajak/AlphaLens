"""Display-only short-interest telemetry stamped at the thematic score stage.

Owner decision: docs/research/event_sourced_lane_design_2026_09_03.md sec. 13
row 11 (#1269). Forward-only FINRA short interest via the Polygon domain
wrapper; never touches selection, ordering, or the brief sort. First
statistical look is deferred to N >= 30 matured episodes (~2026-11/12) and
pays its EDGE-cluster ledger charge then, not now.
"""
