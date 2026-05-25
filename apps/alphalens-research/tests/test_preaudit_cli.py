"""CLI integration tests for `alphalens preaudit`.

Uses typer's `CliRunner` so we exercise the typer wrapper + arg
parsing + exit codes without spawning real subprocesses. The runner
itself is mocked at the `alphalens_research.preaudit.runner.run_smoke` boundary.
"""

from __future__ import annotations

import unittest
from datetime import date
from unittest import mock

from alphalens_cli.main import app
from alphalens_research.preaudit.profiles import (
    CheckType,
    CoverageCheck,
    CoverageReport,
    CoverageStatus,
    DataDep,
    SmokeResult,
    SmokeStatus,
)
from typer.testing import CliRunner

runner = CliRunner()


def _good_report() -> CoverageReport:
    dep = DataDep(name="prices", check_type=CheckType.EXISTS_NONEMPTY)
    return CoverageReport(checks=(CoverageCheck(dep=dep, status=CoverageStatus.PASS),))


def _bad_report() -> CoverageReport:
    dep = DataDep(
        name="ivolatility_smd",
        check_type=CheckType.FLAT_PARQUET,
        min_date=date(2014, 1, 1),
        max_date=date(2017, 12, 31),
    )
    return CoverageReport(
        checks=(
            CoverageCheck(
                dep=dep,
                status=CoverageStatus.FAIL_GAP,
                detail="10/10 sampled files miss window 2014-01-01..2017-12-31",
            ),
        )
    )


class TestPreauditUnknownStrategy(unittest.TestCase):
    def test_unknown_strategy_exits_2(self):
        result = runner.invoke(app, ["preaudit", "__not_a_real_strategy__"])
        self.assertEqual(result.exit_code, 2)
        self.assertIn("not in audit._SCRIPTS", result.stdout + (result.stderr or ""))


class TestPreauditNoProfile(unittest.TestCase):
    def test_strategy_in_scripts_but_no_profile_exits_2(self):
        # tri_factor is in _SCRIPTS but has no SmokeProfile today.
        result = runner.invoke(app, ["preaudit", "tri_factor"])
        self.assertEqual(result.exit_code, 2)


class TestPreauditCoverageGating(unittest.TestCase):
    """Coverage failure must short-circuit: smoke MUST NOT run."""

    @mock.patch("alphalens_research.preaudit.runner.run_smoke")
    @mock.patch("alphalens_research.preaudit.coverage.check_all_deps")
    def test_coverage_fail_aborts_before_smoke(self, m_cov, m_smoke):
        m_cov.return_value = _bad_report()
        result = runner.invoke(app, ["preaudit", "insider_pc_compound"])
        self.assertEqual(result.exit_code, 1)
        # smoke must NOT have been called
        self.assertEqual(m_smoke.call_count, 0)
        self.assertIn("FAIL_GAP", result.stdout + (result.stderr or "").upper())


class TestPreauditSmokeFailure(unittest.TestCase):
    @mock.patch("alphalens_research.preaudit.runner.run_smoke")
    @mock.patch("alphalens_research.preaudit.coverage.check_all_deps")
    def test_smoke_fail_exits_1(self, m_cov, m_smoke):
        m_cov.return_value = _good_report()
        m_smoke.return_value = SmokeResult(
            status=SmokeStatus.FAIL,
            exit_code=8,
            duration_s=12.3,
            detail="experiment subprocess exited 8.\nstderr: KeyError: 'asof'",
        )
        result = runner.invoke(app, ["preaudit", "insider_pc_compound"])
        self.assertEqual(result.exit_code, 1)
        self.assertIn("FAIL", result.stdout + (result.stderr or "").upper())


class TestPreauditHappyPath(unittest.TestCase):
    @mock.patch("alphalens_research.preaudit.runner.run_smoke")
    @mock.patch("alphalens_research.preaudit.coverage.check_all_deps")
    def test_coverage_and_smoke_pass_exits_0(self, m_cov, m_smoke):
        m_cov.return_value = _good_report()
        m_smoke.return_value = SmokeResult(status=SmokeStatus.PASS, exit_code=0, duration_s=82.4)
        result = runner.invoke(app, ["preaudit", "insider_pc_compound"])
        self.assertEqual(result.exit_code, 0)
        out = result.stdout + (result.stderr or "")
        self.assertIn("preaudit OK", out)


class TestPreauditSkipFlags(unittest.TestCase):
    @mock.patch("alphalens_research.preaudit.runner.run_smoke")
    @mock.patch("alphalens_research.preaudit.coverage.check_all_deps")
    def test_skip_coverage_bypasses_stage_1(self, m_cov, m_smoke):
        m_smoke.return_value = SmokeResult(status=SmokeStatus.PASS, exit_code=0, duration_s=1.0)
        result = runner.invoke(app, ["preaudit", "insider_pc_compound", "--skip-coverage"])
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(m_cov.call_count, 0)

    @mock.patch("alphalens_research.preaudit.runner.run_smoke")
    @mock.patch("alphalens_research.preaudit.coverage.check_all_deps")
    def test_skip_smoke_bypasses_stage_2(self, m_cov, m_smoke):
        m_cov.return_value = _good_report()
        result = runner.invoke(app, ["preaudit", "insider_pc_compound", "--skip-smoke"])
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(m_smoke.call_count, 0)


if __name__ == "__main__":
    unittest.main()
