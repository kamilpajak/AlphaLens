from __future__ import annotations

import unittest

from alphalens_pipeline.data.alt_data.saxo_marketdata_client import (
    LIVE_API_BASE_URL,
    SaxoMarketDataClient,
)


class _Resp:
    def __init__(self, status: int, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = str(self._payload)

    def json(self):
        return self._payload


class _Session:
    """Records calls so the test asserts on URL and body, not on transport."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    def _next(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self._responses.pop(0)

    def get(self, url, **kw):
        return self._next("GET", url, **kw)

    def post(self, url, **kw):
        return self._next("POST", url, **kw)

    def patch(self, url, **kw):
        return self._next("PATCH", url, **kw)

    def delete(self, url, **kw):
        return self._next("DELETE", url, **kw)


class _Tokens:
    def access_token(self):
        return "tok"


def _client(session):
    return SaxoMarketDataClient(token_provider=_Tokens(), session=session)


class TestElevateSession(unittest.TestCase):
    def test_patches_trade_level_and_reports_success_on_202(self):
        s = _Session(_Resp(202))
        self.assertTrue(_client(s).elevate_session())
        method, url, kw = s.calls[0]
        self.assertEqual(method, "PATCH")
        self.assertEqual(url, f"{LIVE_API_BASE_URL}/root/v1/sessions/capabilities")
        self.assertEqual(kw["json"], {"TradeLevel": "FullTradingAndChat"})

    def test_reports_failure_without_raising(self):
        """A failed elevation must degrade to 'not elevated', never crash the
        daemon tick."""
        self.assertFalse(_client(_Session(_Resp(403))).elevate_session())


class TestResolveUic(unittest.TestCase):
    def test_picks_the_exact_symbol_match(self):
        payload = {
            "Data": [
                {"Symbol": "AAPLX:xnas", "Identifier": 999},
                {"Symbol": "AAPL:xnas", "Identifier": 211},
            ]
        }
        self.assertEqual(
            _client(_Session(_Resp(200, payload))).resolve_uic("AAPL", exchange_mic="XNAS"), 211
        )

    def test_returns_none_when_no_exact_match(self):
        payload = {"Data": [{"Symbol": "AAPLX:xnas", "Identifier": 999}]}
        self.assertIsNone(
            _client(_Session(_Resp(200, payload))).resolve_uic("AAPL", exchange_mic="XNAS")
        )

    def test_picks_the_row_for_the_requested_venue_not_the_first_row(self):
        """A ticker listed on more than one venue must resolve to the
        instrument on the REQUESTED venue, never whichever row Saxo happens
        to list first. Fixture orders the WRONG-venue row first so the test
        fails if resolution ever falls back to first-match."""
        payload = {
            "Data": [
                {"Symbol": "AAPL:xnys", "Identifier": 111},
                {"Symbol": "AAPL:xnas", "Identifier": 211},
            ]
        }
        self.assertEqual(
            _client(_Session(_Resp(200, payload))).resolve_uic("AAPL", exchange_mic="XNAS"), 211
        )

    def test_returns_none_and_warns_on_ambiguous_match_within_the_same_venue(self):
        """Two rows sharing ticker AND venue is unresolvable ambiguity: fail
        closed with None (never raise — this is called from a daemon tick)
        and log a warning naming the ticker, the MIC and the match count."""
        payload = {
            "Data": [
                {"Symbol": "AAPL:xnas", "Identifier": 211},
                {"Symbol": "AAPL:xnas", "Identifier": 999},
            ]
        }
        logger_name = "alphalens_pipeline.data.alt_data.saxo_marketdata_client"
        with self.assertLogs(logger_name, level="WARNING") as cm:
            got = _client(_Session(_Resp(200, payload))).resolve_uic("AAPL", exchange_mic="XNAS")
        self.assertIsNone(got)
        self.assertTrue(
            any("AAPL" in line and "XNAS" in line and "2" in line for line in cm.output),
            f"expected a warning naming ticker, MIC and match count; got {cm.output}",
        )

    def test_near_miss_symbol_is_not_matched(self):
        """``AAPLX:xnas`` must never satisfy a request for ``AAPL`` on
        ``XNAS`` — the match is on the full symbol, not a prefix."""
        payload = {"Data": [{"Symbol": "AAPLX:xnas", "Identifier": 999}]}
        self.assertIsNone(
            _client(_Session(_Resp(200, payload))).resolve_uic("AAPL", exchange_mic="XNAS")
        )

    def test_unknown_venue_returns_none_without_raising(self):
        """A MIC outside the shared Saxo venue map is refused up front —
        None, no HTTP call, never a raise (unlike the SIM order-resolution
        path, this client is called from a daemon tick, so 'ambiguous or
        unsupported' must always degrade to 'do nothing')."""
        self.assertIsNone(_client(_Session()).resolve_uic("AAPL", exchange_mic="ZZZZ"))

    def test_non_200_search_returns_none_without_alias_retry(self):
        """A failed HTTP search is transport doubt, not 'not listed' — None
        immediately, no alias re-search (the alias step only interprets a
        SUCCESSFUL search)."""
        session = _Session(_Resp(500))
        self.assertIsNone(_client(session).resolve_uic("LAC", exchange_mic="XNYS"))
        self.assertEqual(len(session.calls), 1)


class TestResolveUicTickerAlias(unittest.TestCase):
    """``SAXO_TICKER_ALIASES`` consulted AFTER the exact ticker match fails —
    the LAC -> LAC_NEW case (Saxo renamed the listing post-2023 corporate
    split; live-verified 2026-08-12, uic 38022146). The (symbol, venue)
    pair-matching strictness is unchanged."""

    def test_exact_ticker_match_wins_over_alias(self):
        """A row matching the market ticker itself must win — the alias is a
        fallback, never a preference."""
        payload = {
            "Data": [
                {"Symbol": "LAC:xnys", "Identifier": 111},
                {"Symbol": "LAC_NEW:xnys", "Identifier": 38022146},
            ]
        }
        self.assertEqual(
            _client(_Session(_Resp(200, payload))).resolve_uic("LAC", exchange_mic="XNYS"), 111
        )

    def test_alias_row_resolves_when_exact_match_fails(self):
        """Saxo's fuzzy Keywords search DOES return the LAC_NEW row for
        Keywords=LAC — only the exact symbol match rejects it, so the alias
        retry must find it without a second HTTP call."""
        payload = {"Data": [{"Symbol": "LAC_NEW:xnys", "Identifier": 38022146}]}
        session = _Session(_Resp(200, payload))
        self.assertEqual(_client(session).resolve_uic("LAC", exchange_mic="XNYS"), 38022146)
        self.assertEqual(len(session.calls), 1)

    def test_alias_researches_when_primary_search_returns_no_rows(self):
        """An EMPTY primary keyword search re-searches with the alias keyword
        (the aliased symbol may not surface for the market-ticker keyword at
        all)."""
        empty = _Resp(200, {"Data": []})
        alias_hit = _Resp(200, {"Data": [{"Symbol": "LAC_NEW:xnys", "Identifier": 38022146}]})
        session = _Session(empty, alias_hit)
        self.assertEqual(_client(session).resolve_uic("LAC", exchange_mic="XNYS"), 38022146)
        self.assertEqual(len(session.calls), 2)
        _method, _url, kw = session.calls[1]
        self.assertEqual(kw["params"]["Keywords"], "LAC_NEW")

    def test_no_alias_no_match_stays_none_without_second_search(self):
        """A ticker with no alias entry keeps today's single-search contract:
        no match -> None, never a second HTTP round-trip."""
        session = _Session(_Resp(200, {"Data": []}))
        self.assertIsNone(_client(session).resolve_uic("MP", exchange_mic="XNYS"))
        self.assertEqual(len(session.calls), 1)

    def test_alias_ambiguity_still_refuses(self):
        """Two rows sharing the ALIASED symbol on the requested venue keep the
        fail-closed contract: None + warning, exactly like a direct-match
        ambiguity."""
        payload = {
            "Data": [
                {"Symbol": "LAC_NEW:xnys", "Identifier": 38022146},
                {"Symbol": "LAC_NEW:xnys", "Identifier": 999},
            ]
        }
        logger_name = "alphalens_pipeline.data.alt_data.saxo_marketdata_client"
        with self.assertLogs(logger_name, level="WARNING") as cm:
            got = _client(_Session(_Resp(200, payload))).resolve_uic("LAC", exchange_mic="XNYS")
        self.assertIsNone(got)
        self.assertTrue(any("LAC" in line and "2" in line for line in cm.output))


class TestPriceSubscription(unittest.TestCase):
    def test_create_accepts_201_and_sends_the_measured_body(self):
        snapshot = {"RefreshRate": 1000, "Snapshot": {"Data": []}}
        s = _Session(_Resp(201, snapshot))
        got = _client(s).create_price_subscription(
            context_id="ctx", reference_id="px", uics=[211, 1249]
        )
        self.assertEqual(got["RefreshRate"], 1000)
        method, url, kw = s.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, f"{LIVE_API_BASE_URL}/trade/v1/infoprices/subscriptions")
        body = kw["json"]
        self.assertEqual(body["ContextId"], "ctx")
        self.assertEqual(body["ReferenceId"], "px")
        self.assertEqual(body["Format"], "application/json")
        self.assertEqual(body["Arguments"]["Uics"], "211,1249")
        self.assertEqual(body["Arguments"]["AssetType"], "Stock")

    def test_delete_is_quiet_on_404(self):
        """Deleting an already-gone subscription is not an error."""
        _client(_Session(_Resp(404))).delete_price_subscription("ctx", "px")


if __name__ == "__main__":
    unittest.main()


class TestGetStockInfoprice(unittest.TestCase):
    """One-shot GET /trade/v1/infoprices snapshot (the day-1 gap gate's
    session-open source)."""

    def test_requests_the_uic_with_price_info_details(self):
        session = _Session(_Resp(200, {"PriceInfoDetails": {"Open": 7.92}}))
        payload = _client(session).get_stock_infoprice(6820)

        self.assertEqual(payload["PriceInfoDetails"]["Open"], 7.92)
        method, url, kw = session.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(url, f"{LIVE_API_BASE_URL}/trade/v1/infoprices")
        self.assertEqual(kw["params"]["Uic"], 6820)
        self.assertEqual(kw["params"]["AssetType"], "Stock")
        self.assertIn("PriceInfoDetails", kw["params"]["FieldGroups"])

    def test_non_2xx_raises(self):
        session = _Session(_Resp(400))
        with self.assertRaises(RuntimeError):
            _client(session).get_stock_infoprice(6820)
