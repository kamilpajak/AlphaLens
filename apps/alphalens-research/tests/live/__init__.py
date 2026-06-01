"""L4 live vendor probes (test-strategy Phase 5). Opt-in, env-gated, shape-only.

Each probe runs against a REAL vendor on a representative day and asserts
shape + non-emptiness only — never values. This is the ONLY test layer that
catches the real-data-shape class (the EX-99.1 fixture that was fabricated
wrong, incident #2/#332->#338) and the silent model-retirement 404 (#3): no
hermetic test can, because every hermetic test asserts our assumptions against
our own mocks.

Generalises the proven ``GDELT_LIVE_TEST`` pattern
(``apps/alphalens-research/tests/thematic/test_gdelt_live.py``): one env flag
per vendor, permanent-vs-transient classification, a >50% majority-success
gate. ``run_probes`` below is the shared classifier those four probe modules
call so the GDELT block is not copy-pasted four times.

NEVER in the blocking PR path: the default ``unittest discover`` collects the
probe modules but ``@skipUnless`` skips them (no flag set). They run via
``just probe-live`` or per vendor, e.g.::

    SEC_LIVE_TEST=1 .venv/bin/python -m unittest tests.live.test_sec_live -v

and in the weekly scheduled CI ``live-probes`` job (opens a GitHub issue on
failure — never blocks a merge).

Classification contract (identical across all four probes):
  PERMANENT  shape violation / 404 / empty body / malformed JSON -> FAIL now
  TRANSIENT  429 / timeout / connection reset -> tolerated unless it dominates
"""

from __future__ import annotations

import unittest
from collections.abc import Callable
from dataclasses import dataclass, field


class PermanentProbeError(AssertionError):
    """A shape / 404 / empty failure — the probe's reason to FAIL.

    Subclasses ``AssertionError`` so it reads as a test failure (not an
    error) in unittest output, matching how a shape break should surface.
    """


class TransientProbeError(Exception):
    """A rate-limit / network failure — tolerated unless it dominates the run.

    A single 429 or timeout proves nothing about the vendor's contract, so it
    is collected and warned about; only a >50% transient rate fails the probe
    (the data is too degraded to trust either way).
    """


@dataclass
class ProbeOutcome:
    """The tri-state tally of a probe run (mirrors the GDELT live smoke)."""

    ok: list[str] = field(default_factory=list)
    transient: list[tuple[str, str]] = field(default_factory=list)
    permanent: list[tuple[str, str]] = field(default_factory=list)


def run_probes(
    case: unittest.TestCase,
    items: dict[str, Callable[[], None]],
    *,
    label: str,
) -> ProbeOutcome:
    """Run each named probe callable, classify failures, then assert.

    A callable that raises :class:`PermanentProbeError` records a permanent
    failure; :class:`TransientProbeError` records a transient one; any OTHER
    exception is re-classified as permanent — an unexpected break is a real
    failure, not a flake (the unit suite already mocks away the expected
    paths; a surprise here is exactly what this layer exists to surface).
    A callable that returns normally records a success.

    Asserts (mirrors ``test_gdelt_live.py`` exactly):
      1. ``permanent == []``            — any shape / 404 / empty FAILS.
      2. ``len(ok) >= len(items) // 2`` — a majority must round-trip; if
         transient failures dominate, the vendor / network is too degraded
         for the probe to prove anything, so it fails loudly rather than
         passing on near-empty data.

    Single-item probes (e.g. SEC, Polygon) have ``len(items) // 2 == 0``, so a
    lone TRANSIENT failure passes (``0 >= 0``). That is deliberate: on a WEEKLY
    schedule a single 429 / timeout is inconclusive, not a signal — paging on
    every transient network blip is the flaky-red the strategy memo warns
    against. A genuine shape break still raises PermanentProbeError, which fails
    via gate (1) regardless of item count.
    """
    out = ProbeOutcome()
    for name, fn in items.items():
        try:
            fn()
            out.ok.append(name)
        except PermanentProbeError as exc:
            out.permanent.append((name, str(exc)))
        except TransientProbeError as exc:
            out.transient.append((name, str(exc)))
        except Exception as exc:  # an unexpected break IS a real failure, not a flake
            out.permanent.append((name, f"unexpected {type(exc).__name__}: {exc}"))

    print(
        f"\n[{label} live probe] ok={len(out.ok)} "
        f"transient={len(out.transient)} permanent={len(out.permanent)} "
        f"(total={len(items)})"
    )
    for n, m in out.transient:
        print(f"  TRANSIENT {n}: {m}")
    for n, m in out.permanent:
        print(f"  PERMANENT {n}: {m}")

    case.assertEqual(
        out.permanent,
        [],
        f"{label} live probe caught permanent (shape/404/empty) failures: {out.permanent}",
    )
    case.assertGreaterEqual(
        len(out.ok),
        len(items) // 2,
        f"Only {len(out.ok)}/{len(items)} {label} probes round-tripped — transient "
        f"failures dominate ({out.transient}); re-run after a cool-down before "
        f"trusting this probe.",
    )
    return out


__all__ = [
    "PermanentProbeError",
    "ProbeOutcome",
    "TransientProbeError",
    "run_probes",
]
