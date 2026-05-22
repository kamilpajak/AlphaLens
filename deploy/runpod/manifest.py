"""Generate a run manifest (git SHA + dep versions + env + pod specs).

Written to ``/workspace/alphalens/runs/<run_id>/manifest.json`` at the start
of every experiment run by run_experiment.sh. Synced to the network volume
on completion so every result is reproducible by SHA + lockfile.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
from pathlib import Path


def _run(cmd: list[str], cwd: Path | None = None) -> str:
    try:
        out = subprocess.run(cmd, cwd=cwd, check=False, capture_output=True, text=True, timeout=15)
        return out.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def _file_sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _du_kb(path: Path) -> int | None:
    if not path.exists():
        return None
    out = _run(["du", "-sk", str(path)])
    if not out:
        return None
    try:
        return int(out.split()[0])
    except (ValueError, IndexError):
        return None


def build_manifest(repo_root: Path, run_id: str, command: str | None) -> dict:
    git_sha = _run(["git", "rev-parse", "HEAD"], cwd=repo_root)
    git_branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_root)
    git_dirty = bool(_run(["git", "status", "--porcelain"], cwd=repo_root))

    uv_lock = repo_root / "uv.lock"
    pyproject = repo_root / "pyproject.toml"

    data_root = Path.home() / ".alphalens"
    datasets = [
        "companyfacts_parquet",
        "ivolatility_smd",
        "prices",
        "factors",
        "pit_universe",
        "survivorship",
    ]
    dataset_sizes = {d: _du_kb(data_root / d) for d in datasets}

    return {
        "run_id": run_id,
        "started_at": dt.datetime.now(dt.UTC).isoformat() + "Z",
        "command": command,
        "git": {
            "sha": git_sha,
            "branch": git_branch,
            "dirty": git_dirty,
        },
        "deps": {
            "uv_lock_sha256": _file_sha256(uv_lock),
            "pyproject_sha256": _file_sha256(pyproject),
        },
        "runtime": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "hostname": socket.gethostname(),
            "runpod_pod_id": os.environ.get("RUNPOD_POD_ID"),
            "runpod_gpu_count": os.environ.get("RUNPOD_GPU_COUNT"),
            "runpod_cpu_count": os.cpu_count(),
            "memory_total_kb": _read_memtotal_kb(),
            "disk_free_workspace_kb": _disk_free_kb(Path("/workspace")),
        },
        "data": {
            "root": str(data_root),
            "dataset_sizes_kb": dataset_sizes,
        },
    }


def _read_memtotal_kb() -> int | None:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1])
    except OSError:
        return None
    return None


def _disk_free_kb(path: Path) -> int | None:
    try:
        usage = shutil.disk_usage(path)
        return usage.free // 1024
    except OSError:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--command", default=None)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parent.parent
    manifest = build_manifest(repo_root, args.run_id, args.command)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Wrote manifest: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
