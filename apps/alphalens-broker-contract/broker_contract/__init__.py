"""AlphaLens broker-contract — shared, dependency-free broker primitives.

This package is the A-tier shared leaf from the broker-manager extraction
design memo (§2.1, ``docs/research/broker_manager_extraction_and_exit_geometry_2026_07_31.md``),
mirroring the ADR 0006 ``phase-robust-backtesting`` extraction precedent:
stdlib-only value types + pure math, consumed by BOTH ``alphalens_pipeline``
and ``alphalens_research`` (and, later, by out-of-tenancy broker-manager
clients once the extraction epic physically splits the repo).

Sub-packages / modules:
    broker_contract.contract — Boundary-1 broker Protocol + frozen value types
        (the exception hierarchy, ``InstrumentRef``/``OrderState``/``Position``/
        ``AccountSnapshot``/``BracketOrderRequest``/``PlacedOrder``, the
        ``Broker`` Protocol, and the ``SupportsAmendStop``/``SupportsOcoExit``/
        ``SupportsStandaloneStop`` capability Protocols)
    broker_contract.exit_geometry — pure ATR-bracket exit-geometry leaf (§4)
    broker_contract.fx — pure FX data types (``FxRateQuote``, ``FxConversion``)
    broker_contract.price_feed — the live-market price feed seam (``PricePoint``,
        ``PriceFeed`` Protocol); ``latest(uic) -> PricePoint | None`` where
        ``None`` is the stream-health veto (live-market execution model design)
    broker_contract.sizing — the money-math half of position sizing (value
        types ``TierPlan``/``TpTranchePlan``/``SetupPlan``,
        ``TradeSetupNotPlannableError``, ``compute_setup_plan``,
        ``compute_daily_scale_factor``, ``setup_plan_gross_notional``,
        ``setup_plan_gross_guard_limit``); the brief-parse / arm-time half
        stays client-side in ``alphalens_pipeline.paper.sizing`` (2A-4a)

Dependency direction: this package must never import from
``alphalens_pipeline`` or ``alphalens_research`` — it is a pure leaf consumed
by both, not a consumer of either.
"""
