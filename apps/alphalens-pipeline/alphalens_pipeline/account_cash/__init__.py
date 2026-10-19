"""The account cash book: what the account's money actually did (#1689).

Memo: ``docs/research/account_cashbook_design_2026_10_05.md``. This package is
memo Phase 3 — the STORE half: where the rows live, how they render and decode,
how a sweep folds what came back against what is already on disk.

Three facts about this package that a change must not break.

**The read flows one way.** The cash book reads the broker through
``brokers.account_cash.SupportsAccountCash``; nothing under ``brokers/`` may
reach back in here (dependency rule 1), and nothing here may import
``brokers.automanager`` (rule 2) — the keeper's state seam travels with the
Stage-1 split while this layer's semantics stay in AlphaLens. ``brokers.journal``
and ``brokers.account_cash`` are both outside ``automanager`` and are imported.

**On SIM the store refuses and writes nothing.** The SIM report endpoints answer
with canned rows belonging to OTHER accounts, so storing them would be storing
fiction in a money store, which memo section 3 calls worse than storing nothing.
The refusal lives on the WRITE side, over every appender, never on a nullable
field a caller passes in.

**This journal is never compacted.** The restatement history is the only thing
that could ever detect a corrected row, and a restatement history that gets
compacted is not one. ``tests/account_cash/test_no_compactor_reaches_the_cash_book.py``
holds that structurally rather than as a sentence.
"""

from __future__ import annotations

__all__ = ["codec", "paths", "store", "sweep"]
