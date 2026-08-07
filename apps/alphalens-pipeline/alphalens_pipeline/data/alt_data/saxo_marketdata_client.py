"""Canonical Saxo LIVE MARKET-DATA HTTP client (read-only + session capability).

Lives OUTSIDE ``brokers/`` on purpose: the SIM-only rail (ADR 0014) fails red if
a LIVE URL string appears anywhere in that package. This client never places,
amends or cancels an order; the LIVE app's trading permission stays unused.

All requests go through an injected ``requests.Session`` so this file has no
module-level raw HTTP call.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from alphalens_pipeline.data.alt_data.saxo_marketdata_auth import LiveTokenProvider

logger = logging.getLogger(__name__)

LIVE_API_BASE_URL = "https://gateway.saxobank.com/openapi"

_TIMEOUT_S = 30.0
_ELEVATED_TRADE_LEVEL = "FullTradingAndChat"
# Saxo clamps anything lower to 1000 ms (probed 2026-08-07: 0/100/500 all
# came back assigned 1000), so asking for less is noise.
_MIN_REFRESH_RATE_MS = 1000


class SaxoMarketDataClient:
    def __init__(
        self,
        *,
        token_provider: LiveTokenProvider,
        session: requests.Session | None = None,
    ):
        self._tokens = token_provider
        self._session = session or requests.Session()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._tokens.access_token()}"}

    # ----- session capability -----

    def get_capabilities(self) -> dict[str, Any]:
        resp = self._session.get(
            f"{LIVE_API_BASE_URL}/root/v1/sessions/capabilities",
            headers=self._headers(),
            timeout=_TIMEOUT_S,
        )
        return resp.json() if resp.status_code == 200 else {}

    def elevate_session(self) -> bool:
        """PATCH the session to the elevated trade level (202 on success).

        A default OAuth session is ``OrdersOnly``, which SILENTLY serves
        15-minute-delayed prices. Failure is reported, never raised: a
        non-elevated session simply means every quote carries the delayed flag
        and the freshness gate vetoes it.
        """
        resp = self._session.patch(
            f"{LIVE_API_BASE_URL}/root/v1/sessions/capabilities",
            headers={**self._headers(), "Content-Type": "application/json"},
            json={"TradeLevel": _ELEVATED_TRADE_LEVEL},
            timeout=_TIMEOUT_S,
        )
        if 200 <= resp.status_code < 300:
            return True
        logger.warning("Saxo LIVE session elevation failed: HTTP %s", resp.status_code)
        return False

    # ----- reference data -----

    def resolve_uic(self, ticker: str) -> int | None:
        """Ticker -> LIVE uic. Never assume the SIM uic is the LIVE uic."""
        resp = self._session.get(
            f"{LIVE_API_BASE_URL}/ref/v1/instruments",
            headers=self._headers(),
            params={"Keywords": ticker, "AssetTypes": "Stock"},
            timeout=_TIMEOUT_S,
        )
        if resp.status_code != 200:
            return None
        wanted = f"{ticker.upper()}:"
        for row in resp.json().get("Data", []):
            symbol = str(row.get("Symbol", "")).upper()
            if symbol.startswith(wanted) and symbol.split(":")[0] == ticker.upper():
                return int(row["Identifier"])
        return None

    # ----- subscriptions -----

    def create_price_subscription(
        self,
        *,
        context_id: str,
        reference_id: str,
        uics: list[int],
        refresh_rate_ms: int = _MIN_REFRESH_RATE_MS,
    ) -> dict[str, Any]:
        resp = self._session.post(
            f"{LIVE_API_BASE_URL}/trade/v1/infoprices/subscriptions",
            headers={**self._headers(), "Content-Type": "application/json"},
            json={
                "ContextId": context_id,
                "ReferenceId": reference_id,
                "RefreshRate": max(refresh_rate_ms, _MIN_REFRESH_RATE_MS),
                "Format": "application/json",
                "Arguments": {
                    "AssetType": "Stock",
                    "Uics": ",".join(str(u) for u in uics),
                    "FieldGroups": ["Quote", "PriceInfo", "DisplayAndFormat"],
                },
            },
            timeout=_TIMEOUT_S,
        )
        if not (200 <= resp.status_code < 300):
            raise RuntimeError(f"price subscription failed: HTTP {resp.status_code}")
        return resp.json()

    def delete_price_subscription(self, context_id: str, reference_id: str) -> None:
        """Idempotent teardown: an already-gone subscription is not an error."""
        self._session.delete(
            f"{LIVE_API_BASE_URL}/trade/v1/infoprices/subscriptions/{context_id}/{reference_id}",
            headers=self._headers(),
            timeout=_TIMEOUT_S,
        )
