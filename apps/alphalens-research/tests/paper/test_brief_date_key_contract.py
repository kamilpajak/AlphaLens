"""L2 contract: the paper-chain (D-1) brief date-key (test-strategy Phase 2).

Pins the #343 escape: the daily build on day D writes a brief dated (D-1);
the paper plan + submit jobs must read that brief with ``--date yesterday``.
When submit used ``--date today`` it found 0 PLANNED rows and silently did
nothing — ``exit 0``, looked healthy, traded nothing.

Two halves, both hermetic (no systemd, no Alpaca):
  1. Producer↔consumer filename identity — the brief generator's real
     ``_empty_output`` write path and ``brief_loader.load_brief`` resolve the
     SAME ``{date}.parquet`` filename, so a brief written for (D-1) is found by
     a loader querying (D-1) and NOT by one querying D.
  2. systemd date-offset parity — the plan + submit units use the identical
     ``$(date -u -d yesterday ...)`` expression and the daily build defaults to
     yesterday, so the three date-keys line up. Positive control: a "today"
     expression must be absent.

Failure class (memo §2): seam-contract (brief asof ↔ paper date-key).
"""

from __future__ import annotations

import datetime as dt
import json
import re
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from alphalens_pipeline.paper.brief_loader import load_brief
from alphalens_pipeline.thematic.argumentation.orchestrator import _empty_output

_REPO_ROOT = Path(__file__).resolve().parents[4]
_PLAN_UNIT = _REPO_ROOT / "deploy/systemd/alphalens-paper-plan.service"
_SUBMIT_UNIT = _REPO_ROOT / "deploy/systemd/alphalens-paper-submit.service"
_BUILD_SCRIPT = _REPO_ROOT / "deploy/docker/run_thematic_day.sh"

# D-1 / D pair (fixed dates — no now())
_D_MINUS_1 = dt.date(2026, 5, 28)
_D = dt.date(2026, 5, 29)

# systemd escapes ``%`` as ``%%`` in ExecStart; the (D-1) offset is ``-d yesterday``.
_DATE_EXPR_RE = re.compile(r"\$\(date[^)]*\)")
_YESTERDAY_OFFSET = "-d yesterday"


def _exec_start(unit_path: Path) -> str:
    """Return the single live ``ExecStart=`` directive (ignoring # comments)."""
    for line in unit_path.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("ExecStart="):
            return stripped
    raise AssertionError(f"no live ExecStart= in {unit_path.name}")


def _date_expr(exec_start: str) -> str:
    """Extract the ``$(date ...)`` substitution from an ExecStart directive."""
    match = _DATE_EXPR_RE.search(exec_start)
    if match is None:
        raise AssertionError(f"ExecStart has no $(date ...) expression: {exec_start}")
    return match.group(0)


def _one_row_brief(dirpath: Path, brief_date: dt.date) -> Path:
    setup = {
        "schema_version": "1.0.0",
        "status": "OK",
        "asof_close": 100.0,
        "atr": 1.5,
        "disaster_stop": 80.0,
        "suggested_size_pct": 5.0,
        "order_ttl_days": 10,
        "entry_tiers": [{"limit": 100.0, "alloc_pct": 100.0, "atr_distance": 0.0, "tag": "t0"}],
        "tp_tranches": [{"target": 110.0, "tranche_pct": 100.0, "r_multiple": 1.0, "tag": "tp"}],
    }
    path = dirpath / f"{brief_date.isoformat()}.parquet"
    pd.DataFrame(
        [{"ticker": "TEST", "theme": "t", "verified": True, "brief_trade_setup": json.dumps(setup)}]
    ).to_parquet(path, index=False)
    return path


class TestBriefDateKeyRoundTrip(unittest.TestCase):
    def test_producer_write_path_is_found_by_loader_for_same_date(self):
        # _empty_output is the REAL generator filename code path. A brief
        # written for (D-1) must be loadable by a loader querying (D-1).
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            _empty_output(out, _D_MINUS_1)
            candidates = load_brief(_D_MINUS_1, out)  # must not raise
            self.assertEqual(candidates, [])

    def test_loader_for_today_misses_yesterday_brief(self):
        # positive control: the #343 off-by-one. A brief dated (D-1) is NOT
        # found when the loader queries D (today); the loader raises a loud
        # FileNotFoundError naming the missing date rather than returning [].
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            _one_row_brief(out, _D_MINUS_1)
            # querying (D-1) finds the candidate ...
            self.assertEqual(len(load_brief(_D_MINUS_1, out)), 1)
            # ... querying D (today) does not.
            with self.assertRaises(FileNotFoundError) as ctx:
                load_brief(_D, out)
            self.assertIn(_D.isoformat(), str(ctx.exception))


class TestPaperChainDateOffsetParity(unittest.TestCase):
    def test_plan_and_submit_use_identical_yesterday_expression(self):
        plan_expr = _date_expr(_exec_start(_PLAN_UNIT))
        submit_expr = _date_expr(_exec_start(_SUBMIT_UNIT))
        # Both must compute (D-1) ...
        self.assertIn(_YESTERDAY_OFFSET, plan_expr)
        self.assertIn(_YESTERDAY_OFFSET, submit_expr)
        # ... and use the SAME expression, so plan and submit never diverge.
        self.assertEqual(plan_expr, submit_expr)

    def test_no_today_expression_in_chain(self):
        # positive control: a "today" date expression (no ``-d yesterday``)
        # would re-introduce #343. It must be absent from both units.
        for unit in (_PLAN_UNIT, _SUBMIT_UNIT):
            expr = _date_expr(_exec_start(unit))
            self.assertIn(
                _YESTERDAY_OFFSET,
                expr,
                f"{unit.name} ExecStart uses a non-yesterday date expr: {expr}",
            )

    def test_daily_build_brief_defaults_to_yesterday(self):
        # The build script invokes `alphalens thematic brief` with NO --date,
        # so the CLI default (yesterday) writes (D-1).parquet on day D — the
        # producer half of the contract the loader's --date yesterday matches.
        text = _BUILD_SCRIPT.read_text()
        brief_lines = [
            ln for ln in text.splitlines() if re.search(r"\balphalens thematic brief\b", ln)
        ]
        self.assertTrue(
            brief_lines, "run_thematic_day.sh does not invoke `alphalens thematic brief`"
        )
        for ln in brief_lines:
            self.assertNotIn(
                "--date",
                ln,
                "daily build must NOT pin --date (defaults to yesterday → writes (D-1))",
            )


if __name__ == "__main__":
    unittest.main()
