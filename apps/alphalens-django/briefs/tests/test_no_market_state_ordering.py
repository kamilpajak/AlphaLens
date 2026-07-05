"""Anti-rot: market_state* fields are display-only — never a Django-side sort/filter.

Django-side counterpart to the pipeline guard ``test_no_market_state_in_selection``.
The 8 fields are auto-served (``exclude=("pk",)``) but must not become a queryset
ordering / filter key in any briefs source module. Negative source scan over the
briefs package (excluding tests + migrations) + a positive control so the scan
cannot silently rot to always-pass.
"""

import re
import unittest
from pathlib import Path

_BRIEFS_DIR = Path(__file__).resolve().parents[1]

# order_by / filter / exclude / ordering[_fields] / filterset_fields referencing
# a market_state column on the SAME line (i.e. used as a sort/filter key).
_ORDER_OR_FILTER = re.compile(
    r"(order_by|filter|exclude|ordering|ordering_fields|filterset_fields)[^\n]*market_state"
)


def _source_files():
    for path in _BRIEFS_DIR.rglob("*.py"):
        parts = set(path.parts)
        if "tests" in parts or "migrations" in parts:
            continue
        yield path


class TestMarketStateNotOrderedOrFiltered(unittest.TestCase):
    def test_briefs_source_never_orders_or_filters_by_market_state(self):
        offenders = [
            str(p.relative_to(_BRIEFS_DIR))
            for p in _source_files()
            if _ORDER_OR_FILTER.search(p.read_text(encoding="utf-8"))
        ]
        self.assertEqual(offenders, [])

    def test_positive_control_regex_would_catch_a_leak(self):
        # If the scan rotted to always-pass, this planted sample would slip by.
        self.assertRegex('qs.order_by("-market_state_dist200")', _ORDER_OR_FILTER)


if __name__ == "__main__":
    unittest.main()
