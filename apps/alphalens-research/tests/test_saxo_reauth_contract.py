"""Contract test for the auth-death -> unmanaged-positions handling.

The order/exit layer is OUT of scope for this PR, but the contract it must
honor ships NOW so the future order PR cannot silently ``_safe_call``-swallow
an auth death (hard-cap Finding 3 / CRITICAL). The renewal subsystem provides:

* :class:`SaxoReauthRequiredError` — a distinct, non-swallowed exception the
  exit manager is contractually required to treat as 'positions now
  unmanaged'.
* The gauge NAME ``alphalens_saxo_positions_unmanaged`` (set by the future
  exit manager on auth death) — pinned in the metrics allow-list now.
* The behavioural contract: on ``invalid_grant`` while the access token still
  has life AND an open position exists, the position-management path must
  permit EXACTLY ONE best-effort protective-exit attempt with the still-valid
  token BEFORE going dark — it must NOT blanket-raise immediately.

This test encodes that contract via a reference handler
(:func:`handle_reauth_required_for_positions`) that the order PR will wire to
the real exit manager. The handler ships here as the executable contract; the
real flatten lands with the order layer.
"""

from __future__ import annotations

import unittest

from alphalens_pipeline.data.alt_data.saxo_client import SaxoReauthRequiredError
from alphalens_pipeline.data.alt_data.saxo_reauth_contract import (
    POSITIONS_UNMANAGED_GAUGE,
    handle_reauth_required_for_positions,
)


class _FakeAccessToken:
    def __init__(self, *, valid: bool) -> None:
        self.valid = valid


class TestReauthContract(unittest.TestCase):
    def test_gauge_name_is_the_contracted_constant(self) -> None:
        self.assertEqual(POSITIONS_UNMANAGED_GAUGE, "alphalens_saxo_positions_unmanaged")

    def test_one_protective_exit_attempt_then_raises(self) -> None:
        attempts: list[str] = []

        def protective_exit() -> None:
            attempts.append("flatten")

        with self.assertRaises(SaxoReauthRequiredError):
            handle_reauth_required_for_positions(
                error=SaxoReauthRequiredError("dead", reason="server_rejected"),
                access_token_still_valid=True,
                has_open_position=True,
                protective_exit=protective_exit,
            )
        self.assertEqual(
            attempts,
            ["flatten"],
            "must permit EXACTLY ONE protective-exit attempt with the still-valid token",
        )

    def test_no_open_position_raises_without_exit_attempt(self) -> None:
        attempts: list[str] = []

        with self.assertRaises(SaxoReauthRequiredError):
            handle_reauth_required_for_positions(
                error=SaxoReauthRequiredError("dead", reason="server_rejected"),
                access_token_still_valid=True,
                has_open_position=False,
                protective_exit=lambda: attempts.append("flatten"),
            )
        self.assertEqual(attempts, [], "no open position -> no protective exit attempt")

    def test_expired_access_token_cannot_flatten_still_raises(self) -> None:
        attempts: list[str] = []

        with self.assertRaises(SaxoReauthRequiredError):
            handle_reauth_required_for_positions(
                error=SaxoReauthRequiredError("dead", reason="expired_locally"),
                access_token_still_valid=False,
                has_open_position=True,
                protective_exit=lambda: attempts.append("flatten"),
            )
        self.assertEqual(
            attempts, [], "no still-valid token -> cannot flatten, but must still raise"
        )

    def test_protective_exit_failure_is_swallowed_then_raises(self) -> None:
        # The flatten is best-effort: if it itself fails, the reauth error must
        # still propagate (the position is unmanaged regardless).
        def failing_exit() -> None:
            raise RuntimeError("broker rejected the flatten")

        with self.assertRaises(SaxoReauthRequiredError):
            handle_reauth_required_for_positions(
                error=SaxoReauthRequiredError("dead", reason="server_rejected"),
                access_token_still_valid=True,
                has_open_position=True,
                protective_exit=failing_exit,
            )


if __name__ == "__main__":
    unittest.main()
