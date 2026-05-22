"""Tests for :mod:`alphalens_research.paper_trade.universe_loaders` (U1/U2/U3)."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd
import yaml
from alphalens_research.paper_trade import universe_loaders as ul


def _write_inventory(
    path: Path,
    rows: list[dict],
) -> None:
    """Write an inventory parquet matching ``build_ivol_inventory`` schema."""
    df = pd.DataFrame(rows)
    if not df.empty:
        for col in ("first_date", "last_date"):
            if col in df.columns:
                df[col] = pd.to_datetime(df[col]).dt.date
    df.to_parquet(path, index=False)


def _write_smd_close(path: Path, rows: list[tuple[str, float]]) -> None:
    """Write minimal SMD parquet with ``tradeDate`` + ``close``."""
    df = pd.DataFrame(rows, columns=["tradeDate", "close"])
    df.to_parquet(path)


def _write_companyfacts(path: Path, cik: str, shares_history: list[tuple[str, int]]) -> None:
    """Write minimal companyfacts JSON with ``EntityCommonStockSharesOutstanding``."""
    units = [
        {
            "end": filed,
            "filed": filed,
            "val": shares,
            "form": "10-K",
            "accn": f"acc-{i}",
        }
        for i, (filed, shares) in enumerate(shares_history)
    ]
    payload = {
        "cik": int(cik),
        "facts": {"us-gaap": {"EntityCommonStockSharesOutstanding": {"units": {"shares": units}}}},
    }
    path.write_text(json.dumps(payload))


class PitUnionLegacyTests(unittest.TestCase):
    def test_unions_yamls_with_extra_etfs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pit_dir = Path(tmp)
            (pit_dir / "2010-06.yaml").write_text(
                yaml.safe_dump({"asof": "2010-06-30", "tickers": ["AAA", "BBB"]})
            )
            (pit_dir / "2014-12.yaml").write_text(
                yaml.safe_dump({"asof": "2014-12-31", "tickers": ["BBB", "CCC"]})
            )

            tickers = ul.pit_union_legacy(start_year=2008, pit_dir=pit_dir, extra_etfs=("SPY",))

            self.assertEqual(tickers, ["AAA", "BBB", "CCC", "SPY"])

    def test_skips_yamls_before_start_year(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pit_dir = Path(tmp)
            (pit_dir / "2007-01.yaml").write_text(
                yaml.safe_dump({"asof": "2007-01-31", "tickers": ["OLD"]})
            )
            (pit_dir / "2012-06.yaml").write_text(
                yaml.safe_dump({"asof": "2012-06-30", "tickers": ["NEW"]})
            )

            tickers = ul.pit_union_legacy(start_year=2010, pit_dir=pit_dir, extra_etfs=())

            self.assertEqual(tickers, ["NEW"])


class PitUnionFromIvolCacheTests(unittest.TestCase):
    def test_filters_by_asof_eligibility_and_min_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inv_path = Path(tmp) / "inv.parquet"
            _write_inventory(
                inv_path,
                [
                    {
                        "ticker": "ALIVE",
                        "first_date": "2010-01-01",
                        "last_date": "2026-04-30",
                        "n_rows": 4000,
                        "ivp30_rows": 3000,
                        "pre_2018_rows": 2000,
                        "pre_2008_rows": 0,
                    },
                    {
                        "ticker": "FUTURE",
                        "first_date": "2020-01-01",
                        "last_date": "2026-04-30",
                        "n_rows": 1500,
                        "ivp30_rows": 1000,
                        "pre_2018_rows": 0,
                        "pre_2008_rows": 0,
                    },
                    {
                        "ticker": "DEAD",
                        "first_date": "2010-01-01",
                        "last_date": "2014-12-31",
                        "n_rows": 1200,
                        "ivp30_rows": 800,
                        "pre_2018_rows": 1200,
                        "pre_2008_rows": 0,
                    },
                    {
                        "ticker": "THIN",
                        "first_date": "2014-01-01",
                        "last_date": "2026-04-30",
                        "n_rows": 50,
                        "ivp30_rows": 30,
                        "pre_2018_rows": 30,
                        "pre_2008_rows": 0,
                    },
                ],
            )

            tickers = ul.pit_union_from_ivol_cache(
                date(2014, 6, 30), inventory_path=inv_path, extra_etfs=()
            )

            # ALIVE: covers 2014-06-30 ✓, n=4000 ≥ 100
            # FUTURE: starts 2020-01-01 → not eligible at 2014-06-30
            # DEAD: covers 2014-06-30 (last 2014-12-31) ✓, n=1200 ≥ 100
            # THIN: covers 2014-06-30 ✓ but n=50 < 100 → drop
            self.assertEqual(tickers, ["ALIVE", "DEAD"])

    def test_includes_extra_etfs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inv_path = Path(tmp) / "inv.parquet"
            _write_inventory(inv_path, [])

            tickers = ul.pit_union_from_ivol_cache(
                date(2014, 6, 30),
                inventory_path=inv_path,
                extra_etfs=("SPY", "QQQ"),
            )

            self.assertEqual(tickers, ["QQQ", "SPY"])

    def test_raises_when_inventory_missing(self) -> None:
        with self.assertRaises(FileNotFoundError):
            ul.pit_union_from_ivol_cache(
                date(2014, 6, 30),
                inventory_path=Path("/no/such/path.parquet"),
            )

    def test_uppercases_tickers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inv_path = Path(tmp) / "inv.parquet"
            _write_inventory(
                inv_path,
                [
                    {
                        "ticker": "lower",
                        "first_date": "2010-01-01",
                        "last_date": "2026-04-30",
                        "n_rows": 1000,
                        "ivp30_rows": 800,
                        "pre_2018_rows": 500,
                        "pre_2008_rows": 0,
                    }
                ],
            )

            tickers = ul.pit_union_from_ivol_cache(
                date(2014, 6, 30), inventory_path=inv_path, extra_etfs=()
            )

            self.assertEqual(tickers, ["LOWER"])


class PitUnionNberRebuildTests(unittest.TestCase):
    def _setup(self, tmp: str) -> dict[str, Path]:
        """Build a complete miniature data fixture for U3 tests."""
        tmp_path = Path(tmp)
        inv = tmp_path / "inv.parquet"
        cache = tmp_path / "cache"
        cf = tmp_path / "companyfacts"
        cik_map_path = tmp_path / "cik_map.yaml"
        cache.mkdir()
        cf.mkdir()

        # Two scoreable tickers — both alive at 2014-06-30 with adequate rows.
        _write_inventory(
            inv,
            [
                {
                    "ticker": "MIDCAP",
                    "first_date": "2010-01-01",
                    "last_date": "2026-04-30",
                    "n_rows": 4000,
                    "ivp30_rows": 3000,
                    "pre_2018_rows": 2000,
                    "pre_2008_rows": 0,
                },
                {
                    "ticker": "MEGACAP",
                    "first_date": "2010-01-01",
                    "last_date": "2026-04-30",
                    "n_rows": 4000,
                    "ivp30_rows": 3000,
                    "pre_2018_rows": 2000,
                    "pre_2008_rows": 0,
                },
                {
                    "ticker": "MISSCIK",
                    "first_date": "2010-01-01",
                    "last_date": "2026-04-30",
                    "n_rows": 4000,
                    "ivp30_rows": 3000,
                    "pre_2018_rows": 2000,
                    "pre_2008_rows": 0,
                },
                {
                    "ticker": "MISSFACTS",
                    "first_date": "2010-01-01",
                    "last_date": "2026-04-30",
                    "n_rows": 4000,
                    "ivp30_rows": 3000,
                    "pre_2018_rows": 2000,
                    "pre_2008_rows": 0,
                },
            ],
        )

        # Close prices.
        _write_smd_close(cache / "MIDCAP.parquet", [("2014-06-30", 50.0)])
        _write_smd_close(cache / "MEGACAP.parquet", [("2014-06-30", 500.0)])
        _write_smd_close(cache / "MISSCIK.parquet", [("2014-06-30", 25.0)])
        _write_smd_close(cache / "MISSFACTS.parquet", [("2014-06-30", 25.0)])

        # Shares outstanding for the two with cik mapping. MIDCAP $1B, MEGACAP $50B.
        _write_companyfacts(
            cf / "0000000001.json",
            cik="0000000001",
            shares_history=[("2014-03-31", 20_000_000)],  # 20M × $50 = $1B
        )
        _write_companyfacts(
            cf / "0000000002.json",
            cik="0000000002",
            shares_history=[("2014-03-31", 100_000_000)],  # 100M × $500 = $50B
        )
        # MISSFACTS has cik mapping but no companyfacts JSON on disk.
        cik_map_path.write_text(
            yaml.safe_dump(
                {
                    "MIDCAP": 1,
                    "MEGACAP": 2,
                    "MISSFACTS": 3,
                    # Note: MISSCIK absent from map.
                }
            )
        )

        return {
            "inv": inv,
            "cache": cache,
            "cf": cf,
            "cik_map": cik_map_path,
        }

    def test_keeps_only_cap_band_tickers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._setup(tmp)

            tickers = ul.pit_union_nber_rebuild(
                date(2014, 6, 30),
                inventory_path=paths["inv"],
                smd_cache_dir=paths["cache"],
                companyfacts_dir=paths["cf"],
                ticker_cik_map_path=paths["cik_map"],
                cap_min_usd=300_000_000.0,
                cap_max_usd=3_000_000_000.0,
                on_missing_shares="exclude",
                extra_etfs=(),
            )

            # MIDCAP $1B in band ✓
            # MEGACAP $50B above band → drop
            # MISSCIK no cik mapping; exclude policy → drop
            # MISSFACTS no facts JSON; exclude policy → drop
            self.assertEqual(tickers, ["MIDCAP"])

    def test_include_on_missing_keeps_unmappable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._setup(tmp)

            tickers = ul.pit_union_nber_rebuild(
                date(2014, 6, 30),
                inventory_path=paths["inv"],
                smd_cache_dir=paths["cache"],
                companyfacts_dir=paths["cf"],
                ticker_cik_map_path=paths["cik_map"],
                cap_min_usd=300_000_000.0,
                cap_max_usd=3_000_000_000.0,
                on_missing_shares="include",
                extra_etfs=(),
            )

            # MIDCAP in band ✓; MEGACAP out of band ✗ (cap-resolvable, dropped);
            # MISSCIK no cik → keep (include policy);
            # MISSFACTS cik but no facts → keep (include policy).
            self.assertEqual(tickers, ["MIDCAP", "MISSCIK", "MISSFACTS"])

    def test_rejects_invalid_on_missing_shares(self) -> None:
        with self.assertRaises(ValueError):
            ul.pit_union_nber_rebuild(date(2014, 6, 30), on_missing_shares="bogus")

    def test_drops_when_close_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._setup(tmp)
            # Wipe MIDCAP price data so its close is unresolvable.
            (paths["cache"] / "MIDCAP.parquet").unlink()

            tickers = ul.pit_union_nber_rebuild(
                date(2014, 6, 30),
                inventory_path=paths["inv"],
                smd_cache_dir=paths["cache"],
                companyfacts_dir=paths["cf"],
                ticker_cik_map_path=paths["cik_map"],
                on_missing_shares="exclude",
                extra_etfs=(),
            )

            # MIDCAP no price → drop even though shares were resolvable.
            self.assertEqual(tickers, [])

    def test_extra_etfs_appended_to_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._setup(tmp)

            tickers = ul.pit_union_nber_rebuild(
                date(2014, 6, 30),
                inventory_path=paths["inv"],
                smd_cache_dir=paths["cache"],
                companyfacts_dir=paths["cf"],
                ticker_cik_map_path=paths["cik_map"],
                on_missing_shares="exclude",
                extra_etfs=("SPY", "QQQ"),
            )

            # MIDCAP from cap-band logic + ETFs always added.
            self.assertEqual(tickers, ["MIDCAP", "QQQ", "SPY"])


if __name__ == "__main__":
    unittest.main()
