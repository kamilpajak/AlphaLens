"""The stated FX facts, and the two places they are applied (spec section 5.2.1).

ENGINE module: stdlib only.

#1592 decided that a cross-currency run is PRICED from stated facts rather than
refused. The product the daemon applies in one line
(``broker_contract/sizing.py``: ``total x rate x (1 - buffer/100)``) factorises,
and the two factors have different consumers here:

* the **buffer** scales the account-currency budget, once on the total, before
  the entry ladder splits it;
* the **rate** turns an account-currency quantity into shares, and only the
  take-profit cost gate needs that, because it compares ``min_commission`` --
  an instrument-currency magnitude -- against a notional.

Nothing else in the walk needs the rate: the sizing sites divide a notional by
a price, and ``cash / units`` is already an instrument-currency price per share,
so the rate cancels. That cancellation is why section 5 could give
``avg_entry_price`` a symbolic unit before this issue.

**Both same-currency arms RETURN their argument.** No float operation runs, so
a same-currency run reproduces every pre-#1592 number exactly rather than to
within an ulp. That is deliberate: it makes such a run the preimage every
retired literal is pinned against, and ``broker_contract.fx`` states the same
convention for the same reason -- ``None``, never a rate of 1.0, is how the
same-currency path is represented (``broker_contract/fx.py:62-65``).

The direction of ``rate`` is the contract's: instrument currency per one unit of
account currency (``broker_contract/fx.py:60``). One definition of the direction
in the repo, cited rather than restated.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Fx"]


@dataclass(frozen=True, slots=True)
class Fx:
    """The stated conversion, with ``applies`` derived from the two codes.

    A caller used to state ``fx_applies`` and could state it wrongly on a
    cross-currency document; section 8.1 measures what that cost. A derived
    value cannot contradict the codes it comes from.

    ``rate``, ``round_trip_cost_rate`` and ``sizing_buffer_pct`` are ``None``
    exactly when the two codes agree -- the configuration refuses them on a
    same-currency run, so they are never stated and inert.
    """

    account_currency: str
    instrument_currency: str
    rate: float | None
    round_trip_cost_rate: float | None
    sizing_buffer_pct: float | None

    @property
    def applies(self) -> bool:
        """Whether a conversion is in play. Both codes are already
        ``[A-Z]{3}`` -- the door refuses a lower-case document code and the
        configuration refuses a lower-case stated one -- so this is a plain
        comparison and not a normalising one."""
        return self.instrument_currency != self.account_currency

    def pair_unit(self) -> str:
        """The unit ``mid_rate`` must carry, spelling the direction in words.

        ``USD/PLN`` would read as PLN per USD under market convention, which is
        the INVERSE of what section 5.2.1 means by it, and
        ``FxRateQuote.base_currency`` names the account side, reinforcing the
        market reading. So the token says the direction instead of implying it.

        This string is the only real guard against an inverted rate: the
        derived notional the result publishes is DISPLAY, and an inverted rate
        stays positive, stays plausible, and above the fee card's knee leaves
        the gate's verdict bit-identical (section 8.1).
        """
        return f"{self.instrument_currency}_per_{self.account_currency}"

    def to_shares(self, units: float) -> float:
        """An account-currency quantity as a share count, for the cost gate."""
        if not self.applies or self.rate is None:
            return units
        return units * self.rate

    def sizing_notional(self, total: float) -> float:
        """The budget the entry ladder splits, after the settlement-drift
        haircut the drain also withholds."""
        if not self.applies or self.sizing_buffer_pct is None:
            return total
        return total * (1.0 - self.sizing_buffer_pct / 100.0)
