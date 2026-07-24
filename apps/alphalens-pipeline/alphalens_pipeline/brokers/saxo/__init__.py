"""Saxo Bank OpenAPI adapter (SIM-only) for the broker-agnostic layer.

No ``__status__`` here — the parent ``brokers`` package carries it. Layering
inside this subpackage is strictly one-way::

    broker.py -> client.py -> tokens.py -> errors.py

``client.py`` is THE canonical Saxo HTTP surface (one-client-per-vendor
doctrine; enforced by ``tests/test_no_raw_saxo_http.py``); ``broker.py``
adapts it to the broker-agnostic ``contract.Broker`` Protocol and is the only
module the registry touches.

``streaming.py`` is the dark, SIM-only WebSocket reader (design memo
``saxo_streaming_design_2026_07_24.md``): a pure latency win over the REST poll
backstop, wired in only behind ``ALPHALENS_BROKER_STREAMING_ENABLED``.
"""

from alphalens_pipeline.brokers.saxo.streaming import (
    SIM_STREAMING_BASE_URL,
    SaxoStreamingClient,
    StreamMessage,
    parse_stream_frames,
)

__all__ = [
    "SIM_STREAMING_BASE_URL",
    "SaxoStreamingClient",
    "StreamMessage",
    "parse_stream_frames",
]
