"""Integration test for the daily AV EARNINGS backfill script.

Validates argparse contract, universe selection, AVRateLimitError exit-clean
semantics, and optional rclone sync invocation. Underlying fetcher logic is
covered by `test_alt_data_av_earnings_client`; this suite only tests the
orchestration glue.
"""

import importlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml


def _write_sp500_snapshot(path: Path, *, as_of: str, tickers: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({"as_of": as_of, "source": "test", "tickers": tickers}, sort_keys=False)
    )


def _good_payload(ticker: str) -> dict:
    return {
        "symbol": ticker,
        "quarterlyEarnings": [
            {
                "fiscalDateEnding": "2024-09-30",
                "reportedDate": "2024-10-31",
                "reportedEPS": "1.64",
                "estimatedEPS": "1.60",
            }
        ],
    }


def _import_script():
    """Import the script module fresh on each test so sys.argv parsing is clean."""
    return importlib.import_module("scripts.av_earnings_daily_backfill")


class TestUniverseSelection(unittest.TestCase):
    def test_sp500_union_loads_from_data_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp) / "data"
            _write_sp500_snapshot(
                data_root / "sp500_pit" / "2024.yaml",
                as_of="2024-01-01",
                tickers=["AAPL", "MSFT"],
            )
            cache = Path(tmp) / "cache"

            mod = _import_script()
            with patch.object(mod, "fetch_earnings_batch") as fetch_mock:
                fetch_mock.return_value = {"AAPL": "fetched", "MSFT": "fetched"}
                rc = mod.main(
                    [
                        "--universe",
                        "sp500_union",
                        "--cache-dir",
                        str(cache),
                        "--data-root",
                        str(data_root),
                        "--throttle-seconds",
                        "0",
                    ]
                )

            self.assertEqual(rc, 0)
            fetch_mock.assert_called_once()
            tickers_arg = fetch_mock.call_args.args[0]
            self.assertEqual(sorted(tickers_arg), ["AAPL", "MSFT"])


class TestKnownGapsSkipped(unittest.TestCase):
    """Tickers AV has no EARNINGS data for (under any symbol) are filtered out
    before the fetch batch so the daily run does not burn a quota call on them
    and does not log a spurious ``failed=1``. CTRA (Coterra Energy) is the
    standing case: AV returns an empty ``{}`` payload for CTRA and for both
    legacy symbols (COG / XEC) it was formed from in 2021."""

    def test_known_gap_ticker_excluded_from_fetch_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp) / "data"
            _write_sp500_snapshot(
                data_root / "sp500_pit" / "2024.yaml",
                as_of="2024-01-01",
                tickers=["AAPL", "CTRA", "MSFT"],
            )
            cache = Path(tmp) / "cache"

            mod = _import_script()
            with patch.object(mod, "fetch_earnings_batch") as fetch_mock:
                fetch_mock.return_value = {"AAPL": "fetched", "MSFT": "fetched"}
                rc = mod.main(
                    [
                        "--cache-dir",
                        str(cache),
                        "--data-root",
                        str(data_root),
                        "--throttle-seconds",
                        "0",
                    ]
                )

            self.assertEqual(rc, 0)
            fetch_mock.assert_called_once()
            tickers_arg = fetch_mock.call_args.args[0]
            self.assertNotIn("CTRA", tickers_arg)
            self.assertEqual(sorted(tickers_arg), ["AAPL", "MSFT"])

    def test_ctra_registered_in_known_gaps(self) -> None:
        mod = _import_script()
        self.assertIn("CTRA", mod._KNOWN_AV_EARNINGS_GAPS)


class TestRateLimitExitClean(unittest.TestCase):
    def test_persistent_rate_limit_returns_zero_not_raises(self) -> None:
        """Cron job must not error-alert on rate-limit — tomorrow's quota
        window picks up where today left off."""
        from alphalens_pipeline.data.alt_data.av_earnings_client import AVRateLimitError

        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp) / "data"
            _write_sp500_snapshot(
                data_root / "sp500_pit" / "2024.yaml",
                as_of="2024-01-01",
                tickers=["AAPL"],
            )
            cache = Path(tmp) / "cache"

            mod = _import_script()
            with patch.object(mod, "fetch_earnings_batch") as fetch_mock:
                fetch_mock.side_effect = AVRateLimitError("quota exhausted past retry")
                rc = mod.main(
                    [
                        "--cache-dir",
                        str(cache),
                        "--data-root",
                        str(data_root),
                        "--throttle-seconds",
                        "0",
                    ]
                )

            self.assertEqual(rc, 0)


class TestRcloneSync(unittest.TestCase):
    def test_rclone_invoked_when_remote_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp) / "data"
            _write_sp500_snapshot(
                data_root / "sp500_pit" / "2024.yaml",
                as_of="2024-01-01",
                tickers=["AAPL"],
            )
            cache = Path(tmp) / "cache"

            mod = _import_script()
            with (
                patch.object(mod, "fetch_earnings_batch") as fetch_mock,
                patch.object(mod, "subprocess") as subproc_mock,
            ):
                fetch_mock.return_value = {"AAPL": "fetched"}
                rc = mod.main(
                    [
                        "--cache-dir",
                        str(cache),
                        "--data-root",
                        str(data_root),
                        "--throttle-seconds",
                        "0",
                        "--rclone-remote",
                        "nextcloud:alphalens_research/av_cache",
                    ]
                )

            self.assertEqual(rc, 0)
            subproc_mock.run.assert_called_once()
            cmd = subproc_mock.run.call_args.args[0]
            self.assertEqual(cmd[0], "rclone")
            self.assertEqual(cmd[1], "copy")
            self.assertEqual(cmd[2], str(cache))
            self.assertEqual(cmd[3], "nextcloud:alphalens_research/av_cache")

    def test_rclone_bin_override_forwarded_to_subprocess(self) -> None:
        """systemd user services run on a restricted PATH; operators may need
        to pass an absolute rclone path via --rclone-bin to avoid
        FileNotFoundError. Verify the override reaches subprocess.run."""
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp) / "data"
            _write_sp500_snapshot(
                data_root / "sp500_pit" / "2024.yaml",
                as_of="2024-01-01",
                tickers=["AAPL"],
            )
            cache = Path(tmp) / "cache"

            mod = _import_script()
            with (
                patch.object(mod, "fetch_earnings_batch") as fetch_mock,
                patch.object(mod, "subprocess") as subproc_mock,
            ):
                fetch_mock.return_value = {"AAPL": "fetched"}
                mod.main(
                    [
                        "--cache-dir",
                        str(cache),
                        "--data-root",
                        str(data_root),
                        "--throttle-seconds",
                        "0",
                        "--rclone-remote",
                        "nextcloud:alphalens_research/av_cache",
                        "--rclone-bin",
                        "/usr/local/bin/rclone",
                    ]
                )
            cmd = subproc_mock.run.call_args.args[0]
            self.assertEqual(cmd[0], "/usr/local/bin/rclone")

    def test_rclone_not_invoked_when_remote_unset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp) / "data"
            _write_sp500_snapshot(
                data_root / "sp500_pit" / "2024.yaml",
                as_of="2024-01-01",
                tickers=["AAPL"],
            )
            cache = Path(tmp) / "cache"

            mod = _import_script()
            with (
                patch.object(mod, "fetch_earnings_batch") as fetch_mock,
                patch.object(mod, "subprocess") as subproc_mock,
            ):
                fetch_mock.return_value = {"AAPL": "fetched"}
                mod.main(
                    [
                        "--cache-dir",
                        str(cache),
                        "--data-root",
                        str(data_root),
                        "--throttle-seconds",
                        "0",
                    ]
                )
            subproc_mock.run.assert_not_called()


class TestDataRootDefault(unittest.TestCase):
    """The systemd unit on the VPS runs ``av_earnings_daily_backfill.py``
    without ``--data-root``. ADR 0011 moved the PIT roster YAMLs from
    ``apps/alphalens-research/data/`` to ``apps/alphalens-pipeline/data/``;
    the script's default lagged the move for ~4 days (2026-05-26 → 30)
    and the daily timer exited 1 on ``UniverseError`` before hitting AV
    — backfill stalled at 217/500 tickers cached. These tests pin the
    corrected default so a future ADR-driven path move (or a workspace
    layout refactor) fails CI rather than silently breaking the
    production cron.
    """

    def test_default_data_root_points_at_pipeline_workspace_member(self) -> None:
        mod = _import_script()
        args = mod._parse_args([])

        # Last two path segments must be ``alphalens-pipeline/data`` so
        # the assertion is robust to where the repo lives on disk.
        parts = args.data_root.resolve().parts
        self.assertEqual(parts[-2:], ("alphalens-pipeline", "data"))

    def test_default_data_root_resolves_to_existing_sp500_pit_dir(self) -> None:
        # End-to-end: the resolved default + ``/sp500_pit`` must point at
        # a real directory in this repo. Catches the exact failure mode
        # the VPS hit (path resolved but directory absent).
        mod = _import_script()
        args = mod._parse_args([])

        sp500_dir = args.data_root / "sp500_pit"
        self.assertTrue(
            sp500_dir.is_dir(),
            f"sp500_pit directory missing under default data root: {sp500_dir}",
        )


if __name__ == "__main__":
    unittest.main()
