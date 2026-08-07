"""Saxo streaming WIRE-PROTOCOL decoder — the pure binary-envelope parser.

Relocated out of ``brokers/saxo/streaming.py`` (2026-08-07): this code decodes
Saxo's binary streaming envelope only. It knows nothing about orders,
positions, or the broker adapter — the same infrastructure category as
``saxo_exchanges.py``'s MIC -> Saxo exchange map, which lives here for the
identical reason. Its previous home under ``brokers/`` was a historical
accident from when the SIM order daemon's streaming reader was the only
consumer of a Saxo stream; the LIVE price stream (``saxo_price_stream.py``)
needs the SAME decoder and must live under ``data/alt_data/`` (never
``brokers/`` — the ADR 0014 SIM-only structural rail fails red on any LIVE URL
string inside the ``brokers`` package), so the decoder moved to the shared
infrastructure side of the DAG instead of being duplicated or exempted.

``brokers/saxo/streaming.py`` re-imports :class:`StreamMessage`,
:func:`parse_stream_frames`, and :class:`SaxoStreamProtocolError` from here
unchanged (same names, same call sites, same raised-exception identity) so
existing callers and tests keep working through the same import paths.

Confirmed on real SIM frames (design memo
``docs/research/saxo_streaming_design_2026_07_24.md``): ``[0:8]`` msgId
u64-LE, ``[8:10]`` reserved, ``[10]`` refId size, refId ASCII, one
payload-format byte (0=JSON asserted, 1=protobuf rejected — the readers never
opt in), ``[next 4]`` payload size u32-LE, then payload; multiple messages may
be packed in one WS frame, so the parser loops. Also confirmed unchanged
against real LIVE frames in the 2026-08-07 probe.
"""

from __future__ import annotations

from dataclasses import dataclass

# Envelope layout constants (bytes).
_MSG_ID_LEN = 8
_RESERVED_LEN = 2
_REF_SIZE_LEN = 1
_FORMAT_LEN = 1
_PAYLOAD_SIZE_LEN = 4
_FORMAT_JSON = 0
_FORMAT_PROTOBUF = 1
# Bytes consumed before the refId (msgId + reserved + refId-size byte).
_PREFIX_LEN = _MSG_ID_LEN + _RESERVED_LEN + _REF_SIZE_LEN


class SaxoStreamProtocolError(Exception):
    """The binary frame did not match the confirmed envelope (truncated buffer,
    or the protobuf format byte the readers never opt into).

    A plain ``Exception`` subclass, not part of the ``brokers/saxo`` error
    taxonomy (``SaxoError`` and friends in ``brokers/saxo/errors.py``) — this
    module must not import ``brokers`` (enforced by
    ``test_module_dependencies.py``'s "data must not import brokers" rule),
    and nothing in the codebase catches this specifically as a ``SaxoError``
    or ``SaxoStreamError`` (verified: those catches only wrap REST calls in
    ``broker.py``, never a parse error). ``brokers/saxo/streaming.py`` keeps
    re-exporting this exact class under the same name, so
    ``except SaxoStreamProtocolError`` at every existing call site keeps
    working unchanged.
    """


@dataclass(frozen=True)
class StreamMessage:
    """One decoded Saxo streaming message. ``payload`` stays raw bytes —
    routing keys off ``reference_id`` only."""

    message_id: int
    reference_id: str
    payload: bytes


def parse_stream_frames(buf: bytes) -> list[StreamMessage]:
    """Decode every message packed into one WS frame (PURE — no I/O).

    Raises :class:`SaxoStreamProtocolError` on a truncated buffer (never routes a
    half-decoded frame) or on the protobuf format byte (only JSON is ever opted
    into). An empty buffer yields an empty list.
    """
    messages: list[StreamMessage] = []
    offset = 0
    total = len(buf)
    while offset < total:
        # Need msgId(8) + reserved(2) + refId-size(1) to even read the refId len.
        if offset + _PREFIX_LEN > total:
            raise SaxoStreamProtocolError(
                f"truncated frame header at offset {offset} (have {total - offset} bytes, "
                f"need >= {_PREFIX_LEN})"
            )
        message_id = int.from_bytes(buf[offset : offset + _MSG_ID_LEN], "little")
        ref_size = buf[offset + _MSG_ID_LEN + _RESERVED_LEN]
        ref_start = offset + _PREFIX_LEN
        ref_end = ref_start + ref_size
        # refId + format(1) + payload-size(4) must all be present.
        header_end = ref_end + _FORMAT_LEN + _PAYLOAD_SIZE_LEN
        if header_end > total:
            raise SaxoStreamProtocolError(
                f"truncated frame at offset {offset}: refId/format/size run past the buffer"
            )
        reference_id = buf[ref_start:ref_end].decode("ascii")
        fmt = buf[ref_end]
        if fmt == _FORMAT_PROTOBUF:
            raise SaxoStreamProtocolError(
                f"protobuf payload for refId {reference_id!r} — never opted in; JSON only"
            )
        if fmt != _FORMAT_JSON:
            raise SaxoStreamProtocolError(
                f"unknown payload-format byte {fmt} for refId {reference_id!r}"
            )
        size_start = ref_end + _FORMAT_LEN
        payload_size = int.from_bytes(buf[size_start : size_start + _PAYLOAD_SIZE_LEN], "little")
        payload_start = size_start + _PAYLOAD_SIZE_LEN
        payload_end = payload_start + payload_size
        if payload_end > total:
            raise SaxoStreamProtocolError(
                f"truncated payload for refId {reference_id!r}: declared {payload_size} bytes, "
                f"only {total - payload_start} available"
            )
        messages.append(StreamMessage(message_id, reference_id, buf[payload_start:payload_end]))
        offset = payload_end
    return messages


__all__ = [
    "SaxoStreamProtocolError",
    "StreamMessage",
    "parse_stream_frames",
]
