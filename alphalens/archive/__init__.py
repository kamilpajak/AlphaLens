"""ADR 0005 anti-pattern catalog — closed Layer 2 strategies retained for postmortem reference.

Each subpackage here is CLOSED or ARCHIVED. They are kept as physical evidence of
the paradigm-failure history (10/10 phase-robust failures across 3 architectural
layers) so that future research can grep `__closed_evidence__` paths and read
the original code alongside the postmortem rather than reconstructing from git
history.

Nothing in `alphalens.archive.*` should be imported by ACTIVE or RESEARCH_ONLY
code. The lifecycle status of each subpackage continues to live in its own
`__init__.py` as `__status__`. See `tests/test_layer_status.py` for the
enforcement whitelist.
"""
