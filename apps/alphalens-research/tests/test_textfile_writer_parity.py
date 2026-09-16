"""The two Prometheus textfile writers render values identically (#1462).

``alphalens_pipeline.observability.textfile`` and its Django mirror
``apps/alphalens-django/edge/ingest/textfile.py`` cannot share code: the slim
Django image does not install the pipeline package (ADR 0011). Both decide which
values are valid samples and how they are spelled, so a fix applied to one copy
and forgotten in the other would make the same gauge valid in one file and
dropped, or spelled differently, in the other.

The Django module imports only the standard library, so it is loaded here by
file path, under a private module name, without Django or its package
``__init__``. Both writers get the same pre-created directory through
``ALPHALENS_TEXTFILE_DIR``, which sidesteps their deliberate differences in
directory handling (mkdir and home fallback in the pipeline, neither in Django).
"""

from __future__ import annotations

import datetime as dt
import enum
import importlib.util
import os
import sys
import tempfile
import unittest
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from types import ModuleType
from unittest import mock

import numpy as np
import pandas as pd
from alphalens_pipeline.observability import textfile as pipeline_textfile

REPO_ROOT = Path(__file__).resolve().parents[3]
DJANGO_WRITER = REPO_ROOT / "apps" / "alphalens-django" / "edge" / "ingest" / "textfile.py"
_MODULE_NAME = "_django_textfile_under_test"


def _load_django_writer() -> ModuleType:
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, DJANGO_WRITER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(_MODULE_NAME, None)
    return module


class _Level(enum.IntEnum):
    HIGH = 3


class _HostileFloat(float):
    def __float__(self) -> float:
        raise ValueError("refuses to convert")

    def __repr__(self) -> str:
        raise RuntimeError("refuses to render")


CASES: tuple[tuple[str, object], ...] = (
    ("int", 3),
    ("negative int", -2),
    ("float", 1.5),
    ("epoch float", 1789545634.404397),
    ("integral float", 15000.0),
    ("IntEnum", _Level.HIGH),
    ("np.int64", np.int64(3)),
    ("np.uint64 max", np.uint64(2**64 - 1)),
    ("np.float64", np.float64(1.5)),
    ("np.float32", np.float32(0.1)),
    ("np.float16", np.float16(0.1)),
    ("Fraction", Fraction(1, 4)),
    ("True", True),
    ("False", False),
    ("str", "1"),
    ("None", None),
    ("Decimal", Decimal("1")),
    ("nan", float("nan")),
    ("inf", float("inf")),
    ("-inf", float("-inf")),
    ("np.bool_", np.bool_(True)),
    ("0-d array", np.array(2.5)),
    ("pd.NA", pd.NA),
    ("pd.NaT", pd.NaT),
    ("datetime", dt.datetime(2026, 9, 16)),
    ("huge Fraction", Fraction(10**400, 1)),
    ("hostile float", _HostileFloat(1.0)),
)


def _render_through(writer: ModuleType, directory: str, job: str, value: object) -> str:
    with (
        mock.patch.dict(os.environ, {writer.ENV_VAR: directory}),
        mock.patch.object(writer, "_LATCHED", set()),
        mock.patch.object(writer.logger, "error"),
        mock.patch.object(writer.logger, "info"),
    ):
        path = writer.emit_domain_metrics(job, {"g": value, "anchor": 1})
    return path.read_text()


class TestTheTwoWritersRenderEveryValueIdentically(unittest.TestCase):
    def setUp(self) -> None:
        self.django_textfile = _load_django_writer()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.pipeline_dir = Path(tmp.name) / "pipeline"
        self.django_dir = Path(tmp.name) / "django"
        self.pipeline_dir.mkdir()
        self.django_dir.mkdir()

    def test_the_loaded_module_is_the_django_file_and_not_the_pipeline_one(self) -> None:
        self.assertEqual(Path(self.django_textfile.__file__), DJANGO_WRITER)
        self.assertNotIn(_MODULE_NAME, sys.modules)

    def test_every_case_produces_byte_identical_files(self) -> None:
        for name, value in CASES:
            with self.subTest(case=name):
                ours = _render_through(pipeline_textfile, str(self.pipeline_dir), "parity", value)
                theirs = _render_through(
                    self.django_textfile, str(self.django_dir), "parity", value
                )
                self.assertEqual(ours, theirs)

    def test_the_shared_constants_agree(self) -> None:
        for name in ("INVALID_SAMPLES_METRIC", "_LATCH_CAP", "_CONVERSION_ERRORS"):
            with self.subTest(constant=name):
                self.assertEqual(
                    getattr(pipeline_textfile, name), getattr(self.django_textfile, name)
                )

    def test_the_comparison_can_refute(self) -> None:
        """Positive control: a renderer that differs on one shape must be caught."""
        drifted = _load_django_writer()
        original = drifted._render_value

        def _drifted(value: object) -> str | None:
            return "1" if value is True else original(value)

        with mock.patch.object(drifted, "_render_value", _drifted):
            ours = _render_through(pipeline_textfile, str(self.pipeline_dir), "parity", True)
            theirs = _render_through(drifted, str(self.django_dir), "parity", True)
        self.assertNotEqual(ours, theirs)


if __name__ == "__main__":
    unittest.main()
