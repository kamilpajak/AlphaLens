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
from typing import TYPE_CHECKING, Any

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


def exit_policy_registry() -> dict[str, ExitPolicy]:
    """Every behavioral ExitPolicy, keyed by its own name.

    A key is the policy's IDENTITY, not a lookup convenience: it is what an
    operator reads in a log line. Each policy is therefore constructed with its
    own key as ``name`` (issue #1138) — before that the two bracket policies both
    reported the name of the geometry they wrap, so no record could say which of
    them ran.

    Since #1414 no ENV VAR selects from here. The daemon resolves a policy from
    the document (:func:`resolve_declared_policy`); what is left of this registry
    is the research surface — ``paper.sizing`` reads ``breakeven_trail`` so the
    brief's declaration cannot drift from the deployed numbers, and the ``/edge``
    replay reads ``atr_bracket_1p5``. ``trailing_atr`` went with the variable: it
    was reachable only by name, and no name is resolved by name any more.

    Exposed (rather than inlined in :func:`resolve_exit_policy`) so a test can
    enumerate the registry and assert that property for EVERY entry, including
    ones added later. Lazy import of ``policy`` avoids a module import cycle
    (policy.py imports ExitGeometryPolicy from this module).
    """
    from broker_contract.exit_geometry.policy import (
        AtrBracketPolicy,
        BreakevenTrailPolicy,
        SetupStaticPolicy,
    )

    # Both bracket policies place against the SAME geometry and differ only in
    # how the exit then moves; that is exactly why the behavioral name cannot be
    # derived from the geometry.
    geom = resolve_policy("atr_bracket_1p5")
    return {
        "setup_static": SetupStaticPolicy(),
        "atr_bracket_1p5": AtrBracketPolicy(geom, name="atr_bracket_1p5"),
        # The lens-faithful break-even + fractional-giveback trail (the live
        # port of be_0p5r_trail0p6): no geometry — the brief ladder + brief
        # disaster stop stay placed; only the stop is managed.
        "breakeven_trail": BreakevenTrailPolicy(
            activation_r=0.5, trail_frac=0.6, name="breakeven_trail"
        ),
    }


def resolve_exit_policy(name: str) -> ExitPolicy:
    """Resolve a behavioral ExitPolicy by name (fail-fast on unknown).

    CALL ONCE AT STARTUP — never inside the protection pass (a ValueError here
    would starve the unconditional protection).
    """
    try:
        return exit_policy_registry()[name]
    except KeyError:
        raise ValueError(f"unknown exit policy: {name!r}") from None


def resolve_declared_policy(reaction: Any) -> ExitPolicy:
    """The exit policy a DOCUMENT's reaction primitive asks for (#1236).

    The counterpart to :func:`resolve_exit_policy`, which answers the same
    question from a process-wide environment variable. Here the answer comes from
    the intent itself, so an external producer states what it wants instead of
    inheriting whatever this deployment is configured for — and the #1406 door
    has something concrete to accept and to refuse.

    ``None`` -> the inert policy. That is THE MIGRATION RULE, not a default:
    every pick armed before this existed declares nothing, and a manual pick
    declares nothing by design (#1325). Falling back to the daemon-wide policy
    would start trailing all of them the moment this deploys.

    Parameters are taken from the primitive, never from the registry's own entry.
    A declaration whose numbers the executor silently replaced with its own would
    be a field accepted and not honoured. The resolved policy still reports the
    registry FAMILY name, so a log line and the journal stamp name something an
    operator can look up.

    An unhonourable primitive (``ModelPush``, whose levels would arrive through an
    ``amend_exit`` call that does not exist) degrades to the inert policy rather
    than raising. The door refuses it loudly; THIS function is reached from a
    journal stamp inside the protection pass, where a raise would starve the
    never-naked backstop.
    """
    from broker_contract.exit_geometry.policy import (
        BreakevenTrailPolicy,
        ReanchorOnFillPolicy,
        SetupStaticPolicy,
    )
    from broker_contract.trade_intent.schema import ReanchorOnFill, TrailingStop

    if isinstance(reaction, TrailingStop):
        return BreakevenTrailPolicy(
            activation_r=reaction.arm_trigger_r,
            trail_frac=reaction.trail_frac,
            name="breakeven_trail",
        )
    if isinstance(reaction, ReanchorOnFill):
        return ReanchorOnFillPolicy(k_atr=reaction.k_atr, name="reanchor_on_fill")
    return SetupStaticPolicy()
