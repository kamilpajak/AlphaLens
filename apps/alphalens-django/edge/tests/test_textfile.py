"""The Django-side Prometheus textfile writer (the ADR 0011 mirror of
``alphalens_pipeline.observability.textfile``).

Three behaviours differ from the pipeline module on purpose, and each has a
test here: no home-directory fallback (an unset ``ALPHALENS_TEXTFILE_DIR`` writes
nothing and warns), no ``mkdir`` (a missing directory inside the container means
the bind mount is missing, so the write must fail rather than land on an
unscraped path), and an unwritable directory raises instead of being swallowed.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

import pytest

from edge.ingest import textfile

_JOB = "edge-mirror"


def test_writes_the_domain_file_in_exposition_format(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(textfile.ENV_VAR, str(tmp_path))

    written = textfile.emit_domain_metrics(
        _JOB, {"alphalens_edge_mirror_unsettled_dates": 117, 'a_gauge{label="x"}': 2.5}
    )

    assert written == tmp_path / "alphalens_domain_edge-mirror.prom"
    assert written.read_text() == (
        'alphalens_edge_mirror_unsettled_dates 117\na_gauge{label="x"} 2.5\n'
    )


def test_file_is_world_readable_and_no_temp_file_is_left(tmp_path: Path, monkeypatch):
    # node_exporter reads the directory as ``nobody``; a 0o600 tempfile would be
    # seen but not opened, and the series would silently drop.
    monkeypatch.setenv(textfile.ENV_VAR, str(tmp_path))

    written = textfile.emit_domain_metrics(_JOB, {"g": 1})

    assert stat.S_IMODE(written.stat().st_mode) == 0o644
    assert [p.name for p in tmp_path.iterdir()] == [written.name]


def test_second_emit_replaces_the_file(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(textfile.ENV_VAR, str(tmp_path))
    textfile.emit_domain_metrics(_JOB, {"g": 1})

    written = textfile.emit_domain_metrics(_JOB, {"g": 2})

    assert written.read_text() == "g 2\n"


def test_unset_env_writes_nothing_and_warns(tmp_path: Path, monkeypatch, caplog):
    monkeypatch.delenv(textfile.ENV_VAR, raising=False)
    monkeypatch.chdir(tmp_path)

    with caplog.at_level(logging.WARNING):
        written = textfile.emit_domain_metrics(_JOB, {"g": 1})

    assert written is None
    assert list(tmp_path.iterdir()) == []
    assert f"{textfile.ENV_VAR} unset" in caplog.text


def test_missing_directory_raises_and_is_not_created(tmp_path: Path, monkeypatch):
    # Inside the container a missing directory means the compose bind mount is
    # missing. Creating it would "succeed" into an unscraped path (#377 / #1366).
    missing = tmp_path / "not-mounted"
    monkeypatch.setenv(textfile.ENV_VAR, str(missing))

    with pytest.raises(OSError):
        textfile.emit_domain_metrics(_JOB, {"g": 1})

    assert not missing.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
def test_unwritable_directory_raises(tmp_path: Path, monkeypatch):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    monkeypatch.setenv(textfile.ENV_VAR, str(locked))
    try:
        with pytest.raises(OSError):
            textfile.emit_domain_metrics(_JOB, {"g": 1})
    finally:
        locked.chmod(0o700)
