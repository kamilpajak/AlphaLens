"""Live PyPI yank probe — opt-in via PYPI_YANK_LIVE_TEST=1.

The yanked-dependency gate (#1507) reads ``info.yanked`` / ``info.yanked_reason``
from ``https://pypi.org/pypi/<name>/<version>/json``. The hermetic tests in
``tests/test_check_yanked_packages.py`` feed canned payloads to the pure
functions, so nothing there would notice PyPI reshaping that document — and a
gate that reads a field PyPI stopped sending reports every package as
``unchecked`` at best, or silently clean at worst.

This probe calls the gate's own ``fetch_yank`` rather than the raw endpoint, on
purpose: a probe of the URL alone tests the vendor, while the thing that can
actually rot is the parsing in between. If ``fetch_yank`` ever reads the flag
from the wrong nesting level, or treats a string ``"true"`` as a yank, this is
what says so.

Both fixtures are permanent. A yank is never undone, so ``pandas 3.0.4`` stays a
valid known-yanked example indefinitely; ``pandas 3.0.6`` is the clean control
that keeps a probe which always says "yanked" from passing.

    PYPI_YANK_LIVE_TEST=1 .venv/bin/python -m unittest tests.live.test_pypi_yank_live -v
"""

from __future__ import annotations

import os
import unittest

from scripts import check_yanked_packages as gate

from tests.live import PermanentProbeError, run_probes

_LIVE = os.environ.get("PYPI_YANK_LIVE_TEST") == "1"

#: Withdrawn 2026-09 for "Reported segfaults with datetime-related
#: functionality" — the release that motivated the gate.
_YANKED = gate.Pin("pandas", "3.0.4")
_CLEAN = gate.Pin("pandas", "3.0.6")


@unittest.skipUnless(_LIVE, "set PYPI_YANK_LIVE_TEST=1 to run the live PyPI yank probe")
class TestPypiYankLive(unittest.TestCase):
    def test_the_gate_still_reads_a_real_yank_and_a_real_clean_release(self) -> None:
        def _yanked() -> None:
            result = gate.fetch_yank(_YANKED)
            if result.state == gate.UNCHECKED:
                # fetch_yank folds transients into UNCHECKED after its retries,
                # so the probe cannot tell a blip from a break here; the shared
                # helper tolerates a lone transient and fails on a majority.
                raise PermanentProbeError(f"could not reach PyPI for {_YANKED}: {result.reason}")
            if result.state != gate.YANKED:
                raise PermanentProbeError(
                    f"{_YANKED.name}=={_YANKED.version} read back as {result.state!r}; "
                    "a yank is permanent, so this means the payload shape moved"
                )
            if not result.reason:
                raise PermanentProbeError("a yanked release came back with no reason at all")

        def _clean() -> None:
            result = gate.fetch_yank(_CLEAN)
            if result.state == gate.UNCHECKED:
                raise PermanentProbeError(f"could not reach PyPI for {_CLEAN}: {result.reason}")
            if result.state != gate.CLEAN:
                raise PermanentProbeError(
                    f"{_CLEAN.name}=={_CLEAN.version} read back as {result.state!r}, not clean"
                )

        run_probes(
            self,
            {"pandas/3.0.4 (yanked)": _yanked, "pandas/3.0.6 (clean)": _clean},
            label="pypi-yank",
        )


if __name__ == "__main__":
    unittest.main()
