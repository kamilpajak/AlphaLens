"""Enforce module-direction rules across the AlphaLens workspace.

Two tiers of rules:

1. Intra-research: the ADR 0007 layer DAG (Layer 2 screener → 3 engine →
   4 overlay → 5 attribution) is one-way. backtest must stay screener- and
   attribution-agnostic; screeners must not reach forward into backtest /
   overlays / attribution; overlays must not import attribution; attribution
   (terminal) must not import screeners; gates must not import backtest /
   attribution.

2. Cross-tier (split PR2): ``alphalens_pipeline`` must not import from
   ``alphalens_research`` — the pipeline tier is downstream-free
   infrastructure. The single exemption is the CLI (``alphalens_cli``),
   which orchestrates both tiers via lazy imports inside command bodies
   (the CLI files live in pipeline-side but route into research via
   function-scope imports — see commands/audit.py, preaudit.py,
   preregister.py).

Adding a justified exception requires updating the EXEMPTIONS allowlist
below with a one-line reason — making the trade-off explicit and reviewable.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

# Workspace root = repo top dir (two levels above this test file:
# tests/foo.py → tests/ → apps/alphalens-research/ → apps/ → repo)
WORKSPACE_ROOT = Path(__file__).resolve().parents[3]

# Map from top-level python package name to its workspace member dir.
PACKAGE_DIRS: dict[str, Path] = {
    "alphalens_pipeline": WORKSPACE_ROOT / "apps" / "alphalens-pipeline" / "alphalens_pipeline",
    "alphalens_research": WORKSPACE_ROOT / "apps" / "alphalens-research" / "alphalens_research",
    "alphalens_cli": WORKSPACE_ROOT / "apps" / "alphalens-pipeline" / "alphalens_cli",
    "broker_contract": WORKSPACE_ROOT / "apps" / "alphalens-broker-contract" / "broker_contract",
}

RULES = (
    {
        "name": "backtest must stay screener-agnostic",
        "from_pkg": "alphalens_research.backtest",
        "forbidden_prefix": "alphalens_research.screeners.",
        "exemptions": set(),
    },
    {
        # ADR 0007 + Phase 4 reorg: Layer 3 (engine) produces BacktestReport;
        # Layer 5 (attribution) consumes it. The reverse direction (engine
        # importing attribution metrics, factor regressions, verdict gates)
        # would create a cycle where the engine self-attributes its own output.
        "name": "engine must stay attribution-agnostic (BacktestReport flows L3 -> L5, not back)",
        "from_pkg": "alphalens_research.backtest",
        "forbidden_prefix": "alphalens_research.attribution.",
        "exemptions": set(),
    },
    # ADR 0007 layer DAG (Layer 2 -> 3 -> 4 -> 5). A screener ranks @ time t and
    # is consumed by the engine; it must not reach forward into the engine,
    # overlay, or attribution that sit downstream of it. Three separate rules so
    # a single forbidden_prefix stays exact (a shared prefix would not cover all
    # three sibling packages).
    {
        "name": "screeners must not import backtest (Layer 2 -> 3 is one-way)",
        "from_pkg": "alphalens_research.screeners",
        "forbidden_prefix": "alphalens_research.backtest.",
        "exemptions": set(),
    },
    {
        "name": "screeners must not import overlays (Layer 2 sits upstream of Layer 4)",
        "from_pkg": "alphalens_research.screeners",
        "forbidden_prefix": "alphalens_research.overlays.",
        "exemptions": set(),
    },
    {
        "name": "screeners must not import attribution (Layer 2 sits upstream of Layer 5)",
        "from_pkg": "alphalens_research.screeners",
        "forbidden_prefix": "alphalens_research.attribution.",
        "exemptions": set(),
    },
    {
        # Layer 4 (overlay) resizes portfolio exposure on realised vol; Layer 5
        # (attribution) consumes the overlaid returns. The overlay reaching into
        # attribution would invert the L4 -> L5 direction.
        "name": "overlays must not import attribution (Layer 4 -> 5 is one-way)",
        "from_pkg": "alphalens_research.overlays",
        "forbidden_prefix": "alphalens_research.attribution.",
        "exemptions": set(),
    },
    {
        # Attribution is the terminal consumer (Layer 5). It reads BacktestReport
        # returns, never the screener that produced the picks — that would make
        # the verdict layer depend on a specific Layer 2 implementation.
        "name": "attribution must not import screeners (Layer 5 is terminal)",
        "from_pkg": "alphalens_research.attribution",
        "forbidden_prefix": "alphalens_research.screeners.",
        "exemptions": set(),
    },
    # Layer 2 selection-gate wraps a Scorer and modifies WHICH tickers deploy. It
    # sits between the screener (Layer 2) and the engine (Layer 3); it must not
    # reach forward into the engine or the attribution that sit downstream.
    {
        "name": "gates must not import backtest (gate feeds the engine, not vice versa)",
        "from_pkg": "alphalens_research.gates",
        "forbidden_prefix": "alphalens_research.backtest.",
        "exemptions": set(),
    },
    {
        "name": "gates must not import attribution (gate sits upstream of Layer 5)",
        "from_pkg": "alphalens_research.gates",
        "forbidden_prefix": "alphalens_research.attribution.",
        "exemptions": set(),
    },
    {
        # ADR 0013 R2 via ADR 0014: no broker/execution output (fills,
        # rejections, balances) may ever feed T2 SELECTION. The thematic
        # pipeline (selection side) importing the brokers package — even
        # lazily — would open exactly that channel.
        "name": "thematic must not import brokers (R2: execution never feeds selection)",
        "from_pkg": "alphalens_pipeline.thematic",
        "forbidden_prefix": "alphalens_pipeline.brokers",
        "exemptions": set(),
    },
    {
        # ADR 0012: the feedback replay engines are broker-FREE by design
        # (price-path over Polygon bars). Live fills are a NEW T8 measurement
        # source (ADR 0014), keyed separately — the replay reaching into the
        # brokers package would blur that separation.
        "name": "feedback must not import brokers (replay stays broker-free per ADR 0012)",
        "from_pkg": "alphalens_pipeline.feedback",
        "forbidden_prefix": "alphalens_pipeline.brokers",
        "exemptions": set(),
    },
    {
        # ADR 0014 P2: the broker layer CONSUMES paper/{sizing,calendar,
        # constants} (SetupPlan, GTD calendar math). The reverse direction
        # would cycle the broker-free planner primitives into the execution
        # vendor stack.
        "name": "paper must not import brokers (brokers consumes paper, never the reverse)",
        "from_pkg": "alphalens_pipeline.paper",
        "forbidden_prefix": "alphalens_pipeline.brokers",
        "exemptions": set(),
    },
    {
        # ADR 0014: the automanager reaches concrete saxo ONLY via the broker
        # registry or a lazy import inside a composition-root wiring function
        # (build_default_deps / _default_oauth_provider /
        # _build_streaming_subscriber / _build_stream_handles) — those pass
        # automatically because top_level_only skips function bodies. A
        # top-level saxo import would hard-wire the vendor into the manager.
        "name": "automanager must not import concrete saxo at top level (ADR 0014 wiring)",
        "from_pkg": "alphalens_pipeline.brokers.automanager",
        "forbidden_prefix": "alphalens_pipeline.brokers.saxo",
        "top_level_only": True,
        "exemptions": {
            "streaming_trigger.py"
        },  # anti-rot: drop when the module relocates under brokers/saxo/
    },
    {
        # Workspace split (PR2): the pipeline tier hosts live infrastructure
        # (data, core, scorers, edgar_detector, thematic, literature_scanner) and
        # must remain downstream-free. The research tier consumes pipeline,
        # never the reverse. Direct top-level imports from alphalens_pipeline
        # to alphalens_research would create a workspace-level dependency cycle.
        "name": "alphalens_pipeline must not import from alphalens_research",
        "from_pkg": "alphalens_pipeline",
        "forbidden_prefix": "alphalens_research.",
        "exemptions": set(),
    },
    {
        # Broker-manager extraction, PR-4: execution never reads the replay
        # ledger. The feedback replay engines are a MEASUREMENT tier (ADR
        # 0012); brokers reaching into feedback would let live execution
        # branch on historical replay output, the reverse of the existing
        # "feedback must not import brokers" rule above.
        "name": "brokers must not import feedback (execution never reads the replay ledger)",
        "from_pkg": "alphalens_pipeline.brokers",
        "forbidden_prefix": "alphalens_pipeline.feedback",
        "exemptions": set(),
    },
    {
        # Broker-manager extraction, PR-4: brokers/ depends on the ABSTRACT
        # NotificationPort (brokers/notifications.py); the concrete
        # telegram-backed sink is wired in ONLY at the CLI composition root
        # (alphalens_cli/commands/broker.py, client C). No `top_level_only`
        # here — the telegram imports this rule replaces were lazy
        # (function-scope), so the walker must catch those too.
        "name": "brokers must not import telegram directly (NotificationPort is injected at the CLI root, PR-4)",
        "from_pkg": "alphalens_pipeline.brokers",
        "forbidden_prefix": "alphalens_pipeline.data.alt_data.telegram",
        "exemptions": set(),
    },
    {
        # Broker-manager extraction, PR-7 deleted the daemon-side brief read
        # (``load_brief`` inside ``_place_pick``, V1) + the brief-coupled
        # parse (V2): the daemon now drains a fully-formed TradeIntent off
        # the pick queue and never touches a brief. PR-8 formalizes the
        # tripwire so a regression (a lazy re-import of ``load_brief``
        # inside brokers/ code) fails loudly. No ``top_level_only`` — the
        # deleted V1 read was itself a lazy function-scope import
        # (``alphalens_cli/commands/broker.py`` and
        # ``alphalens_pipeline/feedback/population_ladder_monitor.py`` still
        # import ``brief_loader`` legitimately, from OUTSIDE brokers/, so
        # this rule only ever fires on a brokers-side regression).
        "name": "brokers must not import paper.brief_loader (PR-7 deleted the brief read; PR-8 tripwire)",
        "from_pkg": "alphalens_pipeline.brokers",
        "forbidden_prefix": "alphalens_pipeline.paper.brief_loader",
        "exemptions": set(),
    },
    {
        # Broker-manager extraction memo Revision R2 (operator decision,
        # 2026-07-31): "Earnings gate leaves the manager entirely ... The
        # brokers->thematic coupling is removed by DELETION, not a port."
        # This deleted ``brokers.automanager.earnings_gate`` (the sole
        # ``brokers -> thematic`` coupling, memo cut-table V3) outright. The
        # gate was then relocated to arm-time and, 2026-08-03, REMOVED from
        # the arm CLI too: arm is a pure executor and selection filtering
        # (earnings-window avoidance included) belongs at brief-creation, not
        # in execution tooling. Either way the ``brokers`` package must never
        # import ``thematic`` — a permanent invariant this rule pins. No
        # ``top_level_only`` — the deleted coupling was itself a lazy
        # function-scope import, so the walker must catch that shape too.
        "name": "brokers must not import thematic (V3: earnings-gate deletion is the last brokers->thematic coupling)",
        "from_pkg": "alphalens_pipeline.brokers",
        "forbidden_prefix": "alphalens_pipeline.thematic",
        "exemptions": set(),
    },
    {
        # Broker-manager extraction 2A-2: broker_contract is the shared
        # A-tier leaf (exit_geometry, trade_intent) consumed by BOTH
        # alphalens_pipeline and alphalens_research. No `top_level_only` —
        # a pure published leaf must not import either consumer even
        # lazily, unlike the pipeline<->research cross-tier rule above.
        "name": "broker_contract must not import from alphalens_pipeline",
        "from_pkg": "broker_contract",
        "forbidden_prefix": "alphalens_pipeline",
        "exemptions": set(),
    },
    {
        # Broker-manager extraction 2A-2: same rationale, opposite consumer.
        "name": "broker_contract must not import from alphalens_research",
        "from_pkg": "broker_contract",
        "forbidden_prefix": "alphalens_research",
        "exemptions": set(),
    },
)


def _iter_imports(path: Path, *, include_function_scope: bool):
    """Yield every imported module name in ``path``.

    Covers both ``import X`` and ``from X import Y`` shapes (using ``ast.Import``
    + ``ast.ImportFrom`` respectively). Walks into all non-function nodes so
    forbidden imports nested in ``if TYPE_CHECKING:`` / ``try`` / ``with`` blocks
    are caught — the rule should hold module-import-time regardless of
    surrounding control flow.

    If ``include_function_scope`` is False, skip imports inside function /
    method / lambda bodies (the documented lazy-CLI pattern). Otherwise all
    imports, including lazy ones, are emitted.
    """
    tree = ast.parse(path.read_text(), filename=str(path))

    class _ImportCollector(ast.NodeVisitor):
        def __init__(self) -> None:
            self.modules: list[str] = []

        def visit_Import(self, node: ast.Import) -> None:
            for alias in node.names:
                if alias.name:
                    self.modules.append(alias.name)
            self.generic_visit(node)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            if node.module:
                self.modules.append(node.module)
            self.generic_visit(node)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            if include_function_scope:
                self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            if include_function_scope:
                self.generic_visit(node)

        def visit_Lambda(self, node: ast.Lambda) -> None:
            # Lambdas can't contain import statements; no-op for symmetry.
            return

    collector = _ImportCollector()
    collector.visit(tree)
    yield from collector.modules


def _python_files(pkg_dir: Path):
    return sorted(p for p in pkg_dir.rglob("*.py") if p.name != "__pycache__")


def _resolve_pkg_dir(from_pkg: str) -> Path:
    """Map ``alphalens_research.backtest`` → its on-disk directory."""
    parts = from_pkg.split(".")
    base = PACKAGE_DIRS.get(parts[0])
    if base is None:
        raise KeyError(f"unknown top-level package in rule: {parts[0]}")
    return base.joinpath(*parts[1:]) if len(parts) > 1 else base


class TestModuleDependencies(unittest.TestCase):
    def test_rules(self):
        violations: list[tuple[str, str, str]] = []
        for rule in RULES:
            pkg_dir = _resolve_pkg_dir(rule["from_pkg"])
            # Cross-tier pipeline rule: skip function-scope imports because the
            # CLI is allowed to lazy-import the research tier inside command
            # bodies (see module docstring).
            top_level_only = (
                rule.get("top_level_only", False) or rule["from_pkg"] == "alphalens_pipeline"
            )
            for path in _python_files(pkg_dir):
                rel = str(path.relative_to(WORKSPACE_ROOT))
                # Exemptions are keyed by BASENAME intentionally: the packages these
                # rules scan are flat, so a basename is unambiguous, and it keeps the
                # RULES entries readable (bare filename, not a workspace-relative path).
                # If a scanned package ever grows subdirectories, switch this + the
                # anti-rot check in test_exemptions_still_exist to relative paths.
                if path.name in rule["exemptions"]:
                    continue
                for module in _iter_imports(path, include_function_scope=not top_level_only):
                    if module.startswith(rule["forbidden_prefix"]):
                        violations.append((rule["name"], rel, module))

        self.assertEqual(
            violations,
            [],
            "module dependency violations:\n  "
            + "\n  ".join(f"[{r}] {f}: {m}" for r, f, m in violations),
        )

    def test_brokers_rules_positive_control(self):
        """The brokers-direction rules cannot rot silently.

        Feeds the SAME walker a synthetic module that hides a brokers import
        inside a function body (the sneakiest allowed-elsewhere shape) and
        asserts (1) the walker surfaces it and (2) every brokers rule's
        ``forbidden_prefix`` actually matches it — so neither the AST walk
        nor the prefix strings can drift to a never-firing state.
        """
        import tempfile

        synthetic = (
            "def sneaky():\n"
            "    from alphalens_pipeline.brokers.registry import get_default_broker\n"
            "    return get_default_broker()\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "synthetic_violation.py"
            path.write_text(synthetic)
            modules = list(_iter_imports(path, include_function_scope=True))

        self.assertIn("alphalens_pipeline.brokers.registry", modules)
        brokers_rules = [
            rule for rule in RULES if rule["forbidden_prefix"] == "alphalens_pipeline.brokers"
        ]
        self.assertGreaterEqual(
            len(brokers_rules), 3, "thematic + feedback + paper brokers rules must all exist"
        )
        for rule in brokers_rules:
            with self.subTest(rule=rule["name"]):
                self.assertTrue(
                    any(m.startswith(rule["forbidden_prefix"]) for m in modules),
                    f"rule {rule['name']!r} would not catch the synthetic violation",
                )

    def test_brokers_egress_rules_positive_control(self):
        """PR-4 (NotificationPort): brokers must not import feedback (execution
        never reads the replay ledger) or telegram directly (the sink is
        injected at the CLI composition root). Both telegram imports the
        old code carried were LAZY (function-scope), so this positive control
        MUST run the walker with ``include_function_scope=True`` — a
        ``top_level_only`` rule would silently never catch a regression here.
        """
        import tempfile

        feedback_synthetic = (
            "def sneaky():\n"
            "    from alphalens_pipeline.feedback.shadow_returns import replay\n"
            "    return replay\n"
        )
        telegram_synthetic = (
            "def sneaky():\n"
            "    from alphalens_pipeline.data.alt_data.telegram_client import TelegramClient\n"
            "    return TelegramClient\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            feedback_path = Path(tmp) / "synthetic_feedback_violation.py"
            feedback_path.write_text(feedback_synthetic)
            telegram_path = Path(tmp) / "synthetic_telegram_violation.py"
            telegram_path.write_text(telegram_synthetic)

            feedback_modules = list(_iter_imports(feedback_path, include_function_scope=True))
            telegram_modules = list(_iter_imports(telegram_path, include_function_scope=True))

        self.assertIn("alphalens_pipeline.feedback.shadow_returns", feedback_modules)
        self.assertIn("alphalens_pipeline.data.alt_data.telegram_client", telegram_modules)

        feedback_rules = [
            rule
            for rule in RULES
            if rule["from_pkg"] == "alphalens_pipeline.brokers"
            and rule["forbidden_prefix"] == "alphalens_pipeline.feedback"
        ]
        telegram_rules = [
            rule
            for rule in RULES
            if rule["from_pkg"] == "alphalens_pipeline.brokers"
            and rule["forbidden_prefix"] == "alphalens_pipeline.data.alt_data.telegram"
        ]
        self.assertEqual(len(feedback_rules), 1, "the brokers -> feedback rule must exist once")
        self.assertEqual(len(telegram_rules), 1, "the brokers -> telegram rule must exist once")
        self.assertNotIn(
            "top_level_only",
            feedback_rules[0],
            "the brokers -> feedback rule must catch function-scope imports too",
        )
        self.assertNotIn(
            "top_level_only",
            telegram_rules[0],
            "the brokers -> telegram rule must catch function-scope (lazy) imports too",
        )
        for rule in (*feedback_rules, *telegram_rules):
            with self.subTest(rule=rule["name"]):
                modules = feedback_modules if rule is feedback_rules[0] else telegram_modules
                self.assertTrue(
                    any(m.startswith(rule["forbidden_prefix"]) for m in modules),
                    f"rule {rule['name']!r} would not catch the synthetic violation",
                )

    def test_brokers_brief_loader_tripwire_positive_control(self):
        """PR-7 deleted the daemon-side ``load_brief`` read; PR-8 pins the
        tripwire so a regression (a lazy re-import inside brokers/ code, the
        exact shape the deleted V1 read used) is caught. Mirrors
        ``test_brokers_egress_rules_positive_control`` — a single rule, a
        function-scope synthetic import, ``include_function_scope=True``
        (the walker must catch lazy imports too, since that's the shape that
        was deleted).
        """
        import tempfile

        synthetic = (
            "def sneaky():\n"
            "    from alphalens_pipeline.paper.brief_loader import load_brief\n"
            "    return load_brief\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "synthetic_brief_loader_violation.py"
            path.write_text(synthetic)
            modules = list(_iter_imports(path, include_function_scope=True))

        self.assertIn("alphalens_pipeline.paper.brief_loader", modules)

        brief_loader_rules = [
            rule
            for rule in RULES
            if rule["from_pkg"] == "alphalens_pipeline.brokers"
            and rule["forbidden_prefix"] == "alphalens_pipeline.paper.brief_loader"
        ]
        self.assertEqual(
            len(brief_loader_rules), 1, "the brokers -> paper.brief_loader rule must exist once"
        )
        self.assertNotIn(
            "top_level_only",
            brief_loader_rules[0],
            "the brokers -> paper.brief_loader rule must catch function-scope (lazy) imports too",
        )
        self.assertTrue(
            any(m.startswith(brief_loader_rules[0]["forbidden_prefix"]) for m in modules),
            "rule would not catch the synthetic violation",
        )

    def test_brokers_thematic_tripwire_positive_control(self):
        """Earnings-deletion (memo Revision R2) removed the last
        ``brokers -> thematic`` coupling (``earnings_gate`` lazy-imported
        ``thematic.sources.earnings_calendar``). Pins the tripwire so a
        regression (a lazy re-import inside brokers/ code, the exact shape
        the deleted gate used) is caught. Mirrors
        ``test_brokers_brief_loader_tripwire_positive_control`` — a single
        rule, a function-scope synthetic import, ``include_function_scope=True``
        (the deleted coupling was itself lazy, so the walker must catch that
        shape too).
        """
        import tempfile

        synthetic = (
            "def sneaky():\n"
            "    from alphalens_pipeline.thematic.sources.earnings_calendar import "
            "fetch_next_earnings\n"
            "    return fetch_next_earnings\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "synthetic_thematic_violation.py"
            path.write_text(synthetic)
            modules = list(_iter_imports(path, include_function_scope=True))

        self.assertIn("alphalens_pipeline.thematic.sources.earnings_calendar", modules)

        thematic_rules = [
            rule
            for rule in RULES
            if rule["from_pkg"] == "alphalens_pipeline.brokers"
            and rule["forbidden_prefix"] == "alphalens_pipeline.thematic"
        ]
        self.assertEqual(len(thematic_rules), 1, "the brokers -> thematic rule must exist once")
        self.assertNotIn(
            "top_level_only",
            thematic_rules[0],
            "the brokers -> thematic rule must catch function-scope (lazy) imports too",
        )
        self.assertTrue(
            any(m.startswith(thematic_rules[0]["forbidden_prefix"]) for m in modules),
            "rule would not catch the synthetic violation",
        )

    def test_automanager_saxo_boundary_positive_control(self):
        """The automanager -> saxo boundary rule cannot rot silently.

        Pins BOTH arms of the ADR-0014 wiring contract:
          - a TOP-LEVEL ``from alphalens_pipeline.brokers.saxo...`` import IS
            surfaced when function scope is excluded (the forbidden shape);
          - the SAME import INSIDE a def body is NOT surfaced (the allowed
            composition-root lazy-wiring shape — build_default_deps /
            _default_oauth_provider / _build_streaming_subscriber /
            _build_stream_handles pass because ``top_level_only`` skips bodies).
        Also pins that the streaming_trigger.py file-level exemption survives.
        """
        import tempfile

        forbidden_prefix = "alphalens_pipeline.brokers.saxo"

        top_level = (
            "from alphalens_pipeline.brokers.saxo.errors import SaxoAuthError\nSaxoAuthError\n"
        )
        lazy = (
            "def _wire():\n"
            "    from alphalens_pipeline.brokers.saxo.errors import SaxoAuthError\n"
            "    return SaxoAuthError\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            top_path = Path(tmp) / "top_level_violation.py"
            top_path.write_text(top_level)
            lazy_path = Path(tmp) / "lazy_wiring.py"
            lazy_path.write_text(lazy)

            top_modules = list(_iter_imports(top_path, include_function_scope=False))
            lazy_modules = list(_iter_imports(lazy_path, include_function_scope=False))

        self.assertTrue(
            any(m.startswith(forbidden_prefix) for m in top_modules),
            "top-level saxo import must be flagged by the boundary walker",
        )
        self.assertFalse(
            any(m.startswith(forbidden_prefix) for m in lazy_modules),
            "lazy-wiring saxo import inside a def body must NOT be flagged",
        )

        automanager_rules = [
            rule
            for rule in RULES
            if rule["from_pkg"] == "alphalens_pipeline.brokers.automanager"
            and rule["forbidden_prefix"] == forbidden_prefix
        ]
        self.assertEqual(
            len(automanager_rules),
            1,
            "the automanager -> saxo boundary rule must exist exactly once",
        )
        self.assertIn("streaming_trigger.py", automanager_rules[0]["exemptions"])

    def test_broker_contract_leaf_positive_control(self):
        """The broker_contract leaf rules cannot rot silently.

        Feeds the walker a synthetic module that imports both consumers
        (``alphalens_pipeline.something`` and ``alphalens_research.something``)
        at top level and asserts (a) the walker surfaces both, (b) exactly
        one rule exists per forbidden_prefix, (c) each rule matches the
        synthetic import — so neither rule can drift to a never-firing
        state.
        """
        import tempfile

        synthetic = (
            "from alphalens_pipeline.something import whatever\n"
            "from alphalens_research.something import other\n"
            "whatever, other\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "synthetic_broker_contract_violation.py"
            path.write_text(synthetic)
            modules = list(_iter_imports(path, include_function_scope=True))

        self.assertIn("alphalens_pipeline.something", modules)
        self.assertIn("alphalens_research.something", modules)

        pipeline_rules = [
            rule
            for rule in RULES
            if rule["from_pkg"] == "broker_contract"
            and rule["forbidden_prefix"] == "alphalens_pipeline"
        ]
        research_rules = [
            rule
            for rule in RULES
            if rule["from_pkg"] == "broker_contract"
            and rule["forbidden_prefix"] == "alphalens_research"
        ]
        self.assertEqual(
            len(pipeline_rules), 1, "the broker_contract -> alphalens_pipeline rule must exist once"
        )
        self.assertEqual(
            len(research_rules), 1, "the broker_contract -> alphalens_research rule must exist once"
        )
        for rule in (*pipeline_rules, *research_rules):
            self.assertNotIn(
                "top_level_only",
                rule,
                f"rule {rule['name']!r} must catch function-scope (lazy) imports too",
            )
            with self.subTest(rule=rule["name"]):
                self.assertTrue(
                    any(m.startswith(rule["forbidden_prefix"]) for m in modules),
                    f"rule {rule['name']!r} would not catch the synthetic violation",
                )

    def test_exemptions_still_exist(self):
        """A documented exemption must stay tied to a real violation.

        Two ways an exemption can rot:
          1. The exempted file is deleted — the entry now points at nothing.
          2. The exempted file no longer contains the forbidden import — the
             entry silently widens the allowlist for a smell that is gone.

        Both fail loudly here so a stale exemption can't mask a future
        re-introduction of the same forbidden import.
        """
        for rule in RULES:
            pkg_dir = _resolve_pkg_dir(rule["from_pkg"])
            by_name = {p.name: p for p in _python_files(pkg_dir)}
            for name in rule["exemptions"]:
                path = by_name.get(name)
                self.assertIsNotNone(
                    path,
                    f"exemption refers to missing file: {name} under {rule['from_pkg']}",
                )
                assert path is not None  # narrow for the type checker (assertIsNotNone does not)
                modules = list(_iter_imports(path, include_function_scope=True))
                self.assertTrue(
                    any(m.startswith(rule["forbidden_prefix"]) for m in modules),
                    f"dead exemption: {name} no longer imports "
                    f"{rule['forbidden_prefix']!r} — remove it from rule "
                    f"{rule['name']!r}",
                )


if __name__ == "__main__":
    unittest.main()
