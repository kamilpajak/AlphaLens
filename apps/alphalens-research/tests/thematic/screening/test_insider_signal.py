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


class TestScoreInsider(unittest.TestCase):
    def test_returns_none_score_when_ticker_has_no_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_partition(
                root, 2026, [_record("ins1", dt.date(2026, 5, 1), "P", 100, 10, "OTHER")]
            )
            out = insider_signal.score_insider(
                ticker="BEEM",
                asof=dt.date(2026, 5, 15),
                peers=["BEEM", "OTHER"],
                form4_root=root,
            )
        self.assertIsNone(out["score_usd"])
        self.assertIsNone(out["sector_percentile"])

    def test_percentile_rank_with_peers(self):
        # 3 peers with scalar scores [100, 200, 300]; the candidate's 300
        # should be at the top percentile (100.0) under "fraction with
        # value ≤ candidate".
        with patch.object(
            insider_signal,
            "compute_net_opportunistic_usd",
            side_effect=lambda *, ticker, asof, lookback_days=90, form4_root=None: {
                "A": 100.0,
                "B": 200.0,
                "C": 300.0,
            }.get(ticker),
        ):
            out = insider_signal.score_insider(
                ticker="C", asof=dt.date(2026, 5, 15), peers=["A", "B", "C"]
            )
        self.assertEqual(out["score_usd"], 300.0)
        # Candidate at top of cohort -> 100th percentile.
        self.assertAlmostEqual(out["sector_percentile"], 100.0, places=2)

    def test_shell_peer_excluded_via_feature_fetcher(self):
        """Issue #197: with ``feature_fetcher`` wired in, peers below the
        mcap/price floor must not anchor the cohort."""
        scores = {"CAND": 100.0, "BIG": 200.0, "SHELL": 50_000_000.0}
        features = {
            "CAND": {"price": 50.0, "shares_outstanding": 100_000_000.0},
            "BIG": {"price": 50.0, "shares_outstanding": 100_000_000.0},
            "SHELL": {"price": 1.0, "shares_outstanding": 10_000.0},
        }
        with patch.object(
            insider_signal,
            "compute_net_opportunistic_usd",
            side_effect=lambda *, ticker, asof, lookback_days=90, form4_root=None: scores.get(
                ticker
            ),
        ):
            out = insider_signal.score_insider(
                ticker="CAND",
                asof=dt.date(2026, 5, 15),
                peers=["CAND", "BIG", "SHELL"],
                feature_fetcher=lambda t, _a: features.get(t),
            )
        # SHELL's massive (synthetic) score must not warp percentile —
        # filter drops it; cohort = {CAND, BIG}, CAND below BIG → 50.0.
        self.assertEqual(out["score_usd"], 100.0)
        self.assertAlmostEqual(out["sector_percentile"], 50.0, places=2)

    def test_percentile_skips_peers_with_no_data(self):
        with patch.object(
            insider_signal,
            "compute_net_opportunistic_usd",
            side_effect=lambda *, ticker, asof, lookback_days=90, form4_root=None: {
                "A": 100.0,
                "B": None,
                "C": 50.0,
            }.get(ticker),
        ):
            out = insider_signal.score_insider(
                ticker="C", asof=dt.date(2026, 5, 15), peers=["A", "B", "C"]
            )
        # Only A (100) and C (50) participate; C at bottom -> 50.0 percentile
        # ("≤ candidate" => candidate itself + smaller peers / total).
        self.assertEqual(out["score_usd"], 50.0)
        self.assertAlmostEqual(out["sector_percentile"], 50.0, places=2)


if __name__ == "__main__":
    unittest.main()
