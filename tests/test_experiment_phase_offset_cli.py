"""Tests for the --phase-offset CLI flag on the tri-factor and mom+lowvol
experiment scripts. Wires `BacktestEngine.phase_offset` (validated separately
in tests/test_backtest_engine_stride.py::TestPhaseOffset) through the CLI so
multi-phase audit runs can fix the sampling phase explicitly.

Triggered by methodology audit 2026-04-29:
docs/research/methodology_audit_2026_04_29.md
"""

from __future__ import annotations

import unittest
from datetime import date


class TriFactorPhaseOffsetCLI(unittest.TestCase):
    def test_phase_offset_flag_default_is_zero(self):
        from scripts.experiment_tri_factor_edgar import _build_parser

        args = _build_parser().parse_args([])
        self.assertEqual(args.phase_offset, 0)

    def test_phase_offset_flag_parsed(self):
        from scripts.experiment_tri_factor_edgar import _build_parser

        args = _build_parser().parse_args(["--phase-offset", "3"])
        self.assertEqual(args.phase_offset, 3)

    def test_phase_offset_alongside_other_flags(self):
        from scripts.experiment_tri_factor_edgar import _build_parser

        args = _build_parser().parse_args(
            [
                "--phase-offset",
                "2",
                "--lock-universe",
                "--is-start",
                "2015-01-01",
                "--is-end",
                "2018-12-31",
            ]
        )
        self.assertEqual(args.phase_offset, 2)
        self.assertTrue(args.lock_universe)
        self.assertEqual(args.is_start, date(2015, 1, 1))


class MomentumLowvolPhaseOffsetCLI(unittest.TestCase):
    def test_phase_offset_flag_default_is_zero(self):
        from scripts.experiment_momentum_lowvol_combo import _build_parser

        args = _build_parser().parse_args([])
        self.assertEqual(args.phase_offset, 0)

    def test_phase_offset_flag_parsed(self):
        from scripts.experiment_momentum_lowvol_combo import _build_parser

        args = _build_parser().parse_args(["--phase-offset", "4"])
        self.assertEqual(args.phase_offset, 4)


if __name__ == "__main__":
    unittest.main()
