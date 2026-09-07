"""Read-only probe: does the Saxo LIVE market-data account hold the REAL-TIME
entitlement for a venue?  (#1355, reusable for XWAR / XETR / XPAR / ...)

    .venv/bin/python apps/alphalens-research/scripts/probe_saxo_live_entitlement.py KER@XPAR ALO@XPAR RHM@XETR

For each ``TICKER@MIC`` the script resolves the uic through the shared
MIC -> Saxo ExchangeId map and reads ONE ``/trade/v1/infoprices`` snapshot on
LIVE — the same non-elevating REST path the daemon's day-1 gap gate uses.
The quote's ``DelayedByMinutes`` is the verdict: 0 during an open session is
the entitlement, a positive delay is the gap the runbook's "inert, never
places" note is about.

It NEVER calls ``elevate_session``: the production price reader holds the one
elevated Saxo session per LIVE login, and a second elevation from here would
demote it to delayed quotes (the 2026-08 two-streams incident class).

Exit codes (agent-friendly, one line per target on stdout, logs on stderr):
  0  every target real-time during an open session (entitled)
  4  at least one target delayed during an open session (entitlement gap)
  8  inconclusive for at least one target and no gap seen (market closed,
     ticker unresolved, read failed) — never a "yes"
  2  usage (a target is not TICKER@MIC)

Needs the LIVE market-data env (SAXO_LIVE_APP_KEY / _SECRET /
_AUTH_REDIRECT_URL) + a bootstrapped LIVE token store; on the VPS:
``set -a && . /etc/alphalens/env && set +a`` first.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import sys
from collections.abc import Callable, Sequence
from typing import Any

EXIT_ENTITLED = 0
EXIT_USAGE = 2
EXIT_GAP = 4
EXIT_INCONCLUSIVE = 8

VERDICT_REALTIME = "real-time"
VERDICT_DELAYED = "delayed"
VERDICT_INCONCLUSIVE = "inconclusive"


@dataclasses.dataclass(frozen=True)
class ProbeResult:
    target: str
    verdict: str
    detail: str

    def line(self) -> str:
        return f"{self.target}: {self.verdict} — {self.detail}"


def parse_target(raw: str) -> tuple[str, str]:
    ticker, sep, mic = raw.strip().partition("@")
    if not sep or not ticker or not mic:
        raise ValueError(f"target {raw!r} is not TICKER@MIC")
    return ticker.upper(), mic.upper()


def classify(quote: dict[str, Any]) -> tuple[str, str]:
    """Verdict + detail for one infoprice ``Quote`` block.

    The delay flag is meaningful ONLY while the venue is open: a closed
    market reports stale/delayed quotes regardless of entitlement, so it is
    inconclusive, never a "real-time" verdict."""
    state = str(quote.get("MarketState") or "")
    delay = quote.get("DelayedByMinutes")
    source = quote.get("PriceSource")
    if state != "Open":
        return VERDICT_INCONCLUSIVE, f"market state {state or 'unknown'!r} (source {source})"
    if not isinstance(delay, int | float) or isinstance(delay, bool):
        return VERDICT_INCONCLUSIVE, f"no DelayedByMinutes in the quote (source {source})"
    if delay > 0:
        return VERDICT_DELAYED, f"delayed {int(delay)} min (source {source})"
    return VERDICT_REALTIME, f"DelayedByMinutes 0 (source {source})"


def probe(client: Any, targets: Sequence[tuple[str, str]]) -> list[ProbeResult]:
    results: list[ProbeResult] = []
    for ticker, mic in targets:
        label = f"{ticker}@{mic}"
        try:
            uic = client.resolve_uic(ticker, exchange_mic=mic)
        except Exception as exc:  # a read failure is a verdict, not a crash
            results.append(ProbeResult(label, VERDICT_INCONCLUSIVE, f"resolve failed: {exc}"))
            continue
        if uic is None:
            results.append(ProbeResult(label, VERDICT_INCONCLUSIVE, "unresolved on LIVE"))
            continue
        try:
            quote = client.get_stock_infoprice(int(uic)).get("Quote") or {}
        except Exception as exc:
            results.append(
                ProbeResult(label, VERDICT_INCONCLUSIVE, f"uic {uic}: infoprice failed: {exc}")
            )
            continue
        verdict, detail = classify(quote)
        results.append(ProbeResult(label, verdict, f"uic {uic}: {detail}"))
    return results


def exit_code(results: Sequence[ProbeResult]) -> int:
    verdicts = {r.verdict for r in results}
    if VERDICT_DELAYED in verdicts:
        return EXIT_GAP
    if VERDICT_INCONCLUSIVE in verdicts or not results:
        return EXIT_INCONCLUSIVE
    return EXIT_ENTITLED


def _default_client_factory() -> Any:
    from alphalens_pipeline.data.alt_data.saxo_marketdata_auth import (
        LiveAuthConfig,
        LiveTokenProvider,
    )
    from alphalens_pipeline.data.alt_data.saxo_marketdata_client import SaxoMarketDataClient

    return SaxoMarketDataClient(token_provider=LiveTokenProvider(LiveAuthConfig.from_env()))


def _close(client: Any) -> None:
    # Best-effort: a teardown failure must not turn a computed verdict into a crash.
    with contextlib.suppress(Exception):
        client.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="probe_saxo_live_entitlement",
        description="Read-only LIVE market-data entitlement probe (never elevates the session).",
    )
    parser.add_argument("targets", nargs="+", metavar="TICKER@MIC")
    return parser


def main(
    argv: Sequence[str] | None = None, *, client_factory: Callable[[], Any] | None = None
) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        targets = [parse_target(raw) for raw in args.targets]
    except ValueError as exc:
        print(f"usage: {exc}; expected TICKER@MIC, e.g. KER@XPAR", file=sys.stderr)
        return EXIT_USAGE
    client = (client_factory or _default_client_factory)()
    try:
        results = probe(client, targets)
    finally:
        _close(client)
    for result in results:
        print(result.line())
    return exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
