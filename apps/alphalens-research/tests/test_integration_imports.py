"""Integration test: every submodule under both workspace packages imports without error.

Walks the top-level subpackages of ``alphalens_pipeline`` + ``alphalens_research``
and a few representative screener pipelines. Catches 99% of import-rename
errors cheaply without hand-listing modules.
"""

import importlib
import importlib.util
import pkgutil
import unittest

# (package_root, [subpackages_to_walk]). Each entry is fully qualified.
WALK_TARGETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "alphalens_pipeline",
        ("edgar_detector", "thematic", "data", "core", "scorers", "literature_scanner"),
    ),
    (
        "alphalens_research",
        (
            "backtest",
            "attribution",
            "overlays",
            "gates",
            "preaudit",
            "diagnostics",
            "paper_trade",
            "screeners",
            "screeners.prescreener",
            "screeners.momentum_lowvol",
            "screeners.insider_activity",
        ),
    ),
)


def _all_qualified_names() -> tuple[str, ...]:
    return tuple(f"{prefix}.{sub}" for prefix, subs in WALK_TARGETS for sub in subs)


class TestPackageImports(unittest.TestCase):
    def test_each_subpackage_importable(self):
        for name in _all_qualified_names():
            with self.subTest(package=name):
                mod = importlib.import_module(name)
                self.assertTrue(hasattr(mod, "__path__"), f"{name} should be a package")

    def test_find_spec_resolves_in_installed_context(self):
        """Catches pyproject.toml packages.find.include omissions.

        find_spec uses the installed distribution metadata — if a sub-package isn't
        globbed into the wheel, this fails while plain import still works from cwd.
        """
        for name in _all_qualified_names():
            with self.subTest(package=name):
                spec = importlib.util.find_spec(name)
                self.assertIsNotNone(spec, f"{name} not installed (packages.find.include?)")

    def test_every_submodule_imports(self):
        """Walk each package and try to import every .py file (excluding __pycache__)."""
        failures: list[tuple[str, Exception]] = []
        # Walk only top-level subpackages (avoid double-walking when a screener
        # subpackage is also listed explicitly).
        for prefix, subs in WALK_TARGETS:
            top_subs = tuple(s for s in subs if "." not in s)
            for sub in top_subs:
                pkg = importlib.import_module(f"{prefix}.{sub}")
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
