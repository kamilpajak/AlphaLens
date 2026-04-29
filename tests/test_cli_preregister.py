"""End-to-end tests for `alphalens preregister` CLI."""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from typer.testing import CliRunner

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[mK]")


def _strip(text: str) -> str:
    return re.sub(r"\s+", " ", _ANSI_ESCAPE.sub("", text))


def _params_file(path: Path) -> Path:
    payload = {
        "params_frozen": {
            "top_n": 5,
            "holding": 20,
            "rebalance_stride": 5,
            "weights": {"roe": 0.4, "mom": 0.3, "rev": 0.3},
        },
        "periods": {
            "is_start": "2015-01-01",
            "is_end": "2022-12-31",
            "oos_start": "2023-01-01",
            "oos_end": "2026-04-22",
        },
        "success_criteria": {
            "mode": "multi_phase",
            "min_alpha_t_pass": 1.5,
            "min_alpha_t_mid": 1.0,
        },
    }
    path.write_text(json.dumps(payload))
    return path


class TestPreregisterCLI(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.params = _params_file(self.root / "params.json")
        self.runner = CliRunner()

    def tearDown(self):
        self._tmp.cleanup()

    def _add(self, id="tri_factor_2026_04_29", signal_class="fundamental_quality_x_momentum"):
        from alphalens_cli.main import app

        return self.runner.invoke(
            app,
            [
                "preregister",
                "add",
                "--id",
                id,
                "--signal-class",
                signal_class,
                "--hypothesis",
                "Tri-factor generates phase-robust α t≥1.5.",
                "--scorer-path",
                "scripts/experiment_tri_factor_edgar.py",
                "--params-file",
                str(self.params),
                "--ledger-root",
                str(self.root),
                "--registered-at",
                "2026-04-29",
            ],
        )

    def test_add_writes_ledger(self):
        result = self._add()

        self.assertEqual(result.exit_code, 0, msg=result.output)
        ledger_path = self.root / "ledger.json"
        self.assertTrue(ledger_path.exists())
        payload = json.loads(ledger_path.read_text())
        self.assertEqual(payload["entries"][0]["id"], "tri_factor_2026_04_29")

    def test_add_duplicate_id_exits_nonzero(self):
        self._add()
        result = self._add()

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("already exists", _strip(result.output))

    def test_list_filters_by_signal_class(self):
        from alphalens_cli.main import app

        self._add(id="a", signal_class="momentum")
        self._add(id="b", signal_class="quality")

        result = self.runner.invoke(
            app,
            ["preregister", "list", "--signal-class", "momentum", "--ledger-root", str(self.root)],
        )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        out = _strip(result.output)
        self.assertIn("a", out)
        self.assertNotIn(" b ", out)

    def test_show_prints_full_entry(self):
        from alphalens_cli.main import app

        self._add()

        result = self.runner.invoke(
            app,
            ["preregister", "show", "tri_factor_2026_04_29", "--ledger-root", str(self.root)],
        )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        out = _strip(result.output)
        self.assertIn("tri_factor_2026_04_29", out)
        self.assertIn("fundamental_quality_x_momentum", out)
        self.assertIn("scripts/experiment_tri_factor_edgar.py", out)

    def test_complete_records_outcome(self):
        from alphalens_cli.main import app

        self._add()

        result = self.runner.invoke(
            app,
            [
                "preregister",
                "complete",
                "tri_factor_2026_04_29",
                "--verdict",
                "FAIL",
                "--mean-alpha-t",
                "0.34",
                "--mean-excess-net",
                "-0.085",
                "--audit-path",
                "docs/research/tri_factor_multi_phase_audit.json",
                "--completed-at",
                "2026-04-29",
                "--ledger-root",
                str(self.root),
            ],
        )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        payload = json.loads((self.root / "ledger.json").read_text())
        entry = payload["entries"][0]
        self.assertEqual(entry["status"], "completed")
        self.assertEqual(entry["outcome"]["verdict"], "FAIL")

    def test_threshold_command_prints_critical_t(self):
        from alphalens_cli.main import app

        self._add(id="a", signal_class="momentum")
        self._add(id="b", signal_class="momentum")
        self._add(id="c", signal_class="momentum")

        result = self.runner.invoke(
            app,
            [
                "preregister",
                "threshold",
                "--signal-class",
                "momentum",
                "--ledger-root",
                str(self.root),
            ],
        )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        out = _strip(result.output)
        # 3 entries currently in class at α=0.05 → critical |t| ≈ 2.39
        self.assertIn("2.39", out)
        self.assertIn("momentum", out)
        self.assertIn("3 tests", out)


if __name__ == "__main__":
    unittest.main()
