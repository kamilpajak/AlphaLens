"""sector_peers is now a thin alias over alphalens_pipeline.data.fundamentals.sic_index.

The substantive contract tests live in ``tests/test_sic_index.py``. This
suite verifies the OBSERVABLE behaviour of the legacy public names
(``get_industry_id`` / ``iter_industry_peers`` / ``industry_label``)
through a synthetic SIC-index fixture, so that any future evolution of
the shim (e.g., adding logging, parameter coercion, or deprecation
warnings) does not silently break the contract — testing identity via
``assertIs`` would not survive such a wrapper.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq
from alphalens_pipeline.data.fundamentals import sic_index
from alphalens_pipeline.thematic.screening import sector_peers


class TestSectorPeersLegacyContract(unittest.TestCase):
    """The pre-migration public names continue to honour the same contract
    (returning the int / list / tuple shapes that ``scorer.py`` expects),
    now backed by the SIC index instead of SimFin CSVs.
    """

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        index_path = Path(self._tmp.name) / "sic_index.parquet"
        table = pa.Table.from_pylist(
            [
                {"ticker": "QUBT", "cik": "1", "sic": 3674, "sic_description": "Semiconductors"},
                {"ticker": "IONQ", "cik": "2", "sic": 3674, "sic_description": "Semiconductors"},
                {
                    "ticker": "AAPL",
                    "cik": "3",
                    "sic": 3571,
                    "sic_description": "Electronic Computers",
                },
            ],
            schema=pa.schema(
                [
                    ("ticker", pa.string()),
                    ("cik", pa.string()),
                    ("sic", pa.int32()),
                    ("sic_description", pa.string()),
                ]
            ),
        )
        pq.write_table(table, index_path)
        self._patch = patch.object(sic_index, "_SIC_INDEX_PATH", index_path)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        sic_index._load_index.cache_clear()
        sic_index._load_lookup_dicts.cache_clear()
        self.addCleanup(sic_index._load_index.cache_clear)
        self.addCleanup(sic_index._load_lookup_dicts.cache_clear)

    def test_get_industry_id_returns_sic_for_known_ticker(self) -> None:
        self.assertEqual(sector_peers.get_industry_id("QUBT"), 3674)
        self.assertEqual(sector_peers.get_industry_id("AAPL"), 3571)

    def test_get_industry_id_returns_none_for_unknown_ticker(self) -> None:
        self.assertIsNone(sector_peers.get_industry_id("NVDA"))

    def test_iter_industry_peers_returns_co_sic_tickers(self) -> None:
        peers = sector_peers.iter_industry_peers(3674)
        self.assertEqual(sorted(peers), ["IONQ", "QUBT"])

    def test_industry_label_returns_description_and_division(self) -> None:
        self.assertEqual(
            sector_peers.industry_label(3674),
            ("Semiconductors", "Manufacturing"),
        )

    def test_module_does_not_re_expose_simfin_cache_dir(self) -> None:
        # SIMFIN_CACHE_DIR was the only reason this module ever needed
        # filesystem state. Confirm it is gone so an accidental
        # `from sector_peers import SIMFIN_CACHE_DIR` fails fast.
        self.assertFalse(hasattr(sector_peers, "SIMFIN_CACHE_DIR"))


if __name__ == "__main__":
    unittest.main()
