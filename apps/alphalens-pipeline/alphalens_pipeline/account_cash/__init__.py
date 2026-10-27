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
``episodes``, ``balance_check``, ``coverage``, ``refusals`` and ``record`` take
rows, records or an environment NAME, open no file and never resolve a home, so
the answer they compute is a function of what was handed in and nothing else --
the #1714 shape, where the replay engine states every run value and reads no
deployment state. ``report`` is the ONE module that calls a reader, and
therefore the only one that knows an ``env`` is a store on disk: it composes the
pure ones. That is also what keeps the home-isolation guard's set of builders at
one (``paths._alphalens_home``) -- a second module resolving a root would be red
in ``tests/brokers/automanager/test_home_isolation_gate.py`` first.

The modules are ``paths`` (where the journals are), ``codec`` (the wire form),
``store`` (the appends and the folds), ``sweep`` (plan then apply), ``money``
(the summing vocabulary), ``buckets`` (every booking row in one bucket),
``episodes`` (flat to flat per instrument), ``balance_check`` (does the cash
book account for the cash), ``coverage`` (which days the store can speak for),
``refusals`` (which windows get no answer at all), ``record`` (what one answer
publishes and what each line rests on) and ``report`` (the composition that
reads the store and fills the record). The
DAG is ``paths -> codec -> store -> sweep`` on the store side, ``money ->
{buckets, episodes, balance_check}`` plus ``store -> {coverage, refusals,
balance_check}``, ``coverage -> refusals`` and
``{coverage, buckets, balance_check, money} -> record`` on the derivation side,
and ``report``
above both. **Three edges cross into ``store``, and none of them is a read.**
``coverage``, ``refusals`` and ``balance_check`` all call
``store.sweep_window``, the ONE parse of a stored window's two free-text
bounds: the three answer, in order, whether a window exists at all, whether
any asked window overlaps the one being reported, and which interval to SUM,
so they have to answer the same thing about the same record -- held by
``tests/account_cash/test_store.py::TheSweepWindowIsParsedInExactlyOnePlace``,
after two of them held verbatim copies that would drift on the day the rule
changes. ``balance_check`` imports one thing more, ``store.BalanceAt``, the
record that pairs a balance snapshot with the sweep whose window produced it;
it is a TYPE and the alternative would be declaring the pairing record twice.
A parse of an already-decoded record and a dataclass open no file, so all
three still take rows or records and resolve no home. Two of them know no
``env`` either; :mod:`refusals` takes one as a NAME -- the first parameter of
both its public functions, validated against the vocabulary and named in the
refusal it raises -- which is a string it checks, never a store it opens.
They are deliberately NOT re-exported here: nothing imports the
package for its own sake, and a re-export would make every reader of one
module pay for all of them.
"""
