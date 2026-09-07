"""Unit tests for ``scripts/probe_saxo_live_entitlement.py`` (#1355 PR-C).

The script is the read-only instrument that answers "does the LIVE
market-data account hold the real-time entitlement for venue X" — the
question every European venue arc asks before its first LIVE arm. It must
NEVER elevate the session (the production price reader holds the ONE
elevated Saxo session; a second elevation demotes it), and it must never
report "entitled" from a quote whose delay flag is not meaningful (closed
market, unresolved ticker).
"""

from __future__ import annotations

import importlib
import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


def _load():
    if str(_SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS_DIR))
    return importlib.import_module("probe_saxo_live_entitlement")


class _FakeClient:
    """Duck-typed SaxoMarketDataClient: canned uics + quotes, elevation spy."""

    def __init__(self, uics: dict[tuple[str, str], int], quotes: dict[int, dict[str, Any]]):
        self._uics = uics
        self._quotes = quotes
        self.elevations = 0
        self.closed = False

    def resolve_uic(self, ticker: str, *, exchange_mic: str) -> int | None:
        return self._uics.get((ticker.upper(), exchange_mic.upper()))

    def get_stock_infoprice(self, uic: int, **_kw: Any) -> dict[str, Any]:
        return {"Quote": dict(self._quotes[uic])}

    def elevate_session(self) -> bool:
        self.elevations += 1
        return True

    def close(self) -> None:
        self.closed = True


def _quote(delay: int, state: str = "Open") -> dict[str, Any]:
    return {
        "DelayedByMinutes": delay,
        "MarketState": state,
        "PriceSource": "PAR",
        "Bid": 1.0,
        "Ask": 1.1,
    }


def _run(client: _FakeClient, *targets: str) -> tuple[int, str, str]:
    mod = _load()
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(list(targets), client_factory=lambda: client)
    return code, out.getvalue(), err.getvalue()


class TestVerdicts(unittest.TestCase):
    def test_every_quote_real_time_during_open_session_exits_zero(self) -> None:
        client = _FakeClient({("KER", "XPAR"): 398681}, {398681: _quote(0)})
        code, out, _err = _run(client, "KER@XPAR")
        self.assertEqual(code, 0)
        self.assertIn("KER@XPAR", out)
        self.assertIn("real-time", out)

    def test_delayed_quote_is_an_entitlement_gap_exit_four(self) -> None:
        client = _FakeClient({("KER", "XPAR"): 398681}, {398681: _quote(15)})
        code, out, _err = _run(client, "KER@XPAR")
        self.assertEqual(code, 4)
        self.assertIn("delayed 15", out)

    def test_closed_market_is_inconclusive_never_entitled(self) -> None:
        client = _FakeClient({("KER", "XPAR"): 398681}, {398681: _quote(0, state="Closed")})
        code, out, _err = _run(client, "KER@XPAR")
        self.assertEqual(code, 8)
        self.assertIn("inconclusive", out)

    def test_unresolved_ticker_is_inconclusive(self) -> None:
        client = _FakeClient({}, {})
        code, out, _err = _run(client, "KER@XPAR")
        self.assertEqual(code, 8)
        self.assertIn("unresolved", out)

    def test_one_gap_among_real_time_quotes_still_exits_four(self) -> None:
        client = _FakeClient(
            {("AAPL", "XNAS"): 211, ("KER", "XPAR"): 398681},
            {211: _quote(0), 398681: _quote(15)},
        )
        code, _out, _err = _run(client, "AAPL@XNAS", "KER@XPAR")
        self.assertEqual(code, 4)


class TestSafety(unittest.TestCase):
    def test_never_elevates_the_session(self) -> None:
        client = _FakeClient({("KER", "XPAR"): 398681}, {398681: _quote(15)})
        _run(client, "KER@XPAR")
        self.assertEqual(client.elevations, 0)

    def test_closes_the_client_on_every_path(self) -> None:
        client = _FakeClient({}, {})
        _run(client, "KER@XPAR")
        self.assertTrue(client.closed)

    def test_bad_target_syntax_is_a_usage_error(self) -> None:
        client = _FakeClient({}, {})
        code, _out, err = _run(client, "KER")
        self.assertEqual(code, 2)
        self.assertIn("TICKER@MIC", err)


if __name__ == "__main__":
    unittest.main()
