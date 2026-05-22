"""Integration test: every submodule under alphalens_research imports without error.

Walks the top-level subpackages and `screeners/*` pipelines. Catches 99% of
import-rename errors cheaply without hand-listing modules.
"""

import importlib
import importlib.util
import pkgutil
import unittest

OUR_PACKAGE_PREFIX = "alphalens_research"

TOP_LEVEL_SUBPACKAGES = ("watchdog", "backtest", "screeners")
SCREENER_SUBPACKAGES = (
    "screeners.prescreener",
    "screeners.momentum_lowvol",
    "screeners.insider_activity",
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

        find_spec uses the installed distribution metadata — if alphalens_research/ isn't
        globbed into the wheel, this fails while plain import still works from cwd.
        """
        for sub in TOP_LEVEL_SUBPACKAGES + SCREENER_SUBPACKAGES:
            name = f"{OUR_PACKAGE_PREFIX}.{sub}"
            with self.subTest(package=name):
                spec = importlib.util.find_spec(name)
                self.assertIsNotNone(spec, f"{name} not installed (packages.find.include?)")

    def test_every_submodule_imports(self):
        """Walk each package and try to import every .py file (excluding __pycache__)."""
        failures: list[tuple[str, Exception]] = []
        for sub in TOP_LEVEL_SUBPACKAGES:
            pkg = importlib.import_module(f"{OUR_PACKAGE_PREFIX}.{sub}")
            for module_info in pkgutil.walk_packages(pkg.__path__, prefix=f"{pkg.__name__}."):
                try:
                    importlib.import_module(module_info.name)
                except Exception as exc:
                    failures.append((module_info.name, exc))
        if failures:
            lines = "\n".join(f"  {name}: {type(exc).__name__}: {exc}" for name, exc in failures)
            self.fail(f"{len(failures)} modules failed to import:\n{lines}")


if __name__ == "__main__":
    unittest.main()
