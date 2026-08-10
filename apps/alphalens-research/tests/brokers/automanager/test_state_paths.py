"""Hermetic tests for the broker-state path seam (ADR 0016 / design memo D2-D4).

Every function resolves ``Path.home()`` and ``$ALPHALENS_BROKER_ENVIRONMENT``
at CALL time, so these tests patch ``pathlib.Path.home`` to a temp directory
and ``os.environ`` per-test rather than relying on import-time constants —
mirrors the module's own no-import-time-Path-constants contract (memo D2).
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from alphalens_pipeline.brokers.automanager.state_paths import (
    BROKER_ENVIRONMENT_ENV,
    ENV_LIVE,
    ENV_SIM,
    LEGACY_FLAT_STATE_FILENAMES,
    BrokerStateLayoutError,
    assert_no_legacy_flat_state,
    broker_environment,
    broker_orders_root,
    exec_quality_parquet,
    global_kill_file_path,
    kill_file_path,
    metrics_job,
    picks_path,
    price_stream_metrics_job,
    standalone_stops_path,
    stream_metrics_job,
    submissions_path,
)


class HomeDirTestCase(unittest.TestCase):
    """Base class: patches Path.home() to an isolated temp directory."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        patcher = mock.patch("pathlib.Path.home", return_value=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)


class BrokerEnvironmentTest(HomeDirTestCase):
    @mock.patch.dict(os.environ, {}, clear=False)
    def test_default_is_sim(self) -> None:
        os.environ.pop(BROKER_ENVIRONMENT_ENV, None)
        self.assertEqual(broker_environment(), ENV_SIM)

    @mock.patch.dict(os.environ, {BROKER_ENVIRONMENT_ENV: "live"}, clear=False)
    def test_explicit_live_from_env_var(self) -> None:
        self.assertEqual(broker_environment(), ENV_LIVE)

    @mock.patch.dict(os.environ, {BROKER_ENVIRONMENT_ENV: "prod"}, clear=False)
    def test_invalid_value_from_env_var_raises(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            broker_environment()
        message = str(ctx.exception)
        self.assertIn(BROKER_ENVIRONMENT_ENV, message)
        self.assertIn("prod", message)

    def test_invalid_explicit_env_argument_raises(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            broker_orders_root(env="prod")
        message = str(ctx.exception)
        self.assertIn(BROKER_ENVIRONMENT_ENV, message)
        self.assertIn("prod", message)

    def test_explicit_env_argument_overrides_env_var(self) -> None:
        with mock.patch.dict(os.environ, {BROKER_ENVIRONMENT_ENV: "sim"}, clear=False):
            self.assertEqual(
                broker_orders_root(env=ENV_LIVE),
                self.home / ".alphalens" / "broker_orders" / "live",
            )


class PerEnvRootsAndFilesTest(HomeDirTestCase):
    def test_broker_orders_root_default_sim(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(BROKER_ENVIRONMENT_ENV, None)
            self.assertEqual(
                broker_orders_root(), self.home / ".alphalens" / "broker_orders" / "sim"
            )

    def test_broker_orders_root_live(self) -> None:
        self.assertEqual(
            broker_orders_root(env=ENV_LIVE),
            self.home / ".alphalens" / "broker_orders" / "live",
        )

    def test_submissions_path_per_env(self) -> None:
        self.assertEqual(
            submissions_path(env=ENV_SIM), broker_orders_root(env=ENV_SIM) / "submissions.jsonl"
        )
        self.assertEqual(
            submissions_path(env=ENV_LIVE), broker_orders_root(env=ENV_LIVE) / "submissions.jsonl"
        )

    def test_picks_path_per_env(self) -> None:
        self.assertEqual(picks_path(env=ENV_SIM), broker_orders_root(env=ENV_SIM) / "picks.jsonl")
        self.assertEqual(picks_path(env=ENV_LIVE), broker_orders_root(env=ENV_LIVE) / "picks.jsonl")

    def test_standalone_stops_path_per_env(self) -> None:
        self.assertEqual(
            standalone_stops_path(env=ENV_SIM),
            broker_orders_root(env=ENV_SIM) / "standalone_stops.jsonl",
        )
        self.assertEqual(
            standalone_stops_path(env=ENV_LIVE),
            broker_orders_root(env=ENV_LIVE) / "standalone_stops.jsonl",
        )

    def test_exec_quality_parquet_per_env(self) -> None:
        self.assertEqual(
            exec_quality_parquet(env=ENV_SIM),
            self.home / ".alphalens" / "exec_quality" / "sim" / "tranche_fills.parquet",
        )
        self.assertEqual(
            exec_quality_parquet(env=ENV_LIVE),
            self.home / ".alphalens" / "exec_quality" / "live" / "tranche_fills.parquet",
        )


class KillFilePathsTest(HomeDirTestCase):
    def test_instance_kill_path_is_per_env(self) -> None:
        self.assertEqual(kill_file_path(env=ENV_SIM), broker_orders_root(env=ENV_SIM) / "KILL")
        self.assertEqual(kill_file_path(env=ENV_LIVE), broker_orders_root(env=ENV_LIVE) / "KILL")
        self.assertNotEqual(kill_file_path(env=ENV_SIM), kill_file_path(env=ENV_LIVE))

    def test_global_kill_path_is_parent_level_and_env_independent(self) -> None:
        expected = self.home / ".alphalens" / "broker_orders" / "KILL"
        self.assertEqual(global_kill_file_path(), expected)
        # No env-scoping: the global kill sits one level above every instance root.
        self.assertNotEqual(global_kill_file_path(), kill_file_path(env=ENV_SIM))
        self.assertNotEqual(global_kill_file_path(), kill_file_path(env=ENV_LIVE))


class LegacyFlatStateGuardTest(HomeDirTestCase):
    def _root(self) -> Path:
        return self.home / ".alphalens" / "broker_orders"

    def test_flat_submissions_file_raises(self) -> None:
        root = self._root()
        root.mkdir(parents=True)
        (root / "submissions.jsonl").write_text("{}\n")
        with self.assertRaises(BrokerStateLayoutError) as ctx:
            assert_no_legacy_flat_state()
        message = str(ctx.exception)
        self.assertIn("submissions.jsonl", message)
        self.assertIn("sim", message)  # migration hint names the target subdir

    def test_flat_picks_file_raises(self) -> None:
        root = self._root()
        root.mkdir(parents=True)
        (root / "picks.jsonl").write_text("{}\n")
        with self.assertRaises(BrokerStateLayoutError):
            assert_no_legacy_flat_state()

    def test_flat_standalone_stops_file_raises(self) -> None:
        root = self._root()
        root.mkdir(parents=True)
        (root / "standalone_stops.jsonl").write_text("{}\n")
        with self.assertRaises(BrokerStateLayoutError):
            assert_no_legacy_flat_state()

    def test_clean_nested_layout_passes(self) -> None:
        root = self._root()
        (root / ENV_SIM).mkdir(parents=True)
        (root / ENV_SIM / "submissions.jsonl").write_text("{}\n")
        assert_no_legacy_flat_state()  # must not raise

    def test_missing_broker_orders_dir_passes(self) -> None:
        assert_no_legacy_flat_state()  # nothing on disk at all -> must not raise

    def test_empty_broker_orders_dir_passes(self) -> None:
        self._root().mkdir(parents=True)
        assert_no_legacy_flat_state()  # must not raise

    def test_flat_kill_file_alone_is_not_legacy(self) -> None:
        root = self._root()
        root.mkdir(parents=True)
        (root / "KILL").write_text("")
        assert_no_legacy_flat_state()  # global KILL is expected flat -> must not raise

    def test_legacy_filenames_tuple_matches_the_three_journals(self) -> None:
        self.assertEqual(
            LEGACY_FLAT_STATE_FILENAMES,
            ("submissions.jsonl", "picks.jsonl", "standalone_stops.jsonl"),
        )


class JobNameDerivationTest(unittest.TestCase):
    def test_metrics_job_sim(self) -> None:
        self.assertEqual(metrics_job(env=ENV_SIM), "broker-manager-sim")

    def test_metrics_job_live(self) -> None:
        self.assertEqual(metrics_job(env=ENV_LIVE), "broker-manager-live")

    def test_stream_metrics_job_sim(self) -> None:
        self.assertEqual(stream_metrics_job(env=ENV_SIM), "broker-manager-sim-stream")

    def test_stream_metrics_job_live(self) -> None:
        self.assertEqual(stream_metrics_job(env=ENV_LIVE), "broker-manager-live-stream")

    def test_price_stream_metrics_job_sim(self) -> None:
        self.assertEqual(price_stream_metrics_job(env=ENV_SIM), "live-price-stream-sim")

    def test_price_stream_metrics_job_live(self) -> None:
        self.assertEqual(price_stream_metrics_job(env=ENV_LIVE), "live-price-stream-live")

    @mock.patch.dict(os.environ, {BROKER_ENVIRONMENT_ENV: "live"}, clear=False)
    def test_job_names_default_to_resolved_environment(self) -> None:
        self.assertEqual(metrics_job(), "broker-manager-live")
        self.assertEqual(stream_metrics_job(), "broker-manager-live-stream")
        self.assertEqual(price_stream_metrics_job(), "live-price-stream-live")


if __name__ == "__main__":
    unittest.main()
