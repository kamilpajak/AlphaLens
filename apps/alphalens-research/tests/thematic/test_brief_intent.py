"""The brief producer: one brief row in, the author document for the arming door out (#1469).

The producer states the trade and nothing the door computes. What it must not
get wrong is the size (a percent of the frame the operator names, or the amount
given) and the identity it leaves to the door (no `generation`, so a re-run is a
new pick, never a silent replace).
"""

from __future__ import annotations

import ast
import datetime as dt
import inspect
import unittest

from alphalens_pipeline.paper.brief_loader import CandidateBrief

BRIEF_DATE = dt.date(2026, 8, 21)


def _setup(**overrides: object) -> dict:
    setup: dict = {
        "schema_version": "1.1.0",
        "status": "OK",
        "asof_close": 62.0,
        "atr": 1.5,
        "disaster_stop": 54.0,
        "suggested_size_pct": 3.0,
        "order_ttl_days": 7,
        "entry_tiers": [
            {"limit": 60.0, "alloc_pct": 60.0, "tag": "swing-low"},
            {"limit": 58.0, "alloc_pct": 40.0, "tag": "50-day MA"},
        ],
        "tp_tranches": [
            {"target": 66.0, "tranche_pct": 50.0, "r_multiple": 1.2, "tag": "overhead resistance"},
            {"target": 70.0, "tranche_pct": 50.0, "r_multiple": 2.0, "tag": "overhead resistance"},
        ],
    }
    setup.update(overrides)
    return setup


def _candidate(ticker: str = "KBH", trade_setup: dict | None = None) -> CandidateBrief:
    return CandidateBrief(
        brief_date=BRIEF_DATE,
        ticker=ticker,
        theme="housing",
        verified=True,
        suggested_size_pct=3.0,
        trade_setup=_setup() if trade_setup is None else trade_setup,
        layer4_weighted_score=1.0,
        scorer_config_version="scorer-v1-test",
    )


def _document(candidates=None, **kwargs):
    from alphalens_pipeline.thematic.brief_intent import brief_intent_document

    arguments = {"ticker": "KBH", "brief_date": BRIEF_DATE, "currency": "PLN", "frame": 24000.0}
    arguments.update(kwargs)
    return brief_intent_document([_candidate()] if candidates is None else candidates, **arguments)


class TheDocumentIsWhatAnAuthorWrites(unittest.TestCase):
    def test_it_carries_the_brief_as_the_trade(self) -> None:
        document = _document()

        self.assertEqual(document["instrument"], {"ticker": "KBH", "mic": "XNYS"})
        self.assertEqual(document["meta"], {"source": "brief", "trade_date": "2026-08-21"})
        spec = document["spec"]
        self.assertEqual(
            [(t["limit_price"], t["alloc_pct"], t["tag"]) for t in spec["entry_tiers"]],
            [(60.0, 60.0, "swing-low"), (58.0, 40.0, "50-day MA")],
        )
        self.assertEqual(spec["disaster_stop"], 54.0)
        self.assertEqual(
            [(t["price"], t["tranche_pct"], t["tag"]) for t in spec["tp_tranches"]],
            [(66.0, 50.0, "overhead resistance"), (70.0, 50.0, "overhead resistance")],
        )
        self.assertEqual(spec["order_ttl_days"], 7)
        self.assertEqual(document["exit"]["reaction_plan"][0]["kind"], "trailing_stop")

    def test_it_states_nothing_the_door_derives_or_assigns(self) -> None:
        from alphalens_pipeline.brokers.automanager.intent_door import supplied_derived_paths

        document = _document()

        self.assertEqual(supplied_derived_paths(document), [])
        # No generation: the door assigns the next free one, so a re-run is a new
        # pick and never a silent replace of the one already armed.
        self.assertNotIn("generation", document["meta"])

    def test_the_ticker_is_matched_without_case(self) -> None:
        self.assertEqual(_document(ticker="kbh")["instrument"]["ticker"], "KBH")


class TheSizeIsAnAmount(unittest.TestCase):
    def test_the_brief_percent_of_the_frame(self) -> None:
        # 3% of 24000 PLN.
        self.assertEqual(_document()["spec"]["size"], {"notional_acct": 720.0, "currency": "PLN"})

    def test_a_given_amount_ignores_the_percent(self) -> None:
        document = _document(frame=None, notional=1500.0, currency="EUR")
        self.assertEqual(document["spec"]["size"], {"notional_acct": 1500.0, "currency": "EUR"})

    def test_frame_and_notional_are_one_or_the_other(self) -> None:
        for sizing in ({"frame": None, "notional": None}, {"frame": 24000.0, "notional": 1500.0}):
            with self.subTest(sizing=sizing), self.assertRaises(ValueError):
                _document(**sizing)

    def test_a_percent_above_a_whole_frame_is_refused(self) -> None:
        from alphalens_pipeline.thematic.brief_intent import BriefIntentRefusedError

        candidate = _candidate(trade_setup=_setup(suggested_size_pct=150.0))
        with self.assertRaises(BriefIntentRefusedError):
            _document([candidate])

    def test_a_percent_of_exactly_one_hundred_is_a_whole_frame(self) -> None:
        candidate = _candidate(trade_setup=_setup(suggested_size_pct=100.0))
        self.assertEqual(_document([candidate])["spec"]["size"]["notional_acct"], 24000.0)


class TheRowMustExistAndBePlannable(unittest.TestCase):
    def test_a_ticker_not_in_the_brief_is_not_found(self) -> None:
        from alphalens_pipeline.thematic.brief_intent import BriefRowNotFoundError

        with self.assertRaises(BriefRowNotFoundError) as caught:
            _document(ticker="ZZZZ")
        self.assertIn("not in the 2026-08-21 brief", str(caught.exception))

    def test_a_row_without_a_setup_is_refused(self) -> None:
        from alphalens_pipeline.thematic.brief_intent import BriefIntentRefusedError

        candidate = _candidate()
        candidate = CandidateBrief(**{**candidate.__dict__, "trade_setup": None})
        with self.assertRaises(BriefIntentRefusedError) as caught:
            _document([candidate])
        self.assertIn("no plannable trade_setup", str(caught.exception))

    def test_an_unplannable_setup_is_refused(self) -> None:
        from alphalens_pipeline.thematic.brief_intent import BriefIntentRefusedError

        candidate = _candidate(trade_setup=_setup(status="NO_STRUCTURE"))
        with self.assertRaises(BriefIntentRefusedError) as caught:
            _document([candidate])
        self.assertIn("not plannable", str(caught.exception))


def _identifiers(source: str) -> set[str]:
    """Every name, attribute, argument and import in ``source``, lowercased.

    Docstrings are string constants and are not collected, so prose about the
    removed gate is ignored while a real identifier is caught."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name):
            names.add(node.id.lower())
        elif isinstance(node, ast.Attribute):
            names.add(node.attr.lower())
        elif isinstance(node, ast.arg):
            names.add(node.arg.lower())
        elif isinstance(node, ast.Import | ast.ImportFrom):
            names.update(alias.name.lower() for alias in node.names)
            if isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.lower())
    return names


class TheProducerSelectsNothing(unittest.TestCase):
    """A pure executor: it writes the named ticker's row and consults no
    selection policy (2026-08-03). Earnings-window avoidance and every other
    filter belong at brief creation, so a filtered-out name never reaches the
    brief. Carried over from the deleted `broker arm` tests (#1469)."""

    def test_no_earnings_identifier_in_the_module_or_the_command(self) -> None:
        import textwrap

        from alphalens_cli.commands import thematic
        from alphalens_pipeline.thematic import brief_intent

        for source in (
            inspect.getsource(brief_intent),
            textwrap.dedent(inspect.getsource(thematic.intent_command)),
        ):
            names = _identifiers(source)
            # Positive control: the walk does see the code it scans.
            self.assertTrue({"brief_intent_document", "candidates"} & names)
            self.assertEqual(sorted(n for n in names if "earnings" in n), [])


if __name__ == "__main__":
    unittest.main()
