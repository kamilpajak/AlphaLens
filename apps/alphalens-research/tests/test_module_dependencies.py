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
        # #1677, step 1 of partitioning control_loop.py: the stream rail moved
        # out into its own module, and the direction must stay one-way. The
        # tick imports `stream_handles`; if `stream_handles` ever imported
        # `control_loop` back -- for a helper, a constant, or a type -- the
        # package would have the same shape the extraction removed, and a
        # function-body import would keep it off the import-time path exactly
        # as the cut cycles did. So no `top_level_only` here either.
        "name": "the stream rail must not import the control loop (the partition is one-way)",
        "from_pkg": "alphalens_pipeline.brokers.automanager.stream_handles",
        "forbidden_prefix": "alphalens_pipeline.brokers.automanager.control_loop",
        "exemptions": set(),
    },
    {
        # #1677 step 2: the standalone-stop journal layer came out next, and
        # for the same reason -- it must not reach back. Every large cluster
        # still waiting for extraction calls into it, so a back-edge here
        # would block the rest of the partition.
        "name": "the stop journal must not import the control loop (the partition is one-way)",
        "from_pkg": "alphalens_pipeline.brokers.automanager.stop_journal",
        "forbidden_prefix": "alphalens_pipeline.brokers.automanager.control_loop",
        "exemptions": set(),
    },
    {
        # The audit's biggest cycle (control_loop <-> live_exit_engine, §2.2)
        # existed because `mark_tranche_fired` lazy-imported ONE journal
        # helper from `control_loop`. Step 2 gave that helper its own module,
        # so the engine no longer reaches up. This rule is what keeps it cut:
        # the tick imports the engine, never the other way round. No
        # `top_level_only` -- the cycle lived in a function-body import.
        "name": "the live-exit engine must not import the control loop (the cut cycle stays cut)",
        "from_pkg": "alphalens_pipeline.brokers.automanager.live_exit_engine",
        "forbidden_prefix": "alphalens_pipeline.brokers.automanager.control_loop",
        "exemptions": set(),
    },
    {
        # #1677 step 3 moved the journal's own writers, folds and counters into
        # the module that owns the journal file, and that gave `stop_journal`
        # two imports it did not have before: `entry_trails` (a stop ref is
        # parsed with the same parser that built it) and `position_manager`
        # (the fold answers in `PlannedExit`). Neither imported the journal
        # back, so no cycle appeared -- but nothing except these two rules
        # stops one appearing the next time either module wants a fold it can
        # see. The direction is: journal downstream of both, never upstream.
        #
        # No `top_level_only`. Every cycle this audit cut lived in a
        # function-body import, which is exactly where a reader would put one
        # to avoid an import-time loop -- and that is the import that would
        # make the loop real again at call time.
        "name": "entry trails must not import the stop journal (step 3 added the reverse edge)",
        "from_pkg": "alphalens_pipeline.brokers.automanager.entry_trails",
        "forbidden_prefix": "alphalens_pipeline.brokers.automanager.stop_journal",
        "exemptions": set(),
    },
    {
        "name": "the position manager must not import the stop journal (step 3 added the reverse edge)",
        "from_pkg": "alphalens_pipeline.brokers.automanager.position_manager",
        "forbidden_prefix": "alphalens_pipeline.brokers.automanager.stop_journal",
        "exemptions": set(),
    },
    {
        # Step 4 of the partition moved the entry-watch pass's own helpers into
        # `entry_watch`, which imports costs. Nothing in the
        # opposite direction, now or later: entry_watch is downstream.
        "name": "the cost model must not import the entry watch (step 4 added the reverse edge)",
        "from_pkg": "alphalens_pipeline.brokers.automanager.costs",
        "forbidden_prefix": "alphalens_pipeline.brokers.automanager.entry_watch",
        "exemptions": set(),
    },
    {
        # Step 4 of the partition moved the entry-watch pass's own helpers into
        # `entry_watch`, which imports entry_trail_geometry. Nothing in the
        # opposite direction, now or later: entry_watch is downstream.
        "name": "entry-trail geometry must not import the entry watch (step 4 added the reverse edge)",
        "from_pkg": "alphalens_pipeline.brokers.automanager.entry_trail_geometry",
        "forbidden_prefix": "alphalens_pipeline.brokers.automanager.entry_watch",
        "exemptions": set(),
    },
    {
        # Step 4 of the partition moved the entry-watch pass's own helpers into
        # `entry_watch`, which imports entry_trail_watcher. Nothing in the
        # opposite direction, now or later: entry_watch is downstream.
        "name": "the entry-trail watcher must not import the entry watch (step 4 added the reverse edge)",
        "from_pkg": "alphalens_pipeline.brokers.automanager.entry_trail_watcher",
        "forbidden_prefix": "alphalens_pipeline.brokers.automanager.entry_watch",
        "exemptions": set(),
    },
    {
        # Step 4 of the partition moved the entry-watch pass's own helpers into
        # `entry_watch`, which imports entry_trails. Nothing in the
        # opposite direction, now or later: entry_watch is downstream.
        "name": "entry trails must not import the entry watch (step 4 added the reverse edge)",
        "from_pkg": "alphalens_pipeline.brokers.automanager.entry_trails",
        "forbidden_prefix": "alphalens_pipeline.brokers.automanager.entry_watch",
        "exemptions": set(),
    },
    {
        # Step 4 of the partition moved the entry-watch pass's own helpers into
        # `entry_watch`, which imports labels. Nothing in the
        # opposite direction, now or later: entry_watch is downstream.
        "name": "the label helpers must not import the entry watch (step 4 added the reverse edge)",
        "from_pkg": "alphalens_pipeline.brokers.automanager.labels",
        "forbidden_prefix": "alphalens_pipeline.brokers.automanager.entry_watch",
        "exemptions": set(),
    },
    {
        # Step 4 of the partition moved the entry-watch pass's own helpers into
        # `entry_watch`, which imports live_exit_engine. Nothing in the
        # opposite direction, now or later: entry_watch is downstream.
        "name": "the live-exit engine must not import the entry watch (step 4 added the reverse edge)",
        "from_pkg": "alphalens_pipeline.brokers.automanager.live_exit_engine",
        "forbidden_prefix": "alphalens_pipeline.brokers.automanager.entry_watch",
        "exemptions": set(),
    },
    {
        # Step 4 of the partition moved the entry-watch pass's own helpers into
        # `entry_watch`, which imports stop_journal. Nothing in the
        # opposite direction, now or later: entry_watch is downstream.
        "name": "the stop journal must not import the entry watch (step 4 added the reverse edge)",
        "from_pkg": "alphalens_pipeline.brokers.automanager.stop_journal",
        "forbidden_prefix": "alphalens_pipeline.brokers.automanager.entry_watch",
        "exemptions": set(),
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
        # The lab is a CONSUMER of the workspace, never a consumer of the
        # command-line adapter. `alphalens_cli` is a composition root: it
        # imports from everywhere (116 outbound edges) and nothing imports it
        # (1 inbound, now 0). The one import that existed reached for a
        # registry of research scripts that the CLI happened to hold —
        # `_SCRIPTS` — which put a context cycle between the lab and the
        # composition root. The registry now lives in the lab, where its
        # contents do.
        "name": "alphalens_research must not import from alphalens_cli (the CLI is a composition root)",
        "from_pkg": "alphalens_research",
        "forbidden_prefix": "alphalens_cli",
        "exemptions": set(),
    },
    {
        # `market` is a platform leaf and must stay one (#1678). After the
        # calendar, session and bar primitives moved in, it became the
        # most-depended-on package in the repo (43 inbound). If it ever imports
        # a CONSUMER tier, every one of those consumers inherits the dependency
        # transitively — which is how a leaf stops being a leaf without anyone
        # editing a consumer.
        #
        # Three deny rules rather than one `allowed_prefixes` rule on purpose:
        # the allow-list kind always permits "the rule's own top-level package",
        # which for `alphalens_pipeline.market` is `alphalens_pipeline` — so an
        # allow-list here would permit every sibling and forbid nothing.
        #
        # `market.market_state` -> `data` is allowed and live: `data` is the
        # other platform tier, not a consumer.
        "name": "market must not import feedback (the platform leaf stays a leaf)",
        "from_pkg": "alphalens_pipeline.market",
        "forbidden_prefix": "alphalens_pipeline.feedback",
        "exemptions": set(),
    },
    {
        "name": "market must not import thematic (the platform leaf stays a leaf)",
        "from_pkg": "alphalens_pipeline.market",
        "forbidden_prefix": "alphalens_pipeline.thematic",
        "exemptions": set(),
    },
    {
        "name": "market must not import brokers (the platform leaf stays a leaf)",
        "from_pkg": "alphalens_pipeline.market",
        "forbidden_prefix": "alphalens_pipeline.brokers",
        "exemptions": set(),
    },
    {
        # Selection never reads measurement (#1678). The thematic lane decides
        # WHICH tickers appear in a brief; the feedback lane measures what
        # happened to earlier ones. Two edges used to run the wrong way — the
        # publication clock reached for `feedback.ladder_config.ladder_arrival_session`
        # and the trade-setup geometry for the PRIVATE
        # `feedback.bar_window._window_vwap` — which also closed a real cycle,
        # `feedback.selection_label -> thematic.publication -> feedback.ladder_config`.
        # Both now read the shared primitives from `alphalens_pipeline.market`.
        #
        # The REVERSE direction stays allowed and is live: measurement reads
        # selection (`feedback.selection_label` imports `thematic.publication`),
        # which is why this rule is one-way and there is no mirror of it.
        #
        # No `top_level_only`: a lazy import would re-create the same coupling,
        # and the two edges this replaced were themselves top-level.
        "name": "thematic must not import feedback (selection never reads measurement)",
        "from_pkg": "alphalens_pipeline.thematic",
        "forbidden_prefix": "alphalens_pipeline.feedback",
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
        # The exit_geometry package layers one way: levels (pure functions) <-
        # policy (the ExitPolicy Protocol, its four implementations, and the
        # numeric ExitGeometryPolicy carrier they place against) <- registry
        # (names and lookup only). Before this rule, `registry` held the
        # ExitGeometryPolicy its own entries are constructed with, so `policy`
        # imported it back from `registry` at top level while `registry`
        # imported the policies inside two function bodies -- a real runtime
        # cycle inside the shared contract leaf, kept off the import-time path
        # only by that laziness (architecture audit 2026-10-02, finding #8).
        # No `top_level_only`: the edge that has to stay dead is exactly the
        # lazy shape, so a function-body import must fail here too.
        #
        # Scoped to the whole PACKAGE, not just `policy.py`: the cycle that was
        # cut ran policy <-> registry, but the invariant worth keeping is that
        # the registry is the TOP of the leaf -- nothing inside reaches up to
        # it. Scoping the rule to the one module that happened to break it
        # would leave a second sibling free to re-form the same cycle.
        "name": "nothing in the exit-geometry leaf may import the registry (it layers one way)",
        "from_pkg": "broker_contract.exit_geometry",
        "forbidden_prefix": "broker_contract.exit_geometry.registry",
        # anti-rot: the package root publishes `resolve_exit_policy`, so it is
        # the one legitimate importer. Drop this entry if that re-export moves.
        "exemptions": {"__init__.py"},
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
        # Rules COMPOSE: every one of them must pass, so an adapter row cannot
        # widen what this row allows for a file this row still scans. door.py is
        # exempt because it is the file that imports jsonschema (gate 1). cli.py
        # is not, because it reaches jsonschema only through door.py and an
        # exemption naming no live violation is refused by
        # test_exemptions_still_exist. Spec section 3.1 permits the cli that
        # import too; the day it needs one, add "cli.py" here AND a dedicated
        # allow-list row for `intent_replay.cli`, in the same commit as the
        # import itself.
        "exemptions": {"door.py"},
    },
    {
        "name": "intent_replay.door may import jsonschema",
        "from_pkg": "intent_replay.door",
        "allowed_prefixes": ("broker_contract", "jsonschema"),
        "exemptions": set(),
    },
    {
        # The allow-list treats the rule's own package as legal, so an engine
        # module could reach jsonschema THROUGH the door with the rule above
        # green. These two rows close that: only cli.py may import the door,
        # and only __main__.py may import the cli.
        "name": "intent_replay engine must not import the door",
        "from_pkg": "intent_replay",
        "forbidden_prefix": "intent_replay.door",
        "exemptions": {"cli.py"},
    },
    {
        "name": "intent_replay engine must not import the cli",
        "from_pkg": "intent_replay",
        "forbidden_prefix": "intent_replay.cli",
        "exemptions": {"__main__.py"},
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
    resolves to the package ``pkg.sub`` (the caller appends the imported
    names, because each of them is a module). A level deeper than the package
    is an error: Python refuses it at import time, and a static gate must not
    quietly turn it into a bare name that might happen to be legal."""
    base = package.split(".") if package else []
    if level > len(base):
        raise ValueError(
            f"relative import level {level} reaches past the top of package {package!r}"
        )
    base = base[: len(base) - (level - 1)]
    return ".".join(part for part in (*base, module) if part)


def _package_root(top: str, path: Path, package: str) -> Path | None:
    """The directory of top-level package ``top``: a known workspace package, or
    the scanned file's own package root (so a synthetic package in a temp
    directory resolves the same way)."""
    # The scanned file's OWN package wins over the workspace map, so a
    # synthetic package that borrows a real name resolves against itself.
    if package.split(".", maxsplit=1)[0] == top:
        directory = path.parent
        while directory.name != top and (directory.parent / "__init__.py").is_file():
            directory = directory.parent
        if directory.name == top:
            return directory
    return PACKAGE_DIRS.get(top)


def _level0_targets(node: ast.ImportFrom, path: Path, package: str) -> list[str]:
    """What a level-0 ``from X import a, b`` imports: ``X.a`` for every name that
    is a MODULE of ``X`` on disk (``X/a.py`` or ``X/a/__init__.py``), and ``X``
    itself when any name is not — ``from intent_replay import door`` names the
    door module and used to walk as the bare package, invisible to every rule."""
    assert node.module is not None
    parts = node.module.split(".")
    root = _package_root(parts[0], path, package)
    directory = root.joinpath(*parts[1:]) if root is not None else None
    targets: list[str] = []
    unresolved = False
    for alias in node.names:
        if directory is not None and (
            (directory / f"{alias.name}.py").is_file()
            or (directory / alias.name / "__init__.py").is_file()
        ):
            targets.append(f"{node.module}.{alias.name}")
        else:
            unresolved = True
    if unresolved or not targets:
        targets.append(node.module)
    return targets


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
                base = _resolve_relative(package, node.level, node.module)
                if node.module is None:
                    # `from . import x, y`: each name is a MODULE of the package.
                    self.modules.extend(f"{base}.{alias.name}" for alias in node.names)
                else:
                    self.modules.append(base)
            elif node.module:
                self.modules.extend(_level0_targets(node, path, package))
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
        prefix = rule["forbidden_prefix"]
        # A prefix written WITH a trailing dot names a package, so the package
        # itself breaks the rule too: `from alphalens_research import attribution`
        # resolves to `alphalens_research.attribution`, which `startswith` alone
        # would miss. A prefix written WITHOUT one is deliberately a string
        # match (`alphalens_pipeline.data.alt_data.telegram` must catch
        # `telegram_client`), so it keeps the bare comparison.
        return (prefix.endswith(".") and module == prefix[:-1]) or module.startswith(prefix)
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

    def test_research_must_not_import_the_cli_positive_control(self):
        """The lab -> CLI rule (#1675, the Research-lab/composition-root context
        cycle) cannot rot silently.

        The import it replaced reached for a PRIVATE name, ``_SCRIPTS``, so the
        synthetic violation uses that shape, and hides it in a function body —
        the rule carries no ``top_level_only``, because a lazy import into the
        composition root is just as wrong as a top-level one. The CLI may read
        the lab lazily; the lab may not read the CLI at all.
        """
        import tempfile

        synthetic = (
            "def sneaky():\n"
            "    from alphalens_cli.commands.audit import _SCRIPTS\n"
            "    return _SCRIPTS\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "synthetic_research_cli_violation.py"
            path.write_text(synthetic)
            modules = list(_iter_imports(path, include_function_scope=True))

        self.assertIn("alphalens_cli.commands.audit", modules)

        cli_rules = [
            rule
            for rule in RULES
            if rule["from_pkg"] == "alphalens_research"
            and rule.get("forbidden_prefix") == "alphalens_cli"
        ]
        self.assertEqual(len(cli_rules), 1, "the research -> cli rule must exist exactly once")
        self.assertNotIn(
            "top_level_only",
            cli_rules[0],
            "the research -> cli rule must catch function-scope (lazy) imports too",
        )
        self.assertTrue(
            any(m.startswith(cli_rules[0]["forbidden_prefix"]) for m in modules),
            "rule would not catch the synthetic violation",
        )

    def test_market_leaf_rules_positive_control(self):
        """The three `market` leaf rules cannot rot silently.

        One control for the family, matching how the brokers rules are covered.
        Feeds the walker a synthetic module that reaches up into all three
        consumer tiers from inside a function body, and asserts each rule would
        catch its own target.
        """
        import tempfile

        synthetic = (
            "def sneaky():\n"
            "    from alphalens_pipeline.feedback.bar_window import window_vwap\n"
            "    from alphalens_pipeline.thematic.publication import brief_open_utc\n"
            "    from alphalens_pipeline.brokers.reconcile import sweep\n"
            "    return window_vwap, brief_open_utc, sweep\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "synthetic_market_leaf_violation.py"
            path.write_text(synthetic)
            modules = list(_iter_imports(path, include_function_scope=True))

        leaf_rules = [r for r in RULES if r["from_pkg"] == "alphalens_pipeline.market"]
        self.assertEqual(len(leaf_rules), 3, "expected exactly three market leaf rules")
        for rule in leaf_rules:
            self.assertNotIn(
                "top_level_only",
                rule,
                f"{rule['name']!r} must catch function-scope (lazy) imports too",
            )
            self.assertTrue(
                any(m.startswith(rule["forbidden_prefix"]) for m in modules),
                f"{rule['name']!r} would not catch the synthetic violation",
            )

    def test_market_may_still_import_data(self):
        """`data` is the other platform tier, not a consumer.

        `market.market_state` reads `data.rs_history` and `data.macro.fred_client`.
        If this ever reads zero, a leaf rule was written too broadly and silently
        cut a live edge rather than the three it was aimed at.
        """
        market_dir = PACKAGE_DIRS["alphalens_pipeline"] / "market"
        live = [
            mod
            for path in sorted(market_dir.rglob("*.py"))
            for mod in _iter_imports(path, include_function_scope=True)
            if mod.startswith("alphalens_pipeline.data")
        ]
        self.assertTrue(live, "expected market to keep importing data; found none")

    def test_thematic_must_not_import_feedback_positive_control(self):
        """The selection -> measurement rule (#1678) cannot rot silently.

        The synthetic violation uses the shape the real one had: a PRIVATE name
        (`_window_vwap`) pulled across the boundary, hidden in a function body.
        The rule carries no `top_level_only`, so a lazy import must be caught
        too — the whole point is that the coupling cannot come back by any
        route.
        """
        import tempfile

        synthetic = (
            "def sneaky():\n"
            "    from alphalens_pipeline.feedback.bar_window import _window_vwap\n"
            "    return _window_vwap\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "synthetic_thematic_feedback_violation.py"
            path.write_text(synthetic)
            modules = list(_iter_imports(path, include_function_scope=True))

        self.assertIn("alphalens_pipeline.feedback.bar_window", modules)

        rules = [
            rule
            for rule in RULES
            if rule["from_pkg"] == "alphalens_pipeline.thematic"
            and rule.get("forbidden_prefix") == "alphalens_pipeline.feedback"
        ]
        self.assertEqual(len(rules), 1, "the thematic -> feedback rule must exist exactly once")
        self.assertNotIn(
            "top_level_only",
            rules[0],
            "the thematic -> feedback rule must catch function-scope (lazy) imports too",
        )
        self.assertTrue(
            any(m.startswith(rules[0]["forbidden_prefix"]) for m in modules),
            "rule would not catch the synthetic violation",
        )

    def test_the_reverse_direction_is_still_allowed_and_live(self):
        """Measurement reading selection is the INTENDED direction.

        If this ever reads zero, either the coupling moved somewhere this gate
        cannot see, or someone "fixed" a cycle by reversing it — which would
        put the edge back the wrong way round.
        """
        feedback_dir = PACKAGE_DIRS["alphalens_pipeline"] / "feedback"
        live = [
            path.name
            for path in sorted(feedback_dir.rglob("*.py"))
            for mod in _iter_imports(path, include_function_scope=True)
            if mod.startswith("alphalens_pipeline.thematic")
        ]
        self.assertTrue(live, "expected feedback to keep importing thematic; found none")

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

    def test_nothing_in_the_leaf_may_import_the_registry_positive_control(self):
        """Every spelling of "a leaf module reaches the registry" is seen.

        The import this rule replaced was ``from
        broker_contract.exit_geometry.registry import ExitGeometryPolicy`` — a
        SYMBOL, not a module, which ``_level0_targets`` resolves to the registry
        module. The attribute form (``from broker_contract.exit_geometry import
        registry``) and the relative form (``from . import registry``) are the
        two other ways back in, and a lazy one inside a function body is the
        shape the other half of the cycle lived in for months.

        Runs the real collection loop over a synthetic package on disk, so a
        rule copied from RULES with the names swapped exercises the same
        resolution a real violation would.
        """
        import tempfile

        rules = [
            rule
            for rule in RULES
            if rule["from_pkg"] == "broker_contract.exit_geometry"
            and rule.get("forbidden_prefix") == "broker_contract.exit_geometry.registry"
        ]
        self.assertEqual(len(rules), 1, "the leaf -> registry rule must exist exactly once")
        self.assertNotIn(
            "top_level_only",
            rules[0],
            "the leaf -> registry rule must catch function-scope (lazy) imports too",
        )
        self.assertEqual(
            rules[0]["exemptions"],
            {"__init__.py"},
            "only the package root may import the registry",
        )

        sources = {
            "lazy_dotted.py": (
                "def f():\n"
                "    from synthetic_pkg.registry import ExitGeometryPolicy\n"
                "    return ExitGeometryPolicy\n"
            ),
            "attribute.py": "from synthetic_pkg import registry\n",
            "relative.py": "from . import registry\n",
            "clean.py": "import math\nfrom synthetic_pkg import levels\n",
            "registry.py": "",
            "levels.py": "",
        }
        rule = {
            "name": "synthetic forbid registry",
            "from_pkg": "synthetic_pkg",
            "forbidden_prefix": "synthetic_pkg.registry",
            "exemptions": set(),
        }
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / "synthetic_pkg"
            pkg.mkdir()
            (pkg / "__init__.py").write_text("")
            for name, source in sources.items():
                (pkg / name).write_text(source)
            flagged = sorted(
                (Path(rel).name, module)
                for _, rel, module in _violations_for(rule, _python_files(pkg))
            )
        self.assertEqual(
            flagged,
            [
                ("attribute.py", "synthetic_pkg.registry"),
                ("lazy_dotted.py", "synthetic_pkg.registry"),
                ("relative.py", "synthetic_pkg.registry"),
            ],
        )

    def test_the_registry_still_imports_the_policy(self):
        """The INTENDED direction, and the existence control for the rule above.

        If this ever reads zero, either the two modules stopped depending on
        each other at all — fine in itself, but then the forbid rule above is
        pinning nothing — or someone "fixed" a later cycle by reversing the
        edge, which would put it back the wrong way round.
        """
        registry = PACKAGE_DIRS["broker_contract"] / "exit_geometry" / "registry.py"
        live = [
            mod
            for mod in _iter_imports(registry, include_function_scope=True)
            if mod.startswith("broker_contract.exit_geometry.policy")
        ]
        self.assertTrue(live, "expected the registry to keep importing the policy; found none")

    def test_the_stream_rail_must_not_import_the_control_loop_positive_control(self):
        """Every spelling of "the stream rail reaches back" is seen.

        The partition only holds while the edge runs one way. The shapes that
        would put it back are a plain module import, a symbol import that
        resolves to the module, the attribute form, the relative form, and any
        of those inside a function body -- which is how the cycles this audit
        cut stayed off the import-time path for months.

        Runs the real collection loop over a synthetic package on disk, so the
        control exercises the same resolution a real violation would take.
        """
        import tempfile

        rules = [
            rule
            for rule in RULES
            if rule["from_pkg"] == "alphalens_pipeline.brokers.automanager.stream_handles"
        ]
        self.assertEqual(len(rules), 1, "the stream rail rule must exist exactly once")
        self.assertNotIn(
            "top_level_only",
            rules[0],
            "the stream rail rule must catch function-scope (lazy) imports too",
        )

        sources = {
            "lazy_symbol.py": (
                "def f():\n"
                "    from synthetic_pkg.control_loop import LoopDeps\n"
                "    return LoopDeps\n"
            ),
            "plain.py": "import synthetic_pkg.control_loop\n",
            "attribute.py": "from synthetic_pkg import control_loop\n",
            "relative.py": "from . import control_loop\n",
            "clean.py": "import math\nfrom synthetic_pkg import state_paths\n",
            "control_loop.py": "",
            "state_paths.py": "",
        }
        rule = {
            "name": "synthetic forbid control_loop",
            "from_pkg": "synthetic_pkg",
            "forbidden_prefix": "synthetic_pkg.control_loop",
            "exemptions": set(),
        }
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / "synthetic_pkg"
            pkg.mkdir()
            (pkg / "__init__.py").write_text("")
            for name, source in sources.items():
                (pkg / name).write_text(source)
            flagged = sorted(
                (Path(rel).name, module)
                for _, rel, module in _violations_for(rule, _python_files(pkg))
            )
        self.assertEqual(
            flagged,
            [
                ("attribute.py", "synthetic_pkg.control_loop"),
                ("lazy_symbol.py", "synthetic_pkg.control_loop"),
                ("plain.py", "synthetic_pkg.control_loop"),
                ("relative.py", "synthetic_pkg.control_loop"),
            ],
        )

    def test_the_journal_and_the_engine_must_not_reach_back_positive_control(self):
        """Both step-2 rules see every spelling of reaching back.

        One control for two rules, because they forbid the same target from
        two different modules: the extracted journal layer and the live-exit
        engine whose lazy import was half of the biggest cycle the audit found.
        """
        import tempfile

        rules = [
            rule
            for rule in RULES
            if rule.get("forbidden_prefix") == "alphalens_pipeline.brokers.automanager.control_loop"
        ]
        self.assertEqual(
            {rule["from_pkg"].rsplit(".", 1)[-1] for rule in rules},
            {"stream_handles", "stop_journal", "live_exit_engine"},
            "all three one-way rules must exist",
        )
        for rule in rules:
            self.assertNotIn(
                "top_level_only",
                rule,
                f"{rule['name']!r} must catch function-scope (lazy) imports too",
            )

        sources = {
            "lazy_symbol.py": (
                "def f():\n"
                "    from synthetic_pkg.control_loop import LoopDeps\n"
                "    return LoopDeps\n"
            ),
            "plain.py": "import synthetic_pkg.control_loop\n",
            "attribute.py": "from synthetic_pkg import control_loop\n",
            "relative.py": "from . import control_loop\n",
            "clean.py": "import json\nfrom synthetic_pkg import state_paths\n",
            "control_loop.py": "",
            "state_paths.py": "",
        }
        rule = {
            "name": "synthetic forbid control_loop",
            "from_pkg": "synthetic_pkg",
            "forbidden_prefix": "synthetic_pkg.control_loop",
            "exemptions": set(),
        }
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / "synthetic_pkg"
            pkg.mkdir()
            (pkg / "__init__.py").write_text("")
            for name, source in sources.items():
                (pkg / name).write_text(source)
            flagged = sorted(
                (Path(rel).name, module)
                for _, rel, module in _violations_for(rule, _python_files(pkg))
            )
        self.assertEqual(
            flagged,
            [
                ("attribute.py", "synthetic_pkg.control_loop"),
                ("lazy_symbol.py", "synthetic_pkg.control_loop"),
                ("plain.py", "synthetic_pkg.control_loop"),
                ("relative.py", "synthetic_pkg.control_loop"),
            ],
        )

    def test_nothing_the_journal_now_imports_may_import_it_back_positive_control(self):
        """Both step-3 rules see every spelling of reaching back.

        One control for two rules, because they forbid the same target from two
        different modules -- the two the journal layer started importing when
        step 3 moved the line writers and folds into it.
        """
        import tempfile

        rules = [
            rule
            for rule in RULES
            if rule.get("forbidden_prefix") == "alphalens_pipeline.brokers.automanager.stop_journal"
        ]
        self.assertEqual(
            {rule["from_pkg"].rsplit(".", 1)[-1] for rule in rules},
            {"entry_trails", "position_manager"},
            "both step-3 one-way rules must exist",
        )
        for rule in rules:
            self.assertNotIn(
                "top_level_only",
                rule,
                f"{rule['name']!r} must catch function-scope (lazy) imports too",
            )

        sources = {
            "lazy_symbol.py": (
                "def f():\n    from synthetic_pkg.stop_journal import _coerce\n    return _coerce\n"
            ),
            "plain.py": "import synthetic_pkg.stop_journal\n",
            "attribute.py": "from synthetic_pkg import stop_journal\n",
            "relative.py": "from . import stop_journal\n",
            "clean.py": "import json\nfrom synthetic_pkg import state_paths\n",
            "stop_journal.py": "",
            "state_paths.py": "",
        }
        rule = {
            "name": "synthetic forbid stop_journal",
            "from_pkg": "synthetic_pkg",
            "forbidden_prefix": "synthetic_pkg.stop_journal",
            "exemptions": set(),
        }
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / "synthetic_pkg"
            pkg.mkdir()
            (pkg / "__init__.py").write_text("")
            for name, source in sources.items():
                (pkg / name).write_text(source)
            flagged = sorted(
                (Path(rel).name, module)
                for _, rel, module in _violations_for(rule, _python_files(pkg))
            )
        self.assertEqual(
            flagged,
            [
                ("attribute.py", "synthetic_pkg.stop_journal"),
                ("lazy_symbol.py", "synthetic_pkg.stop_journal"),
                ("plain.py", "synthetic_pkg.stop_journal"),
                ("relative.py", "synthetic_pkg.stop_journal"),
            ],
        )

    def test_nothing_the_entry_watch_imports_may_import_it_back_positive_control(self):
        """All seven step-4 rules see every spelling of reaching back.

        One control for seven rules, because they forbid the same target from
        the seven modules the entry-watch layer started importing when step 4
        moved the pass's helpers into it.
        """
        import tempfile

        rules = [
            rule
            for rule in RULES
            if rule.get("forbidden_prefix") == "alphalens_pipeline.brokers.automanager.entry_watch"
        ]
        self.assertEqual(
            {rule["from_pkg"].rsplit(".", 1)[-1] for rule in rules},
            {
                "costs",
                "entry_trail_geometry",
                "entry_trail_watcher",
                "entry_trails",
                "labels",
                "live_exit_engine",
                "stop_journal",
            },
            "every module the entry watch imports needs its own one-way rule",
        )
        for rule in rules:
            self.assertNotIn(
                "top_level_only",
                rule,
                f"{rule['name']!r} must catch function-scope (lazy) imports too",
            )

        sources = {
            "lazy_symbol.py": (
                "def f():\n    from synthetic_pkg.entry_watch import _ArmRefusal\n"
                "    return _ArmRefusal\n"
            ),
            "plain.py": "import synthetic_pkg.entry_watch\n",
            "attribute.py": "from synthetic_pkg import entry_watch\n",
            "relative.py": "from . import entry_watch\n",
            "clean.py": "import json\nfrom synthetic_pkg import state_paths\n",
            "entry_watch.py": "",
            "state_paths.py": "",
        }
        rule = {
            "name": "synthetic forbid entry_watch",
            "from_pkg": "synthetic_pkg",
            "forbidden_prefix": "synthetic_pkg.entry_watch",
            "exemptions": set(),
        }
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / "synthetic_pkg"
            pkg.mkdir()
            (pkg / "__init__.py").write_text("")
            for name, source in sources.items():
                (pkg / name).write_text(source)
            flagged = sorted(
                (Path(rel).name, module)
                for _, rel, module in _violations_for(rule, _python_files(pkg))
            )
        self.assertEqual(
            flagged,
            [
                ("attribute.py", "synthetic_pkg.entry_watch"),
                ("lazy_symbol.py", "synthetic_pkg.entry_watch"),
                ("plain.py", "synthetic_pkg.entry_watch"),
                ("relative.py", "synthetic_pkg.entry_watch"),
            ],
        )

    def test_the_entry_watch_still_imports_the_seven_modules_it_depends_on(self):
        """The INTENDED direction, and the existence control for all seven rules.

        Reading zero for any one of them would mean that forbid rule guards a
        dependency that is gone, which is a gate that cannot fail.
        """
        entry_watch = (
            PACKAGE_DIRS["alphalens_pipeline"] / "brokers" / "automanager" / "entry_watch.py"
        )
        mods = list(_iter_imports(entry_watch, include_function_scope=True))
        for dependency in (
            "costs",
            "entry_trail_geometry",
            "entry_trail_watcher",
            "entry_trails",
            "labels",
            "live_exit_engine",
            "stop_journal",
        ):
            with self.subTest(dependency=dependency):
                self.assertTrue(
                    [
                        m
                        for m in mods
                        if m == f"alphalens_pipeline.brokers.automanager.{dependency}"
                    ],
                    f"expected the entry watch to keep importing {dependency}",
                )

    def test_the_control_loop_still_imports_the_entry_watch(self):
        """The INTENDED direction: the tick reaches the pass helpers by prefix.

        Reading zero would mean the tick stopped using them, which cannot happen
        while it watches entries -- so it would mean the layer moved back, or a
        `from` import replaced the prefix, which would break every test that
        patches this module.
        """
        control_loop = (
            PACKAGE_DIRS["alphalens_pipeline"] / "brokers" / "automanager" / "control_loop.py"
        )
        live = [
            mod
            for mod in _iter_imports(control_loop, include_function_scope=True)
            if mod.startswith("alphalens_pipeline.brokers.automanager.entry_watch")
        ]
        self.assertTrue(live, "expected the control loop to keep importing the entry watch")

    def test_the_journal_still_imports_the_two_modules_it_now_depends_on(self):
        """The INTENDED direction, and the existence control for both step-3 rules.

        Reading zero for either would mean the forbid rule above guards a
        dependency that is gone, which is a gate that cannot fail. The journal
        needs `entry_trails` to parse a stop ref with the same parser that
        built it, and `position_manager` for the `PlannedExit` its fold
        returns.
        """
        journal = PACKAGE_DIRS["alphalens_pipeline"] / "brokers" / "automanager" / "stop_journal.py"
        mods = list(_iter_imports(journal, include_function_scope=True))
        for dependency in ("entry_trails", "position_manager"):
            self.assertTrue(
                [m for m in mods if m.endswith(dependency)],
                f"expected the stop journal to keep importing {dependency}",
            )

    def test_the_control_loop_still_imports_the_stop_journal(self):
        """The INTENDED direction, and the existence control for the rule above.

        Reading zero would mean the tick stopped using the journal layer, which
        cannot happen while it manages stops -- so it would mean the layer moved
        back, or the import was replaced by something the walker cannot see.
        """
        control_loop = (
            PACKAGE_DIRS["alphalens_pipeline"] / "brokers" / "automanager" / "control_loop.py"
        )
        live = [
            mod
            for mod in _iter_imports(control_loop, include_function_scope=True)
            if mod.startswith("alphalens_pipeline.brokers.automanager.stop_journal")
        ]
        self.assertTrue(live, "expected the control loop to keep importing the stop journal")

    def test_the_live_exit_engine_reads_the_journal_from_its_own_module(self):
        """The engine's journal write must go through the extracted layer.

        This is the edge whose removal cut the audit's biggest cycle. If the
        engine ever imports the journal helper from `control_loop` again the
        rule above goes red; this test is the other half, that it still uses
        the helper at all rather than having quietly stopped journalling.
        """
        engine = (
            PACKAGE_DIRS["alphalens_pipeline"] / "brokers" / "automanager" / "live_exit_engine.py"
        )
        mods = list(_iter_imports(engine, include_function_scope=True))
        self.assertIn("alphalens_pipeline.brokers.automanager.stop_journal", mods)

    def test_the_control_loop_still_imports_the_stream_rail(self):
        """The INTENDED direction, and the existence control for the rule above.

        If this reads zero, either the tick stopped using the stream rail at
        all -- fine in itself, but then the forbid rule above pins nothing --
        or someone moved the wiring back into ``control_loop``, which would
        undo the partition without any gate noticing.
        """
        control_loop = (
            PACKAGE_DIRS["alphalens_pipeline"] / "brokers" / "automanager" / "control_loop.py"
        )
        live = [
            mod
            for mod in _iter_imports(control_loop, include_function_scope=True)
            if mod.startswith("alphalens_pipeline.brokers.automanager.stream_handles")
        ]
        self.assertTrue(live, "expected the control loop to keep importing the stream rail")

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
                "from .a import x\nfrom ..b import y\nfrom . import z, q\nfrom pkg.c import w\n"
            )
            modules = list(_iter_imports(module, include_function_scope=True))

        # `from . import z` names the MODULE pkg.sub.z, not the package: a rule
        # forbidding a sibling module must see it (the intent_replay engine ->
        # door edge of PR 4 is exactly that shape).
        self.assertEqual(modules, ["pkg.sub.a", "pkg.b", "pkg.sub.z", "pkg.sub.q", "pkg.c"])

    def test_a_relative_import_beyond_the_package_root_is_an_error(self):
        """Python refuses `from ... import x` past the top-level package at
        import time; a static gate must not quietly resolve it to a bare name
        that might happen to be legal."""
        with self.assertRaises(ValueError):
            _resolve_relative("pkg.sub", 3, "x")
        with self.assertRaises(ValueError):
            _resolve_relative("", 1, "x")

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
        rules = [
            rule
            for rule in RULES
            if rule["from_pkg"] == "intent_replay" and "allowed_prefixes" in rule
        ]
        self.assertEqual(len(rules), 1, "the intent_replay engine rule must exist exactly once")
        rule = rules[0]
        self.assertEqual(rule["allowed_prefixes"], ("broker_contract",))
        self.assertNotIn("top_level_only", rule, "lazy imports must be caught too")
        # The one escape hatch is the adapter file that imports jsonschema (spec
        # section 3.1); cli.py needs none, because it reaches jsonschema only
        # through door.py, and test_exemptions_still_exist would call a cli.py
        # entry a dead exemption.
        self.assertEqual(rule["exemptions"], {"door.py"})
        self.assertTrue(_python_files(_resolve_pkg_dir(rule["from_pkg"])))

    def test_intent_replay_adapter_rows(self):
        """The per-module split of spec section 3.1, as four rows: each adapter
        module may import jsonschema and nothing else third-party, and the
        engine may not reach an adapter module (and through it jsonschema).
        Each exemption names the ONE file that legitimately breaks the rule."""
        by_name = {rule["name"]: rule for rule in RULES}
        door_allow = by_name["intent_replay.door may import jsonschema"]
        self.assertEqual(door_allow["from_pkg"], "intent_replay.door")
        self.assertEqual(door_allow["allowed_prefixes"], ("broker_contract", "jsonschema"))
        self.assertEqual(door_allow["exemptions"], set())
        # cli.py has no allow-list row of its own, and must not grow one while
        # the engine row still scans it: the rules compose, so the row would
        # publish a permission the engine row refuses.
        self.assertNotIn("intent_replay.cli may import jsonschema", by_name)
        door_rule = by_name["intent_replay engine must not import the door"]
        self.assertEqual(door_rule["from_pkg"], "intent_replay")
        self.assertEqual(door_rule["forbidden_prefix"], "intent_replay.door")
        self.assertEqual(door_rule["exemptions"], {"cli.py"})
        cli_rule = by_name["intent_replay engine must not import the cli"]
        self.assertEqual(cli_rule["from_pkg"], "intent_replay")
        self.assertEqual(cli_rule["forbidden_prefix"], "intent_replay.cli")
        self.assertEqual(cli_rule["exemptions"], {"__main__.py"})

    def test_intent_replay_engine_must_not_reach_the_adapter_positive_control(self):
        """Every spelling of "import the door" is seen by the forbid rule: the
        dotted form, the relative form, and `from intent_replay import door`,
        which names the module as an ATTRIBUTE of the package and used to
        walk as the bare package name, invisible to any rule.

        Runs the real collection loop over a synthetic package on disk, so a
        rule copied from RULES with the package name swapped exercises the same
        resolution a real violation would."""
        import tempfile

        sources = {
            "lazy_dotted.py": "def f():\n    from synthetic_pkg.door import admit\n    return admit\n",
            "attribute.py": "from synthetic_pkg import door\n",
            "relative.py": "from . import door\n",
            "clean.py": "import math\nfrom synthetic_pkg import bars\n",
            "door.py": "import jsonschema\n",
            "bars.py": "",
        }
        rule = {
            "name": "synthetic forbid door",
            "from_pkg": "synthetic_pkg",
            "forbidden_prefix": "synthetic_pkg.door",
            "exemptions": set(),
        }
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / "synthetic_pkg"
            pkg.mkdir()
            (pkg / "__init__.py").write_text("")
            for name, source in sources.items():
                (pkg / name).write_text(source)
            flagged = sorted(
                (Path(rel).name, module)
                for _, rel, module in _violations_for(rule, _python_files(pkg))
            )
        self.assertEqual(
            flagged,
            [
                ("attribute.py", "synthetic_pkg.door"),
                ("lazy_dotted.py", "synthetic_pkg.door"),
                ("relative.py", "synthetic_pkg.door"),
            ],
        )

    def test_a_trailing_dot_rule_sees_the_package_itself(self):
        """The other rule shape in this file, with the same three spellings.

        Ten rules write their prefix WITH a trailing dot (the ADR 0007 layer
        rules and the cross-tier ones). Before the level-0 resolution,
        `from alphalens_research import attribution` walked as the bare
        `alphalens_research` and no rule saw it; after it, the resolved name is
        the package itself, which a bare `startswith` on a dotted prefix still
        misses. `_violates` therefore matches the parent exactly for that shape
        — and only for it, because a prefix written without the dot is a
        deliberate string match (the telegram tripwire catches
        `telegram_client`)."""
        import tempfile

        rule = {
            "name": "synthetic layer rule",
            "from_pkg": "synthetic_pkg",
            "forbidden_prefix": "synthetic_pkg.door.",
            "exemptions": set(),
        }
        sources = {
            "attribute.py": "from synthetic_pkg import door\n",
            "dotted.py": "from synthetic_pkg.door import admit\n",
            "relative.py": "from . import door\n",
            "sibling.py": "from synthetic_pkg import doorway\n",
            "door.py": "",
            "doorway.py": "",
        }
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / "synthetic_pkg"
            pkg.mkdir()
            (pkg / "__init__.py").write_text("")
            for name, source in sources.items():
                (pkg / name).write_text(source)
            flagged = sorted(
                (Path(rel).name, module)
                for _, rel, module in _violations_for(rule, _python_files(pkg))
            )
        # Two things at once. `doorway` shares the prefix as a STRING and is not
        # the package, so the dotted rule leaves it alone. And `admit` is a
        # function rather than a module, so the dotted import resolves to the
        # package it names, which is the resolution rule this file added.
        self.assertEqual(
            flagged,
            [
                ("attribute.py", "synthetic_pkg.door"),
                ("dotted.py", "synthetic_pkg.door"),
                ("relative.py", "synthetic_pkg.door"),
            ],
        )

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
