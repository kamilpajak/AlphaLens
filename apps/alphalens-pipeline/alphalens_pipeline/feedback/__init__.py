"""User feedback ledger — explicit accept/dismiss decisions on briefed candidates.

Single SQLite file at ``~/.alphalens/feedback.db``, same lifecycle as the
Layer 1 candidate queue (``candidates.db``) and the paper-trade ledger
(``paper_ledger.db``): user-authored, NOT regenerable from parquet, lives
on host disk so it survives Docker rebuilds + git ops.

5-action enum (``interested`` / ``watching`` / ``dismissed`` /
``paper_traded`` / ``live_traded``) plus a 2-level dismiss taxonomy
(4 high-level categories × 3 specific reasons + ``other``) per the locked
design memo at ``docs/research/feedback_ledger_design_2026_05_29.md``.

Why feedback ledger:
    Without explicit dismiss / interested data the model has no signal on
    which surfaced candidates were worth showing. L3 weekly review,
    per-signal-combo win-rate, and the eventual learned re-weighting of
    ``layer4_weighted_score`` all consume this ledger. Paper-trade ledger
    is a separate stream that only captures explicit planning; ``decisions``
    here covers the much larger surface of "saw the candidate, made a call".

Storage choice — SQLite over Postgres:
    Feedback is user-authored and NOT regenerable. Briefs cache (Postgres)
    is regenerable from parquet; mixing the two needs separate backup
    discipline. SQLite at ``~/.alphalens/feedback.db`` keeps the
    user-authored data on the same host-volume backup path as the other
    permanent ledgers.

Django + pipeline access:
    Django opens this DB as a secondary database (``DATABASES['feedback']``
    in ``apps/alphalens-django/config/settings.py``) and routes the
    ``Decision`` model to it via a DB router. Pipeline-side helpers
    (this package) and Django share the same on-disk file via the
    ``~/.alphalens`` Docker volume mount.
"""

__status__ = "ACTIVE"
