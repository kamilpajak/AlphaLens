"""Recompute every number #1592 published in PROSE, so none is hand arithmetic.

The spec and the README carry figures no test pins: the section 5 example's own
summary, its derived instrument-currency notional, and the whole-share table
that compares what the entry ladder splits against what the live drain spends.
A figure nothing checks goes stale silently, and this project has lost numbers
that way. So the arithmetic lives here, runnable, and the PR that moves a figure
re-runs it.

NO SECTION USES ``intent_replay`` AS ITS OWN ORACLE. The whole-share rows come
from ``broker_contract.sizing``'s own expression; the cost-gate rows come from
the daemon and are asserted inside ``tests/intent_replay/test_cost_gate.py``
rather than printed here; the example's summary figures are derived from the
published identity of section 5, which holds without knowing how R is computed.

    uv run python -m scripts.record_intent_replay_fx_numbers
    # (run from apps/alphalens-research; no data, no network, no keys)
"""

from __future__ import annotations

import math

# The published template ``pullback-trailing-stop.json``: a 1500 EUR budget on
# KO/XNYS, rungs 68.00 at 60% and 66.50 at 40%, disaster stop 63.00.
BUDGET = 1500.0
RUNGS: tuple[tuple[float, float], ...] = ((68.0, 60.0), (66.5, 40.0))

# The section 5 example's own stated conversion and its printed prices.
MID_RATE = 1.08
BUFFER_PCT = 1.0
AVG_ENTRY = 67.83
DENOMINATOR = 1.63
PNL_CASH_PRE_BUFFER = 41.20
SPEND_PRE_BUFFER = 900.0


def ladder_split(budget: float, rate: float, buffer_pct: float) -> float:
    """What the entry ladder splits, in the INSTRUMENT's currency.

    The one FX line of ``broker_contract.sizing.SetupPlan.sizing_notional``,
    rewritten rather than imported: that property also floors to whole shares,
    which is the half this function exists to compare against.
    """
    return budget * rate * (1.0 - buffer_pct / 100.0)


def drain_spend(budget: float, rate: float, buffer_pct: float) -> float:
    """What the live drain spends: whole shares per tier, at each tier's limit."""
    sizing = ladder_split(budget, rate, buffer_pct)
    spent = 0.0
    for limit, alloc_pct in RUNGS:
        tier = sizing * alloc_pct / 100.0
        spent += max(0, math.floor(tier / limit)) * limit
    return spent


def whole_share_table() -> list[tuple[float, float, float, float, float]]:
    """The section 5 table: rate, buffer, what the ladder splits, what the drain
    spends, and the lattice gap. Both sides in ONE currency, which is what the
    rate is for -- the pre-#1592 figures compared an account-currency split
    against an instrument-currency spend and were only meaningful at a rate
    of 1.0."""
    rows = []
    for rate in (1.0, MID_RATE):
        for buffer_pct in (0.0, BUFFER_PCT):
            split = ladder_split(BUDGET, rate, buffer_pct)
            spend = drain_spend(BUDGET, rate, buffer_pct)
            rows.append((rate, buffer_pct, split, spend, split - spend))
    return rows


def example_summary() -> dict[str, float]:
    """The section 5 example's summary under the stated 1 per cent buffer.

    The buffer scales the cash fields and leaves every ratio alone, because
    ``pnl_pct_of_spent`` is a quotient of two figures it scales equally -- which
    is why R stays 1.90 and the identity of section 5 still closes.
    """
    scale = 1.0 - BUFFER_PCT / 100.0
    spend = SPEND_PRE_BUFFER * scale
    pnl = PNL_CASH_PRE_BUFFER * scale
    pct = pnl / spend * 100.0
    return {
        "notional_spent": spend,
        "pnl_cash": pnl,
        "pnl_pct_of_spent": pct,
        "r_multiple": pct / 100.0 * AVG_ENTRY / DENOMINATOR,
        "fx.notional_spent": spend * MID_RATE,
        # The counterfactual section 5 keeps: what the cash would have been had
        # the printed R of 0.72 been the intended figure.
        "pnl_cash_if_r_were_0_72": 0.72 * DENOMINATOR / AVG_ENTRY * spend,
    }


def main() -> None:
    print("section 5, the whole-share table (one currency per row):")
    print(f"  {'rate':>5} {'buffer':>7} {'ladder splits':>14} {'drain spends':>13} {'gap':>8}")
    for rate, buffer_pct, split, spend, gap in whole_share_table():
        print(f"  {rate:>5} {buffer_pct:>6}% {split:>14.2f} {spend:>13.2f} {gap:>8.2f}")
    print("\nsection 5, the example's summary under the stated buffer:")
    for key, value in example_summary().items():
        print(f"  {key:<26} {value!r}")


if __name__ == "__main__":
    main()
