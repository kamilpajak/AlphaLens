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
import sys
import unittest
from collections.abc import Iterable
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
    "intent_replay": WORKSPACE_ROOT / "apps" / "intent-replay" / "intent_replay",
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
        # Fix round 3 (Task 3, INC-2 LIVE market-data client): data/ hosts
        # shared reference data (e.g. the MIC -> Saxo ExchangeId map both the
        # SIM order-placement adapter and the LIVE read-only market-data
        # client resolve instruments through). brokers/ consumes data/, the
        # SAME direction brokers/automanager already uses for
        # alphalens_pipeline.data.alt_data.yfinance_client. The reverse
        # (data reaching into brokers) would drag order-placement machinery
        # into read-only infrastructure and invert the established DAG.
        "name": "data must not import brokers (data is infrastructure; brokers consumes it, never the reverse)",
        "from_pkg": "alphalens_pipeline.data",
        "forbidden_prefix": "alphalens_pipeline.brokers",
        "exemptions": set(),
    },
    {
        # The thematic commands build and read briefs; they never arm a pick.
        # #1469 moved the brief read out of the broker group, and #1552 removed
        # the brief producer altogether: every pick is a hand-written document
        # armed through `broker arm`. The thematic CLI must not import the
        # broker layer, not even lazily inside a command body. The rule names a
        # module FILE, not a package.
        "name": "the thematic CLI must not import brokers (#1552: picks are hand-written documents)",
        "from_pkg": "alphalens_cli.commands.thematic",
        "forbidden_prefix": "alphalens_pipeline.brokers",
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
    {
        # intent-replay (spec section 3.1): the ENGINE modules import stdlib and
        # broker_contract only, so the measurement half can be lifted into a
        # standalone package carrying no dependency but the contract. This is
        # an ALLOW-list, not a forbid-list, because the risk is the NEXT
        # third-party import, which no list of known-bad prefixes can name.
        # The adapter modules (door, cli) may also import jsonschema; their
        # rows arrive with the files (PR 4 of #1571), since a rule must resolve
        # to an existing module and an exemption must name an existing file.
        "name": "intent_replay engine imports stdlib and broker_contract only",
        "from_pkg": "intent_replay",
        "allowed_prefixes": ("broker_contract",),
        "exemptions": set(),
    },
)


def _package_name_of(path: Path) -> str:
    """The dotted package a module file belongs to, read off the tree: every
    ancestor directory that carries an ``__init__.py``. Empty for a module
    outside any package (the synthetic files the positive controls write)."""
    parts: list[str] = []
    directory = path.parent
    while (directory / "__init__.py").is_file():
        parts.append(directory.name)
        directory = directory.parent
    return ".".join(reversed(parts))


def _resolve_relative(package: str, level: int, module: str | None) -> str:
    """``from ..b import y`` inside ``pkg.sub`` -> ``pkg.b``; ``from . import z``
    -> ``pkg.sub``. A relative import from outside any package resolves to the
    bare module name, which is what the walker used to report for every
    relative import."""
    base = package.split(".") if package else []
    base = base[: len(base) - (level - 1)] if level > 1 else base
    return ".".join(part for part in (*base, module) if part)


def _iter_imports(path: Path, *, include_function_scope: bool):
    """Yield every imported module name in ``path``, relative imports resolved.

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
    package = _package_name_of(path)

    class _ImportCollector(ast.NodeVisitor):
        def __init__(self) -> None:
            self.modules: list[str] = []

        def visit_Import(self, node: ast.Import) -> None:
            for alias in node.names:
                if alias.name:
                    self.modules.append(alias.name)
            self.generic_visit(node)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            if node.level:
                resolved = _resolve_relative(package, node.level, node.module)
                if resolved:
                    self.modules.append(resolved)
            elif node.module:
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


def _python_files(target: Path):
    """Every ``.py`` file of a package directory, or the one module file itself."""
    if target.is_file():
        return [target]
    return sorted(p for p in target.rglob("*.py") if p.name != "__pycache__")


def _resolve_pkg_dir(from_pkg: str) -> Path:
    """Map ``alphalens_research.backtest`` → its on-disk directory, or a module
    such as ``alphalens_cli.commands.thematic`` → its ``.py`` file.

    Raises ``FileNotFoundError`` when neither exists: ``rglob`` over a missing
    directory yields nothing, so a mistyped rule would otherwise pass vacuously.
    """
    parts = from_pkg.split(".")
    base = PACKAGE_DIRS.get(parts[0])
    if base is None:
        raise KeyError(f"unknown top-level package in rule: {parts[0]}")
    target = base.joinpath(*parts[1:]) if len(parts) > 1 else base
    if target.is_dir():
        return target
    module = target.with_suffix(".py")
    if module.is_file():
        return module
    raise FileNotFoundError(f"rule package {from_pkg!r} resolves to no directory or module")


def _violates(rule: dict, module: str) -> bool:
    """Does importing ``module`` from inside ``rule["from_pkg"]`` break the rule?

    Two rule KINDS. ``forbidden_prefix``: the import breaks the rule when it
    starts with the prefix. ``allowed_prefixes``: the import is fine when it is
    stdlib, the rule's own top-level package, or one of the listed prefixes
    (exactly, or as a dotted parent); anything else breaks the rule. A rule
    with both keys or neither is a defect, reported loudly rather than as a
    rule that forbids nothing.
    """
    kinds = {"forbidden_prefix", "allowed_prefixes"} & set(rule)
    if len(kinds) != 1:
        raise ValueError(f"rule {rule.get('name')!r} must carry exactly one kind, has {kinds}")
    if "forbidden_prefix" in rule:
        return module.startswith(rule["forbidden_prefix"])
    top = module.split(".", maxsplit=1)[0]
    if top in sys.stdlib_module_names or top == rule["from_pkg"].split(".")[0]:
        return False
    return not any(
        module == prefix or module.startswith(f"{prefix}.") for prefix in rule["allowed_prefixes"]
    )


def _violations_for(rule: dict, files: Iterable[Path]) -> list[tuple[str, str, str]]:
    """Every (rule name, file, module) triple where ``files`` break ``rule``.

    The one loop both ``test_rules`` and the positive controls run, so a
    control exercises the same code path a real violation would take.
    """
    # Cross-tier pipeline rule: skip function-scope imports because the CLI is
    # allowed to lazy-import the research tier inside command bodies (see the
    # module docstring).
    top_level_only = rule.get("top_level_only", False) or rule["from_pkg"] == "alphalens_pipeline"
    violations: list[tuple[str, str, str]] = []
    for path in files:
        rel = (
            str(path.relative_to(WORKSPACE_ROOT))
            if path.is_relative_to(WORKSPACE_ROOT)
            else str(path)
        )
        # Exemptions are keyed by BASENAME intentionally: the packages these
        # rules scan are flat, so a basename is unambiguous, and it keeps the
        # RULES entries readable (bare filename, not a workspace-relative path).
        # If a scanned package ever grows subdirectories, switch this + the
        # anti-rot check in test_exemptions_still_exist to relative paths.
        if path.name in rule["exemptions"]:
            continue
        for module in _iter_imports(path, include_function_scope=not top_level_only):
            if _violates(rule, module):
                violations.append((rule["name"], rel, module))
    return violations


class TestModuleDependencies(unittest.TestCase):
    def test_rules(self):
        violations: list[tuple[str, str, str]] = []
        for rule in RULES:
            violations.extend(
                _violations_for(rule, _python_files(_resolve_pkg_dir(rule["from_pkg"])))
            )

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
            rule for rule in RULES if rule.get("forbidden_prefix") == "alphalens_pipeline.brokers"
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

    def test_the_thematic_cli_module_brokers_tripwire_positive_control(self):
        """The thematic CLI must not reach into the broker layer, lazily or not
        (#1552: no thematic command arms a pick). The rule names a single MODULE
        file, which the walker used to resolve to a directory that does not
        exist and scan nothing: pin that it scans exactly that file, and that a
        function-scope import there is caught."""
        import tempfile

        rules = [
            rule
            for rule in RULES
            if rule["from_pkg"] == "alphalens_cli.commands.thematic"
            and rule["forbidden_prefix"] == "alphalens_pipeline.brokers"
        ]
        self.assertEqual(len(rules), 1, "the thematic CLI -> brokers rule must exist once")
        self.assertNotIn("top_level_only", rules[0])

        scanned = _python_files(_resolve_pkg_dir(rules[0]["from_pkg"]))
        self.assertEqual(
            [p.relative_to(WORKSPACE_ROOT).as_posix() for p in scanned],
            ["apps/alphalens-pipeline/alphalens_cli/commands/thematic.py"],
        )

        synthetic = (
            "def some_thematic_command():\n"
            "    from alphalens_pipeline.brokers.automanager.picks import arm_pick\n"
            "    return arm_pick\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "synthetic_thematic_cli_violation.py"
            path.write_text(synthetic)
            modules = list(_iter_imports(path, include_function_scope=True))
        self.assertTrue(any(m.startswith(rules[0]["forbidden_prefix"]) for m in modules))

    def test_a_rule_that_resolves_to_nothing_is_an_error(self):
        """A rule whose package or module is missing would scan no file and pass
        whatever it forbids. Every rule must resolve, and a typo must not."""
        for rule in RULES:
            with self.subTest(rule=rule["name"]):
                self.assertTrue(_python_files(_resolve_pkg_dir(rule["from_pkg"])))
        with self.assertRaises(FileNotFoundError):
            _resolve_pkg_dir("alphalens_cli.commands.no_such_module")

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

    def test_data_must_not_import_brokers_positive_control(self):
        """The data -> brokers direction rule (added fix round 3, Task 3:
        ``saxo_marketdata_client.py`` had reached into
        ``brokers.saxo.broker`` for a private MIC map) cannot rot silently.

        Feeds the walker a synthetic module that hides a brokers import
        inside a function body and asserts (1) the walker surfaces it and
        (2) the rule's forbidden_prefix matches it — the sneakiest
        allowed-elsewhere shape, same pattern as the other single-rule
        tripwires above.
        """
        import tempfile

        synthetic = (
            "def sneaky():\n"
            "    from alphalens_pipeline.brokers.saxo.broker import SaxoBroker\n"
            "    return SaxoBroker\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "synthetic_data_violation.py"
            path.write_text(synthetic)
            modules = list(_iter_imports(path, include_function_scope=True))

        self.assertIn("alphalens_pipeline.brokers.saxo.broker", modules)

        data_rules = [
            rule
            for rule in RULES
            if rule["from_pkg"] == "alphalens_pipeline.data"
            and rule["forbidden_prefix"] == "alphalens_pipeline.brokers"
        ]
        self.assertEqual(len(data_rules), 1, "the data -> brokers rule must exist exactly once")
        self.assertNotIn(
            "top_level_only",
            data_rules[0],
            "the data -> brokers rule must catch function-scope (lazy) imports too",
        )
        self.assertTrue(
            any(m.startswith(data_rules[0]["forbidden_prefix"]) for m in modules),
            "rule would not catch the synthetic violation",
        )

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

    def test_relative_imports_resolve_to_absolute_names(self):
        """A relative import is reported as the absolute module it names.

        Before this the walker never read ``node.level``: ``from .bars import
        Bar`` surfaced as a bare ``bars`` and ``from . import x`` vanished. A
        forbid-list rule never noticed, because no forbidden prefix is bare;
        an allow-list rule would flag every intra-package import as foreign.
        Resolving to the absolute name fixes both and lets a rule name a
        sibling module (``intent_replay.door``) as forbidden.
        """
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / "pkg"
            (pkg / "sub").mkdir(parents=True)
            (pkg / "__init__.py").write_text("")
            (pkg / "sub" / "__init__.py").write_text("")
            module = pkg / "sub" / "mod.py"
            module.write_text(
                "from .a import x\nfrom ..b import y\nfrom . import z\nfrom pkg.c import w\n"
            )
            modules = list(_iter_imports(module, include_function_scope=True))

        self.assertEqual(modules, ["pkg.sub.a", "pkg.b", "pkg.sub", "pkg.c"])

    def test_resolved_relative_imports_change_no_existing_verdict(self):
        """Pins the measurement that made the walker change safe: across every
        package the rules scan, no resolved relative import violates the rule
        scanning it (72 relative imports, 0 hits when measured). If one ever
        does, the rule author must decide, not the walker."""
        seen = 0
        for rule in RULES:
            pkg_dir = _resolve_pkg_dir(rule["from_pkg"])
            for path in _python_files(pkg_dir):
                tree = ast.parse(path.read_text(), filename=str(path))
                relative = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level]
                if not relative:
                    continue
                seen += len(relative)
                package = _package_name_of(path)
                for node in relative:
                    resolved = _resolve_relative(package, node.level, node.module)
                    self.assertFalse(
                        _violates(rule, resolved),
                        f"{path.name}: relative import resolved to {resolved!r} "
                        f"violates rule {rule['name']!r}",
                    )
        self.assertGreater(seen, 0, "the scanned packages carry relative imports today")

    def test_every_rule_carries_exactly_one_kind(self):
        """A rule is a forbid-list (``forbidden_prefix``) or an allow-list
        (``allowed_prefixes``), never both and never neither. The predicate is
        only invoked per import, so a rule over a package with no imports would
        never reach it; this test asks the RULES table directly."""
        for rule in RULES:
            with self.subTest(rule=rule["name"]):
                kinds = {"forbidden_prefix", "allowed_prefixes"} & set(rule)
                self.assertEqual(len(kinds), 1, f"rule {rule['name']!r} has kinds {kinds}")
        base = {"name": "synthetic", "from_pkg": "broker_contract", "exemptions": set()}
        with self.assertRaises(ValueError):
            _violates(base, "os")
        with self.assertRaises(ValueError):
            _violates({**base, "forbidden_prefix": "x", "allowed_prefixes": ("y",)}, "os")

    def test_intent_replay_engine_rule_exists_once(self):
        """The intent_replay engine rule is the only barrier keeping the engine
        importable without a library (spec section 3.1); it must exist once,
        in the allow-list kind, with no escape hatch."""
        rules = [rule for rule in RULES if rule["from_pkg"] == "intent_replay"]
        self.assertEqual(len(rules), 1, "the intent_replay engine rule must exist exactly once")
        rule = rules[0]
        self.assertEqual(rule["allowed_prefixes"], ("broker_contract",))
        self.assertNotIn("forbidden_prefix", rule)
        self.assertNotIn("top_level_only", rule, "lazy imports must be caught too")
        self.assertEqual(rule["exemptions"], set())
        self.assertTrue(_python_files(_resolve_pkg_dir(rule["from_pkg"])))

    def test_allowed_prefixes_rule_kind_positive_control(self):
        """The allow-list kind cannot rot silently.

        Runs the REAL collection loop (``_violations_for``) over a synthetic
        engine directory. The engine shape must flag a third-party import, a
        second third-party import that the adapter shape allows, and a lazy
        first-party import; it must pass stdlib, the contract and a relative
        sibling. The adapter shape must pass ``jsonschema`` and still flag
        ``pandas`` — that difference is the per-module split of spec 3.1, the
        one rule with no precedent in this file.
        """
        import tempfile

        engine_rule = {
            "name": "synthetic engine",
            "from_pkg": "synthetic_engine",
            "allowed_prefixes": ("broker_contract",),
            "exemptions": set(),
        }
        adapter_rule = {**engine_rule, "name": "synthetic adapter"}
        adapter_rule["allowed_prefixes"] = ("broker_contract", "jsonschema")
        sources = {
            "uses_pandas.py": "import pandas\n",
            "uses_jsonschema.py": "import jsonschema\n",
            "lazy_pipeline.py": (
                "def f():\n    from alphalens_pipeline.data.factors import x\n    return x\n"
            ),
            "clean.py": (
                "from __future__ import annotations\n"
                "import math\n"
                "from broker_contract.trade_intent.codec import intent_to_jsonable\n"
                "from .bars import Bar\n"
            ),
        }
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / "synthetic_engine"
            pkg.mkdir()
            (pkg / "__init__.py").write_text("")
            for name, source in sources.items():
                (pkg / name).write_text(source)
            files = _python_files(pkg)
            engine = _violations_for(engine_rule, files)
            adapter = _violations_for(adapter_rule, files)

        flagged = sorted(module for _, _, module in engine)
        self.assertEqual(flagged, ["alphalens_pipeline.data.factors", "jsonschema", "pandas"])
        self.assertEqual(
            sorted(module for _, _, module in adapter),
            ["alphalens_pipeline.data.factors", "pandas"],
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
                    any(_violates(rule, m) for m in modules),
                    f"dead exemption: {name} no longer breaks rule "
                    f"{rule['name']!r} — remove it from the rule's exemptions",
                )


if __name__ == "__main__":
    unittest.main()
