"""Paper-trade forward-observation harness.

Sized + driven by the deterministic ``brief_trade_setup`` shipped per
candidate. NOT a strategy validation — this module is a measurement
instrument for the trade-setup ladder. See
``docs/research/paper_trading_capital_sizing_2026_05_28.md`` for the design
memo + sizing math (``N_FIXED = 360``, ``min(suggested_size_pct, 1/N_FIXED)``,
60d time-stop, no hard concurrency cap).

Paper-only by construction. The Alpaca SDK is reached exclusively through
``alphalens_pipeline.data.alt_data.alpaca_client.AlpacaClient`` which
hardcodes ``paper=True`` and rejects non-paper base URLs. The project
doctrine ``capital_deploy_clause`` keeps real capital off the table.
"""

__status__ = "ACTIVE"
