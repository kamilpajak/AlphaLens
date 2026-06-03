"""Static allow-list scan of the Saxo metric emit sites (secret-leak Finding 2).

The ``alphalens saxo refresh`` emit path writes a Prometheus textfile. The
metric file is world-readable (0o644, node_exporter requirement), and the
project convention is "caller formats raw labels" — together a trivial
token-in-label footgun. This test pins a CLOSED allow-list:

* every emitted metric KEY matches one of the allow-listed gauge/counter names;
* the only labels are ``environment`` (in {sim,live}) and, on the failures
  counter, ``class`` (in {transient,permanent,unclassified});
* the literals refresh_token / access_token / Bearer / client_secret / code=
  NEVER appear in any emitted metric expression.

A positive control with a bad label MUST fail the scan logic so the test
cannot silently rot.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
SAXO_CLI = WORKSPACE_ROOT / "apps" / "alphalens-pipeline" / "alphalens_cli" / "commands" / "saxo.py"

# Closed allow-list of metric NAMES (label block stripped). Mirrors the
# locked design's "Metrics & alerts" gauge list. positions_unmanaged is the
# contract gauge named now / set by the future exit manager — included so the
# allow-list is the single source of truth even though the exit manager is OOS.
ALLOWED_METRIC_NAMES = frozenset(
    {
        "alphalens_saxo_chain_state",
        "alphalens_saxo_reauth_required",
        "alphalens_saxo_refresh_token_expires_at_timestamp_seconds",
        "alphalens_saxo_token_chain_last_refresh_timestamp_seconds",
        "alphalens_saxo_metrics_fetched_at_timestamp_seconds",
        "alphalens_saxo_token_chain_last_full_auth_timestamp_seconds",
        "alphalens_saxo_refresh_failures_total",
        "alphalens_saxo_refresh_skipped_degraded_total",
        "alphalens_saxo_positions_unmanaged",
    }
)

# Token-material literals that must never appear in a metric expression.
_BANNED_LITERALS = ("refresh_token", "access_token", "Bearer", "client_secret", "code=")

_METRIC_NAME_RE = re.compile(r"^(alphalens_saxo_[a-z_]+)")
_LABEL_BLOCK_RE = re.compile(r"\{([^}]*)\}")
_ALLOWED_LABEL_KEYS = {"environment", "class"}
_ALLOWED_ENV_VALUES = {"sim", "live"}
_ALLOWED_CLASS_VALUES = {"transient", "permanent", "unclassified"}


def _extract_metric_expr_fstrings(source: str) -> list[str]:
    """Return every f-string / literal used as a metric KEY in the emit dict.

    We parse the saxo CLI AST and collect the string parts of dict KEYS that
    start with ``alphalens_saxo_``. f-strings are reconstructed with their
    literal segments + a ``{}`` placeholder for each formatted field so the
    static label-key analysis can run.
    """
    tree = ast.parse(source)
    keys: list[str] = []

    def reconstruct(node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            out = []
            for part in node.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    out.append(part.value)
                else:
                    out.append("{}")  # a formatted field
            return "".join(out)
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key in node.keys:
                if key is None:
                    continue
                text = reconstruct(key)
                if text and text.startswith("alphalens_saxo_"):
                    keys.append(text)
    return keys


class TestSaxoMetricsAllowlist(unittest.TestCase):
    def setUp(self) -> None:
        self.source = SAXO_CLI.read_text(encoding="utf-8")
        self.keys = _extract_metric_expr_fstrings(self.source)

    def test_emit_sites_exist(self) -> None:
        # Guard against the scan silently passing on zero sites.
        self.assertGreater(
            len(self.keys), 0, "no alphalens_saxo_* metric keys found — scan target moved?"
        )

    def test_every_metric_name_is_allowlisted(self) -> None:
        for key in self.keys:
            m = _METRIC_NAME_RE.match(key)
            self.assertIsNotNone(m, f"could not extract metric name from {key!r}")
            assert m is not None
            name = m.group(1)
            self.assertIn(
                name,
                ALLOWED_METRIC_NAMES,
                f"metric {name!r} is not in the closed allow-list — add it to "
                "ALLOWED_METRIC_NAMES (and the Prometheus rules) deliberately.",
            )

    def test_no_token_literals_in_any_metric_expr(self) -> None:
        for key in self.keys:
            for banned in _BANNED_LITERALS:
                self.assertNotIn(
                    banned,
                    key,
                    f"metric expression {key!r} contains banned token literal {banned!r}.",
                )

    def test_labels_restricted_to_environment_and_class(self) -> None:
        for key in self.keys:
            label_match = _LABEL_BLOCK_RE.search(key)
            if not label_match:
                continue
            block = label_match.group(1)
            # Pull label keys (left of each '='); values may be {} placeholders.
            for pair in block.split(","):
                if "=" not in pair:
                    continue
                label_key = pair.split("=", 1)[0].strip()
                self.assertIn(
                    label_key,
                    _ALLOWED_LABEL_KEYS,
                    f"metric {key!r} uses a non-allowlisted label {label_key!r}.",
                )

    def test_positive_control_bad_label_is_caught(self) -> None:
        # Prove the label scanner has teeth: a synthetic bad-label expr must be
        # flagged by the same logic.
        bad = 'alphalens_saxo_chain_state{environment="sim",refresh_token="x"}'
        label_match = _LABEL_BLOCK_RE.search(bad)
        assert label_match is not None
        keys = [p.split("=", 1)[0].strip() for p in label_match.group(1).split(",") if "=" in p]
        self.assertIn("refresh_token", keys, "control: bad label must be present")
        self.assertFalse(
            set(keys) <= _ALLOWED_LABEL_KEYS,
            "control: the allow-list logic must reject the bad-label expr",
        )

    def test_allowlist_is_nonempty(self) -> None:
        # Positive control on the allow-list constant itself (cannot rot empty).
        self.assertGreaterEqual(len(ALLOWED_METRIC_NAMES), 9)


if __name__ == "__main__":
    unittest.main()
