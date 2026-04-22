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
        from alphalens.alt_data.sec_edgar_client import SecEdgarClient

        with self.assertRaises(ValueError):
            SecEdgarClient(user_agent="")

    def test_rejects_missing_contact_in_user_agent(self):
        """SEC requires a contact (email or URL) in the User-Agent header."""
        from alphalens.alt_data.sec_edgar_client import SecEdgarClient

        with self.assertRaises(ValueError):
            SecEdgarClient(user_agent="AlphaLens")

    def test_accepts_user_agent_with_email(self):
        from alphalens.alt_data.sec_edgar_client import SecEdgarClient

        SecEdgarClient(user_agent="AlphaLens research@example.com")


class TestFetchSubmissions(unittest.TestCase):
    def setUp(self):
        from alphalens.alt_data.sec_edgar_client import SecEdgarClient

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
        from alphalens.alt_data.sec_edgar_client import SecEdgarError

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
        from alphalens.alt_data.sec_edgar_client import SecEdgarClient

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

    def test_raises_on_5xx(self):
        from alphalens.alt_data.sec_edgar_client import SecEdgarError

        self.session.get.return_value = _response(500, text="server error")

        with self.assertRaises(SecEdgarError):
            self.client.fetch_form4_xml(
                cik="0000320193",
                accession_number="0000320193-25-000001",
                primary_doc="wk-form4.xml",
            )


class TestRateLimit(unittest.TestCase):
    def test_throttles_between_calls(self):
        from alphalens.alt_data.sec_edgar_client import SecEdgarClient

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


class TestTransientRetries(unittest.TestCase):
    def test_retries_on_connection_error_then_succeeds(self):
        import requests

        from alphalens.alt_data.sec_edgar_client import SecEdgarClient

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

    def test_exhausts_retries_raises(self):
        import requests

        from alphalens.alt_data.sec_edgar_client import SecEdgarClient, SecEdgarError

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
