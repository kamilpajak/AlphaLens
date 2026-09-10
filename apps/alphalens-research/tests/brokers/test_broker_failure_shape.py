"""The one machine-readable failure shape (``broker_contract/failure.py``, #1389).

The shape is not new. ``_emit_stream_status_missing`` in the broker CLI has been
writing ``{code, message, retryable, details, suggestions:[{argv, why}]}`` to
stderr since #1173. This module generalises THAT object so #1404's
``validate_intent`` raises the same thing instead of inventing a second error
shape inside the contract package.

What is pinned here:

1. **The five doctrine keys, and only those.** A consumer reads the object, not
   our prose, so an extra or missing key is a contract change.
2. **The registry carries only codes the CONTRACT owns.** CLI-side concepts
   (``usage``, ``env_ambiguous``, ...) live with the CLI — see
   ``test_broker_contract_has_no_cli_codes.py``, which mirrors the #1122
   decision that moved the Saxo fee card out of this package.
3. **``retryable`` means "safe to re-run WITHOUT reconciling first"**, not "the
   cause was transient". ``write_outcome_unknown`` is the case that separates
   the two: a 5xx after a POST is transient in cause and forbidden to retry.
4. **A code whose recovery is mechanical must be able to say so** — a machine
   client reads ``retryable: false`` as "drop it", so any such code declares
   ``needs_suggestion`` and its emitters must attach a runnable ``argv``.
"""

from __future__ import annotations

import dataclasses
import json
import unittest

from broker_contract.failure import (
    CONTRACT_FAILURE_CODES,
    ContractError,
    Failure,
    FailureCode,
    Suggestion,
)

# The doctrine keys, in the order the existing stream-status object writes them.
_DOCTRINE_KEYS = ("code", "message", "retryable", "details", "suggestions")

# Codes that belong to the CLI, not to the contract. Named here as a NEGATIVE
# control for the registry: if one of them ever appears in CONTRACT_FAILURE_CODES
# the package has started deciding things the adapter/CLI decides.
_CLI_ONLY_CODES = frozenset(
    {
        "usage",
        "env_ambiguous",
        "live_refused",
        "state_layout",
        "policy_refused",
        "pick_already_armed",
        "not_found",
        "unclassified",
    }
)


class TestFailureShape(unittest.TestCase):
    def test_the_object_carries_exactly_the_five_doctrine_keys(self) -> None:
        failure = Failure(code="broker_failed", message="boom", retryable=False)

        self.assertEqual(tuple(failure.to_jsonable()), _DOCTRINE_KEYS)

    def test_it_renders_as_strict_json(self) -> None:
        failure = Failure(
            code="write_outcome_unknown",
            message='saxo 500 on POST /orders (x-request-id="abc")',
            retryable=False,
            details={"request_id": "abc", "attempts": 4},
            suggestions=(
                Suggestion(argv=("alphalens", "broker", "orders"), why="reconcile first"),
            ),
        )

        body = json.loads(json.dumps(failure.to_jsonable(), allow_nan=False))

        self.assertEqual(body["details"], {"request_id": "abc", "attempts": 4})
        self.assertEqual(body["suggestions"][0]["argv"], ["alphalens", "broker", "orders"])
        self.assertEqual(body["suggestions"][0]["why"], "reconcile first")

    def test_a_failure_with_no_details_still_carries_the_keys(self) -> None:
        """An empty ``details`` / ``suggestions`` is an empty container, never a
        missing key — a consumer must not have to branch on absence."""
        body = Failure(code="broker_failed", message="boom", retryable=False).to_jsonable()

        self.assertEqual(body["details"], {})
        self.assertEqual(body["suggestions"], [])

    def test_a_failure_is_frozen(self) -> None:
        failure = Failure(code="broker_failed", message="boom", retryable=False)

        with self.assertRaises(dataclasses.FrozenInstanceError):
            failure.code = "usage"  # type: ignore[misc]

    def test_suggestion_argv_is_a_tuple_so_it_cannot_be_edited_in_place(self) -> None:
        """``argv`` is shared with whoever renders it; a list would let a caller
        mutate the runnable command another consumer already read."""
        suggestion = Suggestion(argv=("alphalens", "broker", "auth"), why="re-login")

        self.assertIsInstance(suggestion.argv, tuple)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            suggestion.why = "other"  # type: ignore[misc]


class TestContractError(unittest.TestCase):
    def test_it_carries_the_failure_and_reads_as_its_message(self) -> None:
        failure = Failure(code="intent_invalid", message="alloc must sum to 100", retryable=False)

        exc = ContractError(failure)

        self.assertIs(exc.failure, failure)
        self.assertEqual(str(exc), "alloc must sum to 100")

    def test_it_is_an_exception_a_consumer_can_catch_without_our_modules(self) -> None:
        self.assertTrue(issubclass(ContractError, Exception))


class TestContractCodeRegistry(unittest.TestCase):
    def test_every_entry_names_itself(self) -> None:
        """The mapping key and the entry's ``name`` cannot drift apart — a
        renamed key with a stale name would publish two spellings of one code."""
        for key, entry in CONTRACT_FAILURE_CODES.items():
            self.assertEqual(key, entry.name)

    def test_every_entry_states_what_it_means(self) -> None:
        """The README table is generated from this registry, so an entry with no
        meaning publishes a code a client cannot act on."""
        for key, entry in CONTRACT_FAILURE_CODES.items():
            self.assertTrue(entry.meaning.strip(), f"{key} carries no meaning")

    def test_the_registry_holds_no_cli_only_code(self) -> None:
        """Negative control mirroring #1122: the contract does not learn the
        CLI's vocabulary just because both render the same object."""
        leaked = _CLI_ONLY_CODES & set(CONTRACT_FAILURE_CODES)

        self.assertEqual(leaked, set(), f"CLI-only codes leaked into the contract: {leaked}")

    def test_the_two_retryable_codes_are_the_ones_that_are_safe_to_re_run(self) -> None:
        """Pinned by name, not counted: the whole point of #1389 is that a
        client can re-run on these two and must not on any other."""
        retryable = {name for name, entry in CONTRACT_FAILURE_CODES.items() if entry.retryable}

        self.assertEqual(retryable, {"broker_transient", "broker_rate_limited"})

    def test_write_outcome_unknown_is_not_retryable_despite_a_transient_cause(self) -> None:
        """The case that defines the field. A 5xx after a POST may have landed;
        the adapter refuses to blind-retry it, so the contract must not invite a
        client to do what the adapter would not."""
        entry = CONTRACT_FAILURE_CODES["write_outcome_unknown"]

        self.assertFalse(entry.retryable)
        self.assertTrue(entry.needs_suggestion)

    def test_a_non_retryable_code_with_a_mechanical_recovery_demands_a_suggestion(self) -> None:
        """``retryable: false`` reads as "drop it" to a machine. Where a runnable
        next command exists, the code says so and emitters must attach one."""
        demanding = {
            name for name, entry in CONTRACT_FAILURE_CODES.items() if entry.needs_suggestion
        }

        self.assertEqual(demanding, {"write_outcome_unknown", "broker_auth"})

    def test_a_retryable_code_never_demands_a_suggestion(self) -> None:
        """Positive control on the rule above: "just run it again" needs no argv,
        so a retryable code demanding one would mean the flag lost its meaning."""
        for name, entry in CONTRACT_FAILURE_CODES.items():
            if entry.retryable:
                self.assertFalse(entry.needs_suggestion, f"{name} both retryable and demanding")

    def test_the_registry_is_not_writable(self) -> None:
        """Published tables are read; a caller that could append a code would
        make the CI parity gate meaningless for the rest of that process."""
        with self.assertRaises(TypeError):
            CONTRACT_FAILURE_CODES["invented"] = FailureCode(  # type: ignore[index]
                name="invented", retryable=False, meaning="nope"
            )


if __name__ == "__main__":
    unittest.main()
