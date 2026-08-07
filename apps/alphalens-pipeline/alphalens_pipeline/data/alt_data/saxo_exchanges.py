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

MIC_TO_SAXO_EXCHANGE_ID: dict[str, str] = {
    "XNYS": "NYSE",
    "XNAS": "NASDAQ",
    "XWAR": "WSE",
}
