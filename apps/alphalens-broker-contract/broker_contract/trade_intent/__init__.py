"""Boundary-2 wire schema (client -> broker-manager).

Pure value types formalizing today's armed-pick + brief-setup dict that
crosses from the client (the ``/edge`` what-if UI / CLI arming flow) into the
broker-manager. Relocated into the shared ``broker_contract`` package (step
2A-2 of the broker-manager extraction design memo,
``docs/research/broker_manager_extraction_and_exit_geometry_2026_07_31.md``
§2.1) alongside the ``DEFAULT_ORDER_TTL_DAYS`` constant its ``TradeSpec``
default depends on — the extraction this package anticipated is now
realized.
"""

__status__ = "ACTIVE"
