"""iShares holdings-CSV client — single canonical entry point for ishares.com.

The PIT-universe refreshers (``data/alt_data/iwm_refresher.py`` for IWM /
Russell 2000, ``data/universes/ishares_refresher.py`` for the Core S&P
mid/small/500 ETFs) each pulled the undocumented iShares AJAX holdings CSV with
their own ``requests.get``. This client centralises that one host so the
``test_no_raw_ishares_http`` enforcement test can keep a shadow fetch from
creeping in (the same one-client-per-vendor doctrine as the SEC / AV / Polygon /
yfinance / GDELT clients).

Unlike the other canonical clients there is no throttle or bounded retry here:
the iShares refresh is an ad-hoc / manual universe-maintenance step (no live
systemd unit, no shared rate budget), and each refresher already owns the
resilience layer — a ``fallback_path`` to a committed YAML snapshot used when the
fetch raises. So this client deliberately RAISES on any failure (network error /
non-2xx) so that the caller's fallback path triggers; swallowing here would
silently skip the fallback.
"""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

_DEFAULT_USER_AGENT = "AlphaLens (research / personal use)"


class ISharesClient:
    """Canonical client for the iShares holdings-CSV AJAX endpoint.

    A thin, single-host HTTP wrapper: :meth:`fetch_holdings_csv` GETs an iShares
    ``*.ajax?fileType=csv`` URL and returns the raw CSV text, raising on any
    network error or non-2xx status (the caller decides whether to fall back).
    """

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        timeout: float = 30.0,
        user_agent: str = _DEFAULT_USER_AGENT,
    ):
        self._session = session or requests.Session()
        self._timeout = timeout
        self._user_agent = user_agent

    def fetch_holdings_csv(self, url: str) -> str:
        """GET ``url`` (an iShares holdings-CSV AJAX endpoint) and return the raw
        CSV text. Raises ``requests.HTTPError`` on a non-2xx response and the
        underlying ``requests`` exception on a network failure — the refresher's
        ``fallback_path`` is the resilience layer, not this client."""
        resp = self._session.get(
            url,
            headers={"User-Agent": self._user_agent},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.text


_DEFAULT_CLIENT: ISharesClient | None = None


def get_default_ishares_client() -> ISharesClient:
    """Return the process-wide default ISharesClient (lazy-initialized).

    No lock: the refreshers are single-threaded ad-hoc tools, so the simpler
    lazy init is sufficient (and a duplicate construction would be harmless — the
    client holds no shared budget)."""
    global _DEFAULT_CLIENT  # noqa: PLW0603 — lazy singleton is the documented pattern
    if _DEFAULT_CLIENT is None:
        _DEFAULT_CLIENT = ISharesClient()
    return _DEFAULT_CLIENT


def _reset_default_client_for_tests() -> None:
    """Test-only hook: clear the cached singleton so each test starts clean."""
    global _DEFAULT_CLIENT  # noqa: PLW0603 — lazy singleton is the documented pattern
    _DEFAULT_CLIENT = None


__all__ = [
    "ISharesClient",
    "get_default_ishares_client",
]
