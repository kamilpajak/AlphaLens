"""SEC EDGAR HTTP client — retry, throttle, error handling.

These tests guard the long-running Form-4 backfill (5-10 days wall): a
silent regression in retry/backoff math would silently drop CIKs or trigger
SEC's polite-rate enforcement. The full path is exercised against a fake
:class:`requests.Session`; sleep is also injected so retry budget tests run
in milliseconds.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest import mock

import requests
from alphalens_pipeline.data.alt_data.sec_edgar_client import (
    SecEdgarClient,
    SecEdgarError,
    SecForbiddenError,
    SecRateCoordinator,
    _evict_to_capacity,
    _reset_default_client_for_tests,
    get_default_sec_client,
)
from alphalens_pipeline.data.alt_data.sec_rate_coordinator import SEC_COORD_PATH_ENV


class _FakeResponse:
    def __init__(
        self,
        status_code: int,
        json_payload: dict | None = None,
        text: str = "",
        headers: dict | None = None,
    ):
        self.status_code = status_code
        self._json = json_payload or {}
        self.text = text
        self.content = text.encode("utf-8") if text else b""
        self.headers = headers or {}

    def json(self) -> dict:
        return self._json


class _FakeSession:
    """Replays a queue of responses (or exceptions) per call."""

    def __init__(self, responses: list):
        self._queue = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, headers=None, timeout=None):
        self.calls.append((url, headers))
        if not self._queue:
            raise AssertionError(f"unexpected GET {url}; queue exhausted")
        item = self._queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _SleepRecorder:
    def __init__(self):
        self.sleeps: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def _make_client(session: _FakeSession, sleep: Callable[[float], None]) -> SecEdgarClient:
    return SecEdgarClient(
        user_agent="Test User test@example.com",
        session=session,
        sleep=sleep,
    )


class TestUserAgentValidation(unittest.TestCase):
    def test_empty_user_agent_rejected(self):
        with self.assertRaises(ValueError):
            SecEdgarClient(user_agent="")

    def test_user_agent_without_contact_rejected(self):
        # SEC Fair Access requires email or URL; bare name is rejected.
        with self.assertRaises(ValueError):
            SecEdgarClient(user_agent="Just A Name")

    def test_user_agent_with_email_accepted(self):
        SecEdgarClient(user_agent="Kamil Pajak kamil@example.com")  # no raise

    def test_user_agent_with_url_accepted(self):
        SecEdgarClient(user_agent="Org https://example.com")  # no raise


class TestHappyPath(unittest.TestCase):
    def test_fetch_submissions_returns_json_payload(self):
        payload = {"cik": "0000320193", "filings": {"recent": {}}}
        session = _FakeSession([_FakeResponse(200, json_payload=payload)])
        sleep = _SleepRecorder()
        client = _make_client(session, sleep)

        result = client.fetch_submissions("0000320193")
        self.assertEqual(result, payload)
        self.assertEqual(len(session.calls), 1)
        url, headers = session.calls[0]
        self.assertIn("CIK0000320193.json", url)
        self.assertIn("User-Agent", headers)
        self.assertIn("test@example.com", headers["User-Agent"])

    def test_fetch_submissions_overflow_uses_correct_url(self):
        payload = {"filings": {"recent": {"form": []}}}
        session = _FakeSession([_FakeResponse(200, json_payload=payload)])
        client = _make_client(session, _SleepRecorder())

        result = client.fetch_submissions_overflow("CIK0000320193-submissions-001.json")
        self.assertEqual(result, payload)
        url, _ = session.calls[0]
        self.assertEqual(
            url,
            "https://data.sec.gov/submissions/CIK0000320193-submissions-001.json",
        )

    def test_fetch_form4_xml_strips_leading_zeros_from_cik_in_url(self):
        session = _FakeSession([_FakeResponse(200, text="<xml/>")])
        client = _make_client(session, _SleepRecorder())

        client.fetch_form4_xml(
            cik="0000320193",
            accession_number="0000320193-22-000001",
            primary_doc="form4.xml",
        )
        url, _ = session.calls[0]
        # CIK without leading zeros, accession without dashes.
        self.assertIn("/Archives/edgar/data/320193/000032019322000001/form4.xml", url)


class TestCaching(unittest.TestCase):
    def test_fetch_submissions_caches_result(self):
        payload = {"cik": "0000320193"}
        session = _FakeSession([_FakeResponse(200, json_payload=payload)])
        client = _make_client(session, _SleepRecorder())

        client.fetch_submissions("0000320193")
        client.fetch_submissions("0000320193")  # second call hits cache
        self.assertEqual(len(session.calls), 1)

    def test_fetch_submissions_overflow_caches_result(self):
        payload = {"filings": {"recent": {"form": []}}}
        session = _FakeSession([_FakeResponse(200, json_payload=payload)])
        client = _make_client(session, _SleepRecorder())

        client.fetch_submissions_overflow("CIK0000320193-submissions-001.json")
        client.fetch_submissions_overflow("CIK0000320193-submissions-001.json")
        self.assertEqual(len(session.calls), 1)


class TestRateLimitRetry(unittest.TestCase):
    def test_429_retries_with_backoff_then_succeeds(self):
        session = _FakeSession(
            [
                _FakeResponse(429, text="rate limited"),
                _FakeResponse(200, json_payload={"ok": True}),
            ]
        )
        sleep = _SleepRecorder()
        client = _make_client(session, sleep)

        result = client.fetch_submissions("0000320193")
        self.assertEqual(result, {"ok": True})
        # Retry slept 60 s on 429 (per SEC polite guideline).
        self.assertIn(60, sleep.sleeps)

    def test_429_persistent_raises_after_max_attempts(self):
        session = _FakeSession([_FakeResponse(429, text="rate limited")] * 3)
        client = _make_client(session, _SleepRecorder())

        with self.assertRaises(SecEdgarError):
            client.fetch_submissions("0000320193")
        # Three attempts total (initial + 2 retries before final).
        self.assertEqual(len(session.calls), 3)


class TestServerErrorRetry(unittest.TestCase):
    def test_500_retries_with_exponential_backoff_then_succeeds(self):
        session = _FakeSession(
            [
                _FakeResponse(500, text="server error"),
                _FakeResponse(503, text="unavailable"),
                _FakeResponse(200, json_payload={"ok": True}),
            ]
        )
        sleep = _SleepRecorder()
        client = _make_client(session, sleep)

        result = client.fetch_submissions("0000320193")
        self.assertEqual(result, {"ok": True})
        # Backoffs are 10s then 30s on consecutive 5xx (per client constants).
        self.assertIn(10, sleep.sleeps)
        self.assertIn(30, sleep.sleeps)

    def test_mixed_500_then_429_then_success(self):
        # Real-world sequence: server hiccup followed by a rate-limit window.
        session = _FakeSession(
            [
                _FakeResponse(500),
                _FakeResponse(429),
                _FakeResponse(200, json_payload={"ok": True}),
            ]
        )
        client = _make_client(session, _SleepRecorder())

        result = client.fetch_submissions("0000320193")
        self.assertEqual(result, {"ok": True})


class TestPermanent4xx(unittest.TestCase):
    def test_404_raises_immediately_no_retry(self):
        # 404 is permanent (CIK does not exist). No retry budget should be
        # spent — every wasted request burns SEC throttle quota.
        session = _FakeSession([_FakeResponse(404, text="not found")])
        client = _make_client(session, _SleepRecorder())

        with self.assertRaises(SecEdgarError):
            client.fetch_submissions("9999999999")
        self.assertEqual(len(session.calls), 1)

    def test_403_raises_immediately(self):
        # 403 typically means User-Agent rejected — also permanent.
        session = _FakeSession([_FakeResponse(403, text="forbidden")])
        client = _make_client(session, _SleepRecorder())

        with self.assertRaises(SecEdgarError):
            client.fetch_submissions("0000320193")
        self.assertEqual(len(session.calls), 1)


class TestNetworkExceptionRetry(unittest.TestCase):
    def test_connection_error_retries_then_succeeds(self):
        session = _FakeSession(
            [
                requests.exceptions.ConnectionError("boom"),
                _FakeResponse(200, json_payload={"ok": True}),
            ]
        )
        sleep = _SleepRecorder()
        client = _make_client(session, sleep)

        result = client.fetch_submissions("0000320193")
        self.assertEqual(result, {"ok": True})
        # First inner-retry slept 5s.
        self.assertIn(5, sleep.sleeps)

    def test_timeout_retries_then_succeeds(self):
        session = _FakeSession(
            [
                requests.exceptions.Timeout("read timeout"),
                requests.exceptions.Timeout("read timeout"),
                _FakeResponse(200, json_payload={"ok": True}),
            ]
        )
        sleep = _SleepRecorder()
        client = _make_client(session, sleep)

        result = client.fetch_submissions("0000320193")
        self.assertEqual(result, {"ok": True})

    def test_exhausted_network_retries_raises(self):
        session = _FakeSession([requests.exceptions.ConnectionError("boom")] * 3)
        with self.assertRaises(SecEdgarError):
            _make_client(session, _SleepRecorder()).fetch_submissions("0000320193")


class TestThrottle(unittest.TestCase):
    def test_minimum_interval_enforced_between_requests(self):
        # Two back-to-back 200s should result in throttle sleep on the second.
        session = _FakeSession(
            [
                _FakeResponse(200, json_payload={"a": 1}),
                _FakeResponse(200, json_payload={"b": 2}),
            ]
        )
        sleep = _SleepRecorder()
        # Force a fake monotonic clock that never advances so throttle always
        # decides we need to sleep.
        with mock.patch(
            "alphalens_pipeline.data.alt_data.sec_edgar_client.time.monotonic",
            return_value=0.0,
        ):
            client = SecEdgarClient(
                user_agent="X x@example.com",
                rate_limit_per_sec=10,
                session=session,
                sleep=sleep,
            )
            client.fetch_submissions("0000320193")
            client.fetch_submissions_overflow("CIK0000320193-submissions-001.json")

        # Second call should have triggered a throttle sleep of ~0.1 s
        # (1 / rate_limit_per_sec).
        self.assertTrue(
            any(abs(s - 0.1) < 1e-6 for s in sleep.sleeps),
            f"expected ~0.1 s throttle sleep, got: {sleep.sleeps}",
        )


class TestEvictToCapacity(unittest.TestCase):
    """_evict_to_capacity FIFO eviction — mutation testing flagged its
    boundary (``> max_size`` vs ``>= max_size``) as untested.

    Contract: after the call, ``len(cache) <= max_size`` exactly.
    """

    def test_no_eviction_when_already_under_capacity(self):
        cache = {"a": 1, "b": 2}
        _evict_to_capacity(cache, max_size=5)
        self.assertEqual(cache, {"a": 1, "b": 2})

    def test_no_eviction_when_exactly_at_capacity(self):
        # Boundary: cache size == max_size should NOT trigger eviction.
        # Mutation `while len(cache) > max_size` -> `>= max_size` would
        # over-evict here (drop 1 item that should be kept).
        cache = {"a": 1, "b": 2, "c": 3}
        _evict_to_capacity(cache, max_size=3)
        self.assertEqual(len(cache), 3)
        self.assertEqual(cache, {"a": 1, "b": 2, "c": 3})

    def test_evicts_oldest_first_fifo(self):
        # Insertion order: a, b, c, d (Python 3.7+ dict preserves it).
        # Capacity 2 -> drop a, b; keep c, d.
        cache = {"a": 1, "b": 2, "c": 3, "d": 4}
        _evict_to_capacity(cache, max_size=2)
        self.assertEqual(list(cache.keys()), ["c", "d"])

    def test_evicts_down_to_zero_when_max_size_zero(self):
        cache = {"a": 1, "b": 2}
        _evict_to_capacity(cache, max_size=0)
        self.assertEqual(cache, {})

    def test_negative_max_size_clamped_to_zero(self):
        # max_size = max(max_size, 0) — negatives treated as 0 (no
        # crash, no negative-while-loop).
        cache = {"a": 1, "b": 2}
        _evict_to_capacity(cache, max_size=-5)
        self.assertEqual(cache, {})

    def test_empty_cache_no_op(self):
        cache = {}
        _evict_to_capacity(cache, max_size=10)
        self.assertEqual(cache, {})


class TestForbiddenErrorDetail(unittest.TestCase):
    """#380 — a 403 stays a non-retried immediate raise but now carries the FULL
    body + triage headers (truncating at 200 chars hid the epic #379 root cause)
    and logs at WARNING.
    """

    def test_403_still_raises_immediately_without_retry(self):
        session = _FakeSession([_FakeResponse(403, text="Request Rate Threshold Exceeded")])
        client = _make_client(session, _SleepRecorder())
        with self.assertRaises(SecForbiddenError):
            client.fetch_submissions("0000320193")
        self.assertEqual(len(session.calls), 1)

    def test_403_message_includes_full_body_not_truncated(self):
        long_body = "Request Rate Threshold Exceeded. " + ("X" * 500)
        session = _FakeSession([_FakeResponse(403, text=long_body)])
        client = _make_client(session, _SleepRecorder())
        with self.assertRaises(SecForbiddenError) as ctx:
            client.fetch_submissions("0000320193")
        self.assertIn("X" * 500, str(ctx.exception))

    def test_403_message_includes_retry_after_header_labeled(self):
        session = _FakeSession([_FakeResponse(403, text="rate", headers={"Retry-After": "600"})])
        client = _make_client(session, _SleepRecorder())
        with self.assertRaises(SecForbiddenError) as ctx:
            client.fetch_submissions("0000320193")
        msg = str(ctx.exception)
        self.assertIn("Retry-After", msg)
        self.assertIn("'Retry-After': '600'", msg)  # labeled dict repr, not bare 600

    def test_403_logs_full_body_and_headers_at_warning(self):
        long_body = "Undeclared Automated Tool. " + ("Y" * 400)
        session = _FakeSession([_FakeResponse(403, text=long_body, headers={"Retry-After": "120"})])
        client = _make_client(session, _SleepRecorder())
        with self.assertLogs(
            "alphalens_pipeline.data.alt_data.sec_edgar_client", level="WARNING"
        ) as cm:
            with self.assertRaises(SecForbiddenError):
                client.fetch_submissions("0000320193")
        logged = "\n".join(cm.output)
        self.assertIn("403", logged)
        self.assertIn("Y" * 400, logged)
        self.assertIn("Retry-After", logged)

    def test_404_still_raises_secedgarerror_not_forbidden(self):
        session = _FakeSession([_FakeResponse(404, text="not found")])
        client = _make_client(session, _SleepRecorder())
        with self.assertRaises(SecEdgarError) as ctx:
            client.fetch_submissions("9999999999")
        self.assertNotIsInstance(ctx.exception, SecForbiddenError)
        self.assertEqual(len(session.calls), 1)


class _FakeCoordinator:
    """Records wait_for_slot calls. The client only calls wait_for_slot() and
    ignores its return, so we just count invocations.
    """

    def __init__(self):
        self.calls = 0

    def wait_for_slot(self) -> float:
        self.calls += 1
        return 0.0


class TestThrottleCoordinatorIntegration(unittest.TestCase):
    """#381 — the coordinator gate fires ONCE per logical request (not per retry
    attempt), and a client with no coordinator behaves exactly as before.
    """

    def test_coordinator_called_once_per_request(self):
        coord = _FakeCoordinator()
        session = _FakeSession(
            [
                _FakeResponse(200, json_payload={"a": 1}),
                _FakeResponse(200, json_payload={"b": 2}),
            ]
        )
        client = SecEdgarClient(
            user_agent="X x@example.com",
            session=session,
            sleep=_SleepRecorder(),
            coordinator=coord,
        )
        client.fetch_submissions("0000320193")
        client.fetch_submissions_overflow("CIK0000320193-submissions-001.json")
        self.assertEqual(coord.calls, 2)

    def test_coordinator_not_recharged_on_retry(self):
        # If the gate were inside the retry loop, one logical request that retries
        # once (429 -> 200) would call it twice. It sits in _request before the
        # loop, so it is ONE gate call.
        coord = _FakeCoordinator()
        session = _FakeSession(
            [_FakeResponse(429, text="rl"), _FakeResponse(200, json_payload={"ok": True})]
        )
        client = SecEdgarClient(
            user_agent="X x@example.com",
            session=session,
            sleep=_SleepRecorder(),
            coordinator=coord,
        )
        client.fetch_submissions("0000320193")
        self.assertEqual(coord.calls, 1)

    def test_no_coordinator_no_change_in_behaviour(self):
        session = _FakeSession([_FakeResponse(200, json_payload={"a": 1})])
        client = _make_client(session, _SleepRecorder())
        self.assertIsNone(client._coordinator)
        client.fetch_submissions("0000320193")  # no raise


class TestCoordinatorWiring(unittest.TestCase):
    """#381 — get_default_sec_client wires a coordinator at the shared path."""

    def setUp(self):
        _reset_default_client_for_tests()
        self.addCleanup(_reset_default_client_for_tests)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_default_client_has_coordinator_at_override_path(self):
        # Env must be set BEFORE the first call (lazy-singleton reads env once).
        coord_path = Path(self._tmp.name) / "coord.lock"
        with mock.patch.dict(os.environ, {SEC_COORD_PATH_ENV: str(coord_path)}):
            client = get_default_sec_client()
            self.assertIsInstance(client._coordinator, SecRateCoordinator)
            self.assertEqual(client._coordinator._path, coord_path)
            # singleton: same instance + same coordinator on a second call.
            self.assertIs(get_default_sec_client(), client)


if __name__ == "__main__":
    unittest.main()
