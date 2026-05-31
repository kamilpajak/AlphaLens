"""Pin that each CLI success-path emits domain metrics.

The textfile emitter (`alphalens_pipeline.observability.textfile.emit_domain_metrics`)
is wired into four CLI commands. These tests don't run the commands
end-to-end (each has heavy external deps — SEC EDGAR, Perplexity,
Gemini, Alpha Vantage); they verify the wire-up by mocking
``emit_domain_metrics`` at the symbol the command module imported it
under, exercising the minimum code path needed to reach the emit call,
and asserting the call shape.

Why this matters: a future refactor that drops the emit call would
silently delete the dashboard panel + alert input for the affected
job, with no test failure to catch it. The emit IS the contract.
"""

from __future__ import annotations

import importlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


class TestEdgarDetectEmitsDomainMetrics(unittest.TestCase):
    def test_detect_emits_events_and_portfolio_size(self) -> None:
        # Import the CLI module and stub out detector wiring.
        # ``_build_detector`` does live env reads + sqlite opens + an
        # HTTP fetch for company_tickers.json; mocking it isolates the
        # test to the emit-path.
        from alphalens_cli.commands import edgar

        detector = MagicMock()
        detector.run_once.return_value = {
            "events_detected": 12,
            "events_dispatched": 3,
        }
        detector.portfolio.held = ["AAPL", "MSFT", "NVDA"]
        detector.portfolio.watchlist = ["GOOGL", "AMD"]

        with (
            patch.object(edgar, "_build_detector", return_value=detector),
            patch.object(edgar, "emit_domain_metrics") as emit,
        ):
            edgar.detect()

        emit.assert_called_once()
        kwargs = emit.call_args.kwargs
        self.assertEqual(kwargs["job"], "edgar-detect")
        metrics = kwargs["metrics"]
        self.assertEqual(metrics["alphalens_edgar_events_detected_total"], 12)
        self.assertEqual(metrics["alphalens_edgar_events_dispatched_total"], 3)
        self.assertEqual(metrics['alphalens_edgar_portfolio_size{class="held"}'], 3)
        self.assertEqual(metrics['alphalens_edgar_portfolio_size{class="watchlist"}'], 2)


class TestLiteratureScanEmitsDomainMetrics(unittest.TestCase):
    def test_scan_emits_trigger_gauge_with_window_label(self) -> None:
        # The runner returns a ReviewResult dataclass; stub it.
        from alphalens_cli.commands import literature

        result = MagicMock()
        result.path = Path("/tmp/fake.md")
        result.has_trigger = True

        with (
            patch.object(literature, "_resolve_credentials", return_value=("k", "b", "c")),
            patch.object(literature, "run_weekly", return_value=result) as runner,
            patch.object(literature, "emit_domain_metrics") as emit,
        ):
            literature.scan(
                window=literature.ScanWindow.weekly,
                period="2026-W22",
                output_dir=Path("/tmp"),
            )

        runner.assert_called_once()
        emit.assert_called_once()
        kwargs = emit.call_args.kwargs
        self.assertEqual(kwargs["job"], "literature-scan-weekly")
        metrics = kwargs["metrics"]
        self.assertEqual(
            metrics['alphalens_literature_last_run_trigger{window="weekly"}'],
            1,
        )

    def test_scan_emits_zero_trigger_when_runner_reports_no_trigger(self) -> None:
        from alphalens_cli.commands import literature

        result = MagicMock()
        result.path = Path("/tmp/fake.md")
        result.has_trigger = False

        with (
            patch.object(literature, "_resolve_credentials", return_value=("k", "b", "c")),
            patch.object(literature, "run_monthly", return_value=result),
            patch.object(literature, "emit_domain_metrics") as emit,
        ):
            literature.scan(
                window=literature.ScanWindow.monthly,
                period="2026-05",
                output_dir=Path("/tmp"),
            )

        emit.assert_called_once()
        metrics = emit.call_args.kwargs["metrics"]
        self.assertEqual(
            metrics['alphalens_literature_last_run_trigger{window="monthly"}'],
            0,
        )


class TestAvEarningsBackfillEmitsDomainMetrics(unittest.TestCase):
    def _import_script(self):
        return importlib.import_module("scripts.av_earnings_daily_backfill")

    def test_emits_on_success_path(self) -> None:
        mod = self._import_script()
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp) / "data"
            (data_root / "sp500_pit").mkdir(parents=True)
            (data_root / "sp500_pit" / "2024.yaml").write_text(
                "as_of: '2024-01-01'\nsource: test\ntickers: [AAPL, MSFT]\n"
            )

            with (
                patch.object(mod, "fetch_earnings_batch") as fetch,
                patch.object(mod, "emit_domain_metrics") as emit,
            ):
                fetch.return_value = {
                    "AAPL": "fetched",
                    "MSFT": "cached",
                }
                rc = mod.main(
                    [
                        "--cache-dir",
                        str(Path(tmp) / "cache"),
                        "--data-root",
                        str(data_root),
                        "--throttle-seconds",
                        "0",
                    ]
                )

            self.assertEqual(rc, 0)
            emit.assert_called_once()
            kwargs = emit.call_args.kwargs
            self.assertEqual(kwargs["job"], "av-earnings-backfill")
            metrics = kwargs["metrics"]
            self.assertEqual(metrics['alphalens_av_tickers_total{status="fetched"}'], 1)
            self.assertEqual(metrics['alphalens_av_tickers_total{status="cached"}'], 1)
            self.assertEqual(metrics['alphalens_av_tickers_total{status="failed"}'], 0)
            self.assertEqual(metrics["alphalens_av_quota_remaining"], 24)
            self.assertEqual(metrics["alphalens_av_quota_blocked"], 0)

    def test_emits_on_rate_limited_path(self) -> None:
        # The AVRateLimitError exit-clean branch is the steady-state
        # behaviour (free-tier quota burns 25 calls then aborts the
        # batch). Dashboard must see ``quota_blocked=1`` so the
        # operator can distinguish "ran clean" from "ran out of API".
        mod = self._import_script()
        from alphalens_pipeline.data.alt_data.av_earnings_client import AVRateLimitError

        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp) / "data"
            (data_root / "sp500_pit").mkdir(parents=True)
            (data_root / "sp500_pit" / "2024.yaml").write_text(
                "as_of: '2024-01-01'\nsource: test\ntickers: [AAPL]\n"
            )

            with (
                patch.object(mod, "fetch_earnings_batch") as fetch,
                patch.object(mod, "emit_domain_metrics") as emit,
            ):
                fetch.side_effect = AVRateLimitError("quota burned")
                rc = mod.main(
                    [
                        "--cache-dir",
                        str(Path(tmp) / "cache"),
                        "--data-root",
                        str(data_root),
                        "--throttle-seconds",
                        "0",
                    ]
                )

            self.assertEqual(rc, 0)
            emit.assert_called_once()
            metrics = emit.call_args.kwargs["metrics"]
            self.assertEqual(metrics["alphalens_av_quota_blocked"], 1)
            self.assertEqual(metrics["alphalens_av_quota_remaining"], 25)


class TestThematicBriefEmitsDomainMetrics(unittest.TestCase):
    def test_brief_emits_briefs_count_and_per_model_split(self) -> None:
        import pandas as pd
        from alphalens_cli.commands import thematic

        enriched = pd.DataFrame({"ticker": ["A", "B", "C"]})
        enriched.attrs["n_pro"] = 1
        enriched.attrs["n_flash"] = 2

        with tempfile.TemporaryDirectory() as tmp:
            scored_dir = Path(tmp) / "scored"
            scored_dir.mkdir()
            (scored_dir / "2026-05-29.parquet").touch()
            output_dir = Path(tmp) / "briefs"
            output_dir.mkdir()

            with (
                patch.object(thematic.pd, "read_parquet", return_value=pd.DataFrame({"x": [1]})),
                patch.object(thematic.brief_orchestrator, "generate_briefs", return_value=enriched),
                patch.object(thematic, "emit_domain_metrics") as emit,
            ):
                thematic.brief(date="2026-05-29", scored_dir=scored_dir, output_dir=output_dir)

            emit.assert_called_once()
            kwargs = emit.call_args.kwargs
            self.assertEqual(kwargs["job"], "thematic-build")
            metrics = kwargs["metrics"]
            self.assertEqual(metrics["alphalens_thematic_briefs_total"], 3)
            self.assertEqual(metrics['alphalens_thematic_briefs_by_model{model="pro"}'], 1)
            self.assertEqual(metrics['alphalens_thematic_briefs_by_model{model="flash"}'], 2)


class TestEmitFailureDoesNotPoisonSuccessPath(unittest.TestCase):
    """A transient metrics-dir failure (disk full, permission flip)
    must NOT turn a successful cron run into a failed unit.

    The actual work — Telegram alert, scan markdown, briefs parquet,
    AV cache write — is already persisted before ``emit_domain_metrics``
    is called. An OSError from the emitter is pure observability
    debt, not a job failure. Without the try/except guard the unit
    would exit non-zero, the cron-health hook would write
    ``last_exit_code != 0`` + skip ``last_success_timestamp``, and
    PR-3 would eventually fire a staleness alert despite the job
    having done its work cleanly.

    Pin the guard at every callsite. Zen pre-merge review of PR #311
    flagged the absence as the single CRITICAL finding.
    """

    def test_edgar_detect_swallows_emit_oserror(self) -> None:
        from alphalens_cli.commands import edgar

        detector = MagicMock()
        detector.run_once.return_value = {
            "events_detected": 1,
            "events_dispatched": 0,
        }
        detector.portfolio.held = []
        detector.portfolio.watchlist = []

        with (
            patch.object(edgar, "_build_detector", return_value=detector),
            patch.object(edgar, "emit_domain_metrics", side_effect=OSError("disk full")),
        ):
            # MUST NOT raise — the EDGAR poll already shipped any
            # alerts and updated seen_events.db.
            edgar.detect()

    def test_literature_scan_swallows_emit_oserror(self) -> None:
        from alphalens_cli.commands import literature

        result = MagicMock()
        result.path = Path("/tmp/fake.md")
        result.has_trigger = True

        with (
            patch.object(literature, "_resolve_credentials", return_value=("k", "b", "c")),
            patch.object(literature, "run_weekly", return_value=result),
            patch.object(literature, "emit_domain_metrics", side_effect=OSError("perm denied")),
        ):
            literature.scan(
                window=literature.ScanWindow.weekly,
                period="2026-W22",
                output_dir=Path("/tmp"),
            )

    def test_av_backfill_swallows_emit_oserror(self) -> None:
        mod = importlib.import_module("scripts.av_earnings_daily_backfill")

        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp) / "data"
            (data_root / "sp500_pit").mkdir(parents=True)
            (data_root / "sp500_pit" / "2024.yaml").write_text(
                "as_of: '2024-01-01'\nsource: test\ntickers: [AAPL]\n"
            )

            with (
                patch.object(mod, "fetch_earnings_batch", return_value={"AAPL": "fetched"}),
                patch.object(mod, "emit_domain_metrics", side_effect=OSError("no metrics dir")),
            ):
                rc = mod.main(
                    [
                        "--cache-dir",
                        str(Path(tmp) / "cache"),
                        "--data-root",
                        str(data_root),
                        "--throttle-seconds",
                        "0",
                    ]
                )

            # Backfill MUST exit 0 — AV cache write is the work and
            # it succeeded; observability loss is acceptable.
            self.assertEqual(rc, 0)

    def test_thematic_brief_swallows_emit_oserror(self) -> None:
        import pandas as pd
        from alphalens_cli.commands import thematic

        enriched = pd.DataFrame({"ticker": ["A"]})
        enriched.attrs["n_pro"] = 0
        enriched.attrs["n_flash"] = 1

        with tempfile.TemporaryDirectory() as tmp:
            scored_dir = Path(tmp) / "scored"
            scored_dir.mkdir()
            (scored_dir / "2026-05-29.parquet").touch()
            output_dir = Path(tmp) / "briefs"
            output_dir.mkdir()

            with (
                patch.object(thematic.pd, "read_parquet", return_value=pd.DataFrame({"x": [1]})),
                patch.object(thematic.brief_orchestrator, "generate_briefs", return_value=enriched),
                patch.object(thematic, "emit_domain_metrics", side_effect=OSError("ENOSPC")),
            ):
                thematic.brief(date="2026-05-29", scored_dir=scored_dir, output_dir=output_dir)


if __name__ == "__main__":
    unittest.main()
