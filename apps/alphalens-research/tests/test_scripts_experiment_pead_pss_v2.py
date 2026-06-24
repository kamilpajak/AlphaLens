"""Unit tests for the PEAD v2 experiment driver script glue.

The pre-registered SCORER lives in ``score_pead_pss.py`` (pinned by path in the
preregistration ledger). This suite covers the driver/scaffold glue in
``scripts/experiment_pead_pss_v2.py`` only — no methodology surface.
"""

import importlib
import unittest
from datetime import date, timedelta

import pandas as pd


def _import_script():
    return importlib.import_module("scripts.experiment_pead_pss_v2")


class TestFactorWindowEnd(unittest.TestCase):
    """The factor/calendar window must extend past ``is_end`` by ~1.5x the
    holding period so ``build_daily_weights`` can complete the hold window for
    events whose ``reported_date`` falls near ``is_end``. The callees
    (``load_carhart_daily`` / ``_ensure_business_calendar``) are typed on
    ``datetime.date``, so the helper must return a ``date`` — not a pandas
    ``Timestamp`` (the prior code called ``.date()`` on a value that was
    already a ``date``, raising AttributeError at smoke time)."""

    def test_returns_date_extended_by_one_point_five_holding(self) -> None:
        mod = _import_script()
        out = mod._factor_window_end(date(2018, 3, 31), 20)
        # int(20 * 1.5) == 30 calendar days
        self.assertEqual(out, date(2018, 3, 31) + timedelta(days=30))

    def test_return_type_is_plain_date(self) -> None:
        mod = _import_script()
        out = mod._factor_window_end(date(2018, 3, 31), 20)
        self.assertIsInstance(out, date)
        # ``date`` has no ``.date()`` — the original bug. Guard the contract.
        self.assertFalse(hasattr(out, "date"))


class TestRestrictToIsWindow(unittest.TestCase):
    """``weights`` is indexed by the trading calendar, which is a list of
    ``datetime.date`` (object dtype). Restricting to ``<= is_end`` must compare
    date-to-date; wrapping ``is_end`` in a ``pd.Timestamp`` raises 'Cannot
    compare Timestamp with datetime.date' against the object-dtype index."""

    def test_drops_rows_after_is_end_keeps_boundary(self) -> None:
        mod = _import_script()
        idx = [date(2018, 3, 29), date(2018, 3, 30), date(2018, 4, 2)]
        weights = pd.DataFrame({"AAA": [0.1, 0.2, 0.3]}, index=idx)
        out = mod._restrict_to_is_window(weights, date(2018, 3, 30))
        self.assertEqual(list(out.index), [date(2018, 3, 29), date(2018, 3, 30)])


if __name__ == "__main__":
    unittest.main()
