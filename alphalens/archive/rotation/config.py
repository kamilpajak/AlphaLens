import hashlib
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ALLOWED_TICKERS = frozenset({"SPY", "QQQ", "IWM"})
MAX_RULES = 5
MAX_TILT_HARDCAP = 0.25
CORE_WEIGHT_TOL = 1e-6


class ConfigError(ValueError):
    pass


class DirtyRepoError(RuntimeError):
    pass


@dataclass(frozen=True)
class Rule:
    name: str
    signal: str
    operator: str  # "gt" | "lt" | "ge" | "le"
    threshold: float
    tilt: Mapping[str, float]


@dataclass(frozen=True)
class GateConfig:
    rolling_sharpe_min: float = 0.30
    carhart_oos_t_min: float = 1.50


@dataclass(frozen=True)
class RotationConfig:
    core_weights: Mapping[str, float]
    max_tilt: float
    rebalance_stride: int
    etf_spread_bps: Mapping[str, float]
    rules: tuple[Rule, ...]
    gates: GateConfig = field(default_factory=GateConfig)


@dataclass(frozen=True)
class ConfigFingerprint:
    config_path: str
    content_sha256: str
    git_sha: str


def capture_git_sha(*, allow_dirty: bool = False) -> str:
    if not allow_dirty:
        porcelain = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        )
        if porcelain.stdout.strip():
            raise DirtyRepoError("working tree is dirty; commit or stash before capturing SHA")

    rev_parse = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return rev_parse.stdout.strip()


def _parse_rules(raw: list) -> tuple[Rule, ...]:
    if not isinstance(raw, list):
        raise ConfigError("'rules' must be a list")
    if len(raw) > MAX_RULES:
        raise ConfigError(f"too many rules ({len(raw)}); R12 discipline caps at {MAX_RULES}")
    valid_ops = {"gt", "lt", "ge", "le"}
    rules = []
    for i, r in enumerate(raw):
        missing = {"name", "signal", "operator", "threshold", "tilt"} - set(r)
        if missing:
            raise ConfigError(f"rule #{i} missing fields: {sorted(missing)}")
        if r["operator"] not in valid_ops:
            raise ConfigError(
                f"rule '{r['name']}' has invalid operator '{r['operator']}'; "
                f"expected one of {sorted(valid_ops)}"
            )
        tilt = dict(r["tilt"])
        unknown = set(tilt) - ALLOWED_TICKERS
        if unknown:
            raise ConfigError(f"rule '{r['name']}' tilts unknown tickers: {sorted(unknown)}")
        rules.append(
            Rule(
                name=str(r["name"]),
                signal=str(r["signal"]),
                operator=str(r["operator"]),
                threshold=float(r["threshold"]),
                tilt={k: float(v) for k, v in tilt.items()},
            )
        )
    return tuple(rules)


def load_config(path: str | Path) -> RotationConfig:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    raw = yaml.safe_load(path.read_text()) or {}

    core = raw.get("core_weights") or {}
    if not isinstance(core, dict):
        raise ConfigError("'core_weights' must be a mapping ticker → weight")
    unknown_core = set(core) - ALLOWED_TICKERS
    if unknown_core:
        raise ConfigError(
            f"core_weights contains unknown tickers: {sorted(unknown_core)}; "
            f"allowed = {sorted(ALLOWED_TICKERS)}"
        )
    total = sum(float(v) for v in core.values())
    if abs(total - 1.0) > CORE_WEIGHT_TOL:
        raise ConfigError(f"core_weights must sum to 1.0 (got {total:.6f})")

    max_tilt = float(raw.get("max_tilt", 0.10))
    if not 0 < max_tilt <= MAX_TILT_HARDCAP:
        raise ConfigError(f"max_tilt must be in (0, {MAX_TILT_HARDCAP}]; got {max_tilt}")

    stride = int(raw.get("rebalance_stride", 63))
    if stride <= 0:
        raise ConfigError(f"rebalance_stride must be positive; got {stride}")

    etf_spread_bps = raw.get("etf_spread_bps") or {}
    if not isinstance(etf_spread_bps, dict):
        raise ConfigError("'etf_spread_bps' must be a mapping")
    unknown_spread = set(etf_spread_bps) - ALLOWED_TICKERS
    if unknown_spread:
        raise ConfigError(f"etf_spread_bps contains unknown tickers: {sorted(unknown_spread)}")

    rules = _parse_rules(raw.get("rules") or [])

    gates_raw = raw.get("gates") or {}
    gates = GateConfig(
        rolling_sharpe_min=float(gates_raw.get("rolling_sharpe_min", 0.30)),
        carhart_oos_t_min=float(gates_raw.get("carhart_oos_t_min", 1.50)),
    )

    return RotationConfig(
        core_weights={k: float(v) for k, v in core.items()},
        max_tilt=max_tilt,
        rebalance_stride=stride,
        etf_spread_bps={k: float(v) for k, v in etf_spread_bps.items()},
        rules=rules,
        gates=gates,
    )


def compute_fingerprint(path: str | Path, *, allow_dirty: bool = False) -> ConfigFingerprint:
    path = Path(path)
    content_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    git_sha = capture_git_sha(allow_dirty=allow_dirty)
    return ConfigFingerprint(
        config_path=str(path),
        content_sha256=content_sha256,
        git_sha=git_sha,
    )
