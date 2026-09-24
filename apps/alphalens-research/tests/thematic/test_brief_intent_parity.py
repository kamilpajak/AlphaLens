"""Golden parity: the brief producer plus the door arm what `broker arm` armed (#1469).

The fixtures were captured from `broker arm` on four real brief rows before it was
deleted (see `tests/fixtures/brief_intent_parity/README.md`). Each row's producer
document goes through the real `arm` command into an empty inbox, and the
journaled intent must equal the captured one in every field but `meta.armed_ts`,
which is the moment of arming. `intent_id` and `generation` are compared too: an
empty inbox gives generation 1, and the door names that pick `TICKER:DATE`, as
`broker arm` did.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import math
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from alphalens_pipeline.paper.brief_loader import CandidateBrief
from typer.testing import CliRunner

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "brief_intent_parity"
ARMING_MOMENT = dt.datetime(2026, 9, 16, 15, 0, tzinfo=dt.UTC)
R_TOLERANCE = 1e-9


def _fixtures() -> list[dict]:
    return [
        json.loads(path.read_text(encoding="utf-8")) for path in sorted(FIXTURES.glob("*.json"))
    ]


def _candidate(fixture: dict) -> CandidateBrief:
    return CandidateBrief(
        brief_date=dt.date.fromisoformat(fixture["brief_date"]),
        ticker=fixture["ticker"],
        theme="fixture",
        verified=True,
        suggested_size_pct=fixture["brief_trade_setup"]["suggested_size_pct"],
        trade_setup=fixture["brief_trade_setup"],
        layer4_weighted_score=None,
        scorer_config_version="fixture",
    )


def _without_r_and_armed_ts(intent: dict) -> tuple[dict, list[float]]:
    trimmed = copy.deepcopy(intent)
    del trimmed["meta"]["armed_ts"]
    r_multiples = [tranche.pop("r_multiple") for tranche in trimmed["spec"]["tp_tranches"]]
    return trimmed, r_multiples


class TheProducerThroughTheDoorArmsWhatBrokerArmArmed(unittest.TestCase):
    def test_there_is_a_fixture_for_each_captured_shape(self) -> None:
        shapes = {
            (
                len(f["armed_intent"]["spec"]["entry_tiers"]),
                len(f["armed_intent"]["spec"]["tp_tranches"]),
            )
            for f in _fixtures()
        }
        self.assertEqual(shapes, {(3, 3), (3, 2), (3, 1), (2, 1)})

    def test_every_fixture_row(self) -> None:
        from alphalens_cli.commands.broker import broker_app
        from alphalens_pipeline.brokers.automanager.picks import read_pick_fold
        from alphalens_pipeline.thematic.brief_intent import brief_intent_document

        for fixture in _fixtures():
            with self.subTest(ticker=fixture["ticker"]), TemporaryDirectory() as tmp:
                home = Path(tmp)
                document = brief_intent_document(
                    [_candidate(fixture)],
                    ticker=fixture["ticker"],
                    brief_date=dt.date.fromisoformat(fixture["brief_date"]),
                    currency=fixture["currency"],
                    frame=fixture["frame"],
                    exit_policy="trail",
                )
                with (
                    mock.patch("pathlib.Path.home", return_value=home),
                    mock.patch(
                        "alphalens_cli.commands.broker._arming_now", return_value=ARMING_MOMENT
                    ),
                ):
                    result = CliRunner().invoke(
                        broker_app, ["arm", "-"], input=json.dumps(document)
                    )
                    self.assertEqual(result.exit_code, 0, result.output)
                    inbox = home / ".alphalens" / "broker_orders" / "sim" / "picks.jsonl"
                    records = read_pick_fold(path=inbox).records

                self.assertEqual(len(records), 1)
                armed, armed_r = _without_r_and_armed_ts(records[0].record["intent"])
                expected, expected_r = _without_r_and_armed_ts(fixture["armed_intent"])
                self.assertEqual(armed, expected)
                self.assertEqual(len(armed_r), len(expected_r))
                for got, want in zip(armed_r, expected_r, strict=True):
                    self.assertTrue(math.isclose(got, want, abs_tol=R_TOLERANCE), (got, want))


if __name__ == "__main__":
    unittest.main()
