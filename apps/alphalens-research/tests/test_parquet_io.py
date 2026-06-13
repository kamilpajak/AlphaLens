"""Unit tests for the shared atomic parquet writer.

Pins the temp-then-replace contract (no leak, original untouched on failure) and
that the ``index`` flag is honoured — the default ``index=True`` preserves the
clean-titles writer's existing bytes; the brief-enrichment path opts into
``index=False``.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pandas as pd
from alphalens_pipeline.data.parquet_io import write_parquet_atomic


def _leftovers(d: Path, target: str) -> list[str]:
    return [p.name for p in d.iterdir() if p.name != target]


class TestWriteParquetAtomic(unittest.TestCase):
    def test_replaces_in_place_no_temp_leak(self) -> None:
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            path = d / "out.parquet"
            write_parquet_atomic(pd.DataFrame({"a": [1, 2], "b": ["x", "y"]}), path, index=False)
            self.assertTrue(path.exists())
            self.assertEqual(list(pd.read_parquet(path)["a"]), [1, 2])
            self.assertEqual(_leftovers(d, "out.parquet"), [])  # no .tmp leak

    def test_failure_unlinks_temp_and_keeps_original(self) -> None:
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            path = d / "out.parquet"
            pd.DataFrame({"a": [99]}).to_parquet(path, index=False)
            with patch.object(pd.DataFrame, "to_parquet", side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    write_parquet_atomic(pd.DataFrame({"a": [1, 2]}), path, index=False)
            self.assertEqual(list(pd.read_parquet(path)["a"]), [99])  # original untouched
            self.assertEqual(_leftovers(d, "out.parquet"), [])  # temp cleaned up

    def test_index_flag_controls_index_persistence(self) -> None:
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            df = pd.DataFrame({"a": [1, 2]}, index=pd.Index(["r0", "r1"], name="rid"))
            write_parquet_atomic(df, d / "with.parquet")  # default index=True
            write_parquet_atomic(df, d / "without.parquet", index=False)
            # index=True preserves the named index; index=False drops it.
            self.assertEqual(pd.read_parquet(d / "with.parquet").index.name, "rid")
            self.assertIsNone(pd.read_parquet(d / "without.parquet").index.name)

    def test_tempfile_created_in_target_dir(self) -> None:
        # Same-filesystem temp so os.replace is an atomic rename (no cross-device).
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            path = d / "out.parquet"
            with patch(
                "alphalens_pipeline.data.parquet_io.tempfile.mkstemp", wraps=tempfile.mkstemp
            ) as spy:
                write_parquet_atomic(pd.DataFrame({"a": [1]}), path, index=False)
            self.assertEqual(spy.call_args.kwargs["dir"], path.parent)


if __name__ == "__main__":
    unittest.main()
