"""Anti-rot: the index-level context fields are display-only — never a sort/filter.

Django-side counterpart to the pipeline guard ``test_no_market_state_in_selection``.
Covers BOTH index-level families, because neither is named by the other's prefix:
the 8 ``market_state*`` fields and the 3 ``market_ret5_*`` index returns. All 11
are auto-served (``exclude=("pk",)``) but must not become a queryset ordering /
filter key in any briefs source module. Negative source scan over the briefs
package (excluding tests + migrations) + one positive control PER FORM, so a
pattern that matched only ``market_state`` cannot read a ``market_ret5_*`` leak
as clean.
"""

import re
import unittest
from pathlib import Path

_BRIEFS_DIR = Path(__file__).resolve().parents[1]

# order_by / filter / exclude / ordering[_fields] / filterset_fields referencing
# an index-level context column on the SAME line (i.e. used as a sort/filter
# key). The alternation is deliberate rather than a bare ``market_``: an
# unrelated future ``market_`` field that IS a legitimate filter key should not
# be claimed by this guard.
_ORDER_OR_FILTER = re.compile(
    r"(order_by|filter|exclude|ordering|ordering_fields|filterset_fields)"
    r"[^\n]*market_(state|ret5_)"
)


def _source_files():
    for path in _BRIEFS_DIR.rglob("*.py"):
        parts = set(path.parts)
        if "tests" in parts or "migrations" in parts:
            continue
        yield path


class TestMarketContextNotOrderedOrFiltered(unittest.TestCase):
    def test_briefs_source_never_orders_or_filters_by_an_index_context_column(self):
        offenders = [
            str(p.relative_to(_BRIEFS_DIR))
            for p in _source_files()
            if _ORDER_OR_FILTER.search(p.read_text(encoding="utf-8"))
        ]
        self.assertEqual(offenders, [])

    def test_positive_control_regex_would_catch_a_market_state_leak(self):
        # If the scan rotted to always-pass, this planted sample would slip by.
        self.assertRegex('qs.order_by("-market_state_dist200")', _ORDER_OR_FILTER)

    def test_positive_control_regex_would_catch_an_index_return_leak(self):
        # The second form needs its OWN control: the earlier pattern matched
        # only ``market_state`` and would have read this line as clean.
        self.assertRegex('qs.order_by("-market_ret5_qqq")', _ORDER_OR_FILTER)


if __name__ == "__main__":
    unittest.main()
