"""Unit tests for the N-gated, benchmark-relative edge summary (pure functions).

Pins the binding guardrails (memo §3):
* §3.2 — n_matured < 30 → edge + portfolio 'insufficient', NO means;
         30 ≤ n < 100 → 'early'; n ≥ 100 → 'ok'.
* §3.1 — headline central tendency is market-excess (mean + median + quantiles),
         gross_realized_r reported only as a de-emphasised secondary.
* §3.3 — open positions are a descriptive distribution, never pooled into the mean.
* deployment block is N-INDEPENDENT (returned even below the gate).
"""

from __future__ import annotations

from edge.api.summary import N_EARLY_THRESHOLD, N_GATE_THRESHOLD, build_edge_summary


def _terminal(ticker: str, *, excess: float, realized_r: float, classification="TP_FULL") -> dict:
    return {
        "ticker": ticker,
        "brief_date": "2026-05-27",
        "plannable": True,
        "terminal": True,
        "ladder_classification": classification,
        "realized_r": realized_r,
        "market_excess_return": excess,
        "forward_return": excess + 0.01,
        "holding_days_elapsed": 11,
        "realized_return_pct_of_book": 0.002,
        "realized_risk_pct": 0.01,
        "tiers_filled_count": 2.0,
        "open_r": None,
    }


def _ongoing(ticker: str, *, open_r: float) -> dict:
    return {
        "ticker": ticker,
        "brief_date": "2026-05-27",
        "plannable": True,
        "terminal": False,
        "ladder_classification": "OPEN",
        "open_r": open_r,
        "market_excess_return": None,
        "realized_r": None,
    }


def _no_fill(ticker: str) -> dict:
    return {
        "ticker": ticker,
        "brief_date": "2026-05-27",
        "plannable": True,
        "terminal": True,
        "ladder_classification": "NO_FILL",
        "realized_r": None,
        "market_excess_return": None,
        "forward_return": 0.03,
        "open_r": None,
    }


def _pending_fill(ticker: str) -> dict:
    """Non-terminal NO_FILL: entry order still live, no tier filled, no open_r mark.

    This is a tracked candidate with ZERO capital deployed — not an open position.
    """
    return {
        "ticker": ticker,
        "brief_date": "2026-05-27",
        "plannable": True,
        "terminal": False,
        "ladder_classification": "NO_FILL",
        "open_r": None,
        "tiers_filled_count": 0.0,
        "market_excess_return": None,
        "realized_r": None,
    }


class TestNGate:
    def test_below_threshold_is_insufficient_no_means(self):
        rows = [_terminal(f"T{i}", excess=0.01 * i, realized_r=0.5) for i in range(5)]
        out = build_edge_summary(rows)
        assert out["edge"]["status"] == "insufficient"
        assert out["edge"]["n_matured"] == 5
        assert out["edge"]["threshold"] == N_GATE_THRESHOLD
        # Stable shape: the key is present but nulled (N-gate hides the number).
        assert out["edge"]["market_excess_mean"] is None
        assert out["portfolio"]["status"] == "insufficient"
        assert out["portfolio"]["size_weighted_realized_r"] is None

    def test_at_threshold_is_early_with_means(self):
        rows = [_terminal(f"T{i}", excess=0.02, realized_r=0.5) for i in range(N_GATE_THRESHOLD)]
        out = build_edge_summary(rows)
        assert out["edge"]["status"] == "early"
        assert out["edge"]["n_matured"] == N_GATE_THRESHOLD
        assert abs(out["edge"]["market_excess_mean"] - 0.02) < 1e-9
        assert out["edge"]["market_excess_median"] is not None
        assert out["edge"]["market_excess_quantiles"]["p50"] is not None
        # de-emphasised gross R present as a secondary, NOT the headline.
        assert out["edge"]["gross_realized_r_mean"] is not None
        assert out["edge"]["gross_of_cost"] is True

    def test_above_early_threshold_is_ok(self):
        rows = [_terminal(f"T{i}", excess=0.02, realized_r=0.5) for i in range(N_EARLY_THRESHOLD)]
        out = build_edge_summary(rows)
        assert out["edge"]["status"] == "ok"


class TestHitRate:
    def test_hit_rate_is_share_of_strictly_positive_excess(self):
        # 18 winners (excess > 0), 11 losers (< 0), 1 flat (== 0, NOT a hit) = 30.
        rows = [_terminal(f"W{i}", excess=0.02, realized_r=0.5) for i in range(18)]
        rows += [_terminal(f"L{i}", excess=-0.01, realized_r=-0.5) for i in range(11)]
        rows += [_terminal("FLAT", excess=0.0, realized_r=0.0)]
        out = build_edge_summary(rows)
        assert out["edge"]["n_matured"] == N_GATE_THRESHOLD
        # Strict > 0: the flat row is not a hit, so 18/30, not 19/30.
        assert abs(out["edge"]["hit_rate"] - 18 / 30) < 1e-9

    def test_hit_rate_nulled_below_gate(self):
        rows = [_terminal(f"T{i}", excess=0.02, realized_r=0.5) for i in range(5)]
        out = build_edge_summary(rows)
        assert out["edge"]["status"] == "insufficient"
        assert out["edge"]["hit_rate"] is None


class TestOpenExcludedFromExpectancy:
    def test_open_positions_are_distribution_only(self):
        rows = [_terminal(f"T{i}", excess=0.02, realized_r=0.5) for i in range(N_GATE_THRESHOLD)]
        rows += [
            _ongoing("OP1", open_r=0.3),
            _ongoing("OP2", open_r=-0.2),
            _ongoing("OP3", open_r=0.1),
        ]
        out = build_edge_summary(rows)
        assert out["open_positions"]["n_open"] == 3
        assert out["open_positions"]["near_tp"] == 2
        assert out["open_positions"]["near_sl"] == 1
        # n_matured counts only the terminal excess rows, NOT the open ones.
        assert out["edge"]["n_matured"] == N_GATE_THRESHOLD

    def test_pending_fill_is_not_counted_as_open_position(self):
        # A non-terminal NO_FILL candidate (entry order still live, no tier filled)
        # has deployed zero capital and carries no open_r mark — it is NOT an open
        # position and must not inflate n_open (edge-data audit 2026-06-18).
        rows = [_ongoing("OP1", open_r=0.3), _ongoing("OP2", open_r=-0.2)]
        rows += [_pending_fill("PF1"), _pending_fill("PF2"), _pending_fill("PF3")]
        out = build_edge_summary(rows)
        assert out["open_positions"]["n_open"] == 2
        assert out["open_positions"]["near_tp"] == 1
        assert out["open_positions"]["near_sl"] == 1
        # ...but pending-fills remain plannable candidates (in the plannable denom).
        assert out["n_plannable"] == 5


class TestDeploymentIsNIndependent:
    def test_deployment_returned_below_gate(self):
        rows = [_terminal("F1", excess=0.01, realized_r=0.5), _no_fill("NF1"), _no_fill("NF2")]
        out = build_edge_summary(rows)
        # Below the gate the edge panel is insufficient...
        assert out["edge"]["status"] == "insufficient"
        # ...but the deployment block is always present.
        assert out["deployment"]["n_terminal"] == 3
        assert out["deployment"]["n_filled"] == 1
        assert out["deployment"]["n_no_fill"] == 2
        assert abs(out["deployment"]["fill_rate"] - (1 / 3)) < 1e-9


def _char_terminal(i: int, excess: float, rr: float, cls: str = "TP_FULL") -> dict:
    """Richly-varied terminal row used by the full-payload characterization."""
    return {
        "plannable": True,
        "terminal": True,
        "ladder_classification": cls,
        "realized_r": rr,
        "market_excess_return": excess,
        "forward_return": (excess or 0) + 0.01,
        "holding_days_elapsed": 8 + (i % 5),
        "realized_return_pct_of_book": 0.001 * (i + 1),
        "realized_risk_pct": 0.01 + 0.001 * (i % 3),
        "tiers_filled_count": float(1 + (i % 3)),
        "open_r": None,
    }


def _char_ongoing(open_r: float | None) -> dict:
    return {
        "plannable": True,
        "terminal": False,
        "ladder_classification": "OPEN",
        "open_r": open_r,
        "market_excess_return": None,
        "realized_r": None,
    }


def _char_no_fill() -> dict:
    return {
        "plannable": True,
        "terminal": True,
        "ladder_classification": "NO_FILL",
        "realized_r": None,
        "market_excess_return": None,
        "open_r": None,
    }


class TestFullPayloadCharacterization:
    """Golden snapshot of the WHOLE payload over a mixed population (terminal +
    no-fill + open + non-plannable), pinning every field + numeric value so the
    aggregation is provably behaviour-preserving across refactors."""

    def _mixed_rows(self) -> list[dict]:
        rows = [
            _char_terminal(i, excess=round(-0.02 + 0.002 * i, 4), rr=round(0.1 * ((i % 7) - 3), 4))
            for i in range(35)
        ]
        rows += [_char_no_fill(), _char_no_fill()]
        rows += [_char_terminal(99, excess=0.05, rr=0.4, cls="TIME_STOP")]
        rows += [_char_ongoing(0.3), _char_ongoing(-0.2), _char_ongoing(0.0), _char_ongoing(None)]
        rows += [{"plannable": False, "terminal": True}]
        return rows

    def test_full_payload_matches_golden(self):
        out = build_edge_summary(self._mixed_rows())
        assert out == {
            "n_brief": 43,
            "n_plannable": 42,
            "n_terminal": 38,
            "n_matured": 36,
            "n_gate_threshold": 30,
            "benchmark": "SPY",
            "metric_note": (
                "market_excess_return = forward_return − benchmark_window_return "
                "(same window, raw return units); gross / pre-cost; telemetry / "
                "exploratory only — not confirmatory."
            ),
            "edge": {
                "status": "early",
                "n_matured": 36,
                "threshold": 30,
                "market_excess_mean": 0.015000000000000001,
                "market_excess_median": 0.014,
                "market_excess_quantiles": {"p10": -0.014, "p50": 0.014, "p90": 0.044},
                "hit_rate": 0.6944444444444444,
                "gross_realized_r_mean": 0.011111111111111112,
                "gross_realized_r_median": 0.0,
                "gross_realized_r_n": 36,
                "holding_days_n": 36,
                "holding_days_p50": 10.0,
                "holding_days_p95": 12.0,
                "gross_of_cost": True,
                "regime_stratified": False,
            },
            "portfolio": {
                "status": "early",
                "n_matured": 36,
                "threshold": 30,
                "total_realized_contribution_pct_of_book": 0.73,
                "size_weighted_realized_r": 0.009898477157360403,
                "mean_realized_risk_pct": 0.010944444444444444,
                "mean_tiers_filled_count": 1.9444444444444444,
                "gross_of_cost": True,
            },
            "deployment": {
                "n_terminal": 38,
                "n_filled": 36,
                "n_no_fill": 2,
                "fill_rate": 0.9473684210526315,
                "no_fill_rate": 0.05263157894736842,
                "mean_tiers_filled_count": 1.9444444444444444,
            },
            "open_positions": {
                # 4 ongoing rows, but _char_ongoing(None) carries no open_r mark
                # (pending-fill / unmarkable) so it is NOT an open position → 3.
                # The flat _char_ongoing(0.0) stays (open but in neither bucket).
                "n_open": 3,
                "near_tp": 1,
                "near_sl": 1,
                "note": "descriptive only — excluded from expectancy (memo §3.3)",
            },
        }

    def test_gated_payload_nulls_stats_but_keeps_shape(self):
        # 5 terminal rows (< gate) → edge/portfolio nulled, deployment + open intact.
        rows = [_char_terminal(i, excess=0.01 * i, rr=0.5) for i in range(5)]
        rows += [_char_ongoing(0.2), _char_no_fill()]
        out = build_edge_summary(rows)
        assert out["edge"] == {
            "status": "insufficient",
            "n_matured": 5,
            "threshold": 30,
            "market_excess_mean": None,
            "market_excess_median": None,
            "market_excess_quantiles": {"p10": None, "p50": None, "p90": None},
            "hit_rate": None,
            "gross_realized_r_mean": None,
            "gross_realized_r_median": None,
            "gross_realized_r_n": 5,
            "holding_days_n": 5,
            "holding_days_p50": None,
            "holding_days_p95": None,
            "gross_of_cost": True,
            "regime_stratified": False,
        }
        assert out["portfolio"] == {
            "status": "insufficient",
            "n_matured": 5,
            "threshold": 30,
            "total_realized_contribution_pct_of_book": None,
            "size_weighted_realized_r": None,
            "mean_realized_risk_pct": None,
            "mean_tiers_filled_count": None,
            "gross_of_cost": True,
        }
        assert out["deployment"]["n_filled"] == 5
        assert out["deployment"]["n_no_fill"] == 1
        assert out["open_positions"]["n_open"] == 1
