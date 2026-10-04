"""JSON Schema of the ``alphalens broker trades`` envelope, v1 (#1701, memo §3).

Generated from the builder's vocabulary tuples in :mod:`.trades`, never
hand-edited, so an enum value added to the builder reaches the published file
in the same change (§3.0). The published file lives beside the contract
README tables::

    python -m alphalens_pipeline.brokers.automanager.trades_schema \\
        --write apps/alphalens-broker-contract/docs/broker-trades-v1.schema.json

Two variants come from one description:

* the PUBLISHED schema is open: objects accept keys it does not list, because
  v1 may grow optional fields and a consumer validating a later v1 body with
  this file must not fail on them;
* the STRICT variant (``strict=True``) closes every object. Tests validate the
  command's real output against it, which is what makes the published file
  describe every field the command emits rather than a subset.

Enums are closed in both: they list the values of THIS release. A consumer
must still treat an unknown value as unknown (§3.0), because a later v1 may add
one.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from alphalens_pipeline.brokers.automanager import trades

_TIME_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
_DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
# Currency code (money), `shares`, `s`, `%`, `R`, or `price:<ccy>` (§3.3, §5).
_UNIT_PATTERN = r"^(shares|s|%|R|[A-Z]{3}|price:[A-Z]{3})$"
_ENVIRONMENTS = ("sim", "live")
_JOURNALS = ("picks", "entry_trails", "standalone_stops", "submissions")


def _ref(name: str) -> dict[str, Any]:
    return {"$ref": f"#/$defs/{name}"}


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


def _enum(values: Sequence[str], description: str) -> dict[str, Any]:
    return {"type": "string", "enum": list(values), "description": description}


class _Builder:
    """Holds the strictness so every object is closed or open the same way."""

    def __init__(self, *, strict: bool) -> None:
        self.strict = strict

    def obj(
        self,
        properties: dict[str, Any],
        *,
        description: str | None = None,
        optional: Sequence[str] = (),
    ) -> dict[str, Any]:
        node: dict[str, Any] = {"type": "object"}
        if description:
            node["description"] = description
        node["properties"] = properties
        node["required"] = [name for name in properties if name not in optional]
        if self.strict:
            node["additionalProperties"] = False
        return node

    def measured(self) -> dict[str, Any]:
        node = self.obj(
            {
                "value": {
                    "type": ["string", "number", "null"],
                    "description": "The value; null exactly when null_reason is set.",
                },
                "unit": {
                    "anyOf": [{"type": "string", "pattern": _UNIT_PATTERN}, {"type": "null"}],
                    "description": "Currency code for money, 'shares', 's', '%', 'R', "
                    "'price:<ccy>' for a price; null for a time, a rate, an identifier "
                    "or a code.",
                },
                "source": _ref("Source"),
                "ref": {
                    "type": ["string", "null"],
                    "description": "The row it came from: order:<id>, trade:<id>, "
                    "line:<journal>:<kind>, or the plan path.",
                },
                "null_reason": _nullable(_ref("NullReason")),
            },
            description="Every price, quantity, time and money value (§3.3).",
        )
        node["oneOf"] = [
            {
                "properties": {"value": {"type": "null"}, "null_reason": _ref("NullReason")},
            },
            {
                "properties": {
                    "value": {"type": ["string", "number"]},
                    "null_reason": {"type": "null"},
                },
            },
        ]
        return node

    def measured_time(self) -> dict[str, Any]:
        return {
            "allOf": [
                _ref("Measured"),
                {
                    "properties": {
                        "value": _nullable(_ref("Time")),
                        "unit": {"type": "null"},
                    }
                },
            ],
            "description": "A Measured whose value is a time (§3.1).",
        }

    def fill_properties(self) -> dict[str, Any]:
        return {
            "order_id": {"type": "string"},
            "external_reference": {"type": ["string", "null"]},
            "venue_time": _ref("MeasuredTime"),
            "price": _ref("Measured"),
            "qty": _ref("Measured"),
            "executions": {"type": "array", "items": _ref("Execution")},
            "detected_at": _ref("MeasuredTime"),
            "position_id": {"type": ["string", "null"]},
            "related_position_id": {"type": ["string", "null"]},
            "fees": _ref("Fees"),
            "realized_fx": _ref("RealizedFx"),
        }

    def defs(self) -> dict[str, Any]:
        fill = self.fill_properties()
        return {
            "Time": {
                "type": "string",
                "pattern": _TIME_PATTERN,
                "description": "RFC 3339 UTC with milliseconds and Z.",
            },
            "Source": _enum(trades.SOURCES, "Source ROLE of a value (§3.3)."),
            "NullReason": _enum(trades.NULL_REASONS, "Why a value is null (§3.3)."),
            "ExitReason": _enum(trades.EXIT_REASONS, "Why a position exited (§4.4)."),
            "Attribution": _enum(trades.ATTRIBUTIONS, "How a fill's owner was decided (§4.5)."),
            "State": _enum(trades.STATES, "The pick's state (§4.6)."),
            "StateReason": _enum(trades.STATE_REASONS, "Why never_filled / unresolved."),
            "WarningCode": _enum(trades.WARNING_CODES, "A record warning."),
            "Terminal": _enum(trades.TERMINALS, "How an entry tier ended."),
            "TierPath": _enum(trades.TIER_PATHS, "How an entry tier was placed."),
            "SourceStatus": _enum(trades.SOURCE_STATUSES, "Whether a source was read."),
            "SourceReason": _enum(trades.SOURCE_REASONS, "Why a source was not read."),
            "ReplayExclusion": _enum(
                trades.REPLAY_EXCLUSIONS,
                "Why a record cannot be compared with an intent-replay run.",
            ),
            "Measured": self.measured(),
            "MeasuredTime": self.measured_time(),
            "SourceRead": self.obj(
                {
                    "status": _ref("SourceStatus"),
                    "reason": _nullable(_ref("SourceReason")),
                    "window": _nullable(
                        self.obj(
                            {
                                "from": {"type": "string"},
                                "to": {"type": "string"},
                            },
                            description="The read window: times for the audit, "
                            "dates for the reports (padded one day each side).",
                        )
                    ),
                    "rows": {"type": "integer", "minimum": 0},
                }
            ),
            "Counts": self.obj(
                {name: {"type": "integer", "minimum": 0} for name in (*trades.STATES, "total")}
            ),
            "Execution": self.obj(
                {
                    "trade_id": {"type": "string"},
                    "time": _nullable(_ref("Time")),
                    "price": {"type": ["number", "null"]},
                    "qty": {"type": ["number", "null"]},
                },
                description="One venue.trades_report row of the order.",
            ),
            "Fees": self.obj(
                {
                    "commission": _ref("Measured"),
                    "exchange_fee": _ref("Measured"),
                    "fx_conversion": _ref("Measured"),
                },
                description="Signed as the venue sends them (negative = cost). "
                "commission and exchange_fee in the booking currency; fx_conversion "
                "in the account currency.",
            ),
            "RealizedFx": self.obj(
                {
                    "conversion_rate": _ref("Measured"),
                    "share_amount_acct": _ref("Measured"),
                },
                description="The Share Amount booking: realized rate and the "
                "account-currency cash leg, signed as sent.",
            ),
            "Fill": self.obj(fill, description="A venue fill (entries, exits, unattributed)."),
            "Exit": self.obj(
                {
                    **fill,
                    "reason": _nullable(_ref("ExitReason")),
                    "reason_null_reason": _nullable(_ref("NullReason")),
                    "reason_evidence": {"type": "array", "items": {"type": "string"}},
                    "stop_level_at_fill": _ref("Measured"),
                    "tp_label": {"type": ["string", "null"]},
                    "attributed_qty": _ref("Measured"),
                    "attribution": _nullable(_ref("Attribution")),
                },
                description="A Fill that closed (part of) the pick. reason is null "
                "only offline, with reason_null_reason.",
            ),
            "UnattributedFill": self.obj(
                {
                    **fill,
                    "reason": _nullable(_ref("ExitReason")),
                    "reason_null_reason": _nullable(_ref("NullReason")),
                    "reason_evidence": {"type": "array", "items": {"type": "string"}},
                    "unattributed_qty": _ref("Measured"),
                },
                description="A venue fill on a pick's uic that no pick owns (§4.5). "
                "reason is null only offline, with reason_null_reason.",
            ),
            "EntryTier": self.obj(
                {
                    "tier_index": {"type": "integer", "minimum": 0},
                    "path": _ref("TierPath"),
                    "crid": {"type": "string"},
                    "planned_limit": _ref("Measured"),
                    "planned_qty": _ref("Measured"),
                    "window_end": _ref("MeasuredTime"),
                    "touched_at": _ref("MeasuredTime"),
                    "touch_price": _ref("Measured"),
                    "trigger_order_id": {"type": ["string", "null"]},
                    "armed_at": _ref("MeasuredTime"),
                    "terminal": _ref("Terminal"),
                    "terminal_at": _ref("MeasuredTime"),
                    "terminal_at_source": _nullable(_ref("Source")),
                    "fill": _nullable(_ref("Fill")),
                }
            ),
            "Outcome": self.obj(
                {
                    **{
                        name: _ref("Measured")
                        for name in (
                            "entry_qty",
                            "avg_entry_price",
                            "exit_qty",
                            "avg_exit_price",
                            "notional_spent",
                            "pnl_cash",
                            "pnl_pct_of_spent",
                            "denominator_stop",
                            "risk_per_share",
                            "r_multiple",
                            "holding_seconds",
                        )
                    },
                    "fees": _ref("Fees"),
                    "notional_spent_acct": _ref("Measured"),
                    "pnl_cash_acct": _ref("Measured"),
                    "mfe_lower_bound": _ref("Measured"),
                    "fees_not_included": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(trades.FEES_NOT_INCLUDED)},
                    },
                },
                description="Derived values (§5). Gross: no net figure is derived.",
            ),
            "TradeRecord": self.obj(
                {
                    "pick_key": {"type": "string"},
                    "ticker": {"type": "string"},
                    "trade_date": {"type": "string", "pattern": _DATE_PATTERN},
                    "generation": {"type": "integer", "minimum": 1},
                    "pick_status": {"type": "string", "enum": list(trades.PICK_STATUSES)},
                    "pick_status_at": _ref("MeasuredTime"),
                    "pick_status_reason": {"type": ["string", "null"]},
                    "state": _ref("State"),
                    "state_reason": _nullable(_ref("StateReason")),
                    "plan": {
                        "type": ["object", "null"],
                        "description": "The last armed TradeIntent document, verbatim "
                        "(see trade-intent-v3.schema.json; older lines are schema 1 or 2).",
                    },
                    "plan_null_reason": _nullable(_ref("NullReason")),
                    "plan_armed_at": _ref("MeasuredTime"),
                    "plan_schema_version": {"type": ["string", "null"]},
                    "plan_size_shape": {"type": "string", "enum": list(trades.SIZE_SHAPES)},
                    "plan_source": {"type": "string", "enum": list(trades.PLAN_SOURCES)},
                    "plan_disaster_stop": _ref("Measured"),
                    "placed_stop": _ref("Measured"),
                    "instrument": self.obj(
                        {
                            "uic": _ref("Measured"),
                            "exchange_mic": _ref("Measured"),
                            "instrument_currency": _ref("Measured"),
                            "sizing_currency": _ref("Measured"),
                        }
                    ),
                    "sizing_fx": self.obj(
                        {
                            "rate": _ref("Measured"),
                            "bid": _ref("Measured"),
                            "ask": _ref("Measured"),
                            "asof": _ref("MeasuredTime"),
                            "source": _ref("Measured"),
                        },
                        description="The rate at SIZING time; never used as a realized rate.",
                    ),
                    "side": {"type": ["string", "null"], "enum": [*trades.SIDES, None]},
                    "entries": {"type": "array", "items": _ref("EntryTier")},
                    "exits": {"type": "array", "items": _ref("Exit")},
                    "outcome": _ref("Outcome"),
                    "replay_exclusions": {
                        "type": "array",
                        "items": _ref("ReplayExclusion"),
                        "description": "Empty when the record can be compared with an "
                        "intent-replay run of its plan.",
                    },
                    "warnings": {
                        "type": "array",
                        "items": self.obj(
                            {"code": _ref("WarningCode"), "detail": {"type": "string"}}
                        ),
                    },
                }
            ),
        }

    def root(self) -> dict[str, Any]:
        envelope = self.obj(
            {
                "schema": {"const": trades.TRADES_SCHEMA_ID},
                "env": {"type": "string", "enum": list(_ENVIRONMENTS)},
                "generated_at": _ref("Time"),
                "mode": {"type": "string", "enum": list(trades.MODES)},
                "sources": {
                    "type": "object",
                    "propertyNames": _ref("Source"),
                    "additionalProperties": _ref("SourceRead"),
                    "description": "One entry per source role that was read, skipped "
                    "or not read offline.",
                },
                "snapshot_horizon": _nullable(_ref("Time")),
                "counts": self.obj(
                    {
                        "all": _ref("Counts"),
                        "selected": _ref("Counts"),
                        "malformed": self.obj(
                            {name: {"type": "integer", "minimum": 0} for name in _JOURNALS},
                            description="Malformed lines dropped, per journal, each "
                            "distinct raw line counted once.",
                        ),
                        "unattributed_fills": {"type": "integer", "minimum": 0},
                    },
                    description="all: before --state; selected: after it; both before --limit.",
                ),
                "truncated": {"type": "boolean"},
                "trades": {"type": "array", "items": _ref("TradeRecord")},
                "unattributed_fills": {"type": "array", "items": _ref("UnattributedFill")},
            }
        )
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "urn:alphalens:broker:trades:1",
            "title": "alphalens broker trades",
            "description": "One read-only record per pick (alphalens broker trades "
            "--format json). Generated from alphalens_pipeline.brokers.automanager."
            "trades; never edit by hand. Within v1, optional fields and enum values may "
            "be added, so a consumer treats an unknown enum value as unknown; renaming "
            "or removing a field or value, or changing a type or unit, needs v2. "
            "Vocabularies: apps/alphalens-broker-contract/README.md, 'broker trades'.",
            **envelope,
            "$defs": self.defs(),
        }


def build_schema(*, strict: bool = False) -> dict[str, Any]:
    """The schema as a dict; ``strict`` closes every object (tests only)."""
    return _Builder(strict=strict).root()


def render() -> str:
    """The published file's exact text."""
    return json.dumps(build_schema(), indent=2, ensure_ascii=False) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--write", type=Path, help="write the schema to this path")
    args = parser.parse_args(argv)
    text = render()
    if args.write is None:
        print(text, end="")
    else:
        args.write.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
