import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from alphalens_pipeline.thematic.screening import insider_signal


def _record(
    insider_cik: str, txn_date: dt.date, code: str, shares: float, price: float, ticker: str
):
    return {
        "issuer_cik": "0001000",
        "ticker": ticker,
        "accession_number": f"a-{insider_cik}-{txn_date}",
        "filed_date": txn_date,
        "reporting_owner_cik": insider_cik,
        "reporting_owner_name": f"INSIDER {insider_cik}",
        "transaction_date": txn_date,
        "transaction_code": code,
        "transaction_shares": float(shares),
        "transaction_price_per_share": float(price),
        "is_director": True,
        "is_officer": False,
        "is_ten_percent_owner": False,
        "acquired_disposed": "A" if code == "P" else "D",
        "is_amendment": False,
    }


def _seed_partition(root: Path, year: int, rows: list[dict]) -> None:
    part = root / f"transaction_year={year}"
    part.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(part / "compacted.parquet", index=False)


class TestComputeNetOpportunisticUsd(unittest.TestCase):
    def test_returns_none_when_no_form4_data_for_ticker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Seed another ticker so root exists but BEEM has nothing
            _seed_partition(
                root, 2026, [_record("ins1", dt.date(2026, 5, 1), "P", 100, 10, "OTHER")]
            )
            self.assertIsNone(
                insider_signal.compute_net_opportunistic_usd(
                    ticker="BEEM", asof=dt.date(2026, 5, 15), form4_root=root
                )
            )

    def test_returns_zero_when_window_empty_but_history_present(self):
        # Ticker has 2023 data but window 2026-04-15 .. 2026-05-15 is empty.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_partition(root, 2023, [_record("ins1", dt.date(2023, 3, 5), "P", 10, 1, "BEEM")])
            _seed_partition(root, 2024, [])
            _seed_partition(root, 2025, [])
            _seed_partition(root, 2026, [])
            self.assertEqual(
                insider_signal.compute_net_opportunistic_usd(
                    ticker="BEEM", asof=dt.date(2026, 5, 15), form4_root=root
                ),
                0.0,
            )

    def test_returns_net_usd_with_signed_aggregation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_partition(
                root,
                2023,
                [_record("opp", dt.date(2023, 3, 5), "P", 10, 1, "BEEM")],
            )
            _seed_partition(
                root,
                2024,
                [_record("opp", dt.date(2024, 7, 15), "P", 10, 1, "BEEM")],
            )
            _seed_partition(
                root,
                2025,
                [_record("opp", dt.date(2025, 11, 20), "P", 10, 1, "BEEM")],
            )
            _seed_partition(
                root,
                2026,
                [_record("opp", dt.date(2026, 5, 1), "P", 1000, 50, "BEEM")],  # +50_000
            )
            score = insider_signal.compute_net_opportunistic_usd(
                ticker="BEEM", asof=dt.date(2026, 5, 15), form4_root=root
            )
            self.assertEqual(score, 50_000.0)


class TestComputeOpportunisticBuyUsd(unittest.TestCase):
    def test_buy_only_ignores_sales(self):
        # A +50k purchase and a -100k sale in the window: buy-only must report
        # +50k (NOT the −50k net the old aggregator would produce). Multi-year
        # P history makes the insider classify OPPORTUNISTIC (Cohen-Malloy needs
        # a track record); only the in-window 2026 legs count toward the score.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_partition(root, 2023, [_record("opp", dt.date(2023, 3, 5), "P", 10, 1, "BEEM")])
            _seed_partition(root, 2024, [_record("opp", dt.date(2024, 7, 15), "P", 10, 1, "BEEM")])
            # 2025-06 is outside the 180d window (just classifier history).
            _seed_partition(root, 2025, [_record("opp", dt.date(2025, 6, 1), "P", 10, 1, "BEEM")])
            _seed_partition(
                root,
                2026,
                [
                    _record("opp", dt.date(2026, 5, 1), "P", 1000, 50, "BEEM"),  # +50_000
                    _record("opp", dt.date(2026, 5, 2), "S", 2000, 50, "BEEM"),  # sale, ignored
                ],
            )
            buy = insider_signal.compute_opportunistic_buy_usd(
                ticker="BEEM", asof=dt.date(2026, 5, 15), form4_root=root
            )
            net = insider_signal.compute_net_opportunistic_usd(
                ticker="BEEM", asof=dt.date(2026, 5, 15), form4_root=root
            )
        self.assertEqual(buy, 50_000.0)
        self.assertEqual(net, -50_000.0)

    def test_returns_none_when_no_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_partition(
                root, 2026, [_record("ins1", dt.date(2026, 5, 1), "P", 100, 10, "OTHER")]
            )
            self.assertIsNone(
                insider_signal.compute_opportunistic_buy_usd(
                    ticker="BEEM", asof=dt.date(2026, 5, 15), form4_root=root
                )
            )

    def test_returns_zero_when_only_sales_in_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_partition(
                root, 2026, [_record("opp", dt.date(2026, 5, 1), "S", 1000, 50, "BEEM")]
            )
            self.assertEqual(
                insider_signal.compute_opportunistic_buy_usd(
                    ticker="BEEM", asof=dt.date(2026, 5, 15), form4_root=root
                ),
                0.0,
            )


class TestScoreInsider(unittest.TestCase):
    def _patch_buys(self, mapping: dict[str, float | None]):
        return patch.object(
            insider_signal,
            "compute_opportunistic_buy_usd",
            side_effect=lambda *, ticker, asof, lookback_days=180, form4_root=None: mapping.get(
                ticker
            ),
        )

    def test_always_stamps_signal_version(self):
        with self._patch_buys({"C": 300.0}):
            out = insider_signal.score_insider(ticker="C", asof=dt.date(2026, 5, 15), peers=["C"])
        self.assertEqual(out["signal_version"], insider_signal.INSIDER_SIGNAL_VERSION)

    def test_returns_none_score_when_ticker_has_no_data(self):
        with self._patch_buys({"C": None, "A": 100.0}):
            out = insider_signal.score_insider(
                ticker="C", asof=dt.date(2026, 5, 15), peers=["A", "C"]
            )
        self.assertIsNone(out["score_usd"])
        self.assertIsNone(out["sector_percentile"])
        self.assertEqual(out["signal_version"], insider_signal.INSIDER_SIGNAL_VERSION)

    def test_zero_buy_gets_no_percentile_even_among_selling_peers(self):
        # The v1 pathology: a zero-buy candidate must NOT rank ~100th just
        # because peers sold. Buy-only -> 0.0, within-buyers -> percentile None.
        with self._patch_buys({"A": 0.0, "B": 0.0, "C": 0.0}):
            out = insider_signal.score_insider(
                ticker="C", asof=dt.date(2026, 5, 15), peers=["A", "B", "C"]
            )
        self.assertEqual(out["score_usd"], 0.0)
        self.assertIsNone(out["sector_percentile"])

    def test_lone_buyer_has_no_rank(self):
        # Candidate is the ONLY net buyer in its cohort — there is nothing to
        # rank it against, so the percentile is explicitly absent (NOT the
        # empty-peers 50.0 midpoint, which would read as "median buyer").
        with self._patch_buys({"A": 0.0, "B": None, "C": 120000.0}):
            out = insider_signal.score_insider(
                ticker="C", asof=dt.date(2026, 5, 15), peers=["A", "B", "C"]
            )
        self.assertEqual(out["score_usd"], 120000.0)
        self.assertIsNone(out["sector_percentile"])

    def test_within_buyers_rank(self):
        # Candidate C=300 ranked ONLY among net buyers A=100, B=200, C=300 -> top.
        with self._patch_buys({"A": 100.0, "B": 200.0, "C": 300.0}):
            out = insider_signal.score_insider(
                ticker="C", asof=dt.date(2026, 5, 15), peers=["A", "B", "C"]
            )
        self.assertEqual(out["score_usd"], 300.0)
        pct = out["sector_percentile"]
        assert isinstance(pct, float)
        self.assertAlmostEqual(pct, 100.0, places=2)

    def test_within_buyers_excludes_zero_and_none_peers(self):
        # Candidate C=50 buys; A=100 buys; B=0 (no buying) and D=None (no data)
        # are NOT in the buyer cohort. Cohort = {A=100, C=50} -> C at bottom = 50%ile.
        with self._patch_buys({"A": 100.0, "B": 0.0, "C": 50.0, "D": None}):
            out = insider_signal.score_insider(
                ticker="C", asof=dt.date(2026, 5, 15), peers=["A", "B", "C", "D"]
            )
        self.assertEqual(out["score_usd"], 50.0)
        pct = out["sector_percentile"]
        assert isinstance(pct, float)
        self.assertAlmostEqual(pct, 50.0, places=2)


if __name__ == "__main__":
    unittest.main()
