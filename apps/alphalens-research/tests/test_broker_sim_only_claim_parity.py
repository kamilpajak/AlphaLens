"""Doc-parity enforcement: no unqualified blanket "SIM-only" claims in the
broker layer.

Since ADR 0015 (keyed day-bound unlock) and ADR 0017 (standing account-bound
grant + LIVE factory), LIVE is structurally REACHABLE from the broker layer —
a blanket "SIM-only" claim in a docstring/comment that does not acknowledge
the unlock misdescribes reachability, which is exactly the pre-sweep wording
this test locks out. TRUE narrow claims survive (the streaming reader, the
``SaxoAuthClient`` rail, and the SIM OAuth chain genuinely stay SIM-only
post-ADR 0017; "common SIM-only path" comments describe import graphs, not
reachability) — via the qualifier window or the curated allowlist below.

Heuristic (documented precisely so a future edit can reason about it):

* a line matches when it contains ``SIM-only`` / ``SIM only`` (any case);
* a match is QUALIFIED — and therefore fine — when ``ADR 0015``, ``ADR
  0017``, ``unlock``, or ``structural`` (any case, ``structurally`` counts)
  appears within ±:data:`QUALIFIER_WINDOW_LINES` lines of it;
* an unqualified match is an offender unless ``(rel_path, fragment)`` is in
  :data:`ALLOWLIST` — every entry there must stay LOAD-BEARING (it must still
  match an unqualified line in its file, enforced below), so rewording or
  deleting a claim forces the entry's removal and the allowlist cannot rot.

Scope: the one-off CLI command module plus the whole broker layer
(``alphalens_cli/commands/broker.py`` + ``alphalens_pipeline/brokers/**``).

Mirror of the per-vendor ``test_no_raw_*_http`` shape: positive control on
the checker itself, allowlist-exists (+ load-bearing) pins, then the tree
scan.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[3]

_CLI_BROKER_REL = "apps/alphalens-pipeline/alphalens_cli/commands/broker.py"
_BROKERS_PKG_REL = "apps/alphalens-pipeline/alphalens_pipeline/brokers"

SIM_ONLY_PATTERN = re.compile(r"\bSIM[- ]only\b", re.IGNORECASE)

# Any of these within the window qualifies the claim (lower-cased compare, so
# "structurally"/"Unlock" count).
QUALIFIER_MARKERS = ("adr 0015", "adr 0017", "unlock", "structural")
QUALIFIER_WINDOW_LINES = 3

# TRUE narrow claims that carry no qualifier within the window. Each entry is
# (rel_path, fragment): the fragment must be a substring of the offending
# line. Load-bearing check below forces removal once the line is reworded.
ALLOWLIST: tuple[tuple[str, str], ...] = (
    (
        "apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/control_loop.py",
        "Streaming (dark, SIM-only) env gates",
    ),
    (
        "apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/control_loop.py",
        "the common SIM-only path never pulls in the LIVE factory's import graph",
    ),
    (
        "apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/control_loop.py",
        "shared by the SessionKeeper AND the (SIM-only)",
    ),
    (
        "apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/control_loop.py",
        "Streaming early-wake handles (dark, SIM-only)",
    ),
    (
        "apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/saxo_live_price_feed.py",
        "it lives under ``brokers/``, where the SIM-only rail",
    ),
    (
        "apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/streaming_trigger.py",
        "dark Saxo streaming reader (ADR 0014, SIM-only)",
    ),
    (
        "apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/streaming_trigger.py",
        "Build the real SIM-only streaming client",
    ),
    (
        "apps/alphalens-pipeline/alphalens_pipeline/brokers/saxo/__init__.py",
        "``streaming.py`` is the dark, SIM-only WebSocket reader",
    ),
    (
        "apps/alphalens-pipeline/alphalens_pipeline/brokers/saxo/broker.py",
        "import graph light for the common SIM-only path",
    ),
    (
        "apps/alphalens-pipeline/alphalens_pipeline/brokers/saxo/client.py",
        "remain unconditionally SIM-only",
    ),
    (
        "apps/alphalens-pipeline/alphalens_pipeline/brokers/saxo/client.py",
        "the ``SAXO_ENV`` sim-only guard stays FIRST",
    ),
    (
        "apps/alphalens-pipeline/alphalens_pipeline/brokers/saxo/client.py",
        "the SIM-only rail, Bearer discipline, shared throttle",
    ),
    (
        "apps/alphalens-pipeline/alphalens_pipeline/brokers/saxo/oauth.py",
        "SaxoAuthClient is SIM-only: auth_base_url must be",
    ),
    (
        "apps/alphalens-pipeline/alphalens_pipeline/brokers/saxo/streaming.py",
        "Saxo SIM WebSocket streaming reader (dark, SIM-only",
    ),
    (
        "apps/alphalens-pipeline/alphalens_pipeline/brokers/saxo/streaming.py",
        "SaxoStreamingClient is SIM-only: streaming_base_url must be",
    ),
    (
        "apps/alphalens-pipeline/alphalens_pipeline/brokers/saxo/streaming.py",
        "SIM-only Saxo WebSocket reader. DI-clean",
    ),
)


def _scan_files() -> list[Path]:
    files = [WORKSPACE_ROOT / _CLI_BROKER_REL]
    files.extend(sorted((WORKSPACE_ROOT / _BROKERS_PKG_REL).rglob("*.py")))
    return files


def _find_unqualified_sim_only_lines(text: str) -> list[tuple[int, str]]:
    """Return (lineno, line) for every SIM-only mention with NO qualifier
    marker within ±QUALIFIER_WINDOW_LINES lines."""
    lines = text.splitlines()
    hits: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        if not SIM_ONLY_PATTERN.search(line):
            continue
        lo = max(0, index - QUALIFIER_WINDOW_LINES)
        window = "\n".join(lines[lo : index + QUALIFIER_WINDOW_LINES + 1]).lower()
        if any(marker in window for marker in QUALIFIER_MARKERS):
            continue
        hits.append((index + 1, line.rstrip()))
    return hits


def _is_allowlisted(rel_path: str, line: str) -> bool:
    return any(rel == rel_path and fragment in line for rel, fragment in ALLOWLIST)


class TestSimOnlyClaimParity(unittest.TestCase):
    def test_detection_heuristic_locks_blanket_claims(self):
        """Positive control on the checker itself: each pre-sweep wording we
        MEAN to catch is flagged, and qualified/narrow shapes are NOT — so
        the pattern / marker lists cannot rot to empty silently."""
        blanket_samples = [
            # The pre-sweep daemon-doc wording (blanket reachability claim).
            "**SIM-only**; placement gated on ALPHALENS_BROKER_ALLOW_ORDERS=1.",
            # The pre-sweep `broker auth` docstring first line.
            '"""Bootstrap or inspect the Saxo OAuth session (SIM-only, Code grant).',
            "the broker layer is SIM only and can never reach LIVE",
        ]
        for sample in blanket_samples:
            hits = _find_unqualified_sim_only_lines(sample)
            self.assertEqual(
                len(hits), 1, f"expected exactly one hit on blanket sample: {sample!r}"
            )

        qualified_samples = [
            "SIM-only structural rail (ADR 0014, narrowed by ADR 0015 and ADR 0017):",
            "stays SIM-only\n\n\n(unchanged by the ADR 0017 grant)",
            "orders remain SIM-only unless the keyed unlock is presented",
            "the registry stays structurally SIM-only",
            "the daemon places brackets on the configured gateway",  # no mention at all
        ]
        for sample in qualified_samples:
            hits = _find_unqualified_sim_only_lines(sample)
            self.assertEqual(
                len(hits), 0, f"expected zero hits on qualified sample: {sample!r} ({hits})"
            )

        # Window boundary: a marker MORE than QUALIFIER_WINDOW_LINES away must
        # not qualify the claim.
        far_marker = "stays SIM-only for orders" + "\n" * (QUALIFIER_WINDOW_LINES + 1) + "ADR 0017"
        self.assertEqual(len(_find_unqualified_sim_only_lines(far_marker)), 1)

    def test_allowlist_entries_exist_and_are_load_bearing(self):
        """Every allowlist entry must point at a real file AND still match an
        unqualified SIM-only line there — a reworded/removed claim forces the
        entry's deletion, so the allowlist cannot rot into a blanket pass."""
        for rel, fragment in ALLOWLIST:
            with self.subTest(rel=rel, fragment=fragment):
                path = WORKSPACE_ROOT / rel
                self.assertTrue(path.is_file(), f"allowlisted file missing: {rel}")
                text = path.read_text(encoding="utf-8")
                matching = [
                    line for _, line in _find_unqualified_sim_only_lines(text) if fragment in line
                ]
                self.assertTrue(
                    matching,
                    f"allowlist entry no longer matches an unqualified line in {rel}: "
                    f"{fragment!r} — the claim was reworded or removed; delete the entry.",
                )

    def test_no_unqualified_blanket_sim_only_claims(self):
        offenders: list[tuple[str, int, str]] = []
        for py in _scan_files():
            rel = py.relative_to(WORKSPACE_ROOT).as_posix()
            text = py.read_text(encoding="utf-8", errors="replace")
            for lineno, line in _find_unqualified_sim_only_lines(text):
                if _is_allowlisted(rel, line):
                    continue
                offenders.append((rel, lineno, line))

        if offenders:
            details = "\n".join(f"  {p}:{ln}  {src}" for p, ln, src in offenders)
            self.fail(
                "Unqualified blanket 'SIM-only' claim(s) in the broker layer.\n"
                "Since ADR 0015/0017 LIVE is structurally reachable — either\n"
                "qualify the claim (mention ADR 0015/0017/the unlock within "
                f"{QUALIFIER_WINDOW_LINES} lines) or, for a TRUE narrow claim, "
                "add an ALLOWLIST entry.\n"
                f"Offenders:\n{details}"
            )


if __name__ == "__main__":
    unittest.main()
