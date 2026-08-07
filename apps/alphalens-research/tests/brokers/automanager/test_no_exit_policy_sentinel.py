"""Static-source guard pinning the two invariants the ExitPolicy refactor
(Tasks 1-5: registry afccb123, envelope dada8c82, startup-cache c5d4dbf7,
placement 3b8e8f4e, reanchor 9c40951) exists to enforce.

The refactor replaced an ``ALPHALENS_BROKER_EXIT_POLICY`` env-string sentinel
(``_exit_policy() == "setup_static"`` / ``!=``) read on the per-tick hot path
with a resolved-once :class:`~broker_contract.exit_geometry.ExitPolicy`
cached on ``LoopDeps``/``ProtectionView`` at startup
(``control_loop.build_default_deps``). Two invariants must hold forever:

1. NO STRING SENTINEL in the exit-policy decision paths — the per-tick
   protection/placement code must never compare a raw string against
   ``"setup_static"``; it must dispatch on the cached ``ExitPolicy`` object
   (``exit_policy.applies_geometry`` / ``exit_policy.decide_reanchor(...)``).
2. NO HOT-PATH RESOLVE — ``resolve_exit_policy(...)``/``_exit_policy()`` (the
   registry lookup + raw env read) must never be CALLED from the per-tick
   protection or placement-journal code; the only allowed resolve site is
   startup, in ``build_default_deps``.

This is a pure ``ast``-based EXECUTABLE-source text-scan guard (comments and
docstrings stripped via ``_executable_source``, so a historical comment
documenting the retired sentinel in past tense does not trip it) — it holds
even for code paths that no example-based test happens to exercise, and it
is the adversarial-review guard from the refactor's design memo.
"""

import ast
import inspect
import textwrap
import unittest

from alphalens_pipeline.brokers.automanager.control_loop import (
    _geometry_shadow_stamp,
    _journal_tranche_plan,
    _place_tiers,
)
from alphalens_pipeline.brokers.automanager.position_manager import (
    _maybe_reanchor,
    _reconcile_long,
    reconcile_protection,
)


def _executable_source(fn) -> str:
    """Source of fn with comments and docstrings removed (executable code only).

    ``ast`` drops all comments; we additionally strip the docstring of the
    function and every nested function so a historical/explanatory comment or
    docstring can mention the retired sentinel (e.g. documenting what used to
    be there, in past tense) without tripping this guard. The guard's job is
    to catch a REINTRODUCED sentinel or hot-path resolve in real, executable
    code — not prose that quotes it.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


# The old env-sentinel comparison, checked as the bare token (quote-agnostic:
# ``ast.unparse`` normalizes string quotes to single quotes). ``_journal_tier``
# is a function NESTED inside ``_place_tiers`` — ``inspect.getsource``/
# ``_executable_source`` on ``_place_tiers`` returns the whole enclosing
# function body, which is what we want to scan for it.
_SENTINEL_TOKEN = "setup_static"

# The placement-journal helpers ``_place_tiers`` delegates to. They live at module
# level (not nested), so scanning ``_place_tiers`` alone would NOT see them —
# scan each one too, or the guard silently shrinks every time a block is
# extracted out of the placement path.
_PLACEMENT_JOURNAL_HELPERS = (_geometry_shadow_stamp, _journal_tranche_plan)

# The registry-resolve call sites. ``build_default_deps`` (startup, NOT
# scanned here) is the one allowed resolve site.
_HOT_PATH_RESOLVE_CALL = "resolve_exit_policy("
_HOT_PATH_RAW_ENV_READ_CALL = "_exit_policy()"


class NoExitPolicySentinelSurvivesTheRefactor(unittest.TestCase):
    """Pins invariant 1 — no ``"setup_static"`` string-equality sentinel in
    the exit-policy decision paths (``_place_tiers``/nested ``_journal_tier``,
    ``_maybe_reanchor``). Scanned on EXECUTABLE code only (comments and
    docstrings stripped via ``_executable_source``) — a historical comment
    documenting the retired sentinel in past tense is legitimate and must not
    trip this guard; only a REINTRODUCED sentinel comparison in real code
    should."""

    def test_place_tiers_source_has_no_sentinel(self) -> None:
        source = _executable_source(_place_tiers)
        self.assertNotIn(
            _SENTINEL_TOKEN,
            source,
            msg=(
                "control_loop._place_tiers (which encloses the nested "
                "_journal_tier) must dispatch placement geometry via the "
                "cached ExitPolicy (exit_policy.applies_geometry), never via "
                'a raw `_exit_policy() != "setup_static"` / `== "setup_static"` '
                "env-sentinel comparison in EXECUTABLE code — adversarial-review "
                "guard, Task 6."
            ),
        )

    def test_placement_journal_helpers_have_no_sentinel(self) -> None:
        for helper in _PLACEMENT_JOURNAL_HELPERS:
            with self.subTest(helper=helper.__name__):
                self.assertNotIn(
                    _SENTINEL_TOKEN,
                    _executable_source(helper),
                    msg=(
                        f"control_loop.{helper.__name__} is on the placement path "
                        "extracted out of _place_tiers; it must never compare a raw "
                        '"setup_static" env sentinel — it takes the already-resolved '
                        "decision as an argument."
                    ),
                )

    def test_maybe_reanchor_source_has_no_sentinel(self) -> None:
        source = _executable_source(_maybe_reanchor)
        self.assertNotIn(
            _SENTINEL_TOKEN,
            source,
            msg=(
                "position_manager._maybe_reanchor must gate the reanchor "
                "arm via the cached ExitPolicy (policy.decide_reanchor "
                "returning None for the inert policy), never via a raw "
                '`_exit_policy() != "setup_static"` / `== "setup_static"` '
                "env-sentinel comparison in EXECUTABLE code — adversarial-review "
                "guard, Task 6."
            ),
        )


class NoHotPathExitPolicyResolveSurvivesTheRefactor(unittest.TestCase):
    """Pins invariant 2 — the per-tick protection + placement-journal paths
    never call ``resolve_exit_policy(...)`` or the raw ``_exit_policy()`` env
    read; they consume the ONE cached ``ExitPolicy`` resolved at startup by
    ``build_default_deps``. This is the adversarial-review guard: repeatedly
    re-resolving the registry (or re-reading the env var) per tick would
    reintroduce a mid-session flip hazard the resolve-once design exists to
    close."""

    def test_place_tiers_never_resolves_on_the_hot_path(self) -> None:
        source = _executable_source(_place_tiers)
        self.assertNotIn(
            _HOT_PATH_RESOLVE_CALL,
            source,
            msg=(
                "control_loop._place_tiers (encloses _journal_tier) must "
                "consume the exit_policy argument passed in from the cached "
                "LoopDeps, never call resolve_exit_policy(...) itself — "
                "build_default_deps is the only allowed resolve site."
            ),
        )
        self.assertNotIn(
            _HOT_PATH_RAW_ENV_READ_CALL,
            source,
            msg=(
                "control_loop._place_tiers (encloses _journal_tier) must "
                "not call the raw _exit_policy() env read on the per-tick "
                "placement path — build_default_deps is the only allowed "
                "resolve site."
            ),
        )

    def test_placement_journal_helpers_never_resolve_on_the_hot_path(self) -> None:
        for helper in _PLACEMENT_JOURNAL_HELPERS:
            with self.subTest(helper=helper.__name__):
                source = _executable_source(helper)
                self.assertNotIn(_HOT_PATH_RESOLVE_CALL, source)
                self.assertNotIn(
                    _HOT_PATH_RAW_ENV_READ_CALL,
                    source,
                    msg=(
                        f"control_loop.{helper.__name__} runs per placed pick; it "
                        "must consume the decision passed down from the cached "
                        "LoopDeps policy, never resolve or re-read it."
                    ),
                )

    def test_maybe_reanchor_never_resolves_on_the_hot_path(self) -> None:
        source = _executable_source(_maybe_reanchor)
        self.assertNotIn(
            _HOT_PATH_RESOLVE_CALL,
            source,
            msg=(
                "position_manager._maybe_reanchor must consume "
                "view.exit_policy (threaded from the cached LoopDeps), "
                "never call resolve_exit_policy(...) itself."
            ),
        )
        self.assertNotIn(
            _HOT_PATH_RAW_ENV_READ_CALL,
            source,
            msg=(
                "position_manager._maybe_reanchor must not call the raw "
                "_exit_policy() env read on the per-tick reanchor path."
            ),
        )

    def test_reconcile_long_never_resolves_on_the_hot_path(self) -> None:
        source = _executable_source(_reconcile_long)
        self.assertNotIn(
            _HOT_PATH_RESOLVE_CALL,
            source,
            msg=(
                "position_manager._reconcile_long must consume "
                "view.exit_policy, never call resolve_exit_policy(...) "
                "itself on the per-tick protection path."
            ),
        )
        self.assertNotIn(
            _HOT_PATH_RAW_ENV_READ_CALL,
            source,
            msg=(
                "position_manager._reconcile_long must not call the raw "
                "_exit_policy() env read on the per-tick protection path."
            ),
        )

    def test_reconcile_protection_never_resolves_on_the_hot_path(self) -> None:
        source = _executable_source(reconcile_protection)
        self.assertNotIn(
            _HOT_PATH_RESOLVE_CALL,
            source,
            msg=(
                "position_manager.reconcile_protection must consume "
                "view.exit_policy, never call resolve_exit_policy(...) "
                "itself on the per-tick protection entrypoint."
            ),
        )
        self.assertNotIn(
            _HOT_PATH_RAW_ENV_READ_CALL,
            source,
            msg=(
                "position_manager.reconcile_protection must not call the "
                "raw _exit_policy() env read on the per-tick protection "
                "entrypoint."
            ),
        )


if __name__ == "__main__":
    unittest.main()
