"""Named, versioned exit-geometry policies.

A policy pins the numeric parameters of an exit-geometry family (currently
only the ATR bracket) behind a stable ``(name, version)`` key, so callers
(the ``/edge`` what-if lens today, the SIM broker-manager later) resolve a
policy by name instead of threading raw multipliers around. ``"atr_bracket_1p5"``
is the wire key used in stored config / API payloads; "bezpazery" is its
human alias (the betlejem5-inspired bracket doctrine, memo §2 /
``docs/research/bezpazery_lens_design_2026_07_16.md`` §2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from broker_contract.exit_geometry.levels import atr_bracket_levels

if TYPE_CHECKING:
    from broker_contract.exit_geometry.policy import ExitPolicy


@dataclass(frozen=True)
class ExitGeometryPolicy:
    name: str
    version: int
    stop_atr_mult: float
    tp_atr_mult: float
    tp_floor_frac: float

    def levels(
        self, blended: float, atr: float, *, ceiling_price: float | None = None
    ) -> tuple[float, float] | None:
        return atr_bracket_levels(
            blended,
            atr,
            stop_atr_mult=self.stop_atr_mult,
            tp_atr_mult=self.tp_atr_mult,
            tp_floor_frac=self.tp_floor_frac,
            ceiling_price=ceiling_price,
        )


# bezpazery v1 pinned values (memo §2 / bezpazery_lens_design_2026_07_16.md §2).
_ATR_BRACKET_1P5 = ExitGeometryPolicy("atr_bracket_1p5", 1, 1.5, 1.5, 0.006)

EXIT_GEOMETRY_POLICIES: dict[tuple[str, int], ExitGeometryPolicy] = {
    ("atr_bracket_1p5", 1): _ATR_BRACKET_1P5,
}


def resolve_policy(name: str, version: int = 1) -> ExitGeometryPolicy:
    """Look up a registered policy by name + version.

    Raises ``ValueError`` (not ``KeyError``) for an unknown policy so callers
    get a message-bearing exception without having to know the registry's
    internal key shape.
    """
    try:
        return EXIT_GEOMETRY_POLICIES[(name, version)]
    except KeyError:
        raise ValueError(f"unknown exit-geometry policy: {name!r} v{version}") from None


def resolve_exit_policy(name: str) -> ExitPolicy:
    """Resolve a behavioral ExitPolicy by name (fail-fast on unknown).

    CALL ONCE AT STARTUP — never inside the protection pass (a ValueError here
    would starve the unconditional protection). Lazy import of ``policy`` avoids
    a module import cycle (policy.py imports ExitGeometryPolicy from this module).
    """
    from broker_contract.exit_geometry.policy import AtrBracketPolicy, SetupStaticPolicy

    registry: dict[str, ExitPolicy] = {
        "setup_static": SetupStaticPolicy(),
        "atr_bracket_1p5": AtrBracketPolicy(resolve_policy("atr_bracket_1p5")),
    }
    try:
        return registry[name]
    except KeyError:
        raise ValueError(f"unknown exit policy: {name!r}") from None
