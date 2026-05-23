import unittest
from unittest.mock import MagicMock


def _response(status: int, body: dict | None = None, content: bytes | None = None, text: str = ""):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body or {}
    resp.content = content or b""
    resp.text = text or (str(body) if body else "")
    return resp


class TestSecEdgarClientInit(unittest.TestCase):
    def test_rejects_empty_user_agent(self):
        from alphalens_pipeline.data.alt_data.sec_edgar_client import SecEdgarClient

        with self.assertRaises(ValueError):
            SecEdgarClient(user_agent="")

    def test_rejects_missing_contact_in_user_agent(self):
        """SEC requires a contact (email or URL) in the User-Agent header."""
        from alphalens_pipeline.data.alt_data.sec_edgar_client import SecEdgarClient

        with self.assertRaises(ValueError):
            SecEdgarClient(user_agent="AlphaLens")

    def test_accepts_user_agent_with_email(self):
        from alphalens_pipeline.data.alt_data.sec_edgar_client import SecEdgarClient

        SecEdgarClient(user_agent="AlphaLens research@example.com")


class TestFetchSubmissions(unittest.TestCase):
    def setUp(self):
        from alphalens_pipeline.data.alt_data.sec_edgar_client import SecEdgarClient

        self.session = MagicMock()
        self.sleep = MagicMock()
        self.client = SecEdgarClient(
            user_agent="AlphaLens test@example.com",
            rate_limit_per_sec=10,
            session=self.session,
            sleep=self.sleep,
        )

    def test_fetches_submissions_for_cik(self):
        self.session.get.return_value = _response(
            200,
            {
                "cik": "320193",
                "name": "Apple Inc.",
                "filings": {"recent": {"accessionNumber": ["0000320193-25-000001"]}},
            },
        )

        data = self.client.fetch_submissions("0000320193")

        self.assertEqual(data["cik"], "320193")
        url = self.session.get.call_args[0][0]
        self.assertIn("submissions/CIK0000320193.json", url)

    def test_passes_user_agent_header(self):
        self.session.get.return_value = _response(200, {})

        self.client.fetch_submissions("0000320193")

        _, kwargs = self.session.get.call_args
        self.assertEqual(kwargs["headers"]["User-Agent"], "AlphaLens test@example.com")

    def test_raises_on_404(self):
        from alphalens_pipeline.data.alt_data.sec_edgar_client import SecEdgarError

        self.session.get.return_value = _response(404, text="Not Found")

        with self.assertRaises(SecEdgarError):
            self.client.fetch_submissions("9999999999")

    def test_retries_once_on_429(self):
        self.session.get.side_effect = [
            _response(429, text="rate limited"),
            _response(200, {"cik": "320193"}),
        ]

        self.client.fetch_submissions("0000320193")

        self.assertEqual(self.session.get.call_count, 2)
        self.sleep.assert_any_call(60)


class TestFetchForm4Xml(unittest.TestCase):
    def setUp(self):
        from alphalens_pipeline.data.alt_data.sec_edgar_client import SecEdgarClient

        self.session = MagicMock()
        self.sleep = MagicMock()
        self.client = SecEdgarClient(
            user_agent="AlphaLens test@example.com",
            rate_limit_per_sec=10,
            session=self.session,
            sleep=self.sleep,
        )

    def test_returns_raw_xml_bytes(self):
        xml = b"<ownershipDocument><issuer/></ownershipDocument>"
        self.session.get.return_value = _response(200, content=xml)

        result = self.client.fetch_form4_xml(
            cik="0000320193",
            accession_number="0000320193-25-000001",
            primary_doc="wk-form4.xml",
        )

        self.assertEqual(result, xml)

    def test_builds_archive_url_with_accession_nodashes(self):
        self.session.get.return_value = _response(200, content=b"<x/>")

        self.client.fetch_form4_xml(
            cik="0000320193",
            accession_number="0000320193-25-000001",
            primary_doc="wk-form4.xml",
        )

        url = self.session.get.call_args[0][0]
        self.assertIn("/Archives/edgar/data/320193/000032019325000001/wk-form4.xml", url)

    def test_retries_on_503_then_succeeds(self):
        """503 is transient (SEC overload) — must retry, not skip the filing."""
        xml = b"<ownershipDocument/>"
        self.session.get.side_effect = [
            _response(503, text="Service Unavailable"),
            _response(200, content=xml),
        ]

        result = self.client.fetch_form4_xml(
            cik="0000320193",
            accession_number="0000320193-25-000001",
            primary_doc="wk-form4.xml",
        )

        self.assertEqual(result, xml)
        self.assertEqual(self.session.get.call_count, 2)

    def test_retries_on_502_then_succeeds(self):
        xml = b"<ownershipDocument/>"
        self.session.get.side_effect = [
            _response(502, text="Bad Gateway"),
            _response(200, content=xml),
        ]

        result = self.client.fetch_form4_xml(
            cik="0000320193",
            accession_number="0000320193-25-000001",
            primary_doc="wk-form4.xml",
        )

        self.assertEqual(result, xml)

    def test_retries_on_500_then_succeeds(self):
        xml = b"<ownershipDocument/>"
        self.session.get.side_effect = [
            _response(500, text="Internal Server Error"),
            _response(200, content=xml),
        ]

        result = self.client.fetch_form4_xml(
            cik="0000320193",
            accession_number="0000320193-25-000001",
            primary_doc="wk-form4.xml",
        )

        self.assertEqual(result, xml)

    def test_exhausts_5xx_retries_raises(self):
        """After all 5xx retries exhausted, must raise SecEdgarError."""
        from alphalens_pipeline.data.alt_data.sec_edgar_client import SecEdgarError

        self.session.get.return_value = _response(503, text="Service Unavailable")

        with self.assertRaises(SecEdgarError):
            self.client.fetch_form4_xml(
                cik="0000320193",
                accession_number="0000320193-25-000001",
                primary_doc="wk-form4.xml",
            )
        # 3 attempts total
        self.assertEqual(self.session.get.call_count, 3)

    def test_does_not_retry_on_4xx_permanent(self):
        """404 is permanent (file missing) — no retry."""
        from alphalens_pipeline.data.alt_data.sec_edgar_client import SecEdgarError

        self.session.get.return_value = _response(404, text="Not Found")

        with self.assertRaises(SecEdgarError):
            self.client.fetch_form4_xml(
                cik="0000320193",
                accession_number="0000320193-25-000001",
                primary_doc="wk-form4.xml",
            )
        self.assertEqual(self.session.get.call_count, 1)

    def test_5xx_then_429_then_success(self):
        """Mixed transient errors: 500 → retry → 429 → retry → 200.
        Must succeed, not raise. Regression test for the Zen CR finding
        that an old version broke out of the 5xx loop when a 429 landed
        on the retry and fell through to the >=400 raise.
        """
        xml = b"<ownershipDocument/>"
        self.session.get.side_effect = [
            _response(500, text="server error"),
            _response(429, text="rate limited"),
            _response(200, content=xml),
        ]

        result = self.client.fetch_form4_xml(
            cik="0000320193",
            accession_number="0000320193-25-000001",
            primary_doc="wk-form4.xml",
        )

        self.assertEqual(result, xml)
        self.assertEqual(self.session.get.call_count, 3)

    def test_429_then_5xx_then_success(self):
        """Reverse order: 429 → retry → 503 → retry → 200. Must succeed."""
        xml = b"<ownershipDocument/>"
        self.session.get.side_effect = [
            _response(429, text="rate limited"),
            _response(503, text="Service Unavailable"),
            _response(200, content=xml),
        ]

        result = self.client.fetch_form4_xml(
            cik="0000320193",
            accession_number="0000320193-25-000001",
            primary_doc="wk-form4.xml",
        )

        self.assertEqual(result, xml)
        self.assertEqual(self.session.get.call_count, 3)


class TestCacheEviction(unittest.TestCase):
    """In-process caches (submissions JSON + Form 4 XML) must be bounded
    to prevent unbounded memory growth on long-running prewarm processes.
    FIFO eviction, capacity set to a generous default that fits 32h VPS
    prewarm in <1GB observed.
    """

    def _make_client(self):
        from alphalens_pipeline.data.alt_data.sec_edgar_client import SecEdgarClient

        session = MagicMock()
        sleep = MagicMock()
        client = SecEdgarClient(
            user_agent="AlphaLens test@example.com",
            rate_limit_per_sec=10,
            session=session,
            sleep=sleep,
        )
        return client, session

    def test_form4_xml_cache_respects_capacity(self):

        client, session = self._make_client()
        client._form4_xml_cache_capacity = 3

        # Generate 5 distinct filings; each returns unique XML.
        session.get.side_effect = [_response(200, content=f"<xml{i}/>".encode()) for i in range(5)]
        for i in range(5):
            client.fetch_form4_xml(
                cik="0000320193",
                accession_number=f"000-{i:02d}-000000",
                primary_doc="f.xml",
            )

        # Cache should only have the 3 most recent entries.
        self.assertEqual(len(client._form4_xml_cache), 3)

    def test_submissions_cache_respects_capacity(self):
        client, session = self._make_client()
        client._submissions_cache_capacity = 2

        session.get.side_effect = [_response(200, {"cik": f"{i:010d}"}) for i in range(4)]
        for i in range(4):
            client.fetch_submissions(f"{i:010d}")

        self.assertEqual(len(client._submissions_cache), 2)


class TestRateLimit(unittest.TestCase):
    def test_throttles_between_calls(self):
        from alphalens_pipeline.data.alt_data.sec_edgar_client import SecEdgarClient

        session = MagicMock()
        session.get.return_value = _response(200, {})
        sleep = MagicMock()
        client = SecEdgarClient(
            user_agent="AlphaLens test@example.com",
            rate_limit_per_sec=10,  # 100ms min interval
            session=session,
            sleep=sleep,
        )

        client.fetch_submissions("0000320193")
        client.fetch_submissions("0000789019")

        self.assertTrue(sleep.called)


class TestPublicGetHelpers(unittest.TestCase):
    """get_json / get_bytes / get_text are the public escape hatch for shadow
    callers (watchdog, thematic verification) that need to fetch SEC URLs
    not covered by the fetch_* convenience methods. They MUST go through
    the same throttle + retry + User-Agent contract as fetch_submissions.
    """

    def setUp(self):
        from alphalens_pipeline.data.alt_data.sec_edgar_client import SecEdgarClient

        self.session = MagicMock()
        self.sleep = MagicMock()
        self.client = SecEdgarClient(
            user_agent="AlphaLens test@example.com",
            rate_limit_per_sec=10,
            session=self.session,
            sleep=self.sleep,
        )

    def test_get_json_returns_parsed_dict(self):
        self.session.get.return_value = _response(200, {"hello": "world"})

        data = self.client.get_json("https://data.sec.gov/some/url.json")

        self.assertEqual(data, {"hello": "world"})

    def test_get_bytes_returns_raw_bytes(self):
        self.session.get.return_value = _response(200, content=b"<atom/>")

        data = self.client.get_bytes("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany")

        self.assertEqual(data, b"<atom/>")

    def test_get_text_decodes_bytes(self):
        self.session.get.return_value = _response(200, content="<html>über</html>".encode())

        text = self.client.get_text("https://www.sec.gov/Archives/edgar/data/x/y/z.html")

        self.assertEqual(text, "<html>über</html>")

    def test_get_text_custom_encoding(self):
        self.session.get.return_value = _response(200, content="<atom/>".encode("latin-1"))

        text = self.client.get_text(
            "https://www.sec.gov/some.xml",
            encoding="latin-1",
        )

        self.assertEqual(text, "<atom/>")

    def test_get_json_carries_user_agent(self):
        self.session.get.return_value = _response(200, {})

        self.client.get_json("https://data.sec.gov/x.json")

        _, kwargs = self.session.get.call_args
        self.assertEqual(kwargs["headers"]["User-Agent"], "AlphaLens test@example.com")

    def test_get_bytes_retries_on_429(self):
        self.session.get.side_effect = [
            _response(429, text="rate limited"),
            _response(200, content=b"OK"),
        ]

        data = self.client.get_bytes("https://www.sec.gov/x")

        self.assertEqual(data, b"OK")
        self.assertEqual(self.session.get.call_count, 2)
        self.sleep.assert_any_call(60)

    def test_get_json_raises_on_404(self):
        from alphalens_pipeline.data.alt_data.sec_edgar_client import SecEdgarError

        self.session.get.return_value = _response(404, text="Not Found")

        with self.assertRaises(SecEdgarError):
            self.client.get_json("https://data.sec.gov/missing.json")


class TestDefaultClientSingleton(unittest.TestCase):
    """Module-level get_default_sec_client() is the single entry point for
    callers that don't have their own SecEdgarClient instance to inject
    (e.g. thematic verification module-level functions). MUST be lazy
    (no side-effects at import) and MUST resolve User-Agent from
    SEC_EDGAR_USER_AGENT env var with ALPHALENS_DEFAULT_USER_AGENT as
    fallback. The fallback string must satisfy SEC's UA contract
    (email or URL).
    """

    def setUp(self):
        from alphalens_pipeline.data.alt_data import sec_edgar_client as mod

        self._mod = mod
        # Each test starts with a fresh singleton (real production code
        # has it cached for the process lifetime; tests reset to isolate).
        mod._reset_default_client_for_tests()

    def tearDown(self):
        self._mod._reset_default_client_for_tests()

    def test_default_ua_constant_is_valid_per_sec_contract(self):
        ua = self._mod.ALPHALENS_DEFAULT_USER_AGENT
        self.assertTrue("@" in ua or "http" in ua.lower(), ua)

    def test_singleton_returns_same_instance(self):
        c1 = self._mod.get_default_sec_client()
        c2 = self._mod.get_default_sec_client()

        self.assertIs(c1, c2)

    def test_singleton_uses_env_user_agent(self):
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"SEC_EDGAR_USER_AGENT": "Custom-UA contact@example.com"}):
            client = self._mod.get_default_sec_client()

        self.assertEqual(client._user_agent, "Custom-UA contact@example.com")

    def test_singleton_falls_back_to_default_ua(self):
        import os
        from unittest.mock import patch

        # Force env var absence even if operator has it set locally.
        env_without_ua = {k: v for k, v in os.environ.items() if k != "SEC_EDGAR_USER_AGENT"}
        with patch.dict(os.environ, env_without_ua, clear=True):
            client = self._mod.get_default_sec_client()

        self.assertEqual(client._user_agent, self._mod.ALPHALENS_DEFAULT_USER_AGENT)

    def test_reset_helper_creates_new_instance(self):
        c1 = self._mod.get_default_sec_client()
        self._mod._reset_default_client_for_tests()
        c2 = self._mod.get_default_sec_client()

        self.assertIsNot(c1, c2)


class TestTransientRetries(unittest.TestCase):
    def test_retries_on_connection_error_then_succeeds(self):
        import requests
        from alphalens_pipeline.data.alt_data.sec_edgar_client import SecEdgarClient

        session = MagicMock()
        session.get.side_effect = [
            requests.exceptions.ConnectionError("boom"),
            _response(200, {"cik": "320193"}),
        ]
        sleep = MagicMock()
        client = SecEdgarClient(
            user_agent="AlphaLens test@example.com",
            rate_limit_per_sec=10,
            session=session,
            sleep=sleep,
        )

        data = client.fetch_submissions("0000320193")

        self.assertEqual(data["cik"], "320193")
        self.assertEqual(session.get.call_count, 2)

    def test_retries_on_sslerror_then_succeeds(self):
        """SSLError is a RequestException subclass — must be treated as
        transient (retry), not propagated unhandled. Regression test for
        the zen review finding that the narrow _TRANSIENT_NET_EXCEPTIONS
        tuple leaked SSL/redirect noise to callers and crashed the watchdog.
        """
        import requests
        from alphalens_pipeline.data.alt_data.sec_edgar_client import SecEdgarClient

        session = MagicMock()
        session.get.side_effect = [
            requests.exceptions.SSLError("ssl handshake failed"),
            _response(200, {"cik": "320193"}),
        ]
        sleep = MagicMock()
        client = SecEdgarClient(
            user_agent="AlphaLens test@example.com",
            rate_limit_per_sec=10,
            session=session,
            sleep=sleep,
        )

        data = client.fetch_submissions("0000320193")
        self.assertEqual(data["cik"], "320193")
        self.assertEqual(session.get.call_count, 2)

    def test_exhausts_retries_raises(self):
        import requests
        from alphalens_pipeline.data.alt_data.sec_edgar_client import SecEdgarClient, SecEdgarError

        session = MagicMock()
        session.get.side_effect = requests.exceptions.Timeout("timeout")
        sleep = MagicMock()
        client = SecEdgarClient(
            user_agent="AlphaLens test@example.com",
            rate_limit_per_sec=10,
            session=session,
            sleep=sleep,
        )

        with self.assertRaises(SecEdgarError):
            client.fetch_submissions("0000320193")


if __name__ == "__main__":
    unittest.main()
