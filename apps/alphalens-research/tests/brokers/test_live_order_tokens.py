"""LiveOrderTokenProvider (design memo §2 "Token provider") — the adapter
closing the ``LiveTokenProvider`` invalidate/dead-latch gap.

``data.alt_data.saxo_marketdata_auth.LiveTokenProvider`` keeps no in-memory
rejected-token state and has no ``invalidate``; a 401 on a LIVE order call
would re-read the same disk token and tight-loop. This adapter (a) mirrors
the SIM ``OAuthTokenProvider`` rejected-token pattern
(``tokens.py:397-404``) and (b) latches the chain permanently dead on the
first refresh failure (SIM ``_chain_lost`` pattern) so a revoked chain can
never produce a refresh storm against Saxo.

Fully hermetic: the fake underlying provider never touches disk or network,
and no env var / real credential is set anywhere in this file.
"""

from __future__ import annotations

import unittest

from alphalens_pipeline.brokers.automanager.session_keeper import SessionKeeper
from alphalens_pipeline.brokers.saxo.errors import SaxoAuthError
from alphalens_pipeline.brokers.saxo.live_tokens import LiveOrderTokenProvider


class _FakeUnderlying:
    """Scriptable stand-in for ``saxo_marketdata_auth.LiveTokenProvider``.

    ``access_token_queue`` / ``force_refresh_queue`` are consumed one call at
    a time; an entry may be a token string or an exception INSTANCE to raise.
    """

    def __init__(self, *, access_token_queue=None, force_refresh_queue=None):
        self._access_token_queue = list(access_token_queue or [])
        self._force_refresh_queue = list(force_refresh_queue or [])
        self.access_token_calls = 0
        self.force_refresh_calls = 0

    def access_token(self) -> str:
        self.access_token_calls += 1
        return self._consume(self._access_token_queue)

    def force_refresh(self) -> str:
        self.force_refresh_calls += 1
        return self._consume(self._force_refresh_queue)

    @staticmethod
    def _consume(queue: list):
        if not queue:
            raise AssertionError("_FakeUnderlying queue exhausted — test scripted too few calls")
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class _RecordingAlert:
    def __init__(self):
        self.messages: list[str] = []

    def __call__(self, message: str) -> None:
        self.messages.append(message)


class LiveOrderTokenProviderHappyPathTests(unittest.TestCase):
    def test_get_access_token_passes_through_underlying(self) -> None:
        underlying = _FakeUnderlying(access_token_queue=["tok-1"])
        adapter = LiveOrderTokenProvider(underlying)

        token = adapter.get_access_token()

        self.assertEqual(token, "tok-1")
        self.assertEqual(underlying.access_token_calls, 1)
        self.assertEqual(underlying.force_refresh_calls, 0)


class LiveOrderTokenProviderInvalidateTests(unittest.TestCase):
    def test_invalidate_then_same_disk_token_forces_exactly_one_refresh(self) -> None:
        underlying = _FakeUnderlying(
            access_token_queue=["tok-stale", "tok-stale"],
            force_refresh_queue=["tok-fresh"],
        )
        adapter = LiveOrderTokenProvider(underlying)

        first = adapter.get_access_token()
        self.assertEqual(first, "tok-stale")

        adapter.invalidate()
        second = adapter.get_access_token()

        self.assertEqual(second, "tok-fresh")
        self.assertEqual(underlying.access_token_calls, 2)
        self.assertEqual(underlying.force_refresh_calls, 1)

    def test_invalidate_before_any_get_is_a_harmless_noop(self) -> None:
        underlying = _FakeUnderlying(access_token_queue=["tok-1"])
        adapter = LiveOrderTokenProvider(underlying)

        adapter.invalidate()
        token = adapter.get_access_token()

        self.assertEqual(token, "tok-1")
        self.assertEqual(underlying.force_refresh_calls, 0)

    def test_disk_already_rotated_past_the_rejected_token_skips_refresh(self) -> None:
        """If the disk store already moved past the rejected token (a sibling
        rotated it), the adapter must not force a redundant refresh."""
        underlying = _FakeUnderlying(access_token_queue=["tok-stale", "tok-already-fresh"])
        adapter = LiveOrderTokenProvider(underlying)

        adapter.get_access_token()
        adapter.invalidate()
        second = adapter.get_access_token()

        self.assertEqual(second, "tok-already-fresh")
        self.assertEqual(underlying.force_refresh_calls, 0)


class LiveOrderTokenProviderDeadLatchTests(unittest.TestCase):
    def test_refresh_failure_alerts_once_and_raises(self) -> None:
        underlying = _FakeUnderlying(
            access_token_queue=["tok-stale", "tok-stale"],
            force_refresh_queue=[SaxoAuthError("invalid_grant")],
        )
        alert = _RecordingAlert()
        adapter = LiveOrderTokenProvider(underlying, alert=alert)
        adapter.get_access_token()
        adapter.invalidate()

        with self.assertRaises(SaxoAuthError):
            adapter.get_access_token()

        self.assertEqual(len(alert.messages), 1)

    def test_second_call_after_latch_raises_without_realerting_or_calling_underlying(
        self,
    ) -> None:
        underlying = _FakeUnderlying(
            access_token_queue=["tok-stale", "tok-stale"],
            force_refresh_queue=[SaxoAuthError("invalid_grant")],
        )
        alert = _RecordingAlert()
        adapter = LiveOrderTokenProvider(underlying, alert=alert)
        adapter.get_access_token()
        adapter.invalidate()
        with self.assertRaises(SaxoAuthError):
            adapter.get_access_token()

        calls_before = (underlying.access_token_calls, underlying.force_refresh_calls)
        with self.assertRaises(SaxoAuthError):
            adapter.get_access_token()

        self.assertEqual(
            (underlying.access_token_calls, underlying.force_refresh_calls), calls_before
        )
        self.assertEqual(len(alert.messages), 1)

    def test_bare_access_token_failure_also_latches(self) -> None:
        """A failure inside ``access_token()`` itself (the underlying's own
        internal refresh-on-near-expiry path) is the same terminal signal as
        a ``force_refresh`` failure — it must latch too."""
        underlying = _FakeUnderlying(access_token_queue=[SaxoAuthError("invalid_grant")])
        alert = _RecordingAlert()
        adapter = LiveOrderTokenProvider(underlying, alert=alert)

        with self.assertRaises(SaxoAuthError):
            adapter.get_access_token()

        self.assertEqual(len(alert.messages), 1)
        with self.assertRaises(SaxoAuthError):
            adapter.get_access_token()
        self.assertEqual(len(alert.messages), 1)
        self.assertEqual(underlying.access_token_calls, 1)

    def test_refresh_now_delegates_and_latches_on_failure(self) -> None:
        underlying = _FakeUnderlying(force_refresh_queue=[SaxoAuthError("invalid_grant")])
        alert = _RecordingAlert()
        adapter = LiveOrderTokenProvider(underlying, alert=alert)

        with self.assertRaises(SaxoAuthError):
            adapter.refresh_now()

        self.assertEqual(len(alert.messages), 1)
        with self.assertRaises(SaxoAuthError):
            adapter.refresh_now()
        self.assertEqual(len(alert.messages), 1)
        self.assertEqual(underlying.force_refresh_calls, 1)

    def test_refresh_now_happy_path_returns_token(self) -> None:
        underlying = _FakeUnderlying(force_refresh_queue=["tok-rotated"])
        adapter = LiveOrderTokenProvider(underlying)

        token = adapter.refresh_now()

        self.assertEqual(token, "tok-rotated")
        self.assertEqual(underlying.force_refresh_calls, 1)

    def test_latch_via_get_access_token_also_blocks_refresh_now(self) -> None:
        underlying = _FakeUnderlying(access_token_queue=[SaxoAuthError("invalid_grant")])
        alert = _RecordingAlert()
        adapter = LiveOrderTokenProvider(underlying, alert=alert)

        with self.assertRaises(SaxoAuthError):
            adapter.get_access_token()

        with self.assertRaises(SaxoAuthError):
            adapter.refresh_now()

        self.assertEqual(len(alert.messages), 1)
        self.assertEqual(underlying.force_refresh_calls, 0)


class LiveOrderTokenProviderSessionKeeperIntegrationTests(unittest.TestCase):
    def test_session_keeper_reports_not_alive_after_latch_instead_of_crashing(self) -> None:
        underlying = _FakeUnderlying(access_token_queue=[SaxoAuthError("invalid_grant")])
        adapter = LiveOrderTokenProvider(underlying, alert=_RecordingAlert())
        keeper = SessionKeeper(adapter)

        first_status = keeper.ensure_alive()
        self.assertFalse(first_status.alive)

        second_status = keeper.ensure_alive()
        self.assertFalse(second_status.alive)
        self.assertEqual(underlying.access_token_calls, 1)

    def test_session_keeper_keep_alive_reports_not_alive_after_refresh_failure(self) -> None:
        underlying = _FakeUnderlying(force_refresh_queue=[SaxoAuthError("invalid_grant")])
        adapter = LiveOrderTokenProvider(underlying, alert=_RecordingAlert())
        keeper = SessionKeeper(adapter)

        status = keeper.keep_alive()

        self.assertFalse(status.alive)


if __name__ == "__main__":
    unittest.main()
