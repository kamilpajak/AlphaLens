"""Shared feedback-ledger primitives — the dependency-free core.

This package holds the two pieces of the feedback ledger that BOTH the
pipeline and the Django API must touch, and that are pure stdlib so the slim
Django image can import them without the heavy pipeline dependency tree:

- ``store`` — the SQLite ``Decision`` ledger (``~/.alphalens/feedback.db``):
  schema, migrations, the ``Decision`` dataclass + validation, the action
  enum + dismiss taxonomy, and the read/write ``FeedbackStore`` API.
- ``regime`` — VIX-cache read + ``classify_vix`` regime labelling stamped on
  each decision at write time.

Why a standalone workspace package (not ``alphalens_pipeline.feedback``):
    Django is a full read/write client of the SHARED ``feedback.db`` file
    (POST creates + records a ``Decision``; GET lists/reads). The store is
    therefore the single schema owner of a shared file. Keeping it inside
    ``alphalens-pipeline`` forced the slim Django image to either install the
    whole pipeline (heavy) or go without (the image silently failed to build
    — collectstatic ImportError, prod incident 2026-06-01). Extracting the
    pure-stdlib core into this leaf package gives ONE schema owner that both
    sides depend on with zero heavy deps and no boundary violation.

    The pipeline-coupled feedback ANALYTICS (the broker-free ladder + population
    replay engines and their shared bar-window primitives) stay in
    ``alphalens_pipeline.feedback`` — they pull in the heavy pipeline deps and
    are never needed by Django.

Single SQLite file, user-authored, NOT regenerable from parquet — same
lifecycle as ``candidates.db`` / ``paper_ledger.db``; lives on the
``~/.alphalens`` host volume so it survives Docker rebuilds + git ops.
Django opens it as a secondary database (``DATABASES['feedback']``) routed
via a DB router; pipeline + Django share the same on-disk file through the
volume mount. Design memo: ``docs/research/feedback_ledger_design_2026_05_29.md``.
"""
