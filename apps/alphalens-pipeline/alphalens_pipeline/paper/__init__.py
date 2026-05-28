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

Storage choice — SQLite vs Django/Postgres:
    Phase A persists everything to a local SQLite file at
    ``~/.alphalens/paper_ledger.db``. This matches the existing
    ``Layer 1 EDGAR detector`` pattern (``candidates.db``): local CLI
    tool, single-writer (daily cron + manual operator runs), no SPA
    consumer in Phase A. Postgres-via-Django is reserved for the
    SPA-served briefs path; mixing them now would force every
    ``alphalens paper plan`` invocation to bootstrap Django settings +
    a live DB connection even for ``--no-alpaca`` dry-runs.

    Phase B (when ≥10 closed positions accumulate and the SPA needs to
    surface outcomes) will mirror the SQLite ledger into a Django
    ``paper_outcomes`` model via a separate management command —
    identical pattern to how thematic-brief parquets sync into
    ``Brief`` / ``DayMeta`` via ``rebuild_briefs_cache``. SQLite remains
    the operational SoT; Postgres becomes the read-replica for the SPA.
"""

__status__ = "ACTIVE"
