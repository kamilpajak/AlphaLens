"""Hermetic policy-brain tests for :class:`SaxoTokenManager`.

Everything is injected — clock, monotonic, transport, store — so NO network
and NO real sleep is touched. This is the heaviest unit-test file: it pins
every token-lifecycle, skew, classification, recovery, env-interlock and
reauth case from the locked design's test plan.

The manager is single-writer: ONLY :meth:`refresh` calls ``/token`` with
grant_type=refresh_token. :meth:`get_access_token` reads the file, uses a
fresh token, and fails loud if missing/expired — it NEVER refreshes.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from alphalens_pipeline.data.alt_data.saxo_client import (
    SaxoClient,
    SaxoEnvironmentMismatchError,
    SaxoReauthRequiredError,
    SaxoTokenContractError,
    SaxoTransientError,
)
from alphalens_pipeline.data.alt_data.saxo_token_manager import (
    ACCESS_SAFETY_MARGIN_S,
    MAX_TOLERATED_CLOCK_SKEW_S,
    REFRESH_SAFETY_MARGIN_S,
    SaxoBootstrapNeededError,
    SaxoTokenManager,
)
from alphalens_pipeline.data.alt_data.saxo_token_store import (
    SaxoTokenRecord,
    SaxoTokenStore,
)


class _FakeClock:
    """Independently steppable wall + monotonic clocks."""

    def __init__(self, wall: float = 10_000.0, mono: float = 0.0) -> None:
        self._wall = wall
        self._mono = mono

    def wall(self) -> float:
        return self._wall

    def mono(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        self._wall += seconds
        self._mono += seconds

    def step_wall(self, seconds: float) -> None:
        self._wall += seconds

    def step_mono(self, seconds: float) -> None:
        self._mono += seconds


def _record(clock: _FakeClock, **overrides: object) -> SaxoTokenRecord:
    now = clock.wall()
    base: dict[str, object] = {
        "schema_version": 1,
        "environment": "sim",
        "access_token": "AT-current",
        "refresh_token": "RT-current",
        "previous_refresh_token": None,
        "access_token_expires_at": now + 1200.0,
        "refresh_token_expires_at": now + 2400.0,
        "rotated_at": now,
        "reauth_required": False,
        "reauth_reason": "none",
        "journal_state": "active",
        "journal_attempted_at": None,
        "last_full_auth_at": now,
    }
    base.update(overrides)
    return SaxoTokenRecord(**base)  # type: ignore[arg-type]


def _token_response(*, access="AT-new", refresh="RT-new", expires_in=1200, refresh_expires_in=2400):
    body: dict[str, object] = {"access_token": access, "expires_in": expires_in}
    if refresh is not None:
        body["refresh_token"] = refresh
    if refresh_expires_in is not None:
        body["refresh_token_expires_in"] = refresh_expires_in
    return httpx.Response(200, json=body)


class _ManagerFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.clock = _FakeClock()
        self.slept: list[float] = []
        self.posts: list[dict] = []
        self.store = SaxoTokenStore(self.dir, environment="sim", clock=self.clock.wall)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _make_client(self, handler) -> SaxoClient:
        def wrap(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/token"):
                self.posts.append(dict(request.url.params))
            return handler(request)

        return SaxoClient(
            app_key="K",
            redirect_uri="https://x.invalid/cb",
            environment="sim",
            _transport=httpx.MockTransport(wrap),
        )

    def _sleep(self, seconds: float) -> None:
        # Injected sleep advances the fake clock (both wall + monotonic) so the
        # deadline-bounded retry loop makes progress toward the deadline,
        # exactly as a real sleep would advance the wall clock.
        self.slept.append(seconds)
        self.clock.advance(seconds)

    def _manager(self, handler, *, environment: str = "sim") -> SaxoTokenManager:
        client = self._make_client(handler)
        store = SaxoTokenStore(self.dir, environment=environment, clock=self.clock.wall)
        return SaxoTokenManager(
            store=store,
            client=client,
            environment=environment,
            wall_clock=self.clock.wall,
            mono_clock=self.clock.mono,
            sleep=self._sleep,
        )


class TestGetAccessTokenReadOnly(_ManagerFixture):
    def test_fresh_token_outside_margin_returns_cached_no_post(self) -> None:
        self.store.write(_record(self.clock))
        mgr = self._manager(lambda r: _token_response())
        token = mgr.get_access_token()
        self.assertEqual(token, "AT-current")
        self.assertEqual(self.posts, [], "read path must never POST /token")

    def test_missing_file_raises_bootstrap_needed_no_post(self) -> None:
        mgr = self._manager(lambda r: _token_response())
        with self.assertRaises(SaxoBootstrapNeededError):
            mgr.get_access_token()
        self.assertEqual(self.posts, [])

    def test_reader_inside_access_margin_fails_loud_does_not_refresh(self) -> None:
        # A read-only consumer NEVER refreshes; a near-expiry access token is
        # the keep-alive's job. Reader raises so it re-reads / fails loud.
        rec = _record(self.clock, access_token_expires_at=self.clock.wall() + 100.0)
        self.store.write(rec)
        mgr = self._manager(lambda r: _token_response())
        with self.assertRaises(SaxoReauthRequiredError):
            mgr.get_access_token()
        self.assertEqual(self.posts, [], "reader must not POST")

    def test_reauth_required_flag_short_circuits_read(self) -> None:
        self.store.write(_record(self.clock, reauth_required=True, reauth_reason="server_rejected"))
        mgr = self._manager(lambda r: _token_response())
        with self.assertRaises(SaxoReauthRequiredError):
            mgr.get_access_token()


class TestRefreshHappyPath(_ManagerFixture):
    def test_refresh_inside_margin_rotates_once(self) -> None:
        rec = _record(self.clock, access_token_expires_at=self.clock.wall() + 100.0)
        self.store.write(rec)
        mgr = self._manager(lambda r: _token_response(access="AT-new", refresh="RT-new"))
        mgr.refresh()
        self.assertEqual(len(self.posts), 1)
        stored = self.store.read()
        assert stored is not None
        self.assertEqual(stored.access_token, "AT-new")
        self.assertEqual(stored.refresh_token, "RT-new")
        self.assertEqual(stored.previous_refresh_token, "RT-current")
        self.assertFalse(stored.reauth_required)
        self.assertEqual(stored.journal_state, "active")

    def test_refresh_when_fresh_is_noop(self) -> None:
        self.store.write(_record(self.clock))  # 1200s of access life
        mgr = self._manager(lambda r: _token_response())
        mgr.refresh()
        self.assertEqual(self.posts, [], "fresh token needs no refresh")

    def test_refresh_uses_live_expiry_fields(self) -> None:
        rec = _record(self.clock, access_token_expires_at=self.clock.wall() + 100.0)
        self.store.write(rec)
        now = self.clock.wall()
        mgr = self._manager(lambda r: _token_response(expires_in=900, refresh_expires_in=1800))
        mgr.refresh()
        stored = self.store.read()
        assert stored is not None
        self.assertAlmostEqual(stored.access_token_expires_at, now + 900, delta=2)
        self.assertAlmostEqual(stored.refresh_token_expires_at, now + 1800, delta=2)

    def test_missing_refresh_token_in_response_is_contract_error(self) -> None:
        rec = _record(self.clock, access_token_expires_at=self.clock.wall() + 100.0)
        self.store.write(rec)
        mgr = self._manager(lambda r: _token_response(refresh=None))
        with self.assertRaises(SaxoTokenContractError):
            mgr.refresh()
        # The invalidated old RT must NOT be silently kept as active.
        stored = self.store.read()
        assert stored is not None
        self.assertNotEqual(stored.journal_state, "active")


class TestSkewAndMonotonic(_ManagerFixture):
    def test_margin_constants_decompose_with_skew_budget(self) -> None:
        # Auditable constant — the assumption is pinned so a silent shrink
        # fails this test.
        self.assertEqual(MAX_TOLERATED_CLOCK_SKEW_S, 60)
        self.assertEqual(ACCESS_SAFETY_MARGIN_S, 300)
        self.assertEqual(REFRESH_SAFETY_MARGIN_S, 300)

    def test_clock_skew_90s_ahead_triggers_refresh_before_real_death(self) -> None:
        # VPS clock is +90s ahead of Saxo. Token minted expires_in=1200 against
        # Saxo time, so against VPS time it expires at wall+1200 but Saxo kills
        # it 90s earlier. At the 300s skew-aware margin the refresh fires with
        # room; a 120s margin would fire too late.
        wall_now = self.clock.wall()
        # Saxo death (in VPS wall terms) = wall_now + 1200 - 90.
        rec = _record(self.clock, access_token_expires_at=wall_now + 1200 - 90)
        self.store.write(rec)
        # Advance to 250s before the stored expiry (inside the 300s margin).
        self.clock.advance((1200 - 90) - 250)
        mgr = self._manager(lambda r: _token_response())
        self.assertTrue(mgr.needs_refresh(), "300s margin must absorb the 90s skew")

    def test_monotonic_catches_forward_pause_when_wall_steps_backward(self) -> None:
        # Cache a token, advance monotonic +1200 (token is dead in real time),
        # then step wall BACKWARD -600 (so the wall check still thinks it's
        # fresh). needs_refresh must trip on the monotonic deadline.
        self.store.write(_record(self.clock))
        mgr = self._manager(lambda r: _token_response())
        mgr.get_access_token()  # caches the monotonic deadline
        self.clock.step_mono(1200)
        self.clock.step_wall(-600)
        self.assertTrue(mgr.needs_refresh(), "monotonic deadline must trip even if wall says fresh")

    def test_min_rotation_guard_suppresses_backward_step_double_rotation(self) -> None:
        # Refresh once; step wall BACKWARD -90s so a naive wall check would
        # think the (just-rotated) token is near expiry again. The min-rotation
        # guard (rotated < 60s ago AND access still valid) must suppress a
        # second POST.
        rec = _record(self.clock, access_token_expires_at=self.clock.wall() + 100.0)
        self.store.write(rec)
        mgr = self._manager(lambda r: _token_response())
        mgr.refresh()
        self.assertEqual(len(self.posts), 1)
        self.clock.step_wall(-90)
        mgr.refresh()
        self.assertEqual(len(self.posts), 1, "backward NTP step must not double-rotate")


class TestDeadlineBoundedRetry(_ManagerFixture):
    def test_503_near_backstop_raises_transient_without_crossing_deadline(self) -> None:
        # refresh_token_expires_at = now+290 (inside the 300s backstop), 503 on
        # every call. The deadline-bounded loop must STOP before crossing
        # (expiry - 30) and raise SaxoTransientError, NOT mark reauth.
        now = self.clock.wall()
        rec = _record(
            self.clock,
            access_token_expires_at=now + 100.0,
            refresh_token_expires_at=now + 290.0,
        )
        self.store.write(rec)
        mgr = self._manager(lambda r: httpx.Response(503, text="upstream"))
        with self.assertRaises(SaxoTransientError):
            mgr.refresh()
        total_slept = sum(self.slept)
        self.assertLess(total_slept, 290 - 30, "no sleep may cross (expiry - 30)")
        stored = self.store.read()
        assert stored is not None
        self.assertFalse(
            stored.reauth_required, "transient exhaustion must NOT mark the chain dead"
        )


class TestClassificationAndReauth(_ManagerFixture):
    def test_invalid_grant_sets_reauth_required_server_rejected(self) -> None:
        rec = _record(self.clock, access_token_expires_at=self.clock.wall() + 100.0)
        self.store.write(rec)
        mgr = self._manager(lambda r: httpx.Response(400, json={"error": "invalid_grant"}))
        with self.assertRaises(SaxoReauthRequiredError):
            mgr.refresh()
        stored = self.store.read()
        assert stored is not None
        self.assertTrue(stored.reauth_required)
        self.assertEqual(stored.reauth_reason, "server_rejected")
        self.assertEqual(mgr.chain_state(), 1)

    def test_400_html_body_is_transient_not_reauth(self) -> None:
        rec = _record(self.clock, access_token_expires_at=self.clock.wall() + 100.0)
        self.store.write(rec)
        mgr = self._manager(lambda r: httpx.Response(400, text="<html>nope</html>"))
        with self.assertRaises(SaxoTransientError):
            mgr.refresh()
        stored = self.store.read()
        assert stored is not None
        self.assertFalse(stored.reauth_required)


class TestLocallyExpiredShortCircuit(_ManagerFixture):
    def test_expired_refresh_token_does_not_post(self) -> None:
        now = self.clock.wall()
        rec = _record(
            self.clock,
            access_token_expires_at=now - 10.0,
            refresh_token_expires_at=now - 60.0,
        )
        self.store.write(rec)
        mgr = self._manager(lambda r: _token_response())
        with self.assertRaises(SaxoReauthRequiredError):
            mgr.refresh()
        self.assertEqual(self.posts, [], "must NOT POST a wall-expired refresh token")
        stored = self.store.read()
        assert stored is not None
        self.assertTrue(stored.reauth_required)
        self.assertEqual(stored.reauth_reason, "expired_locally")


class TestEnvInterlock(_ManagerFixture):
    def test_record_env_mismatch_raises_no_network_no_write(self) -> None:
        # Record says environment=='live' but the manager is sim.
        rec = _record(self.clock, environment="live")
        # Write the live record into the sim store path directly (the file the
        # sim manager will read).
        self.store.write(rec)
        mgr = self._manager(lambda r: _token_response(), environment="sim")
        with self.assertRaises(SaxoEnvironmentMismatchError):
            mgr.get_access_token()
        self.assertEqual(self.posts, [])


class TestBootstrapStates(_ManagerFixture):
    def test_no_file_chain_state_is_bootstrap_needed(self) -> None:
        mgr = self._manager(lambda r: _token_response())
        self.assertEqual(mgr.chain_state(), 2)

    def test_restored_backup_with_past_expiry_raises_not_healthy(self) -> None:
        # reauth_required=false but a past refresh_token_expires_at -> stale,
        # treated as bootstrap/reauth, never POSTed.
        now = self.clock.wall()
        rec = _record(
            self.clock,
            reauth_required=False,
            access_token_expires_at=now - 100.0,
            refresh_token_expires_at=now - 50.0,
        )
        self.store.write(rec)
        mgr = self._manager(lambda r: _token_response())
        with self.assertRaises(SaxoReauthRequiredError):
            mgr.refresh()
        self.assertEqual(self.posts, [])


class TestJournalRecovery(_ManagerFixture):
    def test_crash_at_replace_leaves_journal_refreshing_with_old_rt(self) -> None:
        # Transport returns RT2 but os.replace raises (simulated crash). The
        # write-ahead journal must already hold journal_state=refreshing + RT1
        # intact so a restart can recover.
        from unittest import mock

        rec = _record(self.clock, access_token_expires_at=self.clock.wall() + 100.0)
        self.store.write(rec)
        mgr = self._manager(lambda r: _token_response(access="AT-2", refresh="RT-2"))

        original_replace = __import__("os").replace
        calls = {"n": 0}

        def flaky_replace(src, dst):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            # First replace = the journal lease write (allow). Second = the
            # final rotated-token commit (crash).
            if calls["n"] >= 2:
                raise OSError("simulated crash committing rotated token")
            return original_replace(src, dst)

        with mock.patch(
            "alphalens_pipeline.data.alt_data.saxo_token_store.os.replace", flaky_replace
        ):
            with self.assertRaises(OSError):
                mgr.refresh()

        stored = self.store.read()
        assert stored is not None
        self.assertEqual(stored.journal_state, "refreshing")
        self.assertEqual(stored.refresh_token, "RT-current", "old RT must remain on disk")

    def test_recovery_retries_journaled_rt_success(self) -> None:
        # On-disk journal=refreshing with RT1 -> a fresh manager retries the
        # journaled RT once; a 2xx recovers the chain.
        now = self.clock.wall()
        rec = _record(
            self.clock,
            journal_state="refreshing",
            journal_attempted_at=now,
            refresh_token="RT-journaled",
            access_token_expires_at=now + 100.0,
        )
        self.store.write(rec)
        mgr = self._manager(lambda r: _token_response(access="AT-rec", refresh="RT-rec"))
        mgr.recover()
        stored = self.store.read()
        assert stored is not None
        self.assertEqual(stored.journal_state, "active")
        self.assertEqual(stored.access_token, "AT-rec")
        self.assertEqual(len(self.posts), 1)

    def test_recovery_invalid_grant_marks_lost_rotation(self) -> None:
        now = self.clock.wall()
        rec = _record(
            self.clock,
            journal_state="refreshing",
            journal_attempted_at=now,
            refresh_token="RT-journaled",
            access_token_expires_at=now + 100.0,
        )
        self.store.write(rec)
        mgr = self._manager(lambda r: httpx.Response(400, json={"error": "invalid_grant"}))
        with self.assertRaises(SaxoReauthRequiredError):
            mgr.recover()
        stored = self.store.read()
        assert stored is not None
        self.assertTrue(stored.reauth_required)
        self.assertEqual(stored.reauth_reason, "lost_rotation")
        self.assertEqual(mgr.chain_state(), 1)


class TestChainStateGauge(_ManagerFixture):
    def test_corrupt_file_chain_state_is_3(self) -> None:
        self.store.token_path.write_text("{ broken ", encoding="utf-8")
        mgr = self._manager(lambda r: _token_response())
        self.assertEqual(mgr.chain_state(), 3)

    def test_healthy_file_chain_state_is_0(self) -> None:
        self.store.write(_record(self.clock))
        mgr = self._manager(lambda r: _token_response())
        self.assertEqual(mgr.chain_state(), 0)


if __name__ == "__main__":
    unittest.main()
