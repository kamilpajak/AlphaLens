"""Market-wide infrastructure shared across the pipeline (infra side, ADR 0011).

Four members:

- ``primitives`` (:mod:`alphalens_pipeline.market.primitives`) — pure
  index-level regime arithmetic.
- ``market_state`` (:mod:`alphalens_pipeline.market.market_state`) — the
  index-level market-regime ``market_state`` classifier + ``enrich``
  broadcast stamp built on those primitives.
- ``calendar`` (:mod:`alphalens_pipeline.market.calendar`) —
  exchange-session arithmetic (ISO 10383 MIC, defaults to ``XNYS``) for the
  broker-free feedback replay, the ``brokers`` GTD / TTL math and the
  thematic publication clock.
- ``bars`` (:mod:`alphalens_pipeline.market.bars`) — pure arithmetic over
  minute bars, starting with the arrival opening-window VWAP anchor.

What earns a place here is arithmetic the selection tier
(``thematic.trade_setup``) and the measurement tier (``feedback``) both read,
and which therefore must sit below both so neither imports the other.
"""
