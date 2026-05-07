"""Tests for the paper-trade strategy registry contract."""

from __future__ import annotations

import importlib
import unittest

from alphalens.paper_trade.registry import (
    REGISTRY,
    Strategy,
    default_paper_trade_dir,
    get_strategy,
    resolve_callable,
)


class StrategyDataclassTests(unittest.TestCase):
    def test_strategy_is_frozen_dataclass(self):
        s = Strategy(
            id="x",
            description="d",
            ledger_filename="x_ledger.parquet",
            state_filename="x_state.yaml",
            verdict_filename="x_verdict.md",
            scorer_callable_path="some.module:fn",
            universe_callable_path="some.module:univ",
            refresh_callable_path="some.module:refresh",
        )
        with self.assertRaises(Exception):  # frozen dataclass — assignment forbidden
            s.id = "y"  # type: ignore[misc]


class RegistryShapeTests(unittest.TestCase):
    def test_v9d_present(self):
        self.assertIn("v9d", REGISTRY)

    def test_v9d_filenames_match_disk(self):
        cfg = REGISTRY["v9d"]
        self.assertEqual(cfg.ledger_filename, "v9d_ledger.parquet")
        self.assertEqual(cfg.state_filename, "v9d_state.yaml")
        self.assertEqual(cfg.verdict_filename, "v9d_verdict.md")

    def test_v9d_callable_paths_resolve(self):
        cfg = REGISTRY["v9d"]
        for path in (
            cfg.scorer_callable_path,
            cfg.universe_callable_path,
            cfg.refresh_callable_path,
        ):
            module_path, _, fn_name = path.partition(":")
            self.assertTrue(module_path and fn_name, f"malformed: {path!r}")
            module = importlib.import_module(module_path)
            self.assertTrue(
                hasattr(module, fn_name),
                f"{module_path} missing {fn_name!r}",
            )


class GetStrategyTests(unittest.TestCase):
    def test_known_strategy_returns_entry(self):
        cfg = get_strategy("v9d")
        self.assertEqual(cfg.id, "v9d")

    def test_unknown_strategy_raises_with_choices(self):
        with self.assertRaises(KeyError) as cm:
            get_strategy("nonexistent")
        self.assertIn("Choices", str(cm.exception))


class ResolveCallableTests(unittest.TestCase):
    def test_valid_path_resolves(self):
        # Use a stable stdlib symbol to avoid binding to project internals.
        fn = resolve_callable("os.path:join")
        self.assertEqual(fn("a", "b"), "a/b")

    def test_missing_separator_raises(self):
        with self.assertRaises(ValueError):
            resolve_callable("os.path.join")

    def test_empty_module_raises(self):
        with self.assertRaises(ValueError):
            resolve_callable(":fn")

    def test_unknown_attr_raises_attribute_error(self):
        with self.assertRaises(AttributeError):
            resolve_callable("os.path:does_not_exist_zzz")


class DefaultPaperTradeDirTests(unittest.TestCase):
    def test_under_alphalens_home(self):
        p = default_paper_trade_dir()
        self.assertEqual(p.name, "paper_trade")
        self.assertIn(".alphalens", str(p))


if __name__ == "__main__":
    unittest.main()
