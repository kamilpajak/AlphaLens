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
    def test_each_group_is_typer(self):
        from alphalens_cli.commands.archive import archive_app
        from alphalens_cli.commands.research import research_app
        from alphalens_cli.commands.watchdog import watchdog_app

        for app in (watchdog_app, archive_app, research_app):
            self.assertIsInstance(app, typer.Typer)

    def test_watchdog_commands(self):
        from alphalens_cli.commands.watchdog import watchdog_app

        names = {cmd.name for cmd in watchdog_app.registered_commands}
        self.assertEqual(names, {"run-once"})

    def test_archive_groups_dispatch_themed_insider_rotation(self):
        # ARCHIVED layer replay (per ADR 0005) lives under `alphalens archive`.
        # Verify the aggregator wires all three dispatched sub-apps.
        from alphalens_cli.commands.archive import archive_app

        groups = {grp.name for grp in archive_app.registered_groups}
        self.assertEqual(groups, {"themed", "insider", "rotation"})

    def test_themed_commands(self):
        from alphalens_cli.commands.themed import themed_app

        names = {cmd.name for cmd in themed_app.registered_commands}
        self.assertEqual(names, {"screen", "status"})

    def test_research_commands(self):
        from alphalens_cli.commands.research import research_app

        names = {cmd.name for cmd in research_app.registered_commands}
        self.assertEqual(
            names,
            {
                "validate-llm-filter",
                "survivorship-pit",
                "walk-forward",
                "cost-validation",
            },
        )

    def test_root_app_top_level_commands(self):
        from alphalens_cli.main import app

        names = {cmd.name for cmd in app.registered_commands}
        self.assertEqual(names, {"status", "backtest", "audit", "preaudit"})


class TestBuilderFactoriesResolveLazyImports(unittest.TestCase):
    """Invoke the factory functions — if a lazy import is broken, this raises ImportError."""

    def setUp(self):
        self.env_patches = {
            "TELEGRAM_BOT_TOKEN": "test-token",
            "TELEGRAM_CHAT_ID": "test-chat",
        }

    @patch.dict(os.environ, {}, clear=False)
    def test_build_watchdog_lazy_imports_resolve(self):
        os.environ.update(self.env_patches)

        cik_loader_cls = f"{OUR_PACKAGE_PREFIX}.watchdog.sources.cik_loader.CIKLoader"
        portfolio_cls = f"{OUR_PACKAGE_PREFIX}.watchdog.portfolio.PortfolioState"

        with patch(cik_loader_cls) as mock_cik, patch(portfolio_cls) as mock_portfolio:
            mock_cik.return_value.load = MagicMock()
            mock_portfolio.load.return_value = MagicMock(held=["AAPL"], watchlist=["MSFT"])
            from alphalens_cli.commands.watchdog import _build_watchdog

            try:
                _build_watchdog()
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
            [
                sys.executable,
                "-c",
                "from alphalens_cli.main import app; assert app is not None",
            ],
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
