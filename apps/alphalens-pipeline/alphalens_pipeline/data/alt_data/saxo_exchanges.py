"""ISO 10383 MIC -> Saxo ExchangeId reference-data mapping.

Shared by both Saxo HTTP surfaces that resolve an instrument by (ticker,
venue): the SIM order-placement adapter
(``brokers.saxo.broker.SaxoBroker.resolve_instrument``) and the LIVE
read-only market-data client
(``data.alt_data.saxo_marketdata_client.SaxoMarketDataClient.resolve_uic``).
Extracted here on the second use rather than duplicated, and placed under
``data/`` (infrastructure) rather than ``brokers/`` so the dependency runs
brokers -> data, the same direction ``brokers/automanager`` already uses for
``alphalens_pipeline.data.alt_data.yfinance_client`` — never the reverse.

Seeded from Saxo's ``/ref/v1/exchanges`` reference data; the SAXO_LIVE_TEST=1
probe (``tests/live/test_saxo_live.py``) verifies the codes against the real
SIM gateway. Saxo display symbols carry the lowercase MIC as suffix
(``"KO:xnys"``), which the exact-symbol match in both consumers exploits.
Adding a venue = one entry here, after verifying it via ``/ref/v1/exchanges``.
"""

from __future__ import annotations

from collections.abc import Mapping

MIC_TO_SAXO_EXCHANGE_ID: dict[str, str] = {
    "XNYS": "NYSE",
    "XNAS": "NASDAQ",
    "XASE": "AMEX",  # NYSE American — live-verified UUUU:xase / uic 549463 (2026-08-12)
    "XWAR": "WSE",
    # Euronext Amsterdam cash equities — live-verified against SIM
    # /ref/v1/exchanges (ExchangeId "AMS", Mic XAMS, NL) and by resolving
    # ASML:xams / uic 1636 / EUR (2026-09-02). Map entry ONLY (#1238 PR 6):
    # XAMS stays out of every probe order and out of arm-manual's
    # SUPPORTED_MICS until its own validation arc.
    "XAMS": "AMS",
    # Deutsche Boerse Xetra cash equities — live-verified against SIM
    # /ref/v1/exchanges (ExchangeId "FSE", Mic XETR, DE) and by resolving
    # RHMG:xetr / uic 16135 / EUR (2026-09-03). CAUTION: several ExchangeIds
    # share Mic XETR (FSE, XETRA, XETR_STARS, XETR_ETF, XETR_ETP) — cash
    # equities live on FSE; the ``:xetr`` display-symbol suffix both
    # resolvers match on is MIC-based, so it covers them all. Map entry only
    # until #1271 PR 4 opens the venue in arm-manual's SUPPORTED_MICS.
    "XETR": "FSE",
}

# Market ticker -> Saxo symbol root, consulted by BOTH resolvers AFTER the
# exact ticker==symbol match fails (the alias is a Saxo-side lookup detail:
# journals, picks and alerts keep speaking the MARKET ticker). Saxo sometimes
# lists a name under a renamed symbol after a corporate action; the fuzzy
# Keywords search usually still returns the renamed row, so the alias only
# redirects the exact-symbol match (and the keyword itself when the primary
# search comes back empty). When Saxo renames a listing back (or again), the
# stale alias is REMOVED and replaced, never stacked — one market ticker maps
# to at most one current Saxo symbol.
# Every alias PINS the expected Saxo uic (zen pre-merge finding): the alias
# match is accepted ONLY when the matched row's Identifier equals the pinned
# uic, so a stale entry (Saxo renames again, or reuses the symbol root for a
# DIFFERENT company) fails to resolve instead of silently trading the wrong
# listing. Both resolvers enforce it via ``alias_expected_for``.
SAXO_TICKER_ALIASES: Mapping[str, tuple[str, int]] = {
    # Lithium Americas, NYSE — Saxo symbol "LAC_NEW:xnys", uic 38022146
    # (post-2023 corporate-split leftover; live-verified 2026-08-12).
    "LAC": ("LAC_NEW", 38022146),
    # Rheinmetall, Xetra — Saxo symbol root "RHMG", not the market ticker
    # RHM (live-verified RHMG:xetr / uic 16135 / EUR on SIM 2026-09-03).
    # A Milan listing 1RHM:xmil exists; the uic pin plus the exact ``:xetr``
    # suffix match keep the alias from resolving the wrong venue.
    "RHM": ("RHMG", 16135),
}


def alias_expected_for(ticker: str) -> tuple[str, int] | None:
    """``(saxo_symbol_root, expected_uic)`` for a market ticker, else ``None``.

    The ONE accessor both resolvers use (zen finding: the exact-then-alias
    logic lives in two modules; sharing the accessor keeps the alias RULE
    single-sourced), so an alias can never be consulted without its uic pin."""
    return SAXO_TICKER_ALIASES.get(ticker.upper())


# Ordered US venue probe list, shared by placement routing
# (``brokers.routing.resolve_us_instrument``) and the day-1 gap gate price
# probe (``brokers.automanager.control_loop``) so the two can never diverge
# on which names are resolvable. Adding a venue here widens the AMBIGUITY
# surface for every un-suffixed ticker — extend deliberately, never for
# convenience. XWAR stays EXPLICIT-ONLY (see ``brokers/routing.py``).
US_MIC_PROBE_ORDER: tuple[str, ...] = ("XNYS", "XNAS", "XASE")
