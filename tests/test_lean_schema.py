import json
import tempfile
import unittest
from pathlib import Path


def _valid_payload():
    return {
        "status": "success",
        "timestamp": "2026-04-18T21:00:00+00:00",
        "version": "1.0",
        "total_scored": 123,
        "universe_size": 782,
        "rankings": [
            {
                "ticker": "aapl",  # lowercase on input is tolerated
                "rank": 1,
                "score": 0.9,
                "roc5": 0.02,
                "roc20": 0.10,
                "roc60": 0.30,
                "volume_surprise": 2.5,
                "trend_strength": 1.0,
                "breakout": True,
                "near_high": 0.95,
                "last_close": 200.0,
                "avg_dollar_volume": 500_000_000.0,
            }
        ],
    }


class TestFromDict(unittest.TestCase):
    def test_parses_valid_payload(self):
        from alphalens.archive.screeners.lean.schema import LeanOutput

        out = LeanOutput.from_dict(_valid_payload())

        self.assertEqual(out.status, "success")
        self.assertEqual(out.total_scored, 123)
        self.assertEqual(len(out.rankings), 1)
        self.assertEqual(out.rankings[0].ticker, "AAPL")  # normalised
        self.assertTrue(out.rankings[0].breakout)

    def test_missing_top_level_raises(self):
        from alphalens.archive.screeners.lean.schema import LeanOutput

        payload = _valid_payload()
        del payload["rankings"]
        with self.assertRaises(ValueError):
            LeanOutput.from_dict(payload)

    def test_version_mismatch_raises(self):
        from alphalens.archive.screeners.lean.schema import LeanOutput

        payload = _valid_payload()
        payload["version"] = "0.9"
        with self.assertRaises(ValueError):
            LeanOutput.from_dict(payload)

    def test_missing_ranking_field_raises(self):
        from alphalens.archive.screeners.lean.schema import LeanOutput

        payload = _valid_payload()
        del payload["rankings"][0]["score"]
        with self.assertRaises(ValueError):
            LeanOutput.from_dict(payload)


class TestFromFile(unittest.TestCase):
    def test_loads_from_disk(self):
        from alphalens.archive.screeners.lean.schema import LeanOutput

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.json"
            path.write_text(json.dumps(_valid_payload()))
            out = LeanOutput.from_file(path)
        self.assertEqual(out.rankings[0].ticker, "AAPL")

    def test_empty_rankings_ok(self):
        from alphalens.archive.screeners.lean.schema import LeanOutput

        payload = _valid_payload()
        payload["rankings"] = []
        out = LeanOutput.from_dict(payload)
        self.assertEqual(out.rankings, [])


if __name__ == "__main__":
    unittest.main()
