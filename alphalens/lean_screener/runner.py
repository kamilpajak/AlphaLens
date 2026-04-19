"""Orchestrate a one-shot Lean backtest in Docker and collect the JSON result.

The default `subprocess_runner` shells out to `docker`. Tests inject a fake
runner so they can simulate Lean's side-effect (writing JSON to results_dir)
without starting a container.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .schema import LeanOutput

logger = logging.getLogger(__name__)

SubprocessRunner = Callable[[list[str], int], "subprocess.CompletedProcess"]


class LeanRunError(RuntimeError):
    pass


def _default_subprocess_runner(cmd: list[str], timeout_sec: int):
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        check=False,
    )


@dataclass(frozen=True)
class LeanRunConfig:
    project_dir: Path
    data_dir: Path
    results_dir: Path
    logs_dir: Path
    image: str
    timeout_sec: int = 1800
    extra_env: dict[str, str] = field(default_factory=dict)


class LeanDockerRunner:
    """Builds `docker run` args for a one-shot Lean backtest and reads back JSON."""

    RESULTS_FILENAME = "candidates.json"

    def __init__(
        self,
        config: LeanRunConfig,
        subprocess_runner: SubprocessRunner = _default_subprocess_runner,
    ):
        self.config = config
        self._runner = subprocess_runner

    def build_docker_args(self) -> list[str]:
        cfg = self.config
        args = [
            "docker", "run", "--rm",
            "-v", f"{cfg.project_dir}:/Project",
            "-v", f"{cfg.data_dir}:/Data",
            "-v", f"{cfg.results_dir}:/Results",
            "-v", f"{cfg.logs_dir}:/Logs",
        ]
        for key, value in cfg.extra_env.items():
            args.extend(["-e", f"{key}={value}"])
        args.append(cfg.image)
        # The Lean base image's default ENTRYPOINT runs the backtest when
        # pointed at the project + data folders via these args.
        args.extend([
            "--data-folder", "/Data",
            "--results-destination-folder", "/Results",
            "--algorithm-location", "/Project/main.py",
            "--algorithm-language", "Python",
            "--algorithm-type-name", "LeanBatchScreener",
        ])
        return args

    def run(self) -> LeanOutput:
        cfg = self.config
        cfg.results_dir.mkdir(parents=True, exist_ok=True)
        cfg.logs_dir.mkdir(parents=True, exist_ok=True)

        target = cfg.results_dir / self.RESULTS_FILENAME
        # Remove any stale artifact so we can distinguish crash from success.
        if target.exists():
            target.unlink()

        args = self.build_docker_args()
        logger.info("lean docker run: %s", " ".join(args))
        started = datetime.now(timezone.utc)

        try:
            completed = self._runner(args, cfg.timeout_sec)
        except subprocess.TimeoutExpired as exc:
            raise LeanRunError(f"Lean timed out after {cfg.timeout_sec}s") from exc

        # Persist stdout/stderr for post-mortem.
        self._persist_log("stdout", completed.stdout or "", started)
        self._persist_log("stderr", completed.stderr or "", started)

        if completed.returncode != 0:
            raise LeanRunError(
                f"Lean exited with code {completed.returncode}. See logs in {cfg.logs_dir}"
            )

        if not target.exists():
            raise LeanRunError(
                f"Lean finished cleanly but did not write {target}. "
                "Check algorithm logs."
            )

        output = LeanOutput.from_file(target)
        if output.status != "success":
            raise LeanRunError(f"Lean reported status={output.status!r}")
        return output

    def _persist_log(self, stream: str, body: str, started: datetime) -> None:
        if not body:
            return
        stamp = started.strftime("%Y%m%dT%H%M%SZ")
        path = self.config.logs_dir / f"lean_{stamp}_{stream}.log"
        try:
            path.write_text(body)
        except OSError as exc:
            logger.warning("failed to persist %s log: %s", stream, exc)


def docker_available(subprocess_runner: SubprocessRunner | None = None) -> bool:
    """Cheap `docker --version` probe — returns False if Docker isn't usable."""
    runner = subprocess_runner or _default_subprocess_runner
    try:
        completed = runner(["docker", "--version"], 5)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False
    return completed.returncode == 0


def default_run_config() -> LeanRunConfig:
    """Standard layout — drop-in for CLI/launchd callers."""
    from .config import (
        DATA_DIR, LEAN_DOCKER_IMAGE, LEAN_PROJECT_DIR, LOGS_DIR, RESULTS_DIR,
    )

    extra_env: dict[str, str] = {}
    api_key = os.environ.get("POLYGON_API_KEY", "")
    if api_key:
        extra_env["POLYGON_API_KEY"] = api_key

    return LeanRunConfig(
        project_dir=LEAN_PROJECT_DIR,
        data_dir=DATA_DIR,
        results_dir=RESULTS_DIR,
        logs_dir=LOGS_DIR,
        image=LEAN_DOCKER_IMAGE,
        extra_env=extra_env,
    )
