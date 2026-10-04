"""The quote source this daemon reads prices from, held for the process.

Finding 7 of the 2026-10-02 architecture audit: a test-only reset hook sat in
``control_loop.py``, the file that places real orders. The hook itself is a
repo pattern, not a mistake — ten production modules pair a lazy singleton with
a ``_reset_*_for_tests``, three of them on the broker path, and the canonical
HTTP-client doctrine in CLAUDE.md is built on it. So the fix is not to remove
the hook but to move the singleton it resets off the tick file and into the
module that owns the concern, which is also the direction of the partition.

Nothing about construction changed. The source is still built on first use, not
at wiring time, because the reader keys each client's touch-latch accumulator by
CONNECTION: a per-tick client would reconnect every ~45 s and always drain an
empty window.

``control_loop`` reads both the function and the singleton through this module
(``quote_source._quote_source()``, ``quote_source._REMOTE_QUOTE_SOURCE``) and
never by a ``from`` import, which would freeze its own binding and defeat both
the reset hook and the gauge read.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from alphalens_pipeline.brokers.automanager import state_paths
from alphalens_pipeline.brokers.automanager.stream_handles import (
    _stream_session_window_if_enabled,
)

logger = logging.getLogger(__name__)


# The cross-process price reader's socket (#1172). When set, this daemon reads
# the ONE elevated Saxo session that reader process holds instead of opening its
# own stream — Saxo grants a single elevated session per LIVE login, so two
# in-process streams demote BOTH daemons to 15-minute quotes, silently. Unset
# keeps today's in-process behaviour, which is also the rollback lever: remove
# the drop-in, restart, done.
_PRICE_READER_SOCKET_ENV = "ALPHALENS_SAXO_PRICE_READER_SOCKET"

# The remote source is held for the process, not per tick: the reader keys each
# client's touch-latch accumulator by CONNECTION, so a per-tick client would
# reconnect every ~45s and always drain an empty window.
_REMOTE_QUOTE_SOURCE: Any | None = None


def _reset_remote_quote_source_for_tests() -> None:
    """Drop the cached remote source. Test-only seam, mirroring
    ``polygon_client._reset_default_client_for_tests``."""
    import contextlib

    global _REMOTE_QUOTE_SOURCE  # noqa: PLW0603 — lazy singleton is the documented pattern
    if _REMOTE_QUOTE_SOURCE is not None:
        with contextlib.suppress(Exception):
            _REMOTE_QUOTE_SOURCE.close()
    _REMOTE_QUOTE_SOURCE = None


def _quote_source() -> Any:
    """The quote source this tick reads from: the shared reader when a socket
    is configured, otherwise this process's own stream.

    Both satisfy ``QuoteSource``, so everything downstream — subscription
    scoping, the price-feed adapter, the touch-latch drain — is identical
    either way."""
    global _REMOTE_QUOTE_SOURCE  # noqa: PLW0603 — lazy singleton is the documented pattern
    socket_path = os.environ.get(_PRICE_READER_SOCKET_ENV)
    if socket_path:
        from alphalens_pipeline.data.alt_data.price_reader_client import RemoteQuoteSource

        if _REMOTE_QUOTE_SOURCE is None:
            _REMOTE_QUOTE_SOURCE = RemoteQuoteSource(socket_path)
            logger.info("live prices: reading the shared price reader at %s", socket_path)
        return _REMOTE_QUOTE_SOURCE

    from alphalens_pipeline.data.alt_data.saxo_price_stream import get_shared_price_stream

    # ADR 0016 D5: the stream's gauges must carry a per-instance job label so a
    # future LIVE daemon's price stream never shares a Prometheus job (and thus
    # textfile) with the SIM instance's. Like metrics_job, session_window only
    # takes effect on the FIRST call that actually constructs the singleton —
    # see get_shared_price_stream.
    return get_shared_price_stream(
        metrics_job=state_paths.price_stream_metrics_job(),
        session_window=_stream_session_window_if_enabled(),
    )
