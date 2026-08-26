"""Tests for the §10.4/§10.5 driver script (#1115).

Everything here runs on SYNTHETIC fixture stores — never cohort rows: the memo
forbids any computation of the A-vs-B contrast on cohort data before the
floors are met, and these tests must be runnable throughout the accrual
window without constituting a look. Where a test patches a floor or the
resample count down for runtime, the frozen production value is pinned by
``TestFrozenDriverConstants`` so the patch cannot mask drift.
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import pandas as pd
from alphalens_pipeline.feedback.population_ladder_monitor import _engine_cutoffs
from alphalens_research.diagnostics import exit_policy_analysis as epa
from scripts import exit_policy_analysis as driver


def _setup() -> dict:
    return {
        "status": "OK",
        "disaster_stop": 90.0,
        "entry_tiers": [{"limit": 100.0, "alloc_pct": 100.0}],
        "tp_tranches": [{"target": 105.0, "tranche_pct": 100.0}],
        "atr": 4.0,
        "order_ttl_days": 7,
    }


def _write_day(store: Path, briefs: Path, day: str, tickers: list[str]) -> None:
    """One synthetic store day: the ladder parquet, the brief parquet, and a
    generous bar path per ticker (fills at 100, rises through both arms'
    targets). Timestamps are REAL 2026 epochs reaching past the
    position-expiry cutoff — an epoch-1970 path classifies as bars_missing
    and silently skips the replay."""
    pd.DataFrame({"ticker": tickers}).to_parquet(store / f"{day}.parquet", index=False)
    pd.DataFrame(
        {
            "ticker": tickers,
            "brief_trade_setup": [json.dumps(_setup()) for _ in tickers],
            "technical_pct_off_52w_high": [None for _ in tickers],
        }
    ).to_parquet(briefs / f"{day}.parquet", index=False)
    *_rest, position_expiry_ms = _engine_cutoffs(dt.date.fromisoformat(day), _setup(), "XNYS")
    day_ms = 24 * 3600 * 1000
    start_ms = position_expiry_ms - 70 * day_ms
    bars = pd.DataFrame(
        [{"t": start_ms + i * day_ms, "l": 99.0, "h": 106.5, "c": 105.0} for i in range(75)]
    )
    for ticker in tickers:
        arrival = dt.date.fromisoformat(day)  # trading-day fixtures: arrival == brief date
        bars.to_parquet(store / "bars" / f"{ticker}_{arrival.isoformat()}.parquet", index=False)


def _fixture_dirs(tmp: str) -> tuple[Path, Path]:
    store = Path(tmp) / "population_ladders"
    (store / "bars").mkdir(parents=True)
    briefs = Path(tmp) / "thematic_briefs"
    briefs.mkdir()
    return store, briefs


class TestFrozenDriverConstants(unittest.TestCase):
    def test_frozen_values_survive(self):
        # Tests below patch some of these down for runtime; this pin is what
        # keeps the patched copies honest against the memo's frozen values.
        self.assertEqual(driver.PRIMARY_SLIPPAGE_BPS, 40.0)
        self.assertEqual(driver.SLIPPAGE_GRID, (0.0, 20.0, 40.0, 80.0))
        self.assertEqual(driver.NOTIONAL_GRID_EXTRA, (1_000.0, 10_000.0))
        self.assertEqual(driver.WINSOR_PCT, 0.01)
        self.assertEqual(driver.EXCHANGE, "XNYS")
        self.assertEqual(epa.BLOCK_FLOOR, 10)
        self.assertEqual(epa.BLOCK_LEN_SESSIONS, 42)
        self.assertEqual(epa.BOOTSTRAP_RESAMPLES, 10_000)
        self.assertEqual(epa.BOOTSTRAP_SEED, 20260824)
        self.assertEqual(epa.ALPHA_TWO_SIDED, 0.05)


class TestExtract(unittest.TestCase):
    def test_extract_is_input_only_and_prints_the_matching_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, briefs = _fixture_dirs(tmp)
            _write_day(store, briefs, "2026-06-02", ["AAA", "BBB"])
            # A store row with no brief entry stays in the extract with a null
            # setup — the extract is the INPUT census; §5.1 exclusion happens
            # at analyze time, visibly.
            pd.DataFrame({"ticker": ["AAA", "BBB", "CCC"]}).to_parquet(
                store / "2026-06-02.parquet", index=False
            )
            out_path = Path(tmp) / "extract.parquet"
            with (
                mock.patch.object(driver, "STORE_DIR", store),
                mock.patch.object(driver, "BRIEFS_DIR", briefs),
                mock.patch.object(
                    driver.sys,
                    "argv",
                    [
                        "x",
                        "extract",
                        "--cohort-open",
                        "2026-06-01",
                        "--analysis-session",
                        "2027-06-01",
                        "--out",
                        str(out_path),
                    ],
                ),
            ):
                out = io.StringIO()
                with redirect_stdout(out):
                    code = driver.main()
            self.assertEqual(code, 0)
            printed = json.loads(out.getvalue())
            self.assertEqual(printed["rows"], 3)
            frame = pd.read_parquet(out_path)
            # §10.5: exactly the input columns — structurally no outcome can exist.
            self.assertEqual(tuple(frame.columns), epa.EXTRACT_COLUMNS)
            self.assertEqual(printed["sha256"], epa.sha256_of(out_path))
            # None round-trips through parquet as NaN — either counts as null.
            self.assertTrue(
                pd.isna(frame.loc[frame["ticker"] == "CCC", "trade_setup_json"].iloc[0])
            )

    def test_extract_span_drops_briefs_after_analysis_minus_horizon(self):
        # §5.5: a brief later than analysis-session − 42 sessions is not in
        # the sample at all — nothing enters by maturing.
        with tempfile.TemporaryDirectory() as tmp:
            store, briefs = _fixture_dirs(tmp)
            _write_day(store, briefs, "2026-06-02", ["AAA"])
            _write_day(store, briefs, "2026-07-15", ["BBB"])  # < 42 sessions before analysis
            out_path = Path(tmp) / "extract.parquet"
            with (
                mock.patch.object(driver, "STORE_DIR", store),
                mock.patch.object(driver, "BRIEFS_DIR", briefs),
            ):
                args = argparse.Namespace(
                    cohort_open="2026-06-01",
                    analysis_session="2026-08-03",
                    out=str(out_path),
                )
                out = io.StringIO()
                with redirect_stdout(out):
                    code = driver.cmd_extract(args)
            self.assertEqual(code, 0)
            frame = pd.read_parquet(out_path)
            self.assertEqual(sorted(frame["ticker"]), ["AAA"])


class TestAnalyzeRefusals(unittest.TestCase):
    def _extract_file(self, tmp: str, days: list[str]) -> Path:
        rows = [
            {
                "brief_date": day,
                "ticker": "AAA",
                "trade_setup_json": json.dumps(_setup()),
                "pct_off_52w_high": None,
            }
            for day in days
        ]
        path = Path(tmp) / "extract.parquet"
        pd.DataFrame(rows, columns=list(epa.EXTRACT_COLUMNS)).to_parquet(path, index=False)
        return path

    def test_wrong_hash_refuses_before_any_outcome_is_computed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._extract_file(tmp, ["2026-06-02"])
            args = argparse.Namespace(
                extract=str(path), sha256="0" * 64, n0=3750.0, sd_d=1.0, delta_min=10.0
            )
            with mock.patch.object(
                driver, "compute_outcomes", side_effect=AssertionError("outcome computed")
            ):
                with self.assertRaises(SystemExit) as ctx:
                    driver.cmd_analyze(args)
        self.assertIn("hash mismatch", str(ctx.exception))

    def test_below_block_floor_refuses_without_consuming_the_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, briefs = _fixture_dirs(tmp)
            _write_day(store, briefs, "2026-06-02", ["AAA"])
            path = self._extract_file(tmp, ["2026-06-02"])
            args = argparse.Namespace(
                extract=str(path),
                sha256=epa.sha256_of(path),
                n0=3750.0,
                sd_d=1.0,
                delta_min=10.0,
            )
            with (
                mock.patch.object(driver, "STORE_DIR", store),
                mock.patch.object(driver, "BRIEFS_DIR", briefs),
            ):
                with self.assertRaises(SystemExit) as ctx:
                    driver.cmd_analyze(args)
        message = str(ctx.exception)
        self.assertIn("blocks", message)
        self.assertIn("slot is not consumed", message)

    def test_below_pair_floor_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, briefs = _fixture_dirs(tmp)
            _write_day(store, briefs, "2026-06-02", ["AAA"])
            path = self._extract_file(tmp, ["2026-06-02"])
            args = argparse.Namespace(
                extract=str(path),
                sha256=epa.sha256_of(path),
                n0=3750.0,
                sd_d=200.0,  # floor >> 1 pair
                delta_min=20.0,
            )
            with (
                mock.patch.object(driver, "STORE_DIR", store),
                mock.patch.object(driver, "BRIEFS_DIR", briefs),
                # Block floor patched THROUGH so the pair floor is reached;
                # the real value is pinned by TestFrozenDriverConstants.
                mock.patch.object(driver, "BLOCK_FLOOR", 0),
            ):
                with self.assertRaises(SystemExit) as ctx:
                    driver.cmd_analyze(args)
        self.assertIn("pairs", str(ctx.exception))


class TestAnalyzeEndToEnd(unittest.TestCase):
    def test_synthetic_cohort_produces_the_full_payload(self):
        days = ["2026-06-02", "2026-06-03", "2026-06-04"]
        with tempfile.TemporaryDirectory() as tmp:
            store, briefs = _fixture_dirs(tmp)
            for day in days:
                _write_day(store, briefs, day, ["AAA", "BBB"])
            extract_path = Path(tmp) / "extract.parquet"
            with (
                mock.patch.object(driver, "STORE_DIR", store),
                mock.patch.object(driver, "BRIEFS_DIR", briefs),
            ):
                with redirect_stdout(io.StringIO()):
                    code = driver.cmd_extract(
                        argparse.Namespace(
                            cohort_open="2026-06-01",
                            analysis_session="2027-06-01",
                            out=str(extract_path),
                        )
                    )
                self.assertEqual(code, 0)
                args = argparse.Namespace(
                    extract=str(extract_path),
                    sha256=epa.sha256_of(extract_path),
                    n0=3750.0,
                    sd_d=1.0,
                    delta_min=10.0,
                )
                out = io.StringIO()
                with (
                    # Floors and resamples patched down for a fixture-scale
                    # run; frozen values pinned by TestFrozenDriverConstants.
                    mock.patch.object(driver, "BLOCK_FLOOR", 0),
                    mock.patch.object(driver, "BOOTSTRAP_RESAMPLES", 150),
                    redirect_stdout(out),
                ):
                    code = driver.cmd_analyze(args)
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(
            set(payload["arms"]),
            {"iid", "cluster_day", "cluster_ticker", "cluster_day_ticker", "moving_block"},
        )
        self.assertIn(payload["verdict"], {"arm_b_better", "arm_a_better", "not_distinguishable"})
        self.assertEqual(payload["floors"]["pairs"], 6)
        self.assertEqual(payload["bootstrap"]["seed"], epa.BOOTSTRAP_SEED)
        self.assertGreaterEqual(payload["fallback_share_b"], 0.0)
        self.assertLessEqual(payload["fallback_share_b"], 1.0)
        sens = payload["sensitivities"]
        self.assertEqual(
            set(sens),
            {
                "jointly_feasible_delta",
                "realised_anchor_delta",
                "unclamped_static_delta",
                "notional_grid_delta",
                "slippage_grid_delta",
                "winsorized_delta",
                "equal_risk_delta",
                "r_space_bridge",
            },
        )
        self.assertEqual(set(sens["slippage_grid_delta"]), {"0.0", "20.0", "40.0", "80.0"})
        self.assertEqual(set(sens["notional_grid_delta"]), {"3750.0", "1000.0", "10000.0"})
        # 6 and 8 are computed at look time — the payload must carry the
        # explicit markers so the results memo cannot silently omit them.
        self.assertIn("look_time", sens["equal_risk_delta"])
        self.assertIn("look_time", sens["r_space_bridge"])
        self.assertEqual(payload["extract_sha256"], args.sha256)
        # §8.1 items 1, 3, 4, 6, 8 beyond the arms: histogram, holding-time
        # median + p95 per arm, MAE distributions per arm, the ceiling-capped
        # share, and the flow-table pointer.
        self.assertEqual(sum(payload["distribution"]["histogram"]["counts"]), 6)
        for arm_key in ("arm_a", "arm_b"):
            with self.subTest(arm=arm_key):
                holding = payload["holding_days"][arm_key]
                self.assertGreaterEqual(holding["p95"], holding["median"])
                mae = payload["mae"][arm_key]
                self.assertEqual(mae["mae_pct"]["n"], 6)
                self.assertEqual(mae["mae_usd"]["n"], 6)
        self.assertEqual(payload["ceiling_capped_share_b"], 0.0)  # fixture pct is null
        self.assertIn("exit_policy_missingness", payload["flow_table"])


if __name__ == "__main__":
    unittest.main()
