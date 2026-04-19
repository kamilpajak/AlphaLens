"""Integration test: CLI wiring — verify typer subcommands register and
factory functions resolve their dependencies without ImportError.
"""

import os
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

import typer

OUR_PACKAGE_PREFIX = "alphalens"


class TestTyperAppRegistration(unittest.TestCase):
    def test_watchdog_app_is_typer(self):
        from alphalens_cli.watchdog_main import watchdog_app

        self.assertIsInstance(watchdog_app, typer.Typer)

    def test_all_subcommands_registered(self):
        from alphalens_cli.watchdog_main import watchdog_app

        names = {cmd.name for cmd in watchdog_app.registered_commands}
        self.assertEqual(
            names,
            {"run-once", "process-queue", "momentum-screen", "momentum-status",
             "lean-screen", "backtest", "validate-llm-filter", "status"},
            f"missing or extra subcommands: {names}",
        )


class TestBuilderFactoriesResolveLazyImports(unittest.TestCase):
    """Invoke the factory functions — if a lazy import is broken, this raises ImportError."""

    def setUp(self):
        self.env_patches = {
            "TELEGRAM_BOT_TOKEN": "test-token",
            "TELEGRAM_CHAT_ID": "test-chat",
            "WATCHDOG_USER_AGENT": "test-ua",
        }

    @patch.dict(os.environ, {}, clear=False)
    def test_build_watchdog_lazy_imports_resolve(self):
        os.environ.update(self.env_patches)

        cik_loader_cls = f"{OUR_PACKAGE_PREFIX}.watchdog.sources.cik_loader.CIKLoader"
        portfolio_cls = f"{OUR_PACKAGE_PREFIX}.watchdog.portfolio.PortfolioState"

        with patch(cik_loader_cls) as mock_cik, patch(portfolio_cls) as mock_portfolio:
            mock_cik.return_value.load = MagicMock()
            mock_portfolio.load.return_value = MagicMock(
                held=["AAPL"], watchlist=["MSFT"]
            )
            from alphalens_cli.watchdog_main import _build_watchdog

            try:
                _build_watchdog()
            except ImportError:
                raise
            except Exception:
                # Other failures (DB, filesystem) are acceptable — this test only
                # asserts that imports resolved. The module got loaded successfully.
                pass

    @patch.dict(os.environ, {}, clear=False)
    def test_build_worker_resolves_without_import_error(self):
        os.environ.update(self.env_patches)

        runner_cls = f"{OUR_PACKAGE_PREFIX}.runner.TradingAgentsRunner"
        queue_cls = f"{OUR_PACKAGE_PREFIX}.queue.CandidateQueue"

        with patch(runner_cls) as mock_runner, patch(queue_cls) as mock_queue:
            mock_runner.return_value = MagicMock()
            mock_queue.return_value = MagicMock()
            from alphalens_cli.watchdog_main import _build_worker

            try:
                _build_worker()
            except ImportError:
                raise
            except Exception:
                pass


class TestRootTyperSubprocessSmoke(unittest.TestCase):
    """Prove the root typer app (cli/main.py) wires together without import errors.

    Runs in a subprocess to get a clean interpreter — module-level side effects
    in other tests can mask this one.
    """

    def test_root_app_imports_cleanly(self):
        result = subprocess.run(
            [sys.executable, "-c", "from alphalens_cli.main import app; assert app is not None"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"alphalens_cli.main import failed:\nstdout: {result.stdout}\nstderr: {result.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
