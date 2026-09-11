"""``broker_contract`` must not define the CLI's failure codes (#1389).

The same decision as #1122, applied to a different kind of leak. There, the Saxo
LIVE fee card had been moved into ``broker_contract.costs``, where a second
adapter would have inherited it silently — not with an error, but with
confidently wrong thresholds. The rule the package states about itself is in
``fx.py``: *"the ADAPTER reports, never the contract decides."*

A failure-code registry is exactly the shape that invites the same mistake. The
broker taxonomy's codes belong to the contract — every adapter raises those
classes. ``usage``, ``env_ambiguous``, ``live_refused``, ``state_layout``,
``policy_refused``, ``pick_already_armed`` and ``unclassified`` do not: they are
decisions a command-line program makes, and a second consumer of the published
package must not find them there and assume they are part of the contract.

DELIBERATELY NARROW, like its #1122 sibling: it pins these code names, not a
general "no CLI concepts" lint. The package legitimately mentions a CLI in a
``suggestions`` argv and in prose, and a test that tried to police that would be
either useless or unbearable.
"""

from __future__ import annotations

import unittest

from alphalens_cli.commands.broker import _CLI_FAILURE_CODES, _FAILURE_CODES
from broker_contract.failure import CONTRACT_FAILURE_CODES

# Named individually rather than derived from `_CLI_FAILURE_CODES`: deriving it
# would make the test agree with whatever the CLI happens to declare, so a code
# moved INTO the contract package would move out of the test's reach at the same
# moment it needed checking.
CLI_OWNED_CODES = frozenset(
    {
        "usage",
        "env_ambiguous",
        "live_refused",
        "state_layout",
        "pick_already_armed",
        "pick_not_writable",
        "intent_malformed",
        "venue_unsupported",
        "policy_refused",
        "stream_metrics_missing",
        "unclassified",
    }
)

# The other side of the same boundary: these are raised by adapter code and by
# the intent validator, so a published consumer needs them.
CONTRACT_OWNED_CODES = frozenset(
    {
        "broker_transient",
        "broker_rate_limited",
        "write_outcome_unknown",
        "broker_auth",
        "instrument_not_found",
        "order_rejected",
        "broker_unsupported",
        "broker_failed",
        "intent_invalid",
    }
)


class TestFailureCodeOwnership(unittest.TestCase):
    def test_the_contract_defines_no_cli_owned_code(self) -> None:
        leaked = CLI_OWNED_CODES & set(CONTRACT_FAILURE_CODES)

        self.assertEqual(leaked, set(), f"CLI codes defined in broker_contract: {sorted(leaked)}")

    def test_the_contract_defines_exactly_its_own_codes(self) -> None:
        """The complement, so the test also fails when a contract code silently
        disappears — a published code vanishing is as breaking as a new one
        appearing in the wrong place."""
        self.assertEqual(set(CONTRACT_FAILURE_CODES), CONTRACT_OWNED_CODES)

    def test_the_cli_defines_exactly_its_own_codes(self) -> None:
        self.assertEqual(set(_CLI_FAILURE_CODES), CLI_OWNED_CODES)

    def test_the_two_halves_do_not_overlap(self) -> None:
        """An overlap would make the published table ambiguous about which
        definition of a code a client is reading."""
        self.assertEqual(set(_CLI_FAILURE_CODES) & set(CONTRACT_FAILURE_CODES), set())

    def test_the_emitter_sees_the_union_of_both(self) -> None:
        self.assertEqual(set(_FAILURE_CODES), CLI_OWNED_CODES | CONTRACT_OWNED_CODES)


if __name__ == "__main__":
    unittest.main()
