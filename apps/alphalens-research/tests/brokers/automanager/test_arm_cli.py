"""CLI tests for `alphalens broker arm`.

PR-7 (broker-manager extraction memo section 5): arm now parses the brief's
trade_setup into a full TradeIntent CLIENT-SIDE (parse_brief_to_spec +
build_exit_geometry_spec) and persists it via arm_pick — the daemon never
touches a brief. Loading is lazy-imported inside the command body, so patches
target the SOURCE modules.

Pure executor (2026-08-03): arm carries NO selection / filtering logic — it
arms exactly the named ticker after only the structural checks (brief present,
ticker present, trade_setup plannable). The old arm-time earnings-window gate
was removed: selection-policy filters (earnings-window avoidance included)
belong at brief-creation (the selection tier), so a filtered-out candidate
never reaches the brief in the first place. The client invoking arm is
responsible for knowing what it arms; the command never second-guesses it.

ADR 0016 / design memo D6 (2026-08-10): arm gains ``--env {sim,live}``
(default sim) choosing which instance inbox (``<env>/picks.jsonl``) the
armed intent is persisted into, via the ``state_paths`` seam.
"""

from __future__ import annotations

import ast
import datetime as dt
import inspect
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from alphalens_pipeline.paper.brief_loader import CandidateBrief
from typer.testing import CliRunner

_BRIEF_DATE = dt.date(2026, 7, 20)


def _plannable_trade_setup() -> dict:
    return {
        "schema_version": "1.0.0",
        "status": "OK",
        "asof_close": 100.0,
        "atr": 1.5,
        "disaster_stop": 90.0,
        "suggested_size_pct": 3.0,
        "entry_tiers": [
            {"limit": 100.0, "alloc_pct": 60.0, "tag": "T1"},
            {"limit": 98.0, "alloc_pct": 40.0, "tag": "T2"},
        ],
        "tp_tranches": [
            {"target": 110.0, "tranche_pct": 100.0, "r_multiple": 2.0, "tag": "TP1"},
        ],
    }


def _candidate(ticker: str = "KO", *, trade_setup: dict | None = "__default__") -> CandidateBrief:
    if trade_setup == "__default__":
        trade_setup = _plannable_trade_setup()
    return CandidateBrief(
        brief_date=_BRIEF_DATE,
        ticker=ticker,
        theme="test-theme",
        verified=True,
        suggested_size_pct=3.0,
        trade_setup=trade_setup,
        n_gates_passed=3,
        n_gates_failed=0,
        layer4_weighted_score=1.0,
        scorer_config_version="scorer-v1-test",
    )


def _isolate_home(case: unittest.TestCase) -> Path:
    """Patch ``Path.home()`` to a fresh, empty temp directory for ``case``.

    ``arm`` now runs the legacy-layout guard (``state_paths.
    assert_no_legacy_flat_state``, ADR 0016 D4) before persisting a pick —
    every test that invokes ``arm`` must be isolated from the REAL
    ``~/.alphalens/broker_orders/`` tree, which on a developer machine
    running the live SIM daemon genuinely holds a pre-ADR-0016 flat layout
    and would otherwise make these hermetic tests fail non-deterministically
    depending on host state."""
    tmp = TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    home = Path(tmp.name)
    patcher = mock.patch("pathlib.Path.home", return_value=home)
    patcher.start()
    case.addCleanup(patcher.stop)
    return home


def _seed_legacy_flat_state(home: Path) -> Path:
    """Write ONE pre-migration flat legacy journal file under ``home`` —
    enough to trip ``assert_no_legacy_flat_state`` (ADR 0016 D4)."""
    legacy_dir = home / ".alphalens" / "broker_orders"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    legacy_file = legacy_dir / "submissions.jsonl"
    legacy_file.write_text("", encoding="utf-8")
    return legacy_file


class ArmCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()
        self.home = _isolate_home(self)

    def test_arm_valid_pick_appends_and_exits_zero(self) -> None:
        from alphalens_cli.commands.broker import broker_app

        with (
            mock.patch(
                "alphalens_pipeline.paper.brief_loader.load_brief",
                return_value=[_candidate("KO"), _candidate("MU")],
            ),
            mock.patch("alphalens_pipeline.brokers.automanager.picks.arm_pick") as arm,
        ):
            result = self.runner.invoke(broker_app, ["arm", "ko", "--date", "2026-07-20"])
        self.assertEqual(result.exit_code, 0, result.output)
        arm.assert_called_once()
        (intent,), _kwargs = arm.call_args
        self.assertEqual(intent.instrument.ticker, "KO")
        self.assertEqual(intent.meta.brief_date, "2026-07-20")
        self.assertEqual(intent.spec.disaster_stop, 90.0)
        self.assertEqual(len(intent.spec.entry_tiers), 2)
        self.assertIsNotNone(intent.exit)
        self.assertIn("armed KO", result.output)

    def test_arm_ticker_absent_from_brief_refuses(self) -> None:
        from alphalens_cli.commands.broker import broker_app

        with (
            mock.patch(
                "alphalens_pipeline.paper.brief_loader.load_brief", return_value=[_candidate("KO")]
            ),
            mock.patch("alphalens_pipeline.brokers.automanager.picks.arm_pick") as arm,
        ):
            result = self.runner.invoke(broker_app, ["arm", "ZZZZ", "--date", "2026-07-20"])
        self.assertEqual(result.exit_code, 1)
        self.assertIn("not in the 2026-07-20 brief", result.output)
        arm.assert_not_called()

    def test_arm_bad_date_refuses(self) -> None:
        from alphalens_cli.commands.broker import broker_app

        result = self.runner.invoke(broker_app, ["arm", "KO", "--date", "not-a-date"])
        self.assertEqual(result.exit_code, 1)
        self.assertIn("invalid --date", result.output)

    def test_arm_missing_brief_parquet_refuses(self) -> None:
        from alphalens_cli.commands.broker import broker_app

        with mock.patch(
            "alphalens_pipeline.paper.brief_loader.load_brief",
            side_effect=FileNotFoundError("thematic brief parquet not found: /x.parquet"),
        ):
            result = self.runner.invoke(broker_app, ["arm", "KO", "--date", "2026-07-20"])
        self.assertEqual(result.exit_code, 1)
        self.assertIn("not found", result.output)

    def test_arm_no_trade_setup_refuses(self) -> None:
        from alphalens_cli.commands.broker import broker_app

        with (
            mock.patch(
                "alphalens_pipeline.paper.brief_loader.load_brief",
                return_value=[_candidate("KO", trade_setup=None)],
            ),
            mock.patch("alphalens_pipeline.brokers.automanager.picks.arm_pick") as arm,
        ):
            result = self.runner.invoke(broker_app, ["arm", "KO", "--date", "2026-07-20"])
        self.assertEqual(result.exit_code, 1)
        self.assertIn("no plannable trade_setup", result.output)
        arm.assert_not_called()

    def test_arm_unplannable_trade_setup_refuses(self) -> None:
        from alphalens_cli.commands.broker import broker_app

        bad_setup = _plannable_trade_setup()
        bad_setup["status"] = "NO_STRUCTURE"
        with (
            mock.patch(
                "alphalens_pipeline.paper.brief_loader.load_brief",
                return_value=[_candidate("KO", trade_setup=bad_setup)],
            ),
            mock.patch("alphalens_pipeline.brokers.automanager.picks.arm_pick") as arm,
        ):
            result = self.runner.invoke(broker_app, ["arm", "KO", "--date", "2026-07-20"])
        self.assertEqual(result.exit_code, 1)
        arm.assert_not_called()

    def test_arm_has_no_earnings_or_selection_filtering_logic(self) -> None:
        # arm is a pure executor: after the structural checks it arms the named
        # ticker, full stop. It must consult NO selection-policy filter — the old
        # arm-time earnings-window gate was removed (that policy moves to
        # brief-creation). Walk the AST and collect every identifier / import —
        # docstrings are Constant nodes, never collected, so a doc note about the
        # removed gate is ignored while any real earnings identifier (a param, a
        # lookup call, an import) is caught. Structural refusals (brief/ticker
        # absent, unplannable setup) are fine — those are not selection policy.
        from alphalens_cli.commands import broker

        tree = ast.parse(inspect.getsource(broker.arm_command))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names.add(node.id.lower())
            elif isinstance(node, ast.Attribute):
                names.add(node.attr.lower())
            elif isinstance(node, ast.arg):
                names.add(node.arg.lower())
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    names.add(alias.name.lower())
                if isinstance(node, ast.ImportFrom) and node.module:
                    names.add(node.module.lower())
        earnings_refs = sorted(n for n in names if "earnings" in n)
        self.assertEqual(
            earnings_refs,
            [],
            f"arm_command must carry no earnings/selection filter; found {earnings_refs}",
        )

    def test_arm_arms_even_with_imminent_earnings_no_lookup(self) -> None:
        # The behavioural counterpart: a candidate the OLD gate would have
        # refused (earnings imminent) now arms — because arm never looks earnings
        # up. No earnings mock is needed precisely because no lookup happens
        # (hermetic: zero network).
        from alphalens_cli.commands.broker import broker_app

        with (
            mock.patch(
                "alphalens_pipeline.paper.brief_loader.load_brief",
                return_value=[_candidate("KO")],
            ),
            mock.patch("alphalens_pipeline.brokers.automanager.picks.arm_pick") as arm,
        ):
            result = self.runner.invoke(broker_app, ["arm", "KO", "--date", "2026-07-20"])
        self.assertEqual(result.exit_code, 0, result.output)
        arm.assert_called_once()


class ArmEnvOptionTest(unittest.TestCase):
    """Thin CLI-option test: `--env` targets the right instance inbox path.

    ``arm_pick`` stays mocked here — these tests only pin the ``path`` kwarg
    it is called with, not the actual file write (covered end-to-end by
    ``ArmEnvIsolationTest`` below).
    """

    def setUp(self) -> None:
        self.runner = CliRunner()
        self.home = _isolate_home(self)

    def test_arm_default_env_passes_sim_picks_path(self) -> None:
        from alphalens_cli.commands.broker import broker_app
        from alphalens_pipeline.brokers.automanager import state_paths

        with (
            mock.patch(
                "alphalens_pipeline.paper.brief_loader.load_brief",
                return_value=[_candidate("KO")],
            ),
            mock.patch("alphalens_pipeline.brokers.automanager.picks.arm_pick") as arm,
        ):
            result = self.runner.invoke(broker_app, ["arm", "KO", "--date", "2026-07-20"])
        self.assertEqual(result.exit_code, 0, result.output)
        _args, kwargs = arm.call_args
        self.assertEqual(kwargs["path"], state_paths.picks_path(env="sim"))

    def test_arm_env_live_passes_live_picks_path(self) -> None:
        from alphalens_cli.commands.broker import broker_app
        from alphalens_pipeline.brokers.automanager import state_paths

        with (
            mock.patch(
                "alphalens_pipeline.paper.brief_loader.load_brief",
                return_value=[_candidate("KO")],
            ),
            mock.patch("alphalens_pipeline.brokers.automanager.picks.arm_pick") as arm,
        ):
            result = self.runner.invoke(
                broker_app, ["arm", "KO", "--date", "2026-07-20", "--env", "live"]
            )
        self.assertEqual(result.exit_code, 0, result.output)
        _args, kwargs = arm.call_args
        self.assertEqual(kwargs["path"], state_paths.picks_path(env="live"))

    def test_arm_invalid_env_fails_loud_via_the_seam(self) -> None:
        # `--env prod` must fail with the SAME ValueError the state_paths seam
        # raises for any other invalid environment value (D1) — never a
        # bespoke CLI-side error string.
        from alphalens_cli.commands.broker import broker_app

        with (
            mock.patch(
                "alphalens_pipeline.paper.brief_loader.load_brief",
                return_value=[_candidate("KO")],
            ),
            mock.patch("alphalens_pipeline.brokers.automanager.picks.arm_pick") as arm,
        ):
            result = self.runner.invoke(
                broker_app, ["arm", "KO", "--date", "2026-07-20", "--env", "prod"]
            )
        self.assertEqual(result.exit_code, 1)
        self.assertIn("ALPHALENS_BROKER_ENVIRONMENT", result.output)
        self.assertIn("prod", result.output)
        arm.assert_not_called()


class ArmEnvIsolationTest(unittest.TestCase):
    """End-to-end (real arm_pick/iter_picks): sim and live inboxes never cross.

    Mirrors ``test_state_paths.HomeDirTestCase`` — patches ``Path.home()`` to
    an isolated temp directory so the write actually lands under
    ``<home>/.alphalens/broker_orders/<env>/picks.jsonl``.
    """

    def setUp(self) -> None:
        self.runner = CliRunner()
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        patcher = mock.patch("pathlib.Path.home", return_value=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_arm_env_live_writes_only_to_the_live_inbox(self) -> None:
        from alphalens_cli.commands.broker import broker_app
        from alphalens_pipeline.brokers.automanager import state_paths
        from alphalens_pipeline.brokers.automanager.picks import iter_picks

        with mock.patch(
            "alphalens_pipeline.paper.brief_loader.load_brief",
            return_value=[_candidate("KO")],
        ):
            result = self.runner.invoke(
                broker_app, ["arm", "KO", "--date", "2026-07-20", "--env", "live"]
            )
        self.assertEqual(result.exit_code, 0, result.output)

        live_picks = list(iter_picks(path=state_paths.picks_path(env="live")))
        sim_picks = list(iter_picks(path=state_paths.picks_path(env="sim")))
        self.assertEqual([i.instrument.ticker for i in live_picks], ["KO"])
        self.assertEqual(sim_picks, [])

    def test_arm_default_env_writes_only_to_the_sim_inbox(self) -> None:
        from alphalens_cli.commands.broker import broker_app
        from alphalens_pipeline.brokers.automanager import state_paths
        from alphalens_pipeline.brokers.automanager.picks import iter_picks

        with mock.patch(
            "alphalens_pipeline.paper.brief_loader.load_brief",
            return_value=[_candidate("KO")],
        ):
            result = self.runner.invoke(broker_app, ["arm", "KO", "--date", "2026-07-20"])
        self.assertEqual(result.exit_code, 0, result.output)

        sim_picks = list(iter_picks(path=state_paths.picks_path(env="sim")))
        live_picks = list(iter_picks(path=state_paths.picks_path(env="live")))
        self.assertEqual([i.instrument.ticker for i in sim_picks], ["KO"])
        self.assertEqual(live_picks, [])


class ArmLegacyLayoutGuardTest(unittest.TestCase):
    """`arm` refuses on a pre-ADR-0016 flat legacy layout (D4), before
    persisting anything to picks.jsonl."""

    def setUp(self) -> None:
        self.runner = CliRunner()
        self.home = _isolate_home(self)

    def test_arm_on_legacy_layout_refuses_and_creates_no_picks_file(self) -> None:
        from alphalens_cli.commands.broker import broker_app
        from alphalens_pipeline.brokers.automanager import state_paths

        _seed_legacy_flat_state(self.home)

        with (
            mock.patch(
                "alphalens_pipeline.paper.brief_loader.load_brief",
                return_value=[_candidate("KO")],
            ),
            mock.patch("alphalens_pipeline.brokers.automanager.picks.arm_pick") as arm,
        ):
            result = self.runner.invoke(broker_app, ["arm", "KO", "--date", "2026-07-20"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("legacy flat broker state", result.output)
        self.assertIn(
            "Migrate into the per-environment layout", result.output, "migration hint expected"
        )
        arm.assert_not_called()
        self.assertFalse(
            state_paths.picks_path(env="sim").exists(), "no pick must be persisted on refusal"
        )


class ArmDefaultEnvParityTest(unittest.TestCase):
    """`_DEFAULT_ARM_ENV` is a literal duplicating `state_paths.ENV_SIM`
    (kept literal so the option default is available without importing
    state_paths at module scope, lazy-CLI doctrine). Pin the two together so
    a future rename of either cannot silently drift them apart."""

    def test_default_arm_env_matches_the_seam_sim_constant(self) -> None:
        from alphalens_cli.commands import broker
        from alphalens_pipeline.brokers.automanager import state_paths

        self.assertEqual(broker._DEFAULT_ARM_ENV, state_paths.ENV_SIM)


if __name__ == "__main__":
    unittest.main()
