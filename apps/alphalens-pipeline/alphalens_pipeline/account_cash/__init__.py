"""The account cash book: what the account's money actually did (#1689).

Memo: ``docs/research/account_cashbook_design_2026_10_05.md``. Two halves live
here. Phase 3 is the STORE: where the rows live, how they render and decode, how
a sweep folds what came back against what is already on disk. Phase 4 is the
DERIVATION: what the rows add up to.

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

**The derivation is pure and the store half is not.** ``money``, ``buckets``,
``episodes`` and ``balance_check`` take rows, take no ``env`` and never resolve
a home, so the answer they compute is a function of what was handed in and
nothing else -- the #1714 shape, where the replay engine states every run value
and reads no deployment state. ``report`` is the ONE module that reads the
store, and therefore the only one that knows ``env``: it composes the pure
four. That is also what keeps the home-isolation guard's set of builders at
one (``paths._alphalens_home``) -- a second module resolving a root would be
red in ``tests/brokers/automanager/test_home_isolation_gate.py`` first.

The modules are ``paths`` (where the journals are), ``codec`` (the wire form),
``store`` (the appends and the folds), ``sweep`` (plan then apply), ``money``
(the summing vocabulary), ``buckets`` (every booking row in one bucket),
``episodes`` (flat to flat per instrument), ``balance_check`` (does the cash
book account for the cash) and ``report`` (one window's published answer). The
DAG is ``paths -> codec -> store -> sweep`` on the store side, ``money ->
{buckets, episodes, balance_check}`` on the derivation side, and ``report``
above both. They are deliberately NOT re-exported here: nothing imports the
package for its own sake, and a re-export would make every reader of one
module pay for all of them.
"""
