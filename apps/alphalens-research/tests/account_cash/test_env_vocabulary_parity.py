"""The cash book spells its environments the way the keeper does (#1689).

Dependency rule 2 forbids ``alphalens_pipeline.account_cash`` from importing
``alphalens_pipeline.brokers.automanager``, so the two modules cannot SHARE the
two strings: this layer's semantics stay in AlphaLens while ``state_paths``
travels with the Stage-1 keeper split. A test is therefore the only thing that
can keep the vocabularies from drifting once they are declared twice.

The name says *read* deliberately: this is a claim about the values
``paths.validate_environment`` accepts, never permission to WRITE a SIM root --
``test_store.py`` owns that refusal.
"""

from __future__ import annotations

import ast
import inspect
import unittest

from alphalens_pipeline.account_cash import paths
from alphalens_pipeline.brokers.automanager import state_paths


class TheEnvironmentVocabularyMatchesTheKeepers(unittest.TestCase):
    def test_the_read_vocabulary_matches_the_keepers_spelling(self) -> None:
        self.assertEqual(paths.ENVIRONMENTS, (state_paths.ENV_SIM, state_paths.ENV_LIVE))

    def test_each_constant_matches_one_for_one(self) -> None:
        self.assertEqual(paths.ENV_SIM, state_paths.ENV_SIM)
        self.assertEqual(paths.ENV_LIVE, state_paths.ENV_LIVE)

    def test_the_cash_book_does_not_import_the_keepers_state_seam(self) -> None:
        """Dependency rule 2 states this over the whole package; here it is
        stated at the one module that would be tempted to reuse the strings.

        An AST walk, not a substring search: the module docstring NAMES
        ``state_paths`` on purpose, to say why the path is declared here and
        not there, and a text match cannot tell a prose reference from an
        import.
        """
        tree = ast.parse(inspect.getsource(paths))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        self.assertEqual(
            [name for name in imported if name.startswith("alphalens_pipeline")],
            [],
            f"paths.py must stay free of the keeper's state seam, imports: {imported}",
        )


if __name__ == "__main__":
    unittest.main()
