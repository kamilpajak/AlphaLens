"""The cash book spells a null the way the keeper does (#1689, memo Phase 4).

Dependency rule 2 forbids ``alphalens_pipeline.account_cash`` from importing
``alphalens_pipeline.brokers.automanager``: this layer's semantics stay in
AlphaLens while the keeper travels with the Stage-1 split. So the two cannot
SHARE the null-reason strings, and a test is the only thing that can keep the
two vocabularies from drifting once they are declared twice. The shape is
``test_env_vocabulary_parity.py``'s, one module over, for the same reason.

Why it matters more than tidiness: ``alphalens broker trades`` and the cash
book will both publish a null money field on the same screen. Two spellings of
one fact -- ``not_closed`` beside ``not-closed`` -- read to a machine as two
different facts, and the owner has to learn which command says which.
"""

from __future__ import annotations

import ast
import inspect
import unittest
from dataclasses import fields

from alphalens_pipeline.account_cash import buckets, episodes, money
from alphalens_pipeline.brokers.account_cash import SIM_REPORTS_UNUSABLE
from alphalens_pipeline.brokers.automanager import trades

#: The pure derivation. ``paths``, ``codec``, ``store`` and ``sweep`` are the
#: store half and are covered by their own tests.
PURE_MODULES = (money, buckets, episodes)


class TheMoneyRecordKeepsTheKeepersDiscipline(unittest.TestCase):
    def test_the_money_record_keeps_the_keepers_null_reason_discipline(self) -> None:
        """Dropping ``null_reason`` lets a null total publish with no reason,
        which is the one thing the memo's ``oneOf`` exists to stop."""
        self.assertEqual(
            tuple(field.name for field in fields(money.AcctAmount)),
            ("value", "currency", "rows", "null_reason"),
        )

    def test_the_shared_null_reasons_are_spelled_the_keepers_way(self) -> None:
        shared = (
            (money.NULL_NOT_CLOSED, trades.NULL_NOT_CLOSED),
            (money.NULL_NON_FINITE, trades.NULL_NON_FINITE),
            (money.NULL_ACCT_AMOUNT_NOT_REPORTED, trades.NULL_ACCT_AMOUNT_NOT_REPORTED),
            (money.NULL_SIM_REPORTS_UNUSABLE, trades.NULL_SIM_REPORTS),
            (money.NULL_SIM_REPORTS_UNUSABLE, SIM_REPORTS_UNUSABLE),
        )
        self.assertGreaterEqual(len(shared), 4)
        for mine, theirs in shared:
            with self.subTest(reason=theirs):
                self.assertEqual(mine, theirs)

    def test_every_shared_reason_is_registered_on_this_side_too(self) -> None:
        for reason in (
            money.NULL_NOT_CLOSED,
            money.NULL_NON_FINITE,
            money.NULL_ACCT_AMOUNT_NOT_REPORTED,
            money.NULL_SIM_REPORTS_UNUSABLE,
        ):
            with self.subTest(reason=reason):
                self.assertIn(reason, money.NULL_REASONS)
                self.assertIn(reason, trades.NULL_REASONS)


class ThePureModulesStayOnTheirSideOfTheSplit(unittest.TestCase):
    def test_the_pure_modules_do_not_import_the_keeper(self) -> None:
        """Dependency rule 2 states this over the whole package.

        Here it is stated at the three modules that are tempted: ``Measured``,
        ``NET_FEE_TOLERANCE_ACCT`` and ``FEES_NOT_INCLUDED`` all live on the
        keeper's side and all look reusable. A lazy in-body import is the shape
        the rule's AST walk is written to catch; this says it at the site.
        """
        for module in PURE_MODULES:
            with self.subTest(module=module.__name__):
                imported: list[str] = []
                for node in ast.walk(ast.parse(inspect.getsource(module))):
                    if isinstance(node, ast.Import):
                        imported.extend(alias.name for alias in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        imported.append(node.module)
                self.assertEqual(
                    [name for name in imported if "automanager" in name],
                    [],
                    f"{module.__name__} must stay off the keeper's side, imports: {imported}",
                )

    def test_the_pure_modules_resolve_no_home_of_their_own(self) -> None:
        """They take rows, so their tests need no isolated ``~/.alphalens``.

        A module here that grew a path builder would also have to join
        ``home_isolation._GUARDED_BUILDERS``, and the gate that reads that set
        off the source would go red first.
        """
        for module in PURE_MODULES:
            with self.subTest(module=module.__name__):
                source = inspect.getsource(module)
                self.assertNotIn("Path.home", source)
                self.assertNotIn("_alphalens_home", source)
                self.assertNotIn("account_cash.paths", source)
                self.assertNotIn("import os", source)


if __name__ == "__main__":
    unittest.main()
