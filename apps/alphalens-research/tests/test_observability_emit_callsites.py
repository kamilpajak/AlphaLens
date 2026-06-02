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

        # 2 of 3 briefs carry a template_id (#399 fill-rate gate instrument).
        enriched = pd.DataFrame(
            {
                "ticker": ["A", "B", "C"],
                "brief_template_id": ["earnings_surprise", None, "m_and_a_press_release"],
            }
        )
        enriched.attrs["n_pro"] = 1
        enriched.attrs["n_flash"] = 2

        with tempfile.TemporaryDirectory() as tmp:
            scored_dir = Path(tmp) / "scored"
            scored_dir.mkdir()
            (scored_dir / "2026-05-29.parquet").touch()
            output_dir = Path(tmp) / "briefs"
            output_dir.mkdir()

            # 5 scored rows in -> 3 briefs out: the input/output stage gauges
            # must capture both so the zero-output-with-nonempty-input alert
            # can distinguish a real silent failure from a quiet day.
            scored_frame = pd.DataFrame({"x": [1, 2, 3, 4, 5]})

            with (
                patch.object(thematic.pd, "read_parquet", return_value=scored_frame),
                patch.object(thematic.brief_orchestrator, "generate_briefs", return_value=enriched),
                patch.object(thematic, "emit_domain_metrics") as emit,
            ):
                thematic.brief(date="2026-05-29", scored_dir=scored_dir, output_dir=output_dir)

            emit.assert_called_once()
            kwargs = emit.call_args.kwargs
            self.assertEqual(kwargs["job"], "thematic-build")
            metrics = kwargs["metrics"]
            # Legacy Grafana panel metrics (unchanged).
            self.assertEqual(metrics["alphalens_thematic_briefs_total"], 3)
            self.assertEqual(metrics['alphalens_thematic_briefs_by_model{model="pro"}'], 1)
            self.assertEqual(metrics['alphalens_thematic_briefs_by_model{model="flash"}'], 2)
            # Phase 4 uniform stage gauges (brief is stage 5).
            self.assertEqual(metrics['alphalens_thematic_stage_output_rows{stage="brief"}'], 3)
            self.assertEqual(metrics['alphalens_thematic_stage_input_rows{stage="brief"}'], 5)
            # #399 gate instrument: 2 of 3 briefs carry a template_id -> 0.6667.
            self.assertEqual(metrics["alphalens_thematic_brief_template_id_total"], 2)
            self.assertAlmostEqual(
                metrics["alphalens_thematic_brief_template_id_fill_ratio"], 0.6667, places=4
            )

    def test_brief_fill_ratio_zero_when_no_template_ids(self) -> None:
        # No brief carries a template_id (the current prod baseline before the
        # subject-match minority lands) -> ratio 0.0, no div-by-zero on counts.
        import pandas as pd
        from alphalens_cli.commands import thematic

        enriched = pd.DataFrame({"ticker": ["A", "B"], "brief_template_id": [None, ""]})
        enriched.attrs["n_pro"] = 0
        enriched.attrs["n_flash"] = 2
        with tempfile.TemporaryDirectory() as tmp:
            scored_dir = Path(tmp) / "scored"
            scored_dir.mkdir()
            (scored_dir / "2026-05-29.parquet").touch()
            output_dir = Path(tmp) / "briefs"
            output_dir.mkdir()
            with (
                patch.object(thematic.pd, "read_parquet", return_value=pd.DataFrame({"x": [1, 2]})),
                patch.object(thematic.brief_orchestrator, "generate_briefs", return_value=enriched),
                patch.object(thematic, "emit_domain_metrics") as emit,
            ):
                thematic.brief(date="2026-05-29", scored_dir=scored_dir, output_dir=output_dir)
            metrics = emit.call_args.kwargs["metrics"]
            self.assertEqual(metrics["alphalens_thematic_brief_template_id_total"], 0)
            self.assertEqual(metrics["alphalens_thematic_brief_template_id_fill_ratio"], 0.0)

    def test_brief_fill_metrics_handles_pd_na_dtype_without_crashing(self) -> None:
        # Nullable string dtype surfaces pd.NA, not None -> bool(pd.NA) would
        # raise TypeError through .apply. The pd.isna guard must count it as
        # unfilled, not crash (zen MEDIUM).
        import pandas as pd
        from alphalens_cli.commands import thematic

        col = pd.array(["earnings_surprise", pd.NA, "m_and_a_press_release"], dtype="string")
        enriched = pd.DataFrame({"ticker": ["A", "B", "C"], "brief_template_id": col})
        out = thematic._brief_template_fill_metrics(enriched)
        self.assertEqual(out["alphalens_thematic_brief_template_id_total"], 2)
        self.assertAlmostEqual(
            out["alphalens_thematic_brief_template_id_fill_ratio"], 0.6667, places=4
        )


class TestThematicStageVolumeEmits(unittest.TestCase):
    """Phase 4 dead-man-switch: each upstream stage emits an input/output
    row-count gauge pair so a silent mid-pipeline failure (e.g. an LLM model
    retiring -> 0 events from 200 news, run still exits 0) trips the
    ``AlphalensThematicStageZeroOutput`` alert. Each stage writes its own
    ``alphalens_domain_thematic-<stage>.prom`` file (the 5 stages are 5
    separate processes in run_thematic_day.sh; a shared job name would have
    each clobber the prior stage's file).
    """

    def _df(self, n: int, **cols):
        import pandas as pd

        if cols:
            return pd.DataFrame(cols)
        return pd.DataFrame({"_": list(range(n))})

    def test_ingest_emits_stage_volume(self) -> None:
        import pandas as pd
        from alphalens_cli.commands import thematic

        df = pd.DataFrame({"source": ["polygon", "rss"], "tickers": [["AAPL"], ["MSFT"]]})

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(thematic.news_ingest, "ingest_daily", return_value=df),
                patch.object(thematic, "emit_domain_metrics") as emit,
            ):
                thematic.ingest(date="2026-05-29", cache_dir=Path(tmp))

            emit.assert_called_once()
            kwargs = emit.call_args.kwargs
            self.assertEqual(kwargs["job"], "thematic-ingest")
            metrics = kwargs["metrics"]
            # Source stage: input == output (no upstream to silently fail).
            self.assertEqual(metrics['alphalens_thematic_stage_output_rows{stage="ingest"}'], 2)
            self.assertEqual(metrics['alphalens_thematic_stage_input_rows{stage="ingest"}'], 2)

    def test_extract_emits_stage_volume_with_news_input(self) -> None:
        import pandas as pd
        from alphalens_cli.commands import thematic

        events = pd.DataFrame(
            {"event_type": ["m_and_a", "guidance", "earnings"], "sentiment": ["+", "-", "+"]}
        )

        with tempfile.TemporaryDirectory() as tmp:
            news_dir = Path(tmp) / "news"
            events_dir = Path(tmp) / "events"
            news_dir.mkdir()
            events_dir.mkdir()
            # 5 news rows the extract stage consumed -> input gauge = 5.
            pd.DataFrame({"news_id": [1, 2, 3, 4, 5]}).to_parquet(
                news_dir / "2026-05-29.parquet", index=False
            )

            with (
                patch.dict("os.environ", {"OPENROUTER_API_KEY": "k"}),
                patch.object(thematic.event_extractor, "extract_daily", return_value=events),
                patch.object(thematic.themes_mod, "roll_up", return_value=self._df(0)),
                patch.object(thematic.themes_mod, "flag_novel", return_value=self._df(0)),
                patch.object(thematic, "emit_domain_metrics") as emit,
            ):
                thematic.extract(date="2026-05-29", news_dir=news_dir, events_dir=events_dir)

            emit.assert_called_once()
            kwargs = emit.call_args.kwargs
            self.assertEqual(kwargs["job"], "thematic-extract")
            metrics = kwargs["metrics"]
            self.assertEqual(metrics['alphalens_thematic_stage_output_rows{stage="extract"}'], 3)
            self.assertEqual(metrics['alphalens_thematic_stage_input_rows{stage="extract"}'], 5)

    def test_map_themes_emits_stage_volume(self) -> None:
        import pandas as pd
        from alphalens_cli.commands import thematic

        novel = pd.DataFrame({"theme": ["quantum", "fusion"]})
        # Columns the map-themes display loop reads after the emit.
        candidates = pd.DataFrame(
            {
                "theme": ["quantum", "quantum", "fusion"],
                "ticker": ["RGTI", "QUBT", "OKLO"],
                "gates_passed": [["press"], ["press"], []],
                "llm_confidence": [0.8, 0.7, 0.6],
                "rationale": ["a", "b", "c"],
            }
        )

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.dict("os.environ", {"OPENROUTER_API_KEY": "k", "POLYGON_API_KEY": "p"}),
                patch.object(thematic.themes_mod, "roll_up", return_value=self._df(2)),
                patch.object(thematic.themes_mod, "flag_novel", return_value=novel),
                patch.object(thematic.orchestrator, "map_themes", return_value=candidates),
                patch.object(thematic, "emit_domain_metrics") as emit,
            ):
                # max_themes/novelty_threshold/window_days must be passed
                # explicitly: a direct call (not via typer) leaves OptionInfo
                # sentinels that break novel.head(max_themes).
                thematic.map_themes_cmd(
                    date="2026-05-29",
                    output_dir=Path(tmp),
                    max_themes=10,
                    novelty_threshold=2.0,
                    window_days=30,
                )

            emit.assert_called_once()
            kwargs = emit.call_args.kwargs
            self.assertEqual(kwargs["job"], "thematic-map-themes")
            metrics = kwargs["metrics"]
            # input = novel themes fed to the mapper; output = candidate rows.
            self.assertEqual(metrics['alphalens_thematic_stage_output_rows{stage="map-themes"}'], 3)
            self.assertEqual(metrics['alphalens_thematic_stage_input_rows{stage="map-themes"}'], 2)

    def test_parquet_num_rows_degrades_to_zero(self) -> None:
        # The extract input gauge reads a parquet footer as an ARGUMENT to
        # _emit_stage_volume (outside its try/except). A missing OR corrupt
        # file must degrade to 0, never raise — a metric read cannot be
        # allowed to crash a stage whose real output is already written.
        from alphalens_cli.commands import thematic

        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.parquet"
            self.assertEqual(thematic._parquet_num_rows(missing), 0)

            corrupt = Path(tmp) / "corrupt.parquet"
            corrupt.write_bytes(b"not a parquet file")
            self.assertEqual(thematic._parquet_num_rows(corrupt), 0)

    def test_score_emits_stage_volume(self) -> None:
        import pandas as pd
        from alphalens_cli.commands import thematic

        candidates = pd.DataFrame({"ticker": ["A", "B", "C", "D"]})
        enriched = pd.DataFrame(
            {"ticker": ["A", "B", "C", "D"], "layer4_weighted_score": [1, 2, 3, 4]}
        )

        with tempfile.TemporaryDirectory() as tmp:
            candidates_dir = Path(tmp) / "candidates"
            output_dir = Path(tmp) / "scored"
            candidates_dir.mkdir()
            (candidates_dir / "2026-05-29.parquet").touch()

            with (
                patch.object(thematic.pd, "read_parquet", return_value=candidates),
                patch.object(thematic.screening_scorer, "score_candidates", return_value=enriched),
                patch.object(thematic, "emit_domain_metrics") as emit,
            ):
                thematic.score(
                    date="2026-05-29", candidates_dir=candidates_dir, output_dir=output_dir
                )

            emit.assert_called_once()
            kwargs = emit.call_args.kwargs
            self.assertEqual(kwargs["job"], "thematic-score")
            metrics = kwargs["metrics"]
            self.assertEqual(metrics['alphalens_thematic_stage_output_rows{stage="score"}'], 4)
            self.assertEqual(metrics['alphalens_thematic_stage_input_rows{stage="score"}'], 4)


class TestCacheRefreshVixEmitsDomainMetrics(unittest.TestCase):
    """``alphalens cache refresh-vix`` emits a freshness gauge (Track A v2
    PR-2 follow-up).

    The metric is a GAUGE carrying the epoch the cache was written, so the
    paired AlphalensVixCache{Stale,MetricMissing} rules can alert when the
    best-effort refresh in run_thematic_day.sh stops landing fresh values
    (which silently degrades ``market_regime_at_entry`` to ``unknown``). The
    emit lives inside ``refresh_vix_cache`` itself — the single place that
    knows ``now`` and has just durably written the cache — so it is exercised
    by a direct call here, no typer round-trip.
    """

    def _series(self):
        import pandas as pd

        # One real observation; refresh_vix_cache takes the last non-null.
        return pd.Series([18.4], index=pd.to_datetime(["2026-06-01"]))

    def test_refresh_vix_emits_fetched_at_gauge(self) -> None:
        import datetime as dt

        from alphalens_cli.commands import cache

        now = dt.datetime(2026, 6, 1, 6, 30, tzinfo=dt.UTC)
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "vix_regime_cache.json"
            with patch.object(cache, "emit_domain_metrics") as emit:
                payload = cache.refresh_vix_cache(cache_path, fred_fetch=self._series, now=now)

            # The cache write is the work; it must still produce the payload.
            self.assertEqual(payload["vix"], 18.4)
            emit.assert_called_once()
            kwargs = emit.call_args.kwargs
            self.assertEqual(kwargs["job"], "vix-cache-refresh")
            metrics = kwargs["metrics"]
            self.assertEqual(
                metrics['alphalens_vix_cache_fetched_at_timestamp_seconds{series="VIXCLS"}'],
                int(now.timestamp()),
            )


class TestPaperReconcileEmitsDomainMetrics(unittest.TestCase):
    """paper-exit hardening: ``alphalens paper reconcile`` emits the
    protection dead-man gauges (filled_without_sl / exits_failed) so the
    AlphalensPaperFilledWithoutSl alert has an input. The emit IS the
    contract — a refactor that drops it silently removes the alert input
    for an unprotected live position.
    """

    def _report(self, **overrides):
        from alphalens_pipeline.paper.reconciler import ReconcileReport

        defaults = {
            "n_orders_checked": 1,
            "n_orders_transitioned": 0,
            "n_fills_appended": 0,
            "n_exits_attached": 2,
            "n_outcomes_written": 0,
            "n_time_stops_fired": 0,
            "n_entries_ttl_canceled": 0,
            "gross_ratio": 0.0,
            "gross_warning_emitted": False,
            "outcomes": (),
            "n_exits_failed": 1,
            "n_entries_canceled": 1,
            "n_filled_without_sl": 1,
            "n_ledger_broker_desync": 1,
        }
        defaults.update(overrides)
        return ReconcileReport(**defaults)

    def test_emit_helper_writes_protection_gauges(self) -> None:
        from alphalens_cli.commands import paper

        report = self._report()
        with patch.object(paper, "emit_domain_metrics") as emit:
            paper._emit_reconcile_metrics(report, account="test")

        emit.assert_called_once()
        kwargs = emit.call_args.kwargs
        self.assertEqual(kwargs["job"], "paper-reconcile")
        metrics = kwargs["metrics"]
        self.assertEqual(metrics['alphalens_paper_filled_without_sl{account="test"}'], 1)
        self.assertEqual(metrics['alphalens_paper_exits_failed{account="test"}'], 1)
        self.assertEqual(metrics['alphalens_paper_entries_canceled{account="test"}'], 1)
        self.assertEqual(metrics['alphalens_paper_exits_attached{account="test"}'], 2)
        self.assertEqual(metrics['alphalens_paper_ledger_broker_desync{account="test"}'], 1)

    def test_emit_helper_swallows_emit_error(self) -> None:
        # The reconcile work is already persisted before the emit; an emit
        # failure must NOT raise (PR #311 callsite rule), so a metrics-dir
        # problem cannot flip the cron-health exit code.
        from alphalens_cli.commands import paper

        report = self._report()
        with patch.object(
            paper, "emit_domain_metrics", side_effect=RuntimeError("bad metrics dir")
        ):
            paper._emit_reconcile_metrics(report, account="main")  # MUST NOT raise

    def test_reconcile_command_emits_via_helper(self) -> None:
        # End-to-end wiring: the reconcile command reaches the emit callsite.
        import datetime as dt

        from alphalens_cli.commands import paper

        report = self._report()
        with (
            patch.object(paper, "_today_utc", return_value=dt.date(2026, 5, 29)),
            patch("alphalens_pipeline.paper.calendar.is_trading_day", return_value=True),
            patch(
                "alphalens_pipeline.paper.broker.get_default_broker_client",
                return_value=object(),
            ),
            patch(
                "alphalens_pipeline.paper.reconciler.reconcile_orders",
                return_value=report,
            ),
            patch.object(paper, "emit_domain_metrics") as emit,
        ):
            paper.reconcile(use_test_account=True)

        emit.assert_called_once()
        self.assertEqual(emit.call_args.kwargs["job"], "paper-reconcile")


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

    def test_thematic_ingest_swallows_emit_oserror(self) -> None:
        # Representative upstream stage: the news parquet is already written
        # by ingest_daily; an emit failure must not fail the unit (same guard
        # as the brief site, applied to every Phase 4 stage emit).
        import pandas as pd
        from alphalens_cli.commands import thematic

        df = pd.DataFrame({"source": ["polygon"], "tickers": [["AAPL"]]})
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(thematic.news_ingest, "ingest_daily", return_value=df),
                patch.object(thematic, "emit_domain_metrics", side_effect=OSError("disk full")),
            ):
                # MUST NOT raise.
                thematic.ingest(date="2026-05-29", cache_dir=Path(tmp))

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

    def test_refresh_vix_swallows_emit_error(self) -> None:
        # The VIX cache JSON is os.replace'd to disk before the emit; ANY emit
        # error must not raise, so the best-effort `|| echo WARN` contract in
        # run_thematic_day.sh still holds and the fresh VIX value is not lost
        # just because observability failed. Use a non-OSError (RuntimeError,
        # e.g. a malformed metrics dict) to pin the broad `except Exception`
        # guard — matching the other emit callsites, not just OSError.
        import datetime as dt

        import pandas as pd
        from alphalens_cli.commands import cache

        series = pd.Series([18.4], index=pd.to_datetime(["2026-06-01"]))
        now = dt.datetime(2026, 6, 1, 6, 30, tzinfo=dt.UTC)
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "vix_regime_cache.json"
            with patch.object(
                cache, "emit_domain_metrics", side_effect=RuntimeError("bad metrics dict")
            ):
                payload = cache.refresh_vix_cache(cache_path, fred_fetch=lambda: series, now=now)
            # MUST NOT raise; the cache write succeeded.
            self.assertEqual(payload["vix"], 18.4)
            self.assertTrue(cache_path.exists())


class TestThematicIngestEmitsSourceRows(unittest.TestCase):
    """#384 EX-99.1 dead-man-switch: ``thematic ingest`` emits a RAW per-source
    row-count gauge ``alphalens_thematic_source_rows{source=...}`` folded into
    the SAME single thematic-ingest emit as the stage-volume gauges (one
    textfile, one atomic write). A sustained ``edgar_press_release=0`` trips
    ``AlphalensEdgarPressReleaseDark``. The count is the RAW pre-dedup out-param
    value, NOT the post-dedup aggregate (which undercounts edgar).
    """

    def test_ingest_folds_per_source_raw_counts_into_single_emit(self) -> None:
        import pandas as pd
        from alphalens_cli.commands import thematic

        df = pd.DataFrame({"source": ["polygon", "rss"], "tickers": [["AAPL"], ["MSFT"]]})

        def fake_ingest(*args, source_row_counts=None, **kwargs):
            if source_row_counts is not None:
                source_row_counts.update(
                    {"edgar_press_release": 0, "polygon": 1, "gdelt": 0, "rss": 1}
                )
            return df

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(thematic.news_ingest, "ingest_daily", side_effect=fake_ingest),
                patch.object(thematic, "emit_domain_metrics") as emit,
            ):
                thematic.ingest(date="2026-05-29", cache_dir=Path(tmp))

            emit.assert_called_once()
            kwargs = emit.call_args.kwargs
            self.assertEqual(kwargs["job"], "thematic-ingest")
            metrics = kwargs["metrics"]
            # Stage gauges still present (folded, not replaced).
            self.assertEqual(metrics['alphalens_thematic_stage_output_rows{stage="ingest"}'], 2)
            # Per-source raw gauges — edgar=0 is the load-bearing signal.
            self.assertEqual(
                metrics['alphalens_thematic_source_rows{source="edgar_press_release"}'], 0
            )
            self.assertEqual(metrics['alphalens_thematic_source_rows{source="polygon"}'], 1)
            self.assertEqual(metrics['alphalens_thematic_source_rows{source="gdelt"}'], 0)
            self.assertEqual(metrics['alphalens_thematic_source_rows{source="rss"}'], 1)

    def test_ingest_passes_a_dict_to_ingest_daily(self) -> None:
        import pandas as pd
        from alphalens_cli.commands import thematic

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(
                    thematic.news_ingest,
                    "ingest_daily",
                    return_value=pd.DataFrame({"source": [], "tickers": []}),
                ) as ingest_daily,
                patch.object(thematic, "emit_domain_metrics"),
            ):
                thematic.ingest(date="2026-05-29", cache_dir=Path(tmp))

            kwargs = ingest_daily.call_args.kwargs
            self.assertIn("source_row_counts", kwargs)
            self.assertIsInstance(kwargs["source_row_counts"], dict)

    def test_source_rows_land_in_prom_file_with_edgar_label(self) -> None:
        # End-to-end through the REAL emitter so the exposition-format label
        # string is exercised (the mock tests never serialize). edgar=0 must be
        # written, not skipped.
        import os

        import pandas as pd
        from alphalens_cli.commands import thematic

        df = pd.DataFrame({"source": ["polygon"], "tickers": [["AAPL"]]})

        def fake_ingest(*args, source_row_counts=None, **kwargs):
            if source_row_counts is not None:
                source_row_counts.update(
                    {"edgar_press_release": 0, "polygon": 1, "gdelt": 0, "rss": 0}
                )
            return df

        with tempfile.TemporaryDirectory() as tmp:
            metrics_dir = Path(tmp) / "metrics"
            with (
                patch.dict(os.environ, {"ALPHALENS_TEXTFILE_DIR": str(metrics_dir)}),
                patch.object(thematic.news_ingest, "ingest_daily", side_effect=fake_ingest),
            ):
                thematic.ingest(date="2026-05-29", cache_dir=Path(tmp) / "news")

            prom = (metrics_dir / "alphalens_domain_thematic-ingest.prom").read_text()
            self.assertIn('alphalens_thematic_source_rows{source="edgar_press_release"} 0', prom)
            self.assertIn('alphalens_thematic_source_rows{source="polygon"} 1', prom)

    def test_emit_failure_does_not_crash_ingest(self) -> None:
        # PR #311 rule: the parquet is already written; an emit failure must not
        # fail the unit (the folded emit shares the same try/except).
        import pandas as pd
        from alphalens_cli.commands import thematic

        df = pd.DataFrame({"source": ["polygon"], "tickers": [["AAPL"]]})
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(thematic.news_ingest, "ingest_daily", return_value=df),
                patch.object(thematic, "emit_domain_metrics", side_effect=OSError("disk full")),
            ):
                thematic.ingest(date="2026-05-29", cache_dir=Path(tmp))  # MUST NOT raise


class TestBackfillRefreshesExecutionTelemetry(unittest.TestCase):
    """v3 PR-3: the nightly ``backfill-shadow-returns`` tail re-emits the
    execution-quality gauges via ``_refresh_execution_telemetry(ledger)``.

    A future refactor that drops the tail call would silently freeze the
    telemetry at its last value (no test failure, stale dashboard). The call
    IS the contract. The pricing sweep is stubbed to a no-op so no Polygon /
    network call happens — the test isolates the wiring, not the sweep.
    """

    def test_backfill_tail_refreshes_telemetry_with_ledger_path(self) -> None:
        from types import SimpleNamespace

        from alphalens_cli.commands import feedback
        from alphalens_cli.main import app
        from typer.testing import CliRunner

        runner = CliRunner()
        recorded: list[Path] = []

        def fake_refresh(ledger: Path):
            recorded.append(ledger)

        # No matured/pending dates → the sweep does no pricing at all; the
        # command still reaches its tail. One stub report keeps the summary
        # echo's start/end indexing valid.
        import datetime as dt

        report = SimpleNamespace(
            matured=False,
            n_priced=0,
            brief_date=dt.date(2026, 5, 20),
        )

        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "feedback.db"
            paper_ledger = Path(tmp) / "paper_ledger.db"
            with (
                patch.object(
                    feedback,
                    "_refresh_execution_telemetry",
                    side_effect=fake_refresh,
                ),
                patch(
                    "alphalens_pipeline.feedback.shadow_return.compute_shadow_returns_window",
                    return_value=[report],
                ),
            ):
                result = runner.invoke(
                    app,
                    [
                        "feedback",
                        "backfill-shadow-returns",
                        "--ledger",
                        str(ledger),
                        "--paper-ledger",
                        str(paper_ledger),
                    ],
                )

        self.assertEqual(result.exit_code, 0, result.stdout)
        self.assertEqual(recorded, [ledger])


if __name__ == "__main__":
    unittest.main()
