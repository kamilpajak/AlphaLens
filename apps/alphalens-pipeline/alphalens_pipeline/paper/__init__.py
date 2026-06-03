"""Shared broker-free trade-setup geometry + exchange-calendar helpers.

This package was formerly the paper-trade forward-observation harness; the
broker chain (Alpaca / Saxo clients, the plan → submit → reconcile → exit
orchestration, the trade_updates WS daemon) was decommissioned (ADR 0012).
What survives is the broker-AGNOSTIC core the broker-free feedback engines
still consume:

- ``calendar`` — exchange-session arithmetic (ISO 10383 MIC, defaults to
  ``XNYS``): session-on-or-after, advance / previous trading day, session-open
  UTC, elapsed trading days.
- ``sizing`` — the ``brief_trade_setup`` ladder validation + geometry.
- ``brief_loader`` — load the per-date brief parquet into ``CandidateBrief``
  rows (the ladder + verification flags the replay engines enumerate).
- ``constants`` — shared TTL / time-stop / sizing constants.

These are pure (calendar + parquet read + arithmetic); no broker, no live
order placement. Feedback measurement is now fully broker-free price-path
replay (see ``alphalens_pipeline.feedback``).
"""

__status__ = "ACTIVE"
