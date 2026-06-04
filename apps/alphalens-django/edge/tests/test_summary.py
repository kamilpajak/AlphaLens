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
