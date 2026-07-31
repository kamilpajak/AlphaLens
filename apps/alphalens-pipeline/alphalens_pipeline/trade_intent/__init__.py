"""Boundary-2 wire schema (client -> broker-manager).

Pure value types formalizing today's armed-pick + brief-setup dict that
crosses from the client (the ``/edge`` what-if UI / CLI arming flow) into the
broker-manager. Nothing in the codebase constructs or consumes these types
yet: this package is a pure, unconsumed leaf so that the future
``broker_contract`` extraction (see the broker-manager extraction design
memo, ``docs/research/broker_manager_extraction_and_exit_geometry_2026_07_31.md``)
can relocate them unchanged instead of inventing the shape at extraction
time.
"""

__status__ = "ACTIVE"
