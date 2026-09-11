"""One append for the four broker journals (#1421).

The journals are append-only JSONL and are the source of truth between the CLI
that arms picks and the daemon that places real orders. A partial write — ENOSPC
is the realistic cause — leaves a line with no trailing newline, and the NEXT
append concatenates onto it: both records merge into one unparseable line and
BOTH are lost. Measured before this helper existed: arm one pick, tear the line,
arm a second; the CLI reported success for the second and the fold never saw it.

Two properties, and they are different things:

* **integrity** — a torn line must be confined to itself, so the next record
  survives. That is the separator guard, and it is what the bug was about.
* **durability** — a buffered write lost to a crash silently drops a record.
  `entry_trails` and the standalone-stop journal already fsync for exactly this,
  each with a docstring saying so; `picks` and `submissions` did not — the two
  that decide whether a pick is placed and whether it is re-placed.

The guard reads the last BYTE, never a character: a UTF-8 continuation byte is
always >= 0x80, so 0x0A cannot occur inside a multi-byte character and a write
torn in the middle of one cannot masquerade as a finished line.
"""

from __future__ import annotations

import json
import logging
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from alphalens_pipeline.brokers.journal import JournalWriteError, append_json_line


class _JournalCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "sub" / "journal.jsonl"

    def seed(self, raw: bytes) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(raw)

    def lines(self) -> list[str]:
        # errors="replace": a torn line can hold bytes that are not valid
        # UTF-8, and the harness must still be able to look at the file.
        return self.path.read_text(encoding="utf-8", errors="replace").splitlines()

    def records(self) -> list[dict]:
        out = []
        for line in self.lines():
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out


class ATornLineNeverEatsTheNextRecord(_JournalCase):
    """The regression this helper exists for."""

    def test_the_record_after_a_torn_write_survives(self) -> None:
        self.seed(b'{"k": "OLD", "trunc')

        append_json_line(self.path, {"k": "NEW"})

        self.assertIn({"k": "NEW"}, self.records())

    def test_the_torn_bytes_stay_confined_to_their_own_line(self) -> None:
        """One damaged record, not two — the counter must be able to say so."""
        self.seed(b'{"k": "OLD", "trunc')

        append_json_line(self.path, {"k": "NEW"})

        damaged = [line for line in self.lines() if line.strip() and not _parses(line)]
        self.assertEqual(len(damaged), 1)

    def test_a_write_torn_inside_a_multibyte_character_is_repaired(self) -> None:
        """Pins a property of the GUARD, not a reachable production shape.

        `json.dumps` defaults to `ensure_ascii=True` and no journal writer
        overrides it, so every line we emit is pure ASCII and our own torn write
        cannot split a character. The guard is byte-safe anyway, and stating why
        keeps the next reader from "simplifying" it into a character read: a
        UTF-8 continuation byte is always >= 0x80, so 0x0A never occurs inside a
        multi-byte character.
        """
        # Escapes, not literals: this repo's sources stay ASCII, and the point
        # is the BYTES anyway — the last one here is half of a 2-byte character.
        torn = ('{"k": "' + "\u00e9\u00e8").encode()[:-1]
        self.seed(torn)

        append_json_line(self.path, {"k": "NEW"})

        self.assertIn({"k": "NEW"}, self.records())


class TheSeparatorIsAddedOnlyWhenItIsMissing(_JournalCase):
    def test_a_healthy_journal_gains_no_blank_line(self) -> None:
        self.seed(b'{"k": "OLD"}\n')

        append_json_line(self.path, {"k": "NEW"})

        self.assertEqual(self.lines(), ['{"k": "OLD"}', '{"k": "NEW"}'])

    def test_an_empty_file_gains_no_leading_blank_line(self) -> None:
        """Not an exotic state: both compactors "create or truncate a file that
        has nothing to compact", so an empty journal is ordinary. Reading the
        last byte of one raises OSError, which is why the guard opens once and
        catches rather than pre-checking the size."""
        self.seed(b"")

        append_json_line(self.path, {"k": "NEW"})

        self.assertEqual(self.lines(), ['{"k": "NEW"}'])

    def test_a_missing_file_is_created_with_its_directory(self) -> None:
        self.assertFalse(self.path.exists())

        append_json_line(self.path, {"k": "NEW"})

        self.assertEqual(self.lines(), ['{"k": "NEW"}'])

    def test_the_payload_reaches_the_file_in_ONE_write(self) -> None:
        """Structural, not incidental. Two `write` calls collapse into one
        syscall under default buffering but NOT under line buffering, so the
        separator would land on disk alone and open a window for another
        writer. Building one string removes the dependency on a setting the
        call site does not state."""
        self.seed(b'{"k": "OLD", "trunc')
        written: list[str] = []

        real_open = Path.open

        def recording_open(self_path, *args, **kwargs):
            handle = real_open(self_path, *args, **kwargs)
            if "a" in str(args[0] if args else kwargs.get("mode", "")):
                original = handle.write

                def capture(text):
                    written.append(text)
                    return original(text)

                handle.write = capture  # type: ignore[method-assign]
            return handle

        with mock.patch.object(Path, "open", recording_open):
            append_json_line(self.path, {"k": "NEW"})

        self.assertEqual(len(written), 1, f"expected one write, got {written}")
        self.assertTrue(written[0].startswith("\n"))


class DurabilityAndFailure(_JournalCase):
    def test_the_append_is_fsynced(self) -> None:
        with mock.patch("alphalens_pipeline.brokers.journal.os.fsync") as fsync:
            append_json_line(self.path, {"k": "NEW"})

        fsync.assert_called_once()

    def test_a_write_failure_becomes_a_typed_error(self) -> None:
        with mock.patch.object(Path, "open", side_effect=OSError("No space left on device")):
            with self.assertRaises(JournalWriteError):
                append_json_line(self.path, {"k": "NEW"})

    def test_that_error_is_still_an_OSError(self) -> None:
        """Load-bearing: `control_loop` catches OSError around a journal write,
        alerts the operator and keeps the tick alive so the protection pass is
        not starved. A new class outside that hierarchy would silently turn a
        handled condition into an aborted tick."""
        self.assertTrue(issubclass(JournalWriteError, OSError))


class TheRepairIsAnnounced(_JournalCase):
    """The only thing that observes this failure mode as it happens.

    The malformed counter is visible solely to a human who runs `broker picks`;
    the drain never even reads it. Without this line the system can quietly heal
    hundreds of torn writes on a nearly full disk and say nothing.
    """

    def test_repairing_a_missing_separator_warns(self) -> None:
        self.seed(b'{"k": "OLD", "trunc')

        with self.assertLogs("alphalens_pipeline.brokers.journal", level=logging.WARNING) as logs:
            append_json_line(self.path, {"k": "NEW"})

        self.assertTrue(any("journal.jsonl" in line for line in logs.output), logs.output)

    def test_an_ordinary_append_is_silent(self) -> None:
        """Positive control: a warning on every append would be noise the
        operator learns to ignore, which is the same as having none."""
        self.seed(b'{"k": "OLD"}\n')

        with mock.patch.object(
            logging.getLogger("alphalens_pipeline.brokers.journal"), "warning"
        ) as warn:
            append_json_line(self.path, {"k": "NEW"})

        warn.assert_not_called()


class TheSerializerStaysPerCaller(_JournalCase):
    """`default` is a parameter, not a constant, and that is deliberate.

    Three journals serialize with `default=str`; `picks` does not. Unifying on
    `default=str` would turn an unrepresentable value in a TradeIntent into a
    silent string instead of a loud refusal — on the money path that is a
    regression dressed as tidiness.
    """

    def test_without_a_default_an_unserializable_value_refuses(self) -> None:
        with self.assertRaises(TypeError):
            append_json_line(self.path, {"k": object()})

    def test_with_default_str_it_is_stringified(self) -> None:
        append_json_line(self.path, {"k": Path("/tmp/x")}, default=str)

        self.assertEqual(self.records(), [{"k": "/tmp/x"}])


def _parses(line: str) -> bool:
    try:
        json.loads(line)
    except json.JSONDecodeError:
        return False
    return True


if __name__ == "__main__":
    unittest.main()
