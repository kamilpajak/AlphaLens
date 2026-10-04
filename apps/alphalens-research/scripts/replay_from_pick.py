"""Build intent-replay's inputs from a LIVE pick's own production record.

Assembling a run by hand is where the errors were: over two days three of the
seven values came out wrong, and every one of them is recorded in the pick's own
journals. This module maps the record onto the two inputs the replay takes.

The replay stays clean. It learns nothing about ``~/.alphalens``: this module
reads the keeper's record and hands the replay a document and a configuration,
which is why it lives in the laboratory rather than in the engine.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import sys
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from alphalens_pipeline.brokers import execution
from alphalens_pipeline.brokers.automanager import costs
from alphalens_pipeline.brokers.automanager.costs import fee_card_for
from alphalens_pipeline.brokers.automanager.entry_trails import fold_entry_trail_lines
from alphalens_pipeline.brokers.automanager.state_paths import entry_trails_path
from alphalens_pipeline.market.calendar import (
    advance_trading_sessions,
    session_close_utc,
    session_open_utc,
)
from broker_contract.trade_intent.codec import supplied_derived_paths
from intent_replay import units
from intent_replay.fx import Fx

__all__ = [
    "ReplayInputs",
    "StatedFacts",
    "authored_document",
    "entry_trail_bps_by_pick",
    "main",
    "replay_inputs",
    "run_configuration",
    "stated_facts",
]

SCHEMA = "alphalens.research.replay_from_pick/v1"

# ``meta.source`` of a hand-written pick. Anything else -- "brief", or the key
# absent -- is the legacy producer shape, whose day-1 anchor is a session later.
_MANUAL_SOURCE = "manual"

_SEGMENT = re.compile(r"([^\[\]]+)((?:\[\d+\])*)")


def _segments(path: str) -> Iterator[str | int]:
    """``spec.tp_tranches[0].r_multiple`` -> ``spec``, ``tp_tranches``, 0,
    ``r_multiple``. The grammar is the one ``supplied_derived_paths`` writes."""
    for part in path.split("."):
        match = _SEGMENT.fullmatch(part)
        if match is None:  # pragma: no cover - the producer writes this grammar
            raise ValueError(f"cannot read the path {path!r}")
        yield match.group(1)
        for index in re.findall(r"\[(\d+)\]", match.group(2)):
            yield int(index)


def _remove(document: Any, path: str) -> None:
    *parents, leaf = _segments(path)
    node = document
    for segment in parents:
        node = node[segment]
    del node[leaf]


def _epoch_ms(moment: dt.datetime) -> int:
    return int(moment.timestamp() * 1000)


def authored_document(plan: Mapping[str, Any]) -> dict[str, Any]:
    """The pick's plan as its AUTHOR wrote it: the armed document minus every
    field a door computes.

    A journalled pick is the armed form, so it carries ``intent_id``,
    ``meta.armed_ts`` and each tranche's ``r_multiple``. The replay refuses
    exactly those (``derived_field_supplied``), and WHICH they are is not
    repeated here -- ``supplied_derived_paths`` is asked, the same function the
    replay's own door asks, so this cannot drift from the refusal it exists to
    avoid.

    The argument is not mutated: a pick's record is read by other callers too.
    """
    document = json.loads(json.dumps(plan))
    for path in supplied_derived_paths(document):
        _remove(document, path)
    return document


def run_configuration(
    document: Mapping[str, Any],
    *,
    fx_rate: float | None,
    fx_rate_asof: str | None,
    fx_rate_source: str | None,
    instrument_currency: str,
    entry_trail_bps: int | None,
) -> dict[str, Any]:
    """The replay's configuration block for ``document``, from stated facts.

    Five facts arrive as arguments because no document path carries them, and
    every one of them is recorded in the pick's own journals: the rate the
    daemon sized with, its as-of time and its source, the currency the broker
    resolved for the instrument, and the entry-trail distance the drain priced.
    They are arguments rather than reads so this function stays pure and the
    reader that fetches them is testable on its own.

    Everything else is DERIVED, and the three values that came out wrong when
    this block was assembled by hand are each derived from their real source:

    * the calendar is asked for ``instrument.mic``, never for a venue the
      author had in mind. XNAS and XNYS share their session times, so a wrong
      MIC between those two moves nothing and is invisible;
    * the cost constants are imported from the daemon that applies them, so the
      design memo's worked example (``exit_edge_min_bps`` 5.0, where production
      declares 50.0) cannot come back;
    * the fee card is the venue's own (``fee_card_for``), so a non-US pick is
      not priced on the US schedule.

    Three keys are stated as absent, each for a reason rather than for want of
    a value. ``ceiling_price`` because the replay accepts it and does not read
    it, and only the producer-side bracket builder reads one on the live side.
    ``time_stop_t`` because this rail places no live time stop. ``oco`` false
    because v1 models no OCO pair and the block travels in the result.
    """
    meta = document["meta"]
    spec = document["spec"]
    mic = document["instrument"]["mic"]
    account_currency = spec["size"]["currency"]
    trade_date = dt.date.fromisoformat(meta["trade_date"])
    ttl_days = spec["order_ttl_days"]

    # The daemon's own two anchors (``control_loop._day1_anchor``: step 0 for a
    # manual pick whose trade_date IS the arm date, step 1 for a brief, which
    # holds T-1 data and trades the next session; an absent source is a brief
    # per the LEGACY(source_brief) entry).
    manual = meta.get("source") == _MANUAL_SOURCE
    day1 = advance_trading_sessions(trade_date, 0 if manual else 1, exchange=mic)
    deadline = advance_trading_sessions(trade_date, ttl_days, exchange=mic)
    anchor_rule = (
        "source=manual counts trade_date itself as day 1"
        if manual
        else "source=brief counts the session after trade_date as day 1"
    )

    card = fee_card_for(instrument_currency, exchange_mic=mic)
    if card is None:
        raise ValueError(f"no fee card for {mic} / {instrument_currency}; the venue is not priced")

    fx = Fx(
        account_currency=account_currency,
        instrument_currency=instrument_currency,
        rate=fx_rate,
        round_trip_cost_rate=costs.FX_ROUND_TRIP_RATE,
        sizing_buffer_pct=execution._FX_SIZING_BUFFER_PCT,
    )
    fx_block: dict[str, Any] = {
        "instrument_currency": {
            "kind": "venue_settlement_currency",
            "value": instrument_currency,
            "unit": units.ISO_4217,
            "source": "keeper.instrument.instrument_currency",
            "formula": f"the currency the broker resolved for this pick on {mic}",
        }
    }
    if fx.applies:
        fx_block["mid_rate"] = {
            "kind": "fx_mid",
            "value": fx_rate,
            # The direction token comes from the engine's own helper, so the
            # pair cannot be spelled one way here and read the other way there.
            "unit": fx.pair_unit(),
            "source": fx_rate_source,
            "formula": f"the rate this pick's own sizing record carries, as of {fx_rate_asof}",
        }
        fx_block["round_trip_cost_rate"] = {
            "value": costs.FX_ROUND_TRIP_RATE,
            "unit": units.FRACTION,
        }
        fx_block["sizing_buffer_pct"] = {
            "value": execution._FX_SIZING_BUFFER_PCT,
            "unit": units.PERCENT,
        }

    return {
        "entry_deadline": {
            "kind": "order_ttl_sessions",
            "value": _epoch_ms(session_close_utc(deadline, exchange=mic)),
            "unit": units.EPOCH_MS_UTC,
            "source": "spec.order_ttl_days",
            "formula": (
                f"session_close_utc(advance_trading_sessions({trade_date}, {ttl_days}, {mic}))"
            ),
        },
        "walk_start": {
            "kind": "day1_session_open",
            "value": _epoch_ms(session_open_utc(day1, exchange=mic)),
            "unit": units.EPOCH_MS_UTC,
            "source": "meta.source + meta.trade_date",
            "formula": f"session_open_utc({day1}, {mic}); {anchor_rule}",
        },
        "entry_trail_bps": entry_trail_bps,
        "ceiling_price": None,
        "time_stop_t": None,
        "oco": False,
        "fx": fx_block,
        "costs": {
            "commission_rate": {"value": card.commission_rate, "unit": units.FRACTION},
            # The card is the VENUE's, so its minimum is already denominated in
            # the notional's own currency -- which is what makes the flag true.
            "min_commission": {"value": card.min_commission, "unit": instrument_currency},
            "min_commission_applies": True,
            "exit_edge_min_bps": {"value": costs.EXIT_EDGE_MIN_BPS, "unit": units.BPS},
        },
    }


@dataclass(frozen=True, slots=True)
class StatedFacts:
    """The facts no document path carries, read off the pick's own record.

    The three FX fields are ``None`` exactly when the instrument settles in the
    account's own currency, which is also when the configuration refuses them:
    the engine's key set is conditional on the two codes agreeing.
    """

    fx_rate: float | None
    fx_rate_asof: str | None
    fx_rate_source: str | None
    instrument_currency: str


@dataclass(frozen=True, slots=True)
class ReplayInputs:
    """The two arguments a replay run takes."""

    document: dict[str, Any]
    configuration: dict[str, Any]


def _measured(node: Mapping[str, Any], path: str) -> Any:
    """One ``Measured`` field's value, refusing a hole.

    A record publishes an absent fact as ``{"value": null, "null_reason":
    ...}``, so the reason is part of the refusal: "the daemon never journalled
    it" and "the journal line was malformed" send a reader to different places.
    """
    value = node.get("value")
    if value is None:
        raise ValueError(f"{path} is not in this pick's record ({node.get('null_reason')})")
    return value


def stated_facts(trade: Mapping[str, Any]) -> StatedFacts:
    """The stated facts of one ``broker trades`` record.

    The account side is the record's own ``instrument.sizing_currency`` rather
    than the document's ``spec.size.currency``: a pre-#1467 percent-size
    document states no currency at all, and this function is read by callers
    that have not yet refused such a record.
    """
    instrument = trade["instrument"]
    instrument_currency = _measured(
        instrument["instrument_currency"], "instrument.instrument_currency"
    )
    account_currency = _measured(instrument["sizing_currency"], "instrument.sizing_currency")
    if instrument_currency == account_currency:
        return StatedFacts(
            fx_rate=None,
            fx_rate_asof=None,
            fx_rate_source=None,
            instrument_currency=instrument_currency,
        )
    sizing_fx = trade["sizing_fx"]
    return StatedFacts(
        fx_rate=_measured(sizing_fx["rate"], "sizing_fx.rate"),
        fx_rate_asof=_measured(sizing_fx["asof"], "sizing_fx.asof"),
        fx_rate_source=_measured(sizing_fx["source"], "sizing_fx.source"),
        instrument_currency=instrument_currency,
    )


def _assert_comparable(trade: Mapping[str, Any]) -> None:
    """Refuse a record the report itself marks as not comparable.

    One function for two callers, so the command can refuse BEFORE it reads any
    journal: a pick that can never be replayed is never reported as a missing
    trail distance instead.
    """
    exclusions = trade.get("replay_exclusions") or []
    if exclusions:
        raise ValueError(
            f"{trade['pick_key']} cannot be compared with a replay: {', '.join(exclusions)}"
        )


def replay_inputs(trade: Mapping[str, Any], *, entry_trail_bps: int | None) -> ReplayInputs:
    """Both replay inputs for one ``broker trades`` record.

    A record carrying any ``replay_exclusions`` is REFUSED, and the reasons are
    named. That list is the record's own verdict on comparability: a position
    closed by hand, one still open, a plan shape the replay does not model.
    Replaying one of those produces a number that reads as a divergence of the
    tool and is a divergence of the trade.

    ``entry_trail_bps`` stays an argument because it is the one configuration
    value this record does NOT carry; it comes from the entry-trail journal via
    :func:`entry_trail_bps_by_pick`.
    """
    _assert_comparable(trade)
    facts = stated_facts(trade)
    document = authored_document(trade["plan"])
    return ReplayInputs(
        document=document,
        configuration=run_configuration(
            document,
            fx_rate=facts.fx_rate,
            fx_rate_asof=facts.fx_rate_asof,
            fx_rate_source=facts.fx_rate_source,
            instrument_currency=facts.instrument_currency,
            entry_trail_bps=entry_trail_bps,
        ),
    )


def entry_trail_bps_by_pick(lines: Iterable[str]) -> dict[str, int | None]:
    """The trail distance each pick's own ``watch_open`` line journalled.

    The value is frozen at DRAIN time. The arm itself re-reads the ambient flag
    (``entry_trails.KEY_DISTANCE``), so the absolute distance an order actually
    got is the ``trail_armed`` line's ``distance`` and is not reproduced by
    dividing it back by a price -- which is why the stated bps is READ here and
    not reconstructed.

    A journalled ``0`` is the live flag's way of saying OFF (the three-limit
    ladder), and the configuration states OFF as ``None`` and refuses a ``0``.
    So a pick WITH a line and no trail maps to ``None``, which a caller tells
    apart from a pick with no line at all by the key being present.
    """
    distances: dict[str, int] = {}
    for state in fold_entry_trail_lines(lines).tiers.values():
        record = state.watch_open
        if record is None:
            continue
        pick = record.get("pick_key")
        distance = record.get("d_bps")
        if not isinstance(pick, str) or not isinstance(distance, int):
            continue
        earlier = distances.get(pick)
        if earlier is not None and earlier != distance:
            raise ValueError(
                f"{pick}: its tiers journalled two trail distances ({earlier} and "
                f"{distance}); no single value is this run's"
            )
        distances[pick] = distance
    return {pick: distance or None for pick, distance in distances.items()}


# ---------------------------------------------------------------------------
# The command. See ``_USAGE`` for the published surface.
# ---------------------------------------------------------------------------

_USAGE = """USAGE
  python -m scripts.replay_from_pick TRADES_JSON PICK_KEY [options]

  TRADES_JSON is the output of `alphalens broker trades --format json`, or `-`
  to read it from stdin. The report decides comparability, so the command needs
  no broker of its own and runs offline.

OPTIONS
  --out DIR              where the two files are written (default: .)
  --entry-trails PATH    the entry-trail journal (default: the report env's own)
  --entry-trail-bps N    state the distance instead of reading it; 0 means OFF
  --bars PATH            bars to put in the suggested command
  --format human|json    default human

OUTPUT
  json: one object on stdout with `document_path`, `config_path`, the stated
  `facts`, the whole `configuration` and a runnable `replay_argv`.

EXIT CODES
  0 ok, 1 the pick cannot be replayed, 2 usage, 4 the report has no such pick

EXAMPLES
  alphalens broker trades --env live --format json > /tmp/trades.json
  python -m scripts.replay_from_pick /tmp/trades.json VST:2026-09-21 --out /tmp/vst
"""


def _report_trades(source: str) -> tuple[str, dict[str, Mapping[str, Any]]]:
    text = sys.stdin.read() if source == "-" else pathlib.Path(source).read_text()
    report = json.loads(text)
    trades = report.get("trades")
    if not isinstance(trades, list):
        raise ValueError(f"{source}: not a `broker trades` report (no `trades` array)")
    try:
        picks = {trade["pick_key"]: trade for trade in trades}
    except (KeyError, TypeError) as exc:
        raise ValueError(f"{source}: a row of `trades` carries no `pick_key`") from exc
    return report.get("env") or "live", picks


def _trail_distance(
    pick: str, *, stated: int | None, journal: pathlib.Path, env: str
) -> int | None:
    """The distance for ``pick``: stated if the caller stated one, else the
    pick's own ``watch_open`` line.

    An absent line is REFUSED rather than read as OFF. A run with the wrong
    entry policy answers a different question, and the ladder and the trail are
    different policies -- not a present and an absent setting.
    """
    if stated is not None:
        return stated or None
    lines = journal.read_text().splitlines() if journal.exists() else []
    distances = entry_trail_bps_by_pick(lines)
    if pick not in distances:
        raise ValueError(
            f"{pick}: {journal} holds no watch_open line for it, so the entry-trail "
            f"distance is unknown (env={env}); state it with --entry-trail-bps (0 = OFF)"
        )
    return distances[pick]


def main(argv: list[str] | None = None) -> int:
    """Write both replay inputs for one pick of a `broker trades` report."""
    parser = argparse.ArgumentParser(
        prog="replay_from_pick",
        usage=argparse.SUPPRESS,
        description=_USAGE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("trades_json")
    parser.add_argument("pick_key")
    parser.add_argument("--out", default=".")
    parser.add_argument("--entry-trails", default=None)
    parser.add_argument("--entry-trail-bps", type=int, default=None)
    parser.add_argument("--bars", default=None)
    parser.add_argument("--format", choices=("human", "json"), default="human")
    args = parser.parse_args(argv)

    try:
        env, trades = _report_trades(args.trades_json)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"replay_from_pick: {exc}", file=sys.stderr)
        return 2
    trade = trades.get(args.pick_key)
    if trade is None:
        print(
            f"replay_from_pick: {args.pick_key} is not in this report "
            f"({len(trades)} picks, env={env})",
            file=sys.stderr,
        )
        return 4

    journal = pathlib.Path(args.entry_trails) if args.entry_trails else entry_trails_path(env)
    try:
        _assert_comparable(trade)
        distance = _trail_distance(
            args.pick_key, stated=args.entry_trail_bps, journal=journal, env=env
        )
        inputs = replay_inputs(trade, entry_trail_bps=distance)
    except (OSError, ValueError) as exc:
        print(f"replay_from_pick: {exc}", file=sys.stderr)
        return 1

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stem = args.pick_key.replace(":", "_")
    document_path = out / f"{stem}.document.json"
    config_path = out / f"{stem}.config.json"
    document_path.write_text(json.dumps(inputs.document, indent=1) + "\n")
    config_path.write_text(json.dumps(inputs.configuration, indent=1) + "\n")

    facts = stated_facts(trade)
    answer = {
        "schema": SCHEMA,
        "env": env,
        "pick": args.pick_key,
        "document_path": str(document_path),
        "config_path": str(config_path),
        "facts": {
            "fx_rate": facts.fx_rate,
            "fx_rate_asof": facts.fx_rate_asof,
            "fx_rate_source": facts.fx_rate_source,
            "instrument_currency": facts.instrument_currency,
            "entry_trail_bps": distance,
        },
        "configuration": inputs.configuration,
        "replay_argv": [
            sys.executable,
            "-m",
            "intent_replay",
            "run",
            str(document_path),
            "--config",
            str(config_path),
            "--bars",
            args.bars or "<BARS.json>",
        ],
    }
    if args.format == "json":
        print(json.dumps(answer))
        return 0
    print(f"{args.pick_key} ({env}) -> {document_path} + {config_path}")
    for name, value in answer["facts"].items():
        print(f"  {name}: {value}")
    print("  next: " + " ".join(answer["replay_argv"]))
    return 0


if __name__ == "__main__":  # pragma: no cover - the entry point
    raise SystemExit(main())
