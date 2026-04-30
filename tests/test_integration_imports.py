"""Integration test: every submodule under alphalens imports without error.

Walks the top-level subpackages and `screeners/*` pipelines. Catches 99% of
import-rename errors cheaply without hand-listing modules.
"""

import importlib
import importlib.util
import pkgutil
import unittest

OUR_PACKAGE_PREFIX = "alphalens"

TOP_LEVEL_SUBPACKAGES = ("watchdog", "backtest", "screeners", "archive")
SCREENER_SUBPACKAGES = (
    "screeners.prescreener",
    "screeners.momentum_lowvol",
    "archive.screeners.themed",
    "archive.screeners.lean",
    "archive.screeners.insider",
)


class TestPackageImports(unittest.TestCase):
    def test_each_subpackage_importable(self):
        for sub in TOP_LEVEL_SUBPACKAGES + SCREENER_SUBPACKAGES:
            name = f"{OUR_PACKAGE_PREFIX}.{sub}"
            with self.subTest(package=name):
                mod = importlib.import_module(name)
                self.assertTrue(hasattr(mod, "__path__"), f"{name} should be a package")

    def test_find_spec_resolves_in_installed_context(self):
        """Catches pyproject.toml packages.find.include omissions.

        find_spec uses the installed distribution metadata — if alphalens/ isn't
        globbed into the wheel, this fails while plain import still works from cwd.
        """
        for sub in TOP_LEVEL_SUBPACKAGES + SCREENER_SUBPACKAGES:
            name = f"{OUR_PACKAGE_PREFIX}.{sub}"
            with self.subTest(package=name):
                spec = importlib.util.find_spec(name)
                self.assertIsNotNone(spec, f"{name} not installed (packages.find.include?)")

    def test_every_submodule_imports(self):
        """Walk each package and try to import every .py file (excluding __pycache__)."""
        # lean_project/ is Lean Docker-side code — imports AlgorithmImports which
        # is only provided inside the Lean container. Expected to fail on host.
        docker_only = {"alphalens.archive.screeners.lean.lean_project"}
        failures: list[tuple[str, Exception]] = []
        for sub in TOP_LEVEL_SUBPACKAGES:
            pkg = importlib.import_module(f"{OUR_PACKAGE_PREFIX}.{sub}")
            for module_info in pkgutil.walk_packages(pkg.__path__, prefix=f"{pkg.__name__}."):
                if any(module_info.name.startswith(p) for p in docker_only):
                    continue
                try:
                    importlib.import_module(module_info.name)
                except Exception as exc:
                    failures.append((module_info.name, exc))
        if failures:
            lines = "\n".join(f"  {name}: {type(exc).__name__}: {exc}" for name, exc in failures)
            self.fail(f"{len(failures)} modules failed to import:\n{lines}")


class TestThemedScreenerPackageData(unittest.TestCase):
    """Catch regressions where universe.yaml stops shipping with the package."""

    def test_universe_yaml_reachable(self):
        mod = importlib.import_module(f"{OUR_PACKAGE_PREFIX}.archive.screeners.themed.config")
        self.assertTrue(
            mod.UNIVERSE_PATH.exists(),
            f"universe.yaml not found at {mod.UNIVERSE_PATH} — package_data regression?",
        )


if __name__ == "__main__":
    unittest.main()
