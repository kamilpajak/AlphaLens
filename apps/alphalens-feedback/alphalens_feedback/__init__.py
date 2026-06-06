"""Shared feedback primitive — the dependency-free VIX-regime helper.

This package holds the one piece BOTH the pipeline and the Django API must
touch, and that is pure stdlib so the slim Django image can import it without
the heavy pipeline dependency tree:

- ``regime`` — VIX-cache read + ``classify_vix`` regime labelling.

History — the removed ``store``:
    This package used to also hold ``store`` — the SQLite ``Decision`` ledger
    (``~/.alphalens/feedback.db``) backing the Track-A user-action click feature
    (the Django POST/GET feedback API). That feature was removed (#465): no UI,
    no Django ``feedback`` app, no writer. With no row ever written to the
    ``decisions`` table, the whole store subsystem (the ``Decision`` dataclass,
    the action enum + dismiss taxonomy, the schema + additive migration chain,
    and the read/write ``FeedbackStore`` API) became dead and was removed. The
    sole feedback signal is now the broker-free, parquet-only population ladder
    monitor in ``alphalens_pipeline.feedback`` — it never touches a decision
    ledger. The old ``~/.alphalens/feedback.db`` file (if present on a host) is
    simply orphaned: nothing opens it, so it is harmless to leave or delete.

Why this stays a standalone workspace package (not ``alphalens_pipeline.feedback``):
    Django still imports ``regime`` for the VIX-regime constant, and it must do
    so without installing the heavy pipeline tree (keeping it inside
    ``alphalens-pipeline`` previously broke the slim Django image build —
    collectstatic ImportError, prod incident 2026-06-01). The pure-stdlib
    ``regime`` leaf keeps that boundary clean. The pipeline-coupled feedback
    ANALYTICS (the broker-free population replay engine + its bar-window
    primitives) stay in ``alphalens_pipeline.feedback`` — they pull in the heavy
    pipeline deps and are never needed by Django.
"""
