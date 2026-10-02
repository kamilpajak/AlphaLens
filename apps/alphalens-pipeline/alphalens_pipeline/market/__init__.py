"""Market-wide infrastructure shared across the pipeline (infra side, ADR 0011).

Three members:

- ``primitives`` (:mod:`alphalens_pipeline.market.primitives`) — pure
  index-level regime arithmetic.
- ``market_state`` (:mod:`alphalens_pipeline.market.market_state`) — the
  index-level market-regime ``market_state`` classifier + ``enrich``
  broadcast stamp built on those primitives.
- ``calendar`` (:mod:`alphalens_pipeline.market.calendar`) —
  exchange-session arithmetic (ISO 10383 MIC, defaults to ``XNYS``) for the
  broker-free feedback replay, the ``brokers`` GTD / TTL math and the
  thematic publication clock.
"""
