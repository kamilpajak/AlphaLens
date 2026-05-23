import tempfile
import unittest
from pathlib import Path


class TestPortfolioState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "portfolio.yaml"

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_state_when_file_missing(self):
        from alphalens_pipeline.edgar_detector.portfolio import PortfolioState

        state = PortfolioState.load(self.path)
        self.assertEqual(state.held, [])
        self.assertEqual(state.watchlist, [])

    def test_load_from_yaml_roundtrip(self):
        from alphalens_pipeline.edgar_detector.portfolio import PortfolioState

        self.path.write_text("held:\n  - AAPL\n  - MSFT\nwatchlist:\n  - GOOG\n")
        state = PortfolioState.load(self.path)
        self.assertEqual(state.held, ["AAPL", "MSFT"])
        self.assertEqual(state.watchlist, ["GOOG"])

    def test_save_creates_file_with_correct_structure(self):
        import yaml
        from alphalens_pipeline.edgar_detector.portfolio import PortfolioState

        state = PortfolioState(held=["NVDA"], watchlist=["AMD", "INTC"])
        state.save(self.path)

        loaded = yaml.safe_load(self.path.read_text())
        self.assertEqual(loaded["held"], ["NVDA"])
        self.assertEqual(loaded["watchlist"], ["AMD", "INTC"])

    def test_relevance_for_held_ticker(self):
        from alphalens_pipeline.edgar_detector.portfolio import PortfolioState, Relevance

        state = PortfolioState(held=["AAPL"], watchlist=["GOOG"])
        self.assertEqual(state.relevance_for("AAPL"), Relevance.HELD)

    def test_relevance_for_watchlist_ticker(self):
        from alphalens_pipeline.edgar_detector.portfolio import PortfolioState, Relevance

        state = PortfolioState(held=["AAPL"], watchlist=["GOOG"])
        self.assertEqual(state.relevance_for("GOOG"), Relevance.WATCHLIST)

    def test_relevance_for_unknown_ticker(self):
        from alphalens_pipeline.edgar_detector.portfolio import PortfolioState, Relevance

        state = PortfolioState(held=["AAPL"], watchlist=["GOOG"])
        self.assertEqual(state.relevance_for("TSLA"), Relevance.FOREIGN)

    def test_default_path_is_in_alphalens_home(self):
        from alphalens_pipeline.edgar_detector.portfolio import default_portfolio_path

        expected = Path.home() / ".alphalens" / "watchdog" / "portfolio.yaml"
        self.assertEqual(default_portfolio_path(), expected)


if __name__ == "__main__":
    unittest.main()
