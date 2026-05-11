"""Smoke-run a registered strategy's experiment script.

Invokes the strategy's ``experiment_*.py`` via subprocess with a tiny
universe + short window so failure surfaces in <2 min instead of after
~30 h of pod compute. Critical safety property (zen 2026-05-11): the
``--out`` argument is OVERRIDDEN with an ephemeral
``/tmp/preaudit_smoke_<uuid>.json`` path so a smoke can never overwrite
a concurrent audit's output artifact, regardless of what the experiment
script's default ``--out`` happens to be.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from alphalens.preaudit.profiles import (
    SMOKE_PROFILES,
    SmokeProfile,
    SmokeResult,
    SmokeStatus,
)
from alphalens_cli.commands.audit import _SCRIPTS

# Smoke wall budget. cap=300 over 1 quarter typically runs in 90s on
# local SSD; allow 5x headroom for cold parquet on MooseFS.
DEFAULT_SMOKE_TIMEOUT_S: int = 300

# stderr tail length attached to FAIL results — enough context for
# diagnostic without flooding the caller's terminal.
_STDERR_TAIL_CHARS = 2000


def run_smoke(
    strategy: str,
    *,
    profile: SmokeProfile | None = None,
    timeout_s: int = DEFAULT_SMOKE_TIMEOUT_S,
    python_executable: str | None = None,
) -> SmokeResult:
    """Execute the strategy's experiment script as a smoke test.

    Returns a :class:`SmokeResult`. The runner does NOT raise on
    subprocess failure — callers inspect ``result.status``.
    """
    if strategy not in _SCRIPTS:
        return SmokeResult(
            status=SmokeStatus.UNKNOWN_STRATEGY,
            detail=(f"strategy {strategy!r} not in audit._SCRIPTS. Known: {sorted(_SCRIPTS)}"),
        )

    if profile is None:
        profile = SMOKE_PROFILES.get(strategy)
    if profile is None:
        return SmokeResult(
            status=SmokeStatus.NO_PROFILE,
            detail=(
                f"strategy {strategy!r} is registered for `alphalens audit` "
                f"but no SmokeProfile exists. Add one to "
                f"`alphalens/preaudit/profiles.py::SMOKE_PROFILES` before "
                f"using `alphalens preaudit {strategy}`."
            ),
        )

    script_path = _SCRIPTS[strategy]
    python = python_executable or sys.executable
    ephemeral_out = Path(f"/tmp/preaudit_smoke_{uuid.uuid4().hex}.json")

    is_start, is_end = profile.smoke_window
    argv: list[str] = [
        python,
        str(script_path),
        "--is-start",
        is_start.isoformat(),
        "--is-end",
        is_end.isoformat(),
        "--out",
        str(ephemeral_out),
        *profile.extra_args,
    ]

    start = time.monotonic()
    try:
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
                env={**os.environ, "ALPHALENS_WORKERS": "1"},
            )
        except subprocess.TimeoutExpired as exc:
            return SmokeResult(
                status=SmokeStatus.TIMEOUT,
                exit_code=None,
                duration_s=time.monotonic() - start,
                detail=(
                    f"smoke exceeded {timeout_s}s wall budget; "
                    f"running on a healthy environment shouldn't take this long.\n"
                    f"command: {' '.join(argv)}\n"
                    f"partial stdout (tail): {(exc.stdout or '')[-_STDERR_TAIL_CHARS:]}\n"
                    f"partial stderr (tail): {(exc.stderr or '')[-_STDERR_TAIL_CHARS:]}"
                ),
            )

        duration = time.monotonic() - start
        if proc.returncode == 0:
            return SmokeResult(
                status=SmokeStatus.PASS,
                exit_code=0,
                duration_s=duration,
            )
        return SmokeResult(
            status=SmokeStatus.FAIL,
            exit_code=proc.returncode,
            duration_s=duration,
            detail=(
                f"experiment subprocess exited {proc.returncode}.\n"
                f"command: {' '.join(argv)}\n"
                f"stderr (tail):\n{(proc.stderr or '')[-_STDERR_TAIL_CHARS:]}"
            ),
        )
    finally:
        # Always clean up the ephemeral output, even on timeout or
        # subprocess failure. /tmp should never accumulate
        # preaudit_smoke_* leftovers from this code path.
        try:
            ephemeral_out.unlink(missing_ok=True)
        except OSError:
            # Permissions on /tmp aren't ours — log via detail later if
            # we ever care; for now, silently ignore.
            pass
