"""Fail when ``uv.lock`` pins a release PyPI has YANKED (#1507).

``pandas==3.0.4`` sat in this lock from 2026-06-29 to 2026-09-21 — a release
withdrawn for "Reported segfaults with datetime-related functionality". Nothing
reported it for three months; it surfaced as a side-effect warning ``uv lock``
printed during an unrelated CVE bump.

Neither existing gate can see a yank. ``uv lock --check`` resolves FROM the lock
without querying the index (measured: exit 0, zero occurrences of "yanked").
``pip-audit`` scans advisory databases, and a maintainer withdrawing a bad build
usually files no CVE. A yank is the maintainers saying "do not use this build",
often for exactly the reasons a test suite cannot see: a wheel built against the
wrong ABI, a packaging mistake, a data-corrupting bug found after release.

Contract
--------
* **Report, never mutate.** This reads the lock and the index. Bumping the lock
  is a human decision with its own PR; a check that edited ``uv.lock`` would
  make a scheduled job change what the VPS installs.
* **"Could not check" is never "clean".** Any pin that does not get a definite
  answer forces :data:`EXIT_INCOMPLETE`. This deliberately departs from the
  ``run_probes`` majority rule used by the live vendor probes: tolerating a
  minority of transients is right when one flaky call proves nothing about a
  vendor's contract, and wrong here, because this is a completeness check over
  a fixed set — one tolerated 429 is one package nobody looked at, and the yank
  could be sitting in exactly that one.
* **An index other than PyPI is refused, not skipped.** The lock has exactly one
  today; if that ever changes it must be a decision, not a silent hole.
* Only the yank flag. Not deprecation, not removal from the index.

HTTP lives in this file rather than behind a canonical vendor client. The
one-client-per-vendor doctrine governs repo-authored clients for the data
pipeline, where it buys quota tracking and one coherent request stream; PyPI has
no key and no quota we track, ``uv`` and ``pip-audit`` already talk to it as
tools, and this is a once-weekly CI gate rather than a data source. Stdlib only,
so the job needs no ``uv sync`` and cannot be killed by a ``--frozen`` mismatch
before the check runs.

Exit codes: ``0`` every pin checked and clean, ``1`` at least one yanked,
``7`` the run could not check every pin. A yank outranks an incomplete run —
it is the actionable state — and the unchecked pins are still listed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

CLEAN = "clean"
YANKED = "yanked"
UNCHECKED = "unchecked"

EXIT_OK = 0
EXIT_YANKED = 1
EXIT_INCOMPLETE = 7

#: The one index this lock resolves from. Anything else is refused.
PYPI_SIMPLE = "https://pypi.org/simple"

#: Source kinds that genuinely have no PyPI release to ask about: a git ref, a
#: workspace member, a local path. Skipping these is a decision; skipping
#: anything else would be an accident, so the set is explicit.
_NO_PYPI_RECORD = frozenset({"git", "editable", "directory", "path", "virtual"})

#: Per-version metadata. 116 KB against 2.1 MB for the whole-package form, and
#: it carries ``info.yanked`` / ``info.yanked_reason`` for that exact version.
_VERSION_JSON = "https://pypi.org/pypi/{name}/{version}/json"

_USER_AGENT = "alphalens-yank-check (+https://github.com/kamilpajak/AlphaLens)"
_TIMEOUT_S = 15.0
_ATTEMPTS = 3
_BACKOFF_S = (1.0, 4.0)

#: PyPI allows a yank with an empty reason. Reading that as "clean" would hide
#: the withdrawal, so it gets a stand-in the report can print.
_NO_REASON = "(no reason given)"

_REPO_ROOT = Path(__file__).resolve().parents[3]


class LockShapeError(ValueError):
    """The lockfile does not look the way this check knows how to read.

    Its own category because the failure mode it guards is the one this whole
    gate exists to stop: a lock entry nobody looked at, in a run that reported
    clean. Anything unrecognised here is loud.
    """


class UnexpectedIndexError(LockShapeError):
    """The lock resolves a package from an index this check does not know."""


class UnrecognisedSourceError(LockShapeError):
    """A ``[[package]]`` source is neither PyPI nor a kind known to have no PyPI record."""


class PermanentFetchError(RuntimeError):
    """The answer cannot be trusted and retrying will not help (404, reshaped body)."""


class TransientFetchError(RuntimeError):
    """The answer did not arrive this time (429, timeout, reset, 5xx)."""


class Pin(NamedTuple):
    name: str
    version: str


class PinResult(NamedTuple):
    pin: Pin
    state: str
    reason: str | None


@dataclass(frozen=True)
class Report:
    exit_code: int
    checked: int
    yanked: list[tuple[str, str, str]]
    unchecked: list[tuple[str, str, str]]

    def as_json(self) -> dict[str, Any]:
        """The document the workflow reads with ``jq`` to build an issue body."""
        return {
            "exit_code": self.exit_code,
            "checked": self.checked,
            "yanked": [{"name": n, "version": v, "reason": r} for n, v, r in self.yanked],
            "unchecked": [{"name": n, "version": v, "why": r} for n, v, r in self.unchecked],
        }

    def as_text(self) -> str:
        lines: list[str] = []
        for name, version, reason in self.yanked:
            lines.append(f"YANKED  {name}=={version}  — {reason}")
        for name, version, why in self.unchecked:
            lines.append(f"UNCHECKED  {name}=={version}  — {why}")
        lines.append(
            f"checked {self.checked} pin(s): {len(self.yanked)} yanked, {len(self.unchecked)} unchecked"
        )
        return "\n".join(lines)


def registry_pins(lock: dict[str, Any]) -> list[Pin]:
    """Every ``(name, version)`` the lock resolves from PyPI. Pure.

    Git and editable entries are dropped: the git dep's version string means
    nothing to the index, and the editable entries are this repo. That is the
    same population ``pip-audit`` reaches through ``--no-emit-workspace`` plus
    ``grep -v '@ git+'``.
    """
    if "package" not in lock:
        raise LockShapeError(
            "lockfile carries no [[package]] entries; the format moved and this "
            "check would otherwise report a clean run over nothing"
        )
    pins: list[Pin] = []
    for package in lock["package"]:
        source = package.get("source", {})
        kinds = set(source)
        if "registry" in kinds:
            index = source["registry"]
            if index != PYPI_SIMPLE:
                raise UnexpectedIndexError(
                    f"{package.get('name')} resolves from {index!r}, not {PYPI_SIMPLE!r}; "
                    "teach this check about that index rather than skipping it"
                )
            pins.append(Pin(package["name"], package["version"]))
        elif not kinds & _NO_PYPI_RECORD:
            # The failure this gate exists to stop, one level up: uv renaming or
            # adding a source kind would make `source.get("registry")` return
            # None for real PyPI pins, which a `continue` would drop silently
            # while the run still said "clean". Unrecognised is loud.
            raise UnrecognisedSourceError(
                f"{package.get('name')} has source keys {sorted(kinds) or ['<none>']}, "
                f"none of which this check knows; expected 'registry' or one of "
                f"{sorted(_NO_PYPI_RECORD)}"
            )
    return pins


def yank_state_from_payload(payload: dict[str, Any]) -> tuple[str, str | None]:
    """Read one ``/pypi/<name>/<version>/json`` body. Pure.

    A missing or non-boolean ``yanked`` is PERMANENT, not transient: a reshaped
    payload is the day this gate would otherwise go quietly blind, and retrying
    it would only turn a visible break into a slow one.
    """
    info = payload.get("info")
    if not isinstance(info, dict) or not isinstance(info.get("yanked"), bool):
        raise PermanentFetchError("payload carries no boolean info.yanked")
    if not info["yanked"]:
        return CLEAN, None
    reason = info.get("yanked_reason")
    return YANKED, reason if isinstance(reason, str) and reason else _NO_REASON


def verdict(results: list[PinResult]) -> Report:
    """Turn per-pin outcomes into an exit code and the report. Pure."""
    yanked = [
        (r.pin.name, r.pin.version, r.reason or _NO_REASON) for r in results if r.state == YANKED
    ]
    unchecked = [
        (r.pin.name, r.pin.version, r.reason or "unknown") for r in results if r.state == UNCHECKED
    ]
    if yanked:
        code = EXIT_YANKED
    elif unchecked:
        code = EXIT_INCOMPLETE
    else:
        code = EXIT_OK
    return Report(exit_code=code, checked=len(results), yanked=yanked, unchecked=unchecked)


def _read_json(url: str, *, opener: Any) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with opener(request, timeout=_TIMEOUT_S) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise PermanentFetchError(f"HTTP 404 for {url}") from exc
        if exc.code == 429 or exc.code >= 500:
            raise TransientFetchError(f"HTTP {exc.code}") from exc
        raise PermanentFetchError(f"HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise TransientFetchError(f"{type(exc).__name__}: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise PermanentFetchError(f"malformed JSON from {url}") from exc
    except TimeoutError as exc:
        raise TransientFetchError("timeout") from exc


def fetch_yank(pin: Pin, *, opener: Any = None, sleep: Any = time.sleep) -> PinResult:
    """Ask PyPI about one pin. The only IO in this module.

    Retries a transient answer a bounded number of times; a permanent one is
    returned as UNCHECKED immediately, because repeating a 404 or a reshaped
    body cannot change it — and UNCHECKED is what keeps it out of a clean run.
    """
    opener = opener or urllib.request.urlopen
    url = _VERSION_JSON.format(name=pin.name, version=pin.version)
    last = "unknown"
    for attempt in range(_ATTEMPTS):
        try:
            state, reason = yank_state_from_payload(_read_json(url, opener=opener))
            return PinResult(pin, state, reason)
        except PermanentFetchError as exc:
            return PinResult(pin, UNCHECKED, str(exc))
        except TransientFetchError as exc:
            last = str(exc)
            if attempt < _ATTEMPTS - 1:
                # Clamped rather than indexed straight: lowering _ATTEMPTS or
                # shortening the backoff tuple should change the pace, never
                # raise IndexError inside the retry.
                sleep(_BACKOFF_S[min(attempt, len(_BACKOFF_S) - 1)])
    return PinResult(pin, UNCHECKED, last)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--lock", type=Path, default=_REPO_ROOT / "uv.lock", help="lockfile to check"
    )
    parser.add_argument(
        "--report-json", type=Path, default=None, help="write the machine-readable report here"
    )
    args = parser.parse_args(argv)

    try:
        with args.lock.open("rb") as handle:
            pins = registry_pins(tomllib.load(handle))
    except LockShapeError as exc:
        # A lockfile this check cannot read is an INCOMPLETE run, not a clean
        # one, and it reads better as one line than as a traceback: the reader
        # has to teach the script a new shape, not debug it.
        report = Report(exit_code=EXIT_INCOMPLETE, checked=0, yanked=[], unchecked=[])
        if args.report_json is not None:
            args.report_json.write_text(json.dumps(report.as_json(), indent=2) + "\n")
        print(f"UNREADABLE LOCK  {args.lock}  — {exc}")
        return EXIT_INCOMPLETE

    results = [fetch_yank(pin) for pin in pins]
    report = verdict(results)

    if args.report_json is not None:
        args.report_json.write_text(json.dumps(report.as_json(), indent=2) + "\n")
    print(report.as_text())
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
