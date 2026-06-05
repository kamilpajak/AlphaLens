"""Enforcement: no raw yfinance access outside the canonical client.

yfinance was the last external vendor without a canonical client. Raw
``yfinance.Ticker(...)`` calls scattered across the pipeline (the mcap filter's
~400-ticker mapping batch, the technicals OHLCV loader, the earnings calendar)
each fired uncoordinated requests that race into a Yahoo 429 burst — silently
zeroing a candidate's technicals (STGW/BAH/MORN, 2026-06-04). The fix routes
every Yahoo call through
:class:`alphalens_pipeline.data.alt_data.yfinance_client.YFinanceClient`, whose
shared throttle + bounded retry give the whole process ONE rate budget.

This test fails red if anyone reintroduces a raw yfinance call (``yf.Ticker(`` /
``yfinance.Ticker(`` / ``import yfinance`` / ``from yfinance``) in a scanned
file that is not the client itself.

Mirror of :mod:`tests.test_no_raw_polygon_http` (+ sec / av / openrouter); same
positive-control discipline so the detection patterns can't silently rot to
empty.

ALLOWLIST (documented TODOs, NOT permitted shadow clients — tighten as each is
migrated):
- ``yfinance_cache.py`` — a DI OHLCV cache shared with research scripts.
- ``edgar_fundamentals.py`` — a yfinance *batch* download with a genuine
  batch-vs-throttle design tension (PR-3).
- ``screeners/prescreener/data_fetcher.py`` — the RESEARCH_ONLY Layer 2a
  prescreener; pulls the full ``.info`` dict, a surface the client does not yet
  expose. Manual ad-hoc, no live-pipeline burst contention.
- four research SCRIPTS (``probe_yfinance_analyst_survivorship.py``,
  ``precheck_pc_cyclicality_is.py``, ``precheck_strategy_cyclicality.py``,
  ``edgar_fundamentals_validation_gate.py``) — ad-hoc, run manually on the Mac
  (a different IP than the VPS pipeline), so they never contend for the live
  rate budget the canonical client coordinates.
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

# The canonical client — the only file allowed to touch yfinance directly.
CANONICAL_CLIENT_REL = "apps/alphalens-pipeline/alphalens_pipeline/data/alt_data/yfinance_client.py"

# Deferred-migration allowlist (documented TODOs, not shadow clients). The
# pipeline sites have a real batch-vs-throttle tension (PR-3); the prescreener is
# a RESEARCH_ONLY layer pulling the full `.info` dict (a surface the client does
# not expose yet); the four research SCRIPTS are ad-hoc, run manually on a
# different IP than the VPS pipeline, so they don't contend for the live rate
# budget — low priority.
DEFERRED_RELS = (
    "apps/alphalens-pipeline/alphalens_pipeline/data/alt_data/yfinance_cache.py",
    "apps/alphalens-pipeline/alphalens_pipeline/data/store/edgar_fundamentals.py",
    "apps/alphalens-research/alphalens_research/screeners/prescreener/data_fetcher.py",
    "apps/alphalens-research/scripts/probe_yfinance_analyst_survivorship.py",
    "apps/alphalens-research/scripts/precheck_pc_cyclicality_is.py",
    "apps/alphalens-research/scripts/precheck_strategy_cyclicality.py",
    "apps/alphalens-research/scripts/edgar_fundamentals_validation_gate.py",
)

# Patterns that constitute a raw yfinance call. ``(?<![\w.])`` defeats a left
# word boundary so ``get_default_yfinance_client`` / ``yfinance_client`` imports
# do NOT match ``yf.Ticker`` / ``yfinance.Ticker``; the import patterns anchor to
# the line start so ``from ...yfinance_client import`` is not a ``from yfinance``.
RAW_YF_PATTERNS = (
    re.compile(r"(?<![\w.])yf\.Ticker\("),
    re.compile(r"(?<![\w.])yfinance\.Ticker\("),
    re.compile(r"^\s*import\s+yfinance\b"),
    re.compile(r"^\s*from\s+yfinance\b"),
)


def _is_exempt(rel_path: str) -> bool:
    return rel_path == CANONICAL_CLIENT_REL or rel_path in DEFERRED_RELS


def _find_raw_yf_lines(text: str) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        for pattern in RAW_YF_PATTERNS:
            if pattern.search(line):
                hits.append((lineno, line.rstrip()))
                break
    return hits


class TestNoRawYfinance(unittest.TestCase):
    def test_detection_regex_locks_shadow_patterns(self):
        """Positive control: each shape we MEAN to catch is flagged; known-safe
        shapes (the client import, a comment) are NOT."""
        shadow = [
            "mc = yf.Ticker(t).fast_info.market_cap",
            "calendar = yfinance.Ticker(t).calendar",
            "import yfinance as yf",
            "from yfinance.exceptions import YFRateLimitError",
        ]
        for sample in shadow:
            self.assertEqual(len(_find_raw_yf_lines(sample)), 1, f"missed shadow: {sample!r}")

        safe = [
            "from alphalens_pipeline.data.alt_data.yfinance_client import get_default_yfinance_client",
            "client = get_default_yfinance_client()",
            "# yf.Ticker(t) in a comment must never trip detection",
            "    return client.market_cap(upper)",
        ]
        for sample in safe:
            self.assertEqual(_find_raw_yf_lines(sample), [], f"false positive: {sample!r}")

    def test_no_raw_yfinance_outside_canonical_client(self):
        offenders: list[str] = []
        for scan_dir in SCAN_DIRS:
            if not scan_dir.exists():
                continue
            for path in scan_dir.rglob("*.py"):
                rel = path.relative_to(WORKSPACE_ROOT).as_posix()
                if _is_exempt(rel):
                    continue
                hits = _find_raw_yf_lines(path.read_text(encoding="utf-8"))
                offenders.extend(f"{rel}:{ln}: {src}" for ln, src in hits)
        self.assertEqual(
            offenders,
            [],
            "raw yfinance access found outside the canonical YFinanceClient "
            "(route it through get_default_yfinance_client()):\n" + "\n".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
