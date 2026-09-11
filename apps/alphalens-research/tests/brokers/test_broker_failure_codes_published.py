"""Every registered failure code appears in the published table, owned correctly.

The table in `apps/alphalens-broker-contract/README.md` is what a third-party
consumer reads to learn which codes exist and which are safe to re-run. Nothing
kept it in step with the two registries, so a code added to
`_CLI_FAILURE_CODES` would be emitted by the CLI while the published catalogue
denied its existence — the exact failure mode #1405 hit when a README sentence
described a gate that did not exist.

Three directions, because one is not enough:

* a registered code missing from the table is an unpublished code;
* a table row naming no registered code is a stale promise;
* an `owner` column disagreeing with the registry that defines it points a
  client at the wrong half of the split (`test_broker_contract_has_no_cli_codes`
  guards the split itself).
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from alphalens_cli.commands.broker import _CLI_FAILURE_CODES
from broker_contract.failure import CONTRACT_FAILURE_CODES

REPO_ROOT = Path(__file__).resolve().parents[4]
README = REPO_ROOT / "apps" / "alphalens-broker-contract" / "README.md"

# `| `code` | owner | ... ` — the owner column is the second cell.
_ROW_RE = re.compile(r"^\|\s*`([a-z_]+)`\s*\|\s*(contract|CLI)\s*\|", re.MULTILINE)


def published_rows() -> dict[str, str]:
    return dict(_ROW_RE.findall(README.read_text(encoding="utf-8")))


class TheTableAndTheRegistriesAgree(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = published_rows()
        self.owners = {
            **dict.fromkeys(CONTRACT_FAILURE_CODES, "contract"),
            **dict.fromkeys(_CLI_FAILURE_CODES, "CLI"),
        }

    def test_the_table_was_parsed_at_all(self) -> None:
        """Positive control: a regex that silently matched nothing would make
        every assertion below vacuously true."""
        self.assertGreaterEqual(len(self.rows), len(self.owners))

    def test_every_registered_code_is_published(self) -> None:
        missing = sorted(set(self.owners) - set(self.rows))
        self.assertEqual(
            missing,
            [],
            f"registered but absent from {README.name} — a client cannot branch on "
            "a code it is never told about",
        )

    def test_every_published_row_names_a_registered_code(self) -> None:
        stale = sorted(set(self.rows) - set(self.owners))
        self.assertEqual(
            stale,
            [],
            f"named in {README.name} but defined nowhere — the row promises a code "
            "that can never be emitted",
        )

    def test_each_row_names_the_half_that_defines_it(self) -> None:
        for code, owner in sorted(self.rows.items()):
            with self.subTest(code=code):
                self.assertEqual(owner, self.owners.get(code), code)


if __name__ == "__main__":
    unittest.main()
