"""sector_peers is now a thin alias over alphalens.data.fundamentals.sic_index.

The substantive contract tests live in ``tests/test_sic_index.py``. This
suite only verifies that the legacy public names still point at the SIC
implementation so ``scorer.py`` and external scripts that import the old
symbols keep working without touching them.
"""

from __future__ import annotations

import unittest

from alphalens.data.fundamentals import sic_index
from alphalens.thematic.screening import sector_peers


class TestSectorPeersIsAliasForSicIndex(unittest.TestCase):
    def test_get_industry_id_is_get_sic(self) -> None:
        self.assertIs(sector_peers.get_industry_id, sic_index.get_sic)

    def test_iter_industry_peers_is_iter_sic_peers(self) -> None:
        self.assertIs(sector_peers.iter_industry_peers, sic_index.iter_sic_peers)

    def test_industry_label_is_sic_label(self) -> None:
        self.assertIs(sector_peers.industry_label, sic_index.sic_label)

    def test_module_does_not_re_expose_simfin_cache_dir(self) -> None:
        # SIMFIN_CACHE_DIR was the only reason this module ever needed
        # filesystem state. Confirm it is gone so an accidental
        # `from sector_peers import SIMFIN_CACHE_DIR` fails fast.
        self.assertFalse(hasattr(sector_peers, "SIMFIN_CACHE_DIR"))


if __name__ == "__main__":
    unittest.main()
