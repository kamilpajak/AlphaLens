"""The ``QuoteSource`` structural contract (#1172).

The feed factory and ``SaxoLivePriceFeed`` reach for exactly six methods on
the quote source. Naming that set as a Protocol is what lets the cross-process
reader client stand in for the in-process ``SaxoPriceStream`` without either
side importing the other.

``is_receiving`` is a REQUIRED member rather than an optional capability
(the ``SupportsSessionLow`` shape) on purpose: a source that cannot answer it
would have to be treated as live, and failing OPEN on a stream-health question
that gates real-money orders is the one direction this must never take.
"""

from __future__ import annotations

import unittest

from alphalens_pipeline.data.alt_data.quote_source import QuoteSource
from alphalens_pipeline.data.alt_data.saxo_price_stream import SaxoPriceStream


class _FakeClient:
    pass


class _FakeTokenProvider:
    def access_token(self) -> str:
        return "tok"


class _FullQuoteSource:
    """A structurally complete stand-in — what the remote client will be."""

    def get(self, uic):  # pragma: no cover - shape only
        return None

    def drain_running_low(self, uic):  # pragma: no cover - shape only
        return None

    def reseed_running_low(self, uic, low):  # pragma: no cover - shape only
        return None

    def live_uic_for(self, ticker, *, exchange_mic):  # pragma: no cover - shape only
        return None

    def ensure_subscribed(self, uics, *, scope="default"):  # pragma: no cover - shape only
        return None

    def is_receiving(self):  # pragma: no cover - shape only
        return True


class _MissingIsReceiving:
    """Positive control for the member added in #1397 — without it the check
    could pass on a source that cannot answer the stream-health question."""

    def get(self, uic):  # pragma: no cover - shape only
        return None

    def drain_running_low(self, uic):  # pragma: no cover - shape only
        return None

    def reseed_running_low(self, uic, low):  # pragma: no cover - shape only
        return None

    def live_uic_for(self, ticker, *, exchange_mic):  # pragma: no cover - shape only
        return None

    def ensure_subscribed(self, uics, *, scope="default"):  # pragma: no cover - shape only
        return None


class _MissingEnsureSubscribed:
    """Positive control: drop ONE method and the check must fail.

    Without this the runtime check could rot into a tautology (a Protocol whose
    methods nothing actually names would accept everything).
    """

    def get(self, uic):  # pragma: no cover - shape only
        return None

    def drain_running_low(self, uic):  # pragma: no cover - shape only
        return None

    def reseed_running_low(self, uic, low):  # pragma: no cover - shape only
        return None

    def live_uic_for(self, ticker, *, exchange_mic):  # pragma: no cover - shape only
        return None


class TestQuoteSourceProtocol(unittest.TestCase):
    def test_the_in_process_stream_satisfies_the_protocol(self):
        stream = SaxoPriceStream(_FakeClient(), _FakeTokenProvider())
        self.assertIsInstance(stream, QuoteSource)

    def test_a_structurally_complete_stand_in_satisfies_the_protocol(self):
        self.assertIsInstance(_FullQuoteSource(), QuoteSource)

    def test_a_partial_implementation_does_not_satisfy_the_protocol(self):
        self.assertNotIsInstance(_MissingEnsureSubscribed(), QuoteSource)

    def test_a_source_that_cannot_answer_stream_health_does_not_satisfy_it(self):
        self.assertNotIsInstance(_MissingIsReceiving(), QuoteSource)


if __name__ == "__main__":
    unittest.main()
