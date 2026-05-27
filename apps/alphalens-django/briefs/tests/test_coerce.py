"""Pure-function coercion tests. No Django setup needed."""

from __future__ import annotations

import datetime as dt
import math

import numpy as np
import pandas as pd
import pytest

from briefs.ingest.coerce import (
    coerce_bool,
    coerce_date,
    coerce_datetime,
    coerce_float,
    coerce_int,
    coerce_json_obj,
    coerce_list_str,
    coerce_str,
    is_missing,
)


class TestIsMissing:
    def test_none(self):
        assert is_missing(None)

    def test_nan(self):
        assert is_missing(float("nan"))

    def test_pandas_nat(self):
        assert is_missing(pd.NaT)

    def test_zero_is_not_missing(self):
        assert not is_missing(0)
        assert not is_missing(0.0)

    def test_empty_string_is_not_missing(self):
        # callers decide whether '' should round-trip; is_missing is strict.
        assert not is_missing("")


class TestCoerceFloat:
    def test_passthrough(self):
        assert coerce_float(1.5) == pytest.approx(1.5)

    def test_int_promoted(self):
        assert coerce_float(7) == pytest.approx(7.0)

    def test_nan_to_none(self):
        assert coerce_float(float("nan")) is None

    def test_none_to_none(self):
        assert coerce_float(None) is None


class TestCoerceInt:
    def test_passthrough(self):
        assert coerce_int(3) == 3

    def test_float_truncated(self):
        assert coerce_int(3.9) == 3

    def test_nan_to_none(self):
        assert coerce_int(math.nan) is None


class TestCoerceBool:
    def test_truthy(self):
        assert coerce_bool(True) is True
        assert coerce_bool(1) is True

    def test_falsy(self):
        assert coerce_bool(False) is False
        assert coerce_bool(0) is False

    def test_missing_defaults_false(self):
        assert coerce_bool(None) is False
        assert coerce_bool(math.nan) is False


class TestCoerceListStr:
    def test_python_list(self):
        assert coerce_list_str(["a", "b"]) == ["a", "b"]

    def test_numpy_array(self):
        assert coerce_list_str(np.array(["x", "y"])) == ["x", "y"]

    def test_scalar_string(self):
        assert coerce_list_str("solo") == ["solo"]

    def test_missing_returns_empty(self):
        assert coerce_list_str(None) == []
        assert coerce_list_str(math.nan) == []

    def test_mixed_types_stringified(self):
        assert coerce_list_str([1, 2.5, "x"]) == ["1", "2.5", "x"]


class TestCoerceDate:
    def test_iso_string(self):
        assert coerce_date("2026-05-22") == dt.date(2026, 5, 22)

    def test_iso_string_with_time_truncates(self):
        assert coerce_date("2026-05-22T10:30:00") == dt.date(2026, 5, 22)

    def test_date_passthrough(self):
        d = dt.date(2026, 1, 1)
        assert coerce_date(d) == d

    def test_pandas_timestamp(self):
        assert coerce_date(pd.Timestamp("2026-05-22")) == dt.date(2026, 5, 22)

    def test_invalid_returns_none(self):
        assert coerce_date("garbage") is None
        assert coerce_date("") is None

    def test_missing(self):
        assert coerce_date(None) is None
        assert coerce_date(pd.NaT) is None


class TestCoerceDatetime:
    def test_naive_iso_gets_utc(self):
        result = coerce_datetime("2026-05-22T09:30:00")
        assert result == dt.datetime(2026, 5, 22, 9, 30, tzinfo=dt.UTC)

    def test_aware_iso_preserved(self):
        result = coerce_datetime("2026-05-22T09:30:00+00:00")
        assert result == dt.datetime(2026, 5, 22, 9, 30, tzinfo=dt.UTC)

    def test_missing(self):
        assert coerce_datetime(None) is None
        assert coerce_datetime(pd.NaT) is None


class TestCoerceJsonObj:
    def test_json_string_parses_to_dict(self):
        assert coerce_json_obj('{"status": "OK", "n": 3}') == {"status": "OK", "n": 3}

    def test_dict_passthrough(self):
        assert coerce_json_obj({"a": 1}) == {"a": 1}

    def test_missing_is_none(self):
        assert coerce_json_obj(None) is None
        assert coerce_json_obj(pd.NaT) is None
        assert coerce_json_obj("") is None

    def test_unparseable_is_none(self):
        assert coerce_json_obj("{ not json") is None

    def test_non_object_json_is_none(self):
        # A JSON array / scalar is not an object → None (object-shaped field only).
        assert coerce_json_obj("[1, 2, 3]") is None
        assert coerce_json_obj("42") is None


class TestCoerceStr:
    def test_passthrough(self):
        assert coerce_str("hello") == "hello"

    def test_none(self):
        assert coerce_str(None) is None

    def test_date_isoformatted(self):
        assert coerce_str(dt.date(2026, 5, 22)) == "2026-05-22"
