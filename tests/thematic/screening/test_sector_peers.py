import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alphalens.thematic.screening import sector_peers

_COMPANIES_CSV = """Ticker;SimFinId;Company Name;IndustryId;ISIN;End of financial year (month);Number Employees;Business Summary;Market;CIK;Main Currency
QUBT;100;Quantum Computing, Inc.;101001;US123;12;100;quantum computing;us;1758009;USD
IONQ;101;IonQ, Inc.;101001;US124;12;200;quantum;us;1824920;USD
RGTI;102;Rigetti Computing, Inc.;101001;US125;12;150;quantum;us;1838359;USD
AAPL;200;Apple Inc.;102002;US126;9;100000;consumer electronics;us;320193;USD
"""

_INDUSTRIES_CSV = """IndustryId;Industry;Sector
101001;Quantum Computing Software;Technology
102002;Consumer Electronics;Technology
"""


def _write_csvs(dir_path: Path) -> None:
    (dir_path / "us-companies.csv").write_text(_COMPANIES_CSV)
    (dir_path / "industries.csv").write_text(_INDUSTRIES_CSV)


class TestSectorPeers(unittest.TestCase):
    def setUp(self):
        sector_peers._load_companies.cache_clear()
        sector_peers._load_industries.cache_clear()

    def tearDown(self):
        sector_peers._load_companies.cache_clear()
        sector_peers._load_industries.cache_clear()

    def test_get_industry_id_returns_int(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_csvs(Path(tmp))
            with patch.object(sector_peers, "SIMFIN_CACHE_DIR", Path(tmp)):
                self.assertEqual(sector_peers.get_industry_id("QUBT"), 101001)
                self.assertEqual(sector_peers.get_industry_id("AAPL"), 102002)

    def test_get_industry_id_case_insensitive(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_csvs(Path(tmp))
            with patch.object(sector_peers, "SIMFIN_CACHE_DIR", Path(tmp)):
                self.assertEqual(sector_peers.get_industry_id("qubt"), 101001)

    def test_get_industry_id_returns_none_for_unknown_ticker(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_csvs(Path(tmp))
            with patch.object(sector_peers, "SIMFIN_CACHE_DIR", Path(tmp)):
                self.assertIsNone(sector_peers.get_industry_id("XYZ_UNKNOWN"))

    def test_iter_industry_peers_returns_all_tickers_in_industry(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_csvs(Path(tmp))
            with patch.object(sector_peers, "SIMFIN_CACHE_DIR", Path(tmp)):
                peers = sector_peers.iter_industry_peers(101001)
                self.assertEqual(set(peers), {"QUBT", "IONQ", "RGTI"})

    def test_iter_industry_peers_returns_empty_for_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_csvs(Path(tmp))
            with patch.object(sector_peers, "SIMFIN_CACHE_DIR", Path(tmp)):
                self.assertEqual(sector_peers.iter_industry_peers(999999), [])

    def test_industry_label_returns_industry_and_sector(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_csvs(Path(tmp))
            with patch.object(sector_peers, "SIMFIN_CACHE_DIR", Path(tmp)):
                label = sector_peers.industry_label(101001)
                self.assertEqual(label, ("Quantum Computing Software", "Technology"))

    def test_industry_label_returns_none_for_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_csvs(Path(tmp))
            with patch.object(sector_peers, "SIMFIN_CACHE_DIR", Path(tmp)):
                self.assertEqual(sector_peers.industry_label(999999), (None, None))

    def test_companies_csv_loaded_once_via_lru_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_csvs(Path(tmp))
            with patch.object(sector_peers, "SIMFIN_CACHE_DIR", Path(tmp)):
                sector_peers.get_industry_id("QUBT")
                sector_peers.get_industry_id("IONQ")
                sector_peers.iter_industry_peers(101001)
            # _load_companies is LRU-cached, so a single hit only
            info = sector_peers._load_companies.cache_info()
            self.assertEqual(info.misses, 1)
            self.assertGreaterEqual(info.hits, 2)


if __name__ == "__main__":
    unittest.main()
