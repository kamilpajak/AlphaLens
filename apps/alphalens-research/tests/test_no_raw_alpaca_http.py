"""Enforcement: no raw alpaca-py SDK imports outside the canonical client.

Every Alpaca call routes through
:class:`alphalens_pipeline.data.alt_data.alpaca_client.AlpacaClient`. The
``paper=True`` guarantee + base-URL guard live there; a shadow client
constructed directly with ``TradingClient(..., paper=False)`` (or with the
``api.alpaca.markets`` URL) would bypass both protections and could trade
real money on accident — which the project doctrine ``capital_deploy_clause``
explicitly forbids.

This test fails red if anyone reintroduces a raw SDK import (defined as
``from alpaca.trading.client import``, ``from alpaca.data.historical``,
``alpaca.trading.client.TradingClient(``, or related shapes) in any file
under ``alphalens_pipeline/``, ``alphalens_research/``, ``alphalens_cli/``,
or ``scripts/``.

Mirror of :mod:`tests.test_no_raw_av_http` / :mod:`tests.test_no_raw_gemini_sdk`;
same structure (positive control + negative scan).
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[3]

SCAN_DIRS = (
    WORKSPACE_ROOT / "apps" / "alphalens-pipeline" / "alphalens_pipeline",
    WORKSPACE_ROOT / "apps" / "alphalens-pipeline" / "alphalens_cli",
    WORKSPACE_ROOT / "apps" / "alphalens-research" / "alphalens_research",
    WORKSPACE_ROOT / "apps" / "alphalens-research" / "scripts",
)

# Path prefixes inside SCAN_DIRS that are intentionally exempt — the canonical
# client itself is allowed (and required) to import the SDK.
EXEMPT_PATH_PREFIXES = (
    "apps/alphalens-pipeline/alphalens_pipeline/data/alt_data/alpaca_client.py",
)

# Patterns that constitute a raw SDK import or shadow client construction.
# - ``from alpaca.trading.client import`` / ``from alpaca.trading.requests``
#   / ``from alpaca.data.historical`` — direct module imports
# - ``alpaca.trading.client.TradingClient(`` — qualified construction
# - ``TradingClient(`` at the SDK-side (not the canonical wrapper)
#
# The canonical client uses ``self._trading.submit_order(...)`` etc., which
# does NOT match — ``self.`` defeats every pattern below.
SHADOW_PATTERNS = (
    re.compile(r"\bfrom\s+alpaca\.trading\b"),
    re.compile(r"\bfrom\s+alpaca\.data\b"),
    re.compile(r"\bfrom\s+alpaca\.common\b"),
    re.compile(r"\bfrom\s+alpaca\.broker\b"),
    re.compile(r"\bimport\s+alpaca\b"),
    re.compile(r"(?<![\w.])TradingClient\("),
    re.compile(r"(?<![\w.])StockHistoricalDataClient\("),
)


def _find_shadow_lines(text: str) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        for pattern in SHADOW_PATTERNS:
            if pattern.search(line):
                hits.append((lineno, line.rstrip()))
                break
    return hits


def _path_is_exempt(rel: str) -> bool:
    return any(rel.startswith(prefix) for prefix in EXEMPT_PATH_PREFIXES)


class TestNoRawAlpacaSdk(unittest.TestCase):
    def test_detection_regex_locks_shadow_patterns(self):
        """Positive control: each shape we MEAN to catch is flagged, each
        known-safe shape is NOT. Without this pin a regex that rotted to
        ``""`` would let every shadow client through silently."""
        shadow_samples = [
            "from alpaca.trading.client import TradingClient",
            "from alpaca.trading.requests import LimitOrderRequest",
            "from alpaca.trading.enums import OrderSide",
            "from alpaca.data.historical import StockHistoricalDataClient",
            "from alpaca.common.enums import BaseURL",
            "from alpaca.broker.client import BrokerClient",
            "import alpaca",
            "import alpaca.trading as t",
            "client = TradingClient('k', 's', paper=False)",
            "data = StockHistoricalDataClient('k', 's')",
        ]
        for sample in shadow_samples:
            hits = _find_shadow_lines(sample)
            self.assertEqual(len(hits), 1, f"expected one hit on shadow sample: {sample!r}")

        safe_samples = [
            "self._trading.submit_order(order_data=req)",
            "self._trading_client.cancel_order_by_id(oid)",
            '"""Docstring mentioning alpaca.trading.client for context."""',
            "# from alpaca.trading.client import TradingClient — in a comment",
            "client = AlpacaClient(api_key='k', secret_key='s')",
            "from alphalens_pipeline.data.alt_data.alpaca_client import AlpacaClient",
            "obj.TradingClient = MagicMock()  # name attribute reassignment",
        ]
        for sample in safe_samples:
            hits = _find_shadow_lines(sample)
            self.assertEqual(
                len(hits), 0, f"expected zero hits on safe sample: {sample!r} (got {hits})"
            )

    def test_no_shadow_alpaca_sdk_outside_canonical_client(self):
        offenders: list[tuple[str, int, str]] = []
        for root in SCAN_DIRS:
            if not root.exists():
                continue
            for py in root.rglob("*.py"):
                rel = py.relative_to(WORKSPACE_ROOT).as_posix()
                if _path_is_exempt(rel):
                    continue
                text = py.read_text(encoding="utf-8", errors="replace")
                for lineno, src in _find_shadow_lines(text):
                    offenders.append((rel, lineno, src))

        if offenders:
            details = "\n".join(f"  {p}:{ln}  {src}" for p, ln, src in offenders)
            self.fail(
                "Raw alpaca-py SDK import / shadow client detected.\n"
                "Route the call through "
                "alphalens_pipeline.data.alt_data.alpaca_client (use\n"
                "get_default_alpaca_client() if you don't have one to inject).\n"
                "The canonical client hardcodes paper=True; a shadow construction\n"
                "could accidentally trade real money — see capital_deploy_clause.\n"
                f"Offenders:\n{details}"
            )


if __name__ == "__main__":
    unittest.main()
