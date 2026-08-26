"""Tests for the §10.3 planning-sd read (#1115, memo §3.4 / §6.4).

The single most important property here is NEGATIVE: the output must carry
``sd_d``, the pair count and the cluster counts, and NOTHING else — no mean,
no median, no sign, no per-row differences. §3.4's enforcement clause: if the
guard is bypassed, the historical read is a look and the memo's slot is
forfeit. The tests pin the payload's exact key set and that no per-row or
directional value can appear in it.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import pandas as pd
from scripts import exit_policy_planning_sd as psd

_MIN = 60_000


def _setup() -> dict:
    return {
        "status": "OK",
        "disaster_stop": 90.0,
        "entry_tiers": [{"limit": 100.0, "alloc_pct": 100.0}],
        "tp_tranches": [{"target": 105.0, "tranche_pct": 100.0}],
        "atr": 4.0,
        "order_ttl_days": 7,
    }


def _bars(n: int, low: float, high: float, close: float) -> pd.DataFrame:
    return pd.DataFrame([{"t": i * _MIN, "l": low, "h": high, "c": close} for i in range(n)])


class TestPayloadShapeGuard(unittest.TestCase):
    def test_payload_keys_are_exactly_the_permitted_set(self):
        payload = psd.build_payload(
            diffs_by_key={
                ("2026-06-02", "AAA"): 1.0,
                ("2026-06-02", "BBB"): 3.0,
                ("2026-06-03", "AAA"): -2.0,
            },
            excluded={"no_bars": 2},
            n0=3750.0,
            slippage_bps=40.0,
            read_ts="2026-08-26T000000Z",
        )
        self.assertEqual(
            set(payload),
            {
                "sd_d_usd",
                "n_pairs",
                "n_days",
                "n_tickers",
                "excluded_by_reason",
                "n0_usd",
                "slippage_bps",
                "read_ts_utc",
                "span",
                "note",
            },
        )

    def test_payload_carries_no_mean_sign_or_per_row_values(self):
        diffs = {
            ("2026-06-02", "AAA"): 123.456,
            ("2026-06-03", "BBB"): -77.7,
            ("2026-06-04", "CCC"): 5.0,
        }
        payload = psd.build_payload(
            diffs_by_key=diffs,
            excluded={},
            n0=3750.0,
            slippage_bps=40.0,
            read_ts="2026-08-26T000000Z",
        )
        rendered = json.dumps(payload)
        # No per-row difference may appear anywhere in the payload.
        for value in diffs.values():
            self.assertNotIn(str(abs(value)), rendered)
        # No directional/statistical leak fields.
        for banned in ("mean", "median", "sum", "delta", "sign", "min", "max"):
            self.assertNotIn(banned, set(payload))
        # sd is invariant to the SIGN of every difference — flipping all signs
        # must not change the payload beyond that invariance (positive control
        # that the payload cannot encode direction).
        flipped = psd.build_payload(
            diffs_by_key={k: -v for k, v in diffs.items()},
            excluded={},
            n0=3750.0,
            slippage_bps=40.0,
            read_ts="2026-08-26T000000Z",
        )
        self.assertEqual(payload["sd_d_usd"], flipped["sd_d_usd"])

    def test_sd_matches_hand_arithmetic(self):
        payload = psd.build_payload(
            diffs_by_key={("d1", "A"): 1.0, ("d1", "B"): 3.0},
            excluded={},
            n0=1.0,
            slippage_bps=0.0,
            read_ts="x",
        )
        # ddof=1 sample sd of {1, 3} = sqrt(2).
        self.assertAlmostEqual(payload["sd_d_usd"], 2.0**0.5, places=12)
        self.assertEqual(payload["n_pairs"], 2)
        self.assertEqual(payload["n_days"], 1)
        self.assertEqual(payload["n_tickers"], 2)


class TestSpanEnforcement(unittest.TestCase):
    def test_refuses_a_span_end_past_the_lock_date(self):
        # §3.4: the historical span ends 2026-08-23; a later end would let
        # cohort rows into the planning read.
        with self.assertRaises(SystemExit):
            psd.enforce_span("2026-05-19", "2026-08-24")

    def test_accepts_the_frozen_span(self):
        psd.enforce_span("2026-05-19", "2026-08-23")  # must not raise


class TestDriverEndToEnd(unittest.TestCase):
    def test_synthetic_store_produces_a_shape_clean_payload(self):
        import io
        import tempfile
        from contextlib import redirect_stdout
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = root / "population_ladders"
            (store / "bars").mkdir(parents=True)
            briefs = root / "thematic_briefs"
            briefs.mkdir()
            day = "2026-06-02"
            pd.DataFrame({"ticker": ["AAA"], "plannable": [True]}).to_parquet(
                store / f"{day}.parquet", index=False
            )
            pd.DataFrame(
                {
                    "ticker": ["AAA"],
                    "brief_trade_setup": [json.dumps(_setup())],
                    "technical_pct_off_52w_high": [None],
                }
            ).to_parquet(briefs / f"{day}.parquet", index=False)
            # A generous bar path covering the whole horizon: fills at 100,
            # rises through both arms' targets.
            _bars(60, 99.0, 106.5, 105.0).to_parquet(
                store / "bars" / f"AAA_{day}.parquet", index=False
            )
            with (
                mock.patch.object(psd, "STORE_DIR", store),
                mock.patch.object(psd, "BRIEFS_DIR", briefs),
                mock.patch.object(psd.sys, "argv", ["x", "--span-start", day, "--span-end", day]),
            ):
                out = io.StringIO()
                with redirect_stdout(out):
                    code = psd.main()
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertIn("sd_d_usd", payload)
        self.assertNotIn("mean", json.dumps(sorted(payload)))
        self.assertEqual(payload["n_pairs"] + sum(payload["excluded_by_reason"].values()), 1)


if __name__ == "__main__":
    unittest.main()
