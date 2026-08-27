"""CLI tests for `alphalens broker price-reader` (#1172 PR-2).

The command is the composition root of the shared reader: it owns the ONE
elevated Saxo session (the stream) and the socket server in front of it. These
tests drive the wiring through injected fakes — the serving loop itself is
covered by tests/data/test_price_reader_server.py.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from typer.testing import CliRunner


class PriceReaderCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()

    def test_the_command_builds_the_reader_and_serves_until_stopped(self) -> None:
        from alphalens_cli.commands.broker import broker_app

        built: dict[str, object] = {}

        class _FakeStream:
            def __init__(self) -> None:
                self.stopped = False

            def stop(self) -> None:
                self.stopped = True

        class _FakeServer:
            def __init__(self, stream, socket_path, **kwargs) -> None:
                built["stream"] = stream
                built["socket_path"] = socket_path
                built["kwargs"] = kwargs
                self.served = False
                self.stopped = False

            def serve_forever(self) -> None:
                self.served = True

            def stop(self) -> None:
                self.stopped = True

        stream = _FakeStream()
        with (
            mock.patch(
                "alphalens_pipeline.data.alt_data.saxo_price_stream.get_shared_price_stream",
                return_value=stream,
            ) as get_stream,
            mock.patch(
                "alphalens_pipeline.data.alt_data.price_reader_server.PriceReaderServer",
                _FakeServer,
            ),
        ):
            result = self.runner.invoke(
                broker_app, ["price-reader", "--socket", "/tmp/alphalens-test/reader.sock"]
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(built["socket_path"], Path("/tmp/alphalens-test/reader.sock"))
        # The stream the reader owns must carry the READER job label, never a
        # per-env one: two emitters on one job erase each other's textfile.
        self.assertEqual(
            get_stream.call_args.kwargs["metrics_job"],
            "live-price-stream-reader",
        )
        # The elevated session is released on exit — a lingering subscription
        # would keep the venue session pinned after the unit stops.
        self.assertTrue(stream.stopped)

    def test_the_session_gate_predicate_is_wired_from_the_environment(self) -> None:
        """Outside the trading window the reader must be able to hold no
        WebSocket, exactly like the in-process daemon does."""
        from alphalens_cli.commands.broker import broker_app

        with (
            mock.patch(
                "alphalens_pipeline.data.alt_data.saxo_price_stream.get_shared_price_stream"
            ) as get_stream,
            mock.patch(
                "alphalens_pipeline.brokers.automanager.control_loop."
                "_stream_session_window_if_enabled",
                return_value="PREDICATE",
            ),
            mock.patch("alphalens_pipeline.data.alt_data.price_reader_server.PriceReaderServer"),
        ):
            result = self.runner.invoke(
                broker_app, ["price-reader", "--socket", "/tmp/alphalens-test/reader.sock"]
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(get_stream.call_args.kwargs["session_window"], "PREDICATE")

    def test_the_default_socket_path_comes_from_the_shared_resolver(self) -> None:
        from alphalens_cli.commands.broker import broker_app

        with (
            mock.patch(
                "alphalens_pipeline.data.alt_data.saxo_price_stream.get_shared_price_stream"
            ),
            mock.patch(
                "alphalens_pipeline.data.alt_data.price_reader_server.PriceReaderServer"
            ) as server_cls,
            mock.patch(
                "alphalens_pipeline.data.alt_data.price_reader_server.default_socket_path",
                return_value=Path("/tmp/alphalens-test/default.sock"),
            ),
        ):
            result = self.runner.invoke(broker_app, ["price-reader"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(server_cls.call_args.args[1], Path("/tmp/alphalens-test/default.sock"))


if __name__ == "__main__":
    unittest.main()
