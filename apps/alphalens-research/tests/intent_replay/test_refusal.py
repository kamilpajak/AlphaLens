"""Unit tests for ``intent_replay/refusal.py`` — the one place an engine
refusal is built, so the closed-reason rule of ``bars.py`` is written once.
"""

from __future__ import annotations

import unittest

from broker_contract.failure import ContractError
from intent_replay.refusal import refuse


class _RefusedError(ContractError):
    pass


REASONS = {"a_reason": "What it means."}


class RefusalBuilderTest(unittest.TestCase):
    def test_a_registered_reason_builds_the_requested_error(self) -> None:
        error = refuse(
            _RefusedError, "some_code", "the message", reasons=REASONS, reason="a_reason", n=1
        )
        self.assertIsInstance(error, _RefusedError)
        self.assertEqual(error.failure.code, "some_code")
        self.assertEqual(error.failure.message, "the message")
        self.assertEqual(dict(error.failure.details), {"reason": "a_reason", "n": 1})

    def test_a_refusal_is_never_retryable(self) -> None:
        self.assertFalse(refuse(_RefusedError, "c", "m").failure.retryable)

    def test_an_absent_reason_is_allowed(self) -> None:
        # ``bars_empty`` ships without a reason; a vocabulary is a constraint on
        # a reason that IS present, not a demand that one be.
        self.assertIsInstance(
            refuse(_RefusedError, "c", "m", reasons=REASONS, index=3), _RefusedError
        )

    def test_an_unregistered_reason_cannot_ship(self) -> None:
        with self.assertRaises(ValueError):
            refuse(_RefusedError, "c", "m", reasons=REASONS, reason="made_up")

    def test_a_reason_without_a_vocabulary_cannot_ship(self) -> None:
        # ``path_unclassified`` carries no reason vocabulary; a reason on it is a
        # programming error, not a published field.
        with self.assertRaises(ValueError):
            refuse(_RefusedError, "c", "m", reason="a_reason")


if __name__ == "__main__":
    unittest.main()
